"""Editing a baby, including its photo (invented data only)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import PROBE_UUID
from factories import BABY_ID, baby_payload, payload_for
from fake_server import FakeSyncServer
from selection_helpers import PHONE, posted, rid, seed, synced

from pybabytracker import (
    DIAPER_WET,
    MAX_BABY_NAME_LENGTH,
    OP_DELETE,
    OP_UPDATE,
    AccountChangedError,
    Baby,
    BabyTrackerClient,
    BabyTrackerConnectionError,
    RecordNotFoundError,
    SyncEngine,
    UnknownBabyError,
)

PHOTO = b"\xff\xd8\xff\xe0invented-photo\xff\xd9"
LANDSCAPE = b"\xff\xd8\xff\xe0invented-landscape\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n"
OLD_PICTURE = "B7000000-0000-4000-8000-0000000000B1"
# The keys of a Baby update an iPhone posted (captured 2026-11-12), in its order.
IPHONE_KEYS = [
    "dueDay",
    "BCObjectType",
    "gender",
    "pictureName",
    "dob",
    "newFlage",
    "timestamp",
    "name",
    "objectID",
]


def iphone_baby(**overrides: Any) -> dict[str, Any]:
    """A baby's own record as an iPhone synced it."""
    payload: dict[str, Any] = {
        "dueDay": "2026-11-02 05:39:38 +0000",
        "BCObjectType": "Baby",
        "gender": "true",
        "pictureName": OLD_PICTURE,
        "dob": "2026-10-27 16:17:31 +0000",
        "newFlage": "false",
        "timestamp": "2026-11-09 20:40:48 +0000",
        "name": "Test Baby",
        "objectID": BABY_ID,
    }
    payload.update(overrides)
    return payload


def opcodes(server: FakeSyncServer) -> list[int]:
    return [int(e["OPCode"]) for e in server.posted(PROBE_UUID)]


def changed(payload: dict[str, Any], original: dict[str, Any]) -> set[str]:
    return {k for k in set(payload) | set(original) if payload.get(k) != original.get(k)}


