"""Builders for new records, in the shape the app writes them.

Each ``draft_*`` function checks its input at once (so a bad call is refused before the engine
makes any request) and returns a ``Draft``: everything about the record except the ID, the
baby, and the write timestamp, which the engine fills in under its lock. The payload shapes
follow the iPad capture (``tests/fixtures/captured_payloads.json``) key for key.

Conventions:

- ``time`` is when the activity started (a nursing session, a sleep, a pump) or when it
  happened (a bottle, a diaper). It's written in UTC. ``timezone`` is seconds east of UTC,
  defaulting to the offset of the time given. The app itself writes the zone's *standard*
  offset all year, ignoring daylight saving (observed 2026-10-26), so pass that to match it:
  ``standard_offset_seconds(tz, at)`` computes it. The default is the time's actual offset,
  which in summer differs from the app's by an hour.
- Volumes are ``Volume(value, unit)`` with unit ``"oz"`` or ``"ml"``, written as a
  ``VolumeMeasure``: ``englishMeasure`` "true" for ounces, "false" for milliliters, with the
  number as given and no conversion. Both forms were captured from the app (ounces and
  milliliters on 2026-10-26). Whole numbers are written as integers, like the capture.
- Durations are whole minutes. A sleep's is its length rounded half up (the capture stored about
  30 seconds as 1 minute), and never less than 1 for a finished sleep, because 0 means a sleep
  in progress. Nursing and pump durations are given in whole minutes already.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .exceptions import NoUsualDoseError
from .models import (
    DIAPER_DIRTY,
    DIAPER_MIXED,
    DIAPER_WET,
    SIDE_LEFT,
    SIDE_NONE,
    SIDE_RIGHT,
    UNIT_CELSIUS,
    UNIT_CENTIMETERS,
    UNIT_FAHRENHEIT,
    UNIT_INCHES,
    UNIT_KILOGRAMS,
    UNIT_MILLILITERS,
    UNIT_OUNCES,
    UNIT_POUNDS,
    Baby,
    Diaper,
    Formula,
    Growth,
    Measurement,
    Medication,
    MedicationSelection,
    Milestone,
    Nursing,
    OtherActivity,
    OtherFeed,
    OtherFeedSelection,
    Pump,
    Pumped,
    Record,
    Sleep,
    Temperature,
    Vaccine,
    Volume,
    VolumeMeasure,
    _selection_name,
)
from .models import _number as _plain_number
from .times import format_time

MAX_MINUTES_PER_SIDE = 240
MAX_SLEEP = timedelta(hours=24)
MAX_SLEEP_MINUTES = 24 * 60
# How far ahead of this clock a timer's start may be: phones and Home Assistant disagree a little.
START_SKEW = timedelta(minutes=1)


# What the app filled in on the captured nursing add: a finished, detailed session.
_NURSING_FINISHED_STATE = 5


@dataclass(frozen=True, slots=True)
class Draft[R: Record]:
    """A validated record waiting for an ID, a baby, and a write timestamp."""

    record_type: type[R]
    _build: Callable[[str, Baby | None, datetime], R]

    def build(self, object_id: str, baby: Baby | None, timestamp: datetime) -> R:
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


def _finite(value: float) -> bool:
    """Whether a number is finite; false for an int too large for a float."""
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _number(value: float) -> int | float:
    """A volume or dose as the app writes it: rounded to 2 decimals, an integer when whole."""
    rounded = round(float(value), 2)
    return int(rounded) if rounded.is_integer() else rounded


def _checked(volume: object, name: str = "volume") -> Volume:
    if not isinstance(volume, Volume):
        raise ValueError(f"{name} must be a Volume(value, unit)")
    if volume.unit not in (UNIT_OUNCES, UNIT_MILLILITERS):
        raise ValueError(f"unknown {name} unit {volume.unit!r}: use 'oz' or 'ml'")
    value = volume.value
    if isinstance(value, bool) or not isinstance(value, int | float) or not _finite(value):
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
    """The record's side (``finishSide``, which the app shows as the side a session started on):
    as given, else the only side used, else ``SIDE_NONE`` when both were, since the start can't be
    told from the totals. The app also saves a two-sided pump with no side."""
    if finish_side is not None:
        if isinstance(finish_side, bool) or finish_side not in (SIDE_NONE, SIDE_LEFT, SIDE_RIGHT):
            raise ValueError(
                f"finish_side must be SIDE_LEFT, SIDE_RIGHT, or SIDE_NONE, not {finish_side!r}"
            )
        return finish_side
    if left_used and right_used:
        return SIDE_NONE
    return SIDE_LEFT if left_used else SIDE_RIGHT


def _base(
    object_id: str, baby: Baby | None, timestamp: datetime, event: datetime, tz: int, text: str
) -> dict[str, Any]:
    """The fields every new record carries; ``baby`` is left out when there is none."""
    common: dict[str, Any] = {
        "object_id": object_id,
        "time": event,
        "timestamp": timestamp,
        "timezone": tz,
        "note": text,
        "picture_note": [],
        "new_flag": "true",
        "picture_loaded": "true",
    }
    if baby is not None:
        common["baby"] = baby
    return common


def draft_diaper(
    status: int, *, time: datetime | None, timezone_offset: int | None, note: str
) -> Draft[Diaper]:
    if status not in (DIAPER_WET, DIAPER_DIRTY, DIAPER_MIXED):
        raise ValueError(f"unknown diaper status: {status!r}")
    event, tz, text = _common(time, timezone_offset, note)

    def build(object_id: str, baby: Baby | None, timestamp: datetime) -> Diaper:
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


def draft_milestone(
    selection: Mapping[str, Any],
    *,
    time: datetime | None,
    timezone_offset: int | None,
    note: str,
) -> Draft[Milestone]:
    """A milestone reached, embedding the milestone it marks as ``milestoneType``."""
    embedded = dict(selection)
    event, tz, text = _common(time, timezone_offset, note)

    def build(object_id: str, baby: Baby | None, timestamp: datetime) -> Milestone:
        return Milestone(
            **_base(object_id, baby, timestamp, event, tz, text),
            milestone_type=dict(embedded),
            # false: the milestone was picked from the list, not typed in. The value we expect
            # the apps to send; not yet seen in synced data.
            has_new_type="false",
        )

    return Draft(Milestone, build)


# The longest activity, in minutes: a day. A guard against seconds passed as minutes.
MAX_ACTIVITY_MINUTES = 24 * 60


def draft_activity(
    selection: Mapping[str, Any],
    *,
    duration: int,
    time: datetime | None,
    timezone_offset: int | None,
    note: str,
) -> Draft[OtherActivity]:
    """An activity, such as tummy time or a bath, embedding its type as ``desc``.

    ``duration`` is whole minutes, 0 when not timed, as the apps send it. The record has the
    keys the iPhone app sends, and not ``descID`` or ``locationID``, which the model reads but
    the app keeps in its local database.
    """
    if isinstance(duration, bool) or not isinstance(duration, int):
        raise ValueError("duration must be a whole number of minutes")
    if duration < 0:
        raise ValueError("duration can't be negative")
    if duration > MAX_ACTIVITY_MINUTES:
        raise ValueError(f"duration can't be more than {MAX_ACTIVITY_MINUTES} minutes")
    embedded = dict(selection)
    event, tz, text = _common(time, timezone_offset, note)

    def build(object_id: str, baby: Baby | None, timestamp: datetime) -> OtherActivity:
        return OtherActivity(
            **_base(object_id, baby, timestamp, event, tz, text),
            desc=dict(embedded),
            duration=duration,
        )

    return Draft(OtherActivity, build)


def _checked_measure(
    name: str,
    reading: object,
    ranges: Mapping[str, tuple[float, float]],
    object_type: str,
    english_units: set[str],
) -> dict[str, Any]:
    """A checked measurement as the iPhone app syncs one: ``object_type``, ``englishMeasure``
    "true" for a unit in ``english_units`` and "false" otherwise, and the number rounded as
    ``_number`` does. Refuses anything but a ``Measurement`` of a unit in ``ranges``, a finite
    number (not a bool), within that unit's range.
    """
    if not isinstance(reading, Measurement):
        raise ValueError(f"{name} must be a Measurement(value, unit)")
    if reading.unit not in ranges:
        units = " or ".join(repr(unit) for unit in ranges)
        raise ValueError(f"unknown {name} unit {reading.unit!r}: use {units}")
    value = reading.value
    if isinstance(value, bool) or not isinstance(value, int | float) or not _finite(value):
        raise ValueError(f"{name} must be a finite number")
    low, high = ranges[reading.unit]
    if not low <= value <= high:
        raise ValueError(
            f"a {name} in {reading.unit} must be from {low:g} to {high:g}, not {value:g}"
        )
    return {
        "BCObjectType": object_type,
        "englishMeasure": "true" if reading.unit in english_units else "false",
        "value": _number(value),
    }


# What each growth measurement may be, by unit: from a premature newborn to a child, as a guard
# against a typo or a measurement passed in the wrong field. The metric bounds match the
# imperial ones, so a reading near a limit is treated the same in either unit.
GROWTH_RANGES: dict[str, dict[str, tuple[float, float]]] = {
    "weight": {UNIT_POUNDS: (0.5, 100.0), UNIT_KILOGRAMS: (0.23, 45.0)},
    "length": {UNIT_INCHES: (5.0, 60.0), UNIT_CENTIMETERS: (12.7, 152.0)},
    "head": {UNIT_INCHES: (5.0, 30.0), UNIT_CENTIMETERS: (12.7, 76.0)},
}
_GROWTH_MEASURES = {"weight": "WeightMeasure", "length": "LengthMeasure", "head": "LengthMeasure"}
_GROWTH_US_UNITS = {UNIT_POUNDS, UNIT_INCHES}


def draft_growth(
    *,
    weight: Measurement | None,
    length: Measurement | None,
    head: Measurement | None,
    time: datetime | None,
    timezone_offset: int | None,
    note: str,
) -> Draft[Growth]:
    """A growth measurement of any of weight, length, and head (at least one).

    Each is a ``WeightMeasure`` or ``LengthMeasure`` with ``englishMeasure`` "true" for pounds
    or inches, the number rounded to two decimals (a whole number is written as an integer). A
    weight in pounds is decimal pounds, as the apps store it (9 lb 4 oz is 9.25). One not taken
    is left out, as in the account seed, since the apps read 0 as not measured. Length and head
    share one unit in the apps, so they must share it here.
    """
    given = {"weight": weight, "length": length, "head": head}
    measures = {
        name: _checked_measure(
            name, reading, GROWTH_RANGES[name], _GROWTH_MEASURES[name], _GROWTH_US_UNITS
        )
        for name, reading in given.items()
        if reading is not None
    }
    if not measures:
        raise ValueError("give at least one of weight, length, and head")
    if length is not None and head is not None and length.unit != head.unit:
        raise ValueError("length and head must be in the same unit")
    event, tz, text = _common(time, timezone_offset, note)

    def build(object_id: str, baby: Baby | None, timestamp: datetime) -> Growth:
        # Each record gets its own copy of every measure.
        own = {name: dict(measure) for name, measure in measures.items()}
        return Growth(
            **_base(object_id, baby, timestamp, event, tz, text),
            weight=own.get("weight"),
            length=own.get("length"),
            head=own.get("head"),
        )

    return Draft(Growth, build)


# The temperatures a reading may be, by unit: a body's, with room to spare. A guard against a
# Celsius number given as Fahrenheit (37 °F) or the reverse (98.6 °C).
TEMPERATURE_RANGES: dict[str, tuple[float, float]] = {
    UNIT_FAHRENHEIT: (86.0, 113.0),
    UNIT_CELSIUS: (30.0, 45.0),
}


def draft_temperature(
    reading: Measurement,
    *,
    time: datetime | None,
    timezone_offset: int | None,
    note: str,
) -> Draft[Temperature]:
    """A temperature, written as the iPhone app syncs one: a ``TemperatureMeasure`` with
    ``englishMeasure`` "true" for Fahrenheit and "false" for Celsius, the number rounded to two
    decimals (a whole number is written as an integer)."""
    measure = _checked_measure(
        "temperature", reading, TEMPERATURE_RANGES, "TemperatureMeasure", {UNIT_FAHRENHEIT}
    )
    event, tz, text = _common(time, timezone_offset, note)

    def build(object_id: str, baby: Baby | None, timestamp: datetime) -> Temperature:
        return Temperature(
            **_base(object_id, baby, timestamp, event, tz, text), temperature=dict(measure)
        )

    return Draft(Temperature, build)


# The largest solid food amount: a guard against a typo, since the unit is free text, not a limit
# the app is known to enforce.
MAX_OTHER_FEED_AMOUNT = 1000
# The longest unit, in characters (a guard).
MAX_UNIT_LENGTH = 30
# The timestamp the iPhone app gives a meal's description and an embedded food.
_EMBEDDED_TIMESTAMP = "1970-01-01 00:00:00 +0000"


def _embedded(type_name: str, selection: Mapping[str, Any]) -> dict[str, Any]:
    """A pick-list entry as the iPhone app embeds it in a record: typed, not new, and dated 1970."""
    return {
        "BCObjectType": type_name,
        **selection,
        "newFlage": "false",
        "timestamp": _EMBEDDED_TIMESTAMP,
    }


def draft_other_feed(
    selection: Mapping[str, Any],
    *,
    amount: float,
    unit: str,
    meal_id: str,
    time: datetime | None,
    timezone_offset: int | None,
    note: str,
) -> Draft[OtherFeed]:
    """A solid food (or a drink other than milk), embedding the food as ``feedType``.

    ``amount`` counts ``unit``, which is free text such as "Oz", "Pieces", or "Tsp". The iPhone
    app writes the amount as a ``VolumeMeasure`` with ``englishMeasure`` "true" whatever the
    unit, so this does too. ``desc`` is the meal: an unnamed ``FeedDescription`` whose
    ``objectID`` is shared by the foods logged together, as the app does, so give each food of
    one meal the same ``meal_id``.
    """
    if isinstance(amount, bool) or not isinstance(amount, int | float):
        raise ValueError("amount must be a number")
    if not _finite(amount) or _number(amount) <= 0:
        raise ValueError("amount must be more than zero")
    if amount > MAX_OTHER_FEED_AMOUNT:
        raise ValueError(f"amount can't be more than {MAX_OTHER_FEED_AMOUNT}")
    if not isinstance(unit, str) or not unit.strip():
        raise ValueError("unit must be text, such as 'Oz' or 'Pieces'")
    if len(unit.strip()) > MAX_UNIT_LENGTH:
        raise ValueError(f"unit can't be longer than {MAX_UNIT_LENGTH} characters")
    if not isinstance(meal_id, str) or not meal_id.strip():
        raise ValueError("meal_id must be text")
    embedded = _embedded("OtherFeedSelection", selection)
    written_unit = unit.strip()
    value = _number(amount)
    desc = {
        "BCObjectType": "FeedDescription",
        "newFlage": "true",
        "objectID": meal_id.strip(),
        "timestamp": _EMBEDDED_TIMESTAMP,
    }
    event, tz, text = _common(time, timezone_offset, note)

    def build(object_id: str, baby: Baby | None, timestamp: datetime) -> OtherFeed:
        return OtherFeed(
            **_base(object_id, baby, timestamp, event, tz, text),
            feed_type=dict(embedded),
            desc=dict(desc),
            amount=VolumeMeasure(value, "true"),
            unit=written_unit,
        )

    return Draft(OtherFeed, build)


# The longest name of a food added to the pick list, in characters.
MAX_FOOD_NAME_LENGTH = 50


def draft_food(name: str, *, bottle: bool) -> Draft[OtherFeedSelection]:
    """A new food for the pick list, as both apps sync one: ``BCObjectType``, ``objectID``,
    ``name``, ``bottle``, ``newFlage`` "true", and ``timestamp``, with no record fields.

    ``bottle`` true offers it on the bottle screen as a drink. The name is written without
    surrounding spaces.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("a food's name must be text")
    written = name.strip()
    if len(written) > MAX_FOOD_NAME_LENGTH:
        raise ValueError(f"a food's name can't be longer than {MAX_FOOD_NAME_LENGTH} characters")
    if not isinstance(bottle, bool):
        raise ValueError("bottle must be True or False")

    def build(object_id: str, baby: Baby | None, timestamp: datetime) -> OtherFeedSelection:
        return OtherFeedSelection(
            object_id=object_id,
            name=written,
            bottle="true" if bottle else "false",
            new_flag="true",
            timestamp=timestamp,
        )

    return Draft(OtherFeedSelection, build)


