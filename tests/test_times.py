"""The standard (non-DST) UTC offset a record's ``timezone`` field carries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from pybabytracker import standard_offset_seconds


@pytest.mark.parametrize(
    ("zone", "when", "expected"),
    [
        ("America/New_York", datetime(2026, 7, 15, 12), -18000),
        ("America/New_York", datetime(2026, 1, 15, 12), -18000),
        ("America/Chicago", datetime(2026, 7, 15, 12), -21600),
        ("America/Chicago", datetime(2026, 1, 15, 12), -21600),
        ("Asia/Kolkata", datetime(2026, 7, 15, 12), 19800),
        ("UTC", datetime(2026, 7, 15, 12), 0),
        ("Europe/London", datetime(2026, 7, 15, 12), 0),
        ("Australia/Sydney", datetime(2026, 1, 15, 12), 36000),
    ],
)
def test_a_zones_standard_offset_ignores_daylight_saving(
    zone: str, when: datetime, expected: int
) -> None:
    assert standard_offset_seconds(ZoneInfo(zone), when) == expected


def test_a_fixed_offset_is_its_own_standard_offset() -> None:
    zone = timezone(timedelta(hours=-5))
    assert standard_offset_seconds(zone, datetime(2026, 7, 15, 12)) == -18000
    assert standard_offset_seconds(UTC, datetime(2026, 7, 15, 12)) == 0


def test_an_aware_moment_is_read_in_the_given_zone() -> None:
    summer = datetime(2026, 7, 15, 12, tzinfo=ZoneInfo("America/New_York"))
    assert standard_offset_seconds(ZoneInfo("America/New_York"), summer) == -18000


def test_the_instant_before_and_after_a_transition_gives_one_answer() -> None:
    zone = ZoneInfo("America/New_York")
    before = datetime(2026, 3, 8, 1, 59, tzinfo=zone)
    after = datetime(2026, 3, 8, 3, 1, tzinfo=zone)
    assert standard_offset_seconds(zone, before) == standard_offset_seconds(zone, after) == -18000


def test_an_aware_utc_moment_is_converted_to_the_zone_first() -> None:
    zone = ZoneInfo("America/New_York")
    assert standard_offset_seconds(zone, datetime(2026, 7, 15, 3, tzinfo=UTC)) == -18000
