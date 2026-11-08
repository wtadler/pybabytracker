from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from factories import ALL_TYPES, BABY_ID, baby_payload, encode_b64, payload_for

from pybabytracker import (
    Baby,
    Diaper,
    Growth,
    Measurement,
    Medication,
    Nursing,
    NursingSession,
    OtherActivity,
    OtherFeed,
    Sleep,
    TransactionDecodeError,
    UnknownRecord,
    decode_transaction,
    encode_transaction,
    format_time,
    from_unix,
    numeric_code,
    parse_time,
    to_unix,
)


def _round_trip(payload: dict[str, Any]) -> dict[str, Any]:
    record = decode_transaction(encode_b64(payload))
    result = json.loads(base64.b64decode(encode_transaction(record)))
    assert isinstance(result, dict)
    return result


@pytest.mark.parametrize("type_name", ALL_TYPES)
def test_every_known_type_round_trips_to_the_same_json(type_name: str) -> None:
    payload = payload_for(type_name)
    assert _round_trip(payload) == payload


@pytest.mark.parametrize("type_name", ALL_TYPES)
def test_every_known_type_decodes_to_its_own_class(type_name: str) -> None:
    record = decode_transaction(encode_b64(payload_for(type_name)))
    assert not isinstance(record, UnknownRecord)
    assert type(record).__name__ == type_name
    assert record.record_type == type_name


def test_diaper_fields_are_typed() -> None:
    record = decode_transaction(encode_b64(payload_for("Diaper", status=1, note="x")))
    assert isinstance(record, Diaper)
    assert record.status == 1
    assert record.note == "x"
    assert record.object_id == "AAAAAAAA-0000-4000-8000-000000000001"
    assert isinstance(record.baby, Baby)
    assert record.baby.name == "Test Baby"
    assert record.baby_id == BABY_ID


def test_sleep_duration_is_typed() -> None:
    record = decode_transaction(encode_b64(payload_for("Sleep", duration=1800)))
    assert isinstance(record, Sleep)
    assert record.duration == 1800


def test_baby_record_decodes() -> None:
    record = decode_transaction(encode_b64(baby_payload("Test Baby")))
    assert isinstance(record, Baby)
    assert record.name == "Test Baby"
    assert record.timestamp == datetime(1970, 1, 1, tzinfo=UTC)


def test_unknown_top_level_and_nested_keys_survive_the_round_trip() -> None:
    payload = payload_for("Diaper", futureField={"nested": [1, 2, {"a": None}]})
    payload["baby"]["futureBabyField"] = "kept"
    assert _round_trip(payload) == payload


def test_absent_optional_keys_stay_absent_and_explicit_nulls_stay_null() -> None:
    payload = payload_for("Diaper")
    del payload["pictureNote"]
    payload["note"] = None
    assert _round_trip(payload) == payload


def test_unknown_record_type_is_kept_raw() -> None:
    payload = {"BCObjectType": "Teleporter", "objectID": "X-1", "mass": 12, "note": "héllo"}
    record = decode_transaction(encode_b64(payload))
    assert isinstance(record, UnknownRecord)
    assert record.record_type == "Teleporter"
    assert record.object_id == "X-1"
    assert record.to_payload() == payload
    assert _round_trip(payload) == payload


def test_times_become_timezone_aware_datetimes() -> None:
    record = decode_transaction(encode_b64(payload_for("Diaper")))
    assert record.time == datetime(2026, 10, 25, 15, 40, 49, tzinfo=UTC)
    assert record.timestamp == datetime(2026, 10, 26, 15, 41, 1, tzinfo=UTC)
    assert record.time is not None
    assert record.time.utcoffset() == timedelta(0)


def test_non_utc_offsets_are_preserved_on_the_round_trip() -> None:
    payload = payload_for("Diaper", time="2026-10-25 10:40:49 -0500")
    record = decode_transaction(encode_b64(payload))
    assert record.time == datetime(2026, 10, 25, 10, 40, 49, tzinfo=timezone(timedelta(hours=-5)))
    assert _round_trip(payload) == payload


def test_time_helpers() -> None:
    assert parse_time("2026-10-25 15:40:49 +0000") == datetime(2026, 10, 25, 15, 40, 49, tzinfo=UTC)
    assert (
        format_time(datetime(2026, 10, 25, 15, 40, 49, tzinfo=UTC)) == "2026-10-25 15:40:49 +0000"
    )
    with pytest.raises(ValueError):
        format_time(datetime(2026, 10, 25, 15, 40, 49))
    with pytest.raises(ValueError):
        parse_time("yesterday")


def test_database_times_are_unix_seconds() -> None:
    moment = from_unix(1_792_942_849)
    assert moment == datetime(2026, 10, 25, 15, 40, 49, tzinfo=UTC)
    assert moment.tzinfo is not None
    assert to_unix(moment) == 1_792_942_849


