"""The apps' repair operations, Relive and Conflict (invented data only)."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from factories import BABY_ID, baby_payload, payload_for
from fake_server import FakeSyncServer
from selection_helpers import PHONE, posted, rid, seed, synced

from pybabytracker import (
    DIAPER_WET,
    OP_ADD,
    OP_CONFLICT,
    OP_DELETE,
    OP_RELIVE,
    OP_UPDATE,
    BabyTrackerClient,
    OtherFeed,
    SyncEngine,
)

TABLET = "BBBB2222-0000-4000-8000-00000000000B"
BABY_TWO = "22222222-BBBB-4BBB-8BBB-000000000002"
FOOD: dict[str, Any] = {
    "bottle": "false",
    "newFlage": "true",
    "objectID": "35000000-0000-4000-8000-0000000000F1",
    "timestamp": "2026-02-25 16:33:51 +0000",
    "BCObjectType": "OtherFeedSelection",
    "name": "Oatmeal",
}
TWIN: dict[str, Any] = {**FOOD, "objectID": "49000000-0000-4000-8000-0000000000F2"}


def solid_of(food: dict[str, Any], n: int) -> dict[str, Any]:
    payload = payload_for("OtherFeed", rid(n))
    payload["feedType"] = {**food, "newFlage": "false", "timestamp": "1970-01-01 00:00:00 +0000"}
    return payload


def conflict(winner: Any, loser: Any) -> dict[str, Any]:
    """A ConflictRecord with the Android class's fields (not a capture)."""
    return {
        "BCObjectType": "ConflictRecord",
        "objectID": "CF000000-0000-4000-8000-0000000000C1",
        "timestamp": "2026-11-12 10:00:00 +0000",
        "newFlage": True,
        "winner": winner,
        "loser": loser,
    }


def named_food(engine: SyncEngine, object_id: str) -> str:
    """The ID of the food a solid food record names."""
    solid = engine.records[object_id]
    assert isinstance(solid, OtherFeed)
    assert solid.feed_type is not None
    return str(solid.feed_type["objectID"])


def test_the_opcodes_are_the_android_apps() -> None:
    assert (OP_RELIVE, OP_CONFLICT) == (3, 4)


# --- Relive ------------------------------------------------------------------------------


async def test_a_food_whose_delete_a_phone_refused_stays_usable_with_or_without_the_relive(
    client: BabyTrackerClient, fake_server: FakeSyncServer, caplog: pytest.LogCaptureFixture
) -> None:
    # A food in use is deleted on the tablet; the phone, which has a solid food of it, refuses
    # the delete and posts a Relive of its copy.
    seed(fake_server, FOOD, solid_of(FOOD, 3))
    fake_server.add_device(TABLET)
    fake_server.add_transaction(TABLET, OP_DELETE, {**FOOD, "newFlage": "false"})
    fake_server.add_transaction(PHONE, OP_RELIVE, {**FOOD, "newFlage": "false"})

    with caplog.at_level(logging.DEBUG, logger="pybabytracker"):
        engine = await synced(client)

    # The Relive isn't applied and isn't an unknown opcode.
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("Relive" in r.getMessage() for r in caplog.records)
    # The food is still a choice, from the solid food that names it, and can be logged.
    assert engine.find_food("oatmeal").object_id == FOOD["objectID"]
    record = await engine.add_other_feed(client, BABY_ID, "Oatmeal", amount=1, unit="Oz")
    assert record.food_name == "Oatmeal"
    assert posted(fake_server)[-1]["feedType"]["objectID"] == FOOD["objectID"]


