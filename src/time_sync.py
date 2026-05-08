"""伺服器時間同步: 用 KKTIX 的 HTTP Date header 校準本地時鐘漂移。

若本地時鐘和 KKTIX 伺服器有 N 秒誤差，會導致提早或太晚開搶，因此每次啟動先校準。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from . import logger as log


# 多次取樣取中位數，避免單次網路抖動
_SAMPLE_COUNT = 5
_SAMPLE_URL = "https://kktix.com/"


def measure_offset(samples: int = _SAMPLE_COUNT, url: str = _SAMPLE_URL) -> float:
    """回傳 server_time - local_time，單位秒。

    使用 HTTP HEAD 請求，取出 Response.Date header，並校正一半 RTT。
    """
    offsets: list[float] = []
    with httpx.Client(http2=False, timeout=5.0, follow_redirects=False) as client:
        for i in range(samples):
            t0 = time.time()
            try:
                r = client.head(url)
            except Exception as e:  # noqa: BLE001
                log.warn(f"  時間同步取樣 #{i + 1} 失敗: {e}")
                continue
            t1 = time.time()
            date_hdr = r.headers.get("Date")
            if not date_hdr:
                continue
            try:
                server_dt = parsedate_to_datetime(date_hdr)
            except Exception:  # noqa: BLE001
                continue
            if server_dt.tzinfo is None:
                server_dt = server_dt.replace(tzinfo=timezone.utc)
            server_ts = server_dt.timestamp()
            # 假設 server 在 RTT 中點產生 Date header
            mid = (t0 + t1) / 2
            offsets.append(server_ts - mid)
            time.sleep(0.05)

    if not offsets:
        log.warn("  無法取得伺服器時間，使用本地時鐘")
        return 0.0

    offsets.sort()
    median = offsets[len(offsets) // 2]
    return median


class Clock:
    """加上 offset 後的時鐘。"""

    def __init__(self, offset_seconds: float = 0.0) -> None:
        self.offset = offset_seconds

    def now(self) -> float:
        return time.time() + self.offset

    def now_dt(self) -> datetime:
        return datetime.fromtimestamp(self.now(), tz=timezone.utc)

    def remaining_to(self, target_ts: float) -> float:
        return target_ts - self.now()


def build_clock(use_server_time: bool) -> Clock:
    if not use_server_time:
        return Clock(0.0)
    log.info("校準伺服器時間 ...")
    offset = measure_offset()
    log.ok(f"  伺服器時間 offset = {offset * 1000:+.1f} ms")
    return Clock(offset)
