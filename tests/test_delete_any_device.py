"""Deleting a record another device created (invented data only)."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import PROBE_UUID
from factories import BABY_ID, payload_for
from fake_server import FakeSyncServer
from selection_helpers import PHONE, posted, rid, seed, synced

from pybabytracker import (
    OP_DELETE,
    AccountChangedError,
    BabyTrackerClient,
    EntryInUseError,
    NotOwnRecordError,
    RecordNotFoundError,
    SyncEngine,
    WrongRecordTypeError,
)

IPHONE_ACTIVITY = "4F000000-0000-4000-8000-0000000000A1"
ANDROID_JOY = "09000000-0000-4000-8000-0000000000b2"
FOOD = {"BCObjectType": "OtherFeedSelection", "objectID": "F-1", "name": "Avocado"}


def iphone_activity() -> dict[str, Any]:
    """An activity with a photo, as an iPhone synced one, with an unknown key."""
    payload = payload_for("OtherActivity", IPHONE_ACTIVITY)
    payload["desc"] = {"objectID": "T-1", "name": "Tummy Time", "newFlage": "false"}
    payload["duration"] = 1
    payload["newFlage"] = "true"
    payload["pictureNote"] = [
        {
            "activityID": IPHONE_ACTIVITY,
            "BCObjectType": "Picture",
            "mediaType": 0,
            "fileName": "D1000000-0000-4000-8000-0000000000F1",
            "deleted": "false",
            "newFlage": "true",
            "timestamp": "1970-01-01 00:00:00 +0000",
            "objectID": "D1000000-0000-4000-8000-0000000000F1",
        }
    ]
    payload["someFutureKey"] = {"kept": True}
    return payload


def android_joy() -> dict[str, Any]:
    """A joy as an Android phone synced one: JSON bools and a lowercase id."""
    payload = payload_for("Joy", ANDROID_JOY)
    payload.update(newFlage=True, pictureLoaded=True)
    return payload


def opcodes(server: FakeSyncServer) -> list[int]:
    return [int(e["OPCode"]) for e in server.posted(PROBE_UUID)]


async def test_another_devices_record_is_deleted_with_the_record_as_last_saved(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = iphone_activity()
    seed(fake_server, original)
    engine = await synced(client)

    await engine.delete_record(client, IPHONE_ACTIVITY, any_device=True)

    assert opcodes(fake_server) == [OP_DELETE]
    [payload] = posted(fake_server)
    # As the apps delete each other's records: everything as saved, only newFlage changed, and
    # the timestamp not bumped.
    assert list(payload) == list(original)
    assert payload == {**original, "newFlage": "false"}
    assert IPHONE_ACTIVITY not in engine.records
    assert engine.is_deleted(IPHONE_ACTIVITY)
    assert IPHONE_ACTIVITY not in engine.created_ids


async def test_an_android_record_is_deleted_under_the_id_it_has(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, android_joy())
    engine = await synced(client)

    await engine.delete_record(client, ANDROID_JOY.upper(), any_device=True)

    [payload] = posted(fake_server)
    assert payload["objectID"] == ANDROID_JOY
    assert payload["newFlage"] == "false"
    assert payload["pictureLoaded"] is True
    assert engine.is_deleted(ANDROID_JOY)


async def test_a_fresh_engine_reading_the_logs_agrees_the_record_is_gone(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_activity())
    engine = await synced(client)
    await engine.delete_record(client, IPHONE_ACTIVITY, any_device=True)

    other = SyncEngine()
    await other.sync(client)

    assert IPHONE_ACTIVITY not in other.records
    assert other.is_deleted(IPHONE_ACTIVITY)


async def test_a_pump_can_be_deleted_too(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    pump = payload_for("Pump", rid(8))
    pump.pop("baby", None)
    seed(fake_server, pump)
    engine = await synced(client)

    await engine.delete_record(client, rid(8), any_device=True)

    assert engine.is_deleted(rid(8))


async def test_without_any_device_another_devices_record_is_still_refused(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_activity())
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(NotOwnRecordError):
        await engine.delete_record(client, IPHONE_ACTIVITY)
    with pytest.raises(NotOwnRecordError):
        await engine.delete_record(client, IPHONE_ACTIVITY, any_device=False)
    assert len(fake_server.requests) == before
    assert IPHONE_ACTIVITY in engine.records


@pytest.mark.parametrize(
    ("object_id", "error"),
    [
        ("NO-SUCH-RECORD", RecordNotFoundError),
        (rid(7), RecordNotFoundError),  # already deleted
        (BABY_ID, EntryInUseError),  # a baby that still has records
        (rid(9), WrongRecordTypeError),  # a type the library doesn't know
    ],
)
async def test_what_another_device_made_and_cant_be_deleted_is_refused_with_no_request(
    client: BabyTrackerClient,
    fake_server: FakeSyncServer,
    object_id: str,
    error: type[Exception],
) -> None:
    unknown = {"BCObjectType": "Supplement", "objectID": rid(9)}
    seed(fake_server, payload_for("Diaper", rid(7)), FOOD, unknown)
    fake_server.add_transaction(PHONE, OP_DELETE, payload_for("Diaper", rid(7)))
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(error):
        await engine.delete_record(client, object_id, any_device=True)
    assert len(fake_server.requests) == before


async def test_any_device_still_deletes_this_devices_own_records(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    food = await engine.add_food(client, "Avocado")

    # Its own pick-list entry: allowed with or without any_device, as before.
    await engine.delete_record(client, food.object_id, any_device=True)

    assert opcodes(fake_server) == [0, OP_DELETE]
    with pytest.raises(RecordNotFoundError):
        await engine.delete_record(client, food.object_id, any_device=True)


async def test_deleting_twice_is_refused_the_second_time(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_activity())
    engine = await synced(client)
    await engine.delete_record(client, IPHONE_ACTIVITY, any_device=True)

    with pytest.raises(RecordNotFoundError):
        await engine.delete_record(client, IPHONE_ACTIVITY, any_device=True)
    assert opcodes(fake_server) == [OP_DELETE]


async def test_a_delete_after_the_group_is_reset_posts_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_activity())
    engine = await synced(client)
    fake_server.account_id = 777  # the group was reset in the app
    fake_server.sessions.clear()

    with pytest.raises(AccountChangedError):
        await engine.delete_record(client, IPHONE_ACTIVITY, any_device=True)
    assert posted(fake_server) == []


async def test_the_delete_carries_what_another_device_saved_since_the_last_sync(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_activity())
    engine = await synced(client)
    newer = iphone_activity() | {"note": "Edited on the phone", "newFlage": "false"}
    newer["timestamp"] = "2026-10-26 16:00:00 +0000"
    fake_server.add_transaction(PHONE, 1, newer)  # an edit this engine hasn't synced

    await engine.delete_record(client, IPHONE_ACTIVITY, any_device=True)

    [payload] = posted(fake_server)
    assert payload == newer


async def test_a_record_deleted_on_another_device_since_the_last_sync_is_not_deleted_again(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_activity())
    engine = await synced(client)
    fake_server.add_transaction(PHONE, OP_DELETE, iphone_activity() | {"newFlage": "false"})

    with pytest.raises(RecordNotFoundError):
        await engine.delete_record(client, IPHONE_ACTIVITY, any_device=True)
    assert posted(fake_server) == []
