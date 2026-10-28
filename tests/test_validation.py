"""Write validation: the amount, minute, and sleep-range checks, and the typed errors they raise."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from pybabytracker import (
    AmountTooSmallError,
    BabyTrackerError,
    EndBeforeStartError,
    InvalidAmountError,
    MinutesTooLongError,
    SleepStartOutOfRangeError,
    SleepTooLongError,
    ValidationError,
    WriteError,
)
from pybabytracker.builders import MAX_MINUTES_PER_SIDE, MAX_SLEEP, START_SKEW
from pybabytracker.validation import (
    check_amount,
    check_end_after_start,
    check_minutes,
    check_pump_amounts,
    check_sleep_range,
    check_sleep_start,
)

START = datetime(2026, 10, 26, 14, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 26, 18, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "error",
    [
        InvalidAmountError,
        AmountTooSmallError,
        MinutesTooLongError,
        EndBeforeStartError,
        SleepTooLongError,
        SleepStartOutOfRangeError,
    ],
)
def test_every_validation_error_is_a_write_error_refused_before_anything_is_sent(
    error: type[Exception],
) -> None:
    assert issubclass(error, ValidationError)
    assert issubclass(ValidationError, WriteError)
    assert issubclass(ValidationError, BabyTrackerError)


# Amounts


@pytest.mark.parametrize("amount", [0.01, 0.005, 1, 4.5, 120])
def test_a_positive_amount_passes_and_is_returned(amount: float) -> None:
    assert check_amount(amount) == amount


@pytest.mark.parametrize("amount", [0, 0.0, -1, -0.01, math.nan, math.inf, -math.inf])
def test_a_zero_negative_or_non_finite_amount_is_invalid(amount: float) -> None:
    with pytest.raises(InvalidAmountError) as raised:
        check_amount(amount)
    assert raised.value.amount == amount or math.isnan(raised.value.amount)


@pytest.mark.parametrize("amount", [0.001, 0.004, 0.0049999])
def test_an_amount_that_rounds_to_zero_at_two_decimals_is_too_small(amount: float) -> None:
    with pytest.raises(AmountTooSmallError) as raised:
        check_amount(amount)
    assert raised.value.amount == amount


def test_an_invalid_amount_is_not_reported_as_too_small() -> None:
    with pytest.raises(InvalidAmountError):
        check_amount(0)
    with pytest.raises(InvalidAmountError):
        check_amount(-0.001)


# A pump's side amounts


def test_a_pump_with_an_amount_on_one_side_passes() -> None:
    check_pump_amounts(3.0, None)
    check_pump_amounts(None, 2.5)
    check_pump_amounts(0, 1)
    check_pump_amounts(1, 0)


def test_a_pump_with_an_amount_on_both_sides_passes() -> None:
    check_pump_amounts(3.0, 2.5)


@pytest.mark.parametrize("bad", [-1, -0.01, math.nan, math.inf])
def test_a_negative_or_non_finite_side_amount_is_invalid(bad: float) -> None:
    with pytest.raises(InvalidAmountError):
        check_pump_amounts(bad, 3)
    with pytest.raises(InvalidAmountError):
        check_pump_amounts(3, bad)


def test_side_amounts_that_total_nothing_are_invalid() -> None:
    with pytest.raises(InvalidAmountError):
        check_pump_amounts(0, 0)
    with pytest.raises(InvalidAmountError):
        check_pump_amounts(None, None)
    with pytest.raises(InvalidAmountError):
        check_pump_amounts(0, None)


def test_side_amounts_that_each_round_to_nothing_are_too_small() -> None:
    with pytest.raises(AmountTooSmallError):
        check_pump_amounts(0.004, None)
    with pytest.raises(AmountTooSmallError):
        check_pump_amounts(0.002, 0.002)


def test_sides_that_total_a_cent_but_each_round_to_nothing_are_too_small() -> None:
    # 0.004 + 0.004 = 0.008 passes the total, but each side is saved to two decimals: 0 + 0.
    with pytest.raises(AmountTooSmallError):
        check_pump_amounts(0.004, 0.004)


# Minutes


def test_minutes_up_to_240_pass() -> None:
    check_minutes(0, 0)
    check_minutes(1, 240)
    check_minutes(MAX_MINUTES_PER_SIDE)
    check_minutes()


@pytest.mark.parametrize("minutes", [(241, 0), (0, 241), (500, 500), (240, 241), (1000,)])
def test_minutes_over_240_on_any_side_are_too_long(minutes: tuple[int, ...]) -> None:
    with pytest.raises(MinutesTooLongError) as raised:
        check_minutes(*minutes)
    assert raised.value.maximum == MAX_MINUTES_PER_SIDE == 240
    assert raised.value.minutes == max(minutes)


# Sleep ranges


def test_a_sleep_that_ends_after_it_starts_and_is_within_24_hours_passes() -> None:
    check_sleep_range(START, START + timedelta(seconds=1))
    check_sleep_range(START, START + MAX_SLEEP)


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(seconds=-1), timedelta(hours=-5)])
def test_a_sleep_that_ends_at_or_before_it_starts_is_refused(delta: timedelta) -> None:
    with pytest.raises(EndBeforeStartError) as raised:
        check_sleep_range(START, START + delta)
    assert (raised.value.start, raised.value.end) == (START, START + delta)


def test_a_sleep_longer_than_24_hours_is_too_long() -> None:
    with pytest.raises(SleepTooLongError) as raised:
        check_sleep_range(START, START + MAX_SLEEP + timedelta(seconds=1))
    assert raised.value.maximum == MAX_SLEEP
    assert raised.value.length == MAX_SLEEP + timedelta(seconds=1)


def test_the_end_is_checked_before_the_length() -> None:
    with pytest.raises(EndBeforeStartError):
        check_sleep_range(START + timedelta(days=3), START)


def test_ending_a_running_sleep_needs_only_an_end_after_its_start() -> None:
    check_end_after_start(START, START + timedelta(days=3))
    with pytest.raises(EndBeforeStartError):
        check_end_after_start(START, START)
    with pytest.raises(EndBeforeStartError):
        check_end_after_start(START, START - timedelta(minutes=1))


# A running sleep's start


def test_a_sleep_start_up_to_a_minute_ahead_and_a_day_back_passes() -> None:
    check_sleep_start(NOW, NOW)
    check_sleep_start(NOW + START_SKEW, NOW)
    check_sleep_start(NOW - MAX_SLEEP, NOW)
    check_sleep_start(NOW - timedelta(minutes=5), NOW)


def test_a_sleep_start_more_than_a_minute_ahead_is_out_of_range() -> None:
    begin = NOW + START_SKEW + timedelta(seconds=1)
    with pytest.raises(SleepStartOutOfRangeError) as raised:
        check_sleep_start(begin, NOW)
    assert (raised.value.start, raised.value.now) == (begin, NOW)


def test_a_sleep_start_more_than_24_hours_back_is_out_of_range() -> None:
    begin = NOW - MAX_SLEEP - timedelta(seconds=1)
    with pytest.raises(SleepStartOutOfRangeError) as raised:
        check_sleep_start(begin, NOW)
    assert (raised.value.start, raised.value.now) == (begin, NOW)
