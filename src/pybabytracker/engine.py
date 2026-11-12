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
- Object ids match whatever their case, because Android writes lowercase UUIDs and iOS uppercase
  ones. A write acts on the stored id's case, and an add whose id equals another device's
  record's id apart from case is refused with ``ObjectIdInUseError``.
- A confirmed write is applied to local state at once, under its own number, so the next sync
  sees nothing new from this device and doesn't fetch or reapply it.
- Writes and syncs share one lock, so concurrent writes get consecutive numbers.
- Only records this device created can be deleted. Every add in this device's log counts,
  whether it was applied locally or read back by a sync. Like the app, a delete sends the
  record exactly as last saved, with only ``newFlage`` set to ``"false"``: its timestamp is not
  bumped.
- The edits (opcode 1) are ``end_sleep``, which ends a running sleep, and ``add_pictures``, which
  adds photos to a joy, milestone, or activity, whichever device made it. An update shares the
  numbering, pending, and grace-window rules above, but it isn't an add: it never makes a record
  this device's own (``created_ids``, ``own_writes``). Its local apply is the same merge a replay
  of the log performs.

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
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .builders import (
    Draft,
    Edit,
    NewPicture,
    activity_changes,
    baby_changes,
    bottle_changes,
    check_pictures,
    common_changes,
    diaper_changes,
    draft_activity,
    draft_activity_type,
    draft_bottle,
    draft_diaper,
    draft_food,
    draft_growth,
    draft_joy,
    draft_medication,
    draft_medicine,
    draft_milestone,
    draft_nursing,
    draft_other_feed,
    draft_pump,
    draft_sleep,
    draft_sleep_start,
    draft_temperature,
    draft_vaccine,
    edited_payload,
    ended_sleep_payload,
    growth_changes,
    medication_changes,
    nursing_edit,
    other_feed_changes,
    picture_entry,
    pictured_payload,
    pump_edit,
    resolve_sleep_end,
    sleep_changes,
    temperature_changes,
)
from .client import Device, RawTransaction
from .exceptions import (
    AccountChangedError,
    AuthenticationError,
    BabyTrackerError,
    DownloadRefusedError,
    InvalidSelectionNameError,
    NoResponseError,
    NotOwnRecordError,
    NotSyncedError,
    ObjectIdInUseError,
    ProtocolError,
    RecordNotFoundError,
    SelectionExistsError,
    TimerNotRunningError,
    TransactionDecodeError,
    UnknownActivityError,
    UnknownBabyError,
    UnknownFoodError,
    UnknownMedicineError,
    UnknownMilestoneError,
    UnknownVaccineError,
    WriteOutcomeUnknownError,
    WrongRecordTypeError,
)
from .models import (
    OP_ADD,
    OP_DELETE,
    OP_UPDATE,
    RECORD_TYPES,
    UNIT_OUNCES,
    Baby,
    Diaper,
    Formula,
    Growth,
    Joy,
    Measurement,
    Medication,
    MedicationSelection,
    Milestone,
    MilestoneSelection,
    Nursing,
    NursingSession,
    OtherActivity,
    OtherActivityDescription,
    OtherFeed,
    OtherFeedSelection,
    Pump,
    Pumped,
    Record,
    Sleep,
    Temperature,
    UnknownRecord,
    Vaccine,
    VaccineSelection,
    Volume,
    check_jpeg,
    decode_payload,
    decode_transaction,
    encode_transaction,
    is_account_id,
)
from .seed import Seed, read_seed
from .selections import (
    BUILT_IN_ACTIVITIES,
    BUILT_IN_FOODS,
    BUILT_IN_MILESTONES,
    BUILT_IN_VACCINES,
    SelectionChoice,
    embedded_copy,
    find_choice,
    merged_choices,
)

_LOGGER = logging.getLogger(__name__)

# What ``delete_record`` won't delete for another device, and ``edit_record`` won't edit: the
# apps refuse to delete a baby or a pick-list entry that is still in use, they aren't records
# with a time and a note, and an unknown type can't be checked.
_NOT_DELETABLE_FROM_OTHERS = (
    Baby,
    MedicationSelection,
    MilestoneSelection,
    OtherActivityDescription,
    OtherFeedSelection,
    VaccineSelection,
    UnknownRecord,
)

