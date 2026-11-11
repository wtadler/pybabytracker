"""Logging a joy, and adding photos to a saved record (invented data only)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import PROBE_UUID
from factories import BABY_ID, payload_for
from fake_server import FakeSyncServer
from selection_helpers import PHONE, posted, rid, seed, synced

from pybabytracker import (
    OP_DELETE,
    OP_UPDATE,
    AccountChangedError,
    BabyTrackerClient,
    BabyTrackerConnectionError,
    Joy,
    NewPicture,
    OtherActivity,
    RecordNotFoundError,
    SyncEngine,
    WrongRecordTypeError,
)

EVENT_TIME = datetime(2026, 11, 11, 20, 36, tzinfo=UTC)
PHOTO = b"\xff\xd8\xff\xe0invented-photo\xff\xd9"
THUMBNAIL = b"\xff\xd8\xff\xe0invented-thumbnail\xff\xd9"
PICTURES = [NewPicture(PHOTO, THUMBNAIL)]
# The keys the iPhone app sends for a joy (and baby), captured 2026-11-11.
JOY_KEYS = {
    "BCObjectType",
    "note",
    "time",
    "timezone",
    "newFlage",
    "pictureLoaded",
    "pictureNote",
    "timestamp",
    "objectID",
}
ACTIVITY_ID = "5A000000-0000-4000-8000-0000000000A1"
OLD_PICTURE = {
    "activityID": ACTIVITY_ID,
    "BCObjectType": "Picture",
    "mediaType": 0,
    "fileName": "2F000000-0000-4000-8000-0000000000F1",
    "deleted": "false",
    "newFlage": "true",
    "timestamp": "1970-01-01 00:00:00 +0000",
    "objectID": "2F000000-0000-4000-8000-0000000000F1",
}


def iphone_activity(**overrides: Any) -> dict[str, Any]:
    """An activity another phone logged, in the iPhone's key order, with an unknown key."""
    payload = payload_for("OtherActivity", ACTIVITY_ID)
    payload["desc"] = {"objectID": "T-1", "name": "Brush Teeth", "newFlage": "false"}
    payload["duration"] = 0
    payload["newFlage"] = "true"
    payload["someFutureKey"] = {"kept": True}
    payload.update(overrides)
    return payload


def test_add_pictures_names_the_record_types_the_app_attaches_photos_to() -> None:
    assert "joy, milestone, or activity" in (SyncEngine.add_pictures.__doc__ or "")


def updates(server: FakeSyncServer) -> list[int]:
    return [int(e["OPCode"]) for e in server.posted(PROBE_UUID)]


