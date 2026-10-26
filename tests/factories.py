"""Invented transaction payloads for tests. Nothing here comes from a real account."""

from __future__ import annotations

import base64
import json
from typing import Any

BABY_ID = "11111111-AAAA-4AAA-8AAA-000000000001"
BABY_TWO_ID = "22222222-BBBB-4BBB-8BBB-000000000002"

TIME = "2026-10-25 15:40:49 +0000"
TIMESTAMP = "2026-10-26 15:41:01 +0000"

# Type-specific fields, written from the app's table columns (lowerCamelCase).
TYPE_FIELDS: dict[str, dict[str, Any]] = {
    "Diaper": {
        "status": 2,
        "amount": 2,
        "peeColor": 5,
        "pooColor": 5,
        "texture": 5,
        "flag": 0,
    },
    "Nursing": {
        "descID": "desc-1",
        "finishSide": 1,
        "leftDuration": 300,
        "rightDuration": 240,
        "bothDuration": 0,
    },
    "Formula": {"descID": "desc-1", "amount": 4.5, "isEnglishScale": 1},
    "Pumped": {"descID": "desc-1", "amount": 3.0, "isEnglishScale": 1, "pumpID": "pump-1"},
    "OtherFeed": {
        "descID": "desc-1",
        "typeID": "type-1",
        "amount": 2.5,
        "isEnglishScale": 1,
        "unit": "oz",
    },
    "Pump": {
        "finishSide": 2,
        "sides": 3,
        "amount": 6.0,
        "isEnglishScale": 1,
        "label": "Test Label",
        "pumpedID": "pumped-1",
        "leftAmount": 3.0,
        "rightAmount": 3.0,
        "leftDuration": 600,
        "rightDuration": 600,
    },
    "Sleep": {"duration": 3600, "locationID": "loc-1", "descID": "desc-1"},
    "Growth": {
        "weight": 7.5,
        "length": 21.0,
        "head": 14.0,
        "isEnglishLengthScale": 1,
        "isEnglishWeightScale": 1,
    },
    "Medicine": {"medID": "med-1", "amount": 2.5},
    "Temperature": {"temp": 98.6, "isEnglishScale": 1},
    "Vaccine": {"vaccID": "vacc-1", "docVisitID": "visit-1"},
    "Milestone": {"milestoneSelectionID": "milestone-1"},
    "OtherActivity": {"descID": "desc-1", "locationID": "loc-1", "duration": 900},
    "Bath": {"descID": "desc-1"},
    "Allergen": {"sourceID": "source-1", "reaction": "rash", "severity": 2, "notSure": 0},
    "HealthQuestion": {
        "question": "Test question?",
        "answer": "Test answer.",
        "doctorVisitID": "visit-1",
    },
    "Sick": {"symptom": "cough"},
    "DoctorVisit": {
        "isAppointment": 0,
        "visitType": 1,
        "doctorID": "doctor-1",
        "doctorNote": "Test note",
        "symptom": "none",
        "growthID": "growth-1",
        "sickID": "sick-1",
    },
    "Joy": {},
    "Journal": {},
}

ACTIVITY_TYPES = sorted(TYPE_FIELDS)
ALL_TYPES = ["Baby", *ACTIVITY_TYPES]


def baby_payload(
    name: str = "Test Baby", object_id: str = BABY_ID, **overrides: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "BCObjectType": "Baby",
        "objectID": object_id,
        "name": name,
        "dob": "2026-09-01 00:00:00 +0000",
        "gender": "true",
        "pictureName": "",
        "newFlage": "false",
        "timestamp": "1970-01-01 00:00:00 +0000",
    }
    payload.update(overrides)
    return payload


def payload_for(
    type_name: str,
    object_id: str = "AAAAAAAA-0000-4000-8000-000000000001",
    *,
    time: str = TIME,
    timestamp: str = TIMESTAMP,
    note: str = "",
    baby: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """A plausible payload for any record type."""
    if type_name == "Baby":
        return baby_payload(object_id=object_id, **overrides)
    payload: dict[str, Any] = {
        "BCObjectType": type_name,
        "objectID": object_id,
        "time": time,
        "timestamp": timestamp,
        "timezone": -18000,
        "note": note,
        "pictureNote": [],
        "newFlage": "true",
        "pictureLoaded": "true",
        **TYPE_FIELDS[type_name],
        "baby": baby if baby is not None else baby_payload(),
    }
    if type_name != "Diaper":
        payload["hasPicture"] = 0
    payload.update(overrides)
    return payload


def encode_b64(payload: Any) -> str:
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def wire(sync_id: int, op_code: int, payload: Any) -> dict[str, Any]:
    """One entry as the server returns it from the transaction log endpoint."""
    return {"SyncID": sync_id, "OPCode": op_code, "Transaction": encode_b64(payload)}
