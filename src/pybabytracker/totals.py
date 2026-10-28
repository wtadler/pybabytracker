"""Stub."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from .models import Record

MILLILITERS_PER_FLUID_OUNCE = 29.5735295625


@dataclass(frozen=True, slots=True)
class DayTotals:
    diapers: int = 0
    wet: int = 0
    dirty: int = 0
    mixed: int = 0
    feeds: int = 0
    bottle_milliliters: float = 0.0
    start: datetime | None = None


def day_totals(
    records: Iterable[Record], baby_id: str, start: datetime, end: datetime
) -> DayTotals:
    return DayTotals(start=start)
