"""The apps' repair operations, Relive and Conflict (invented data only)."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from factories import BABY_ID, baby_payload, payload_for
from fake_server import FakeSyncServer
from selection_helpers import PHONE, posted, rid, seed, synced

from pybabytracker import (
    OP_ADD,
    OP_CONFLICT,
    OP_DELETE,
    OP_RELIVE,
    AmbiguousSelectionError,
    BabyTrackerClient,
)

TABLET = "BBBB2222-0000-4000-8000-00000000000B"
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


def test_the_opcodes_are_the_android_apps() -> None:
    assert (OP_RELIVE, OP_CONFLICT) == (3, 4)


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


async def test_the_sync_number_moves_past_a_repair_operation(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, FOOD)
    fake_server.add_transaction(PHONE, OP_RELIVE, FOOD)
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Diaper", rid(8)))

    engine = await synced(client)

    assert rid(8) in engine.records
    assert engine.last_seen[PHONE] == 4


async def test_a_conflict_is_not_merged_and_the_shared_name_names_both(
    client: BabyTrackerClient, fake_server: FakeSyncServer, caplog: pytest.LogCaptureFixture
) -> None:
    conflict = {
        "BCObjectType": "ConflictRecord",
        "objectID": "CF000000-0000-4000-8000-0000000000C1",
        "timestamp": "2026-11-12 10:00:00 +0000",
        "newFlage": True,
        "winner": FOOD,
        "loser": TWIN,
    }
    seed(fake_server, FOOD, TWIN)
    fake_server.add_transaction(PHONE, OP_CONFLICT, conflict)

    with caplog.at_level(logging.DEBUG, logger="pybabytracker"):
        engine = await synced(client)

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert conflict["objectID"] not in engine.records
    with pytest.raises(AmbiguousSelectionError) as raised:
        engine.find_food("Oatmeal")
    assert set(raised.value.object_ids) == {FOOD["objectID"], TWIN["objectID"]}
    assert engine.find_food(FOOD["objectID"]).name == "Oatmeal"
