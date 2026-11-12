"""Adding an activity type to the pick list (invented data only)."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import PROBE_UUID
from factories import BABY_ID, payload_for
from fake_server import FakeSyncServer
from selection_helpers import posted, rid, seed, synced

from pybabytracker import (
    MAX_ACTIVITY_TYPE_NAME_LENGTH,
    OP_DELETE,
    AccountChangedError,
    BabyTrackerClient,
    OtherActivityDescription,
    RecordNotFoundError,
    SelectionExistsError,
    WrongRecordTypeError,
)

SEED_TIME = "1970-01-01 00:00:00 +0000"
# The keys an iPhone's new food has, with a food's own ``bottle`` left out. They are also the
# keys of the copy an iPhone embeds in an activity of a custom type.
TYPE_KEYS = {"BCObjectType", "objectID", "name", "newFlage", "timestamp"}
# A custom type as an iPhone embeds it in an activity (seen 2026-11-11), with invented values.
IPHONE_EMBEDDED = {
    "BCObjectType": "OtherActivityDescription",
    "name": "Brush Teeth",
    "newFlage": "false",
    "timestamp": SEED_TIME,
    "objectID": "50000000-0000-4000-8000-0000000000b1",
}
# The same type as an Android phone embeds it: no BCObjectType, a JSON bool, its own timestamp.
ANDROID_EMBEDDED = {
    "name": "Upside Down Time",
    "newFlage": False,
    "objectID": "3E000000-0000-4000-8000-0000000000A2",
    "timestamp": "2025-03-25 03:48:13 +0000",
}


def activity_of(n: int, desc: dict[str, Any]) -> dict[str, Any]:
    payload = payload_for("OtherActivity", rid(n))
    payload.update(desc=dict(desc), duration=0)
    return payload


async def test_a_new_type_is_posted_with_only_its_own_keys(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    kind = await engine.add_activity_type(client, "  Bath ")

    assert isinstance(kind, OtherActivityDescription)
    [payload] = posted(fake_server)
    assert set(payload) == TYPE_KEYS
    assert (payload["BCObjectType"], payload["name"], payload["newFlage"]) == (
        "OtherActivityDescription",
        "Bath",
        "true",
    )
    assert payload["objectID"] == kind.object_id
    assert payload["timestamp"] != SEED_TIME


async def test_a_new_type_is_offered_and_an_activity_embeds_it_as_the_iphone_does(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    kind = await engine.add_activity_type(client, "Bath")
    assert engine.find_activity("bath").object_id == kind.object_id
    record = await engine.add_activity(client, BABY_ID, kind.object_id, duration=10)

    assert record.activity_name == "Bath"
    _, activity = posted(fake_server)
    # Exactly the keys and values of an iPhone's embedded custom type.
    assert activity["desc"] == {
        "BCObjectType": "OtherActivityDescription",
        "objectID": kind.object_id,
        "name": "Bath",
        "newFlage": "false",
        "timestamp": SEED_TIME,
    }


async def test_an_iphones_embedded_type_is_embedded_unchanged(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, activity_of(2, IPHONE_EMBEDDED))
    engine = await synced(client)

    await engine.add_activity(client, BABY_ID, "brush teeth")

    [payload] = posted(fake_server)
    assert payload["desc"] == IPHONE_EMBEDDED


async def test_an_android_phones_embedded_type_is_embedded_as_it_synced(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, activity_of(2, ANDROID_EMBEDDED))
    engine = await synced(client)

    await engine.add_activity(client, BABY_ID, "upside down time")

    [payload] = posted(fake_server)
    assert payload["desc"] == ANDROID_EMBEDDED


@pytest.mark.parametrize(
    ("name", "existing"),
    [
        ("tummy time", "EasyLogSeed3559dca0-8cbf-11e2-9e96-0800200c9a66"),  # built in
        ("  BRUSH TEETH ", IPHONE_EMBEDDED["objectID"]),  # known from an activity
    ],
)
async def test_a_name_already_on_the_list_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, name: str, existing: str
) -> None:
    seed(fake_server, activity_of(2, IPHONE_EMBEDDED))
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(SelectionExistsError) as raised:
        await engine.add_activity_type(client, name)
    assert (raised.value.what, raised.value.object_id) == ("activity", existing)
    assert len(fake_server.requests) == before


@pytest.mark.parametrize("name", ["", "   ", 7, None, "x" * (MAX_ACTIVITY_TYPE_NAME_LENGTH + 1)])
async def test_a_bad_name_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, name: Any
) -> None:
    seed(fake_server)
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(ValueError):
        await engine.add_activity_type(client, name)
    assert len(fake_server.requests) == before


async def test_the_longest_name_is_allowed_and_a_retry_posts_nothing_more(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    object_id = "B0000000-0000-4000-8000-0000000000AA"
    name = "x" * MAX_ACTIVITY_TYPE_NAME_LENGTH

    first = await engine.add_activity_type(client, name, object_id=object_id)
    again = await engine.add_activity_type(client, name, object_id=object_id.lower())

    assert again == first
    assert len(posted(fake_server)) == 1


async def test_its_own_type_can_be_deleted_and_another_devices_cant(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    theirs = {**IPHONE_EMBEDDED, "newFlage": "true", "timestamp": "2026-11-11 20:36:00 +0000"}
    seed(fake_server, theirs)
    engine = await synced(client)
    assert isinstance(engine.records[theirs["objectID"]], OtherActivityDescription)
    kind = await engine.add_activity_type(client, "Bath")

    with pytest.raises(WrongRecordTypeError):
        await engine.delete_record(client, theirs["objectID"], any_device=True)
    with pytest.raises(WrongRecordTypeError):
        await engine.edit_record(client, theirs["objectID"], note="Edited")
    await engine.delete_record(client, kind.object_id)

    assert [int(e["OPCode"]) for e in fake_server.posted(PROBE_UUID)] == [0, OP_DELETE]
    with pytest.raises(RecordNotFoundError):
        await engine.delete_record(client, kind.object_id)


async def test_a_type_after_the_group_is_reset_posts_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    fake_server.account_id = 777  # the group was reset in the app
    fake_server.sessions.clear()

    with pytest.raises(AccountChangedError):
        await engine.add_activity_type(client, "Bath")
    assert posted(fake_server) == []
