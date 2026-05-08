"""KKTIX 頁面操作邏輯。

KKTIX 標準購票流程:
  1. 活動頁 (/events/{id})            — 售票時間到才會出現「立即購票」
  2. 報名頁 (/events/{id}/registrations/new) — 選票 + 同意條款 + 送出
  3. 付款頁 (/orders/{id}/payment 或類似)    — 信用卡 / ATM
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from urllib.parse import urlparse, urlunparse

from playwright.sync_api import (
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from . import logger as log


# 付款頁 URL 特徵: /payment, /orders/, /checkout, /complete
_PAYMENT_PATTERNS = re.compile(r"/(payment|orders/\d+|checkout|complete|thanks?|finished)", re.I)

# 主辦單位子網域 (例: globalmusic.kktix.cc) 抓 events/{slug} 的 regex
_KKTIX_EVENT_PATH = re.compile(r"^/events/([^/?#]+)", re.I)


class TicketStatus(str, Enum):
    """目標票價在報名頁上的狀態。"""
    AVAILABLE = "available"   # 該價錢的票還在賣 (可下單)
    SOLD_OUT = "sold_out"     # 該價錢的票存在但售完 (應刷新重試)
    NOT_FOUND = "not_found"   # 頁面上根本沒這個價錢 (config 寫錯, 應放棄)


@dataclass
class TicketRow:
    """報名頁上一個「可購買」票種的資訊。

    現代 KKTIX 報名頁是 AngularJS SPA。每個 `.ticket-unit` 包含:
        <span class="ticket-name">座位區 ... 2MF</span>
        <span class="ticket-price">TWD$4,500</span>
        <span class="ticket-quantity">
          <button class="minus" ng-click="quantityBtnClick(-1)">-</button>
          <input ng-model="ticketModel.quantity" value="0">
          <button class="plus"  ng-click="quantityBtnClick(1)">+</button>
        </span>

    售完的票種會渲染 `<span class="ticket-quantity">Sold Out</span>` (沒有 input),
    `parse_ticket_rows` 只回傳「真的有 input 可填數量」的票種。
    """
    name: str          # 票種名稱（含區域）
    price: int         # 票價（找不到則 -1）
    unit: object       # Playwright Locator 指向 .ticket-unit 容器
    plus_button: object   # `+` 按鈕 (用來增加數量, 透過 AngularJS handler)
    quantity_input: object  # <input> 直接讀現值
    max_quantity: int  # 該票種上限 (KKTIX 通常 4)


def derive_register_url(event_url: str) -> str:
    """從活動頁 URL 推導報名頁 URL。

    KKTIX 的報名 (`/registrations/new`) 流程**只在主站 `kktix.com` 提供**，
    主辦單位子網域 (例: `globalmusic.kktix.cc`、`kwan.kktix.cc`) 上的
    `/events/{slug}/registrations/new` 會被 redirect 回 `kktix.com/`。
    所以必須把 host 改成 `kktix.com`。

    Examples
    --------
    >>> derive_register_url('https://kwan.kktix.cc/events/754b7611')
    'https://kktix.com/events/754b7611/registrations/new'
    >>> derive_register_url('https://globalmusic.kktix.cc/events/5dee326c')
    'https://kktix.com/events/5dee326c/registrations/new'
    >>> derive_register_url('https://kktix.com/events/5dee326c/registrations/new')
    'https://kktix.com/events/5dee326c/registrations/new'
    """
    url = event_url.rstrip("/")
    parsed = urlparse(url)

    m = _KKTIX_EVENT_PATH.match(parsed.path)
    if m is None:
        # 未知格式，退回舊行為（頂多原樣 + /registrations/new）
        if url.endswith("/registrations/new"):
            return url
        return f"{url}/registrations/new"

    slug = m.group(1)
    new_path = f"/events/{slug}/registrations/new"
    return urlunparse(("https", "kktix.com", new_path, "", "", ""))


def is_payment_page(url: str) -> bool:
    """判斷目前 URL 是否已經到付款 / 完成階段。"""
    return bool(_PAYMENT_PATTERNS.search(urlparse(url).path))


# ---------------------------------------------------------------------------
# 第一階段: 等開賣 + 進入報名頁
# ---------------------------------------------------------------------------

def prewarm(page: Page, event_url: str) -> None:
    """提前打開活動頁，建立 TLS / cookie / Cloudflare clearance。"""
    log.step(f"預熱頁面: {event_url}")
    try:
        page.goto(event_url, wait_until="domcontentloaded", timeout=30_000)
    except PlaywrightTimeoutError:
        log.warn("  預熱頁面逾時，繼續嘗試")
    log.ok("  預熱完成")


def try_enter_registration(page: Page, event_url: str, register_url: str) -> bool:
    """嘗試進入報名頁。回傳是否成功（已在報名頁且看得到票券表單）。

    策略:
      A. 直接 goto 報名頁 — 最快
      B. 若失敗回到活動頁，找「立即購票 / 立即報名 / Get Ticket」按鈕點下去

    報名頁是 AngularJS SPA, `domcontentloaded` 後票種還沒 hydrate, 因此 goto
    完還要等 `.ticket-unit` 等特徵元素出現。
    """
    # --- A. 直接 navigate ---
    try:
        page.goto(register_url, wait_until="domcontentloaded", timeout=10_000)
    except PlaywrightTimeoutError:
        return False

    # 等 SPA 把票種 hydrate 出來
    if _wait_for_registration_form(page, timeout_ms=6_000):
        return True

    # --- B. 回到活動頁找按鈕 ---
    try:
        page.goto(event_url, wait_until="domcontentloaded", timeout=10_000)
    except PlaywrightTimeoutError:
        return False

    btn = _find_register_button(page)
    if btn is None:
        return False

    try:
        btn.click(timeout=2_000)
    except PlaywrightTimeoutError:
        return False

    page.wait_for_load_state("domcontentloaded", timeout=10_000)
    return _wait_for_registration_form(page, timeout_ms=6_000)


def _find_register_button(page: Page):
    """找活動頁上的『立即購票 / Get Ticket』按鈕，找不到回 None。

    最穩健的方式: 直接找 `href` 指向 `/registrations/new` 的 anchor，
    不依賴文字。這樣英文版 ('Get Ticket')、繁中版 ('立即購票' / '立即報名')、
    日文版都涵蓋。文字版的 selector 留作 fallback。
    """
    # 策略 A: 用 href 找 (跨語言最穩)
    href_candidates = [
        "a[href*='/registrations/new']",
    ]
    for sel in href_candidates:
        try:
            locs = page.locator(sel).all()
        except Exception:  # noqa: BLE001
            continue
        for loc in locs:
            try:
                if loc.is_visible(timeout=200):
                    return loc
            except Exception:  # noqa: BLE001
                continue

    # 策略 B: 用文字找 (萬一 KKTIX 改成 SPA / 點擊 button 才導頁)
    text_candidates = [
        # 繁中
        "a:has-text('立即報名')",
        "a:has-text('立即購票')",
        "button:has-text('立即報名')",
        "button:has-text('立即購票')",
        "a:has-text('購票')",
        "a:has-text('報名')",
        # 英文
        "a:has-text('Get Tickets')",
        "a:has-text('Get Ticket')",
        "a:has-text('Buy Tickets')",
        "a:has-text('Buy Ticket')",
        "a:has-text('Register Now')",
        "a:has-text('Register')",
        "button:has-text('Get Tickets')",
        "button:has-text('Get Ticket')",
        "button:has-text('Buy Tickets')",
    ]
    for sel in text_candidates:
        loc = page.locator(sel).first
        try:
            if loc.is_visible(timeout=200):
                return loc
        except Exception:  # noqa: BLE001
            continue

    return None


# 報名頁的特徵元素 (現代 SPA 與舊版皆涵蓋)
_REGISTRATION_FORM_SELECTORS = [
    ".ticket-unit",                  # 現代 AngularJS SPA: 一個票種一個 unit
    "#registrationsNewApp",          # SPA root
    ".ticket-quantity",              # 數量輸入容器 (新舊版都有)
    "form#registrationForm",         # 舊版 server-rendered form
    "select[name*='quantity']",      # 舊版 select
    "input[type='submit']",
]


def _registration_form_visible(page: Page) -> bool:
    """檢查目前頁面是否是「能下單」的報名頁 (snapshot, 不等)。"""
    if "/registrations/new" not in page.url:
        return False
    for sel in _REGISTRATION_FORM_SELECTORS:
        try:
            if page.locator(sel).first.is_visible(timeout=300):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _wait_for_registration_form(page: Page, timeout_ms: int) -> bool:
    """等到報名頁的票種表單出現 (有等)。SPA 可能慢一點才 hydrate。"""
    if "/registrations/new" not in page.url:
        return False
    try:
        page.wait_for_selector(
            ", ".join(_REGISTRATION_FORM_SELECTORS),
            state="visible",
            timeout=timeout_ms,
        )
        return True
    except PlaywrightTimeoutError:
        return False


# ---------------------------------------------------------------------------
# 第二階段: 在報名頁選票 + 送出
# ---------------------------------------------------------------------------

def parse_ticket_rows(page: Page) -> list[TicketRow]:
    """掃描報名頁上所有票種 (現代 AngularJS SPA 結構)。

    每個票種是一個 `.ticket-unit` div, 內含:
        .ticket-name   票名 (含區域)
        .ticket-price  例 'TWD$4,500'
        .ticket-quantity > input[type=text]   數量輸入
        .ticket-quantity > button.plus / .minus  ±按鈕

    舊版 fallback (server-rendered form with `<select>`) 暫時放棄, 真要支援
    再回頭加。
    """
    rows: list[TicketRow] = []

    units = page.locator(".ticket-unit").all()
    for unit in units:
        try:
            # 售完的票種雖然也有 <span class="ticket-quantity">, 但裡面只放
            # "Sold Out" 文字, 沒有 <input>。所以判 purchasable 的最穩信號是:
            # `.ticket-quantity input[type=text]` 必須存在
            qty_input_loc = unit.locator(".ticket-quantity input[type=text]").first
            if qty_input_loc.count() == 0:
                continue
            qty_span = unit.locator(".ticket-quantity").first

            # 票價
            try:
                price_text = unit.locator(".ticket-price").first.inner_text(timeout=500)
            except Exception:  # noqa: BLE001
                price_text = unit.inner_text(timeout=500)
            price = _extract_price(price_text)

            # 票名
            try:
                name_text = unit.locator(".ticket-name").first.inner_text(timeout=500)
            except Exception:  # noqa: BLE001
                name_text = unit.inner_text(timeout=500)
            name = _normalize_ticket_name(name_text)

            # ± 按鈕 + input
            plus_btn = qty_span.locator("button.plus, button[ng-click*='quantityBtnClick(1)']").first
            qty_input = qty_span.locator("input[type=text]").first

            rows.append(TicketRow(
                name=name or "(未知票種)",
                price=price,
                unit=unit,
                plus_button=plus_btn,
                quantity_input=qty_input,
                max_quantity=4,  # KKTIX 慣例上限 4
            ))
        except Exception as e:  # noqa: BLE001
            log.warn(f"  解析某個票種時失敗 (略過): {e}")
            continue

    return rows


def _normalize_ticket_name(text: str) -> str:
    """把 '座位區\\n2MF' 之類的票名收成一行。"""
    parts = [p.strip() for p in text.splitlines() if p.strip()]
    return " ".join(parts)[:60]


def check_target_price_status(
    page: Page, target_prices: list[int],
) -> tuple[TicketStatus, list[int]]:
    """檢查目標票價清單在當前報名頁上的狀態。

    對任一目標票價:
      - 找到 `.ticket-unit` 且內含 `.ticket-quantity input[type=text]` → AVAILABLE
      - 找到 `.ticket-unit` 但無 input (只有 'Sold Out' 文字)         → SOLD_OUT
      - 沒找到價錢符合的 unit                                          → NOT_FOUND

    多個目標時取最樂觀: 任一個 AVAILABLE → AVAILABLE; 否則任一 SOLD_OUT → SOLD_OUT;
    全部 NOT_FOUND → NOT_FOUND。

    回傳 (status, all_prices_seen) — `all_prices_seen` 是頁面上所有 unit 解析出的
    價格 (debug 用, 讓使用者知道頁面實際有哪些票)。
    """
    target_set = set(int(p) for p in target_prices if int(p) > 0)
    if not target_set:
        return (TicketStatus.NOT_FOUND, [])

    seen_prices: list[int] = []
    has_sold_out = False
    has_available = False

    for unit in page.locator(".ticket-unit").all():
        try:
            try:
                price_text = unit.locator(".ticket-price").first.inner_text(timeout=500)
            except Exception:  # noqa: BLE001
                price_text = unit.inner_text(timeout=500)
            price = _extract_price(price_text)
            if price <= 0:
                continue
            seen_prices.append(price)
            if price not in target_set:
                continue
            # 該 unit 是目標票價: 看有沒有 input
            has_input = unit.locator(".ticket-quantity input[type=text]").count() > 0
            if has_input:
                has_available = True
            else:
                has_sold_out = True
        except Exception:  # noqa: BLE001
            continue

    if has_available:
        return (TicketStatus.AVAILABLE, seen_prices)
    if has_sold_out:
        return (TicketStatus.SOLD_OUT, seen_prices)
    return (TicketStatus.NOT_FOUND, seen_prices)


def _extract_price(text: str) -> int:
    """從 '座位區 NT$4,500 ...' 之類的字串抓出第一個價格。"""
    # 優先匹配 "NT$X,XXX" 或 "$X,XXX"
    m = re.search(r"(?:NT\$|TWD\$?|\$)\s*([\d,]+)", text)
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            pass
    # 退一步: 找形如 4,500 的數字
    m = re.search(r"([\d]{1,3}(?:,\d{3})+|\d{3,5})\s*元?", text)
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return -1


def _extract_name(text: str) -> str:
    """取容器文字第一行當票種名稱。"""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("$") and not line.startswith("NT$"):
            return line[:40]
    return ""


def pick_ticket_row(rows: list[TicketRow], target_price: int) -> Optional[TicketRow]:
    """挑出價格 == target_price 的票種；找不到回 None。"""
    for r in rows:
        if r.price == target_price:
            return r
    return None


def select_quantity(row: TicketRow, qty: int) -> bool:
    """設定票券張數。

    最穩的方式是「點 N 次 `+` 按鈕」, 因為:
      - AngularJS `ng-click="quantityBtnClick(1)"` 會更新 `ticketModel.quantity`
        並觸發 `cantBuyMore()` / `couldNextStep()` 的 watcher (送出按鈕才會 enable)。
      - 直接 `fill(input)` 在 AngularJS 1.x 上有時不會 trigger digest,
        要靠 input event + change event 雙重 dispatch 才穩。

    qty 通常是 1~4, 點 4 次以內就好。
    """
    target = min(qty, row.max_quantity)
    if target <= 0:
        return False
    plus = row.plus_button

    for i in range(target):
        try:
            # `+` 按鈕在最大值時會被 ng-disabled, click 會炸 → 用 force
            plus.click(timeout=2_000)
        except Exception as e:  # noqa: BLE001
            log.warn(f"  點 + 第 {i+1} 次失敗: {e}")
            return False

    # 驗證實際 value
    try:
        actual = row.quantity_input.evaluate("el => el.value")
        if str(actual).strip() not in (str(target), f"{target}.0"):
            log.warn(f"  數量設定後 input.value={actual!r} 預期={target}")
            return False
    except Exception:  # noqa: BLE001
        pass
    return True


def agree_terms(page: Page) -> int:
    """勾選所有同意條款 checkbox。回傳勾了幾個。"""
    count = 0
    boxes = page.locator("input[type=checkbox]").all()
    for cb in boxes:
        try:
            if not cb.is_visible(timeout=200):
                continue
            if cb.is_checked():
                continue
            cb.check(timeout=1_000)
            count += 1
        except Exception:  # noqa: BLE001
            continue
    return count


def submit(page: Page) -> bool:
    """點下一步 / 確認 / submit 按鈕。

    現代 KKTIX SPA 有兩個按鈕:
      `<button ng-click="challenge(1)">Best Available</button>`  ← 電腦配位 (最快)
      `<button ng-click="challenge()">Pick Your Seat(s)</button>` ← 手動選位
    沒勾同意條款 / 數量為 0 時 `disabled="disabled"`。
    優先點 `challenge(1)` (電腦配位、不用再選位、最快進付款頁)。
    """
    candidates = [
        # 現代 SPA — 優先電腦配位
        "button[ng-click='challenge(1)']:not([disabled])",
        "button[ng-click='challenge()']:not([disabled])",
        # 舊版 fallback
        "input[type=submit]",
        "button[type=submit]",
        "button:has-text('Best Available')",
        "button:has-text('Pick Your Seat')",
        "button:has-text('下一步')",
        "button:has-text('確認')",
        "button:has-text('Next')",
        "a.btn-primary:has-text('下一步')",
    ]
    for sel in candidates:
        loc = page.locator(sel).first
        try:
            if loc.is_visible(timeout=300):
                loc.click(timeout=2_000)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


# ---------------------------------------------------------------------------
# 第三階段: 等到付款頁
# ---------------------------------------------------------------------------

def wait_for_payment(page: Page, timeout_s: float = 30.0) -> bool:
    """等待頁面跳轉到付款頁（或訂單建立完成的頁面）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if is_payment_page(page.url):
            return True
        try:
            page.wait_for_event("framenavigated", timeout=1_000)
        except PlaywrightTimeoutError:
            continue
    return is_payment_page(page.url)
