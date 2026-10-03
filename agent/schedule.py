"""Cron helpers, always in the configured time zone."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter


def valid_cron(expr: str) -> bool:
    return len(expr.split()) == 5 and croniter.is_valid(expr)


def next_run_after(expr: str, tz: str, after: float | None = None) -> float:
    base = datetime.fromtimestamp(after if after is not None else time.time(), ZoneInfo(tz))
    return croniter(expr, base).get_next(datetime).timestamp()


def today(tz: str) -> str:
    return datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d")


def now_text(tz: str) -> str:
    return datetime.now(ZoneInfo(tz)).strftime("%A %Y-%m-%d %H:%M")
