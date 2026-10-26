"""The sync engine: merges every device's log into the current set of records.

Rules:

- Records are keyed by ``objectID``. An add or update (opcodes 0 and 1) stores the record. A
  delete (opcode 2) removes it and leaves a tombstone, so a delete that arrives before the add
  it targets still wins.
- Each stored record and tombstone carries a version: the record's ``timestamp``, the op rank
  (add 0, update 1, delete 2), the device, and the sync number. The rank breaks a tie at an
  equal timestamp, because an update or delete necessarily comes after the add it follows.
  Within one device's log, later numbers always win (log order). Across devices the result
  doesn't depend on which device the engine fetched first:

  - An add or update replaces a stored record if its ``(timestamp, rank, device, number)`` is
    newer.
  - A delete removes a stored record unless the record's timestamp is newer than the delete's.
  - An add or update from another device brings a deleted record back only with a strictly
    newer timestamp than the tombstone.

- A transaction that can't be decoded, has an unknown opcode, or has no ``objectID`` is
  skipped with a warning and never stops the sync. A record of an unknown type, or of a known
  type with values the models don't understand, is kept raw.
- The engine tracks the highest number seen per device, counting skipped transactions, and
  only fetches devices whose ``LastSyncID`` is ahead of that. A fresh ``SyncEngine`` replays
  every log, so it recovers skipped payloads once a library fix ships.
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
    RECORD_TYPES,
    Baby,
    Record,
    UnknownRecord,
    decode_payload,
    decode_transaction,
)

_LOGGER = logging.getLogger(__name__)

STATE_VERSION = 3

_RANK_ADD, _RANK_UPDATE, _RANK_DELETE = 0, 1, 2
_OP_RANKS = {OP_ADD: _RANK_ADD, OP_UPDATE: _RANK_UPDATE, OP_DELETE: _RANK_DELETE}


class SyncSource(Protocol):
    """What the engine needs from a client. ``BabyTrackerClient`` satisfies it."""

    async def get_devices(self) -> list[Device]: ...

    async def get_transactions(
        self, device_uuid: str, since_sync_id: int
    ) -> list[RawTransaction]: ...


@dataclass(frozen=True, slots=True)
class _Version:
    timestamp: float
    rank: int
    device_uuid: str
    sync_id: int

    def key(self) -> tuple[float, int, str, int]:
        return (self.timestamp, self.rank, self.device_uuid, self.sync_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "rank": self.rank,
            "device": self.device_uuid,
            "sync_id": self.sync_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], default_rank: int) -> _Version:
        """Version 2 state has no rank; ``default_rank`` stands in for it."""
        return cls(
            float(data["timestamp"]),
            int(data.get("rank", default_rank)),
            str(data["device"]),
            int(data["sync_id"]),
        )


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
        self._versions: dict[str, _Version] = {}
        self._tombstones: dict[str, _Version] = {}
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
        if isinstance(record, UnknownRecord) and record.record_type not in RECORD_TYPES:
            _LOGGER.warning(
                "Unknown record type %r (%s); keeping the raw payload", record.record_type, where
            )
        stamp = record.timestamp.timestamp() if record.timestamp is not None else 0.0
        version = _Version(stamp, _OP_RANKS[transaction.op_code], device_uuid, transaction.sync_id)
        if transaction.op_code == OP_DELETE:
            self._delete(record.object_id, version)
        else:
            self._store(record, version)
        return True

    def _store(self, record: Record, version: _Version) -> None:
        object_id = record.object_id
        tombstone = self._tombstones.get(object_id)
        if tombstone is not None:
            same_device = tombstone.device_uuid == version.device_uuid
            if not same_device and version.timestamp <= tombstone.timestamp:
                return
            del self._tombstones[object_id]
        current = self._versions.get(object_id)
        if current is not None and not _newer(version, current):
            return
        self._records[object_id] = record
        self._versions[object_id] = version

    def _delete(self, object_id: str, version: _Version) -> None:
        current = self._versions.get(object_id)
        if current is not None:
            same_device = current.device_uuid == version.device_uuid
            if not same_device and version.timestamp < current.timestamp:
                return  # the record was edited after this delete
            del self._records[object_id]
            del self._versions[object_id]
        tombstone = self._tombstones.get(object_id)
        if tombstone is None or _newer(version, tombstone):
            self._tombstones[object_id] = version

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
            "records": [
                {"version": self._versions[i].to_dict(), "payload": r.to_payload()}
                for i, r in sorted(self._records.items())
            ],
            "tombstones": [
                {"object_id": i, "version": v.to_dict()}
                for i, v in sorted(self._tombstones.items())
            ],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SyncEngine:
        """Rebuild an engine from ``to_dict`` output. Versions 1 and 2 still load.

        Version 1 has no tombstones. Version 2 has no op rank, so its records load as adds and
        its tombstones as deletes.
        """
        version = data.get("version")
        if version not in (1, 2, STATE_VERSION):
            raise ValueError(f"unsupported engine state version: {version!r}")
        engine = cls()
        engine._last_seen = {str(k): int(v) for k, v in data["last_seen"].items()}
        for entry in data["records"]:
            if version == 1:
                record = decode_payload(entry)
                stamp = record.timestamp.timestamp() if record.timestamp is not None else 0.0
                stored = _Version(stamp, _RANK_ADD, "", 0)
            else:
                record = decode_payload(entry["payload"])
                stored = _Version.from_dict(entry["version"], _RANK_ADD)
            engine._records[record.object_id] = record
            engine._versions[record.object_id] = stored
        for entry in data.get("tombstones", []):
            engine._tombstones[str(entry["object_id"])] = _Version.from_dict(
                entry["version"], _RANK_DELETE
            )
        return engine


def _newer(new: _Version, old: _Version) -> bool:
    """Whether ``new`` replaces ``old``: log order within a device, version key across devices."""
    if new.device_uuid == old.device_uuid:
        return new.sync_id > old.sync_id
    return new.key() > old.key()


def _sort_key(record: Record) -> tuple[float, float]:
    assert record.time is not None
    stamp = record.timestamp.timestamp() if record.timestamp is not None else 0.0
    return (record.time.timestamp(), stamp)
