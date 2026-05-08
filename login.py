#!/usr/bin/env python3
"""手動登入 KKTIX 並把 session 存到持久化 profile。

只要跑過一次，搶票時就不用再登入。

用法:
  python login.py [--config config.yaml]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from src import config as cfg_mod
from src import logger as log


LOGIN_URL = "https://kktix.com/users/sign_in"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml", help="設定檔路徑")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        log.err(f"找不到設定檔: {cfg_path}")
        log.info("複製 config.example.yaml 成 config.yaml 後再修改")
        return 1

    cfg = cfg_mod.load(cfg_path)
    cfg.browser.user_data_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"使用持久化 profile: {cfg.browser.user_data_dir}")
    log.info("打開瀏覽器，請手動登入 KKTIX (含手機驗證、預填資料)")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(cfg.browser.user_data_dir),
            headless=False,
            viewport={
                "width": cfg.browser.viewport_width,
                "height": cfg.browser.viewport_height,
            },
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(LOGIN_URL)

        log.ok("瀏覽器已開啟。完成下列步驟後關閉瀏覽器：")
        log.info("  1. 登入 KKTIX 帳號")
        log.info("  2. 確認手機與 Email 已驗證 (會員設定 → 個人資料)")
        log.info("  3. 預先填寫『報名預填資料』(會員設定 → 報名預填資料)")
        log.info("     建議: 姓名、手機號碼，搶票時會自動帶入")
        log.info("  4. (推薦) 先去測試場次完成一次完整購票流程，讓 Cloudflare 信任此瀏覽器")
        log.info("")
        log.info("完成後直接關閉瀏覽器視窗即可，session 會自動保存。")

        try:
            page.wait_for_event("close", timeout=0)
        except KeyboardInterrupt:
            pass
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass

    log.ok(f"Session 已保存至 {cfg.browser.user_data_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
