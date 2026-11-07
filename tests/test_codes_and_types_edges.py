"""Edge cases for the Android value types, Medication, and NursingSession."""

from __future__ import annotations

from typing import Any

import pytest
from factories import encode_b64, payload_for

from pybabytracker import (
    Medication,
    NursingSession,
    decode_transaction,
    numeric_code,
)
from pybabytracker.models import VolumeMeasure


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("01", 1),
        pytest.param("1" * 5000, None, id="5000 digits"),  # past the int-string limit
        pytest.param("\u0661", None, id="arabic-indic digit"),  # the apps send ASCII only
        (" 1", None),
        ("+1", None),
        ("1.0", None),
        (1.0, None),
    ],
)
def test_numeric_code_reads_only_plain_ascii_digit_strings(
    value: Any, expected: int | None
) -> None:
    assert numeric_code(value) == expected


@pytest.mark.parametrize(
    ("english_measure", "unit"),
    [("true", "oz"), (True, "oz"), ("false", "ml"), (False, "ml"), (1, "ml"), (1.0, "ml")],
)
def test_only_true_or_the_string_true_means_ounces(english_measure: Any, unit: str) -> None:
    # 1 == True in Python, so a membership test would read the number 1 as ounces.
    measure = VolumeMeasure(
        raw={"BCObjectType": "VolumeMeasure", "value": 4, "englishMeasure": english_measure}
    )
    volume = measure.volume
    assert volume is not None
    assert volume.unit == unit


def test_a_medication_with_androids_prescription_spelling_round_trips() -> None:
    payload = payload_for("Medication")
    selection = dict(payload["medicationSelection"])
    del selection["isPresciption"]
    selection["prescription"] = True
    payload["medicationSelection"] = selection
    record = decode_transaction(encode_b64(payload))
    assert isinstance(record, Medication)
    assert record.medicine_name == "Test Medicine"
    assert record.to_payload() == payload


def test_a_nursing_session_keeps_its_type_name_and_timestamps() -> None:
    payload = payload_for("NursingSession", leftTimestamps=[1.5, 2.5], rightTimestamps=[3.5])
    record = decode_transaction(encode_b64(payload))
    assert isinstance(record, NursingSession)
    assert record.record_type == "NursingSession"
    assert record.extra["leftTimestamps"] == [1.5, 2.5]
    assert record.extra["rightTimestamps"] == [3.5]
    assert record.to_payload()["BCObjectType"] == "NursingSession"