STATE_VERSION = 4
# How this library reads a seed. Raise it when a fix reads more out of seeds: state saved at an
# older format reads the seed again (records merge idempotently).
SEED_FORMAT = 1

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

    async def upload_picture(self, file_name: str, data: bytes | bytearray) -> None: ...


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
    # This sync read the account's seed, or learned it has none: the state changed even if no
    # log entry was applied, so save it.
    seed_read: bool = False


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
        # Objects this device posted an update to (a sleep's end, photos added, or an edit).
        self._own_updates: set[str] = set()
        self._logged_unknown_types: set[str] = set()
        self._account_id: int | None = None
        self._seeded = 0  # the seed format the account's seed was read at; 0 when it wasn't
        self._seed_failed = False  # a download failed this run; warn once
        self._lock = asyncio.Lock()

    @property
    def device_uuid(self) -> str | None:
        return self._device_uuid

    @property
    def seeded(self) -> bool:
        """Whether the account's seed has been read at this library's ``SEED_FORMAT``.

        It turns true during a sync that reads the seed, even one that then fails, so a caller
        that saves when it changes keeps the seed's records."""
        return self._seeded >= SEED_FORMAT

    @property
    def account_id(self) -> int | None:
        """The sync group's ``AccountID`` this state came from, once a sync has seen one."""
        return self._account_id

    def _check_account(self, source: SyncSource) -> None:
        """Remember the group's ``AccountID``, or refuse a different one.

        A source that doesn't sign in (a test double, a file) has no ``account_id`` and is
        trusted. Call it after a request, so the client has signed in by then.
        """
        account_id = getattr(source, "account_id", None)
        if not is_account_id(account_id):
            return
        if self._account_id is None:
            self._account_id = account_id
        elif account_id != self._account_id:
            raise AccountChangedError(self._account_id, account_id)

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

    def is_deleted(self, object_id: str) -> bool:
        """Whether a record has been deleted and not added back since. Cheap: no state is encoded.

        A record never seen, and one that exists, are both ``False``.
        """
        return object_id in self._tombstones

    def deleted_ids(self) -> frozenset[str]:
        """IDs of every deleted record that has not been added back, as a snapshot.

        Of any type: a tombstone doesn't keep its record's type, so a caller after one type
        (the babies, say) checks its own IDs with ``is_deleted``. Cheap: no state is encoded.
        """
        return frozenset(self._tombstones)

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
        The app itself writes the zone's *standard* offset all year, ignoring daylight saving
        (-18000 for Eastern in July and January), so for a record that matches it pass
        ``standard_offset_seconds(tz, time)``. ``object_id`` defaults to a new uppercase UUID4.
        Pass the same one to retry a write that raised: if that write landed after all, its
        record is returned and nothing is posted. An id matches whatever its case, and one equal
        to another device's id apart from case raises ``ObjectIdInUseError``. Every ``add_*``
        method shares these rules. Bad input raises ``ValueError`` before any request.
        """
        draft = draft_diaper(status, time=time, timezone_offset=timezone_offset, note=note)
        return await self._add(target, baby_id, draft, object_id)

    async def add_joy(
        self,
        target: WriteTarget,
        baby_id: str,
        *,
        time: datetime | None = None,
        timezone_offset: int | None = None,
        note: str = "",
        object_id: str | None = None,
        pictures: Sequence[NewPicture] = (),
    ) -> Joy:
        """Post a joy, a moment kept with its ``note`` and ``pictures``, at ``time`` (default
        now). A joy with neither (a blank note and no pictures) raises ``ValueError`` before any
        request. ``pictures`` work as for ``add_activity``, and the other arguments as for
        ``add_diaper``."""
        draft = draft_joy(time=time, timezone_offset=timezone_offset, note=note)
        if not note.strip() and not check_pictures(pictures):
            raise ValueError("a joy needs a note or a picture")
        return await self._add(target, baby_id, draft, object_id, pictures=pictures)

    async def add_pictures(
        self, target: WriteTarget, object_id: str, pictures: Sequence[NewPicture]
    ) -> Joy | Milestone | OtherActivity:
        """Add photos to a joy, milestone, or activity already saved, whichever device made it:
        post an update (opcode 1), as both apps do when a photo is added while editing one. Those
        are the record types the app's entry screens attach photos to.

        ``pictures`` (at least one) are uploaded as for ``add_activity``'s ``pictures``. Then the
        update carries the record as last saved with only ``pictureNote`` (its entries, then the
        new ones), ``newFlage`` false (in the type the record stores it), and ``timestamp``
        changed. The timestamp is the write time, or one second past the stored one if that is
        ahead of this clock, so the update wins the merge. Returns the record as updated.

        Before reading the record, every device's log is caught up, so a photo, an edit, or a
        delete another device made since the last sync is kept (or refused, for a delete). An
        edit that lands on the service between that read and the post is overwritten, as it is
        when a photo is added in the app.

        Refused before any request: ``ValueError`` for bad or no pictures,
        ``RecordNotFoundError`` for a record never seen or deleted, and ``WrongRecordTypeError``
        for any other record (a diaper, a sleep, a baby, and so on). There is
        no retry by id: if the update raised, check the record's ``pictures`` after the next
        sync before adding the photos again. ``object_id`` can be in either case.
        """
        photos = check_pictures(pictures)
        if not photos:
            raise ValueError("give at least one picture")
        async with self._lock:
            self._check_can_write(target)
            object_id = self._resolve_id(object_id, self._records, self._tombstones) or object_id
            self._picturable(object_id)
            # Every device's log, not just this one's: the update carries the whole record, so
            # it must hold whatever another device saved since the last sync.
            await self._catch_up(target)
            number = await self._prepare(target)
            record = self._picturable(object_id)  # as last saved, after the catch-up
            names = await self._upload_pictures(target, photos)
            entries = [picture_entry(name, record.object_id) for name in names]
            updated = decode_payload(pictured_payload(record, entries, _update_stamp(record)))
            assert isinstance(updated, Joy | Milestone | OtherActivity)
            await self._send(target, number, OP_UPDATE, encode_transaction(updated))
            return updated

    async def edit_record(
        self,
        target: WriteTarget,
        object_id: str,
        *,
        time: datetime | None = None,
        timezone_offset: int | None = None,
        note: str | None = None,
    ) -> Record:
        """Change the time or the note of a saved record, whichever device made it: post an
        update (opcode 1), as the apps do when a record is edited.

        ``time`` (aware) is when the record happened, or started for a sleep, a nursing, or a
        pump; a sleep's or nursing's length is kept, so its end moves too. ``timezone_offset``
        changes the record's zone, which is otherwise kept. ``note`` replaces the note ("" to
        clear it). The update carries the record as last saved with only those keys,
        ``newFlage`` false, and ``timestamp`` changed, and the timestamp is set as for
        ``add_pictures``. Returns the record as updated. When nothing would change, the record
        is returned as it is and nothing is posted, so a retry after a write that landed is
        safe.

        It edits a baby's record or a pump. Refused before any request: ``ValueError`` for
        nothing to change or a bad value, ``RecordNotFoundError`` for a record never seen or
        deleted, and ``WrongRecordTypeError`` for a baby, a pick-list entry, or a record of a
        type the library doesn't know. ``object_id`` can be in either case. The ``edit_*`` methods
        for each record type also change a record's own fields.
        """
        edit = Edit(common_changes(time=time, timezone_offset=timezone_offset, note=note))
        return await self._edit(target, object_id, lambda record: edit)

    async def edit_diaper(
        self,
        target: WriteTarget,
        object_id: str,
        *,
        status: int | None = None,
        time: datetime | None = None,
        timezone_offset: int | None = None,
        note: str | None = None,
    ) -> Diaper:
        """Change a saved diaper: its ``status`` (``DIAPER_WET``, ``DIAPER_DIRTY``, or
        ``DIAPER_MIXED``), and its time, zone, and note as for ``edit_record``, whose rules it
        shares. Give at least one. A record that isn't a diaper raises ``WrongRecordTypeError``.
        """
        common = common_changes(
            time=time, timezone_offset=timezone_offset, note=note, required=False
        )
        edit = Edit(_some({**diaper_changes(status), **common}))
        record = await self._edit(target, object_id, lambda record: edit, Diaper)
        assert isinstance(record, Diaper)
        return record

    async def edit_bottle(
        self,
        target: WriteTarget,
        object_id: str,
        *,
        volume: Volume | None = None,
        time: datetime | None = None,
        timezone_offset: int | None = None,
        note: str | None = None,
    ) -> Formula | Pumped:
        """Change a saved formula or pumped-milk bottle: its ``volume`` (more than zero, as for
        ``add_formula``), and its time, zone, and note as for ``edit_record``, whose rules it
        shares. Give at least one. Formula can't become pumped milk or the reverse: they are
        different record types, so delete the bottle and add the other. A record that isn't a
        bottle raises ``WrongRecordTypeError``.
        """
        common = common_changes(
            time=time, timezone_offset=timezone_offset, note=note, required=False
        )
        edit = Edit(_some({**bottle_changes(volume), **common}))
        record = await self._edit(target, object_id, lambda record: edit, Formula, Pumped)
        assert isinstance(record, Formula | Pumped)
        return record

    async def edit_nursing(
        self,
        target: WriteTarget,
        object_id: str,
        *,
        left_minutes: int | None = None,
        right_minutes: int | None = None,
        finish_side: int | None = None,
        time: datetime | None = None,
        timezone_offset: int | None = None,
        note: str | None = None,
    ) -> Nursing:
        """Change a saved nursing: the minutes on a side (a side not given keeps its own), the
        side it is recorded under, and its time, zone, and note as for ``edit_record``, whose
        rules it shares. Give at least one. ``time`` is the start.

        The checks are ``add_nursing``'s: whole minutes, at most ``MAX_MINUTES_PER_SIDE`` a
        side, and at least a minute in all. When the minutes change, the keys an iPhone's timer
        left on the record are dropped, as the iPhone app drops them. The minutes of an
        Android timer's ``NursingSession`` can't be changed (``WrongRecordTypeError``): it
        carries the timer's own timestamps, and how the app edits one hasn't been seen. Its
        time, zone, and note can.
        """
        common = common_changes(
            time=time, timezone_offset=timezone_offset, note=note, required=False
        )

        def changes(record: Record) -> Edit:
            assert isinstance(record, Nursing)
            own = nursing_edit(
                record,
                left_minutes=left_minutes,
                right_minutes=right_minutes,
                finish_side=finish_side,
            )
            if own.drop and isinstance(record, NursingSession):
                raise WrongRecordTypeError(
                    f"record {record.object_id} is a NursingSession, whose minutes come from "
                    "its timer and can't be edited"
                )
            return Edit(_some({**own.set, **common}), own.drop)

        record = await self._edit(target, object_id, changes, Nursing)
        assert isinstance(record, Nursing)
        return record

    async def edit_sleep(
        self,
        target: WriteTarget,
        object_id: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        timezone_offset: int | None = None,
        note: str | None = None,
    ) -> Sleep:
        """Change a saved sleep: its ``start`` and ``end`` (aware), and its zone and note as for
        ``edit_record``, whose rules it shares. Give at least one.

        The one not given is kept, so a new ``start`` keeps the saved end and the sleep gets
        longer or shorter; ``edit_record`` with a ``time`` moves the whole sleep instead. A
        running sleep stays running when only its start moves, and an ``end`` finishes it. The
        checks are ``add_sleep``'s: the end after the start, at most 24 hours, and the length
        in whole minutes, rounded half up and at least 1.
        """
        common = common_changes(
            time=None, timezone_offset=timezone_offset, note=note, required=False
        )

        def changes(record: Record) -> Edit:
            assert isinstance(record, Sleep)
            return Edit(_some({**sleep_changes(record, start=start, end=end), **common}))

        record = await self._edit(target, object_id, changes, Sleep)
        assert isinstance(record, Sleep)
        return record

    async def edit_temperature(
        self,
        target: WriteTarget,
        object_id: str,
        *,
        temperature: Measurement | None = None,
        time: datetime | None = None,
        timezone_offset: int | None = None,
        note: str | None = None,
    ) -> Temperature:
        """Change a saved temperature: its reading (``Measurement(value, "F")`` or
        ``Measurement(value, "C")``, checked as for ``add_temperature``), and its time, zone,
        and note as for ``edit_record``, whose rules it shares. Give at least one.
        """
        common = common_changes(
            time=time, timezone_offset=timezone_offset, note=note, required=False
        )
        edit = Edit(_some({**temperature_changes(temperature), **common}))
        record = await self._edit(target, object_id, lambda record: edit, Temperature)
        assert isinstance(record, Temperature)
        return record

    async def edit_growth(
        self,
        target: WriteTarget,
        object_id: str,
        *,
        weight: Measurement | None = None,
        length: Measurement | None = None,
        head: Measurement | None = None,
        time: datetime | None = None,
        timezone_offset: int | None = None,
        note: str | None = None,
    ) -> Growth:
        """Change a saved growth record: any of its ``weight``, ``length``, and ``head``
        (each checked as for ``add_growth``), and its time, zone, and note as for
        ``edit_record``, whose rules it shares. Give at least one. A measurement not given is
        kept, and one can't be removed. A new length or head must be in the unit of the other,
        given or saved.
        """
        common = common_changes(
            time=time, timezone_offset=timezone_offset, note=note, required=False
        )

        def changes(record: Record) -> Edit:
            assert isinstance(record, Growth)
            own = growth_changes(record, weight=weight, length=length, head=head)
            return Edit(_some({**own, **common}))

        record = await self._edit(target, object_id, changes, Growth)
        assert isinstance(record, Growth)
        return record

    async def edit_medication(
        self,
        target: WriteTarget,
        object_id: str,
        *,
        amount: float | None = None,
        time: datetime | None = None,
        timezone_offset: int | None = None,
        note: str | None = None,
    ) -> Medication:
        """Change a saved dose: its ``amount`` (more than zero, as for ``add_medication``),
        and its time, zone, and note as for ``edit_record``, whose rules it shares. Give at
        least one. The medicine itself can't be changed: delete the dose and add another.
        """
        common = common_changes(
            time=time, timezone_offset=timezone_offset, note=note, required=False
        )
        edit = Edit(_some({**medication_changes(amount), **common}))
        record = await self._edit(target, object_id, lambda record: edit, Medication)
        assert isinstance(record, Medication)
        return record

    async def edit_other_feed(
        self,
        target: WriteTarget,
        object_id: str,
        *,
        amount: float | None = None,
        unit: str | None = None,
        time: datetime | None = None,
        timezone_offset: int | None = None,
        note: str | None = None,
    ) -> OtherFeed:
        """Change a saved solid food: its ``amount`` and ``unit`` (checked as for
        ``add_other_feed``), and its time, zone, and note as for ``edit_record``, whose rules
        it shares. Give at least one. The food and the meal can't be changed: delete the
        record and add another.
        """
        common = common_changes(
            time=time, timezone_offset=timezone_offset, note=note, required=False
        )
        edit = Edit(_some({**other_feed_changes(amount, unit), **common}))
        record = await self._edit(target, object_id, lambda record: edit, OtherFeed)
        assert isinstance(record, OtherFeed)
        return record

    async def edit_activity(
        self,
        target: WriteTarget,
        object_id: str,
        *,
        duration: int | None = None,
        time: datetime | None = None,
        timezone_offset: int | None = None,
        note: str | None = None,
    ) -> OtherActivity:
        """Change a saved activity: its ``duration`` (whole minutes, as for
        ``add_activity``), and its time, zone, and note as for ``edit_record``, whose rules it
        shares. Give at least one. The activity type can't be changed: delete the record and
        add another.
        """
        common = common_changes(
            time=time, timezone_offset=timezone_offset, note=note, required=False
        )
        edit = Edit(_some({**activity_changes(duration), **common}))
        record = await self._edit(target, object_id, lambda record: edit, OtherActivity)
        assert isinstance(record, OtherActivity)
        return record

    async def edit_pump(
        self,
        target: WriteTarget,
        object_id: str,
        *,
        amount: Volume | None = None,
        left_volume: Volume | None = None,
        right_volume: Volume | None = None,
        left_minutes: int | None = None,
        right_minutes: int | None = None,
        finish_side: int | None = None,
        time: datetime | None = None,
        timezone_offset: int | None = None,
        note: str | None = None,
    ) -> Pump:
        """Change a saved pump: its volumes, the minutes on a side, the side it is recorded
        under, and its time, zone, and note as for ``edit_record``, whose rules it shares. Give
        at least one. ``time`` is the start.

        Give the total as ``amount``, or the volume of one or both sides, not both, as for
        ``add_pump``. A new total drops the per-side volumes. With a side volume, the other
        side keeps its saved volume (0 when it has none), both must be in one unit, and the
        total becomes their sum. Minutes and the side not given are kept, and the pump must
        still have a volume or a length.
        """
        common = common_changes(
            time=time, timezone_offset=timezone_offset, note=note, required=False
        )

        def changes(record: Record) -> Edit:
            assert isinstance(record, Pump)
            own = pump_edit(
                record,
                amount=amount,
                left_volume=left_volume,
                right_volume=right_volume,
                left_minutes=left_minutes,
                right_minutes=right_minutes,
                finish_side=finish_side,
            )
            return Edit(_some({**own.set, **common}), own.drop)

        record = await self._edit(target, object_id, changes, Pump)
        assert isinstance(record, Pump)
        return record

    async def edit_baby(
        self,
        target: WriteTarget,
        baby_id: str,
        *,
        name: str | None = None,
        birth: datetime | None = None,
        due: datetime | None = None,
        boy: bool | None = None,
        photo: bytes | None = None,
        landscape_photo: bytes | None = None,
    ) -> Baby:
        """Change a baby: its ``name``, ``birth`` and ``due`` dates (aware), whether it is a
        ``boy``, and its ``photo``. Give at least one. It posts an update (opcode 1) of the
        ``Baby`` record, as the iPhone app does: the baby as last saved with only the edited
        keys, ``newFlage`` false, and ``timestamp`` changed, as for ``edit_record``.

        ``photo`` is the bytes of a JPEG. It is uploaded first, under a new uppercase UUID plus
        ".jpg" and again plus "_l.jpg" (the landscape copy, ``landscape_photo`` when given, else
        the same photo, as the iPhone app uploads it), and the baby's ``pictureName`` becomes
        that UUID. The old photo's files stay on the service.

        Returns the baby as updated; records added afterwards embed it. Without a photo,
        nothing is posted when the baby already has the values. Refused before any request:
        ``ValueError`` for nothing to change or a bad value, ``UnknownBabyError`` for a baby
        never seen, and ``RecordNotFoundError`` for a baby that was deleted, or whose own
        record hasn't synced (only the copies its records carry, which may be partial).
        """
        picture = check_jpeg(photo, "a baby's photo") if photo is not None else None
        landscape = (
            check_jpeg(landscape_photo, "a baby's landscape photo")
            if landscape_photo is not None
            else picture
        )
        if landscape_photo is not None and picture is None:
            raise ValueError("give a photo with its landscape_photo")

        def changes(baby: Baby) -> Edit:
            given = baby_changes(baby, name=name, birth=birth, due=due, boy=boy)
            if not given and picture is None:
                raise ValueError("give something to change")
            return Edit(given)

        async with self._lock:
            self._check_can_write(target)
            baby_id = self._resolve_id(baby_id, self._records, self._tombstones) or baby_id
            baby = self._saved_baby(baby_id)
            if picture is None and changes(baby).already_in(baby.to_payload()):
                return baby
            # Every device's log: the update carries the whole baby as last saved.
            await self._catch_up(target)
            number = await self._prepare(target)
            baby = self._saved_baby(baby_id)
            edit = changes(baby)
            if picture is None and edit.already_in(baby.to_payload()):
                return baby
            if picture is not None and landscape is not None:
                picture_name = str(uuid.uuid4()).upper()
                await target.upload_picture(f"{picture_name}.jpg", picture)
                await target.upload_picture(f"{picture_name}_l.jpg", landscape)
                edit = Edit({**edit.set, "pictureName": picture_name})
            updated = decode_payload(edited_payload(baby, edit, _update_stamp(baby)))
            assert isinstance(updated, Baby)
            await self._send(target, number, OP_UPDATE, encode_transaction(updated))
            return updated

    def _saved_baby(self, baby_id: str) -> Baby:
        """The baby's own record, or raise why it can't be edited."""
        record = self._records.get(baby_id)
        if isinstance(record, Baby):
            return record
        if baby_id in self._tombstones:
            raise RecordNotFoundError(f"baby {baby_id} was deleted")
        if record is not None or baby_id not in self.babies():
            raise UnknownBabyError(f"no baby {baby_id} is known from sync")
        raise RecordNotFoundError(
            f"baby {baby_id}'s own record hasn't synced, only the copies its records carry"
        )

    async def _edit(
        self,
        target: WriteTarget,
        object_id: str,
        changes_for: Callable[[Record], Edit],
        *kinds: type[Record],
    ) -> Record:
        """The shared path of an edit: check, catch up, and post the record as last saved with
        the edit applied. ``changes_for`` gives the edit for the saved record and raises for bad
        input; ``kinds`` are the record types it applies to (any baby's record or pump when
        empty). Nothing is posted when the record already has the edit."""
        async with self._lock:
            self._check_can_write(target)
            object_id = self._resolve_id(object_id, self._records, self._tombstones) or object_id
            record = self._editable(object_id, kinds)
            if changes_for(record).already_in(record.to_payload()):
                return record
            # Every device's log, not just this one's: the update carries the whole record, so
            # it must hold whatever another device saved since the last sync.
            await self._catch_up(target)
            number = await self._prepare(target)
            record = self._editable(object_id, kinds)  # as last saved, after the catch-up
            edit = changes_for(record)
            if edit.already_in(record.to_payload()):
                return record
            updated = decode_payload(edited_payload(record, edit, _update_stamp(record)))
            await self._send(target, number, OP_UPDATE, encode_transaction(updated))
            return updated

    def _editable(self, object_id: str, kinds: tuple[type[Record], ...] = ()) -> Record:
        """The record ``object_id`` names, or raise why it can't be edited: one of ``kinds``, or
        any baby's record or pump when there are none."""
        wanted = " or ".join(kind.object_type for kind in kinds) or "baby's record or a pump"
        if object_id in self.babies():
            raise WrongRecordTypeError(f"record {object_id} is a Baby, not a {wanted}")
        record = self._saved_record(object_id)
        if isinstance(record, _NOT_DELETABLE_FROM_OTHERS) or (
            kinds and not isinstance(record, kinds)
        ):
            raise WrongRecordTypeError(
                f"record {object_id} is a {record.record_type or 'record'}, not a {wanted}"
            )
        return record

    def _picturable(self, object_id: str) -> Joy | Milestone | OtherActivity:
        """The joy, milestone, or activity ``object_id`` names, or raise why photos can't be
        added to it."""
        if object_id in self.babies():
            raise WrongRecordTypeError(
                f"record {object_id} is a Baby, not a joy, milestone, or activity"
            )
        record = self._saved_record(object_id)
        if not isinstance(record, Joy | Milestone | OtherActivity):
            raise WrongRecordTypeError(
                f"record {object_id} is a {record.record_type or 'record'}, "
                "not a joy, milestone, or activity"
            )
        return record

    async def add_milestone(
        self,
        target: WriteTarget,
        baby_id: str,
        milestone: str,
        *,
        time: datetime | None = None,
        timezone_offset: int | None = None,
        note: str = "",
        object_id: str | None = None,
        pictures: Sequence[NewPicture] = (),
    ) -> Milestone:
        """Post a milestone the baby reached, and apply it locally.

        ``milestone`` is a name or ID from ``milestone_choices(baby_id)``: one the app ships
        with, or one added in the app and known from sync. A name matches ignoring case and
        surrounding spaces; a name that matches two entries raises ``AmbiguousSelectionError``,
        and the ID must be passed instead. The record embeds that milestone as the app does. An
        empty or non-text ``milestone`` raises ``InvalidSelectionNameError``, an unknown one
        ``UnknownMilestoneError``, and no baby ``UnknownBabyError``, all before any request;
        adding a new milestone to the pick list isn't supported yet. ``pictures`` work as for
        ``add_activity``, and the other arguments as for ``add_diaper``.
        """
        self._check_baby(target, baby_id)

        def resolve() -> Draft[Milestone]:
            choice = self.find_milestone(milestone, baby_id)
            return draft_milestone(
                choice.selection, time=time, timezone_offset=timezone_offset, note=note
            )

        resolve()  # a bad name or an unknown or ambiguous milestone fails before the lock
        return await self._add(target, baby_id, resolve, object_id, pictures=pictures)

    def milestone_choices(self, baby_id: str | None = None) -> list[SelectionChoice]:
        """Every milestone a record for the baby can name.

        The app's built-in milestones come first, in its order, then the ones added in the app,
        by name. A milestone added for one baby only is left out for the others, and so is a
        custom one that only another baby's records embed; with no ``baby_id``, all are listed.
        Of several copies of a milestone, a synced one wins, then the one in the newest record
        (by its ``timestamp``, then its ``time``); a copy replaces the built-in template, so a
        record embeds what the app has.
        """
        synced = [
            r.to_payload() for r in self._records.values() if isinstance(r, MilestoneSelection)
        ]
        embedded = [
            embedded_copy(r, r.milestone_type)
            for r in self._records.values()
            if isinstance(r, Milestone)
        ]
        return merged_choices(BUILT_IN_MILESTONES, embedded, synced, baby_id=baby_id)

    def find_milestone(self, milestone: str, baby_id: str | None = None) -> SelectionChoice:
        """The milestone with that ID, or else that name.

        Raises ``UnknownMilestoneError`` without one, ``AmbiguousSelectionError`` when a name
        matches two, and ``InvalidSelectionNameError`` for an empty or non-text ``milestone``.
        """
        wanted = _wanted(milestone, "milestone")
        found = find_choice(self.milestone_choices(baby_id), wanted, "milestone")
        if found is None:
            raise UnknownMilestoneError(
                f"no milestone {milestone!r} is built in or known from sync"
            )
        return found

    async def add_vaccine(
        self,
        target: WriteTarget,
        baby_id: str,
        vaccine: str,
        *,
        time: datetime | None = None,
        timezone_offset: int | None = None,
        note: str = "",
        object_id: str | None = None,
    ) -> Vaccine:
        """Post a vaccine the baby was given, and apply it locally.

        ``vaccine`` is a name or ID from ``vaccine_choices()``: one of the app's built-in
        vaccines (``BUILT_IN_VACCINES``) or one added in the app and known from sync, matched as
        for ``add_milestone``. The record embeds it as ``vaccineType``. Bad input raises the
        errors ``add_milestone`` does (``UnknownVaccineError`` for an unknown vaccine) before any
        request. The other arguments work as for ``add_diaper``.
        """
        self._check_baby(target, baby_id)

        def resolve() -> Draft[Vaccine]:
            choice = self.find_vaccine(vaccine)
            return draft_vaccine(
                choice.selection, time=time, timezone_offset=timezone_offset, note=note
            )

        resolve()
        return await self._add(target, baby_id, resolve, object_id)

    def vaccine_choices(self) -> list[SelectionChoice]:
        """Every vaccine a record can name: the app's built-ins in its order, then added ones.

        Copies are chosen as for ``milestone_choices``. Vaccines aren't narrowed by baby: a
        ``VaccineSelection`` with a ``babyID`` hasn't been seen in synced data.
        """
        synced = [r.to_payload() for r in self._records.values() if isinstance(r, VaccineSelection)]
        embedded = [
            embedded_copy(r, r.vaccine_type)
            for r in self._records.values()
            if isinstance(r, Vaccine)
        ]
        return merged_choices(BUILT_IN_VACCINES, embedded, synced)

    def find_vaccine(self, vaccine: str) -> SelectionChoice:
        """The vaccine with that ID, or else that name; errors as for ``find_milestone``."""
        found = find_choice(self.vaccine_choices(), _wanted(vaccine, "vaccine"), "vaccine")
        if found is None:
            raise UnknownVaccineError(f"no vaccine {vaccine!r} is built in or known from sync")
        return found

    async def add_activity(
        self,
        target: WriteTarget,
        baby_id: str,
        activity: str,
        *,
        duration: int = 0,
        time: datetime | None = None,
        timezone_offset: int | None = None,
        note: str = "",
        object_id: str | None = None,
        pictures: Sequence[NewPicture] = (),
    ) -> OtherActivity:
        """Post an activity, such as tummy time or a bath, and apply it locally.

        ``activity`` is a name or ID from ``activity_choices()``: one of the app's built-in
        types (``BUILT_IN_ACTIVITIES``) or one added in the app and known from sync, matched as
        for ``add_milestone``. The record embeds it as ``desc``. ``duration`` is whole minutes,
        0 (the default) when it wasn't timed, at most ``MAX_ACTIVITY_MINUTES``. Bad input raises
        the errors ``add_milestone`` does (``UnknownActivityError`` for an unknown type) or
        ``ValueError`` for a bad duration, before any request; ``add_activity_type`` adds a
        new type to the pick list. The other arguments work as for ``add_diaper``.

        ``pictures`` are photos, each a ``NewPicture(photo, thumbnail)`` of JPEG bytes, as both
        apps attach them when a record is added. ``add_activity``, ``add_milestone``, and
        ``add_joy`` take them: those are the record types the app's entry screens attach photos
        to, and the others have no photo picker. Before the record is posted, each photo is
        uploaded as a new uppercase UUID plus ".jpg" and its thumbnail as that UUID plus
        "__thumbnail.jpg", and the record lists them in its ``pictureNote``. If an upload fails,
        the error is raised and nothing is posted; a file that did land stays on the service (it
        can't be deleted), and a retry uploads the pictures again under new names.
        """
        self._check_baby(target, baby_id)

        def resolve() -> Draft[OtherActivity]:
            choice = self.find_activity(activity)
            return draft_activity(
                choice.selection,
                duration=duration,
                time=time,
                timezone_offset=timezone_offset,
                note=note,
            )

        resolve()
        return await self._add(target, baby_id, resolve, object_id, pictures=pictures)

    async def add_growth(
        self,
        target: WriteTarget,
        baby_id: str,
        *,
        weight: Measurement | None = None,
        length: Measurement | None = None,
        head: Measurement | None = None,
        time: datetime | None = None,
        timezone_offset: int | None = None,
        note: str = "",
        object_id: str | None = None,
    ) -> Growth:
        """Post a growth measurement: any of ``weight`` (``"lb"`` or ``"kg"``), ``length``, and
        ``head`` (``"in"`` or ``"cm"``), each a ``Measurement``, at least one given.

        Units are not converted and numbers are rounded to two decimals. A weight in pounds is
        decimal pounds (9 lb 4 oz is 9.25). Length and head must share a unit, and each must be
        in ``GROWTH_RANGES``. The other arguments work as for ``add_diaper``.
        """
        draft = draft_growth(
            weight=weight,
            length=length,
            head=head,
            time=time,
            timezone_offset=timezone_offset,
            note=note,
        )
        return await self._add(target, baby_id, draft, object_id)

    async def add_temperature(
        self,
        target: WriteTarget,
        baby_id: str,
        temperature: Measurement,
        *,
        time: datetime | None = None,
        timezone_offset: int | None = None,
        note: str = "",
        object_id: str | None = None,
    ) -> Temperature:
        """Post a temperature, ``Measurement(value, "F")`` or ``Measurement(value, "C")``.

        The unit is not converted and the number is rounded to two decimals. It must be in
        ``TEMPERATURE_RANGES`` for its unit, so a Celsius reading passed as Fahrenheit is
        refused. The other arguments work as for ``add_diaper``.
        """
        draft = draft_temperature(
            temperature, time=time, timezone_offset=timezone_offset, note=note
        )
        return await self._add(target, baby_id, draft, object_id)

    async def add_activity_type(
        self, target: WriteTarget, name: str, *, object_id: str | None = None
    ) -> OtherActivityDescription:
        """Add an activity type, such as "Bath", to the account's pick list, and apply it
        locally.

        The type is posted as its own ``OtherActivityDescription``, as the apps post a new food
        or medicine before the first record that names it, so log an activity with
        ``add_activity`` and the returned record's ``object_id``. ``name`` is written without
        surrounding spaces, at most ``MAX_ACTIVITY_TYPE_NAME_LENGTH`` characters. A name already
        on the list (ignoring case, built-in or not) raises ``SelectionExistsError`` with that
        type's ID, and a bad name ``ValueError``, before any request. The type belongs to the
        account, not a baby. ``object_id`` works as for ``add_diaper``.
        """

        def resolve() -> Draft[OtherActivityDescription]:
            draft = draft_activity_type(name)
            wanted = name.strip().casefold()
            for choice in self.activity_choices():
                # A retry names the type that landed, whatever the case of its id.
                is_retry = (
                    object_id is not None
                    and self._resolve_id(object_id, (choice.object_id,)) is not None
                )
                if choice.name.strip().casefold() == wanted and not is_retry:
                    raise SelectionExistsError("activity", name.strip(), choice.object_id)
            return draft

        resolve()
        return await self._add(target, None, resolve, object_id)

    def activity_choices(self) -> list[SelectionChoice]:
        """Every activity type a record can name: the app's built-ins in its order, then added
        ones, by name. Copies are chosen as for ``milestone_choices``. A type added in the app
        is embedded in each activity of that type, and one synced as its own
        ``OtherActivityDescription`` record (as ``add_activity_type`` posts it) is offered too.
        """
        synced = [
            r.to_payload()
            for r in self._records.values()
            if isinstance(r, OtherActivityDescription)
        ]
        embedded = [
            embedded_copy(r, r.desc) for r in self._records.values() if isinstance(r, OtherActivity)
        ]
        return merged_choices(BUILT_IN_ACTIVITIES, embedded, synced)

    def find_activity(self, activity: str) -> SelectionChoice:
        """The activity type with that ID, or else that name; errors as for ``find_milestone``."""
        found = find_choice(self.activity_choices(), _wanted(activity, "activity"), "activity")
        if found is None:
            raise UnknownActivityError(f"no activity {activity!r} is built in or known from sync")
        return found

    async def add_other_feed(
        self,
        target: WriteTarget,
        baby_id: str,
        food: str,
        *,
        amount: float,
        unit: str,
        meal_id: str | None = None,
        time: datetime | None = None,
        timezone_offset: int | None = None,
        note: str = "",
        object_id: str | None = None,
    ) -> OtherFeed:
        """Post a solid food (or a drink such as water or juice), and apply it locally.

        ``food`` is a name or ID from ``food_choices()``: one of the app's built-in foods
        (``BUILT_IN_FOODS``) or one added in the app and known from sync, matched as for
        ``add_milestone``. ``amount`` (more than zero, at most ``MAX_OTHER_FEED_AMOUNT``) counts
        ``unit``, free text such as "Oz", "Pieces", or "Tsp" (surrounding spaces dropped).
        Foods eaten together share a meal: log the first, then pass its ``meal_id`` when
        logging the rest. It defaults to a new one. Bad input raises the errors
        ``add_milestone`` does (``UnknownFoodError`` for an unknown food) or ``ValueError``,
        before any request; ``add_food`` adds a new food to the pick list. The food is embedded
        as the iPhone app embeds one: ``newFlage`` "false" and the 1970 timestamp. The other
        arguments work as for ``add_diaper``.
        """
        self._check_baby(target, baby_id)
        meal = meal_id if meal_id is not None else str(uuid.uuid4()).upper()

        def resolve() -> Draft[OtherFeed]:
            choice = self.find_food(food)
            return draft_other_feed(
                choice.selection,
                amount=amount,
                unit=unit,
                meal_id=meal,
                time=time,
                timezone_offset=timezone_offset,
                note=note,
            )

        resolve()
        return await self._add(target, baby_id, resolve, object_id)

    async def add_food(
        self,
        target: WriteTarget,
        name: str,
        *,
        bottle: bool = False,
        object_id: str | None = None,
    ) -> OtherFeedSelection:
        """Add a food to the account's pick list, and apply it locally.

        The food is posted as its own ``OtherFeedSelection``, as both apps do before the first
        solid food that names it, so log that with ``add_other_feed`` and the returned record's
        ``object_id``. ``bottle`` True offers it as a drink on the bottle screen. ``name`` is
        written without surrounding spaces, at most ``MAX_FOOD_NAME_LENGTH`` characters. A name
        already on the list (ignoring case, built-in or not) raises ``SelectionExistsError``
        with that food's ID, and a bad name or ``bottle`` ``ValueError``, before any request.
        The food belongs to the account, not a baby. ``object_id`` works as for ``add_diaper``.
        """

        def resolve() -> Draft[OtherFeedSelection]:
            draft = draft_food(name, bottle=bottle)
            wanted = name.strip().casefold()
            for choice in self.food_choices():
                # A retry names the food that landed, whatever the case of its id.
                is_retry = (
                    object_id is not None
                    and self._resolve_id(object_id, (choice.object_id,)) is not None
                )
                if choice.name.strip().casefold() == wanted and not is_retry:
                    raise SelectionExistsError("food", name.strip(), choice.object_id)
            return draft

        resolve()
        return await self._add(target, None, resolve, object_id)

    def food_choices(self) -> list[SelectionChoice]:
        """Every food a solid food record can name: the app's built-ins in its order, then
        added ones, by name. Copies are chosen as for ``milestone_choices``. A food added in the
        app syncs as its own ``OtherFeedSelection`` record and is embedded in each record of it
        as ``feedType``; either makes it a choice, even a synced one this library can't decode.
        A food only in the account's seed, not yet used by a record, is not.
        """
        synced = [
            r.to_payload() for r in self._records.values() if r.record_type == "OtherFeedSelection"
        ]
        embedded = [
            embedded_copy(r, r.feed_type)
            for r in self._records.values()
            if isinstance(r, OtherFeed)
        ]
        return merged_choices(BUILT_IN_FOODS, embedded, synced)

    def find_food(self, food: str) -> SelectionChoice:
        """The food with that ID, or else that name; errors as for ``find_milestone``."""
        found = find_choice(self.food_choices(), _wanted(food, "food"), "food")
        if found is None:
            raise UnknownFoodError(f"no food {food!r} is built in or known from sync")
        return found

    async def add_medication(
        self,
        target: WriteTarget,
        baby_id: str,
        medicine: str,
        *,
        amount: float | None = None,
        time: datetime | None = None,
        timezone_offset: int | None = None,
        note: str = "",
        object_id: str | None = None,
    ) -> Medication:
        """Post a dose of a medicine the baby was given, and apply it locally.

        ``medicine`` is a name or ID from ``medicine_choices()``: a medicine the account has used
        or added in the app (the app ships none), matched as for ``add_milestone``: two medicines
        of one name raise ``AmbiguousSelectionError``, so pass the ID. ``amount`` is in the
        medicine's own unit (its ``unit``, such as "ml"), rounded to 2 decimals. It defaults to
        the medicine's usual dose (``amountPerTime``) as the newest record that embeds it has it;
        a medicine without a usual dose that is a number above zero needs an ``amount``. The
        record embeds the medicine as ``medicationSelection``. An unknown medicine raises
        ``UnknownMedicineError``, and a missing or bad amount ``ValueError``, before any request.
        The other arguments work as for ``add_diaper``.
        """
        self._check_baby(target, baby_id)

        def resolve() -> Draft[Medication]:
            choice = self.find_medicine(medicine)
            return draft_medication(
                choice.selection,
                amount,
                time=time,
                timezone_offset=timezone_offset,
                note=note,
            )

        resolve()
        return await self._add(target, baby_id, resolve, object_id)

    async def add_medicine(
        self,
        target: WriteTarget,
        name: str,
        *,
        unit: str,
        amount_per_time: float,
        interval: timedelta,
        prescription: bool = False,
        description: str = "",
        object_id: str | None = None,
    ) -> MedicationSelection:
        """Add a medicine to the account's pick list, and apply it locally.

        The medicine is posted as its own ``MedicationSelection``, as the iPhone app does just
        before the first dose of it, so log that with ``add_medication`` and the returned
        record's ``object_id``. ``amount_per_time`` is the usual dose in ``unit`` (free text,
        such as "ml" or "Drops"), and ``interval`` the time between doses, in whole hours as the
        app takes them (written in minutes). A name already on the list (ignoring case)
        raises ``SelectionExistsError`` with that medicine's ID, and bad input ``ValueError``,
        before any request. The medicine belongs to the account, not a baby. ``object_id``
        works as for ``add_diaper``.
        """

        def resolve() -> Draft[MedicationSelection]:
            draft = draft_medicine(
                name,
                unit=unit,
                amount_per_time=amount_per_time,
                interval=interval,
                prescription=prescription,
                description=description,
            )
            wanted = name.strip().casefold()
            for choice in self.medicine_choices():
                # A retry names the medicine that landed, whatever the case of its id.
                is_retry = (
                    object_id is not None
                    and self._resolve_id(object_id, (choice.object_id,)) is not None
                )
                if choice.name.strip().casefold() == wanted and not is_retry:
                    raise SelectionExistsError("medicine", name.strip(), choice.object_id)
            return draft

        resolve()
        return await self._add(target, None, resolve, object_id)

    def medicine_choices(self) -> list[SelectionChoice]:
        """Every medicine a dose can name, by name: ones synced as ``MedicationSelection``
        records, and ones doses already embed. The app ships no medicines. Copies are chosen as
        for ``milestone_choices``, so a medicine's usual dose is the newest dose's."""
        synced = [
            r.to_payload() for r in self._records.values() if isinstance(r, MedicationSelection)
        ]
        embedded = [
            embedded_copy(r, r.medication_selection)
            for r in self._records.values()
            if isinstance(r, Medication)
        ]
        return merged_choices((), embedded, synced)

    def find_medicine(self, medicine: str) -> SelectionChoice:
        """The medicine with that ID, or else that name; errors as for ``find_milestone``."""
        found = find_choice(self.medicine_choices(), _wanted(medicine, "medicine"), "medicine")
        if found is None:
            raise UnknownMedicineError(f"no medicine {medicine!r} is known from sync")
        return found

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
        (the session just ended). Give at most one of them. ``finish_side`` (``SIDE_LEFT``,
        ``SIDE_RIGHT``, or ``SIDE_NONE``), which the app shows as the side the session started
        on, defaults to the only side used, or ``SIDE_NONE`` when both were.
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
        *,
        amount: Volume | None = None,
        left_volume: Volume | None = None,
        right_volume: Volume | None = None,
        left_minutes: int = 0,
        right_minutes: int = 0,
        finish_side: int | None = None,
        time: datetime | None = None,
        timezone_offset: int | None = None,
        note: str = "",
        object_id: str | None = None,
        default_unit: str = UNIT_OUNCES,
    ) -> Pump:
        """Post a pumping session. A pump belongs to the account, so it takes no baby.

        Give the total as ``amount``, or the volume of at least one side (one unit for both, not
        mixed with ``amount``). With sides the total is their sum and a side left out is written
        as 0; with only ``amount`` the payload has no per-side amounts, like the app's own
        pump. With no volume at all the minutes are required, and the payload has the durations
        and an ``amount`` of 0 in ``default_unit``, as the app writes a timed pump with no volume.
        ``time`` is the session's start (default now). ``finish_side`` defaults as in
        ``add_nursing``.
        """
        draft = draft_pump(
            amount=amount,
            left_volume=left_volume,
            right_volume=right_volume,
            left_minutes=left_minutes,
            right_minutes=right_minutes,
            finish_side=finish_side,
            time=time,
            timezone_offset=timezone_offset,
            note=note,
            default_unit=default_unit,
        )
        return await self._add(target, None, draft, object_id)

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

        ``object_id`` can be in either case: a sleep started on an Android phone has a lowercase
        id and one from iOS an uppercase id, and the update is posted under the id the record
        already has.
        """
        async with self._lock:
            self._check_can_write(target)
            object_id = self._resolve_id(object_id, self._records, self._tombstones) or object_id
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
            payload = ended_sleep_payload(sleep, finish.minutes, _update_stamp(sleep))
            ended_sleep = decode_payload(payload)
            assert isinstance(ended_sleep, Sleep)
            await self._send(target, number, OP_UPDATE, encode_transaction(ended_sleep))
            return ended_sleep

    def _endable(self, object_id: str) -> tuple[Sleep, bool]:
        """The sleep ``object_id`` names and whether this device already ended it (its own end
        update is in this device's log); else raise why it can't be ended."""
        record = self._saved_record(object_id)
        if not isinstance(record, Sleep):
            raise WrongRecordTypeError(
                f"record {object_id} is a {record.record_type or 'record'}, not a Sleep"
            )
        if record.is_running:
            return record, False
        if object_id in self._own_updates:
            return record, True
        raise TimerNotRunningError(f"sleep {object_id} isn't running")

    def _saved_record(self, object_id: str) -> Record:
        """The record ``object_id`` names, or ``RecordNotFoundError`` when it was deleted or was
        never seen."""
        record = self._records.get(object_id)
        if record is None:
            if object_id in self._tombstones:
                raise RecordNotFoundError(f"record {object_id} was deleted")
            raise RecordNotFoundError(f"no record {object_id} is known from sync")
        return record

    def _resolve_id(self, object_id: str, *held: Collection[str]) -> str | None:
        """The id the engine holds for ``object_id``, whatever its case, or ``None``.

        Android writes lowercase UUIDs and iOS uppercase ones, and records are kept under the id
        exactly as received, so an id a caller passes can differ from the stored one only in
        case. An exact match wins. Otherwise a stored id that is equal when upper-cased is
        returned. ``None`` means none of ``held`` has the id in any case.
        """
        if any(object_id in ids for ids in held):
            return object_id
        wanted = object_id.upper()
        for ids in held:
            for stored in ids:
                if stored.upper() == wanted:
                    return stored
        return None

    async def _add[R: Record](
        self,
        target: WriteTarget,
        baby_id: str | None,
        draft: Draft[R] | Callable[[], Draft[R]],
        object_id: str | None,
        *,
        pictures: Sequence[NewPicture] = (),
    ) -> R:
        """The shared write path of every add: number, upload pictures, post, confirm, apply.
        ``baby_id`` is ``None`` for a record that belongs to the account, not a baby (a pump)."""
        photos = check_pictures(pictures)
        async with self._lock:
            self._check_can_write(target)
            baby = None
            if baby_id is not None:
                baby = self.babies().get(baby_id)
                if baby is None:
                    raise UnknownBabyError(f"no baby {baby_id} is known from sync")
            number = await self._prepare(target)
            if not isinstance(draft, Draft):
                # A pick-list entry and a default dose are resolved here, after the catch-up,
                # so a write that waited behind a sync posts the current ones.
                draft = draft()
            if object_id is not None:
                # An id the engine already holds keeps the case it has; a new one is written in
                # uppercase, like the ids the library generates.
                object_id = (
                    self._resolve_id(object_id, self._created, self._records, self._tombstones)
                    or object_id.upper()
                )
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
            if photos:
                names = await self._upload_pictures(target, photos)
                record = replace(
                    record, picture_note=[picture_entry(name, record.object_id) for name in names]
                )
            await self._send(target, number, OP_ADD, encode_transaction(record))
            return record

    @staticmethod
    async def _upload_pictures(target: WriteTarget, photos: Sequence[NewPicture]) -> list[str]:
        """Upload each photo as a new uppercase UUID plus ".jpg" and its thumbnail as that UUID
        plus "__thumbnail.jpg", and return the UUIDs. The files go before the record that names
        them, since other devices fetch a picture as soon as they merge its record."""
        names = [str(uuid.uuid4()).upper() for _ in photos]
        for name, photo in zip(names, photos, strict=True):
            await target.upload_picture(f"{name}.jpg", photo.photo)
            await target.upload_picture(f"{name}__thumbnail.jpg", photo.thumbnail)
        return names

    async def delete_record(
        self, target: WriteTarget, object_id: str, *, any_device: bool = False
    ) -> None:
        """Post a delete of a record this device created, carrying the full record.

        The payload is the record exactly as last saved with ``newFlage`` set to ``"false"``;
        like the app, the timestamp is not bumped. Raises ``NotOwnRecordError`` for a record
        another device created (or one never seen), and ``RecordNotFoundError`` if this
        device's record is already deleted. ``object_id`` matches whatever its case, and the delete
        is posted under the id the record has.

        With ``any_device`` true, a record another device created can be deleted too, as the
        apps delete each other's records (and this library's), with the same payload. That
        covers a baby's records and pumps. A baby or a pick-list entry another device created
        raises ``WrongRecordTypeError``, since the apps refuse to delete one that is still in
        use; so does a record of a type the library doesn't know. A record never seen, or
        already deleted, raises ``RecordNotFoundError``.
        """
        async with self._lock:
            self._check_can_write(target)
            object_id = (
                self._resolve_id(object_id, self._created, self._records, self._tombstones)
                or object_id
            )
            if object_id not in self._created:
                if not any_device:
                    raise NotOwnRecordError(f"record {object_id} wasn't created by this device")
                self._deletable(object_id)
                # Every device's log, not just this one's: the delete carries the whole record,
                # so it must hold whatever another device saved since the last sync.
                await self._catch_up(target)
            elif object_id not in self._records:
                raise RecordNotFoundError(f"record {object_id} is already deleted")
            number = await self._prepare(target)
            # The catch-up may have brought in a delete, or another device's newer version.
            record = (
                self._records.get(object_id)
                if object_id in self._created
                else self._deletable(object_id)
            )
            if record is None:
                raise RecordNotFoundError(f"record {object_id} is already deleted")
            payload = record.to_payload()
            payload["newFlage"] = "false"
            await self._send(target, number, OP_DELETE, encode_transaction(decode_payload(payload)))

    def _deletable(self, object_id: str) -> Record:
        """Another device's record ``object_id`` names, or raise why it can't be deleted."""
        if object_id in self.babies():
            raise WrongRecordTypeError(
                f"record {object_id} is a Baby, which only the device that created it can delete"
            )
        record = self._saved_record(object_id)
        if isinstance(record, _NOT_DELETABLE_FROM_OTHERS):
            raise WrongRecordTypeError(
                f"record {object_id} is a {record.record_type or 'record'}, which only the "
                "device that created it can delete"
            )
        return record

    def _check_can_write(self, target: WriteTarget) -> None:
        if self._device_uuid is None:
            raise ValueError("this engine has no device_uuid, so it can't write")
        if target.device_uuid != self._device_uuid:
            raise ValueError("the client's device ID doesn't match the engine's device_uuid")
        if not self._synced:
            raise NotSyncedError("sync at least once before writing")

    def _check_baby(self, target: WriteTarget, baby_id: object) -> None:
        """Refuse a missing or unknown baby before a pick list is searched, so a milestone left
        out for a baby that isn't there isn't reported as an unknown milestone. ``_add`` checks
        again under the lock."""
        self._check_can_write(target)
        if not isinstance(baby_id, str) or baby_id not in self.babies():
            raise UnknownBabyError(f"no baby {baby_id!r} is known from sync")

    async def _prepare(self, target: WriteTarget) -> int:
        """Settle any earlier failed post, catch up on this device's log, and choose the next
        number. Hold the lock."""
        own = self._require_device()
        server_last = await self._server_last(target)
        self._check_account(target)
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
        except (AuthenticationError, AccountChangedError):
            self._pending = None  # refused before it was sent: nothing landed
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
        seed_read = False
        if not self.seeded:
            await self._read_seed(source)
            seed_read = self.seeded
        devices, applied, skipped = await self._catch_up(source)
        return SyncResult(devices, applied, skipped, seed_read)

    async def _catch_up(self, source: SyncSource) -> tuple[int, int, int]:
        """Read every device's log through its ``LastSyncID``: how many devices were read, and
        the transactions applied and skipped. Hold the lock."""
        listed = await source.get_devices()
        self._check_account(source)
        devices = self.devices_to_fetch(listed)
        applied = skipped = 0
        for device in devices:
            result = await self._read_through(source, device.device_uuid, device.last_sync_id)
            applied += result.applied
            skipped += result.skipped
        return len(devices), applied, skipped

    async def _read_seed(self, source: SyncSource) -> None:
        """Load the account's seed, once, if ``source`` can fetch it (``get_seed``).

        A download that fails, or is redirected, is warned about and tried again on the next sync;
        the logs still sync meanwhile. A seed that downloads but can't be read, or that is over
        the client's size cap, is warned about and not tried again until a newer ``SEED_FORMAT``. A
        rejected sign-in, or a sign-in to a different sync group, raises, as it would for the logs.
        """
        get_seed = getattr(source, "get_seed", None)
        if get_seed is None:
            return
        try:
            archive = await get_seed()
        except (AuthenticationError, AccountChangedError):
            raise
        except DownloadRefusedError as err:
            # An oversize seed would come back the same way on every sync.
            _LOGGER.warning(
                "Can't download the account's seed, so older history is missing: %s", err
            )
            self._seeded = SEED_FORMAT
            return
        except BabyTrackerError as err:
            log = _LOGGER.debug if self._seed_failed else _LOGGER.warning
            log("Couldn't download the account's seed; trying again next sync: %s", err)
            self._seed_failed = True
            return
        self._check_account(source)  # never merge a seed from a different sync group
        if archive:
            try:
                seed = await asyncio.to_thread(read_seed, archive)
            except ProtocolError as err:
                _LOGGER.warning(
                    "Can't read the account's seed, so older history is missing: %s", err
                )
            else:
                self.load_seed(seed)
        self._seeded = SEED_FORMAT

    def load_seed(self, seed: Seed) -> None:
        """Merge a seed's records under the logs. No I/O.

        Each seed record is stored as an add from no device, number 0, so the merge rules treat
        it as older than any log entry with the same timestamp: a log copy of the record wins
        unless its timestamp is older than the seed's, as between any two devices, and a delete
        removes it unless the seed's copy is newer. Every log is still read from the start, as
        the app does after loading a seed.
        """
        for record in seed.records:
            stamp = record.timestamp.timestamp() if record.timestamp is not None else 0.0
            self._store(record, _Version(stamp, _RANK_ADD, "", 0))
        self._seeded = SEED_FORMAT

    async def _read_through(self, source: SyncSource, device_uuid: str, last: int) -> ApplyResult:
        """Read and apply a device's log page by page from what's been seen, until reaching
        ``last``, a read comes back empty, or a read brings nothing new (warned, so a stuck
        server can't loop forever)."""
        applied = skipped = 0
        while (since := self._last_seen.get(device_uuid, 0)) < last:
            transactions = await source.get_transactions(device_uuid, since)
            self._check_account(source)  # a sign-in during the read may have joined a new group
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
        if (
            isinstance(record, UnknownRecord)
            and record.record_type not in RECORD_TYPES
            and record.record_type not in self._logged_unknown_types
        ):
            # The app has types the library doesn't model yet, and a first sync loads every
            # record, so this is logged once per type and only for debugging.
            self._logged_unknown_types.add(record.record_type)
            _LOGGER.debug(
                "Unknown record type %r (first seen at %s); keeping the raw payloads",
                record.record_type,
                where,
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

    def latest_pump(self) -> Pump | None:
        """The newest pump on the account, by the time it happened, whoever logged it. Pumps
        belong to the account, not a baby, so ``latest_records`` never includes them."""
        latest: Pump | None = None
        for record in self._records.values():
            if not isinstance(record, Pump) or record.time is None:
                continue
            if latest is None or _sort_key(record) > _sort_key(latest):
                latest = record
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
            "seeded": self._seeded,
            "last_seen": dict(sorted(self._last_seen.items())),
            "account_id": self._account_id,
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
        account_id = data.get("account_id")
        engine._account_id = account_id if is_account_id(account_id) else None
        # State saved before seeds were read has none: the next sync reads it.
        engine._seeded = _seed_format(data.get("seeded"))
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


def _seed_format(saved: object) -> int:
    """The seed format saved state was read at. ``true`` is from before formats were saved."""
    if saved is True:
        return 1
    return saved if isinstance(saved, int) and not isinstance(saved, bool) and saved > 0 else 0


def _wanted(value: object, what: str) -> str:
    """A pick-list name or ID as given, or ``InvalidSelectionNameError`` when it's empty or not
    text."""
    if not isinstance(value, str) or not value.strip():
        raise InvalidSelectionNameError(what, value)
    return value


def _some(changes: dict[str, Any]) -> dict[str, Any]:
    """``changes``, or ``ValueError`` when an edit was given nothing to change."""
    if not changes:
        raise ValueError("give something to change")
    return changes


def _update_stamp(record: Record) -> datetime:
    """An update's timestamp: now, or one second past the stored one if that is ahead of this
    clock, so the update wins the merge on every device."""
    stamp = _now()
    if record.timestamp is not None and record.timestamp >= stamp:
        stamp = record.timestamp.astimezone(UTC) + timedelta(seconds=1)
    return stamp


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