# The longest gap between doses. The app takes whole hours; an iPhone has synced 555, which is a
# sighting, not a limit the app is known to enforce.
MAX_MEDICINE_INTERVAL = timedelta(hours=999)
# The largest usual dose: a guard against a typo, like MAX_OTHER_FEED_AMOUNT.
MAX_MEDICINE_AMOUNT = 1000


def _text(value: object, what: str, longest: int) -> str:
    """``value`` without surrounding spaces, or ``ValueError`` unless it is text of 1 to
    ``longest`` characters."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be text")
    if len(value.strip()) > longest:
        raise ValueError(f"{what} can't be longer than {longest} characters")
    return value.strip()


def draft_medicine(
    name: str,
    *,
    unit: str,
    amount_per_time: float,
    interval: timedelta,
    prescription: bool,
    description: str,
) -> Draft[MedicationSelection]:
    """A new medicine for the pick list, as the iPhone app syncs one (captured 2026-11-11):
    ``BCObjectType``, ``objectID``, ``name``, ``desc``, ``unit``, ``amountPerTime``,
    ``interval`` in minutes, ``isPresciption`` (the app's spelling) "true" or "false",
    ``newFlage`` "true", and ``timestamp``, with no record fields.

    ``interval`` is the time between doses, a whole number of hours as the app takes it, at
    most ``MAX_MEDICINE_INTERVAL``. ``amount_per_time`` is the usual dose in ``unit``, which is
    free text such as "ml" or "Drops". Text is written without surrounding spaces.
    """
    written_name = _text(name, "a medicine's name", MAX_FOOD_NAME_LENGTH)
    written_unit = _text(unit, "a medicine's unit", MAX_UNIT_LENGTH)
    if isinstance(amount_per_time, bool) or not isinstance(amount_per_time, int | float):
        raise ValueError("a medicine's usual dose must be a number")
    if amount_per_time > MAX_MEDICINE_AMOUNT:
        raise ValueError(f"a medicine's usual dose can't be more than {MAX_MEDICINE_AMOUNT}")
    if not _finite(amount_per_time) or _number(amount_per_time) <= 0:
        raise ValueError("a medicine's usual dose must be a number more than zero")
    if not isinstance(interval, timedelta):
        raise ValueError("interval must be a timedelta, such as timedelta(hours=4)")
    if interval <= timedelta(0) or interval > MAX_MEDICINE_INTERVAL:
        raise ValueError(f"interval must be more than zero and at most {MAX_MEDICINE_INTERVAL}")
    if interval % timedelta(hours=1):
        raise ValueError("interval must be a whole number of hours, as the app takes it")
    if not isinstance(prescription, bool):
        raise ValueError("prescription must be True or False")
    if not isinstance(description, str):
        raise ValueError("a medicine's description must be text")
    written_desc = description.strip()
    usual = _number(amount_per_time)
    minutes = int(interval / timedelta(minutes=1))
    flag = "true" if prescription else "false"

    def build(object_id: str, baby: Baby | None, timestamp: datetime) -> MedicationSelection:
        return MedicationSelection(
            object_id=object_id,
            name=written_name,
            desc=written_desc,
            unit=written_unit,
            amount_per_time=usual,
            interval=minutes,
            new_flag="true",
            timestamp=timestamp,
            extra={"isPresciption": flag},
        )

    return Draft(MedicationSelection, build)


def _usual_dose(selection: Mapping[str, Any]) -> int | float | None:
    """A medicine's usual dose (``amountPerTime``), or ``None`` unless it is a number that is
    still above zero once written. Text, a bool, null, zero, and a non-finite number are not a
    usual dose: the caller must give an amount rather than have one guessed."""
    usual = _plain_number(selection.get("amountPerTime"))
    if usual is None or not _finite(usual) or _number(usual) <= 0:
        return None
    return _number(usual)


def draft_medication(
    selection: Mapping[str, Any],
    amount: float | None,
    *,
    time: datetime | None,
    timezone_offset: int | None,
    note: str,
) -> Draft[Medication]:
    """A dose of a medicine: ``amount`` in its unit (default its usual dose, ``amountPerTime``),
    with the medicine embedded as ``medicationSelection``. An iPhone-shaped medicine is embedded
    as the iPhone app does (typed, ``newFlage`` "false", the 1970 timestamp: captured
    2026-11-11); one of any other shape, such as an Android phone's, is embedded as it synced."""
    # iPhone-shaped means it has the iPhone's "isPresciption" flag. An Android phone's medicine
    # has "prescription" instead, with JSON booleans, and nothing shows how either app reads a
    # mix of the two, so it keeps its own shape.
    embedded = (
        _embedded("MedicationSelection", selection)
        if "isPresciption" in selection
        else dict(selection)
    )
    if amount is None:
        amount = _usual_dose(embedded)
        if amount is None:
            raise NoUsualDoseError(_selection_name(embedded))
    if isinstance(amount, bool) or not isinstance(amount, int | float) or not _finite(amount):
        raise ValueError("a dose's amount must be a finite number")
    if amount <= 0:
        raise ValueError("a dose's amount must be more than zero")
    dose = _number(amount)
    if dose <= 0:
        raise ValueError("a dose's amount must be more than zero")  # also after rounding
    event, tz, text = _common(time, timezone_offset, note)

    def build(object_id: str, baby: Baby | None, timestamp: datetime) -> Medication:
        return Medication(
            **_base(object_id, baby, timestamp, event, tz, text),
            amount=dose,
            medication_selection=dict(embedded),
        )

    return Draft(Medication, build)


def draft_vaccine(
    selection: Mapping[str, Any],
    *,
    time: datetime | None,
    timezone_offset: int | None,
    note: str,
) -> Draft[Vaccine]:
    """A vaccine given, embedding the vaccine as ``vaccineType``.

    The record carries the keys we expect the apps to send (expected shape, not yet seen in
    synced data), and not ``vaccID`` or ``docVisitID``, which the model reads but the app keeps
    in its local database.
    """
    embedded = dict(selection)
    event, tz, text = _common(time, timezone_offset, note)

    def build(object_id: str, baby: Baby | None, timestamp: datetime) -> Vaccine:
        return Vaccine(
            **_base(object_id, baby, timestamp, event, tz, text), vaccine_type=dict(embedded)
        )

    return Draft(Vaccine, build)


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

    def build(object_id: str, baby: Baby | None, timestamp: datetime) -> R:
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

    def build(object_id: str, baby: Baby | None, timestamp: datetime) -> Nursing:
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
    minutes = sleep_minutes(start, end)
    event, tz, text = _common(start, timezone_offset, note)

    def build(object_id: str, baby: Baby | None, timestamp: datetime) -> Sleep:
        return Sleep(
            **_base(object_id, baby, timestamp, event, tz, text),
            duration=minutes,
        )

    return Draft(Sleep, build)


def sleep_minutes(start: datetime, end: datetime) -> int:
    """A finished sleep's duration: whole minutes rounded half up, at least 1 (0 means running).
    The caller checks that ``end`` is after ``start``."""
    seconds = (end - start).total_seconds()
    return max(1, math.floor(seconds / 60 + 0.5))


def draft_sleep_start(
    *, start: datetime | None, timezone_offset: int | None, note: str, now: datetime | None = None
) -> Draft[Sleep]:
    """A running sleep: ``duration`` 0, as the app posts at a timer's start. ``start`` defaults to
    now; it can't be in the future (beyond a minute of clock skew) or more than 24 hours ago,
    since such a sleep could never be ended inside the cap."""
    current = now if now is not None else datetime.now(UTC)
    begin = start if start is not None else current
    _offset(begin, "start")
    if begin - current > START_SKEW:
        raise ValueError("a sleep can't start in the future")
    if current - begin > MAX_SLEEP:
        raise ValueError("a sleep can't have started more than 24 hours ago")
    event, tz, text = _common(begin, timezone_offset, note)

    def build(object_id: str, baby: Baby | None, timestamp: datetime) -> Sleep:
        return Sleep(
            **_base(object_id, baby, timestamp, event, tz, text),
            duration=0,
        )

    return Draft(Sleep, build)


@dataclass(frozen=True, slots=True)
class SleepEnd:
    """How a running sleep ends: the minutes to write, and what was adjusted to get them."""

    minutes: int
    end: datetime
    future_clamped: bool = False  # ``end`` was more than START_SKEW ahead, so now was used
    long_clamped: bool = False  # longer than 24 hours, so 1,440 minutes are written


def resolve_sleep_end(sleep: Sleep, end: datetime, now: datetime) -> SleepEnd:
    """Check that ``end`` can end ``sleep`` at ``now``, and work out the duration to write.

    - ``end`` must be aware. One more than ``START_SKEW`` ahead of ``now`` is clamped to ``now``.
    - An end at or before the start, by up to ``START_SKEW``, is a 1-minute sleep: a start saved
      by a clock running a little fast (a phone's, or this one's within ``start_sleep``'s skew)
      can always be stopped. An end further before the start raises ``ValueError``.
    - Otherwise ``sleep_minutes``. A sleep longer than 24 hours is clamped to 1,440 minutes
      rather than refused, so a timer someone forgot can always be stopped; the length can be
      fixed in the app afterward.
    """
    _offset(end, "end")
    start = sleep.time
    if start is None:
        raise ValueError(f"sleep {sleep.object_id} has no start time")
    future = end - now > START_SKEW
    if future:
        end = now
    if end <= start:
        if start - end > START_SKEW:
            raise ValueError("end must be after the sleep's start")
        return SleepEnd(1, end, future_clamped=future)
    if end - start > MAX_SLEEP:
        return SleepEnd(MAX_SLEEP_MINUTES, end, future_clamped=future, long_clamped=True)
    return SleepEnd(sleep_minutes(start, end), end, future_clamped=future)


def ended_sleep_payload(sleep: Sleep, minutes: int, timestamp: datetime) -> dict[str, Any]:
    """The update that ends a running sleep, as the app posts it at a timer's stop: the record as
    last saved (key order and unknown keys kept), with only ``duration``, ``newFlage`` "false",
    and ``timestamp`` changed."""
    payload = sleep.to_payload()
    payload["duration"] = minutes
    payload["newFlage"] = "false"
    payload["timestamp"] = format_time(timestamp.astimezone(UTC))
    return payload


def draft_pump(
    *,
    amount: Volume | None,
    left_volume: Volume | None,
    right_volume: Volume | None,
    left_minutes: int,
    right_minutes: int,
    finish_side: int | None,
    time: datetime | None,
    timezone_offset: int | None,
    note: str,
    default_unit: str = UNIT_OUNCES,
) -> Draft[Pump]:
    """A pumping session, which belongs to the account (no baby). Give the total as ``amount``,
    or the volume of one or both sides. With only a total the payload has no per-side amounts,
    like the app's own pump; with sides, ``amount`` is their sum and a side left out is written
    as 0 in the other side's unit. With no volume at all, the session needs a length: the payload
    then has the durations and an ``amount`` of 0 in ``default_unit``, as the app writes a timed
    pump whose volume wasn't entered (never a missing key).
    ``time`` is the session's start."""
    if amount is not None and (left_volume is not None or right_volume is not None):
        raise ValueError("give a pump's total volume or its side volumes, not both")
    left = _minutes(left_minutes, "left_minutes")
    right = _minutes(right_minutes, "right_minutes")
    if amount is None and left_volume is None and right_volume is None and left + right == 0:
        raise ValueError("a pump needs a volume for at least one side, or a length")
    if default_unit not in (UNIT_OUNCES, UNIT_MILLILITERS):
        raise ValueError(f"unknown volume unit {default_unit!r}")
    per_side = amount is None and (left_volume is not None or right_volume is not None)
    unit: str = default_unit
    left_amount: int | float = 0
    right_amount: int | float = 0
    total: int | float = 0
    if amount is not None:
        total_v = _checked(amount, "amount")
        unit = total_v.unit
        total = total_v.value
    elif per_side:
        left_v = _checked(left_volume, "left_volume") if left_volume is not None else None
        right_v = _checked(right_volume, "right_volume") if right_volume is not None else None
        units = {v.unit for v in (left_v, right_v) if v is not None}
        if len(units) != 1:
            raise ValueError("both sides of a pump must use the same unit")
        unit = units.pop()
        left_amount = left_v.value if left_v is not None else 0
        right_amount = right_v.value if right_v is not None else 0
        total = round(left_amount + right_amount, 6)
    if (amount is not None or per_side) and total <= 0:
        raise ValueError("a pump's total volume must be more than zero")
    side = _side(finish_side, left_amount > 0 or left > 0, right_amount > 0 or right > 0)
    event, tz, text = _common(time, timezone_offset, note)

    def build(object_id: str, baby: Baby | None, timestamp: datetime) -> Pump:
        # A pump never carries a baby, whatever the caller passes.
        volumes: dict[str, Any] = {"amount": _measure(Volume(total, unit))}
        if per_side:
            volumes["left_amount"] = _measure(Volume(left_amount, unit))
            volumes["right_amount"] = _measure(Volume(right_amount, unit))
        return Pump(
            **_base(object_id, None, timestamp, event, tz, text),
            **volumes,
            left_duration=left,
            right_duration=right,
            finish_side=side,
            # Always 0 on every capture; its meaning isn't known.
            sides=0,
        )

    return Draft(Pump, build)