def test_non_ascii_notes_round_trip() -> None:
    note = "Légère fuite 💧 日本語 — ça va"
    payload = payload_for("Diaper", note=note)
    record = decode_transaction(encode_b64(payload))
    assert record.note == note
    assert _round_trip(payload) == payload


def test_building_a_record_by_hand_encodes_a_valid_payload() -> None:
    record = Diaper(
        object_id="AAAAAAAA-0000-4000-8000-000000000009",
        time=datetime(2026, 10, 25, 15, 40, 49, tzinfo=UTC),
        status=0,
    )
    payload = record.to_payload()
    assert payload["BCObjectType"] == "Diaper"
    assert payload["objectID"] == "AAAAAAAA-0000-4000-8000-000000000009"
    assert payload["time"] == "2026-10-25 15:40:49 +0000"
    assert payload["status"] == 0
    assert "note" not in payload


def test_describe_lists_only_the_type_specific_fields_that_are_set() -> None:
    record = decode_transaction(encode_b64(payload_for("Diaper", status=1)))
    description = record.describe()
    assert description["status"] == 1
    assert "object_id" not in description
    assert "baby" not in description


def test_bad_base64_raises_a_decode_error() -> None:
    with pytest.raises(TransactionDecodeError):
        decode_transaction("!!! not base64 !!!")


def test_non_json_raises_a_decode_error() -> None:
    with pytest.raises(TransactionDecodeError):
        decode_transaction(base64.b64encode(b"hello there").decode("ascii"))


def test_invalid_utf8_raises_a_decode_error() -> None:
    with pytest.raises(TransactionDecodeError):
        decode_transaction(base64.b64encode(b"\xff\xfe\xfa").decode("ascii"))


def test_json_that_is_not_an_object_raises_a_decode_error() -> None:
    with pytest.raises(TransactionDecodeError):
        decode_transaction(encode_b64([1, 2, 3]))


def test_missing_object_id_raises_a_decode_error() -> None:
    payload = payload_for("Diaper")
    del payload["objectID"]
    with pytest.raises(TransactionDecodeError):
        decode_transaction(encode_b64(payload))


@pytest.mark.parametrize(
    "overrides",
    [
        {"time": "2026-10-25 15:40:49.5 +0000"},
        {"time": ""},
        {"time": "last tuesday"},
        {"baby": "not an object"},
    ],
)
def test_a_known_type_with_an_unexpected_value_is_kept_raw(overrides: dict[str, Any]) -> None:
    payload = payload_for("Sleep", **overrides)
    record = decode_transaction(encode_b64(payload))
    assert isinstance(record, UnknownRecord)
    assert record.record_type == "Sleep"
    assert record.to_payload() == payload
    assert _round_trip(payload) == payload


def test_a_solid_feed_reads_its_synced_food_and_quantity() -> None:
    payload = payload_for(
        "OtherFeed",
        feedType={
            "BCObjectType": "OtherFeedSelection",
            "objectID": "food-1",
            "name": "Test Food",
            "bottle": "false",
        },
        desc={"BCObjectType": "FeedDescription", "objectID": "desc-1"},
        amount={"BCObjectType": "VolumeMeasure", "englishMeasure": "true", "value": 2},
        unit="Pieces",
    )
    record = decode_transaction(encode_b64(payload))
    assert isinstance(record, OtherFeed)
    assert (record.food_name, record.quantity, record.unit) == ("Test Food", 2.0, "Pieces")
    assert _round_trip(payload) == payload


def test_a_solid_feed_with_a_plain_amount_and_no_food_still_reads() -> None:
    record = decode_transaction(encode_b64(payload_for("OtherFeed")))
    assert isinstance(record, OtherFeed)
    assert (record.food_name, record.quantity) == (None, 2.5)


def test_an_android_timer_session_is_a_nursing_record() -> None:
    # The Android app syncs a timed session as NursingSession; it is still a feed.
    record = decode_transaction(encode_b64(payload_for("NursingSession")))
    assert isinstance(record, NursingSession)
    assert isinstance(record, Nursing)
    assert (record.left_duration, record.finish_side) == (5, 1)
    assert record.extra["detailed"] is True


def test_a_medication_names_its_embedded_medicine() -> None:
    record = decode_transaction(encode_b64(payload_for("Medication")))
    assert isinstance(record, Medication)
    assert record.amount == 2.5
    assert record.medicine_name == "Test Medicine"


def test_a_medication_without_an_embedded_medicine_has_no_name() -> None:
    record = decode_transaction(encode_b64(payload_for("Medication", medicationSelection=None)))
    assert isinstance(record, Medication)
    assert record.medicine_name is None