async def test_a_joy_with_a_photo_is_written_as_the_iphone_app_writes_one(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    joy = await engine.add_joy(
        client, BABY_ID, time=EVENT_TIME, note="First laugh", pictures=PICTURES
    )

    assert isinstance(joy, Joy)
    [payload] = posted(fake_server)
    assert set(payload) - {"baby"} == JOY_KEYS
    assert (payload["BCObjectType"], payload["note"]) == ("Joy", "First laugh")
    [entry] = payload["pictureNote"]
    assert entry["activityID"] == joy.object_id
    [picture] = joy.pictures
    assert fake_server.pictures[f"{picture.file_name}.jpg"] == PHOTO


async def test_a_joy_without_a_photo_has_an_empty_picture_note(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    await engine.add_joy(client, BABY_ID, note="Giggles")

    [payload] = posted(fake_server)
    assert payload["pictureNote"] == []
    assert fake_server.uploads == []


async def test_adding_a_photo_posts_the_record_as_saved_with_three_fields_changed(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = iphone_activity()
    seed(fake_server, original)
    engine = await synced(client)

    updated = await engine.add_pictures(client, ACTIVITY_ID, PICTURES)

    assert updates(fake_server) == [OP_UPDATE]
    [payload] = posted(fake_server)
    assert list(payload) == list(original)  # key order kept
    changed = {k for k in payload if payload[k] != original[k]}
    assert changed == {"pictureNote", "newFlage", "timestamp"}
    assert payload["newFlage"] == "false"
    assert payload["timestamp"] > original["timestamp"]
    [entry] = payload["pictureNote"]
    assert entry["activityID"] == ACTIVITY_ID
    assert payload["someFutureKey"] == {"kept": True}
    assert isinstance(updated, OtherActivity)
    assert engine.records[ACTIVITY_ID] == updated
    [picture] = updated.pictures
    assert fake_server.pictures[f"{picture.file_name}__thumbnail.jpg"] == THUMBNAIL


async def test_a_milestone_takes_photos_too(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    milestone = payload_for("Milestone", rid(5))
    milestone["milestoneType"] = {"objectID": "M-1", "name": "First smile"}
    seed(fake_server, milestone)
    engine = await synced(client)

    updated = await engine.add_pictures(client, rid(5), PICTURES)

    assert len(updated.pictures) == 1


async def test_photos_already_there_are_kept_ahead_of_the_new_ones(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_activity(pictureNote=[OLD_PICTURE]))
    engine = await synced(client)

    updated = await engine.add_pictures(client, ACTIVITY_ID, PICTURES + PICTURES)

    [payload] = posted(fake_server)
    assert payload["pictureNote"][0] == OLD_PICTURE
    assert len(payload["pictureNote"]) == 3
    assert len({p.file_name for p in updated.pictures}) == 3


async def test_an_android_record_keeps_its_own_values_and_id_case(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    android_id = "ea000000-0000-4000-8000-0000000000a2"
    android = payload_for("OtherActivity", android_id)
    android.update(newFlage=True, pictureLoaded=True, duration=0, desc={"objectID": "T-2"})
    seed(fake_server, android)
    engine = await synced(client)

    await engine.add_pictures(client, android_id.upper(), PICTURES)

    [payload] = posted(fake_server)
    assert payload["objectID"] == android_id
    assert payload["pictureLoaded"] is True
    assert payload["newFlage"] == "false"


async def test_a_stored_timestamp_ahead_of_this_clock_is_passed_by_a_second(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_activity(timestamp="2099-01-01 00:00:00 +0000"))
    engine = await synced(client)

    await engine.add_pictures(client, ACTIVITY_ID, PICTURES)

    [payload] = posted(fake_server)
    assert payload["timestamp"] == "2099-01-01 00:00:01 +0000"


@pytest.mark.parametrize(
    ("object_id", "error"),
    [
        ("NO-SUCH-RECORD", RecordNotFoundError),
        (rid(7), RecordNotFoundError),  # deleted
        (BABY_ID, WrongRecordTypeError),
        (rid(8), WrongRecordTypeError),  # a pump
        (rid(9), WrongRecordTypeError),  # a diaper: the app has no photo picker for one
        (rid(1), WrongRecordTypeError),  # another diaper
    ],
)
async def test_a_record_that_cant_take_photos_is_refused_with_no_request(
    client: BabyTrackerClient,
    fake_server: FakeSyncServer,
    object_id: str,
    error: type[Exception],
) -> None:
    pump = payload_for("Pump", rid(8))
    pump.pop("baby", None)
    seed(fake_server, payload_for("Diaper", rid(7)), pump, payload_for("Diaper", rid(9)))
    fake_server.add_transaction(PHONE, OP_DELETE, payload_for("Diaper", rid(7)))
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(error):
        await engine.add_pictures(client, object_id, PICTURES)
    assert len(fake_server.requests) == before


@pytest.mark.parametrize("pictures", [[], (), [b"photo"], NewPicture(PHOTO, THUMBNAIL), None])
async def test_no_or_bad_pictures_are_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, pictures: Any
) -> None:
    seed(fake_server, iphone_activity())
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(ValueError):
        await engine.add_pictures(client, ACTIVITY_ID, pictures)
    assert len(fake_server.requests) == before


async def test_a_failed_upload_posts_no_update(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_activity())
    engine = await synced(client)
    fake_server.upload_status = 500

    with pytest.raises(BabyTrackerConnectionError):
        await engine.add_pictures(client, ACTIVITY_ID, PICTURES)
    assert posted(fake_server) == []
    assert not engine.to_dict()["pending"]
    assert engine.records[ACTIVITY_ID].pictures == []


async def test_photos_after_the_group_is_reset_post_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_activity())
    engine = await synced(client)
    fake_server.account_id = 777  # the group was reset in the app
    fake_server.sessions.clear()

    with pytest.raises(AccountChangedError):
        await engine.add_pictures(client, ACTIVITY_ID, PICTURES)
    with pytest.raises(AccountChangedError):
        await engine.add_joy(client, BABY_ID, pictures=PICTURES)
    assert fake_server.uploads == []
    assert posted(fake_server) == []
