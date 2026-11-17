"""Renaming a pick-list entry, and deleting one another device added (invented data only)."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import PROBE_UUID
from factories import BABY_ID, payload_for
from fake_server import FakeSyncServer
from selection_helpers import PHONE, posted, rid, seed, synced

from pybabytracker import (
    OP_DELETE,
    OP_UPDATE,
    BabyTrackerClient,
    EntryInUseError,
    MedicationSelection,
    OtherFeedSelection,
    RecordNotFoundError,
    SelectionExistsError,
    WrongRecordTypeError,
)

# A food and a medicine as an iPhone synced them on their own (captured 2026-02 and 2026-11-11),
# with invented values.
FOOD: dict[str, Any] = {
    "bottle": "false",
    "newFlage": "true",
    "objectID": "35000000-0000-4000-8000-0000000000F1",
    "timestamp": "2026-02-25 16:33:51 +0000",
    "BCObjectType": "OtherFeedSelection",
    "name": "Oatmeal",
}
OTHER_FOOD = {**FOOD, "objectID": "49000000-0000-4000-8000-0000000000F2", "name": "Grain"}
MEDICINE: dict[str, Any] = {
    "name": "Nose Drops",
    "BCObjectType": "MedicationSelection",
    "amountPerTime": 2,
    "unit": "Drops",
    "isPresciption": "false",
    "interval": 240,
    "newFlage": "true",
    "timestamp": "2026-11-11 21:24:17 +0000",
    "desc": "For a stuffy nose",
    "objectID": "C7000000-0000-4000-8000-0000000000C1",
}


def solid_of(food: dict[str, Any], n: int) -> dict[str, Any]:
    payload = payload_for("OtherFeed", rid(n))
    payload["feedType"] = {**food, "newFlage": "false", "timestamp": "1970-01-01 00:00:00 +0000"}
    return payload


def opcodes(server: FakeSyncServer) -> list[int]:
    return [int(e["OPCode"]) for e in server.posted(PROBE_UUID)]


async def test_renaming_an_entry_changes_only_its_name_and_the_envelope(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, FOOD)
    engine = await synced(client)

    renamed = await engine.rename_entry(client, FOOD["objectID"], "  Porridge ")

    assert opcodes(fake_server) == [OP_UPDATE]
    [payload] = posted(fake_server)
    assert list(payload) == list(FOOD)
    assert {k for k in payload if payload[k] != FOOD[k]} == {"name", "newFlage", "timestamp"}
    assert (payload["name"], payload["newFlage"]) == ("Porridge", "false")
    assert payload["timestamp"] > FOOD["timestamp"]
    assert isinstance(renamed, OtherFeedSelection)
    assert engine.find_food("porridge").object_id == FOOD["objectID"]


async def test_a_medicine_keeps_its_other_fields_when_renamed(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, MEDICINE)
    engine = await synced(client)

    renamed = await engine.rename_entry(client, MEDICINE["objectID"].lower(), "Saline")

    [payload] = posted(fake_server)
    assert {k for k in payload if payload[k] != MEDICINE[k]} == {"name", "newFlage", "timestamp"}
    assert isinstance(renamed, MedicationSelection)
    dose = await engine.add_medication(client, BABY_ID, "saline")
    assert dose.medicine_name == "Saline"


async def test_an_entry_this_device_added_can_be_renamed(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    kind = await engine.add_activity_type(client, "Bath")

    await engine.rename_entry(client, kind.object_id, "Bath time")

    assert opcodes(fake_server) == [0, OP_UPDATE]
    assert [c.name for c in engine.activity_choices()][-1] == "Bath time"


async def test_a_name_another_entry_of_the_list_has_is_refused_and_its_own_name_is_a_no_op(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, FOOD, OTHER_FOOD)
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(SelectionExistsError) as raised:
        await engine.rename_entry(client, FOOD["objectID"], " grain ")
    assert (raised.value.what, raised.value.object_id) == ("food", OTHER_FOOD["objectID"])
    with pytest.raises(SelectionExistsError):
        await engine.rename_entry(client, FOOD["objectID"], "Fruit")  # a built-in food
    same = await engine.rename_entry(client, FOOD["objectID"], "Oatmeal")
    assert same.object_id == FOOD["objectID"]
    assert len(fake_server.requests) == before


@pytest.mark.parametrize("name", ["", "   ", 7, None, "x" * 51])
async def test_a_bad_name_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, name: Any
) -> None:
    seed(fake_server, FOOD)
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(ValueError):
        await engine.rename_entry(client, FOOD["objectID"], name)
    assert len(fake_server.requests) == before


async def test_what_has_no_record_of_its_own_or_isnt_an_entry_cant_be_renamed(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    embedded_only = {"objectID": "T-9", "name": "Swim", "newFlage": "false"}
    activity = payload_for("OtherActivity", rid(5))
    activity.update(desc=embedded_only, duration=0)
    seed(fake_server, activity)
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(RecordNotFoundError):  # known only from the copy an activity embeds
        await engine.rename_entry(client, "T-9", "Swimming")
    with pytest.raises(RecordNotFoundError):  # built in
        await engine.rename_entry(
            client, "EasyLogSeed3559dca0-8cbf-11e2-9e96-0800200c9a66", "Belly Time"
        )
    with pytest.raises(WrongRecordTypeError):  # a diaper
        await engine.rename_entry(client, rid(1), "Nappy")
    with pytest.raises(WrongRecordTypeError):
        await engine.rename_entry(client, BABY_ID, "Renamed")
    assert len(fake_server.requests) == before


async def test_another_devices_unused_entry_is_deleted_as_saved(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, FOOD)
    engine = await synced(client)
    assert engine.entry_users(FOOD["objectID"]) == 0

    await engine.delete_record(client, FOOD["objectID"], any_device=True)

    assert opcodes(fake_server) == [OP_DELETE]
    [payload] = posted(fake_server)
    assert payload == {**FOOD, "newFlage": "false"}
    assert "Oatmeal" not in [c.name for c in engine.food_choices()]


async def test_another_devices_entry_still_in_use_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, FOOD, solid_of(FOOD, 3), solid_of(FOOD, 4))
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(EntryInUseError) as raised:
        await engine.delete_record(client, FOOD["objectID"].lower(), any_device=True)
    assert (raised.value.what, raised.value.object_id, raised.value.users) == (
        "food",
        FOOD["objectID"],
        2,
    )
    assert len(fake_server.requests) == before
    assert engine.entry_users(FOOD["objectID"].lower()) == 2


async def test_an_entry_another_device_used_since_the_last_sync_is_not_deleted(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, FOOD)
    engine = await synced(client)
    fake_server.add_transaction(PHONE, 0, solid_of(FOOD, 3))  # not yet synced here

    with pytest.raises(EntryInUseError):
        await engine.delete_record(client, FOOD["objectID"], any_device=True)
    assert posted(fake_server) == []


async def test_this_devices_own_entry_is_deleted_as_before_in_use_or_not(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    food = await engine.add_food(client, "Avocado")
    await engine.add_other_feed(client, BABY_ID, "avocado", amount=1, unit="Oz")
    assert engine.entry_users(food.object_id) == 1

    await engine.delete_record(client, food.object_id)

    assert opcodes(fake_server) == [0, 0, OP_DELETE]


async def test_a_record_that_names_an_entry_only_by_its_plain_id_counts_as_a_user(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    # As a seed row comes out when the seed has no food to embed: no feedType, only typeID.
    by_id = payload_for("OtherFeed", rid(6))
    by_id.pop("feedType", None)
    by_id["typeID"] = FOOD["objectID"].lower()
    dose = payload_for("Medication", rid(7))
    dose.pop("medicationSelection", None)
    dose["medID"] = MEDICINE["objectID"]
    seed(fake_server, FOOD, MEDICINE, solid_of(FOOD, 3), by_id, dose)
    engine = await synced(client)
    before = len(fake_server.requests)

    assert engine.entry_users(FOOD["objectID"]) == 2
    assert engine.entry_users(MEDICINE["objectID"]) == 1
    with pytest.raises(EntryInUseError) as raised:
        await engine.delete_record(client, MEDICINE["objectID"], any_device=True)
    assert raised.value.users == 1
    assert len(fake_server.requests) == before
