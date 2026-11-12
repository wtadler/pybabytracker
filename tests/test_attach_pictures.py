"""Attaching photos to a new record (invented data only)."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from factories import BABY_ID
from fake_server import FakeSyncServer
from selection_helpers import posted, seed, synced

from pybabytracker import (
    MEDIA_PHOTO,
    AccountChangedError,
    BabyTrackerClient,
    BabyTrackerConnectionError,
    NewPicture,
    SyncEngine,
)
from pybabytracker import models as models_module

EVENT_TIME = datetime(2026, 11, 11, 19, 51, tzinfo=UTC)
PHOTO = b"\xff\xd8\xff\xe0invented-photo\xff\xd9"
THUMBNAIL = b"\xff\xd8\xff\xe0invented-thumbnail\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n"
# The keys of a pictureNote entry the iPhone app wrote for a photo added with a record
# (captured 2026-11-11 from a tummy time logged with a photo).
ENTRY_KEYS = {
    "activityID",
    "BCObjectType",
    "mediaType",
    "fileName",
    "deleted",
    "newFlage",
    "timestamp",
    "objectID",
}


def transaction_posts(server: FakeSyncServer) -> list[int]:
    """Where each transaction POST falls among all requests."""
    return [
        i
        for i, r in enumerate(server.requests)
        if (r.method, r.path) == ("POST", "/account/transaction")
    ]


async def test_a_photo_is_uploaded_with_its_thumbnail_before_the_record(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    record = await engine.add_activity(
        client, BABY_ID, "Tummy Time", time=EVENT_TIME, pictures=[NewPicture(PHOTO, THUMBNAIL)]
    )

    [entry] = posted(fake_server)[0]["pictureNote"]
    name = entry["fileName"]
    assert name == name.upper() and len(name) == 36
    assert fake_server.pictures == {f"{name}.jpg": PHOTO, f"{name}__thumbnail.jpg": THUMBNAIL}
    uploads = [
        i
        for i, r in enumerate(fake_server.requests)
        if (r.method, r.path) == ("POST", "/account/picture")
    ]
    assert len(uploads) == 2
    assert max(uploads) < min(transaction_posts(fake_server))
    [picture] = record.pictures
    assert (picture.file, picture.thumbnail_file) == (f"{name}.jpg", f"{name}__thumbnail.jpg")
    assert await client.get_picture(picture.thumbnail_file) == THUMBNAIL


async def test_the_entry_is_written_as_the_iphone_app_writes_one(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    record = await engine.add_activity(
        client, BABY_ID, "Tummy Time", time=EVENT_TIME, pictures=[NewPicture(PHOTO, THUMBNAIL)]
    )

    [entry] = posted(fake_server)[0]["pictureNote"]
    assert set(entry) == ENTRY_KEYS
    assert entry == {
        "activityID": record.object_id,
        "BCObjectType": "Picture",
        "mediaType": MEDIA_PHOTO,
        "fileName": entry["fileName"],
        "deleted": "false",
        "newFlage": "true",
        "timestamp": "1970-01-01 00:00:00 +0000",
        "objectID": entry["fileName"],
    }


async def test_several_photos_get_their_own_names_in_order(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    second = PHOTO + b"-2"

    record = await engine.add_activity(
        client,
        BABY_ID,
        "Tummy Time",
        pictures=[NewPicture(PHOTO, THUMBNAIL), NewPicture(second, THUMBNAIL)],
    )

    first_file, second_file = (p.file for p in record.pictures)
    assert first_file is not None and second_file is not None
    assert first_file != second_file
    assert (fake_server.pictures[first_file], fake_server.pictures[second_file]) == (PHOTO, second)


async def test_without_pictures_nothing_is_uploaded(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    await engine.add_activity(client, BABY_ID, "Tummy Time")

    assert fake_server.uploads == []
    assert posted(fake_server)[0]["pictureNote"] == []


Add = Callable[[SyncEngine, BabyTrackerClient, list[NewPicture]], Awaitable[Any]]
ADDS: dict[str, Add] = {
    "milestone": lambda e, c, p: e.add_milestone(c, BABY_ID, "First smile", pictures=p),
    "activity": lambda e, c, p: e.add_activity(c, BABY_ID, "Tummy Time", pictures=p),
}


@pytest.mark.parametrize("kind", sorted(ADDS))
async def test_the_adds_whose_app_screens_take_photos_take_pictures(
    client: BabyTrackerClient, fake_server: FakeSyncServer, kind: str
) -> None:
    seed(fake_server)
    engine = await synced(client)

    record = await ADDS[kind](engine, client, [NewPicture(PHOTO, THUMBNAIL)])

    [picture] = record.pictures
    assert picture.file is not None
    assert fake_server.pictures[picture.file] == PHOTO
    [entry] = posted(fake_server)[-1]["pictureNote"]
    assert entry["activityID"] == record.object_id


def test_only_those_adds_take_pictures() -> None:
    """The Android app's entry screens attach photos only to a joy, a milestone, and an
    activity; the rest have no photo picker, so no other add writes pictures."""
    taking = sorted(
        name
        for name, method in inspect.getmembers(SyncEngine, inspect.iscoroutinefunction)
        if name.startswith("add_") and "pictures" in inspect.signature(method).parameters
    )
    assert taking == ["add_activity", "add_joy", "add_milestone", "add_pictures"]


@pytest.mark.parametrize(
    "pictures",
    [
        NewPicture(PHOTO, THUMBNAIL),  # one picture, not a list of them
        PHOTO,
        "photo.jpg",
        [(PHOTO, THUMBNAIL)],
        [PHOTO],
        [NewPicture(PNG, THUMBNAIL)],
        [NewPicture(PHOTO, PNG)],
        [NewPicture(PHOTO, b"")],
        [NewPicture("\xff\xd8\xff", THUMBNAIL)],  # type: ignore[arg-type]
        [NewPicture(PHOTO, THUMBNAIL), None],
        None,
    ],
)
async def test_bad_pictures_are_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, pictures: Any
) -> None:
    seed(fake_server)
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(ValueError):
        await engine.add_activity(client, BABY_ID, "Tummy Time", pictures=pictures)
    assert len(fake_server.requests) == before


async def test_a_picture_over_the_limit_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(fake_server)
    engine = await synced(client)
    before = len(fake_server.requests)
    monkeypatch.setattr(models_module, "MAX_PICTURE_BYTES", len(PHOTO) - 1)

    with pytest.raises(ValueError):
        await engine.add_activity(
            client, BABY_ID, "Tummy Time", pictures=[NewPicture(PHOTO, THUMBNAIL)]
        )
    assert len(fake_server.requests) == before


async def test_a_failed_upload_posts_no_record_and_leaves_nothing_pending(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    fake_server.upload_status = 500

    with pytest.raises(BabyTrackerConnectionError):
        await engine.add_activity(
            client, BABY_ID, "Tummy Time", pictures=[NewPicture(PHOTO, THUMBNAIL)]
        )
    assert posted(fake_server) == []
    assert not engine.to_dict()["pending"]

    fake_server.upload_status = 201
    record = await engine.add_activity(
        client, BABY_ID, "Tummy Time", pictures=[NewPicture(PHOTO, THUMBNAIL)]
    )
    assert [p["objectID"] for p in posted(fake_server)] == [record.object_id]


async def test_retrying_a_write_that_landed_uploads_nothing_again(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    object_id = "D0000000-0000-4000-8000-0000000000AA"
    pictures = [NewPicture(PHOTO, THUMBNAIL)]

    first = await engine.add_activity(
        client, BABY_ID, "Tummy Time", object_id=object_id, pictures=pictures
    )
    again = await engine.add_activity(
        client, BABY_ID, "Tummy Time", object_id=object_id, pictures=pictures
    )

    assert again == first
    assert len(fake_server.uploads) == 2


async def test_pictures_after_the_group_is_reset_upload_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    fake_server.account_id = 777  # the group was reset in the app
    fake_server.sessions.clear()

    with pytest.raises(AccountChangedError):
        await engine.add_activity(
            client, BABY_ID, "Tummy Time", pictures=[NewPicture(PHOTO, THUMBNAIL)]
        )
    assert fake_server.uploads == []
    assert posted(fake_server) == []