async def test_a_new_photo_changes_only_what_the_iphone_app_changed(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = iphone_baby()
    seed(fake_server, original)
    engine = await synced(client)

    updated = await engine.edit_baby(client, BABY_ID, photo=PHOTO)

    assert opcodes(fake_server) == [OP_UPDATE]
    [payload] = posted(fake_server)
    # As an iPhone's change of a baby's photo (captured 2026-11-12): the same keys in the same
    # order, with only pictureName and timestamp changed.
    assert list(payload) == IPHONE_KEYS
    assert changed(payload, original) == {"pictureName", "timestamp"}
    name = payload["pictureName"]
    assert name != OLD_PICTURE and name == name.upper() and len(name) == 36
    # The iPhone app uploads the photo twice, the second as its landscape copy.
    assert fake_server.pictures == {f"{name}.jpg": PHOTO, f"{name}_l.jpg": PHOTO}
    uploads = [i for i, r in enumerate(fake_server.requests) if r.path == "/account/picture"]
    post = [
        i
        for i, r in enumerate(fake_server.requests)
        if (r.method, r.path) == ("POST", "/account/transaction")
    ]
    assert len(uploads) == 2 and max(uploads) < min(post)
    assert isinstance(updated, Baby)
    assert (updated.picture_file, updated.landscape_picture_file) == (
        f"{name}.jpg",
        f"{name}_l.jpg",
    )
    assert await client.get_picture(updated.picture_file or "") == PHOTO


async def test_a_landscape_photo_of_its_own_is_uploaded_as_the_landscape_copy(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_baby())
    engine = await synced(client)

    updated = await engine.edit_baby(client, BABY_ID, photo=PHOTO, landscape_photo=LANDSCAPE)

    name = updated.picture_name
    assert fake_server.pictures == {f"{name}.jpg": PHOTO, f"{name}_l.jpg": LANDSCAPE}


async def test_a_name_dates_and_sex_change_in_one_update(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = iphone_baby()
    seed(fake_server, original)
    engine = await synced(client)

    updated = await engine.edit_baby(
        client,
        BABY_ID,
        name="  Renamed Baby ",
        birth=datetime(2026, 10, 27, 16, 0, tzinfo=UTC),
        due=datetime(2026, 11, 1, 6, 0, tzinfo=UTC),
        boy=False,
    )

    [payload] = posted(fake_server)
    assert list(payload) == IPHONE_KEYS
    assert changed(payload, original) == {"name", "dob", "dueDay", "gender", "timestamp"}
    assert (payload["name"], payload["dob"], payload["dueDay"], payload["gender"]) == (
        "Renamed Baby",
        "2026-10-27 16:00:00 +0000",
        "2026-11-01 06:00:00 +0000",
        "false",
    )
    assert payload["newFlage"] == "false"
    assert updated.name == "Renamed Baby"
    assert fake_server.uploads == []


async def test_a_baby_whose_sex_is_a_json_bool_keeps_that_type(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_baby(gender=False, newFlage=False))
    engine = await synced(client)

    await engine.edit_baby(client, BABY_ID, boy=True)

    [payload] = posted(fake_server)
    assert payload["gender"] is True
    assert payload["newFlage"] is False


async def test_records_added_afterwards_embed_the_edited_baby(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_baby())
    engine = await synced(client)
    await engine.edit_baby(client, BABY_ID, name="Renamed Baby")

    await engine.add_diaper(client, BABY_ID, DIAPER_WET)

    assert posted(fake_server)[-1]["baby"]["name"] == "Renamed Baby"
    assert engine.babies()[BABY_ID].name == "Renamed Baby"


async def test_a_fresh_engine_reading_the_logs_sees_the_edit(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_baby())
    engine = await synced(client)
    await engine.edit_baby(client, BABY_ID, name="Renamed Baby")

    other = SyncEngine()
    await other.sync(client)

    assert other.babies()[BABY_ID].name == "Renamed Baby"


async def test_an_edit_that_changes_nothing_posts_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_baby())
    engine = await synced(client)
    first = await engine.edit_baby(client, BABY_ID, name="Renamed Baby", boy=False)

    again = await engine.edit_baby(client, BABY_ID, name="Renamed Baby", boy=False)

    assert again == first
    assert len(posted(fake_server)) == 1  # the logs are read again, and nothing is posted


async def test_the_edit_carries_what_another_device_saved_since_the_last_sync(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_baby())
    engine = await synced(client)
    newer = iphone_baby(name="Renamed On Phone", timestamp="2026-11-09 21:00:00 +0000")
    fake_server.add_transaction(PHONE, OP_UPDATE, newer)  # not yet synced here

    await engine.edit_baby(client, BABY_ID, boy=False)

    [payload] = posted(fake_server)
    assert (payload["name"], payload["gender"]) == ("Renamed On Phone", "false")


async def test_a_stored_timestamp_ahead_of_this_clock_is_passed_by_a_second(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_baby(timestamp="2099-01-01 00:00:00 +0000"))
    engine = await synced(client)

    await engine.edit_baby(client, BABY_ID, name="Renamed Baby")

    [payload] = posted(fake_server)
    assert payload["timestamp"] == "2099-01-01 00:00:01 +0000"


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"name": ""},
        {"name": "   "},
        {"name": 7},
        {"name": "x" * (MAX_BABY_NAME_LENGTH + 1)},
        {"birth": datetime(2026, 10, 27, 16, 0)},  # naive
        {"due": "2026-11-01"},
        {"boy": "yes"},
        {"boy": 1},
        {"photo": PNG},
        {"photo": b""},
        {"photo": "photo.jpg"},
        {"photo": PHOTO, "landscape_photo": PNG},
        {"landscape_photo": LANDSCAPE},  # no photo to go with it
    ],
)
async def test_nothing_to_change_or_a_bad_value_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, bad: dict[str, Any]
) -> None:
    seed(fake_server, iphone_baby())
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(ValueError):
        await engine.edit_baby(client, BABY_ID, **bad)
    assert len(fake_server.requests) == before


async def test_a_baby_never_seen_or_deleted_or_not_a_baby_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    gone = baby_payload("Gone Baby", rid(50))
    seed(fake_server, iphone_baby(), gone, payload_for("Diaper", rid(7)))
    fake_server.add_transaction(PHONE, OP_DELETE, gone)
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(UnknownBabyError):
        await engine.edit_baby(client, "NO-SUCH-BABY", name="Renamed Baby")
    with pytest.raises(UnknownBabyError):
        await engine.edit_baby(client, rid(7), name="Renamed Baby")  # a diaper
    with pytest.raises(RecordNotFoundError):
        await engine.edit_baby(client, rid(50), name="Renamed Baby")
    assert len(fake_server.requests) == before


async def test_a_baby_known_only_from_copies_in_its_records_is_refused(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)  # a diaper that embeds the baby, and no Baby record of its own
    engine = await synced(client)
    assert BABY_ID in engine.babies()
    before = len(fake_server.requests)

    with pytest.raises(RecordNotFoundError):
        await engine.edit_baby(client, BABY_ID, name="Renamed Baby")
    assert len(fake_server.requests) == before


async def test_a_failed_upload_posts_no_update(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_baby())
    engine = await synced(client)
    fake_server.upload_status = 500

    with pytest.raises(BabyTrackerConnectionError):
        await engine.edit_baby(client, BABY_ID, photo=PHOTO)
    assert posted(fake_server) == []
    assert not engine.to_dict()["pending"]
    assert engine.babies()[BABY_ID].picture_name == OLD_PICTURE


async def test_an_edit_after_the_group_is_reset_posts_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_baby())
    engine = await synced(client)
    fake_server.account_id = 777  # the group was reset in the app
    fake_server.sessions.clear()

    with pytest.raises(AccountChangedError):
        await engine.edit_baby(client, BABY_ID, photo=PHOTO)
    assert fake_server.uploads == []
    assert posted(fake_server) == []
