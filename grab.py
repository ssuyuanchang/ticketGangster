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
import sys
import time
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


# ---------------------------------------------------------------------------
# 報名頁處理 (選票 + 送出)
# ---------------------------------------------------------------------------

def handle_registration_form(page: Page, cfg: cfg_mod.Config) -> bool:
    """到達報名頁後，自動選票 + 送出。回傳是否成功送出表單。"""
    log.step("解析票種...")
    rows = kktix.parse_ticket_rows(page)
    if not rows:
        log.err("  找不到任何票種 select，可能頁面結構不同或未成功進入報名頁")
        return False

    log.info(f"  找到 {len(rows)} 個票種:")
    for r in rows:
        log.info(f"    - {r.name}  價格={r.price}  上限={r.max_quantity}")

    # 依優先序嘗試: 主票價 + fallback
    candidates = [cfg.ticket.price] + cfg.ticket.fallback_prices
    chosen: Optional[kktix.TicketRow] = None
    for price in candidates:
        row = kktix.pick_ticket_row(rows, price)
        if row:
            chosen = row
            log.ok(f"  選定票價: NT${price} ({row.name})")
            break

    if chosen is None:
        log.err(f"  找不到指定票價 {cfg.ticket.price} (含 fallback {cfg.ticket.fallback_prices})")
        log.warn("  可能價格寫錯，或這場次的票種跟設定的不同")
        return False

    log.step(f"設定數量 = {cfg.ticket.quantity}")
    if not kktix.select_quantity(chosen, cfg.ticket.quantity):
        return False

    if cfg.strategy.auto_agree:
        n = kktix.agree_terms(page)
        log.info(f"  勾選了 {n} 個同意條款 checkbox")

    if cfg.strategy.auto_submit:
        log.step("送出表單")
        if not kktix.submit(page):
            log.err("  找不到送出按鈕")
            return False
        log.ok("  已送出")
    else:
        log.warn("  auto_submit=false，請手動點下一步")

    return True


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

    # ---- 3. 衝擊報名頁 ----
    log.ok(f"[T{tab_id}] >>> ATTACK <<<")
    attempts = 0
    interval = cfg.strategy.refresh_interval_ms / 1000.0
    max_attempts = 200  # 約 30s @ 150ms

    while attempts < max_attempts:
        attempts += 1
        t0 = time.time()
        success = kktix.try_enter_registration(page, event_url, register_url)
        dt = (time.time() - t0) * 1000
        if success:
            log.ok(f"[T{tab_id}] 第 {attempts} 次嘗試成功進入報名頁 ({dt:.0f}ms)")
            break
        if attempts % 5 == 0:
            log.warn(f"[T{tab_id}] 第 {attempts} 次嘗試失敗，繼續...")
        # 精準間隔
        sleep_left = interval - (time.time() - t0)
        if sleep_left > 0:
            time.sleep(sleep_left)
    else:
        log.err(f"[T{tab_id}] {max_attempts} 次嘗試都進不去報名頁")
        return False

    # ---- 4. 處理報名表 ----
    if not handle_registration_form(page, cfg):
        return False

    # ---- 5. 等付款頁 ----
    log.step(f"[T{tab_id}] 等待付款頁 ...")
    if kktix.wait_for_payment(page, timeout_s=30):
        log.ok(f"[T{tab_id}] >>> 已進入付款頁: {page.url}")
        return True

    # 即使沒偵測到付款 URL，也可能成功（顯示在某個訂單頁），交給人類確認
    log.warn(f"[T{tab_id}] 沒有明確偵測到付款頁，但已送出。當前 URL: {page.url}")
    return True


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
