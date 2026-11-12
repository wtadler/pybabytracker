"""Removing photos from a record (invented data only)."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import PROBE_UUID
from factories import payload_for
from fake_server import FakeSyncServer
from selection_helpers import posted, rid, seed, synced

from pybabytracker import (
    OP_UPDATE,
    BabyTrackerClient,
    Joy,
    NewPicture,
    RecordNotFoundError,
    WrongRecordTypeError,
)

JOY = "EF000000-0000-4000-8000-0000000000C1"
FIRST = "D4000000-0000-4000-8000-0000000000F1"
SECOND = "2F000000-0000-4000-8000-0000000000F2"
PHOTO = b"\xff\xd8\xff\xe0invented-photo\xff\xd9"


def iphone_entry(name: str, **overrides: Any) -> dict[str, Any]:
    """A pictureNote entry as the iPhone app writes one (captured 2026-11-11)."""
    entry: dict[str, Any] = {
        "activityID": JOY,
        "BCObjectType": "Picture",
        "mediaType": 0,
        "fileName": name,
        "deleted": "false",
        "newFlage": "true",
        "timestamp": "1970-01-01 00:00:00 +0000",
        "objectID": name,
    }
    entry.update(overrides)
    return entry


def android_entry(name: str) -> dict[str, Any]:
    """A pictureNote entry as the Android app writes one (captured 2026-11-11)."""
    return {
        "activityID": JOY,
        "attached": True,
        "deleted": False,
        "fileName": name,
        "mediaType": "0",
        "newFlage": True,
        "objectID": name,
        "timestamp": "2026-11-11 20:25:19 +0000",
    }


def joy(*entries: dict[str, Any]) -> dict[str, Any]:
    payload = payload_for("Joy", JOY)
    payload.update(pictureNote=list(entries), newFlage="true")
    return payload


async def test_removing_every_photo_marks_each_entry_deleted_and_changes_nothing_else(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = joy(iphone_entry(FIRST), iphone_entry(SECOND))
    seed(fake_server, original)
    engine = await synced(client)

    updated = await engine.remove_pictures(client, JOY)

    assert [int(e["OPCode"]) for e in fake_server.posted(PROBE_UUID)] == [OP_UPDATE]
    [payload] = posted(fake_server)
    assert list(payload) == list(original)
    assert {k for k in payload if payload[k] != original[k]} == {
        "pictureNote",
        "newFlage",
        "timestamp",
    }
    # The entries stay, with only ``deleted`` changed.
    assert payload["pictureNote"] == [
        iphone_entry(FIRST, deleted="true"),
        iphone_entry(SECOND, deleted="true"),
    ]
    assert isinstance(updated, Joy)
    assert updated.pictures == []
    assert engine.records[JOY].pictures == []


async def test_one_photo_is_removed_by_its_id_in_either_case(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, joy(iphone_entry(FIRST), iphone_entry(SECOND)))
    engine = await synced(client)

    updated = await engine.remove_pictures(client, JOY.lower(), [SECOND.lower()])

    [payload] = posted(fake_server)
    assert [e["deleted"] for e in payload["pictureNote"]] == ["false", "true"]
    assert [p.file_name for p in updated.pictures] == [FIRST]


async def test_an_android_entry_keeps_its_json_bool(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    name = "c3000000-0000-4000-8000-0000000000a3"
    seed(fake_server, joy(android_entry(name)))
    engine = await synced(client)

    await engine.remove_pictures(client, JOY)

    [payload] = posted(fake_server)
    assert payload["pictureNote"] == [{**android_entry(name), "deleted": True}]


async def test_a_photo_already_removed_is_left_alone_and_nothing_to_remove_posts_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, joy(iphone_entry(FIRST, deleted="true"), iphone_entry(SECOND)))
    engine = await synced(client)

    await engine.remove_pictures(client, JOY)
    before = len(fake_server.requests)
    again = await engine.remove_pictures(client, JOY)

    assert len(posted(fake_server)) == 1
    assert len(fake_server.requests) == before
    assert again.pictures == []
    with pytest.raises(ValueError):
        await engine.remove_pictures(client, JOY, [SECOND])  # no longer one of its photos


async def test_a_photo_the_library_added_can_be_removed(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, joy())
    engine = await synced(client)
    with_photo = await engine.add_pictures(client, JOY, [NewPicture(PHOTO, PHOTO)])
    [picture] = with_photo.pictures

    updated = await engine.remove_pictures(client, JOY, [picture.object_id or ""])

    assert updated.pictures == []
    assert posted(fake_server)[-1]["pictureNote"][0]["deleted"] == "true"


@pytest.mark.parametrize("pictures", [[], ["NO-SUCH-PICTURE"], [""], [7], "abc", FIRST])
async def test_a_bad_or_unknown_picture_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, pictures: Any
) -> None:
    seed(fake_server, joy(iphone_entry(FIRST)))
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(ValueError):
        await engine.remove_pictures(client, JOY, pictures)
    assert len(fake_server.requests) == before


async def test_a_record_that_cant_have_photos_or_isnt_there_is_refused(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, payload_for("Diaper", rid(7)))
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(WrongRecordTypeError):
        await engine.remove_pictures(client, rid(7))
    with pytest.raises(RecordNotFoundError):
        await engine.remove_pictures(client, "NO-SUCH-RECORD")
    assert len(fake_server.requests) == before
