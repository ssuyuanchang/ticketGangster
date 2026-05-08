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
from typing import Optional
from urllib.parse import urlparse

from playwright.sync_api import (
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from . import logger as log


# 付款頁 URL 特徵: /payment, /orders/, /checkout, /complete
_PAYMENT_PATTERNS = re.compile(r"/(payment|orders/\d+|checkout|complete|thanks?|finished)", re.I)


@dataclass
class TicketRow:
    """報名頁上一個票種的資訊。"""
    name: str          # 票種名稱（例: "座位區"、"NT$4500"）
    price: int         # 解析出的票價（找不到則 -1）
    quantity_locator: object  # Playwright Locator 指向 quantity 控件 (select 或 input)
    max_quantity: int  # 該票種上限


def derive_register_url(event_url: str) -> str:
    """從活動頁 URL 推導報名頁 URL。

    Examples
    --------
    >>> derive_register_url('https://kwan.kktix.cc/events/754b7611')
    'https://kwan.kktix.cc/events/754b7611/registrations/new'
    """
    url = event_url.rstrip("/")
    if url.endswith("/registrations/new"):
        return url
    return f"{url}/registrations/new"


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
      B. 若失敗回到活動頁，找「立即購票 / 立即報名」按鈕點下去
    """
    # --- A. 直接 navigate ---
    try:
        page.goto(register_url, wait_until="domcontentloaded", timeout=8_000)
    except PlaywrightTimeoutError:
        return False

    if _registration_form_visible(page):
        return True

    # --- B. 回到活動頁找按鈕 ---
    current = page.url
    if "/registrations/new" not in current:
        # 已經被 redirect 出去 (可能是還沒開賣 → 跳回 event)
        pass
    try:
        page.goto(event_url, wait_until="domcontentloaded", timeout=8_000)
    except PlaywrightTimeoutError:
        return False

    btn = _find_register_button(page)
    if btn is None:
        return False

    try:
        btn.click(timeout=2_000)
    except PlaywrightTimeoutError:
        return False

    page.wait_for_load_state("domcontentloaded", timeout=8_000)
    return _registration_form_visible(page)


def _find_register_button(page: Page):
    """找活動頁上的『立即購票』按鈕，找不到回 None。"""
    candidates = [
        # KKTIX 常見按鈕
        "a.btn-primary:has-text('立即報名')",
        "a.btn-primary:has-text('立即購票')",
        "a.btn:has-text('立即報名')",
        "a.btn:has-text('立即購票')",
        "a:has-text('立即報名')",
        "a:has-text('立即購票')",
        "a:has-text('Register Now')",
        "button:has-text('立即報名')",
        "button:has-text('立即購票')",
    ]
    for sel in candidates:
        loc = page.locator(sel).first
        try:
            if loc.is_visible(timeout=200):
                return loc
        except Exception:  # noqa: BLE001
            continue
    return None


def _registration_form_visible(page: Page) -> bool:
    """檢查目前頁面是否是「能下單」的報名頁。"""
    if "/registrations/new" not in page.url:
        return False
    # KKTIX 報名頁會有 form#registrationForm 或 .ticket-quantity / select 數量
    selectors = [
        "form#registrationForm",
        ".ticket-quantity",
        "select[name*='quantity']",
        "input[type='submit']",
    ]
    for sel in selectors:
        try:
            if page.locator(sel).first.is_visible(timeout=300):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


# ---------------------------------------------------------------------------
# 第二階段: 在報名頁選票 + 送出
# ---------------------------------------------------------------------------

def parse_ticket_rows(page: Page) -> list[TicketRow]:
    """掃描報名頁上所有票種。

    KKTIX 報名表常見結構（不同主辦單位略有差異）：
      <div class='ticket'>                              ← 一個票種一列
        <span class='ticket-name'>...</span>
        <span class='ticket-price'>NT$2,500</span>
        <select class='ticket-quantity'>...
    這個函式對結構做寬鬆匹配。
    """
    rows: list[TicketRow] = []

    # 主要策略: 找所有 select 數量控件，往上找最近的「票種容器」
    quantity_selects = page.locator("select").all()
    for sel in quantity_selects:
        try:
            # 只保留 quantity 相關的 select
            name_attr = (sel.get_attribute("name") or "").lower()
            id_attr = (sel.get_attribute("id") or "").lower()
            cls_attr = (sel.get_attribute("class") or "").lower()
            if not any(k in (name_attr + id_attr + cls_attr) for k in ("quantity", "qty", "ticket")):
                # 不是票券數量控件
                continue

            # 抓最大值（KKTIX 通常 select option 最後一個是最大可購數量）
            options = sel.locator("option").all()
            max_qty = 0
            for opt in options:
                try:
                    v = int((opt.get_attribute("value") or "0").strip())
                    if v > max_qty:
                        max_qty = v
                except (TypeError, ValueError):
                    continue
            if max_qty == 0:
                # fallback: 預設 KKTIX 上限 4
                max_qty = 4

            # 從 select 往上找最近的容器，抓 name + price 文字
            container_text = ""
            try:
                container = sel.locator(
                    "xpath=ancestor::*[self::div or self::tr or self::li][1]"
                )
                container_text = container.inner_text(timeout=500)
            except Exception:  # noqa: BLE001
                container_text = ""

            price = _extract_price(container_text)
            name = _extract_name(container_text)

            rows.append(TicketRow(
                name=name or "(未知票種)",
                price=price,
                quantity_locator=sel,
                max_quantity=max_qty,
            ))
        except Exception as e:  # noqa: BLE001
            log.warn(f"  解析某個票種時失敗 (略過): {e}")
            continue

    return rows


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
    """設定票券張數，回傳是否成功。"""
    target = min(qty, row.max_quantity)
    sel = row.quantity_locator
    try:
        sel.select_option(value=str(target), timeout=2_000)
        return True
    except Exception:
        # 嘗試用 label
        try:
            sel.select_option(label=str(target), timeout=2_000)
            return True
        except Exception as e:  # noqa: BLE001
            log.warn(f"  設定數量 {target} 失敗: {e}")
            return False


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
    """點下一步 / 確認 / submit 按鈕。"""
    candidates = [
        "input[type=submit]",
        "button[type=submit]",
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
