"""Today's totals for one baby, counted from synced records.

The caller says what a day is: the library counts the records inside the bounds it is given. A
caller that wants a local calendar day passes local midnight to the next local midnight, which
makes a day with a clock change 23 or 25 hours long, as it is.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from .models import (
    DIAPER_DIRTY,
    DIAPER_MIXED,
    DIAPER_WET,
    UNIT_MILLILITERS,
    UNIT_OUNCES,
    Diaper,
    Formula,
    Nursing,
    Pumped,
    Record,
    Volume,
)

MILLILITERS_PER_FLUID_OUNCE = 29.5735295625


@dataclass(frozen=True, slots=True)
class DayTotals:
    """What one baby had logged inside one day."""

    diapers: int = 0
    wet: int = 0
    dirty: int = 0
    mixed: int = 0
    feeds: int = 0
    # Formula and pumped bottles together, in milliliters.
    bottle_milliliters: float = 0.0
    # The start of the day these totals cover.
    start: datetime | None = None


def _milliliters(volume: Volume | None) -> float:
    if volume is None:
        return 0.0
    if volume.unit == UNIT_OUNCES:
        return volume.value * MILLILITERS_PER_FLUID_OUNCE
    if volume.unit == UNIT_MILLILITERS:
        return float(volume.value)
    return 0.0


def day_totals(
    records: Iterable[Record], baby_id: str, start: datetime, end: datetime
) -> DayTotals:
    """Count a baby's records that happened from ``start`` up to, not including, ``end``.

    Only records the library models are counted: a raw record (a type the library lacks, or a
    known type it couldn't decode) is an ``UnknownRecord`` and matches none of the classes below.
    A bottle with no amount or no known unit is a feed that adds no volume.
    """
    diapers = wet = dirty = mixed = feeds = 0
    milliliters = 0.0
    for record in records:
        if record.time is None or record.baby_id != baby_id or not start <= record.time < end:
            continue
        if isinstance(record, Diaper):
            diapers += 1
            wet += record.status == DIAPER_WET
            dirty += record.status == DIAPER_DIRTY
            mixed += record.status == DIAPER_MIXED
        elif isinstance(record, Nursing | Formula | Pumped):
            feeds += 1
            if not isinstance(record, Nursing):
                milliliters += _milliliters(record.volume)
    return DayTotals(diapers, wet, dirty, mixed, feeds, milliliters, start)
