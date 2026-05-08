#!/usr/bin/env python3
"""ticketGangster 主搶票腳本。

流程:
  1. 載入 config + 校準伺服器時間
  2. 開瀏覽器（持久化 profile，已登入狀態）
  3. T-prewarm 秒前打開活動頁預熱
  4. T-attack_lead_ms 開始衝擊報名頁
  5. 進到報名頁後，自動選票價 + 數量 + 同意 + 送出
  6. 偵測到付款頁時暫停，由使用者刷卡
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Optional

from playwright.sync_api import (
    BrowserContext,
    Page,
    sync_playwright,
)

from src import config as cfg_mod
from src import kktix
from src import logger as log
from src import time_sync


class FormResult(str, Enum):
    """`handle_registration_form` 的執行結果, 用來決定外層迴圈下一步。"""
    SUBMITTED = "submitted"            # 已送出表單, 進入等付款頁階段
    SOLD_OUT = "sold_out"              # 目標票價售完, 應刷新重試
    NOT_FOUND = "not_found"            # 頁面找不到 config 票價, 放棄
    TRANSIENT_FAIL = "transient_fail"  # 點 + / submit 動作失敗 (race?), 應刷新重試
    MANUAL = "manual"                  # auto_submit=false, 等使用者手動操作


# ---------------------------------------------------------------------------
# 倒數計時
# ---------------------------------------------------------------------------

def countdown(clock: time_sync.Clock, target_ts: float) -> None:
    """印出倒數，直到 (target_ts - 1.5s) 為止，最後 1.5 秒進入 busy-wait。"""
    last_print = 0.0
    while True:
        remaining = clock.remaining_to(target_ts)
        if remaining <= 1.5:
            break
        # 每秒印一次
        if time.time() - last_print >= 1.0:
            mm = int(remaining // 60)
            ss = remaining - mm * 60
            log.info(f"  倒數 {mm:02d}:{ss:05.2f} (server time)")
            last_print = time.time()
        # 距離越遠睡越久；越近睡越短
        if remaining > 60:
            time.sleep(0.5)
        elif remaining > 10:
            time.sleep(0.2)
        else:
            time.sleep(0.05)


def busy_wait_until(clock: time_sync.Clock, target_ts: float) -> None:
    """最後 1~2 秒精準 busy-wait。"""
    while clock.remaining_to(target_ts) > 0:
        # spin
        pass


def jittered_interval(base_s: float, pct: int) -> float:
    """回傳 base_s * (1 ± pct%) 的隨機值, 用來打破固定間隔的機械感。

    pct = 20 → 抖動範圍 [0.8 * base_s, 1.2 * base_s]
    pct = 0  → 不抖動 (回原值)
    """
    if pct <= 0:
        return base_s
    factor = 1.0 + random.uniform(-pct, pct) / 100.0
    return max(0.0, base_s * factor)


# ---------------------------------------------------------------------------
# 報名頁處理 (選票 + 送出)
# ---------------------------------------------------------------------------

def handle_registration_form(page: Page, cfg: cfg_mod.Config) -> FormResult:
    """已在報名頁時, 嘗試選票+勾同意+送出。回傳 FormResult 讓外層決定下一步。

    回傳值:
      SUBMITTED       — 已送出表單, 接下來等付款頁
      SOLD_OUT        — 目標票售完, 外層應 sleep+刷新重試
      NOT_FOUND       — config 票價在頁面上不存在, 外層應放棄
      TRANSIENT_FAIL  — 選張數/送出動作失敗 (race), 外層應刷新重試
      MANUAL          — auto_submit=false, 完成選票後交給使用者
    """
    candidates = [cfg.ticket.price] + cfg.ticket.fallback_prices

    rows = kktix.parse_ticket_rows(page)
    if not rows:
        # 沒任何可購買的票 — 全售完還是頁面爛掉?
        status, seen = kktix.check_target_price_status(page, candidates)
        log.warn(f"  無可購買票種 (頁面看到的價格: {seen})")
        if status == kktix.TicketStatus.SOLD_OUT:
            return FormResult.SOLD_OUT
        return FormResult.NOT_FOUND

    log.info(f"  找到 {len(rows)} 個可購買票種:")
    for r in rows:
        log.info(f"    - {r.name}  NT${r.price}  上限={r.max_quantity}")

    chosen: Optional[kktix.TicketRow] = None
    for price in candidates:
        row = kktix.pick_ticket_row(rows, price)
        if row:
            chosen = row
            log.ok(f"  選定票價: NT${price} ({row.name})")
            break

    if chosen is None:
        # 目標票價在「可購買」清單裡找不到, 看是售完還是 config 錯
        status, seen = kktix.check_target_price_status(page, candidates)
        if status == kktix.TicketStatus.SOLD_OUT:
            log.warn(f"  目標票價 {candidates} 售完 (頁面看到: {seen})")
            return FormResult.SOLD_OUT
        log.err(f"  找不到指定票價 {candidates} (頁面看到: {seen})")
        log.warn("  可能 config 寫錯, 或票種命名與設定不同, 放棄此場次")
        return FormResult.NOT_FOUND

    log.step(f"設定數量 = {cfg.ticket.quantity}")
    if not kktix.select_quantity(chosen, cfg.ticket.quantity):
        log.warn("  設定數量失敗 (race? 票種瞬間售完?)")
        return FormResult.TRANSIENT_FAIL

    if cfg.strategy.auto_agree:
        n = kktix.agree_terms(page)
        log.info(f"  勾選了 {n} 個同意條款 checkbox")

    if not cfg.strategy.auto_submit:
        log.warn("  auto_submit=false，請手動點下一步")
        return FormResult.MANUAL

    log.step("送出表單")
    if not kktix.submit(page):
        log.warn("  找不到送出按鈕 (可能尚未變 enabled)")
        return FormResult.TRANSIENT_FAIL
    log.ok("  已送出")
    return FormResult.SUBMITTED


# ---------------------------------------------------------------------------
# 主流程 (單分頁)
# ---------------------------------------------------------------------------

def run_single_tab(
    page: Page,
    cfg: cfg_mod.Config,
    clock: time_sync.Clock,
    tab_id: int = 0,
) -> bool:
    """在一個分頁上完整跑一次搶票流程。回傳是否成功進到付款頁。"""
    event_url = cfg.event.url
    register_url = kktix.derive_register_url(event_url)
    target_ts = cfg.event.sale_start.timestamp()

    log.info(f"[T{tab_id}] 活動頁: {event_url}")
    log.info(f"[T{tab_id}] 報名頁: {register_url}")

    # ---- 1. 預熱 ----
    prewarm_at = target_ts - cfg.strategy.prewarm_seconds
    if clock.remaining_to(prewarm_at) > 0:
        log.info(f"[T{tab_id}] 等待預熱時間 ({cfg.strategy.prewarm_seconds}s 前)...")
        countdown(clock, prewarm_at)

    kktix.prewarm(page, event_url)

    # ---- 2. 倒數到開賣 ----
    attack_at = target_ts - cfg.strategy.attack_lead_ms / 1000.0
    log.step(f"[T{tab_id}] 倒數至 attack 時間 (T-{cfg.strategy.attack_lead_ms}ms)...")
    countdown(clock, attack_at)
    busy_wait_until(clock, attack_at)

    # ---- 3. 衝擊報名頁 (持續刷新, 直到搶到、deadline 到、或 config 錯放棄) ----
    log.ok(f"[T{tab_id}] >>> ATTACK <<<")
    interval_s = cfg.strategy.refresh_interval_ms / 1000.0
    jitter_pct = cfg.strategy.refresh_jitter_pct
    duration_s = cfg.strategy.attack_duration_minutes * 60.0
    payment_timeout_s = cfg.strategy.payment_wait_timeout_seconds
    attack_start = time.time()
    deadline = attack_start + duration_s
    log.info(f"[T{tab_id}] 最多搶 {cfg.strategy.attack_duration_minutes} 分鐘, "
             f"刷新間隔 {cfg.strategy.refresh_interval_ms}ms ±{jitter_pct}%, "
             f"送出後等付款頁 timeout = {payment_timeout_s}s")

    enter_attempts = 0   # 進入報名頁次數
    submit_attempts = 0  # 成功送出次數 (含等付款頁逾時要重試的)

    while time.time() < deadline:
        # --- (a) 進入報名頁 ---
        enter_attempts += 1
        t0 = time.time()
        entered = kktix.try_enter_registration(page, event_url, register_url)
        if not entered:
            if enter_attempts % 10 == 0:
                elapsed = time.time() - attack_start
                remain = deadline - time.time()
                log.warn(f"[T{tab_id}] 進報名頁失敗 #{enter_attempts}, "
                         f"已過 {elapsed:.0f}s, 剩 {remain:.0f}s")
            # 用 jitter 後的間隔精準補齊 (避免被偵測成定時打點)
            target_interval = jittered_interval(interval_s, jitter_pct)
            sleep_left = target_interval - (time.time() - t0)
            if sleep_left > 0:
                time.sleep(sleep_left)
            continue
        log.ok(f"[T{tab_id}] 進入報名頁 (第 {enter_attempts} 次嘗試, "
               f"{(time.time()-t0)*1000:.0f}ms): {page.url}")

        # --- (b) 解析票種 + 選票 + 送出 ---
        result = handle_registration_form(page, cfg)

        if result == FormResult.NOT_FOUND:
            # config 寫錯 (頁面上根本沒這個價錢) — 不刷新, 直接放棄
            return False
        if result == FormResult.MANUAL:
            log.info(f"[T{tab_id}] auto_submit=false, 等使用者手動接手")
            return True
        if result == FormResult.SOLD_OUT:
            log.warn(f"[T{tab_id}] 售完, 刷新報名頁繼續等釋票...")
            time.sleep(jittered_interval(interval_s, jitter_pct))
            continue
        if result == FormResult.TRANSIENT_FAIL:
            log.warn(f"[T{tab_id}] 下單動作失敗 (race?), 刷新重試")
            time.sleep(jittered_interval(interval_s, jitter_pct))
            continue

        # result == SUBMITTED — 等付款頁
        submit_attempts += 1
        log.step(f"[T{tab_id}] 等付款頁 (timeout {payment_timeout_s}s)...")
        if kktix.wait_for_payment(page, timeout_s=payment_timeout_s):
            log.ok(f"[T{tab_id}] >>> 已進入付款頁: {page.url}")
            return True

        # 等太久 — 卡 queue / 網路問題. 放棄這次提交, 刷新重新搶
        # 注意: 此舉可能放棄 KKTIX 已經為我們鎖定的座位
        log.warn(f"[T{tab_id}] 送出後 {payment_timeout_s}s 仍未進付款頁, "
                 f"當前 URL: {page.url}")
        log.warn(f"[T{tab_id}] 可能卡在等候室或網路問題, 刷新報名頁重新嘗試 "
                 f"(已成功送出 {submit_attempts} 次)")
        # 不 sleep, 直接下一輪, 下輪會 page.goto(register_url) = 強制刷新

    # deadline 到
    elapsed = time.time() - attack_start
    log.err(f"[T{tab_id}] {cfg.strategy.attack_duration_minutes} 分鐘到 "
            f"(進入報名頁 {enter_attempts} 次, 送出 {submit_attempts} 次, "
            f"共 {elapsed:.0f}s) 仍未搶到, 放棄")
    return False


# ---------------------------------------------------------------------------
# 通知
# ---------------------------------------------------------------------------

def notify_success(cfg: cfg_mod.Config) -> None:
    log.ok("=" * 60)
    log.ok(">>> 搶到票了！請立刻在瀏覽器完成信用卡付款 <<<")
    log.ok("=" * 60)
    if cfg.notify.beep:
        # ASCII BEL × 5
        try:
            sys.stdout.write("\a" * 5)
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="KKTIX 自動搶票")
    parser.add_argument("--config", default="config.yaml", help="設定檔路徑")
    parser.add_argument("--dry-run", action="store_true",
                        help="僅檢查設定與時間同步，不真的開瀏覽器")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        log.err(f"找不到設定檔: {cfg_path}")
        log.info("複製 config.example.yaml 成 config.yaml 後再修改")
        return 1

    cfg = cfg_mod.load(cfg_path)

    log.info("=" * 60)
    log.info(" ticketGangster - KKTIX 搶票")
    log.info("=" * 60)
    log.info(f"  活動頁: {cfg.event.url}")
    log.info(f"  開賣時間: {cfg.event.sale_start.isoformat()}")
    log.info(f"  目標票價: NT${cfg.ticket.price} × {cfg.ticket.quantity}")
    if cfg.ticket.fallback_prices:
        log.info(f"  備援票價: {cfg.ticket.fallback_prices}")
    log.info(f"  並行分頁: {cfg.strategy.parallel_tabs}")
    log.info(f"  最長搶票時間: {cfg.strategy.attack_duration_minutes} 分鐘")
    log.info(f"  Profile: {cfg.browser.user_data_dir}")
    log.info("")

    clock = time_sync.build_clock(cfg.strategy.use_server_time)
    target_ts = cfg.event.sale_start.timestamp()
    rem = clock.remaining_to(target_ts)
    if rem <= 0:
        log.warn(f"  開賣時間已過 {-rem:.0f}s，仍會嘗試（票可能已售完）")
    else:
        log.info(f"  距開賣還有 {rem:.0f}s ({rem / 60:.1f} 分鐘)")

    if args.dry_run:
        log.ok("dry-run 結束")
        return 0

    if not cfg.browser.user_data_dir.exists():
        log.err(f"找不到 profile: {cfg.browser.user_data_dir}")
        log.info("請先執行: python login.py")
        return 1

    # ---- 開瀏覽器 ----
    with sync_playwright() as p:
        ctx: BrowserContext = p.chromium.launch_persistent_context(
            user_data_dir=str(cfg.browser.user_data_dir),
            headless=cfg.browser.headless,
            viewport={
                "width": cfg.browser.viewport_width,
                "height": cfg.browser.viewport_height,
            },
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
            ],
        )

        # 開 N 個分頁
        pages: list[Page] = []
        if ctx.pages:
            pages.append(ctx.pages[0])
        while len(pages) < cfg.strategy.parallel_tabs:
            pages.append(ctx.new_page())

        if cfg.strategy.parallel_tabs == 1:
            # 簡單同步單分頁
            try:
                ok = run_single_tab(pages[0], cfg, clock, tab_id=0)
            except Exception as e:  # noqa: BLE001
                log.err(f"流程例外: {e}")
                ok = False

            if ok:
                notify_success(cfg)
        else:
            # 多分頁: 用 thread 跑（Playwright sync API 是 thread-safe per page）
            import threading

            results: list[bool] = [False] * cfg.strategy.parallel_tabs
            stop_event = threading.Event()

            def worker(idx: int) -> None:
                try:
                    if stop_event.is_set():
                        return
                    results[idx] = run_single_tab(pages[idx], cfg, clock, tab_id=idx)
                    if results[idx]:
                        stop_event.set()
                except Exception as e:  # noqa: BLE001
                    log.err(f"[T{idx}] 例外: {e}")

            threads = [
                threading.Thread(target=worker, args=(i,), daemon=True)
                for i in range(cfg.strategy.parallel_tabs)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            if any(results):
                notify_success(cfg)

        # ---- 不關瀏覽器，讓使用者刷卡 ----
        log.info("")
        log.info("瀏覽器保持開啟。完成付款後按 Ctrl+C 結束本程式。")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            log.info("收到 Ctrl+C，結束")
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
