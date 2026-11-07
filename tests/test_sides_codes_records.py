"""Side numbering, Android value types, and the Medication and NursingSession records."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from factories import BABY_ID, baby_payload, payload_for
from fake_server import FakeSyncServer
from test_writes import decode, nth_post, seed, synced_engine

import pybabytracker
from pybabytracker import (
    DIAPER_DIRTY,
    DIAPER_MIXED,
    DIAPER_WET,
    RECORD_TYPES,
    SIDE_LEFT,
    SIDE_NOT_SET,
    SIDE_RIGHT,
    BabyTrackerClient,
    Medication,
    Nursing,
    NursingSession,
    Record,
    UnknownRecord,
    VolumeMeasure,
    day_totals,
    decode_payload,
    decode_transaction,
    encode_transaction,
    numeric_code,
)
from pybabytracker.builders import Draft, draft_nursing, draft_pump
from pybabytracker.models import Volume

DAY_START = datetime(2026, 10, 26, tzinfo=UTC)
DAY_END = DAY_START + timedelta(days=1)
NOON = DAY_START + timedelta(hours=12)
NOON_TEXT = "2026-10-26 12:00:00 +0000"
START = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


def nursing_draft(left: int, right: int, finish_side: int | None = None):  # type: ignore[no-untyped-def]
    return draft_nursing(
        left_minutes=left,
        right_minutes=right,
        finish_side=finish_side,
        start_time=START,
        end_time=None,
        timezone_offset=None,
        note="",
    )


def pump_draft(left: int, right: int, finish_side: int | None = None):  # type: ignore[no-untyped-def]
    return draft_pump(
        amount=None,
        left_volume=Volume(2, "oz") if left else None,
        right_volume=Volume(3, "oz") if right else None,
        left_minutes=left,
        right_minutes=right,
        finish_side=finish_side,
        time=START,
        timezone_offset=None,
        note="",
    )


def built(draft: Any) -> Any:
    return draft.build("AAAAAAAA-0000-4000-8000-000000000009", None, START)


def totals_for(*payloads: dict[str, Any]) -> pybabytracker.DayTotals:
    records = [
        decode_payload({**p, "time": NOON_TEXT, "baby": baby_payload(object_id=BABY_ID)})
        for p in payloads
    ]
    return day_totals(records, BABY_ID, DAY_START, DAY_END)


def round_trip(payload: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(payload).encode()
    record = decode_transaction(base64.b64encode(text).decode())
    out: dict[str, Any] = json.loads(base64.b64decode(encode_transaction(record)))
    return out


# --- #120: side numbering -----------------------------------------------------------------


def test_the_side_numbers_are_the_apps() -> None:
    assert (SIDE_NOT_SET, SIDE_LEFT, SIDE_RIGHT) == (0, 1, 2)


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [(5, 0, 1), (0, 5, 2), (5, 5, 2)],
    ids=["left only", "right only", "both"],
)
def test_a_nursing_session_defaults_to_the_last_side_used(
    left: int, right: int, expected: int
) -> None:
    assert built(nursing_draft(left, right)).finish_side == expected


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [(5, 0, 1), (0, 5, 2), (5, 5, 2)],
    ids=["left only", "right only", "both"],
)
def test_a_pump_defaults_to_the_last_side_used(left: int, right: int, expected: int) -> None:
    assert built(pump_draft(left, right)).finish_side == expected


@pytest.mark.parametrize("side", [1, 2])
def test_an_explicit_finish_side_is_written_as_given(side: int) -> None:
    assert built(nursing_draft(5, 5, side)).finish_side == side
    assert built(pump_draft(5, 5, side)).finish_side == side


@pytest.mark.parametrize("side", [0, 3, -1, True])
def test_an_invalid_finish_side_is_refused(side: Any) -> None:
    with pytest.raises(ValueError, match="finish_side"):
        nursing_draft(5, 5, side)
    with pytest.raises(ValueError, match="finish_side"):
        pump_draft(5, 5, side)


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [(5, 0, 1), (0, 5, 2), (5, 5, 0)],
    ids=["left only", "right only", "both"],
)
def test_a_pumps_sides_field_is_the_apps_both_left_right(
    left: int, right: int, expected: int
) -> None:
    assert built(pump_draft(left, right)).sides == expected


def test_a_pump_with_only_durations_writes_both_sides_when_both_ran() -> None:
    draft = draft_pump(
        amount=None,
        left_volume=None,
        right_volume=None,
        left_minutes=4,
        right_minutes=4,
        finish_side=None,
        time=START,
        timezone_offset=None,
        note="",
    )
    assert built(draft).sides == 0


def test_a_nursing_record_written_with_the_old_numbers_still_decodes() -> None:
    record = decode_payload(payload_for("Nursing", finishSide=0))
    assert isinstance(record, Nursing)
    assert record.finish_side == 0


# --- #122: Android value types ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, 1),
        (0, 0),
        (12, 12),
        ("1", 1),
        ("0", 0),
        ("12", 12),
        ("", None),
        ("1.5", None),
        (" 1", None),
        ("-1", None),
        ("one", None),
        (None, None),
        (True, None),
        (False, None),
        (1.0, None),
        (1.5, None),
        ([1], None),
    ],
)
def test_numeric_code(value: Any, expected: int | None) -> None:
    assert numeric_code(value) == expected


def test_numeric_code_of_an_absurdly_long_digit_string_is_none() -> None:
    assert numeric_code("9" * 5000) is None


def test_numeric_code_does_not_turn_a_bool_into_an_int() -> None:
    assert numeric_code(True) is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("0", (1, 0, 0)),
        ("1", (0, 1, 0)),
        ("2", (0, 0, 1)),
        (DIAPER_WET, (1, 0, 0)),
        (DIAPER_DIRTY, (0, 1, 0)),
        (DIAPER_MIXED, (0, 0, 1)),
        ("9", (0, 0, 0)),
        ("", (0, 0, 0)),
    ],
)
def test_a_diaper_with_a_string_status_is_counted_by_kind(
    status: Any, expected: tuple[int, int, int]
) -> None:
    result = totals_for(payload_for("Diaper", status=status, amount="1"))
    assert (result.diapers, result.wet, result.dirty, result.mixed) == (1, *expected)


def test_a_day_of_android_and_ios_diapers_adds_up() -> None:
    result = totals_for(
        payload_for("Diaper", object_id="A1", status="0"),
        payload_for("Diaper", object_id="A2", status="1"),
        payload_for("Diaper", object_id="A3", status="2"),
        payload_for("Diaper", object_id="A4", status=0),
    )
    assert (result.diapers, result.wet, result.dirty, result.mixed) == (4, 2, 1, 1)


def measure(value: Any, english: Any) -> dict[str, Any]:
    return {"BCObjectType": "VolumeMeasure", "englishMeasure": english, "value": value}


@pytest.mark.parametrize(
    ("english", "unit"),
    [(True, "oz"), ("true", "oz"), (False, "ml"), ("false", "ml"), (None, "ml"), ("maybe", "ml")],
)
def test_english_measure_is_read_the_same_as_a_boolean_or_a_string(english: Any, unit: str) -> None:
    assert VolumeMeasure(raw=measure(4, english)).volume == Volume(4.0, unit)


def test_android_ounce_and_milliliter_bottles_count_in_the_day_volume() -> None:
    result = totals_for(
        payload_for("Formula", object_id="B1", amount=measure(4, True)),
        payload_for("Pumped", object_id="B2", amount=measure(60, False)),
    )
    assert result.bottle_milliliters == pytest.approx(4 * 29.5735295625 + 60)


def test_an_android_payload_round_trips_with_its_raw_types() -> None:
    payloads = [
        payload_for("Diaper", status="1", amount="2"),
        payload_for("Nursing", finishSide="1", state="5", pairSessionState="0", descID="d"),
        payload_for("Formula", amount=measure(4, True), isEnglishScale=1),
        payload_for("Pump", amount=measure(4, False), finishSide="2"),
    ]
    for payload in payloads:
        assert round_trip(payload) == payload


def test_string_codes_stay_strings_on_the_decoded_record() -> None:
    record = decode_payload(payload_for("Nursing", finishSide="1", state="5"))
    assert isinstance(record, Nursing)
    assert (record.finish_side, record.state) == ("1", "5")


# --- #121: Medication ------------------------------------------------------------------------


def medication_payload(prescription_key: str | None, **overrides: Any) -> dict[str, Any]:
    selection: dict[str, Any] = {
        "BCObjectType": "MedicationSelection",
        "objectID": "SEL-1",
        "name": "Test Syrup",
        "unit": "ml",
        "amountPerTime": 5,
        "interval": 6,
    }
    if prescription_key is not None:
        selection[prescription_key] = 0
    object_id = overrides.pop("object_id", "MED-1")
    payload = payload_for("Medicine", object_id=object_id, **overrides)
    payload.pop("medID")
    return {
        **payload,
        "BCObjectType": "Medication",
        "amount": 5,
        "medicationSelection": selection,
        **overrides,
    }


@pytest.mark.parametrize("key", ["isPresciption", "prescription"])
def test_a_medication_decodes_with_either_prescription_spelling(key: str) -> None:
    payload = medication_payload(key)
    record = decode_payload(payload)
    assert isinstance(record, Medication)
    assert not isinstance(record, UnknownRecord)
    assert record.amount == 5
    assert record.medicine_name == "Test Syrup"
    assert record.medication_selection is not None
    assert record.medication_selection["amountPerTime"] == 5
    assert round_trip(payload) == payload


def test_a_medication_without_a_selection_has_no_name() -> None:
    payload = medication_payload(None)
    del payload["medicationSelection"]
    record = decode_payload(payload)
    assert isinstance(record, Medication)
    assert record.medication_selection is None
    assert record.medicine_name is None
    assert round_trip(payload) == payload


@pytest.mark.parametrize("selection", [None, "text", {}, {"name": 5}])
def test_a_medication_with_an_odd_selection_has_no_name(selection: Any) -> None:
    record = decode_payload(medication_payload(None, medicationSelection=selection))
    assert isinstance(record, Medication)
    assert record.medicine_name is None


def test_medication_is_registered_and_medicine_is_unchanged() -> None:
    assert RECORD_TYPES["Medication"] is Medication
    medicine = decode_payload(payload_for("Medicine"))
    assert type(medicine).__name__ == "Medicine"
    assert medicine.describe() == {"med_id": "med-1", "amount": 2.5}


def test_medication_is_not_counted_in_the_day_totals() -> None:
    result = totals_for(medication_payload("prescription", object_id="M1"))
    assert (result.diapers, result.feeds) == (0, 0)


# --- #121: NursingSession --------------------------------------------------------------------


def session_payload(**overrides: Any) -> dict[str, Any]:
    payload = payload_for("Nursing", **overrides)
    payload.update(
        BCObjectType="NursingSession",
        detailed="true",
        leftTimestamps=["2026-10-26 12:00:00 +0000"],
        rightTimestamps=[],
    )
    return payload


def test_a_nursing_session_decodes_as_a_nursing() -> None:
    record = decode_payload(session_payload(finishSide="1"))
    assert isinstance(record, NursingSession)
    assert isinstance(record, Nursing)
    assert record.record_type == "NursingSession"
    assert record.detailed == "true"
    assert record.left_timestamps == ["2026-10-26 12:00:00 +0000"]
    assert record.right_timestamps == []
    assert RECORD_TYPES["NursingSession"] is NursingSession


def test_a_nursing_session_round_trips_with_its_type_name() -> None:
    payload = session_payload(finishSide="2", state="5")
    out = round_trip(payload)
    assert out == payload
    assert out["BCObjectType"] == "NursingSession"


def test_a_nursing_session_counts_as_a_feed() -> None:
    result = totals_for(session_payload(object_id="S1"), payload_for("Nursing", object_id="S2"))
    assert result.feeds == 2


def test_a_plain_nursing_record_is_still_a_nursing() -> None:
    record = decode_payload(payload_for("Nursing"))
    assert type(record) is Nursing
    assert record.record_type == "Nursing"


@pytest.mark.parametrize("type_name", ["Medication", "NursingSession"])
def test_the_new_types_describe_only_set_fields(type_name: str) -> None:
    payload = medication_payload("prescription") if type_name == "Medication" else session_payload()
    record: Record = decode_payload(payload)
    expected = (
        {"amount", "medication_selection"}
        if type_name == "Medication"
        else {
            "desc_id",
            "finish_side",
            "left_duration",
            "right_duration",
            "both_duration",
            "detailed",
            "left_timestamps",
            "right_timestamps",
        }
    )
    assert set(record.describe()) == expected


async def test_a_nursing_session_can_be_written_and_deleted(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)

    def build(object_id: str, baby: Any, timestamp: datetime) -> NursingSession:
        return NursingSession(
            object_id=object_id,
            time=START,
            timestamp=timestamp,
            baby=baby,
            finish_side=SIDE_LEFT,
            left_timestamps=[],
        )

    record = await engine._add(client, BABY_ID, Draft(NursingSession, build), None)
    assert isinstance(engine.records[record.object_id], NursingSession)
    assert decode(nth_post(fake_server, 0))["BCObjectType"] == "NursingSession"

    await engine.delete_record(client, record.object_id)

    assert record.object_id not in engine.records
    deleted = decode(nth_post(fake_server, 1))
    assert deleted["BCObjectType"] == "NursingSession"
    assert deleted["newFlage"] == "false"
