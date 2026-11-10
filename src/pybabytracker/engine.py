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
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .builders import (
    Draft,
    draft_activity,
    draft_bottle,
    draft_diaper,
    draft_food,
    draft_growth,
    draft_medication,
    draft_milestone,
    draft_nursing,
    draft_other_feed,
    draft_pump,
    draft_sleep,
    draft_sleep_start,
    draft_temperature,
    draft_vaccine,
    ended_sleep_payload,
    resolve_sleep_end,
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
    Measurement,
    Medication,
    MedicationSelection,
    Milestone,
    MilestoneSelection,
    Nursing,
    OtherActivity,
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
        self._own_updates: set[str] = set()  # objects this device posted an update (an end) to
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
    ) -> Milestone:
        """Post a milestone the baby reached, and apply it locally.

        ``milestone`` is a name or ID from ``milestone_choices(baby_id)``: one the app ships
        with, or one added in the app and known from sync. A name matches ignoring case and
        surrounding spaces; a name that matches two entries raises ``AmbiguousSelectionError``,
        and the ID must be passed instead. The record embeds that milestone as the app does. An
        empty or non-text ``milestone`` raises ``InvalidSelectionNameError``, an unknown one
        ``UnknownMilestoneError``, and no baby ``UnknownBabyError``, all before any request;
        adding a new milestone to the pick list isn't supported yet. The other arguments work as
        for ``add_diaper``.
        """
        self._check_baby(target, baby_id)

        def resolve() -> Draft[Milestone]:
            choice = self.find_milestone(milestone, baby_id)
            return draft_milestone(
                choice.selection, time=time, timezone_offset=timezone_offset, note=note
            )

        resolve()  # a bad name or an unknown or ambiguous milestone fails before the lock
        return await self._add(target, baby_id, resolve, object_id)

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
    ) -> OtherActivity:
        """Post an activity, such as tummy time or a bath, and apply it locally.

        ``activity`` is a name or ID from ``activity_choices()``: one of the app's built-in
        types (``BUILT_IN_ACTIVITIES``) or one added in the app and known from sync, matched as
        for ``add_milestone``. The record embeds it as ``desc``. ``duration`` is whole minutes,
        0 (the default) when it wasn't timed, at most ``MAX_ACTIVITY_MINUTES``. Bad input raises
        the errors ``add_milestone`` does (``UnknownActivityError`` for an unknown type) or
        ``ValueError`` for a bad duration, before any request; adding a new type to the pick
        list isn't supported yet. The other arguments work as for ``add_diaper``.
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
        return await self._add(target, baby_id, resolve, object_id)

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

    def activity_choices(self) -> list[SelectionChoice]:
        """Every activity type a record can name: the app's built-ins in its order, then added
        ones, by name. Copies are chosen as for ``milestone_choices``. A type added in the app
        syncs as its own ``OtherActivityDescription`` record (kept raw) and is embedded in each
        activity of that type.
        """
        synced = [
            r.to_payload()
            for r in self._records.values()
            if r.record_type == "OtherActivityDescription"
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
                if choice.name.strip().casefold() == wanted and choice.object_id != object_id:
                    raise SelectionExistsError("food", name.strip(), choice.object_id)
            return draft

        resolve()
        return await self._add(target, None, resolve, object_id)

    def food_choices(self) -> list[SelectionChoice]:
        """Every food a solid food record can name: the app's built-ins in its order, then
        added ones, by name. Copies are chosen as for ``milestone_choices``. A food added in the
        app syncs as its own ``OtherFeedSelection`` record and is embedded in each record of it
        as ``feedType``; either makes it a choice.
        """
        synced = [
            r.to_payload() for r in self._records.values() if isinstance(r, OtherFeedSelection)
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
    ) -> R:
        """The shared write path of every add: number, post, confirm, apply. ``baby_id`` is
        ``None`` for a record that belongs to the account, not a baby (a pump)."""
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
            await self._send(target, number, OP_ADD, encode_transaction(record))
            return record

    async def delete_record(self, target: WriteTarget, object_id: str) -> None:
        """Post a delete of a record this device created, carrying the full record.

        The payload is the record exactly as last saved with ``newFlage`` set to ``"false"``;
        like the app, the timestamp is not bumped. Raises ``NotOwnRecordError`` for a record
        another device created (or one never seen), and ``RecordNotFoundError`` if this
        device's record is already deleted. ``object_id`` matches whatever its case, and the delete
        is posted under the id the record has.
        """
        async with self._lock:
            self._check_can_write(target)
            object_id = self._resolve_id(object_id, self._created, self._records) or object_id
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
        listed = await source.get_devices()
        self._check_account(source)
        devices = self.devices_to_fetch(listed)
        applied = skipped = 0
        for device in devices:
            result = await self._read_through(source, device.device_uuid, device.last_sync_id)
            applied += result.applied
            skipped += result.skipped
        return SyncResult(len(devices), applied, skipped, seed_read)

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
