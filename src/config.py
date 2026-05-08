"""設定檔讀取與驗證。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import yaml

TAIPEI_TZ = timezone(timedelta(hours=8))


@dataclass
class EventCfg:
    url: str
    sale_start: datetime  # tz-aware (Asia/Taipei)


@dataclass
class TicketCfg:
    price: int
    quantity: int
    fallback_prices: list[int] = field(default_factory=list)


@dataclass
class StrategyCfg:
    prewarm_seconds: int = 60
    attack_lead_ms: int = 200
    refresh_interval_ms: int = 150
    parallel_tabs: int = 1
    use_server_time: bool = True
    auto_agree: bool = True
    auto_submit: bool = True
    # 從 attack 開始之後，最多持續搶多久 (分鐘)。超時就放棄並關閉。
    # 適用情境: 90 分鐘還沒搶到通常代表這場已售完, 不如停下來。
    attack_duration_minutes: int = 90
    # 送出表單後等付款頁出現的 timeout (秒)。超時 = 卡 queue 或網路問題,
    # 此時放棄這次提交並刷新報名頁重新來過。預設 45 秒。
    payment_wait_timeout_seconds: int = 45
    # refresh_interval_ms 的 jitter 百分比 (0~100)。每次 sleep 以
    # `refresh_interval_ms * (1 ± pct/100)` 隨機抖動，避免被偵測成 bot
    # 規律行為。預設 20 = ±20%。
    refresh_jitter_pct: int = 20


@dataclass
class BrowserCfg:
    user_data_dir: Path
    headless: bool = False
    viewport_width: int = 1280
    viewport_height: int = 900


@dataclass
class NotifyCfg:
    beep: bool = True


@dataclass
class Config:
    event: EventCfg
    ticket: TicketCfg
    strategy: StrategyCfg
    browser: BrowserCfg
    notify: NotifyCfg


def _parse_dt(s: str) -> datetime:
    # 格式: YYYY-MM-DD HH:MM:SS (台北時間)
    dt = datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=TAIPEI_TZ)


def load(path: str | Path) -> Config:
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"設定檔格式錯誤: {path}")

    ev = raw["event"]
    tk = raw["ticket"]
    st = raw.get("strategy") or {}
    br = raw.get("browser") or {}
    nf = raw.get("notify") or {}

    cfg = Config(
        event=EventCfg(
            url=ev["url"].strip(),
            sale_start=_parse_dt(ev["sale_start"]),
        ),
        ticket=TicketCfg(
            price=int(tk["price"]),
            quantity=int(tk["quantity"]),
            fallback_prices=[int(p) for p in (tk.get("fallback_prices") or [])],
        ),
        strategy=StrategyCfg(
            prewarm_seconds=int(st.get("prewarm_seconds", 60)),
            attack_lead_ms=int(st.get("attack_lead_ms", 200)),
            refresh_interval_ms=int(st.get("refresh_interval_ms", 150)),
            parallel_tabs=int(st.get("parallel_tabs", 1)),
            use_server_time=bool(st.get("use_server_time", True)),
            auto_agree=bool(st.get("auto_agree", True)),
            auto_submit=bool(st.get("auto_submit", True)),
            attack_duration_minutes=int(st.get("attack_duration_minutes", 90)),
            payment_wait_timeout_seconds=int(st.get("payment_wait_timeout_seconds", 45)),
            refresh_jitter_pct=int(st.get("refresh_jitter_pct", 20)),
        ),
        browser=BrowserCfg(
            user_data_dir=Path(br.get("user_data_dir", "./profile")).expanduser().resolve(),
            headless=bool(br.get("headless", False)),
            viewport_width=int((br.get("viewport") or {}).get("width", 1280)),
            viewport_height=int((br.get("viewport") or {}).get("height", 900)),
        ),
        notify=NotifyCfg(
            beep=bool(nf.get("beep", True)),
        ),
    )

    # 簡單驗證
    if not cfg.event.url.startswith("http"):
        raise ValueError("event.url 必須是 http(s) 開頭")
    if not (1 <= cfg.ticket.quantity <= 4):
        raise ValueError("ticket.quantity 必須在 1~4 之間 (KKTIX 單筆上限)")
    if cfg.strategy.parallel_tabs < 1:
        cfg.strategy.parallel_tabs = 1
    if cfg.strategy.attack_duration_minutes < 1:
        raise ValueError("strategy.attack_duration_minutes 必須 >= 1")
    if cfg.strategy.payment_wait_timeout_seconds < 5:
        raise ValueError("strategy.payment_wait_timeout_seconds 必須 >= 5")
    if not (0 <= cfg.strategy.refresh_jitter_pct <= 80):
        raise ValueError("strategy.refresh_jitter_pct 必須在 0~80 之間")

    return cfg