async def test_a_baby_whose_delete_a_phone_refused_stays_with_or_without_the_relive(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    baby = baby_payload()
    seed(fake_server, baby)  # the baby's own record, and a diaper of it
    fake_server.add_device(TABLET)
    fake_server.add_transaction(TABLET, OP_DELETE, baby)
    fake_server.add_transaction(PHONE, OP_RELIVE, baby)

    engine = await synced(client)

    assert BABY_ID in engine.babies()
    assert engine.babies()[BABY_ID].name == "Test Baby"


async def test_the_sync_number_moves_past_a_relive(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, FOOD)
    fake_server.add_transaction(PHONE, OP_RELIVE, FOOD)
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Diaper", rid(8)))

    engine = await synced(client)

    assert rid(8) in engine.records
    assert engine.last_seen[PHONE] == 4


# --- Conflict ----------------------------------------------------------------------------


async def test_a_conflict_folds_the_losing_food_into_the_winner(
    client: BabyTrackerClient, fake_server: FakeSyncServer, caplog: pytest.LogCaptureFixture
) -> None:
    seed(fake_server, FOOD, TWIN, solid_of(FOOD, 3), solid_of(TWIN, 4))
    fake_server.add_transaction(PHONE, OP_CONFLICT, conflict(FOOD, TWIN))

    with caplog.at_level(logging.DEBUG, logger="pybabytracker"):
        engine = await synced(client)

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    # One Oatmeal is left, the winner, and the name finds it.
    oatmeals = [c.object_id for c in engine.food_choices() if c.name == "Oatmeal"]
    assert oatmeals == [FOOD["objectID"]]
    assert engine.find_food("oatmeal").object_id == FOOD["objectID"]
    assert TWIN["objectID"] not in engine.records
    # The loser's solid food names the winner now; the winner's is untouched.
    assert named_food(engine, rid(4)) == FOOD["objectID"]
    assert engine.records[rid(3)].to_payload()["feedType"] == solid_of(FOOD, 3)["feedType"]
    assert engine.merged_entries() == {TWIN["objectID"].upper(): FOOD["objectID"]}


async def test_a_conflict_folds_the_losing_babys_records_into_the_winner(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    winner = baby_payload()
    loser = baby_payload(object_id=BABY_TWO)
    seed(fake_server, winner, loser, payload_for("Diaper", rid(5), baby=loser))
    fake_server.add_transaction(PHONE, OP_CONFLICT, conflict(winner, loser))

    engine = await synced(client)

    assert list(engine.babies()) == [BABY_ID]
    assert engine.records[rid(5)].baby_id == BABY_ID
    assert engine.latest_records(BABY_TWO) == {}
    # A new record for the winner embeds the winner.
    added = await engine.add_diaper(client, BABY_ID, DIAPER_WET)
    assert added.baby_id == BABY_ID


async def test_a_losers_record_that_arrives_after_the_conflict_is_folded_too(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, FOOD, TWIN)
    fake_server.add_transaction(PHONE, OP_CONFLICT, conflict(FOOD, TWIN))
    engine = await synced(client)
    # A device that hasn't applied the Conflict yet logs the loser, and then edits it.
    fake_server.add_device(TABLET)
    fake_server.add_transaction(TABLET, OP_ADD, solid_of(TWIN, 6))
    edited = {**TWIN, "name": "Porridge", "timestamp": "2026-11-13 09:00:00 +0000"}
    fake_server.add_transaction(TABLET, OP_UPDATE, edited)

    await engine.sync(client)

    assert named_food(engine, rid(6)) == FOOD["objectID"]
    assert TWIN["objectID"] not in engine.records  # the edit doesn't bring the loser back
    assert TWIN["objectID"] not in [c.object_id for c in engine.food_choices()]


async def test_the_fold_doesnt_depend_on_which_log_is_read_first(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    # The Conflict is in the phone's log, read first; the loser and its record are in the
    # tablet's, read second.
    seed(fake_server, FOOD)
    fake_server.add_transaction(PHONE, OP_CONFLICT, conflict(FOOD, TWIN))
    fake_server.add_device(TABLET)
    fake_server.add_transaction(TABLET, OP_ADD, TWIN)
    fake_server.add_transaction(TABLET, OP_ADD, solid_of(TWIN, 7))

    engine = await synced(client)

    assert engine.find_food("Oatmeal").object_id == FOOD["objectID"]
    assert named_food(engine, rid(7)) == FOOD["objectID"]
    assert TWIN["objectID"] not in engine.records


async def test_saved_state_keeps_folding_after_a_reload(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, FOOD, TWIN, solid_of(TWIN, 4))
    fake_server.add_transaction(PHONE, OP_CONFLICT, conflict(FOOD, TWIN))
    engine = await synced(client)

    reloaded = SyncEngine.from_dict(json.loads(json.dumps(engine.to_dict())))
    fake_server.add_transaction(PHONE, OP_ADD, solid_of(TWIN, 8))
    await reloaded.sync(client)

    assert reloaded.merged_entries() == engine.merged_entries()
    assert named_food(reloaded, rid(4)) == FOOD["objectID"]
    assert named_food(reloaded, rid(8)) == FOOD["objectID"]


async def test_state_saved_before_conflicts_were_folded_loads_with_none(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, FOOD)
    engine = await synced(client)
    state = engine.to_dict()
    del state["merged"]

    assert SyncEngine.from_dict(state).merged_entries() == {}


BAD_CONFLICTS: list[dict[str, Any]] = [
    conflict(FOOD, None),
    conflict("FOOD", TWIN),
    conflict(FOOD, baby_payload()),  # a food against a baby
    conflict(payload_for("Diaper", rid(9)), payload_for("Diaper", rid(10))),
    conflict(FOOD, {"BCObjectType": "OtherFeedSelection", "name": "No ID"}),
]


@pytest.mark.parametrize("bad", BAD_CONFLICTS)
async def test_a_conflict_that_isnt_two_entries_of_one_kind_is_skipped_with_a_warning(
    client: BabyTrackerClient,
    fake_server: FakeSyncServer,
    caplog: pytest.LogCaptureFixture,
    bad: dict[str, Any],
) -> None:
    seed(fake_server, FOOD, TWIN)
    fake_server.add_transaction(PHONE, OP_CONFLICT, bad)
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Diaper", rid(8)))

    with caplog.at_level(logging.WARNING, logger="pybabytracker"):
        engine = await synced(client)

    assert any("Conflict" in r.getMessage() for r in caplog.records)
    assert engine.merged_entries() == {}
    assert TWIN["objectID"] in engine.records
    assert rid(8) in engine.records  # the sync moved past it


async def test_a_conflict_of_one_entry_with_itself_just_stores_the_winner(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    newer = {**FOOD, "name": "Porridge", "timestamp": "2026-11-13 09:00:00 +0000"}
    seed(fake_server, FOOD)
    fake_server.add_transaction(PHONE, OP_CONFLICT, conflict(newer, FOOD))

    engine = await synced(client)

    assert engine.merged_entries() == {}
    assert engine.find_food("Porridge").object_id == FOOD["objectID"]


async def test_a_chain_of_conflicts_ends_at_the_last_winner(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    third = {**FOOD, "objectID": "5A000000-0000-4000-8000-0000000000F3"}
    seed(fake_server, FOOD, TWIN, third, solid_of(TWIN, 4))
    fake_server.add_transaction(PHONE, OP_CONFLICT, conflict(FOOD, TWIN))
    fake_server.add_transaction(PHONE, OP_CONFLICT, conflict(third, FOOD))

    engine = await synced(client)

    oatmeals = [c.object_id for c in engine.food_choices() if c.name == "Oatmeal"]
    assert oatmeals == [third["objectID"]]
    assert named_food(engine, rid(4)) == third["objectID"]


# --- Edge cases found by a review --------------------------------------------------------


async def test_a_relive_brings_back_an_entry_this_engine_had_deleted(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, FOOD, solid_of(FOOD, 3))
    fake_server.add_device(TABLET)
    engine = await synced(client)
    fake_server.add_transaction(TABLET, OP_DELETE, {**FOOD, "newFlage": "false"})
    await engine.sync(client)
    assert engine.is_deleted(FOOD["objectID"])

    # The phone, which has a solid food of it, refused the delete and posts its copy.
    fake_server.add_transaction(PHONE, OP_RELIVE, {**FOOD, "newFlage": "false"})
    await engine.sync(client)

    assert not engine.is_deleted(FOOD["objectID"])
    assert FOOD["objectID"] not in engine.deleted_ids()
    assert engine.records[FOOD["objectID"]].to_payload()["name"] == "Oatmeal"
    # And the state says the same after a reload.
    reloaded = SyncEngine.from_dict(json.loads(json.dumps(engine.to_dict())))
    assert not reloaded.is_deleted(FOOD["objectID"])
    assert FOOD["objectID"] in reloaded.records


async def test_a_relive_brings_back_a_baby_and_a_later_delete_still_applies(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    baby = baby_payload()
    seed(fake_server, baby)
    fake_server.add_device(TABLET)
    engine = await synced(client)
    fake_server.add_transaction(TABLET, OP_DELETE, baby)
    await engine.sync(client)
    assert engine.is_deleted(BABY_ID)

    fake_server.add_transaction(PHONE, OP_RELIVE, baby)
    await engine.sync(client)
    assert not engine.is_deleted(BABY_ID)
    assert engine.records[BABY_ID].to_payload()["name"] == "Test Baby"

    # A delete after the Relive is a new delete, and applies.
    fake_server.add_transaction(TABLET, OP_DELETE, baby)
    await engine.sync(client)
    assert engine.is_deleted(BABY_ID)


async def test_a_relive_of_an_entry_never_seen_stores_it(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    fake_server.add_transaction(PHONE, OP_RELIVE, FOOD)

    engine = await synced(client)

    assert engine.find_food("Oatmeal").object_id == FOOD["objectID"]
    assert FOOD["objectID"] in engine.records


async def test_a_relive_of_an_entry_still_here_changes_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, FOOD)
    engine = await synced(client)
    before = engine.records[FOOD["objectID"]]

    fake_server.add_transaction(PHONE, OP_RELIVE, {**FOOD, "name": "An Older Copy"})
    await engine.sync(client)

    assert engine.records[FOOD["objectID"]] == before


async def test_a_relive_of_anything_else_is_skipped_with_a_warning(
    client: BabyTrackerClient, fake_server: FakeSyncServer, caplog: pytest.LogCaptureFixture
) -> None:
    seed(fake_server)
    fake_server.add_transaction(PHONE, OP_RELIVE, payload_for("Diaper", rid(9)))

    with caplog.at_level(logging.WARNING, logger="pybabytracker"):
        engine = await synced(client)

    assert rid(9) not in engine.records
    assert any("Relive of a Diaper" in r.getMessage() for r in caplog.records)


async def test_two_conflicts_that_reverse_each_other_leave_one_entry(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, FOOD, TWIN, solid_of(FOOD, 3), solid_of(TWIN, 4))
    fake_server.add_device(TABLET)
    fake_server.add_transaction(PHONE, OP_CONFLICT, conflict(FOOD, TWIN))
    fake_server.add_transaction(TABLET, OP_CONFLICT, conflict(TWIN, FOOD))

    engine = await synced(client)

    # Whichever was read first stands: one Oatmeal is left, and both solid foods name it.
    [(loser, winner)] = engine.merged_entries().items()
    assert {loser, winner.upper()} == {FOOD["objectID"].upper(), TWIN["objectID"].upper()}
    assert winner in engine.records
    assert [c.object_id for c in engine.food_choices() if c.name == "Oatmeal"] == [winner]
    assert {named_food(engine, rid(3)), named_food(engine, rid(4))} == {winner}
    # A solid food of the loser that arrives later is folded the same way.
    late = solid_of(FOOD if winner == TWIN["objectID"] else TWIN, 5)
    fake_server.add_transaction(PHONE, OP_ADD, late)
    await engine.sync(client)
    assert named_food(engine, rid(5)) == winner


async def test_a_fold_repoints_a_record_that_names_the_loser_by_a_plain_id(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    # As seed rows whose entry the seed doesn't hold: only the entry's ID, in either case.
    plain = payload_for("OtherFeed", rid(4))
    plain.pop("feedType", None)
    plain["typeID"] = TWIN["objectID"].lower()
    dose = payload_for("Medication", rid(5))
    dose.pop("medicationSelection", None)
    dose["medID"] = "M-LOSER"
    winner_medicine = {
        "BCObjectType": "MedicationSelection",
        "objectID": "M-WINNER",
        "name": "Test Drops",
        "unit": "ml",
        "timestamp": "2026-02-25 16:33:51 +0000",
    }
    seed(fake_server, FOOD, TWIN, plain, dose, winner_medicine)
    fake_server.add_transaction(PHONE, OP_CONFLICT, conflict(FOOD, TWIN))
    fake_server.add_transaction(
        PHONE, OP_CONFLICT, conflict(winner_medicine, {**winner_medicine, "objectID": "M-LOSER"})
    )
    late = payload_for("OtherFeed", rid(6))
    late.pop("feedType", None)
    late["typeID"] = TWIN["objectID"]
    fake_server.add_transaction(PHONE, OP_ADD, late)

    engine = await synced(client)

    assert engine.records[rid(4)].to_payload()["typeID"] == FOOD["objectID"]
    assert engine.records[rid(6)].to_payload()["typeID"] == FOOD["objectID"]
    assert engine.records[rid(5)].to_payload()["medID"] == "M-WINNER"
