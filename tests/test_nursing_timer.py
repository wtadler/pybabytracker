"""The nursing and pump timer state machines: sides, switching, rounding, and the 240-minute cap."""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta

import pytest

from pybabytracker import NursingTimer, PumpTimer
from pybabytracker.builders import MAX_MINUTES_PER_SIDE
from pybabytracker.timers import LEFT, RIGHT, SIDES, other_side

START = datetime(2026, 10, 26, 14, 0, tzinfo=UTC)
OBJECT_ID = "C0000000-0000-4000-8000-000000000001"


def nursing(side: str = LEFT, now: datetime = START) -> NursingTimer:
    return NursingTimer.start(OBJECT_ID, side, now)


def pump(now: datetime = START) -> PumpTimer:
    return PumpTimer.start(OBJECT_ID, now)


def test_the_sides_are_left_and_right() -> None:
    assert (LEFT, RIGHT) == ("left", "right")
    assert SIDES == (LEFT, RIGHT)


def test_the_other_side_of_left_is_right_and_of_right_is_left() -> None:
    assert other_side(LEFT) == RIGHT
    assert other_side(RIGHT) == LEFT


def test_a_new_timer_runs_on_its_side_from_its_start_with_nothing_banked() -> None:
    timer = nursing(RIGHT)
    assert (timer.object_id, timer.started, timer.side, timer.side_started) == (
        OBJECT_ID,
        START,
        RIGHT,
        START,
    )
    assert timer.totals(START) == (0.0, 0.0)


def test_the_running_side_counts_up_to_now() -> None:
    assert nursing(LEFT).totals(START + timedelta(seconds=90)) == (90.0, 0.0)
    assert nursing(RIGHT).totals(START + timedelta(seconds=90)) == (0.0, 90.0)


def test_switching_banks_the_stretch_on_the_side_it_ran_and_runs_the_new_side() -> None:
    switched = nursing(LEFT).switched(RIGHT, START + timedelta(minutes=4))
    assert switched.side == RIGHT
    assert switched.side_started == START + timedelta(minutes=4)
    assert switched.started == START
    assert switched.object_id == OBJECT_ID
    assert (switched.left_seconds, switched.right_seconds) == (240.0, 0.0)
    assert switched.totals(START + timedelta(minutes=10)) == (240.0, 360.0)


def test_switching_back_adds_to_the_time_already_on_that_side() -> None:
    timer = (
        nursing(LEFT)
        .switched(RIGHT, START + timedelta(minutes=4))
        .switched(LEFT, START + timedelta(minutes=10))
    )
    assert timer.totals(START + timedelta(minutes=13)) == (240.0 + 180.0, 360.0)


def test_switching_leaves_the_timer_it_started_from_alone() -> None:
    original = nursing(LEFT)
    original.switched(RIGHT, START + timedelta(minutes=4))
    assert original.side == LEFT
    assert original.left_seconds == 0.0


def test_a_clock_that_runs_backward_counts_as_no_time_on_the_running_side() -> None:
    assert nursing(LEFT).totals(START - timedelta(seconds=30)) == (0.0, 0.0)


@pytest.mark.parametrize(
    ("seconds", "minutes"),
    [(30, 1), (59, 1), (89, 1), (90, 2), (149, 2), (150, 3), (600, 10)],
)
def test_a_side_rounds_to_whole_minutes_half_up(seconds: int, minutes: int) -> None:
    assert nursing(LEFT).minutes(START + timedelta(seconds=seconds)) == (minutes, 0)
    assert nursing(RIGHT).minutes(START + timedelta(seconds=seconds)) == (0, minutes)


def test_each_side_is_rounded_on_its_own() -> None:
    timer = nursing(LEFT).switched(RIGHT, START + timedelta(seconds=89))
    assert timer.minutes(START + timedelta(seconds=89 + 91)) == (1, 2)


def test_a_session_under_30_seconds_is_one_minute_on_the_side_it_ended_on() -> None:
    assert nursing(LEFT).minutes(START + timedelta(seconds=29)) == (1, 0)
    assert nursing(RIGHT).minutes(START + timedelta(seconds=29)) == (0, 1)


def test_a_session_stopped_the_instant_it_started_is_one_minute() -> None:
    assert nursing(RIGHT).minutes(START) == (0, 1)


def test_two_stretches_that_each_round_to_zero_are_one_minute_on_the_side_it_ended_on() -> None:
    timer = nursing(LEFT).switched(RIGHT, START + timedelta(seconds=20))
    assert timer.minutes(START + timedelta(seconds=40)) == (0, 1)


def test_the_one_minute_floor_applies_only_when_both_sides_round_to_zero() -> None:
    timer = nursing(LEFT).switched(RIGHT, START + timedelta(seconds=60))
    assert timer.minutes(START + timedelta(seconds=70)) == (1, 0)


def test_a_side_is_capped_at_240_minutes() -> None:
    assert MAX_MINUTES_PER_SIDE == 240
    timer = nursing(LEFT)
    assert timer.minutes(START + timedelta(minutes=240)) == (240, 0)
    assert timer.minutes(START + timedelta(minutes=241)) == (240, 0)
    assert timer.minutes(START + timedelta(hours=30)) == (240, 0)


