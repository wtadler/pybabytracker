"""Editing a temperature, a growth record, a dose, a solid food, and an activity (invented
data only)."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import pytest
from factories import payload_for
from fake_server import FakeSyncServer
from selection_helpers import posted, rid, seed, synced

from pybabytracker import (
    MAX_ACTIVITY_MINUTES,
    MAX_OTHER_FEED_AMOUNT,
    UNIT_CELSIUS,
    UNIT_CENTIMETERS,
    UNIT_FAHRENHEIT,
    UNIT_INCHES,
    UNIT_KILOGRAMS,
    UNIT_POUNDS,
    BabyTrackerClient,
    Growth,
    Measurement,
    Medication,
    OtherActivity,
    OtherFeed,
    Temperature,
    WrongRecordTypeError,
)

TEMPERATURE, GROWTH, DOSE, FOOD, ACTIVITY = rid(21), rid(22), rid(23), rid(24), rid(25)
ENVELOPE = {"newFlage", "timestamp"}
MEDICINE = {
    "name": "Test Drops",
    "BCObjectType": "MedicationSelection",
    "amountPerTime": 2,
    "unit": "Drops",
    "isPresciption": "false",
    "interval": 240,
    "newFlage": "false",
    "timestamp": "1970-01-01 00:00:00 +0000",
    "objectID": "M0000000-0000-4000-8000-000000000001",
}


def measure(kind: str, value: float, english: str = "true") -> dict[str, Any]:
    return {"value": value, "englishMeasure": english, "BCObjectType": kind}


def temperature() -> dict[str, Any]:
    payload = payload_for("Temperature", TEMPERATURE)
    for key in ("temp", "isEnglishScale"):
        payload.pop(key, None)
    payload.update(temperature=measure("TemperatureMeasure", 99.5), newFlage="true")
    return payload


def growth(**measures: Any) -> dict[str, Any]:
    payload = payload_for("Growth", GROWTH)
    for key in ("weight", "length", "head", "isEnglishLengthScale", "isEnglishWeightScale"):
        payload.pop(key, None)
    payload.update(weight=measure("WeightMeasure", 9.25), newFlage="true")
    payload.update(measures)
    return payload


def dose() -> dict[str, Any]:
    payload = payload_for("Medication", DOSE)
    payload.update(amount=2, medicationSelection=dict(MEDICINE), newFlage="true")
    return payload


def food() -> dict[str, Any]:
    payload = payload_for("OtherFeed", FOOD)
    for key in ("descID", "typeID", "isEnglishScale"):
        payload.pop(key, None)
    payload.update(
        feedType={"objectID": "F-1", "name": "Pear", "bottle": "false"},
        desc={"objectID": "MEAL-1", "newFlage": "true", "BCObjectType": "FeedDescription"},
        amount=measure("VolumeMeasure", 2),
        unit="Oz",
        newFlage="true",
    )
    return payload


def activity() -> dict[str, Any]:
    payload = payload_for("OtherActivity", ACTIVITY)
    payload.update(desc={"objectID": "T-1", "name": "Tummy Time"}, duration=5, newFlage="true")
    return payload


def changed(payload: dict[str, Any], original: dict[str, Any]) -> set[str]:
    return {k for k in set(payload) | set(original) if payload.get(k) != original.get(k)}


async def test_a_temperatures_reading_changes_and_nothing_else(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = temperature()
    seed(fake_server, original)
    engine = await synced(client)

    updated = await engine.edit_temperature(
        client, TEMPERATURE, temperature=Measurement(37.8, UNIT_CELSIUS)
    )

    [payload] = posted(fake_server)
    assert list(payload) == list(original)
    assert changed(payload, original) == {"temperature"} | ENVELOPE
    assert payload["temperature"] == measure("TemperatureMeasure", 37.8, "false")
    assert isinstance(updated, Temperature)
    assert updated.reading == Measurement(37.8, UNIT_CELSIUS)


async def test_a_growth_records_weight_changes_and_a_length_is_added(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = growth()
    seed(fake_server, original)
    engine = await synced(client)

    updated = await engine.edit_growth(
        client,
        GROWTH,
        weight=Measurement(4.3, UNIT_KILOGRAMS),
        length=Measurement(21.5, UNIT_INCHES),
    )

    [payload] = posted(fake_server)
    assert changed(payload, original) == {"weight", "length"} | ENVELOPE
    assert payload["weight"] == measure("WeightMeasure", 4.3, "false")
    assert payload["length"] == measure("LengthMeasure", 21.5)
    assert "head" not in payload
    assert isinstance(updated, Growth)
    assert updated.weight_reading == Measurement(4.3, UNIT_KILOGRAMS)


async def test_a_new_head_must_match_the_saved_lengths_unit(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, growth(length=measure("LengthMeasure", 55, "false")))
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(ValueError):
        await engine.edit_growth(client, GROWTH, head=Measurement(14, UNIT_INCHES))
    assert len(fake_server.requests) == before

    updated = await engine.edit_growth(client, GROWTH, head=Measurement(36, UNIT_CENTIMETERS))
    assert updated.head_reading == Measurement(36, UNIT_CENTIMETERS)
    assert updated.length_reading == Measurement(55, UNIT_CENTIMETERS)


async def test_a_doses_amount_changes_and_its_medicine_stays(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = dose()
    seed(fake_server, original)
    engine = await synced(client)

    updated = await engine.edit_medication(client, DOSE, amount=2.5)

    [payload] = posted(fake_server)
    assert changed(payload, original) == {"amount"} | ENVELOPE
    assert payload["amount"] == 2.5
    assert payload["medicationSelection"] == MEDICINE
    assert isinstance(updated, Medication)


async def test_a_solid_foods_amount_and_unit_change_and_its_food_and_meal_stay(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = food()
    seed(fake_server, original)
    engine = await synced(client)

    updated = await engine.edit_other_feed(client, FOOD, amount=3, unit=" Pieces ")

    [payload] = posted(fake_server)
    assert changed(payload, original) == {"amount", "unit"} | ENVELOPE
    assert payload["amount"] == measure("VolumeMeasure", 3)
    assert payload["unit"] == "Pieces"
    assert isinstance(updated, OtherFeed)
    assert (updated.food_name, updated.meal_id, updated.quantity) == ("Pear", "MEAL-1", 3)


async def test_an_activitys_length_changes_and_its_type_stays(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = activity()
    seed(fake_server, original)
    engine = await synced(client)

    updated = await engine.edit_activity(
        client, ACTIVITY, duration=12, time=datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
    )

    [payload] = posted(fake_server)
    assert changed(payload, original) == {"duration", "time"} | ENVELOPE
    assert payload["duration"] == 12
    assert isinstance(updated, OtherActivity)
    assert updated.activity_name == "Tummy Time"


BAD: list[tuple[str, str, dict[str, Any]]] = [
    ("edit_temperature", TEMPERATURE, {"temperature": Measurement(37, UNIT_FAHRENHEIT)}),
    ("edit_temperature", TEMPERATURE, {"temperature": Measurement(310, "K")}),
    ("edit_temperature", TEMPERATURE, {"temperature": 99.5}),
    ("edit_growth", GROWTH, {"weight": Measurement(150, UNIT_POUNDS)}),
    ("edit_growth", GROWTH, {"weight": Measurement(9, UNIT_INCHES)}),
    (
        "edit_growth",
        GROWTH,
        {"length": Measurement(21, UNIT_INCHES), "head": Measurement(36, UNIT_CENTIMETERS)},
    ),
    ("edit_medication", DOSE, {"amount": 0}),
    ("edit_medication", DOSE, {"amount": 0.001}),
    ("edit_medication", DOSE, {"amount": math.nan}),
    ("edit_medication", DOSE, {"amount": True}),
    ("edit_other_feed", FOOD, {"amount": 0}),
    ("edit_other_feed", FOOD, {"amount": MAX_OTHER_FEED_AMOUNT + 1}),
    ("edit_other_feed", FOOD, {"unit": "  "}),
    ("edit_other_feed", FOOD, {"unit": "x" * 31}),
    ("edit_activity", ACTIVITY, {"duration": -1}),
    ("edit_activity", ACTIVITY, {"duration": MAX_ACTIVITY_MINUTES + 1}),
    ("edit_activity", ACTIVITY, {"duration": 1.5}),
]


@pytest.mark.parametrize(("method", "object_id", "bad"), BAD)
async def test_a_bad_value_is_refused_with_no_request(
    client: BabyTrackerClient,
    fake_server: FakeSyncServer,
    method: str,
    object_id: str,
    bad: dict[str, Any],
) -> None:
    seed(fake_server, temperature(), growth(), dose(), food(), activity())
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(ValueError):
        await getattr(engine, method)(client, object_id, **bad)
    assert len(fake_server.requests) == before


EDITS: list[tuple[str, str, dict[str, Any]]] = [
    ("edit_temperature", TEMPERATURE, {"temperature": Measurement(100.4, UNIT_FAHRENHEIT)}),
    ("edit_growth", GROWTH, {"weight": Measurement(10, UNIT_POUNDS)}),
    ("edit_medication", DOSE, {"amount": 3}),
    ("edit_other_feed", FOOD, {"amount": 4}),
    ("edit_activity", ACTIVITY, {"duration": 10}),
]


@pytest.mark.parametrize(("method", "object_id", "change"), EDITS)
async def test_each_edit_refuses_another_kind_of_record_and_nothing_to_change(
    client: BabyTrackerClient,
    fake_server: FakeSyncServer,
    method: str,
    object_id: str,
    change: dict[str, Any],
) -> None:
    seed(
        fake_server,
        temperature(),
        growth(),
        dose(),
        food(),
        activity(),
        payload_for("Diaper", rid(30)),
    )
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(WrongRecordTypeError):
        await getattr(engine, method)(client, rid(30), **change)  # a diaper
    with pytest.raises(ValueError):
        await getattr(engine, method)(client, object_id)
    assert len(fake_server.requests) == before


@pytest.mark.parametrize(("method", "object_id", "change"), EDITS)
async def test_repeating_an_edit_posts_nothing_more_and_a_note_rides_along(
    client: BabyTrackerClient,
    fake_server: FakeSyncServer,
    method: str,
    object_id: str,
    change: dict[str, Any],
) -> None:
    seed(fake_server, temperature(), growth(), dose(), food(), activity())
    engine = await synced(client)

    first = await getattr(engine, method)(client, object_id, note="Edited", **change)
    again = await getattr(engine, method)(client, object_id, note="Edited", **change)

    assert again == first
    [payload] = posted(fake_server)
    assert payload["note"] == "Edited"
