"""Adding a medicine to the pick list, and how a dose embeds one (invented data only)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from factories import BABY_ID
from fake_server import FakeSyncServer
from selection_helpers import posted, seed, synced

from pybabytracker import (
    MAX_MEDICINE_INTERVAL,
    MAX_MEDICINE_NAME_LENGTH,
    MAX_MEDICINE_UNIT_LENGTH,
    AccountChangedError,
    BabyTrackerClient,
    MedicationSelection,
    SelectionExistsError,
)

SEED_TIME = "1970-01-01 00:00:00 +0000"
# The keys the iPhone app sends for a new medicine, captured 2026-11-11: no record fields, and
# the app's own spelling of isPresciption.
MEDICINE_KEYS = {
    "BCObjectType",
    "objectID",
    "name",
    "desc",
    "unit",
    "amountPerTime",
    "interval",
    "isPresciption",
    "newFlage",
    "timestamp",
}
# A medicine as the iPhone synced one of its own, with invented values.
IPHONE_MEDICINE: dict[str, Any] = {
    "name": "Nose Drops",
    "BCObjectType": "MedicationSelection",
    "amountPerTime": 2,
    "unit": "Drops",
    "isPresciption": "false",
    "interval": 240,
    "newFlage": "true",
    "timestamp": "2026-11-11 21:24:17 +0000",
    "desc": "For a stuffy nose",
    "objectID": "C0000000-0000-4000-8000-0000000000C1",
}


async def test_a_new_medicine_is_posted_with_the_iphone_keys_and_minutes(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    medicine = await engine.add_medicine(
        client,
        " Nose Drops ",
        unit=" Drops ",
        amount_per_time=2,
        interval=timedelta(hours=4),
        description="For a stuffy nose ",
    )

    assert isinstance(medicine, MedicationSelection)
    [payload] = posted(fake_server)
    assert set(payload) == MEDICINE_KEYS
    assert {k: payload[k] for k in MEDICINE_KEYS - {"objectID", "timestamp"}} == {
        "BCObjectType": "MedicationSelection",
        "name": "Nose Drops",
        "desc": "For a stuffy nose",
        "unit": "Drops",
        "amountPerTime": 2,
        "interval": 240,
        "isPresciption": "false",
        "newFlage": "true",
    }
    assert payload["objectID"] == medicine.object_id
    assert payload["timestamp"] != SEED_TIME


async def test_a_prescription_is_flagged(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    await engine.add_medicine(
        client,
        "Amoxicillin",
        unit="ml",
        amount_per_time=2.5,
        interval=timedelta(hours=12),
        prescription=True,
    )

    [payload] = posted(fake_server)
    assert (payload["isPresciption"], payload["amountPerTime"], payload["interval"]) == (
        "true",
        2.5,
        720,
    )


async def test_a_new_medicine_is_offered_and_a_dose_embeds_it_as_the_iphone_does(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    medicine = await engine.add_medicine(
        client, "Nose Drops", unit="Drops", amount_per_time=2, interval=timedelta(hours=4)
    )
    assert engine.find_medicine("nose drops").object_id == medicine.object_id
    dose = await engine.add_medication(client, BABY_ID, medicine.object_id)

    assert dose.amount == 2  # the usual dose
    _, dose_post = posted(fake_server)
    embedded = dose_post["medicationSelection"]
    assert (embedded["BCObjectType"], embedded["newFlage"], embedded["timestamp"]) == (
        "MedicationSelection",
        "false",
        SEED_TIME,
    )
    assert (embedded["isPresciption"], embedded["interval"]) == ("false", 240)


async def test_a_synced_medicine_decodes_and_a_dose_embeds_it_as_the_iphone_did(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, IPHONE_MEDICINE)
    engine = await synced(client)

    assert isinstance(engine.records[IPHONE_MEDICINE["objectID"]], MedicationSelection)
    await engine.add_medication(client, BABY_ID, "Nose Drops")

    [payload] = posted(fake_server)
    # Exactly the copy the iPhone embedded in its dose of the medicine it had just synced.
    assert payload["medicationSelection"] == {
        **IPHONE_MEDICINE,
        "newFlage": "false",
        "timestamp": SEED_TIME,
    }


@pytest.mark.parametrize("name", ["nose drops", "  NOSE DROPS "])
async def test_a_name_already_on_the_list_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, name: str
) -> None:
    seed(fake_server, IPHONE_MEDICINE)
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(SelectionExistsError) as raised:
        await engine.add_medicine(
            client, name, unit="ml", amount_per_time=1, interval=timedelta(hours=6)
        )
    assert (raised.value.what, raised.value.object_id) == ("medicine", IPHONE_MEDICINE["objectID"])
    assert len(fake_server.requests) == before


GOOD: dict[str, Any] = {
    "name": "Nose Drops",
    "unit": "Drops",
    "amount_per_time": 2,
    "interval": timedelta(hours=4),
}


@pytest.mark.parametrize(
    "bad",
    [
        {"name": ""},
        {"name": "  "},
        {"name": 7},
        {"name": "x" * (MAX_MEDICINE_NAME_LENGTH + 1)},
        {"unit": ""},
        {"unit": None},
        {"unit": "x" * (MAX_MEDICINE_UNIT_LENGTH + 1)},
        {"amount_per_time": 0},
        {"amount_per_time": -1},
        {"amount_per_time": 0.001},  # zero once written
        {"amount_per_time": float("nan")},
        {"amount_per_time": True},
        {"amount_per_time": "2"},
        {"interval": timedelta(0)},
        {"interval": timedelta(hours=-4)},
        {"interval": timedelta(minutes=90)},  # the app takes whole hours
        {"interval": MAX_MEDICINE_INTERVAL + timedelta(hours=1)},
        {"interval": 4},
        {"interval": 240},
        {"prescription": "no"},
        {"prescription": 0},
        {"description": None},
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
        await engine.add_medicine(client, name, **args)
    assert len(fake_server.requests) == before


async def test_the_longest_interval_is_allowed(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    medicine = await engine.add_medicine(
        client, "Monthly", unit="ml", amount_per_time=1, interval=MAX_MEDICINE_INTERVAL
    )
    assert medicine.interval == 999 * 60


async def test_retrying_a_medicine_that_landed_returns_it_without_a_second_post(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    object_id = "C0000000-0000-4000-8000-0000000000AA"
    args: dict[str, Any] = {**GOOD, "object_id": object_id}
    name = args.pop("name")

    first = await engine.add_medicine(client, name, **args)
    again = await engine.add_medicine(client, name, **{**args, "object_id": object_id.lower()})

    assert again == first
    assert len(posted(fake_server)) == 1


async def test_a_medicine_after_the_group_is_reset_posts_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    fake_server.account_id = 777  # the group was reset in the app
    fake_server.sessions.clear()
    args = dict(GOOD)
    name = args.pop("name")

    with pytest.raises(AccountChangedError):
        await engine.add_medicine(client, name, **args)
    assert posted(fake_server) == []
