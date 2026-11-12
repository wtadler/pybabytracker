"""Finding the pick-list entry a record names (invented data only)."""

from __future__ import annotations

from typing import Any

from factories import payload_for
from fake_server import FakeSyncServer
from selection_helpers import rid, seed, synced

from pybabytracker import BabyTrackerClient, SyncEngine

PUREE: dict[str, Any] = {
    "bottle": False,
    "name": "Puree",
    "newFlage": False,
    "objectID": "38000000-0000-4000-8000-0000000000f0",
    "timestamp": "2026-02-25 23:40:08 +0000",
}
FOOD: dict[str, Any] = {
    "bottle": "false",
    "newFlage": "true",
    "objectID": "35000000-0000-4000-8000-0000000000F1",
    "timestamp": "2026-02-25 16:33:51 +0000",
    "BCObjectType": "OtherFeedSelection",
    "name": "Oatmeal",
}


def solid(
    n: int, *, embedded: dict[str, Any] | None = None, type_id: str | None = None
) -> dict[str, Any]:
    payload = payload_for("OtherFeed", rid(n))
    payload.pop("typeID", None)
    payload.pop("feedType", None)
    if embedded is not None:
        payload["feedType"] = dict(embedded)
    if type_id is not None:
        payload["typeID"] = type_id
    return payload


def name_of(engine: SyncEngine, object_id: str) -> str | None:
    entry = engine.entry_of(engine.records[object_id])
    return entry.name if entry is not None else None


async def test_a_seed_row_with_only_an_id_finds_the_food_another_record_embeds(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    # As on a real account: the seed has solid foods whose food it doesn't hold, so they carry
    # only typeID, while a synced record embeds the same food.
    seed(
        fake_server,
        solid(3, type_id=PUREE["objectID"]),
        solid(4, type_id=PUREE["objectID"].upper()),
        solid(5, embedded=PUREE),
    )
    engine = await synced(client)

    assert engine.records[rid(3)].food_name is None  # type: ignore[attr-defined]
    assert [name_of(engine, rid(n)) for n in (3, 4, 5)] == ["Puree", "Puree", "Puree"]
    assert engine.entry_users(PUREE["objectID"]) == 3


async def test_the_entry_has_its_current_name_after_a_rename(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    embedded = {**FOOD, "newFlage": "false", "timestamp": "1970-01-01 00:00:00 +0000"}
    seed(fake_server, FOOD, solid(3, embedded=embedded))
    engine = await synced(client)
    await engine.rename_entry(client, FOOD["objectID"], "Porridge")

    assert engine.records[rid(3)].food_name == "Oatmeal"  # type: ignore[attr-defined]
    assert name_of(engine, rid(3)) == "Porridge"


async def test_every_kind_of_record_that_names_an_entry_finds_it(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    dose = payload_for("Medication", rid(3))
    dose["medicationSelection"] = {"objectID": "M-1", "name": "Test Drops", "unit": "ml"}
    activity = payload_for("OtherActivity", rid(4))
    activity["desc"] = {
        "objectID": "EasyLogSeed3559dca0-8cbf-11e2-9e96-0800200c9a66",
        "name": "Tummy Time",
    }
    milestone = payload_for("Milestone", rid(5))
    milestone["milestoneSelectionID"] = "EasyLogSeed56cc56b0-894f-11e2-9e96-0800200c9a66"
    milestone.pop("milestoneType", None)
    vaccine = payload_for("Vaccine", rid(6))
    vaccine["vaccID"] = "EasyLogSeed001ddeae8f0-8805-11e2-9e96-0800200c9a66"
    vaccine.pop("vaccineType", None)
    seed(fake_server, dose, activity, milestone, vaccine)
    engine = await synced(client)

    assert [name_of(engine, rid(n)) for n in (3, 4, 5, 6)] == [
        "Test Drops",
        "Tummy Time",
        "First smile",
        "HepB",
    ]


async def test_a_record_that_names_no_entry_or_an_unknown_one_has_none(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, solid(3, type_id="NO-SUCH-FOOD"), solid(4))
    engine = await synced(client)

    assert engine.entry_of(engine.records[rid(1)]) is None  # a diaper
    assert engine.entry_of(engine.records[rid(3)]) is None
    assert engine.entry_of(engine.records[rid(4)]) is None


async def test_an_embedded_copy_no_list_knows_is_returned_as_it_is(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    # The copy is in the choices by way of the record itself, so this is the same answer.
    seed(fake_server, solid(3, embedded=PUREE))
    engine = await synced(client)

    entry = engine.entry_of(engine.records[rid(3)])
    assert entry is not None
    assert (entry.object_id, entry.name, entry.built_in) == (PUREE["objectID"], "Puree", False)
