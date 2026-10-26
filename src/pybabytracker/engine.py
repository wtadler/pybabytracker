"""The sync engine: merges every device's log into the current set of records.

Rules:

- Records are keyed by ``objectID``. An add or update (opcodes 0 and 1) replaces the stored
  record. A delete (opcode 2) removes it, and removing a record never seen is a no-op.
- A device's transactions apply in ``SyncID`` order. The record's own ``timestamp`` is not
  consulted, so an update with an older timestamp than the stored record still applies: the
  log order is the truth.
- When several devices have new transactions in one sync, devices apply in ``DeviceUUID``
  order, so two devices editing one record always resolve the same way (the last applied
  wins).
- The engine tracks the highest ``SyncID`` seen per device, counting transactions it had to
  skip, and only fetches devices whose ``LastSyncID`` is ahead of that.
- A transaction that can't be decoded, has an unknown opcode, or has no ``objectID`` is
  skipped with a warning and never stops the sync. A record of an unknown type is kept raw.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .client import Device, RawTransaction
from .exceptions import TransactionDecodeError
from .models import (
    OP_ADD,
    OP_DELETE,
    OP_UPDATE,
    Baby,
    Record,
    UnknownRecord,
    decode_payload,
    decode_transaction,
)

_LOGGER = logging.getLogger(__name__)

STATE_VERSION = 1


class SyncSource(Protocol):
    """What the engine needs from a client. ``BabyTrackerClient`` satisfies it."""

    async def get_devices(self) -> list[Device]: ...

    async def get_transactions(
        self, device_uuid: str, since_sync_id: int
    ) -> list[RawTransaction]: ...


@dataclass(frozen=True, slots=True)
class ApplyResult:
    applied: int = 0
    skipped: int = 0


@dataclass(frozen=True, slots=True)
class SyncResult:
    devices_fetched: int = 0
    transactions_applied: int = 0
    transactions_skipped: int = 0


class SyncEngine:
    def __init__(self) -> None:
        self._records: dict[str, Record] = {}
        self._last_seen: dict[str, int] = {}

    @property
    def records(self) -> Mapping[str, Record]:
        """Current state: every surviving record, by ``objectID``."""
        return self._records

    @property
    def last_seen(self) -> Mapping[str, int]:
        """The highest transaction number seen, per device."""
        return self._last_seen

    def devices_to_fetch(self, devices: Iterable[Device]) -> list[Device]:
        """The devices whose log is ahead of what has been seen, in ``DeviceUUID`` order."""
        return sorted(
            (d for d in devices if d.last_sync_id > self._last_seen.get(d.device_uuid, 0)),
            key=lambda d: d.device_uuid,
        )

    async def sync(self, source: SyncSource) -> SyncResult:
        """Fetch and apply what's new. Progress on earlier devices survives a later failure."""
        devices = self.devices_to_fetch(await source.get_devices())
        applied = skipped = 0
        for device in devices:
            since = self._last_seen.get(device.device_uuid, 0)
            transactions = await source.get_transactions(device.device_uuid, since)
            result = self.apply(device.device_uuid, transactions)
            applied += result.applied
            skipped += result.skipped
        return SyncResult(len(devices), applied, skipped)

    def apply(self, device_uuid: str, transactions: Iterable[RawTransaction]) -> ApplyResult:
        """Apply one device's transactions. No I/O."""
        seen = self._last_seen.get(device_uuid, 0)
        pending = sorted((t for t in transactions if t.sync_id > seen), key=lambda t: t.sync_id)
        applied = skipped = 0
        for transaction in pending:
            if self._apply_one(device_uuid, transaction):
                applied += 1
            else:
                skipped += 1
            self._last_seen[device_uuid] = transaction.sync_id
        return ApplyResult(applied, skipped)

    def _apply_one(self, device_uuid: str, transaction: RawTransaction) -> bool:
        where = f"device {device_uuid} number {transaction.sync_id}"
        if transaction.op_code not in (OP_ADD, OP_UPDATE, OP_DELETE):
            _LOGGER.warning(
                "Skipping transaction with unknown opcode %s (%s)", transaction.op_code, where
            )
            return False
        try:
            record = decode_transaction(transaction.transaction)
        except TransactionDecodeError as err:
            _LOGGER.warning("Skipping undecodable transaction (%s): %s", where, err)
            return False
        if isinstance(record, UnknownRecord):
            _LOGGER.warning(
                "Unknown record type %r (%s); keeping the raw payload", record.record_type, where
            )
        if transaction.op_code == OP_DELETE:
            self._records.pop(record.object_id, None)
        else:
            self._records[record.object_id] = record
        return True

    def babies(self) -> dict[str, Baby]:
        """Every baby seen, by ID. A ``Baby`` record in the log wins over an embedded copy."""
        found: dict[str, Baby] = {}
        for record in self._records.values():
            if record.baby is not None:
                found[record.baby.object_id] = record.baby
        for record in self._records.values():
            if isinstance(record, Baby):
                found[record.object_id] = record
        return found

    def latest_records(self, baby_id: str) -> dict[str, Record]:
        """The newest record of each type for a baby, by the time it happened."""
        latest: dict[str, Record] = {}
        for record in self._records.values():
            if isinstance(record, Baby) or record.time is None or record.baby_id != baby_id:
                continue
            current = latest.get(record.record_type)
            if current is None or _sort_key(record) > _sort_key(current):
                latest[record.record_type] = record
        return latest

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe snapshot, for the caller to persist."""
        return {
            "version": STATE_VERSION,
            "last_seen": dict(sorted(self._last_seen.items())),
            "records": [r.to_payload() for _, r in sorted(self._records.items())],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SyncEngine:
        """Rebuild an engine from ``to_dict`` output."""
        if data.get("version") != STATE_VERSION:
            raise ValueError(f"unsupported engine state version: {data.get('version')!r}")
        engine = cls()
        engine._last_seen = {str(k): int(v) for k, v in data["last_seen"].items()}
        for payload in data["records"]:
            record = decode_payload(payload)
            engine._records[record.object_id] = record
        return engine


def _sort_key(record: Record) -> tuple[float, float]:
    assert record.time is not None
    stamp = record.timestamp.timestamp() if record.timestamp is not None else 0.0
    return (record.time.timestamp(), stamp)
