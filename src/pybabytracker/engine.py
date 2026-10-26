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
  only fetches devices whose ``LastSyncID`` is ahead of that. The service returns at most 500
  transactions per read, so ``sync`` keeps reading each device from its highest number until it
  reaches the device's ``LastSyncID`` (as listed at the start of the sync), a read comes back
  empty, or a read brings nothing new (a warning, so a stuck server can't loop forever). A
  fresh ``SyncEngine`` replays every log, so it recovers skipped payloads once a library fix ships.

Writes (the ``add_*`` methods, ``start_sleep``, ``end_sleep``, and ``delete_record``) post as this
engine's own device:

- A write needs a completed sync (for the baby record and this device's numbers) and a client
  signed in as the engine's ``device_uuid``. Otherwise it's refused before any request.
- Numbers are at-most-once. The next number is one more than the highest of: this device's
  ``LastSyncID`` on the server, the highest number of this device's log already applied, and
  the last number this engine posted. Before choosing, the engine applies any of its own log it
  hasn't seen, so its own log stays contiguous. If that log is shorter than ``LastSyncID``, the
  write is refused (``ProtocolError``) rather than skipping the missing entries.
- The outgoing transaction is held as "pending" until the post is confirmed. If a post fails,
  the engine reads its own log entry at that number: if the exact same transaction is there, the
  post landed and the write succeeds. If not, the error is raised and the post stays pending.
  The next write settles it first, catching up through whatever now holds that number:
  - After a definite HTTP error, a number nothing holds is free and is reused.
  - After no answer at all (``NoResponseError``: a timeout or dropped connection), the post may
    still land, so the number is reused only once ``unknown_outcome_grace`` seconds have passed
    since the failure with nothing under it. Until then a write raises
    ``WriteOutcomeUnknownError`` without posting.
  A post is never resent blindly.
- A retry of the same logical add can pass the same ``object_id``. If the earlier attempt
  landed after all, the existing record is returned instead of posting a second copy.
- A confirmed write is applied to local state at once, under its own number, so the next sync
  sees nothing new from this device and doesn't fetch or reapply it.
- Writes and syncs share one lock, so concurrent writes get consecutive numbers.
- Only records this device created can be deleted. Every add in this device's log counts,
  whether it was applied locally or read back by a sync. Like the app, a delete sends the
  record exactly as last saved, with only ``newFlage`` set to ``"false"``: its timestamp is not
  bumped.
- The only edit (opcode 1) is ``end_sleep``, which ends a running sleep whichever device started
  it. An update shares the numbering, pending, and grace-window rules above, but it isn't an add:
  it never makes a record this device's own (``created_ids``, ``own_writes``). Its local apply
  is the same merge a replay of the log performs.

What the caller must do:

- Save ``to_dict()`` after every write, including one that raised: the pending post lives only
  in that state. If the process dies mid-post without saving, recovery falls back on the
  server's ``LastSyncID``, which can lag.
- Use one device ID per running process (one per Home Assistant config entry). Two processes
  writing as the same device can collide on a number while ``LastSyncID`` lags; the settle
  check narrows that window but can't close it.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .builders import (
    Draft,
    draft_bottle,
    draft_diaper,
    draft_nursing,
    draft_pump,
    draft_sleep,
    draft_sleep_start,
    ended_sleep_payload,
    resolve_sleep_end,
)
from .client import Device, RawTransaction
from .exceptions import (
    AuthenticationError,
    BabyTrackerError,
    NoResponseError,
    NotOwnRecordError,
    NotSyncedError,
    ObjectIdInUseError,
    ProtocolError,
    RecordNotFoundError,
    TimerNotRunningError,
    TransactionDecodeError,
    UnknownBabyError,
    WriteOutcomeUnknownError,
    WrongRecordTypeError,
)
from .models import (
    OP_ADD,
    OP_DELETE,
    OP_UPDATE,
    RECORD_TYPES,
    Baby,
    Diaper,
    Formula,
    Nursing,
    Pump,
    Pumped,
    Record,
    Sleep,
    UnknownRecord,
    Volume,
    decode_payload,
    decode_transaction,
    encode_transaction,
)

_LOGGER = logging.getLogger(__name__)

STATE_VERSION = 4

_RANK_ADD, _RANK_UPDATE, _RANK_DELETE = 0, 1, 2
_OP_RANKS = {OP_ADD: _RANK_ADD, OP_UPDATE: _RANK_UPDATE, OP_DELETE: _RANK_DELETE}


class SyncSource(Protocol):
    """What the engine needs from a client. ``BabyTrackerClient`` satisfies it."""

    async def get_devices(self) -> list[Device]: ...

    async def get_transactions(
        self, device_uuid: str, since_sync_id: int
    ) -> list[RawTransaction]: ...


class WriteTarget(SyncSource, Protocol):
    """What the engine needs to write. ``BabyTrackerClient`` satisfies it."""

    @property
    def device_uuid(self) -> str: ...

    async def post_transaction(self, op_code: int, sync_id: int, transaction: str) -> None: ...


@dataclass(frozen=True, slots=True)
class _Pending:
    """A transaction posted, or about to be, whose landing isn't confirmed yet."""

    sync_id: int
    op_code: int
    transaction: str
    # Unix time the post went unanswered; None after a definite HTTP error (or before posting).
    unknown_since: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sync_id": self.sync_id,
            "op_code": self.op_code,
            "transaction": self.transaction,
            "unknown_since": self.unknown_since,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> _Pending:
        since = data.get("unknown_since")
        return cls(
            int(data["sync_id"]),
            int(data["op_code"]),
            str(data["transaction"]),
            float(since) if since is not None else None,
        )


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
    """Merged state of every device's log, plus this device's outgoing writes.

    ``device_uuid`` is the device this engine writes as. It must match the client's. An engine
    without one can sync but not write. ``unknown_outcome_grace`` is how long, in seconds, a
    post that got no answer blocks reuse of its number; make it a few times the client's request
    timeout.
    """

    def __init__(
        self, device_uuid: str | None = None, *, unknown_outcome_grace: float = 90.0
    ) -> None:
        self._device_uuid = device_uuid
        self._unknown_outcome_grace = unknown_outcome_grace
        self._records: dict[str, Record] = {}
        self._versions: dict[str, _Version] = {}
        self._tombstones: dict[str, _Version] = {}
        self._last_seen: dict[str, int] = {}
        self._synced = False
        self._last_posted = 0
        self._pending: _Pending | None = None
        self._created: set[str] = set()
        self._add_numbers: dict[str, int] = {}  # object_id -> number of this device's add
        self._own_updates: set[str] = set()  # objects this device posted an update (an end) to
        self._lock = asyncio.Lock()

    @property
    def device_uuid(self) -> str | None:
        return self._device_uuid

    @property
    def records(self) -> Mapping[str, Record]:
        """Current state: every surviving record, by ``objectID``."""
        return self._records

    @property
    def last_seen(self) -> Mapping[str, int]:
        """The highest transaction number seen, per device."""
        return self._last_seen

    @property
    def created_ids(self) -> frozenset[str]:
        """IDs of every record this device added. Only these can be deleted."""
        return frozenset(self._created)

    def own_writes(self) -> list[tuple[str, int]]:
        """This device's adds that still exist, as ``(object_id, sync_id)``, oldest first.

        ``sync_id`` is the number of this device's add (the newest one, if the same record was
        added again). It doesn't change when another device edits the record, so the last pair
        is always the record this device added most recently. State saved before numbers were
        kept loads with the number recovered from the record's version when this device's add is
        still its newest version, and 0 (oldest) otherwise; a record without a number sorts
        among the 0s by ``object_id``. Cheap: no state is encoded.
        """
        numbers = self._add_numbers
        return sorted(
            ((i, numbers.get(i, 0)) for i in self._created if i in self._records),
            key=lambda pair: (pair[1], pair[0]),
        )

    async def add_diaper(
        self,
        target: WriteTarget,
        baby_id: str,
        status: int,
        *,
        time: datetime | None = None,
        timezone_offset: int | None = None,
        note: str = "",
        object_id: str | None = None,
    ) -> Diaper:
        """Post a new diaper for a baby known from sync, and apply it locally.

        ``status`` is ``DIAPER_WET``, ``DIAPER_DIRTY``, or ``DIAPER_MIXED``. ``time`` (aware)
        defaults to now. ``timezone_offset`` (seconds east of UTC) defaults to ``time``'s offset.
        ``object_id`` defaults to a new uppercase UUID4. Pass the same one to retry a write that
        raised: if that write landed after all, its record is returned and nothing is posted.
        Every ``add_*`` method shares these rules. Bad input raises ``ValueError`` before any
        request.
        """
        draft = draft_diaper(status, time=time, timezone_offset=timezone_offset, note=note)
        return await self._add(target, baby_id, draft, object_id)

    async def add_formula(
        self,
        target: WriteTarget,
        baby_id: str,
        *,
        volume: Volume,
        time: datetime | None = None,
        timezone_offset: int | None = None,
        note: str = "",
        object_id: str | None = None,
    ) -> Formula:
        """Post a formula bottle of ``volume`` (more than zero), at ``time`` (default now)."""
        draft = draft_bottle(Formula, volume, time=time, timezone_offset=timezone_offset, note=note)
        return await self._add(target, baby_id, draft, object_id)

    async def add_pumped(
        self,
        target: WriteTarget,
        baby_id: str,
        *,
        volume: Volume,
        time: datetime | None = None,
        timezone_offset: int | None = None,
        note: str = "",
        object_id: str | None = None,
    ) -> Pumped:
        """Post a bottle of pumped milk. Takes what ``add_formula`` takes."""
        draft = draft_bottle(Pumped, volume, time=time, timezone_offset=timezone_offset, note=note)
        return await self._add(target, baby_id, draft, object_id)

    async def add_nursing(
        self,
        target: WriteTarget,
        baby_id: str,
        *,
        left_minutes: int = 0,
        right_minutes: int = 0,
        finish_side: int | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        timezone_offset: int | None = None,
        note: str = "",
        object_id: str | None = None,
    ) -> Nursing:
        """Post a finished nursing session of whole minutes per side (at least one in total).

        The record's ``time`` is the session's start, as in the capture: ``start_time`` if
        given, else ``end_time`` minus the total minutes, else now minus the total minutes
        (the session just ended). Give at most one of them. ``finish_side`` (``SIDE_LEFT`` or
        ``SIDE_RIGHT``) defaults to the last side used, right when both were.
        """
        draft = draft_nursing(
            left_minutes=left_minutes,
            right_minutes=right_minutes,
            finish_side=finish_side,
            start_time=start_time,
            end_time=end_time,
            timezone_offset=timezone_offset,
            note=note,
        )
        return await self._add(target, baby_id, draft, object_id)

    async def add_sleep(
        self,
        target: WriteTarget,
        baby_id: str,
        *,
        start: datetime,
        end: datetime,
        timezone_offset: int | None = None,
        note: str = "",
        object_id: str | None = None,
    ) -> Sleep:
        """Post a finished sleep. ``start`` and ``end`` are aware, with ``end`` after ``start``.

        The record's ``time`` is ``start``. ``duration`` is the length in whole minutes, rounded
        half up (the capture stored about 30 seconds as 1), at least 1.
        """
        draft = draft_sleep(start=start, end=end, timezone_offset=timezone_offset, note=note)
        return await self._add(target, baby_id, draft, object_id)

    async def add_pump(
        self,
        target: WriteTarget,
        baby_id: str,
        *,
        left_volume: Volume | None = None,
        right_volume: Volume | None = None,
        left_minutes: int = 0,
        right_minutes: int = 0,
        finish_side: int | None = None,
        time: datetime | None = None,
        timezone_offset: int | None = None,
        note: str = "",
        object_id: str | None = None,
    ) -> Pump:
        """Post a pumping session, with the volume of at least one side (one unit for both).

        The total is the sum of the two sides; a side left out is written as 0. ``time`` is the
        session's start (default now). ``finish_side`` defaults as in ``add_nursing``.
        """
        draft = draft_pump(
            left_volume=left_volume,
            right_volume=right_volume,
            left_minutes=left_minutes,
            right_minutes=right_minutes,
            finish_side=finish_side,
            time=time,
            timezone_offset=timezone_offset,
            note=note,
        )
        return await self._add(target, baby_id, draft, object_id)

    async def start_sleep(
        self,
        target: WriteTarget,
        baby_id: str,
        *,
        start: datetime | None = None,
        timezone_offset: int | None = None,
        note: str = "",
        object_id: str | None = None,
    ) -> Sleep:
        """Post a running sleep: an add with ``duration`` 0, as the app posts at a timer's start.

        ``start`` (aware) defaults to now. It can't be more than a minute in the future or more
        than 24 hours ago. End the sleep with ``end_sleep``. A retry with the same ``object_id``
        works as for every ``add_*`` method.
        """
        draft = draft_sleep_start(start=start, timezone_offset=timezone_offset, note=note)
        return await self._add(target, baby_id, draft, object_id)

    async def end_sleep(
        self, target: WriteTarget, object_id: str, *, end: datetime | None = None
    ) -> Sleep:
        """End a running sleep, whichever device started it: post an update (opcode 1).

        This is the one edit the library makes to a record it may not have created. Like the
        app, the update carries the record as last saved with only ``duration``, ``newFlage``
        "false", and ``timestamp`` changed. The timestamp is the write time, or one second past
        the stored one if that is ahead of this clock, so the update wins the merge on every
        device.

        ``end`` (aware) defaults to now. The duration is whole minutes, rounded as in
        ``add_sleep`` (see ``builders.resolve_sleep_end``): an end more than a minute in the
        future is clamped to now, an end at or up to a minute before the start (a clock running
        fast) is 1 minute, and a sleep longer than 24 hours is 1,440 minutes; each clamp is
        logged. An end further before the start raises ``ValueError``.

        Refused before any request: ``RecordNotFoundError`` for a record never seen or deleted,
        ``WrongRecordTypeError`` for a record that isn't a ``Sleep``, ``TimerNotRunningError``
        for a finished sleep. A finished sleep this device ended (its own end update is in this
        device's log, whatever other devices edited since) is returned as is with nothing posted,
        so a retry after a write that landed late, or a second call racing the first, is safe.
        """
        object_id = object_id.upper()
        async with self._lock:
            self._check_can_write(target)
            sleep, ended = self._endable(object_id)
            if ended:
                return sleep
            now = datetime.now(UTC)
            resolve_sleep_end(sleep, end if end is not None else now, now)  # refuse bad input
            number = await self._prepare(target)
            # Catching up this device's own log may have brought in an earlier end that landed.
            sleep, ended = self._endable(object_id)
            if ended:
                return sleep
            now = datetime.now(UTC)
            finish = resolve_sleep_end(sleep, end if end is not None else now, now)
            if finish.future_clamped:
                _LOGGER.warning("The end of sleep %s is in the future; ending it now", object_id)
            if finish.long_clamped:
                _LOGGER.warning("Sleep %s ran longer than 24 hours; writing 24 hours", object_id)
            stamp = _now()
            if sleep.timestamp is not None and sleep.timestamp >= stamp:
                stamp = sleep.timestamp.astimezone(UTC) + timedelta(seconds=1)
            payload = ended_sleep_payload(sleep, finish.minutes, stamp)
            ended_sleep = decode_payload(payload)
            assert isinstance(ended_sleep, Sleep)
            await self._send(target, number, OP_UPDATE, encode_transaction(ended_sleep))
            return ended_sleep

    def _endable(self, object_id: str) -> tuple[Sleep, bool]:
        """The sleep ``object_id`` names and whether this device already ended it (its own end
        update is in this device's log); else raise why it can't be ended."""
        record = self._records.get(object_id)
        if record is None:
            if object_id in self._tombstones:
                raise RecordNotFoundError(f"record {object_id} was deleted")
            raise RecordNotFoundError(f"no record {object_id} is known from sync")
        if not isinstance(record, Sleep):
            raise WrongRecordTypeError(
                f"record {object_id} is a {record.record_type or 'record'}, not a Sleep"
            )
        if record.is_running:
            return record, False
        if object_id in self._own_updates:
            return record, True
        raise TimerNotRunningError(f"sleep {object_id} isn't running")

    async def _add[R: Record](
        self, target: WriteTarget, baby_id: str, draft: Draft[R], object_id: str | None
    ) -> R:
        """The shared write path of every add: number, post, confirm, apply."""
        if object_id is not None:
            object_id = object_id.upper()
        async with self._lock:
            self._check_can_write(target)
            baby = self.babies().get(baby_id)
            if baby is None:
                raise UnknownBabyError(f"no baby {baby_id} is known from sync")
            number = await self._prepare(target)
            if object_id is not None:
                if object_id in self._created:
                    existing = self._records.get(object_id)
                    if isinstance(existing, draft.record_type):
                        return existing  # an earlier attempt of this write landed
                    if existing is not None:
                        raise ObjectIdInUseError(
                            f"object_id {object_id} belongs to a record of another type"
                        )
                    raise RecordNotFoundError(f"record {object_id} was added and then deleted")
                if object_id in self._records or object_id in self._tombstones:
                    raise ObjectIdInUseError(f"object_id {object_id} belongs to another record")
            record = draft.build(object_id or str(uuid.uuid4()).upper(), baby, _now())
            await self._send(target, number, OP_ADD, encode_transaction(record))
            return record

    async def delete_record(self, target: WriteTarget, object_id: str) -> None:
        """Post a delete of a record this device created, carrying the full record.

        The payload is the record exactly as last saved with ``newFlage`` set to ``"false"``;
        like the app, the timestamp is not bumped. Raises ``NotOwnRecordError`` for a record
        another device created (or one never seen), and ``RecordNotFoundError`` if this
        device's record is already deleted.
        """
        async with self._lock:
            self._check_can_write(target)
            if object_id not in self._created:
                raise NotOwnRecordError(f"record {object_id} wasn't created by this device")
            if object_id not in self._records:
                raise RecordNotFoundError(f"record {object_id} is already deleted")
            number = await self._prepare(target)
            record = self._records.get(object_id)
            if record is None:
                raise RecordNotFoundError(f"record {object_id} is already deleted")
            payload = record.to_payload()
            payload["newFlage"] = "false"
            await self._send(target, number, OP_DELETE, encode_transaction(decode_payload(payload)))

    def _check_can_write(self, target: WriteTarget) -> None:
        if self._device_uuid is None:
            raise ValueError("this engine has no device_uuid, so it can't write")
        if target.device_uuid != self._device_uuid:
            raise ValueError("the client's device ID doesn't match the engine's device_uuid")
        if not self._synced:
            raise NotSyncedError("sync at least once before writing")

    async def _prepare(self, target: WriteTarget) -> int:
        """Settle any earlier failed post, catch up on this device's log, and choose the next
        number. Hold the lock."""
        own = self._require_device()
        server_last = await self._server_last(target)
        pending = self._pending
        if pending is not None:
            # If anything now holds the pending number (that post, landed late, or another
            # process's), catch up through it even if LastSyncID lags, so it can't be reused.
            entry = await self._own_entry(target, pending.sync_id)
            if entry is not None:
                if self._matches_pending(entry):
                    _LOGGER.info("An earlier post (number %s) did land", entry.sync_id)
                server_last = max(server_last, entry.sync_id)
            elif (
                pending.unknown_since is not None
                and _time.time() - pending.unknown_since < self._unknown_outcome_grace
            ):
                raise WriteOutcomeUnknownError(
                    f"the previous write's outcome isn't known yet (number {pending.sync_id} "
                    "got no answer and may still land); try again shortly"
                )
            self._pending = None
        await self._catch_up_own(target, server_last)
        return max(server_last, self._last_seen.get(own, 0), self._last_posted) + 1

    async def _send(self, target: WriteTarget, number: int, op_code: int, transaction: str) -> None:
        """Post under ``number``, confirm, and apply locally. Hold the lock."""
        own = self._require_device()
        self._pending = _Pending(number, op_code, transaction)
        try:
            await target.post_transaction(op_code, number, transaction)
        except AuthenticationError:
            self._pending = None  # rejected outright: nothing landed
            raise
        except BabyTrackerError as err:
            # Did it land anyway? Only the exact transaction under this number counts.
            try:
                entry = await self._own_entry(target, number)
            except BabyTrackerError:
                entry = None
            if entry is None or not self._matches_pending(entry):
                if isinstance(err, NoResponseError):
                    self._pending = replace(self._pending, unknown_since=_time.time())
                raise
        self._pending = None
        self._last_posted = max(self._last_posted, number)
        self.apply(own, [RawTransaction(number, op_code, transaction)])

    async def _server_last(self, target: WriteTarget) -> int:
        own = self._require_device()
        for device in await target.get_devices():
            if device.device_uuid == own:
                return device.last_sync_id
        return 0  # the service lists a device only after its first post

    async def _own_entry(self, target: WriteTarget, sync_id: int) -> RawTransaction | None:
        """This device's log entry under ``sync_id``, if the service has one."""
        entries = await target.get_transactions(self._require_device(), sync_id - 1)
        return next((e for e in entries if e.sync_id == sync_id), None)

    def _matches_pending(self, entry: RawTransaction) -> bool:
        pending = self._pending
        assert pending is not None
        if entry.op_code == pending.op_code and entry.transaction == pending.transaction:
            return True
        _LOGGER.warning(
            "Number %s in this device's log holds a different transaction; "
            "another process may be using the same device ID",
            entry.sync_id,
        )
        return False

    async def _catch_up_own(self, target: WriteTarget, server_last: int) -> None:
        """Apply this device's log up to ``server_last``, so local numbering stays contiguous."""
        own = self._require_device()
        await self._read_through(target, own, server_last)
        if (seen := self._last_seen.get(own, 0)) < server_last:
            raise ProtocolError(
                f"this device's log stops at {seen} although LastSyncID is {server_last}; "
                "refusing to write past the missing entries"
            )

    def _require_device(self) -> str:
        if self._device_uuid is None:
            raise ValueError("this engine has no device_uuid, so it can't write")
        return self._device_uuid

    def devices_to_fetch(self, devices: Iterable[Device]) -> list[Device]:
        """The devices whose log is ahead of what has been seen, in ``DeviceUUID`` order."""
        return sorted(
            (d for d in devices if d.last_sync_id > self._last_seen.get(d.device_uuid, 0)),
            key=lambda d: d.device_uuid,
        )

    async def sync(self, source: SyncSource) -> SyncResult:
        """Fetch and apply what's new. Progress on earlier devices survives a later failure."""
        async with self._lock:
            result = await self._sync(source)
            self._synced = True
            return result

    async def _sync(self, source: SyncSource) -> SyncResult:
        devices = self.devices_to_fetch(await source.get_devices())
        applied = skipped = 0
        for device in devices:
            result = await self._read_through(source, device.device_uuid, device.last_sync_id)
            applied += result.applied
            skipped += result.skipped
        return SyncResult(len(devices), applied, skipped)

    async def _read_through(self, source: SyncSource, device_uuid: str, last: int) -> ApplyResult:
        """Read and apply a device's log page by page from what's been seen, until reaching
        ``last``, a read comes back empty, or a read brings nothing new (warned, so a stuck
        server can't loop forever)."""
        applied = skipped = 0
        while (since := self._last_seen.get(device_uuid, 0)) < last:
            transactions = await source.get_transactions(device_uuid, since)
            result = self.apply(device_uuid, transactions)
            applied += result.applied
            skipped += result.skipped
            if self._last_seen.get(device_uuid, 0) == since:
                if transactions:
                    _LOGGER.warning(
                        "Device %s returned nothing new after number %s; stopping",
                        device_uuid,
                        since,
                    )
                break
        return ApplyResult(applied, skipped)

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
        if device_uuid == self._device_uuid and transaction.op_code == OP_ADD:
            self._created.add(record.object_id)
            self._add_numbers[record.object_id] = transaction.sync_id
        if device_uuid == self._device_uuid and transaction.op_code == OP_UPDATE:
            self._own_updates.add(record.object_id)
        stamp = record.timestamp.timestamp() if record.timestamp is not None else 0.0
        version = _Version(stamp, _OP_RANKS[transaction.op_code], device_uuid, transaction.sync_id)
        if transaction.op_code == OP_DELETE:
            self._delete(record.object_id, version)
        else:
            self._store(record, version)
            if record.object_id not in self._records:
                self._add_numbers.pop(record.object_id, None)  # a newer delete rejected the add
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
            self._add_numbers.pop(object_id, None)
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
            "device": self._device_uuid,
            "synced": self._synced,
            "last_posted": self._last_posted,
            "pending": self._pending.to_dict() if self._pending is not None else None,
            "created": sorted(self._created),
            "created_numbers": dict(sorted(self._add_numbers.items())),
            "own_updates": sorted(self._own_updates),
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
    def from_dict(
        cls,
        data: Mapping[str, Any],
        device_uuid: str | None = None,
        *,
        unknown_outcome_grace: float = 90.0,
    ) -> SyncEngine:
        """Rebuild an engine from ``to_dict`` output. Versions 1 through 4 load.

        Version 1 has no tombstones. Versions without an op rank in the version dicts (2, and
        version 3 as saved by an early write-records build) load records as adds and tombstones
        as deletes. Versions without write state (1, 2, and version 3 as saved by the op-rank
        build) load with none: pass ``device_uuid`` to write with them. Every newer field is
        read with a default, so either version 3 shape loads. Version 4 state saved before
        ``created_numbers`` existed loads too: see ``own_writes`` for how its order is
        recovered. A ``device_uuid`` that differs from the saved one raises ``ValueError``.
        """
        version = data.get("version")
        if version not in (1, 2, 3, STATE_VERSION):
            raise ValueError(f"unsupported engine state version: {version!r}")
        saved_device = data.get("device")
        if saved_device is not None and device_uuid is not None and saved_device != device_uuid:
            raise ValueError("the saved state belongs to a different device")
        engine = cls(
            device_uuid if device_uuid is not None else saved_device,
            unknown_outcome_grace=unknown_outcome_grace,
        )
        engine._last_seen = {str(k): int(v) for k, v in data["last_seen"].items()}
        engine._synced = bool(data.get("synced", bool(engine._last_seen)))
        engine._last_posted = int(data.get("last_posted", 0))
        pending = data.get("pending")
        engine._pending = _Pending.from_dict(pending) if pending else None
        engine._created = {str(i) for i in data.get("created", [])}
        engine._add_numbers = {
            str(i): int(n)
            for i, n in data.get("created_numbers", {}).items()
            if str(i) in engine._created
        }
        engine._own_updates = {str(i) for i in data.get("own_updates", [])}
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
            if (
                record.object_id in engine._created
                and record.object_id not in engine._add_numbers
                and stored.device_uuid == engine._device_uuid
                and stored.rank == _RANK_ADD
            ):
                # State saved before numbers were kept: this device's add is still the version.
                engine._add_numbers[record.object_id] = stored.sync_id
        for entry in data.get("tombstones", []):
            engine._tombstones[str(entry["object_id"])] = _Version.from_dict(
                entry["version"], _RANK_DELETE
            )
        return engine


def _now() -> datetime:
    """The write time, to the second, as payload timestamps carry it."""
    return datetime.now(UTC).replace(microsecond=0)


def _newer(new: _Version, old: _Version) -> bool:
    """Whether ``new`` replaces ``old``: log order within a device, version key across devices."""
    if new.device_uuid == old.device_uuid:
        return new.sync_id > old.sync_id
    return new.key() > old.key()


def _sort_key(record: Record) -> tuple[float, float]:
    assert record.time is not None
    stamp = record.timestamp.timestamp() if record.timestamp is not None else 0.0
    return (record.time.timestamp(), stamp)
