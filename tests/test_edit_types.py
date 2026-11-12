"""Editing a diaper, a bottle, a nursing, and a sleep (invented data only)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import PROBE_UUID
from factories import payload_for
from fake_server import FakeSyncServer
from selection_helpers import posted, rid, seed, synced

from pybabytracker import (
    DIAPER_DIRTY,
    DIAPER_MIXED,
    OP_UPDATE,
    SIDE_LEFT,
    SIDE_RIGHT,
    BabyTrackerClient,
    Diaper,
    Formula,
    Nursing,
    Pumped,
    Sleep,
    Volume,
    WrongRecordTypeError,
)
from pybabytracker.builders import MAX_MINUTES_PER_SIDE

DIAPER, BOTTLE, NURSING, SLEEP = rid(11), rid(12), rid(13), rid(14)
ENVELOPE = {"newFlage", "timestamp"}


def diaper() -> dict[str, Any]:
    payload = payload_for("Diaper", DIAPER)
    payload.update(status=0, newFlage="true")
    return payload


def bottle(kind: str = "Formula") -> dict[str, Any]:
    payload = payload_for(kind, BOTTLE)
    payload["amount"] = {"value": 4, "englishMeasure": "true", "BCObjectType": "VolumeMeasure"}
    payload["newFlage"] = "true"
    return payload


def iphone_nursing() -> dict[str, Any]:
    """A nursing as an iPhone's timer synced it: 12 minutes on the left."""
    payload = payload_for("Nursing", NURSING, time="2026-11-10 10:18:00 +0000")
    payload.update(
        leftDuration=12,
        rightDuration=0,
        bothDuration=0,
        finishSide=1,
        isDetailed="true",
        pairSessionState=0,
        state=5,
        newFlage="true",
    )
    return payload


def sleep(duration: int = 60) -> dict[str, Any]:
    payload = payload_for("Sleep", SLEEP, time="2026-11-11 20:00:00 +0000")
    payload.update(duration=duration, newFlage="true")
    return payload


def changed(payload: dict[str, Any], original: dict[str, Any]) -> set[str]:
    return {k for k in set(payload) | set(original) if payload.get(k) != original.get(k)}


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 11, hour, minute, tzinfo=UTC)


# --- diaper ------------------------------------------------------------------------------


async def test_a_diapers_kind_changes_and_nothing_else(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = diaper()
    seed(fake_server, original)
    engine = await synced(client)

    updated = await engine.edit_diaper(client, DIAPER, status=DIAPER_MIXED)

    assert [int(e["OPCode"]) for e in fake_server.posted(PROBE_UUID)] == [OP_UPDATE]
    [payload] = posted(fake_server)
    assert list(payload) == list(original)
    assert changed(payload, original) == {"status"} | ENVELOPE
    assert payload["status"] == DIAPER_MIXED
    assert isinstance(updated, Diaper)
    assert updated.status == DIAPER_MIXED


async def test_a_diapers_kind_time_and_note_change_in_one_update(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = diaper()
    seed(fake_server, original)
    engine = await synced(client)

    await engine.edit_diaper(client, DIAPER, status=DIAPER_DIRTY, time=at(9), note="Late log")

    [payload] = posted(fake_server)
    assert changed(payload, original) == {"status", "time", "note"} | ENVELOPE
    assert payload["time"] == "2026-11-11 09:00:00 +0000"


@pytest.mark.parametrize("status", [3, -1, True, "1", 1.0])
async def test_a_bad_diaper_kind_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, status: Any
) -> None:
    seed(fake_server, diaper())
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(ValueError):
        await engine.edit_diaper(client, DIAPER, status=status)
    assert len(fake_server.requests) == before


# --- bottle ------------------------------------------------------------------------------


@pytest.mark.parametrize(("kind", "model"), [("Formula", Formula), ("Pumped", Pumped)])
async def test_a_bottles_volume_changes_and_nothing_else(
    client: BabyTrackerClient, fake_server: FakeSyncServer, kind: str, model: type
) -> None:
    original = bottle(kind)
    seed(fake_server, original)
    engine = await synced(client)

    updated = await engine.edit_bottle(client, BOTTLE, volume=Volume(150, "ml"))

    [payload] = posted(fake_server)
    assert payload["BCObjectType"] == kind
    assert changed(payload, original) == {"amount"} | ENVELOPE
    assert payload["amount"] == {
        "BCObjectType": "VolumeMeasure",
        "englishMeasure": "false",
        "value": 150,
    }
    assert isinstance(updated, model)


@pytest.mark.parametrize(
    "volume", [Volume(0, "oz"), Volume(-1, "oz"), Volume(0.001, "oz"), Volume(4, "cups"), 4]
)
async def test_a_bad_volume_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, volume: Any
) -> None:
    seed(fake_server, bottle())
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(ValueError):
        await engine.edit_bottle(client, BOTTLE, volume=volume)
    assert len(fake_server.requests) == before


