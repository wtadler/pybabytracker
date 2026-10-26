"""Builders for new records, in the shape the app writes them.

Each ``draft_*`` function checks its input at once (so a bad call is refused before the engine
makes any request) and returns a ``Draft``: everything about the record except the ID, the
baby, and the write timestamp, which the engine fills in under its lock. The payload shapes
follow the iPad capture (``tests/fixtures/captured_payloads.json``) key for key.

Conventions:

- ``time`` is when the activity started (a nursing session, a sleep, a pump) or when it
  happened (a bottle, a diaper). It's written in UTC. ``timezone`` is seconds east of UTC,
  defaulting to the offset of the time given.
- Volumes are ``Volume(value, unit)`` with unit ``"oz"`` or ``"ml"``, written as a
  ``VolumeMeasure``: ``englishMeasure`` "true" for ounces, "false" for milliliters. Only the
  ounce form has been captured; milliliters follow the model's assumption (see
  ``VolumeMeasure``). Whole numbers are written as integers, like the capture.
- Durations are whole minutes. A sleep's is its length rounded half up (the capture stored about
  30 seconds as 1 minute), and never less than 1 for a finished sleep, because 0 means a sleep
  in progress. Nursing and pump durations are given in whole minutes already.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import (
    DIAPER_DIRTY,
    DIAPER_MIXED,
    DIAPER_WET,
    SIDE_LEFT,
    SIDE_RIGHT,
    UNIT_MILLILITERS,
    UNIT_OUNCES,
    Baby,
    Diaper,
    Formula,
    Nursing,
    Pump,
    Pumped,
    Record,
    Sleep,
    Volume,
    VolumeMeasure,
)

MAX_MINUTES_PER_SIDE = 240
MAX_SLEEP = timedelta(hours=24)

# What the app filled in on the captured nursing add: a finished, detailed session.
_NURSING_FINISHED_STATE = 5


@dataclass(frozen=True, slots=True)
class Draft[R: Record]:
    """A validated record waiting for an ID, a baby, and a write timestamp."""

    record_type: type[R]
    _build: Callable[[str, Baby, datetime], R]

    def build(self, object_id: str, baby: Baby, timestamp: datetime) -> R:
        return self._build(object_id, baby, timestamp)


def _common(
    time: datetime | None, timezone_offset: int | None, note: str
) -> tuple[datetime, int, str]:
    """The event time in UTC (to the second), its offset, and the note."""
    event = time if time is not None else datetime.now().astimezone()
    offset = _offset(event, "time")
    return (
        event.astimezone(UTC).replace(microsecond=0),
        timezone_offset if timezone_offset is not None else offset,
        note,
    )


def _offset(moment: datetime, name: str) -> int:
    offset = moment.utcoffset()
    if offset is None:
        raise ValueError(f"{name} must be timezone-aware")
    return int(offset.total_seconds())


def _number(value: float) -> int | float:
    """A volume as the app writes it: rounded to 2 decimals, an integer when whole."""
    rounded = round(float(value), 2)
    return int(rounded) if rounded.is_integer() else rounded


def _checked(volume: object, name: str = "volume") -> Volume:
    if not isinstance(volume, Volume):
        raise ValueError(f"{name} must be a Volume(value, unit)")
    if volume.unit == UNIT_MILLILITERS:
        raise ValueError(
            "metric volumes aren't supported until the app's metric format is confirmed: use ounces"
        )
    if volume.unit != UNIT_OUNCES:
        raise ValueError(f"unknown {name} unit {volume.unit!r}: use 'oz'")
    value = volume.value
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if value < 0:
        raise ValueError(f"{name} can't be negative")
    return Volume(_number(value), volume.unit)


def _measure(volume: Volume) -> VolumeMeasure:
    return VolumeMeasure(_number(volume.value), "true" if volume.unit == UNIT_OUNCES else "false")


def _minutes(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a whole number of minutes")
    if value < 0:
        raise ValueError(f"{name} can't be negative")
    if value > MAX_MINUTES_PER_SIDE:
        raise ValueError(f"{name} can't be more than {MAX_MINUTES_PER_SIDE}")
    return value


def _side(finish_side: int | None, left_used: bool, right_used: bool) -> int:
    """The side finished on: as given, else the last side used (right when both were)."""
    if finish_side is not None:
        if finish_side not in (SIDE_LEFT, SIDE_RIGHT):
            raise ValueError(f"finish_side must be SIDE_LEFT or SIDE_RIGHT, not {finish_side!r}")
        return finish_side
    return SIDE_LEFT if left_used and not right_used else SIDE_RIGHT


def _base(
    object_id: str, baby: Baby, timestamp: datetime, event: datetime, tz: int, text: str
) -> dict[str, Any]:
    """The fields every new record carries."""
    return {
        "object_id": object_id,
        "time": event,
        "timestamp": timestamp,
        "timezone": tz,
        "note": text,
        "picture_note": [],
        "new_flag": "true",
        "picture_loaded": "true",
        "baby": baby,
    }


def draft_diaper(
    status: int, *, time: datetime | None, timezone_offset: int | None, note: str
) -> Draft[Diaper]:
    if status not in (DIAPER_WET, DIAPER_DIRTY, DIAPER_MIXED):
        raise ValueError(f"unknown diaper status: {status!r}")
    event, tz, text = _common(time, timezone_offset, note)

    def build(object_id: str, baby: Baby, timestamp: datetime) -> Diaper:
        return Diaper(
            **_base(object_id, baby, timestamp, event, tz, text),
            status=status,
            # What the app filled in on the captured add (PROTOCOL.md) with only a type set.
            amount=2,
            pee_color=5,
            poo_color=5,
            texture=5,
            flag=0,
        )

    return Draft(Diaper, build)


def draft_bottle[R: (Formula, Pumped)](
    record_type: type[R],
    volume: Volume,
    *,
    time: datetime | None,
    timezone_offset: int | None,
    note: str,
) -> Draft[R]:
    """A formula or pumped-milk bottle."""
    checked = _checked(volume)
    if checked.value <= 0:
        raise ValueError("a bottle's volume must be more than zero")  # also after rounding
    event, tz, text = _common(time, timezone_offset, note)

    def build(object_id: str, baby: Baby, timestamp: datetime) -> R:
        return record_type(
            **_base(object_id, baby, timestamp, event, tz, text),
            amount=_measure(checked),
        )

    return Draft(record_type, build)


def draft_nursing(
    *,
    left_minutes: int,
    right_minutes: int,
    finish_side: int | None,
    start_time: datetime | None,
    end_time: datetime | None,
    timezone_offset: int | None,
    note: str,
) -> Draft[Nursing]:
    """A finished nursing session. ``time`` is its start: ``start_time``, or ``end_time`` minus
    the total minutes, or (given neither) now minus the total minutes."""
    left = _minutes(left_minutes, "left_minutes")
    right = _minutes(right_minutes, "right_minutes")
    if left + right == 0:
        raise ValueError("a nursing session needs at least one minute on a side")
    if start_time is not None and end_time is not None:
        raise ValueError("give start_time or end_time, not both")
    side = _side(finish_side, left > 0, right > 0)
    total = timedelta(minutes=left + right)
    if start_time is not None:
        _offset(start_time, "start_time")
        start = start_time
    else:
        end = end_time if end_time is not None else datetime.now().astimezone()
        _offset(end, "end_time")
        start = end - total
    event, tz, text = _common(start, timezone_offset, note)

    def build(object_id: str, baby: Baby, timestamp: datetime) -> Nursing:
        return Nursing(
            **_base(object_id, baby, timestamp, event, tz, text),
            left_duration=left,
            right_duration=right,
            both_duration=0,
            finish_side=side,
            is_detailed="true",
            pair_session_state=0,
            state=_NURSING_FINISHED_STATE,
        )

    return Draft(Nursing, build)


def draft_sleep(
    *, start: datetime, end: datetime, timezone_offset: int | None, note: str
) -> Draft[Sleep]:
    """A finished sleep from ``start`` to ``end``."""
    _offset(start, "start")
    _offset(end, "end")
    if end <= start:
        raise ValueError("end must be after start")
    if end - start > MAX_SLEEP:
        raise ValueError("a sleep can't be longer than 24 hours")
    seconds = (end - start).total_seconds()
    minutes = max(1, math.floor(seconds / 60 + 0.5))
    event, tz, text = _common(start, timezone_offset, note)

    def build(object_id: str, baby: Baby, timestamp: datetime) -> Sleep:
        return Sleep(
            **_base(object_id, baby, timestamp, event, tz, text),
            duration=minutes,
        )

    return Draft(Sleep, build)


def draft_pump(
    *,
    left_volume: Volume | None,
    right_volume: Volume | None,
    left_minutes: int,
    right_minutes: int,
    finish_side: int | None,
    time: datetime | None,
    timezone_offset: int | None,
    note: str,
) -> Draft[Pump]:
    """A pumping session. ``amount`` is the sum of the sides; a side left out is written as 0
    in the other side's unit. ``time`` is the session's start."""
    if left_volume is None and right_volume is None:
        raise ValueError("a pump needs a volume for at least one side")
    left_v = _checked(left_volume, "left_volume") if left_volume is not None else None
    right_v = _checked(right_volume, "right_volume") if right_volume is not None else None
    units = {v.unit for v in (left_v, right_v) if v is not None}
    if len(units) != 1:
        raise ValueError("both sides of a pump must use the same unit")
    unit = units.pop()
    left_amount = left_v.value if left_v is not None else 0
    right_amount = right_v.value if right_v is not None else 0
    total = round(left_amount + right_amount, 6)
    if total <= 0:
        raise ValueError("a pump's total volume must be more than zero")
    left = _minutes(left_minutes, "left_minutes")
    right = _minutes(right_minutes, "right_minutes")
    side = _side(finish_side, left_amount > 0 or left > 0, right_amount > 0 or right > 0)
    event, tz, text = _common(time, timezone_offset, note)

    def build(object_id: str, baby: Baby, timestamp: datetime) -> Pump:
        return Pump(
            **_base(object_id, baby, timestamp, event, tz, text),
            amount=_measure(Volume(total, unit)),
            left_amount=_measure(Volume(left_amount, unit)),
            right_amount=_measure(Volume(right_amount, unit)),
            left_duration=left,
            right_duration=right,
            finish_side=side,
            # Always 0 on the capture, which pumped both sides; its meaning isn't known.
            sides=0,
        )

    return Draft(Pump, build)