@pytest.mark.parametrize(
    ("type_name", "key", "name_of", "expected"),
    [
        ("OtherActivity", "desc", lambda r: r.activity_name, "Tummy time"),
        ("Milestone", "milestoneType", lambda r: r.milestone_name, "Test Milestone"),
        ("Vaccine", "vaccineType", lambda r: r.vaccine_name, "Test Vaccine"),
    ],
)
def test_a_record_names_its_embedded_selection(
    type_name: str, key: str, name_of: Any, expected: str
) -> None:
    record = decode_transaction(encode_b64(payload_for(type_name)))
    assert name_of(record) == expected
    assert record.to_payload()[key]["name"] == expected
    assert key not in record.extra


@pytest.mark.parametrize(
    ("type_name", "key", "name_of"),
    [
        ("OtherActivity", "desc", lambda r: r.activity_name),
        ("Milestone", "milestoneType", lambda r: r.milestone_name),
        ("Vaccine", "vaccineType", lambda r: r.vaccine_name),
    ],
)
@pytest.mark.parametrize("selection", [None, "Tummy time", {"name": 7}, {}])
def test_a_record_without_a_named_selection_has_no_name(
    type_name: str, key: str, name_of: Any, selection: object
) -> None:
    overrides: dict[str, Any] = {key: selection}
    record = decode_transaction(encode_b64(payload_for(type_name, **overrides)))
    assert name_of(record) is None


def test_an_activity_keeps_its_duration_in_minutes() -> None:
    record = decode_transaction(encode_b64(payload_for("OtherActivity")))
    assert isinstance(record, OtherActivity)
    assert record.duration == 15


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, 1),
        ("1", 1),
        ("12", 12),
        (0, 0),
        ("0", 0),
        (True, None),
        (1.5, None),
        ("", None),
        ("-1", None),
        ("left", None),
        (None, None),
    ],
)
def test_numeric_codes_read_ints_and_androids_digit_strings(
    value: Any, expected: int | None
) -> None:
    assert numeric_code(value) == expected


def test_a_growth_reads_its_synced_measures() -> None:
    record = decode_transaction(encode_b64(payload_for("Growth")))
    assert isinstance(record, Growth)
    assert record.weight_reading == Measurement(7.5, "lb")
    assert record.length_reading == Measurement(21.0, "in")
    assert record.head_reading == Measurement(14.0, "in")


@pytest.mark.parametrize(
    ("english", "metric"), [("false", True), (False, True), (None, True), (1, True), ("1", True)]
)
def test_a_metric_growth_reads_kilograms_and_centimeters(english: Any, metric: bool) -> None:
    def measure(kind: str, value: float) -> dict[str, Any]:
        found: dict[str, Any] = {"BCObjectType": kind, "value": value}
        if english is not None:
            found["englishMeasure"] = english
        return found

    payload = payload_for(
        "Growth",
        weight=measure("WeightMeasure", 3.4),
        length=measure("LengthMeasure", 53.0),
        head=measure("LengthMeasure", 35.5),
    )
    record = decode_transaction(encode_b64(payload))
    assert isinstance(record, Growth)
    assert (record.weight_reading, record.length_reading, record.head_reading) == (
        Measurement(3.4, "kg"),
        Measurement(53.0, "cm"),
        Measurement(35.5, "cm"),
    )
    assert _round_trip(payload) == payload


def test_an_android_growth_with_boolean_units_reads_pounds() -> None:
    payload = payload_for(
        "Growth",
        weight={"BCObjectType": "WeightMeasure", "englishMeasure": True, "value": 9.25},
    )
    record = decode_transaction(encode_b64(payload))
    assert isinstance(record, Growth)
    assert record.weight_reading == Measurement(9.25, "lb")


@pytest.mark.parametrize(("scale", "unit"), [(1, "lb"), (0, "kg")])
def test_a_growth_in_the_database_shape_reads_its_scale(scale: int, unit: str) -> None:
    payload = payload_for("Growth", weight=7.5, isEnglishWeightScale=scale, length=None, head=None)
    record = decode_transaction(encode_b64(payload))
    assert isinstance(record, Growth)
    assert record.weight_reading == Measurement(7.5, unit)
    assert record.length_reading is None


@pytest.mark.parametrize(
    "weight",
    [
        None,
        {"BCObjectType": "WeightMeasure", "englishMeasure": "true"},
        {"BCObjectType": "WeightMeasure", "value": "7.5"},
        {"BCObjectType": "WeightMeasure", "value": True},
        7.5,  # a plain number without a 0 or 1 scale has no unit
    ],
)
def test_a_growth_without_a_readable_weight_has_no_reading(weight: Any) -> None:
    record = decode_transaction(encode_b64(payload_for("Growth", weight=weight)))
    assert isinstance(record, Growth)
    assert record.weight_reading is None
