"""STUB for the red run."""

from __future__ import annotations

from datetime import datetime
from typing import Any

OP_ADD = 0
OP_UPDATE = 1
OP_DELETE = 2


class Record:
    record_type = ""

    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)

    def to_payload(self) -> dict[str, Any]:
        return {}

    def describe(self) -> dict[str, Any]:
        return {"object_id": 1}


class Baby(Record): ...
class Diaper(Record): ...
class Sleep(Record): ...
class UnknownRecord(Record): ...


def decode_transaction(value: str) -> Record:
    return Record()


def encode_transaction(record: Record) -> str:
    return ""


def parse_time(value: str) -> datetime:
    return datetime(1970, 1, 1)


def format_time(value: datetime) -> str:
    return ""


def from_unix(value: float) -> datetime:
    return datetime(1970, 1, 1)


def to_unix(value: datetime) -> float:
    return 0.0
