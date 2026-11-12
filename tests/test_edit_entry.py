"""Changing the pick-list entry a saved record names (invented data only)."""

from __future__ import annotations

from typing import Any

import pytest
from factories import BABY_ID, payload_for
from fake_server import FakeSyncServer
from selection_helpers import posted, rid, seed, synced

from pybabytracker import (
    BabyTrackerClient,
    Medication,
    Milestone,
    OtherActivity,
    OtherFeed,
    UnknownActivityError,
    UnknownFoodError,
    UnknownMedicineError,
    UnknownMilestoneError,
    UnknownVaccineError,
    Vaccine,
    WrongRecordTypeError,
)

DOSE, FOOD, ACTIVITY, MILESTONE, VACCINE = rid(31), rid(32), rid(33), rid(34), rid(35)
ENVELOPE = {"newFlage", "timestamp"}
EMBEDDED_AT = "1970-01-01 00:00:00 +0000"
DROPS: dict[str, Any] = {
    "name": "Test Drops",
    "BCObjectType": "MedicationSelection",
    "amountPerTime": 2,
    "unit": "Drops",
    "isPresciption": "false",
    "interval": 240,
    "newFlage": "false",
    "timestamp": EMBEDDED_AT,
    "objectID": "M0000000-0000-4000-8000-000000000001",
}
# A second medicine, as its own record from an iPhone.
SYRUP: dict[str, Any] = {
    "name": "Test Syrup",
    "BCObjectType": "MedicationSelection",
    "amountPerTime": 5,
    "unit": "ml",
    "isPresciption": "false",
    "interval": 360,
    "newFlage": "true",
    "timestamp": "2026-11-10 08:00:00 +0000",
    "objectID": "M0000000-0000-4000-8000-000000000002",
}
PEAR = {"objectID": "F0000000-0000-4000-8000-000000000001", "name": "Pear", "bottle": "false"}
TUMMY_TIME = "EasyLogSeed3559dca0-8cbf-11e2-9e96-0800200c9a66"
FIRST_SMILE = "EasyLogSeed56cc56b0-894f-11e2-9e96-0800200c9a66"
HEPB = "EasyLogSeed001ddeae8f0-8805-11e2-9e96-0800200c9a66"


def dose() -> dict[str, Any]:
    payload = payload_for("Medication", DOSE)
    payload.update(amount=2, medicationSelection=dict(DROPS), newFlage="true")
    return payload


def food(**changes: Any) -> dict[str, Any]:
    payload = payload_for("OtherFeed", FOOD)
    for key in ("descID", "typeID", "isEnglishScale"):
        payload.pop(key, None)
    payload.update(
        feedType=dict(PEAR),
        desc={"objectID": "MEAL-1", "newFlage": "true", "BCObjectType": "FeedDescription"},
        amount={"value": 2, "englishMeasure": "true", "BCObjectType": "VolumeMeasure"},
        unit="Oz",
        newFlage="true",
    )
    payload.update(changes)
    return {k: v for k, v in payload.items() if v is not None}


def activity() -> dict[str, Any]:
    payload = payload_for("OtherActivity", ACTIVITY)
    payload.update(desc={"objectID": TUMMY_TIME, "name": "Tummy Time"}, duration=5, newFlage="true")
    return payload


def changed(payload: dict[str, Any], original: dict[str, Any]) -> set[str]:
    return {k for k in set(payload) | set(original) if payload.get(k) != original.get(k)}


