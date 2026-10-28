"""Stub."""

from __future__ import annotations

from datetime import datetime


def check_amount(amount: float) -> float:
    return amount


def check_pump_amounts(left: float | None, right: float | None) -> None:
    pass


def check_minutes(*minutes: int) -> None:
    pass


def check_end_after_start(start: datetime, end: datetime) -> None:
    pass


def check_sleep_range(start: datetime, end: datetime) -> None:
    pass


def check_sleep_start(start: datetime, now: datetime) -> None:
    pass
