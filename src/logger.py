"""簡易帶毫秒時間戳的 logger，搶票時要看精準的 timing。"""
from __future__ import annotations

import sys
import time
from datetime import datetime


_COLOR = {
    "info": "\033[36m",   # cyan
    "ok": "\033[32m",     # green
    "warn": "\033[33m",   # yellow
    "err": "\033[31m",    # red
    "step": "\033[35m",   # magenta
    "reset": "\033[0m",
}


def _stamp() -> str:
    now = datetime.now()
    return now.strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}"


def _emit(level: str, msg: str) -> None:
    color = _COLOR.get(level, "")
    reset = _COLOR["reset"]
    print(f"{color}[{_stamp()}] [{level.upper():4s}]{reset} {msg}", flush=True)


def info(msg: str) -> None:
    _emit("info", msg)


def ok(msg: str) -> None:
    _emit("ok", msg)


def warn(msg: str) -> None:
    _emit("warn", msg)


def err(msg: str) -> None:
    _emit("err", msg)


def step(msg: str) -> None:
    _emit("step", msg)


def now_ms() -> int:
    return int(time.time() * 1000)
