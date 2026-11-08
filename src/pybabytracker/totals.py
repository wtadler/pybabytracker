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
    SIDE_LEFT,
    SIDE_NONE,
    SIDE_RIGHT,
    UNIT_MILLILITERS,
    UNIT_OUNCES,
    Diaper,
    Formula,
    Nursing,
    Pump,
    Pumped,
    Record,
    Volume,
    numeric_code,
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
    # Nursing sessions (an Android timer's NursingSession included) and their minutes: left,
    # right, and both sides at once, added up.
    nursings: int = 0
    nursing_minutes: int = 0


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
    diapers = wet = dirty = mixed = feeds = nursings = nursing_minutes = 0
    milliliters = 0.0
    for record in records:
        if record.time is None or record.baby_id != baby_id or not start <= record.time < end:
            continue
        if isinstance(record, Diaper):
            diapers += 1
            status = numeric_code(record.status)  # a string of digits from Android
            wet += status == DIAPER_WET
            dirty += status == DIAPER_DIRTY
            mixed += status == DIAPER_MIXED
        elif isinstance(record, Nursing | Formula | Pumped):
            feeds += 1
            if isinstance(record, Nursing):
                nursings += 1
                nursing_minutes += _session_minutes(record)
            else:
                milliliters += _milliliters(record.volume)
    return DayTotals(
        diapers, wet, dirty, mixed, feeds, milliliters, start, nursings, nursing_minutes
    )


def _session_minutes(nursing: Nursing) -> int:
    """A session's minutes on the left, on the right, and on both at once, added up.

    A side that isn't a whole number of minutes (or a string of digits, as Android may send)
    adds nothing, and nor does a negative one.
    """
    sides = (nursing.left_duration, nursing.right_duration, nursing.both_duration)
    return sum(max(minutes, 0) for side in sides if (minutes := numeric_code(side)) is not None)


@dataclass(frozen=True, slots=True)
class LastBreast:
    """The newest nursing or pumping session, as the app shows it before the next one.

    ``kind`` is ``"nursing"`` or ``"pump"``. ``side`` is the session's ``finishSide``:
    ``SIDE_LEFT``, ``SIDE_RIGHT``, or ``SIDE_NONE`` when it has none. The app labels that field
    as the side a session started on.
    """

    kind: str
    side: int
    time: datetime


def last_breast(records: Iterable[Record], now: datetime) -> LastBreast | None:
    """The newest nursing or pump up to ``now``, whichever baby it was for, as the app picks it.

    The app's nursing and pump screens show this as the last side: the newest nursing of any
    baby (the same parent feeds them all) and the newest pump without a ``label`` (a milk-stash
    adjustment has one), not after now. The pump wins a tie. ``None`` when there is neither.
    """
    newest_nursing: tuple[datetime, Nursing] | None = None
    newest_pump: tuple[datetime, Pump] | None = None
    for record in records:
        time = record.time
        if time is None or time > now:
            continue
        if isinstance(record, Nursing):
            if newest_nursing is None or time > newest_nursing[0]:
                newest_nursing = (time, record)
        elif (isinstance(record, Pump) and not record.label) and (
            newest_pump is None or time > newest_pump[0]
        ):
            newest_pump = (time, record)
    chosen: Nursing | Pump
    if newest_nursing is not None and (newest_pump is None or newest_nursing[0] > newest_pump[0]):
        (time, chosen), kind = newest_nursing, "nursing"
    elif newest_pump is not None:
        (time, chosen), kind = newest_pump, "pump"
    else:
        return None
    side = numeric_code(chosen.finish_side)
    if side not in (SIDE_LEFT, SIDE_RIGHT):
        side = SIDE_NONE
    return LastBreast(kind, side, time)
