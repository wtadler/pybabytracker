"""Day totals: diapers, feeds, and bottle volume inside a day the caller bounds."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from itertools import count
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from factories import BABY_ID, BABY_TWO_ID, baby_payload, payload_for

from pybabytracker import (
    DIAPER_DIRTY,
    DIAPER_MIXED,
    DIAPER_WET,
    DayTotals,
    Record,
    day_totals,
    decode_payload,
)
from pybabytracker.totals import MILLILITERS_PER_FLUID_OUNCE

NEW_YORK = ZoneInfo("America/New_York")


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S +0000")


_IDS = count(1)


def record(
    type_name: str, moment: datetime | None, baby_id: str = BABY_ID, **overrides: Any
) -> Record:
    payload = payload_for(
        type_name,
        object_id=f"AAAAAAAA-0000-4000-8000-{next(_IDS):012d}",
        time=stamp(moment) if moment is not None else "",
        baby=baby_payload(object_id=baby_id),
        **overrides,
    )
    if moment is None:
        del payload["time"]
    return decode_payload(payload)


def local_day(day: date, zone: ZoneInfo = NEW_YORK) -> tuple[datetime, datetime]:
    """Local midnight to the next local midnight, the way a caller would work them out."""
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return start, end


DAY_START = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)
DAY_END = DAY_START + timedelta(days=1)
NOON = DAY_START + timedelta(hours=12)


def totals(*records: Record, baby_id: str = BABY_ID) -> DayTotals:
    return day_totals(records, baby_id, DAY_START, DAY_END)


def test_an_empty_day_is_all_zeros_and_remembers_when_it_started() -> None:
    result = totals()
    assert result == DayTotals(start=DAY_START)
    assert (result.diapers, result.feeds, result.bottle_milliliters) == (0, 0, 0.0)


def test_diapers_are_counted_in_total_and_by_kind() -> None:
    result = totals(
        record("Diaper", NOON, status=DIAPER_WET),
        record("Diaper", NOON + timedelta(minutes=1), status=DIAPER_WET),
        record("Diaper", NOON + timedelta(minutes=2), status=DIAPER_DIRTY),
        record("Diaper", NOON + timedelta(minutes=3), status=DIAPER_MIXED),
    )
    assert (result.diapers, result.wet, result.dirty, result.mixed) == (4, 2, 1, 1)


def test_a_diaper_of_an_unknown_kind_counts_in_total_only() -> None:
    result = totals(record("Diaper", NOON, status=99))
    assert (result.diapers, result.wet, result.dirty, result.mixed) == (1, 0, 0, 0)


def test_nursing_and_bottles_are_feeds() -> None:
    result = totals(
        record("Nursing", NOON),
        record("Formula", NOON + timedelta(minutes=1)),
        record("Pumped", NOON + timedelta(minutes=2)),
    )
    assert result.feeds == 3


def test_other_records_are_not_counted() -> None:
    result = totals(
        record("Sleep", NOON),
        record("OtherFeed", NOON),
        record("Pump", NOON),
        record("Growth", NOON),
        decode_payload({"BCObjectType": "FromTheFuture", "objectID": "X", "time": stamp(NOON)}),
    )
    assert result == DayTotals(start=DAY_START)


def test_bottle_volume_adds_formula_and_pumped_in_milliliters() -> None:
    result = totals(
        record("Formula", NOON, amount=120, isEnglishScale=0),
        record("Pumped", NOON + timedelta(minutes=1), amount=60.5, isEnglishScale=0),
    )
    assert result.bottle_milliliters == pytest.approx(180.5)


def test_ounces_convert_to_milliliters() -> None:
    result = totals(record("Formula", NOON, amount=4.5, isEnglishScale=1))
    assert result.bottle_milliliters == pytest.approx(4.5 * 29.5735295625)
    assert pytest.approx(29.5735295625) == MILLILITERS_PER_FLUID_OUNCE


def test_ounces_and_milliliters_mix() -> None:
    result = totals(
        record("Formula", NOON, amount=2, isEnglishScale=1),
        record("Pumped", NOON + timedelta(minutes=1), amount=100, isEnglishScale=0),
    )
    assert result.bottle_milliliters == pytest.approx(2 * 29.5735295625 + 100)


def test_a_bottle_without_a_known_unit_counts_as_a_feed_but_adds_no_volume() -> None:
    result = totals(record("Formula", NOON, amount=4.5, isEnglishScale=2))
    assert (result.feeds, result.bottle_milliliters) == (1, 0.0)


def test_a_bottle_without_an_amount_counts_as_a_feed_but_adds_no_volume() -> None:
    result = totals(record("Formula", NOON, amount=None))
    assert (result.feeds, result.bottle_milliliters) == (1, 0.0)


def test_nursing_adds_no_volume() -> None:
    assert totals(record("Nursing", NOON)).bottle_milliliters == 0.0


def test_only_the_named_baby_is_counted() -> None:
    mine = record("Diaper", NOON, status=DIAPER_WET)
    theirs = record("Diaper", NOON, baby_id=BABY_TWO_ID, status=DIAPER_WET)
    assert totals(mine, theirs).diapers == 1
    assert totals(mine, theirs, baby_id=BABY_TWO_ID).diapers == 1
    assert totals(mine, theirs, baby_id="no-such-baby").diapers == 0


def test_a_record_with_no_time_is_not_counted() -> None:
    assert totals(record("Diaper", None, status=DIAPER_WET)).diapers == 0


def test_the_day_includes_its_start_and_excludes_its_end() -> None:
    result = totals(
        record("Diaper", DAY_START - timedelta(seconds=1), status=DIAPER_WET),
        record("Diaper", DAY_START, status=DIAPER_DIRTY),
        record("Diaper", DAY_END - timedelta(seconds=1), status=DIAPER_MIXED),
        record("Diaper", DAY_END, status=DIAPER_WET),
    )
    assert (result.diapers, result.wet, result.dirty, result.mixed) == (2, 0, 1, 1)


def test_totals_cover_local_midnight_to_midnight_not_utc_days() -> None:
    start, end = local_day(date(2026, 10, 26))  # 04:00 UTC to 04:00 UTC the next day
    records = [
        record("Diaper", datetime(2026, 10, 26, 3, 59, tzinfo=UTC), status=DIAPER_WET),
        record("Diaper", datetime(2026, 10, 26, 4, 0, tzinfo=UTC), status=DIAPER_WET),
        record("Diaper", datetime(2026, 10, 27, 3, 59, tzinfo=UTC), status=DIAPER_WET),
        record("Diaper", datetime(2026, 10, 27, 4, 0, tzinfo=UTC), status=DIAPER_WET),
    ]
    result = day_totals(records, BABY_ID, start, end)
    assert result.diapers == 2
    assert result.start == start


def test_the_next_day_starts_where_the_last_one_ended() -> None:
    first = local_day(date(2026, 10, 26))
    second = local_day(date(2026, 10, 27))
    around_midnight = [
        record("Diaper", datetime(2026, 10, 27, 3, 59, 59, tzinfo=UTC), status=DIAPER_WET),
        record("Diaper", datetime(2026, 10, 27, 4, 0, 0, tzinfo=UTC), status=DIAPER_DIRTY),
    ]
    today = day_totals(around_midnight, BABY_ID, *first)
    tomorrow = day_totals(around_midnight, BABY_ID, *second)
    assert (today.wet, today.dirty) == (1, 0)
    assert (tomorrow.wet, tomorrow.dirty) == (0, 1)


def test_a_spring_forward_day_is_23_hours_long() -> None:
    start, end = local_day(date(2026, 3, 8))
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=23)
    records = [
        record("Diaper", datetime(2026, 3, 8, 4, 59, tzinfo=UTC), status=DIAPER_WET),  # 23:59 EST
        record("Diaper", datetime(2026, 3, 8, 5, 0, tzinfo=UTC), status=DIAPER_WET),  # 00:00 EST
        record("Diaper", datetime(2026, 3, 8, 7, 30, tzinfo=UTC), status=DIAPER_WET),  # 03:30 EDT
        record("Diaper", datetime(2026, 3, 9, 3, 59, tzinfo=UTC), status=DIAPER_WET),  # 23:59 EDT
        record("Diaper", datetime(2026, 3, 9, 4, 0, tzinfo=UTC), status=DIAPER_WET),  # 00:00 EDT
    ]
    assert day_totals(records, BABY_ID, start, end).diapers == 3


def test_a_fall_back_day_is_25_hours_long() -> None:
    start, end = local_day(date(2026, 11, 1))
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=25)
    records = [
        record("Diaper", datetime(2026, 11, 1, 3, 59, tzinfo=UTC), status=DIAPER_WET),  # 23:59 EDT
        record("Diaper", datetime(2026, 11, 1, 4, 0, tzinfo=UTC), status=DIAPER_WET),  # 00:00 EDT
        record("Diaper", datetime(2026, 11, 1, 5, 30, tzinfo=UTC), status=DIAPER_WET),  # 01:30 EDT
        record("Diaper", datetime(2026, 11, 1, 6, 30, tzinfo=UTC), status=DIAPER_WET),  # 01:30 EST
        record("Diaper", datetime(2026, 11, 2, 4, 59, tzinfo=UTC), status=DIAPER_WET),  # 23:59 EST
        record("Diaper", datetime(2026, 11, 2, 5, 0, tzinfo=UTC), status=DIAPER_WET),  # 00:00 EST
    ]
    assert day_totals(records, BABY_ID, start, end).diapers == 4


@pytest.mark.parametrize("day", [date(2026, 3, 8), date(2026, 11, 1)])
def test_feeds_and_bottle_volume_follow_the_same_dst_day(day: date) -> None:
    start, end = local_day(day)
    inside = start.astimezone(UTC) + timedelta(hours=1)
    just_before = start.astimezone(UTC) - timedelta(seconds=1)
    just_after = end.astimezone(UTC)
    records = [
        record("Formula", inside, amount=100, isEnglishScale=0),
        record("Nursing", inside),
        record("Formula", just_before, amount=500, isEnglishScale=0),
        record("Pumped", just_after, amount=700, isEnglishScale=0),
    ]
    result = day_totals(records, BABY_ID, start, end)
    assert (result.feeds, result.bottle_milliliters) == (2, 100.0)
