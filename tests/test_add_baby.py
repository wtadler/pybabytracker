"""Adding a baby (invented data only)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import PROBE_UUID
from factories import BABY_ID
from fake_server import FakeSyncServer
from selection_helpers import posted, seed, synced

from pybabytracker import (
    DIAPER_WET,
    MAX_BABY_NAME_LENGTH,
    AccountChangedError,
    Baby,
    BabyTrackerClient,
    BabyTrackerConnectionError,
    SelectionExistsError,
    SyncEngine,
)

BIRTH = datetime(2026, 10, 27, 16, 17, 31, tzinfo=UTC)
DUE = datetime(2026, 11, 2, 5, 39, 38, tzinfo=UTC)
PHOTO = b"\xff\xd8\xff\xe0invented-photo\xff\xd9"
LANDSCAPE = b"\xff\xd8\xff\xe0invented-landscape\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n"
# The keys of a Baby update an iPhone posted (captured 2026-11-12).
IPHONE_KEYS = {
    "dueDay",
    "BCObjectType",
    "gender",
    "pictureName",
    "dob",
    "newFlage",
    "timestamp",
    "name",
    "objectID",
}


async def test_a_baby_with_everything_has_the_keys_of_an_iphones_baby(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    baby = await engine.add_baby(
        client, "  Second Baby ", birth=BIRTH, boy=True, due=DUE, photo=PHOTO
    )

    assert isinstance(baby, Baby)
    assert [int(e["OPCode"]) for e in fake_server.posted(PROBE_UUID)] == [0]
    [payload] = posted(fake_server)
    assert set(payload) == IPHONE_KEYS
    name = payload["pictureName"]
    assert {k: payload[k] for k in IPHONE_KEYS - {"objectID", "timestamp", "pictureName"}} == {
        "BCObjectType": "Baby",
        "name": "Second Baby",
        "dob": "2026-10-27 16:17:31 +0000",
        "dueDay": "2026-11-02 05:39:38 +0000",
        "gender": "true",
        "newFlage": "true",
    }
    assert fake_server.pictures == {f"{name}.jpg": PHOTO, f"{name}_l.jpg": PHOTO}
    assert baby.picture_file == f"{name}.jpg"
    assert engine.babies()[baby.object_id] == baby


async def test_a_baby_with_only_what_the_app_asks_for_leaves_the_rest_out(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    baby = await engine.add_baby(client, "Second Baby", birth=BIRTH, boy=False)

    [payload] = posted(fake_server)
    assert set(payload) == IPHONE_KEYS - {"dueDay", "pictureName"}
    assert payload["gender"] == "false"
    assert fake_server.uploads == []
    assert (baby.picture_file, baby.landscape_picture_file) == (None, None)


async def test_a_landscape_photo_of_its_own_is_uploaded_as_the_landscape_copy(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    baby = await engine.add_baby(
        client, "Second Baby", birth=BIRTH, boy=True, photo=PHOTO, landscape_photo=LANDSCAPE
    )

    name = baby.picture_name
    assert fake_server.pictures == {f"{name}.jpg": PHOTO, f"{name}_l.jpg": LANDSCAPE}


async def test_the_new_babys_records_can_be_logged_and_embed_it(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    baby = await engine.add_baby(client, "Second Baby", birth=BIRTH, boy=True)

    diaper = await engine.add_diaper(client, baby.object_id, DIAPER_WET)

    assert diaper.baby_id == baby.object_id
    assert posted(fake_server)[-1]["baby"]["name"] == "Second Baby"
    other = SyncEngine()
    await other.sync(client)
    assert other.babies()[baby.object_id].name == "Second Baby"


@pytest.mark.parametrize("name", ["test baby", "  TEST BABY "])
async def test_a_name_another_baby_has_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, name: str
) -> None:
    seed(fake_server)  # a baby named "Test Baby"
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(SelectionExistsError) as raised:
        await engine.add_baby(client, name, birth=BIRTH, boy=True, photo=PHOTO)
    assert (raised.value.what, raised.value.object_id) == ("baby", BABY_ID)
    assert len(fake_server.requests) == before


GOOD: dict[str, Any] = {"name": "Second Baby", "birth": BIRTH, "boy": True}


@pytest.mark.parametrize(
    "bad",
    [
        {"name": ""},
        {"name": "   "},
        {"name": 7},
        {"name": "x" * (MAX_BABY_NAME_LENGTH + 1)},
        {"birth": datetime(2026, 10, 27, 16, 17)},  # naive
        {"birth": "2026-10-27"},
        {"birth": None},
        {"boy": "yes"},
        {"boy": 1},
        {"due": "2026-11-02"},
        {"photo": PNG},
        {"photo": "photo.jpg"},
        {"landscape_photo": LANDSCAPE},  # no photo to go with it
    ],
)
async def test_bad_input_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, bad: dict[str, Any]
) -> None:
    seed(fake_server)
    engine = await synced(client)
    before = len(fake_server.requests)
    args = {**GOOD, **bad}
    name = args.pop("name")

    with pytest.raises(ValueError):
        await engine.add_baby(client, name, **args)
    assert len(fake_server.requests) == before


async def test_a_retry_of_an_add_that_landed_returns_it_and_uploads_nothing_again(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    object_id = "BA000000-0000-4000-8000-0000000000AA"

    first = await engine.add_baby(
        client, "Second Baby", birth=BIRTH, boy=True, photo=PHOTO, object_id=object_id
    )
    again = await engine.add_baby(
        client, "Second Baby", birth=BIRTH, boy=True, photo=PHOTO, object_id=object_id.lower()
    )

    assert again == first
    assert len(posted(fake_server)) == 1
    assert len(fake_server.uploads) == 2


async def test_a_failed_upload_posts_no_baby(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    fake_server.upload_status = 500

    with pytest.raises(BabyTrackerConnectionError):
        await engine.add_baby(client, "Second Baby", birth=BIRTH, boy=True, photo=PHOTO)
    assert posted(fake_server) == []
    assert list(engine.babies()) == [BABY_ID]


async def test_a_baby_after_the_group_is_reset_posts_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    fake_server.account_id = 777  # the group was reset in the app
    fake_server.sessions.clear()

    with pytest.raises(AccountChangedError):
        await engine.add_baby(client, "Second Baby", birth=BIRTH, boy=True)
    assert posted(fake_server) == []
