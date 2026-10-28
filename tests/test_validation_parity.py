"""The builders and the validation checks hold the same limits.

The builders raise ``ValueError`` and the checks raise ``ValidationError`` for the same
conditions. At each limit's boundary, one raises exactly when the other does, so a limit changed
on one side alone fails here.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from pybabytracker import Formula, ValidationError, Volume
from pybabytracker.builders import (
    MAX_MINUTES_PER_SIDE,
    MAX_SLEEP,
    START_SKEW,
    draft_bottle,
    draft_nursing,
    draft_sleep,
    draft_sleep_start,
)
from pybabytracker.validation import (
    check_amount,
    check_minutes,
    check_sleep_range,
    check_sleep_start,
)

START = datetime(2026, 10, 26, 14, 0, tzinfo=UTC)
ONE_SECOND = timedelta(seconds=1)


def refused(call: Callable[[], object], error: type[Exception]) -> bool:
    try:
        call()
    except error:
        return True
    return False


def assert_same_verdict(
    builder: Callable[[], object], check: Callable[[], object], expected: bool
) -> None:
    by_builder = refused(builder, ValueError)
    by_check = refused(check, ValidationError)
    assert (by_builder, by_check) == (expected, expected)


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(1, False), (MAX_MINUTES_PER_SIDE - 1, False), (240, False), (241, True), (1000, True)],
)
@pytest.mark.parametrize("side", ["left", "right"])
def test_minutes_are_refused_by_both_above_240(side: str, minutes: int, expected: bool) -> None:
    left, right = (minutes, 1) if side == "left" else (1, minutes)
    assert_same_verdict(
        lambda: draft_nursing(
            left_minutes=left,
            right_minutes=right,
            finish_side=None,
            start_time=START,
            end_time=None,
            timezone_offset=None,
            note="",
        ),
        lambda: check_minutes(left, right),
        expected,
    )


@pytest.mark.parametrize(
    ("length", "expected"),
    [
        (ONE_SECOND, False),
        (MAX_SLEEP - ONE_SECOND, False),
        (MAX_SLEEP, False),
        (MAX_SLEEP + ONE_SECOND, True),
        (timedelta(0), True),
        (-ONE_SECOND, True),
    ],
)
def test_a_sleep_range_is_refused_by_both_at_the_same_lengths(
    length: timedelta, expected: bool
) -> None:
    end = START + length
    assert_same_verdict(
        lambda: draft_sleep(start=START, end=end, timezone_offset=None, note=""),
        lambda: check_sleep_range(START, end),
        expected,
    )


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (timedelta(0), False),
        (START_SKEW, False),
        (START_SKEW + ONE_SECOND, True),
        (-MAX_SLEEP, False),
        (-MAX_SLEEP - ONE_SECOND, True),
    ],
)
def test_a_sleep_start_is_refused_by_both_at_the_same_distance_from_now(
    offset: timedelta, expected: bool
) -> None:
    begin = START + offset
    assert_same_verdict(
        lambda: draft_sleep_start(start=begin, timezone_offset=None, note="", now=START),
        lambda: check_sleep_start(begin, START),
        expected,
    )


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (1, False),
        (0.01, False),
        (0.005, False),
        (0.0049999, True),
        (0.004, True),
        (0.001, True),
        (0, True),
        (-0.01, True),
        (-1, True),
        (math.nan, True),
        (math.inf, True),
        (-math.inf, True),
    ],
)
@pytest.mark.parametrize("unit", ["oz", "ml"])
def test_an_amount_is_refused_by_both_at_the_same_values(
    amount: float, unit: str, expected: bool
) -> None:
    assert_same_verdict(
        lambda: draft_bottle(
            Formula, Volume(amount, unit), time=START, timezone_offset=None, note=""
        ),
        lambda: check_amount(amount),
        expected,
    )
