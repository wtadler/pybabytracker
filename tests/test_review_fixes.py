"""Edge cases found by a review of the edit and delete paths (invented data only)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from factories import BABY_ID, baby_payload, payload_for
from fake_server import FakeSyncServer
from selection_helpers import PHONE, posted, rid, seed, synced

from pybabytracker import (
    OP_ADD,
    OP_UPDATE,
    BabyTrackerClient,
    EntryInUseError,
    SelectionExistsError,
    TimerNotRunningError,
)

OTHER_BABY = "BA000000-0000-4000-8000-0000000000B2"
DIAPER, SLEEP, JOY = rid(41), rid(42), rid(43)


def stamp(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S +0000")


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def running_sleep(start: datetime) -> dict[str, Any]:
    payload = payload_for("Sleep", SLEEP, time=stamp(start))
    payload.update(duration=0, newFlage="true")
    return payload


async def test_a_baby_cant_take_another_babys_name(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    # add_baby refuses a second baby of one name because the apps merge the two; an edit that
    # made two babies share a name would do the same.
    seed(fake_server, baby_payload(), baby_payload("Other Baby", OTHER_BABY))
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(SelectionExistsError) as raised:
        await engine.edit_baby(client, OTHER_BABY, name="  test baby ")

    assert (raised.value.what, raised.value.object_id) == ("baby", BABY_ID)
    assert len(fake_server.requests) == before
    # Its own name in another case is still an edit of that one baby.
    renamed = await engine.edit_baby(client, OTHER_BABY, name="OTHER BABY")
    assert renamed.name == "OTHER BABY"


async def test_an_edit_back_to_a_value_another_device_changed_is_posted(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = payload_for("Diaper", DIAPER, note="A")
    seed(fake_server, original)
    engine = await synced(client)
    # A phone changed the note, and this engine hasn't synced since: its copy still says "A".
    newer = {**original, "note": "B", "newFlage": "false", "timestamp": "2026-10-26 16:00:00 +0000"}
    fake_server.add_transaction(PHONE, OP_UPDATE, newer)

    updated = await engine.edit_record(client, DIAPER, note="A")

    [payload] = posted(fake_server)
    assert payload["note"] == "A"
    assert updated.note == "A"
    assert engine.records[DIAPER].note == "A"


async def test_a_babys_edit_back_to_a_value_another_device_changed_is_posted(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    baby = baby_payload(timestamp="2026-10-20 08:00:00 +0000")
    seed(fake_server, baby)
    engine = await synced(client)
    fake_server.add_transaction(
        PHONE, OP_UPDATE, {**baby, "name": "Renamed", "timestamp": "2026-10-26 16:00:00 +0000"}
    )

    updated = await engine.edit_baby(client, BABY_ID, name="Test Baby")

    [payload] = posted(fake_server)
    assert (payload["name"], updated.name) == ("Test Baby", "Test Baby")


async def test_editing_a_running_sleep_isnt_taken_for_ending_it(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    start = now() - timedelta(hours=2)
    seed(fake_server, running_sleep(start))
    engine = await synced(client)
    edited = await engine.edit_record(client, SLEEP, note="in the car")
    assert edited.is_running  # type: ignore[attr-defined]
    # The phone that started it ends it.
    ended = {
        **edited.to_payload(),
        "duration": 45,
        "newFlage": "false",
        "timestamp": stamp(now() + timedelta(seconds=30)),
    }
    fake_server.add_transaction(PHONE, OP_UPDATE, ended)
    await engine.sync(client)

    # This device never ended it, so its end isn't quietly dropped.
    with pytest.raises(TimerNotRunningError):
        await engine.end_sleep(client, SLEEP, end=now())
    assert len(posted(fake_server)) == 1


async def test_this_devices_own_end_is_still_returned_on_a_retry(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    start = now() - timedelta(hours=2)
    seed(fake_server, running_sleep(start))
    engine = await synced(client)
    await engine.edit_record(client, SLEEP, note="in the car")
    ended = await engine.end_sleep(client, SLEEP, end=start + timedelta(minutes=30))

    again = await engine.end_sleep(client, SLEEP, end=start + timedelta(minutes=30))

    assert (again.duration, again.object_id) == (ended.duration, SLEEP)
    assert len(posted(fake_server)) == 2  # the note, then the end


@pytest.mark.parametrize("how", ["edit_sleep", "edit_record"])
@pytest.mark.parametrize("offset", [timedelta(hours=3), -timedelta(days=5)])
async def test_a_running_sleeps_start_is_checked_as_a_new_ones(
    client: BabyTrackerClient, fake_server: FakeSyncServer, how: str, offset: timedelta
) -> None:
    seed(fake_server, running_sleep(now() - timedelta(hours=2)))
    engine = await synced(client)
    before = len(fake_server.requests)
    moved = now() + offset

    with pytest.raises(ValueError, match="a sleep can't"):
        if how == "edit_sleep":
            await engine.edit_sleep(client, SLEEP, start=moved)
        else:
            await engine.edit_record(client, SLEEP, time=moved)

    assert len(fake_server.requests) == before
    # A start inside the day still moves, either way.
    ok = now() - timedelta(hours=3)
    if how == "edit_sleep":
        assert (await engine.edit_sleep(client, SLEEP, start=ok)).time == ok
    else:
        assert (await engine.edit_record(client, SLEEP, time=ok)).time == ok


async def test_a_finished_sleeps_time_can_still_move_to_any_day(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    payload = payload_for("Sleep", SLEEP, time="2026-10-01 20:00:00 +0000")
    payload.update(duration=60, newFlage="true")
    seed(fake_server, payload)
    engine = await synced(client)
    earlier = datetime(2026, 9, 30, 20, 0, tzinfo=UTC)

    assert (await engine.edit_record(client, SLEEP, time=earlier)).time == earlier


async def test_a_record_of_an_unknown_type_still_keeps_its_baby_in_use(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    other = baby_payload("Other Baby", OTHER_BABY)
    unknown = {
        "BCObjectType": "SomeFutureType",
        "objectID": rid(44),
        "baby": {"objectID": OTHER_BABY.lower(), "name": "Other Baby"},
        "time": "2026-10-25 15:40:49 +0000",
        "timestamp": "2026-10-25 15:41:00 +0000",
    }
    seed(fake_server, other)
    fake_server.add_transaction(PHONE, OP_ADD, unknown)
    engine = await synced(client)

    assert engine.baby_users(OTHER_BABY) == 1
    with pytest.raises(EntryInUseError):
        await engine.delete_record(client, OTHER_BABY, any_device=True)
    assert posted(fake_server) == []


@pytest.mark.parametrize("picture_note", ["absent", None, []])
async def test_removing_photos_from_a_record_with_none_posts_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer, picture_note: Any
) -> None:
    payload = payload_for("Joy", JOY)
    if picture_note == "absent":
        payload.pop("pictureNote", None)
    else:
        payload["pictureNote"] = picture_note
    seed(fake_server, payload)
    engine = await synced(client)

    unchanged = await engine.remove_pictures(client, JOY)

    assert posted(fake_server) == []
    assert unchanged.pictures == []
