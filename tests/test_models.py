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
    Sleep,
    TransactionDecodeError,
    UnknownRecord,
    decode_transaction,
    encode_transaction,
    format_time,
    from_unix,
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


def test_unparseable_time_raises_a_decode_error() -> None:
    with pytest.raises(TransactionDecodeError):
        decode_transaction(encode_b64(payload_for("Diaper", time="last tuesday")))
