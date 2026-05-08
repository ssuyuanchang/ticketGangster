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

    return cfg