async def test_a_dose_gets_another_medicine_and_keeps_its_amount(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = dose()
    seed(fake_server, original, SYRUP)
    engine = await synced(client)

    updated = await engine.edit_medication(client, DOSE, medicine="test syrup")

    [payload] = posted(fake_server)
    assert changed(payload, original) == {"medicationSelection"} | ENVELOPE
    # Embedded as an add embeds an iPhone's medicine: not new, and dated 1970.
    assert payload["medicationSelection"] == {
        **SYRUP,
        "newFlage": "false",
        "timestamp": EMBEDDED_AT,
    }
    assert (payload["amount"], payload["newFlage"]) == (2, "false")
    assert isinstance(updated, Medication)
    assert updated.medicine_name == "Test Syrup"
    assert engine.entry_users(SYRUP["objectID"]) == 1
    assert engine.entry_users(DROPS["objectID"]) == 0


async def test_a_dose_gets_another_medicine_and_amount_in_one_update(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = dose()
    seed(fake_server, original, SYRUP)
    engine = await synced(client)

    await engine.edit_medication(
        client, DOSE, medicine=SYRUP["objectID"].lower(), amount=5, note="after lunch"
    )

    [payload] = posted(fake_server)
    assert changed(payload, original) == {"medicationSelection", "amount", "note"} | ENVELOPE
    assert payload["medicationSelection"]["name"] == "Test Syrup"
    assert (payload["amount"], payload["note"]) == (5, "after lunch")


async def test_a_solid_food_gets_another_food_embedded_as_an_add_embeds_it(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = food()
    seed(fake_server, original)
    engine = await synced(client)
    added = await engine.add_other_feed(client, BABY_ID, "Cereal", amount=1, unit="Tsp")

    updated = await engine.edit_other_feed(client, FOOD, food="cereal")

    payload = posted(fake_server)[-1]
    assert changed(payload, original) == {"feedType"} | ENVELOPE
    assert payload["feedType"] == added.to_payload()["feedType"]
    assert isinstance(updated, OtherFeed)
    assert (updated.food_name, updated.meal_id, updated.quantity, updated.unit) == (
        "Cereal",
        "MEAL-1",
        2,
        "Oz",
    )


async def test_an_activity_gets_another_type_and_length(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = activity()
    seed(fake_server, original)
    engine = await synced(client)
    added = await engine.add_activity(client, BABY_ID, "Story Time", duration=0)

    updated = await engine.edit_activity(client, ACTIVITY, activity="Story Time", duration=20)

    payload = posted(fake_server)[-1]
    assert changed(payload, original) == {"desc", "duration"} | ENVELOPE
    assert payload["desc"] == added.to_payload()["desc"]
    assert isinstance(updated, OtherActivity)
    assert (updated.activity_name, updated.duration) == ("Story Time", 20)


async def test_a_milestone_and_a_vaccine_get_another_entry(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    milestone, vaccine = payload_for("Milestone", MILESTONE), payload_for("Vaccine", VACCINE)
    seed(fake_server, milestone, vaccine)
    engine = await synced(client)
    added_milestone = await engine.add_milestone(client, BABY_ID, "Rolls over")
    added_vaccine = await engine.add_vaccine(client, BABY_ID, "DTaP")

    new_milestone = await engine.edit_milestone(client, MILESTONE, milestone=FIRST_SMILE)
    new_vaccine = await engine.edit_vaccine(client, VACCINE, vaccine="hepb", note="left thigh")

    milestone_payload, vaccine_payload = posted(fake_server)[-2:]
    assert changed(milestone_payload, milestone) == {"milestoneType"} | ENVELOPE
    assert milestone_payload["hasNewType"] == "false"
    assert milestone_payload["milestoneType"]["objectID"] == FIRST_SMILE
    # The same keys an add of a built-in milestone embeds.
    assert set(milestone_payload["milestoneType"]) == set(
        added_milestone.to_payload()["milestoneType"]
    )
    assert changed(vaccine_payload, vaccine) == {"vaccineType", "note"} | ENVELOPE
    assert vaccine_payload["vaccineType"]["objectID"] == HEPB
    assert set(vaccine_payload["vaccineType"]) == set(added_vaccine.to_payload()["vaccineType"])
    assert isinstance(new_milestone, Milestone)
    assert isinstance(new_vaccine, Vaccine)
    assert (new_milestone.milestone_name, new_vaccine.vaccine_name) == ("First smile", "HepB")


async def test_a_record_with_only_an_entrys_id_loses_it_for_the_embedded_entry(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    # As a seed row whose food the seed doesn't hold: no feedType, only typeID.
    original = food(feedType=None, typeID="F0000000-0000-4000-8000-0000000000FF")
    seed(fake_server, original)
    engine = await synced(client)

    updated = await engine.edit_other_feed(client, FOOD, food="Cereal")

    [payload] = posted(fake_server)
    assert changed(payload, original) == {"feedType", "typeID"} | ENVELOPE
    assert "typeID" not in payload
    assert updated.food_name == "Cereal"
    assert engine.entry_users("F0000000-0000-4000-8000-0000000000FF") == 0


async def test_naming_the_entry_a_record_already_has_posts_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    plain = food(feedType=None, typeID=PEAR["objectID"].lower(), objectID=rid(36))
    seed(fake_server, dose(), food(), activity(), plain)
    engine = await synced(client)

    assert (await engine.edit_medication(client, DOSE, medicine="Test Drops")).amount == 2
    await engine.edit_medication(client, DOSE, medicine=DROPS["objectID"].lower())
    await engine.edit_other_feed(client, FOOD, food="pear")
    await engine.edit_activity(client, ACTIVITY, activity="Tummy Time")
    # Named only by its plain ID, in another case: still the same food.
    await engine.edit_other_feed(client, rid(36), food="Pear")

    assert posted(fake_server) == []


async def test_the_same_entry_with_another_change_posts_only_that_change(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = activity()
    seed(fake_server, original)
    engine = await synced(client)

    await engine.edit_activity(client, ACTIVITY, activity="Tummy Time", duration=9)

    [payload] = posted(fake_server)
    assert changed(payload, original) == {"duration"} | ENVELOPE
    assert payload["desc"] == original["desc"]


BAD: list[tuple[str, str, dict[str, Any], type[Exception]]] = [
    ("edit_medication", DOSE, {"medicine": "No Such Medicine"}, UnknownMedicineError),
    ("edit_medication", DOSE, {"medicine": " "}, ValueError),
    ("edit_medication", DOSE, {"medicine": 7}, ValueError),
    ("edit_medication", DOSE, {"medicine": "Test Syrup", "amount": 0}, ValueError),
    ("edit_other_feed", FOOD, {"food": "No Such Food"}, UnknownFoodError),
    ("edit_other_feed", FOOD, {"food": ""}, ValueError),
    ("edit_activity", ACTIVITY, {"activity": "No Such Activity"}, UnknownActivityError),
    ("edit_activity", ACTIVITY, {"activity": None, "duration": None}, ValueError),
    ("edit_milestone", MILESTONE, {"milestone": "No Such Milestone"}, UnknownMilestoneError),
    ("edit_milestone", MILESTONE, {"milestone": ""}, ValueError),
    ("edit_milestone", MILESTONE, {}, ValueError),
    ("edit_vaccine", VACCINE, {"vaccine": "No Such Vaccine"}, UnknownVaccineError),
    ("edit_vaccine", VACCINE, {"vaccine": ""}, ValueError),
    ("edit_vaccine", VACCINE, {}, ValueError),
    ("edit_milestone", VACCINE, {"milestone": "First smile"}, WrongRecordTypeError),
    ("edit_vaccine", MILESTONE, {"vaccine": "HepB"}, WrongRecordTypeError),
    ("edit_milestone", rid(1), {"note": "x"}, WrongRecordTypeError),
]


@pytest.mark.parametrize(("method", "object_id", "bad", "error"), BAD)
async def test_a_bad_entry_is_refused_with_no_request(
    client: BabyTrackerClient,
    fake_server: FakeSyncServer,
    method: str,
    object_id: str,
    bad: dict[str, Any],
    error: type[Exception],
) -> None:
    seed(
        fake_server,
        dose(),
        SYRUP,
        food(),
        activity(),
        payload_for("Milestone", MILESTONE),
        payload_for("Vaccine", VACCINE),
    )
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(error):
        await getattr(engine, method)(client, object_id, **bad)

    assert len(fake_server.requests) == before


async def test_a_milestone_and_a_vaccine_change_their_time_and_note_alone(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    milestone, vaccine = payload_for("Milestone", MILESTONE), payload_for("Vaccine", VACCINE)
    seed(fake_server, milestone, vaccine)
    engine = await synced(client)

    await engine.edit_milestone(client, MILESTONE, note="at grandma's")
    await engine.edit_vaccine(client, VACCINE, note="left thigh")

    first, second = posted(fake_server)
    assert changed(first, milestone) == {"note"} | ENVELOPE
    assert changed(second, vaccine) == {"note"} | ENVELOPE
    assert first["milestoneType"] == milestone["milestoneType"]
    assert second["vaccineType"] == vaccine["vaccineType"]
