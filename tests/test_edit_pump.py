"""Editing a pump (invented data only)."""

from __future__ import annotations

from typing import Any

import pytest
from factories import payload_for
from fake_server import FakeSyncServer
from selection_helpers import posted, rid, seed, synced

from pybabytracker import (
    SIDE_RIGHT,
    BabyTrackerClient,
    Pump,
    Volume,
    WrongRecordTypeError,
)

PUMP = rid(31)
ENVELOPE = {"newFlage", "timestamp"}


def measure(value: float, english: str = "true") -> dict[str, Any]:
    return {"value": value, "englishMeasure": english, "BCObjectType": "VolumeMeasure"}


def iphone_pump(**overrides: Any) -> dict[str, Any]:
    """A pump as an iPhone synced one, with both sides: the keys seen on 2026-11-12."""
    payload = payload_for("Pump", PUMP)
    for key in ("baby", "isEnglishScale", "label", "pumpedID"):
        payload.pop(key, None)
    payload.update(
        amount=measure(6),
        leftAmount=measure(3.5),
        rightAmount=measure(2.5),
        leftDuration=10,
        rightDuration=8,
        finishSide=1,
        sides=0,
        newFlage="true",
    )
    payload.update(overrides)
    return payload


def total_only_pump() -> dict[str, Any]:
    payload = iphone_pump(leftDuration=0, rightDuration=0)
    del payload["leftAmount"], payload["rightAmount"]
    return payload


def changed(payload: dict[str, Any], original: dict[str, Any]) -> set[str]:
    return {k for k in set(payload) | set(original) if payload.get(k) != original.get(k)}


async def test_one_sides_volume_changes_and_the_total_follows(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = iphone_pump()
    seed(fake_server, original)
    engine = await synced(client)

    updated = await engine.edit_pump(client, PUMP, left_volume=Volume(4, "oz"))

    [payload] = posted(fake_server)
    assert list(payload) == list(original)
    assert changed(payload, original) == {"amount", "leftAmount"} | ENVELOPE
    assert (payload["leftAmount"], payload["rightAmount"], payload["amount"]) == (
        measure(4),
        measure(2.5),
        measure(6.5),
    )
    assert "baby" not in payload
    assert isinstance(updated, Pump)
    assert updated.volume == Volume(6.5, "oz")


async def test_a_new_total_drops_the_side_volumes(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = iphone_pump()
    seed(fake_server, original)
    engine = await synced(client)

    await engine.edit_pump(client, PUMP, amount=Volume(200, "ml"))

    [payload] = posted(fake_server)
    assert changed(payload, original) == {"amount", "leftAmount", "rightAmount"} | ENVELOPE
    assert payload["amount"] == measure(200, "false")
    assert not {"leftAmount", "rightAmount"} & set(payload)
    assert (payload["leftDuration"], payload["rightDuration"]) == (10, 8)


async def test_a_side_volume_on_a_pump_with_only_a_total_starts_the_sides(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, total_only_pump())
    engine = await synced(client)

    await engine.edit_pump(client, PUMP, right_volume=Volume(3, "oz"))

    [payload] = posted(fake_server)
    assert (payload["leftAmount"], payload["rightAmount"], payload["amount"]) == (
        measure(0),
        measure(3),
        measure(3),
    )


async def test_minutes_the_side_and_a_note_change_in_one_update(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = iphone_pump()
    seed(fake_server, original)
    engine = await synced(client)

    updated = await engine.edit_pump(
        client, PUMP, right_minutes=12, finish_side=SIDE_RIGHT, note="Second letdown"
    )

    [payload] = posted(fake_server)
    assert changed(payload, original) == {"rightDuration", "finishSide", "note"} | ENVELOPE
    assert (updated.left_duration, updated.right_duration, updated.finish_side) == (
        10,
        12,
        SIDE_RIGHT,
    )


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"amount": Volume(5, "oz"), "left_volume": Volume(3, "oz")},
        {"amount": Volume(0, "oz")},
        {"amount": Volume(5, "cups")},
        {"amount": 5},
        {"left_volume": Volume(100, "ml")},  # the saved right side is in ounces
        {"left_volume": Volume(0, "oz"), "right_volume": Volume(0, "oz")},
        {"left_volume": Volume(-1, "oz")},
        {"left_minutes": -1},
        {"right_minutes": 1.5},
        {"left_minutes": 241},
        {"finish_side": 3},
        {"finish_side": True},
    ],
)
async def test_bad_pump_values_are_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, bad: dict[str, Any]
) -> None:
    seed(fake_server, iphone_pump())
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(ValueError):
        await engine.edit_pump(client, PUMP, **bad)
    assert len(fake_server.requests) == before


async def test_a_pump_cant_be_left_with_no_volume_and_no_length(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    timed = iphone_pump(amount=measure(0), leftDuration=10, rightDuration=0)
    del timed["leftAmount"], timed["rightAmount"]
    seed(fake_server, timed)
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(ValueError):
        await engine.edit_pump(client, PUMP, left_minutes=0)
    assert len(fake_server.requests) == before

    updated = await engine.edit_pump(client, PUMP, left_minutes=0, amount=Volume(2, "oz"))
    assert (updated.left_duration, updated.volume) == (0, Volume(2, "oz"))


async def test_another_kind_of_record_is_refused_and_a_repeat_posts_nothing_more(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_pump(), payload_for("Diaper", rid(32)))
    engine = await synced(client)

    with pytest.raises(WrongRecordTypeError):
        await engine.edit_pump(client, rid(32), amount=Volume(5, "oz"))
    first = await engine.edit_pump(client, PUMP, left_volume=Volume(4, "oz"), left_minutes=11)
    again = await engine.edit_pump(client, PUMP, left_volume=Volume(4, "oz"), left_minutes=11)

    assert again == first
    assert len(posted(fake_server)) == 1