def test_each_side_is_capped_on_its_own() -> None:
    both = nursing(LEFT).switched(RIGHT, START + timedelta(hours=5))
    assert both.minutes(START + timedelta(hours=11)) == (240, 240)
    shorter = nursing(LEFT).switched(RIGHT, START + timedelta(minutes=10))
    assert shorter.minutes(START + timedelta(hours=11)) == (10, 240)


def test_running_past_the_cap_warns_and_reaching_it_does_not(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="pybabytracker"):
        nursing().minutes(START + timedelta(minutes=240))
        assert caplog.text == ""
        nursing().minutes(START + timedelta(minutes=241))
    assert OBJECT_ID in caplog.text
    assert "240" in caplog.text


def test_a_timer_is_overdue_once_it_has_run_for_the_given_time() -> None:
    timer = nursing()
    after = timedelta(hours=4)
    assert not timer.is_overdue(START + timedelta(hours=4) - timedelta(seconds=1), after)
    assert timer.is_overdue(START + timedelta(hours=4), after)
    assert timer.is_overdue(START + timedelta(hours=5), after)


def test_a_side_switch_does_not_restart_the_overdue_clock() -> None:
    timer = nursing().switched(RIGHT, START + timedelta(hours=3, minutes=59))
    assert timer.is_overdue(START + timedelta(hours=4), timedelta(hours=4))


def test_a_nursing_timer_round_trips_through_its_saved_form() -> None:
    timer = nursing(LEFT).switched(RIGHT, START + timedelta(minutes=4, seconds=30))
    saved = timer.to_dict()
    assert saved == {
        "object_id": OBJECT_ID,
        "started": "2026-10-26T14:00:00+00:00",
        "side": "right",
        "side_started": "2026-10-26T14:04:30+00:00",
        "left_seconds": 270.0,
        "right_seconds": 0.0,
    }
    assert NursingTimer.from_dict(saved) == timer


def test_a_saved_timer_without_banked_seconds_reads_as_zero() -> None:
    saved = nursing().to_dict()
    del saved["left_seconds"], saved["right_seconds"]
    timer = NursingTimer.from_dict(saved)
    assert (timer.left_seconds, timer.right_seconds) == (0.0, 0.0)


@pytest.mark.parametrize(
    "change",
    [
        {"side": "middle"},
        {"started": "not a time"},
        {"side_started": "not a time"},
        {"started": "2026-10-26T14:00:00"},
        {"side_started": "2026-10-26T14:00:00"},
        {"left_seconds": -1.0},
        {"right_seconds": -1.0},
        {"left_seconds": math.nan},
        {"right_seconds": math.inf},
        {"left_seconds": "soon"},
    ],
)
def test_a_saved_nursing_timer_that_cannot_be_read_is_refused(change: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        NursingTimer.from_dict({**nursing().to_dict(), **change})


def test_a_saved_nursing_timer_with_a_null_amount_is_refused() -> None:
    with pytest.raises(TypeError):
        NursingTimer.from_dict({**nursing().to_dict(), "left_seconds": None})


@pytest.mark.parametrize("missing", ["object_id", "started", "side", "side_started"])
def test_a_saved_nursing_timer_missing_a_field_is_refused(missing: str) -> None:
    saved = nursing().to_dict()
    del saved[missing]
    with pytest.raises(KeyError):
        NursingTimer.from_dict(saved)


@pytest.mark.parametrize(
    ("seconds", "minutes"),
    [(0, 1), (10, 1), (29, 1), (30, 1), (89, 1), (90, 2), (150, 3), (3600, 60)],
)
def test_a_pump_is_rounded_half_up_with_a_floor_of_one_minute(seconds: int, minutes: int) -> None:
    assert pump().minutes(START + timedelta(seconds=seconds)) == minutes


def test_a_pump_is_capped_at_240_minutes_and_warns_past_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="pybabytracker"):
        assert pump().minutes(START + timedelta(minutes=240)) == 240
        assert caplog.text == ""
        assert pump().minutes(START + timedelta(minutes=241)) == 240
        assert pump().minutes(START + timedelta(hours=30)) == 240
    assert OBJECT_ID in caplog.text
    assert "pump" in caplog.text.lower()


def test_a_pump_on_a_clock_that_runs_backward_is_one_minute() -> None:
    assert pump().minutes(START - timedelta(minutes=5)) == 1


def test_a_pump_timer_is_overdue_once_it_has_run_for_the_given_time() -> None:
    after = timedelta(hours=4)
    assert not pump().is_overdue(START + timedelta(hours=3), after)
    assert pump().is_overdue(START + timedelta(hours=4), after)


def test_a_pump_timer_round_trips_through_its_saved_form() -> None:
    timer = pump()
    assert timer.to_dict() == {"object_id": OBJECT_ID, "started": "2026-10-26T14:00:00+00:00"}
    assert PumpTimer.from_dict(timer.to_dict()) == timer


@pytest.mark.parametrize(
    "saved",
    [
        {"object_id": "", "started": "2026-10-26T14:00:00+00:00"},
        {"object_id": OBJECT_ID, "started": "not a time"},
        {"object_id": OBJECT_ID, "started": "2026-10-26T14:00:00"},
    ],
)
def test_a_saved_pump_timer_that_cannot_be_read_is_refused(saved: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        PumpTimer.from_dict(saved)


def test_a_saved_pump_timer_missing_a_field_is_refused() -> None:
    with pytest.raises(KeyError):
        PumpTimer.from_dict({"object_id": OBJECT_ID})
