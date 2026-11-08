"""Nursing in the day totals, and the last breast the app shows before a nursing or a pump."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest
from factories import BABY_ID, BABY_TWO_ID, baby_payload, payload_for

from pybabytracker import (
    SIDE_LEFT,
    SIDE_NONE,
    SIDE_RIGHT,
    LastBreast,
    Record,
    day_totals,
    decode_payload,
    last_breast,
)

DAY_START = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)
DAY_END = DAY_START + timedelta(days=1)
NOW = DAY_START + timedelta(hours=12)
_IDS = count(1)


def at(hours: float) -> datetime:
    return DAY_START + timedelta(hours=hours)


def record(type_name: str, moment: datetime, baby_id: str = BABY_ID, **fields: Any) -> Record:
    payload = payload_for(
        type_name,
        object_id=f"AAAAAAAA-0000-4000-8000-{next(_IDS):012d}",
        time=moment.strftime("%Y-%m-%d %H:%M:%S +0000"),
        baby=baby_payload(object_id=baby_id),
        **fields,
    )
    return decode_payload(payload)


def nursing(moment: datetime, side: Any = SIDE_LEFT, **fields: Any) -> Record:
    minutes = {"leftDuration": 5, "rightDuration": 7, "bothDuration": 0, **fields}
    return record("Nursing", moment, finishSide=side, **minutes)


def android_session(moment: datetime, side: Any = SIDE_RIGHT, **fields: Any) -> Record:
    """An Android phone's timed session, with its codes as the app sends them."""
    minutes = {"leftDuration": 4, "rightDuration": 6, "bothDuration": 0, **fields}
    return record("NursingSession", moment, finishSide=side, detailed=True, **minutes)


def pump(moment: datetime, side: int = SIDE_RIGHT, label: str | None = None) -> Record:
    """A pumping session; a real one syncs with no ``label`` key unless it adjusts the stash."""
    payload = payload_for(
        "Pump",
        object_id=f"AAAAAAAA-0000-4000-8000-{next(_IDS):012d}",
        time=moment.strftime("%Y-%m-%d %H:%M:%S +0000"),
        finishSide=side,
    )
    payload.pop("label", None)
    if label is not None:
        payload["label"] = label
    return decode_payload(payload)


def test_nursing_minutes_add_up_both_kinds_of_session() -> None:
    result = day_totals(
        [nursing(at(1)), android_session(at(3)), nursing(at(5), bothDuration=2)],
        BABY_ID,
        DAY_START,
        DAY_END,
    )
    assert (result.nursings, result.nursing_minutes, result.feeds) == (3, 12 + 10 + 14, 3)


def test_nursing_minutes_read_android_digit_strings_and_skip_junk() -> None:
    result = day_totals(
        [
            android_session(at(1), leftDuration="4", rightDuration="6"),
            nursing(at(2), leftDuration=None, rightDuration=[], bothDuration="x"),
        ],
        BABY_ID,
        DAY_START,
        DAY_END,
    )
    assert (result.nursings, result.nursing_minutes) == (2, 10)


def test_another_babys_nursing_and_one_outside_the_day_are_not_counted() -> None:
    result = day_totals(
        [nursing(at(1), baby_id=BABY_TWO_ID), nursing(at(-1)), nursing(at(25))],
        BABY_ID,
        DAY_START,
        DAY_END,
    )
    assert (result.nursings, result.nursing_minutes) == (0, 0)


def test_the_last_breast_is_the_newest_nursing_of_any_baby() -> None:
    records = [nursing(at(1), SIDE_LEFT), android_session(at(3), SIDE_RIGHT, baby_id=BABY_TWO_ID)]
    assert last_breast(records, NOW) == LastBreast("nursing", SIDE_RIGHT, at(3))


def test_a_newer_pump_is_the_last_breast_and_wins_a_tie() -> None:
    assert last_breast([nursing(at(1)), pump(at(2), SIDE_LEFT)], NOW) == LastBreast(
        "pump", SIDE_LEFT, at(2)
    )
    assert last_breast([nursing(at(2), SIDE_LEFT), pump(at(2), SIDE_RIGHT)], NOW) == LastBreast(
        "pump", SIDE_RIGHT, at(2)
    )


def test_a_milk_stash_adjustment_is_not_a_pump() -> None:
    records = [nursing(at(1), SIDE_LEFT), pump(at(2), label="InventoryAdjustment")]
    assert last_breast(records, NOW) == LastBreast("nursing", SIDE_LEFT, at(1))


def test_a_session_after_now_is_ignored() -> None:
    records = [nursing(at(1), SIDE_LEFT), nursing(at(13), SIDE_RIGHT)]
    assert last_breast(records, NOW) == LastBreast("nursing", SIDE_LEFT, at(1))


@pytest.mark.parametrize(("side", "expected"), [("1", SIDE_LEFT), (0, SIDE_NONE), (7, SIDE_NONE)])
def test_the_side_reads_android_strings_and_anything_else_as_none(side: Any, expected: int) -> None:
    assert last_breast([android_session(at(1), side)], NOW) == LastBreast(
        "nursing", expected, at(1)
    )


def test_with_no_nursing_or_pump_there_is_no_last_breast() -> None:
    assert last_breast([record("Diaper", at(1))], NOW) is None


@pytest.mark.parametrize(
    ("fields", "minutes"),
    [
        ({"leftDuration": -5, "rightDuration": 7}, 7),
        ({"leftDuration": 5.0, "rightDuration": 7}, 7),
        ({"leftDuration": True, "rightDuration": 7}, 7),
        ({"leftDuration": "", "rightDuration": " 7 "}, 0),
        ({"leftDuration": None, "rightDuration": None, "bothDuration": None}, 0),
    ],
)
def test_odd_nursing_minutes_add_nothing(fields: dict[str, Any], minutes: int) -> None:
    result = day_totals([nursing(at(1), **fields)], BABY_ID, DAY_START, DAY_END)
    assert (result.nursings, result.nursing_minutes) == (1, minutes)


def test_a_nursing_with_no_minute_keys_still_counts_as_a_session() -> None:
    payload = payload_for(
        "Nursing",
        object_id="AAAAAAAA-0000-4000-8000-999999999999",
        time=at(1).strftime("%Y-%m-%d %H:%M:%S +0000"),
        baby=baby_payload(object_id=BABY_ID),
    )
    for key in ("leftDuration", "rightDuration", "bothDuration"):
        payload.pop(key, None)
    result = day_totals([decode_payload(payload)], BABY_ID, DAY_START, DAY_END)
    assert (result.nursings, result.nursing_minutes) == (1, 0)


def test_a_session_exactly_at_now_counts_and_a_missing_side_is_none() -> None:
    assert last_breast([nursing(NOW, side=None)], NOW) == LastBreast("nursing", SIDE_NONE, NOW)


def test_a_pump_with_an_empty_label_is_still_a_pump() -> None:
    assert last_breast([nursing(at(1)), pump(at(2), SIDE_LEFT, label="")], NOW) == LastBreast(
        "pump", SIDE_LEFT, at(2)
    )
