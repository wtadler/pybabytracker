"""The captured feed, pump, nursing, and sleep payloads, as typed records."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from factories import baby_payload, encode_b64, payload_for

from pybabytracker import (
    OP_ADD,
    OP_DELETE,
    OP_UPDATE,
    Formula,
    Nursing,
    Pump,
    Pumped,
    RawTransaction,
    Sleep,
    SyncEngine,
    UnknownRecord,
    Volume,
    VolumeMeasure,
    decode_payload,
    decode_transaction,
    encode_transaction,
)

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "captured_payloads.json").read_text(encoding="utf-8")
)
BY_LABEL = {f["label"]: f for f in FIXTURES}
DEVICE = "DDDD4444-0000-4000-8000-00000000000D"
OTHER_DEVICE = "EEEE5555-0000-4000-8000-00000000000E"
SLEEP_ID = "CAFE0000-0000-4000-8000-000000000005"
FORMULA_ID = "CAFE0000-0000-4000-8000-000000000001"

TYPED = {
    "Formula": Formula,
    "Pumped": Pumped,
    "Pump": Pump,
    "Nursing": Nursing,
    "Sleep": Sleep,
}


def raw(fixture: dict[str, Any]) -> RawTransaction:
    return RawTransaction(
        sync_id=fixture["sync_id"], op_code=fixture["opcode"], transaction=fixture["transaction"]
    )


def by_prefix(prefix: str) -> list[dict[str, Any]]:
    return [f for f in FIXTURES if f["label"].startswith(prefix)]


# --- every fixture decodes typed and round-trips -----------------------------------------


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f["label"] for f in FIXTURES])
def test_a_fixture_round_trips_to_the_same_json(fixture: dict[str, Any]) -> None:
    record = decode_transaction(fixture["transaction"])
    assert not isinstance(record, UnknownRecord)
    assert record.to_payload() == fixture["payload"]
    decoded = json.loads(base64.b64decode(encode_transaction(record)))
    assert decoded == fixture["payload"]


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f["label"] for f in FIXTURES])
def test_a_fixture_round_trips_byte_for_byte(fixture: dict[str, Any]) -> None:
    record = decode_transaction(fixture["transaction"])
    assert encode_transaction(record) == fixture["transaction"]


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f["label"] for f in FIXTURES])
def test_a_fixture_decodes_to_its_typed_class(fixture: dict[str, Any]) -> None:
    record = decode_transaction(fixture["transaction"])
    type_name = fixture["payload"]["BCObjectType"]
    assert type(record).__name__ == type_name
    if type_name in TYPED:
        assert type(record) is TYPED[type_name]


@pytest.mark.parametrize("label", ["C1 formula add", "C2 pumped bottle add", "C3 pump add"])
def test_a_captured_volume_is_a_volume_measure_object(label: str) -> None:
    record = decode_transaction(BY_LABEL[label]["transaction"])
    assert isinstance(record.amount, VolumeMeasure)  # type: ignore[attr-defined]


# --- volumes ------------------------------------------------------------------------------


def test_a_formula_volume_is_a_number_in_ounces() -> None:
    record = decode_transaction(BY_LABEL["C1 formula add"]["transaction"])
    assert isinstance(record, Formula)
    assert record.volume == Volume(4.0, "oz")
    assert record.volume.value == 4
    assert record.volume.unit == "oz"


def test_a_pumped_bottle_volume_is_a_number_in_ounces() -> None:
    record = decode_transaction(BY_LABEL["C2 pumped bottle add"]["transaction"])
    assert isinstance(record, Pumped)
    assert record.volume == Volume(3.0, "oz")


def test_a_pump_has_total_and_per_side_volumes() -> None:
    record = decode_transaction(BY_LABEL["C3 pump add"]["transaction"])
    assert isinstance(record, Pump)
    assert record.volume == Volume(7.0, "oz")
    assert record.left_volume == Volume(2.0, "oz")
    assert record.right_volume == Volume(5.0, "oz")
    assert (record.left_duration, record.right_duration) == (1, 1)
    assert (record.finish_side, record.sides) == (0, 0)


def test_english_measure_false_means_milliliters() -> None:
    payload = payload_for(
        "Formula",
        amount={"BCObjectType": "VolumeMeasure", "englishMeasure": "false", "value": 120},
    )
    record = decode_payload(payload)
    assert isinstance(record, Formula)
    assert record.volume == Volume(120.0, "ml")
    assert record.to_payload() == payload


def test_a_missing_english_measure_means_milliliters() -> None:
    payload = payload_for("Formula", amount={"BCObjectType": "VolumeMeasure", "value": 90})
    record = decode_payload(payload)
    assert isinstance(record, Formula)
    assert record.volume == Volume(90.0, "ml")
    assert record.to_payload() == payload  # and the key stays absent


def test_a_fractional_volume_survives() -> None:
    payload = payload_for(
        "Pumped",
        amount={"BCObjectType": "VolumeMeasure", "englishMeasure": "true", "value": 3.5},
    )
    record = decode_payload(payload)
    assert isinstance(record, Pumped)
    assert record.volume == Volume(3.5, "oz")
    assert record.to_payload() == payload


def test_a_non_numeric_volume_value_decodes_without_a_number() -> None:
    payload = payload_for(
        "Formula",
        amount={"BCObjectType": "VolumeMeasure", "englishMeasure": "true", "value": "lots"},
    )
    record = decode_payload(payload)
    assert isinstance(record, Formula)
    assert record.volume is None
    assert record.to_payload() == payload


def test_a_boolean_volume_value_is_not_a_number() -> None:
    payload = payload_for(
        "Formula",
        amount={"BCObjectType": "VolumeMeasure", "englishMeasure": "true", "value": True},
    )
    record = decode_payload(payload)
    assert record.volume is None  # type: ignore[attr-defined]
    assert record.to_payload() == payload


def test_a_volume_measure_with_no_value_has_no_volume() -> None:
    payload = payload_for(
        "Formula", amount={"BCObjectType": "VolumeMeasure", "englishMeasure": "true"}
    )
    record = decode_payload(payload)
    assert isinstance(record, Formula)
    assert record.volume is None
    assert record.to_payload() == payload


def test_a_plain_number_volume_still_decodes_and_round_trips() -> None:
    payload = payload_for("Formula", amount=4.5, isEnglishScale=1)
    record = decode_payload(payload)
    assert isinstance(record, Formula)
    assert record.to_payload() == payload
    assert record.volume == Volume(4.5, "oz")


def test_a_plain_number_volume_with_the_metric_flag_is_milliliters() -> None:
    record = decode_payload(payload_for("Pumped", amount=120, isEnglishScale=0))
    assert isinstance(record, Pumped)
    assert record.volume == Volume(120.0, "ml")


def test_a_plain_number_volume_with_no_scale_has_no_unit() -> None:
    payload = payload_for("Formula", amount=4)
    del payload["isEnglishScale"]
    record = decode_payload(payload)
    assert isinstance(record, Formula)
    assert record.volume is None
    assert record.to_payload() == payload


def test_a_volume_that_is_neither_object_nor_number_is_kept_without_failing() -> None:
    payload = payload_for("Formula", amount="four")
    record = decode_payload(payload)
    assert record.to_payload() == payload
    assert record.volume is None  # type: ignore[attr-defined]


def test_a_null_or_absent_volume_is_none() -> None:
    payload = payload_for("Pump")
    for key in ("amount", "leftAmount", "rightAmount"):
        payload.pop(key, None)
    record = decode_payload(payload)
    assert isinstance(record, Pump)
    assert (record.volume, record.left_volume, record.right_volume) == (None, None, None)
    assert record.to_payload() == payload
    payload["amount"] = None
    record = decode_payload(payload)
    assert record.volume is None  # type: ignore[attr-defined]
    assert record.to_payload() == payload


def test_unknown_keys_inside_a_volume_measure_survive() -> None:
    payload = payload_for(
        "Formula",
        amount={
            "BCObjectType": "VolumeMeasure",
            "englishMeasure": "true",
            "value": 4,
            "futureKey": [1, {"a": None}],
        },
    )
    record = decode_payload(payload)
    assert record.to_payload() == payload


# --- nursing --------------------------------------------------------------------------------


def test_a_captured_nursing_has_its_new_fields() -> None:
    record = decode_transaction(
        BY_LABEL["C4 nursing add (timer posts nothing until stopped)"]["transaction"]
    )
    assert isinstance(record, Nursing)
    assert record.left_duration == 1
    assert record.right_duration == 1
    assert record.both_duration == 0
    assert record.finish_side == 1
    assert record.is_detailed == "true"
    assert record.state == 5
    assert record.pair_session_state == 0


def test_a_nursing_with_only_a_left_duration() -> None:
    payload = payload_for("Nursing")
    for key in ("rightDuration", "bothDuration", "finishSide", "descID"):
        payload.pop(key)
    payload["leftDuration"] = 7
    record = decode_payload(payload)
    assert isinstance(record, Nursing)
    assert record.left_duration == 7
    assert record.right_duration is None
    assert record.both_duration is None
    assert record.to_payload() == payload


# --- pump -------------------------------------------------------------------------------------


@pytest.mark.parametrize("sides", [1, 2, 3, -1])
def test_a_pump_with_sides_other_than_zero_round_trips(sides: int) -> None:
    payload = payload_for("Pump", sides=sides)
    record = decode_payload(payload)
    assert isinstance(record, Pump)
    assert record.sides == sides
    assert record.to_payload() == payload


# --- sleep ------------------------------------------------------------------------------------


def test_a_sleep_with_duration_zero_is_running_and_the_update_is_finished() -> None:
    started = decode_transaction(
        BY_LABEL["C5 sleep add at timer start (duration 0)"]["transaction"]
    )
    stopped = decode_transaction(BY_LABEL["C5 sleep update at timer stop"]["transaction"])
    assert isinstance(started, Sleep) and isinstance(stopped, Sleep)
    assert started.duration == 0
    assert started.is_running is True
    assert stopped.duration == 1
    assert stopped.is_running is False


def test_a_sleep_with_no_duration_is_not_running() -> None:
    payload = payload_for("Sleep")
    del payload["duration"]
    record = decode_payload(payload)
    assert isinstance(record, Sleep)
    assert record.is_running is False


# --- extras and non-ASCII notes ---------------------------------------------------------------


@pytest.mark.parametrize("type_name", ["Formula", "Pumped", "Pump", "Nursing", "Sleep"])
def test_unknown_extra_keys_survive_the_round_trip(type_name: str) -> None:
    payload = payload_for(type_name, futureField={"a": [1, 2, None]}, anotherNew="x")
    record = decode_payload(payload)
    assert type(record) is TYPED[type_name]
    expected = {"futureField": {"a": [1, 2, None]}, "anotherNew": "x"}
    if type_name == "Pump":
        expected["baby"] = payload["baby"]  # a pump's baby is kept raw, never read
    assert record.extra == expected
    assert record.to_payload() == payload
    again = decode_transaction(encode_transaction(record))
    assert again.to_payload() == payload


@pytest.mark.parametrize("type_name", ["Formula", "Pumped", "Pump", "Nursing", "Sleep"])
def test_non_ascii_notes_survive_the_round_trip(type_name: str) -> None:
    note = "Nuit calme — bébé dort ❤ 日本語"
    payload = payload_for(type_name, note=note)
    record = decode_transaction(encode_b64(payload))
    assert type(record) is TYPED[type_name]
    assert record.note == note
    assert json.loads(base64.b64decode(encode_transaction(record)))["note"] == note


# --- replays through the engine ---------------------------------------------------------------


def test_replaying_the_sleep_add_and_two_updates_leaves_one_finished_sleep() -> None:
    engine = SyncEngine()
    result = engine.apply(DEVICE, [raw(f) for f in by_prefix("C5")])
    assert result.applied == 3 and result.skipped == 0
    sleeps = [r for r in engine.records.values() if isinstance(r, Sleep)]
    assert len(sleeps) == 1
    sleep = sleeps[0]
    assert sleep.object_id == SLEEP_ID
    assert sleep.is_running is False
    assert sleep.duration == 1
    assert sleep.note == "C5"


def test_the_sleep_is_running_after_only_the_add() -> None:
    engine = SyncEngine()
    engine.apply(DEVICE, [raw(by_prefix("C5 sleep add")[0])])
    sleep = engine.records[SLEEP_ID]
    assert isinstance(sleep, Sleep)
    assert sleep.is_running is True


def test_a_sleep_update_arriving_before_its_add_still_wins() -> None:
    add, stop, note = by_prefix("C5")
    engine = SyncEngine()
    engine.apply(DEVICE, [raw(note)])
    engine.apply(OTHER_DEVICE, [raw(add)])
    sleep = engine.records[SLEEP_ID]
    assert isinstance(sleep, Sleep)
    assert (sleep.duration, sleep.note) == (1, "C5")
    assert not sleep.is_running


def test_a_sleep_update_alone_is_stored_as_a_finished_sleep() -> None:
    update = by_prefix("C5 sleep update at timer stop")[0]
    engine = SyncEngine()
    engine.apply(DEVICE, [raw(update)])
    sleep = engine.records[SLEEP_ID]
    assert isinstance(sleep, Sleep)
    assert sleep.is_running is False


def test_replaying_the_formula_add_and_its_delete_leaves_no_record() -> None:
    add = BY_LABEL["C1 formula add"]
    delete = BY_LABEL["C7 formula delete (timestamp not bumped)"]
    assert add["payload"]["timestamp"] == delete["payload"]["timestamp"]
    engine = SyncEngine()
    engine.apply(DEVICE, [raw(add), raw(delete)])
    assert FORMULA_ID not in engine.records
    assert dict(engine.records) == {}


def test_a_formula_delete_with_no_add_leaves_no_record() -> None:
    delete = BY_LABEL["C7 formula delete (timestamp not bumped)"]
    engine = SyncEngine()
    engine.apply(DEVICE, [raw(delete)])
    assert FORMULA_ID not in engine.records


def test_all_captured_fixtures_replay_in_one_log() -> None:
    engine = SyncEngine()
    result = engine.apply(DEVICE, [raw(f) for f in FIXTURES])
    assert result.applied == len(FIXTURES) and result.skipped == 0
    kinds = sorted(type(r).__name__ for r in engine.records.values())
    assert kinds == ["Diaper", "Nursing", "Pump", "Pump", "Pumped", "Sleep"]
    assert FORMULA_ID not in engine.records
    for record in engine.records.values():
        assert not isinstance(record, UnknownRecord)


def test_a_state_snapshot_of_the_captured_records_restores_them() -> None:
    engine = SyncEngine()
    engine.apply(DEVICE, [raw(f) for f in FIXTURES])
    restored = SyncEngine.from_dict(json.loads(json.dumps(engine.to_dict())))
    assert {i: r.to_payload() for i, r in restored.records.items()} == {
        i: r.to_payload() for i, r in engine.records.items()
    }
    pump = next(r for r in restored.records.values() if isinstance(r, Pump))
    assert pump.left_volume == Volume(2.0, "oz")


def test_op_constants_match_the_fixtures() -> None:
    assert BY_LABEL["C1 formula add"]["opcode"] == OP_ADD
    assert BY_LABEL["C5 sleep update at timer stop"]["opcode"] == OP_UPDATE
    assert BY_LABEL["C7 formula delete (timestamp not bumped)"]["opcode"] == OP_DELETE


def test_a_baby_payload_helper_is_unchanged() -> None:
    assert baby_payload()["BCObjectType"] == "Baby"
