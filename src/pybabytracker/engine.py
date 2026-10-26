"""STUB for the red run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .client import Device, RawTransaction
from .models import Baby, Record


@dataclass
class ApplyResult:
    applied: int = 0
    skipped: int = 0


@dataclass
class SyncResult:
    devices_fetched: int = 0
    transactions_applied: int = 0
    transactions_skipped: int = 0


class SyncEngine:
    records: dict[str, Record] = {}
    last_seen: dict[str, int] = {}

    def apply(self, device_uuid: str, transactions: list[RawTransaction]) -> ApplyResult:
        return ApplyResult()

    def devices_to_fetch(self, devices: list[Device]) -> list[Device]:
        return list(devices)

    async def sync(self, source: Any) -> SyncResult:
        return SyncResult()

    def to_dict(self) -> dict[str, Any]:
        return {}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SyncEngine:
        return cls()

    def babies(self) -> dict[str, Baby]:
        return {"x": Baby()}

    def latest_records(self, baby_id: str) -> dict[str, Record]:
        return {"x": Record()}
