"""Medicine: the medicines a dose can name, and logging a dose."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from factories import BABY_ID, payload_for
from fake_server import FakeSyncServer
from selection_helpers import posted, rid, seed, synced

from pybabytracker import (
    BabyTrackerClient,
    Medication,
    MedicationSelection,
    UnknownMedicineError,
    decode_payload,
)

EVENT_TIME = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
# The keys a Medication is reported to have from both an iPhone and an Android phone (the
# expected shape; no capture is kept here), besides baby.
DOSE_KEYS = {
    "BCObjectType",
    "amount",
    "medicationSelection",
    "newFlage",
    "note",
    "objectID",
    "pictureLoaded",
    "pictureNote",
    "time",
    "timestamp",
    "timezone",
}
# An iPhone's embedded medicine, as synced, with invented values.
IOS_MEDICINE = {
    "name": "Test Drops",
    "BCObjectType": "MedicationSelection",
    "unit": "ml",
    "interval": 240,
    "isPresciption": "false",
    "newFlage": "false",
    "timestamp": "1970-01-01 00:00:00 +0000",
    "amountPerTime": 3.75,
    "objectID": "M0000000-0000-4000-8000-000000000001",
}
# An Android phone's, as reported: no BCObjectType, JSON booleans, "prescription" (expected
# shape, not yet seen in synced data here).
ANDROID_MEDICINE = {
    "objectID": "M0000000-0000-4000-8000-000000000002",
    "name": "Test Probiotic",
    "amountPerTime": 5,
    "interval": 1440,
    "unit": "drops",
    "prescription": False,
    "newFlage": False,
    "timestamp": "1970-01-01 00:00:00 +0000",
}


def dose(n: int, medicine: dict[str, Any], amount: float) -> dict[str, Any]:
    return payload_for("Medication", rid(n), amount=amount, medicationSelection=medicine)


def test_a_synced_medicine_selection_decodes() -> None:
    record = decode_payload({**ANDROID_MEDICINE, "BCObjectType": "MedicationSelection"})
    assert isinstance(record, MedicationSelection)
    assert (record.name, record.unit, record.amount_per_time, record.interval) == (
        "Test Probiotic",
        "drops",
        5,
        1440,
    )


async def test_the_medicines_are_the_ones_doses_have_used_by_name(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, dose(2, IOS_MEDICINE, 3.75), dose(3, ANDROID_MEDICINE, 5))
    engine = await synced(client)

    assert [c.name for c in engine.medicine_choices()] == ["Test Drops", "Test Probiotic"]
    assert not any(c.built_in for c in engine.medicine_choices())


async def test_a_dose_defaults_to_the_medicines_usual_amount_and_embeds_it_as_synced(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, dose(2, IOS_MEDICINE, 3.75))
    engine = await synced(client)

    record = await engine.add_medication(client, BABY_ID, "test drops", time=EVENT_TIME)

    assert isinstance(record, Medication)
    assert record.medicine_name == "Test Drops"
    [payload] = posted(fake_server)
    assert set(payload) - {"baby"} == DOSE_KEYS
    assert payload["amount"] == 3.75
    assert payload["medicationSelection"] == IOS_MEDICINE


async def test_a_dose_can_give_its_own_amount(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, dose(2, ANDROID_MEDICINE, 5))
    engine = await synced(client)

    await engine.add_medication(client, BABY_ID, "Test Probiotic", amount=2.5, time=EVENT_TIME)

    [payload] = posted(fake_server)
    assert payload["amount"] == 2.5
    assert payload["medicationSelection"] == ANDROID_MEDICINE


@pytest.mark.parametrize("amount", [0, -1, float("nan"), float("inf"), True, 0.001])
async def test_a_bad_amount_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, amount: Any
) -> None:
    seed(fake_server, dose(2, IOS_MEDICINE, 3.75))
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(ValueError):
        await engine.add_medication(client, BABY_ID, "Test Drops", amount=amount)
    assert len(fake_server.requests) == before


async def test_a_medicine_without_a_usual_dose_needs_an_amount(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    no_usual = {k: v for k, v in ANDROID_MEDICINE.items() if k != "amountPerTime"}
    seed(fake_server, dose(2, no_usual, 1))
    engine = await synced(client)

    with pytest.raises(ValueError, match="give an amount"):
        await engine.add_medication(client, BABY_ID, "Test Probiotic", time=EVENT_TIME)
    assert posted(fake_server) == []


async def test_an_unknown_medicine_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(UnknownMedicineError):
        await engine.add_medication(client, BABY_ID, "Tylenol", amount=1)
    assert len(fake_server.requests) == before