# --- nursing -----------------------------------------------------------------------------


async def test_a_nursings_minutes_change_as_an_iphones_edit_changed_them(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = iphone_nursing()
    seed(fake_server, original)
    engine = await synced(client)

    updated = await engine.edit_nursing(client, NURSING, right_minutes=7)

    [payload] = posted(fake_server)
    # As an iPhone's edit (captured 2026-11-12): rightDuration changes, the timer keys go, and
    # the left minutes, the side, and the start stay.
    assert (
        changed(payload, original)
        == {
            "rightDuration",
            "isDetailed",
            "pairSessionState",
            "state",
        }
        | ENVELOPE
    )
    assert payload["rightDuration"] == 7
    assert not {"isDetailed", "pairSessionState", "state"} & set(payload)
    assert (payload["leftDuration"], payload["finishSide"]) == (12, 1)
    assert isinstance(updated, Nursing)
    assert (updated.left_duration, updated.right_duration) == (12, 7)


async def test_a_nursings_side_alone_keeps_the_timer_keys(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = iphone_nursing()
    seed(fake_server, original)
    engine = await synced(client)

    await engine.edit_nursing(client, NURSING, finish_side=SIDE_RIGHT)

    [payload] = posted(fake_server)
    assert changed(payload, original) == {"finishSide"} | ENVELOPE
    assert payload["finishSide"] == SIDE_RIGHT


async def test_both_sides_and_the_start_can_change_together(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_nursing())
    engine = await synced(client)

    updated = await engine.edit_nursing(
        client, NURSING, left_minutes=5, right_minutes=6, finish_side=SIDE_LEFT, time=at(10, 5)
    )

    assert (updated.left_duration, updated.right_duration, updated.time) == (5, 6, at(10, 5))


@pytest.mark.parametrize(
    "minutes",
    [
        {"left_minutes": 0},  # would leave no minutes at all
        {"left_minutes": -1},
        {"right_minutes": MAX_MINUTES_PER_SIDE + 1},
        {"left_minutes": 1.5},
        {"right_minutes": True},
        {"finish_side": 3},
        {"finish_side": True},
    ],
)
async def test_bad_nursing_minutes_or_side_are_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, minutes: dict[str, Any]
) -> None:
    seed(fake_server, iphone_nursing())
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(ValueError):
        await engine.edit_nursing(client, NURSING, **minutes)
    assert len(fake_server.requests) == before


async def test_an_android_timers_minutes_cant_be_edited_but_its_note_can(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    session = payload_for("NursingSession", rid(15))
    session.update(leftDuration=5, rightDuration=0, leftTimestamps=[], rightTimestamps=[])
    seed(fake_server, session)
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(WrongRecordTypeError):
        await engine.edit_nursing(client, rid(15), left_minutes=6)
    assert len(fake_server.requests) == before

    updated = await engine.edit_nursing(client, rid(15), note="Fussy")
    assert updated.note == "Fussy"
    assert posted(fake_server)[0]["leftTimestamps"] == []


# --- sleep -------------------------------------------------------------------------------


async def test_a_sleeps_end_changes_its_length(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = sleep(60)
    seed(fake_server, original)
    engine = await synced(client)

    updated = await engine.edit_sleep(client, SLEEP, end=at(21, 30))

    [payload] = posted(fake_server)
    assert changed(payload, original) == {"duration"} | ENVELOPE
    assert payload["duration"] == 90
    assert isinstance(updated, Sleep)


async def test_a_sleeps_new_start_keeps_its_end(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = sleep(60)  # 20:00 to 21:00
    seed(fake_server, original)
    engine = await synced(client)

    await engine.edit_sleep(client, SLEEP, start=at(19, 45))

    [payload] = posted(fake_server)
    assert changed(payload, original) == {"time", "duration"} | ENVELOPE
    assert (payload["time"], payload["duration"]) == ("2026-11-11 19:45:00 +0000", 75)


async def test_a_running_sleep_stays_running_when_its_start_moves_and_an_end_finishes_it(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, sleep(0))
    engine = await synced(client)

    moved = await engine.edit_sleep(client, SLEEP, start=at(19, 50))
    assert (moved.is_running, moved.time) == (True, at(19, 50))

    ended = await engine.edit_sleep(client, SLEEP, end=at(20, 20))
    assert (ended.is_running, ended.duration) == (False, 30)


@pytest.mark.parametrize(
    "times",
    [
        {"end": datetime(2026, 11, 11, 20, 0, tzinfo=UTC)},  # at the start
        {"end": datetime(2026, 11, 11, 19, 0, tzinfo=UTC)},  # before the start
        {"end": datetime(2026, 11, 12, 20, 1, tzinfo=UTC)},  # over 24 hours
        {"start": datetime(2026, 11, 11, 21, 0, tzinfo=UTC)},  # at the saved end
        {"start": datetime(2026, 11, 11, 19, 0)},  # naive
        {"end": "2026-11-11 21:00:00 +0000"},
    ],
)
async def test_bad_sleep_times_are_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, times: dict[str, Any]
) -> None:
    seed(fake_server, sleep(60))
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(ValueError):
        await engine.edit_sleep(client, SLEEP, **times)
    assert len(fake_server.requests) == before


# --- shared ------------------------------------------------------------------------------


async def test_each_edit_refuses_another_kind_of_record_and_nothing_to_change(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, diaper(), bottle(), iphone_nursing(), sleep())
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(WrongRecordTypeError):
        await engine.edit_diaper(client, BOTTLE, status=DIAPER_DIRTY)
    with pytest.raises(WrongRecordTypeError):
        await engine.edit_bottle(client, DIAPER, volume=Volume(4, "oz"))
    with pytest.raises(WrongRecordTypeError):
        await engine.edit_nursing(client, SLEEP, left_minutes=5)
    with pytest.raises(WrongRecordTypeError):
        await engine.edit_sleep(client, NURSING, end=at(21))
    with pytest.raises(ValueError):
        await engine.edit_diaper(client, DIAPER)
    with pytest.raises(ValueError):
        await engine.edit_bottle(client, BOTTLE)
    with pytest.raises(ValueError):
        await engine.edit_nursing(client, NURSING)
    with pytest.raises(ValueError):
        await engine.edit_sleep(client, SLEEP)
    assert len(fake_server.requests) == before


async def test_repeating_an_edit_posts_nothing_more(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, diaper(), bottle(), iphone_nursing(), sleep())
    engine = await synced(client)
    for _ in range(2):
        await engine.edit_diaper(client, DIAPER, status=DIAPER_MIXED)
        await engine.edit_bottle(client, BOTTLE, volume=Volume(5, "oz"))
        await engine.edit_nursing(client, NURSING, right_minutes=7)
        await engine.edit_sleep(client, SLEEP, end=at(21, 30))

    assert len(posted(fake_server)) == 4
