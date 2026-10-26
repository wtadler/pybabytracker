"""Equal-timestamp tie-breaks: an update or delete outranks the add it follows."""

from __future__ import annotations

import itertools
import json
import random
from typing import Any

import pytest
from factories import encode_b64, payload_for

from pybabytracker import (
    OP_ADD,
    OP_DELETE,
    OP_UPDATE,
    Device,
    RawTransaction,
    SyncEngine,
    UnknownRecord,
)

LOW = "AAAA0000-0000-4000-8000-000000000001"  # sorts before HIGH
HIGH = "ZZZZ0000-0000-4000-8000-000000000002"
MID = "MMMM0000-0000-4000-8000-000000000003"

STAMP = "2026-10-26 12:00:05 +0000"
RID = "AAAAAAAA-0000-4000-8000-000000000001"


def tx(sync_id: int, op: int, payload: Any) -> RawTransaction:
    return RawTransaction(sync_id=sync_id, op_code=op, transaction=encode_b64(payload))


def diaper(note: str, stamp: str = STAMP, object_id: str = RID) -> dict[str, Any]:
    return payload_for("Diaper", object_id, note=note, timestamp=stamp)


class MemorySource:
    def __init__(self, logs: dict[str, list[RawTransaction]], *, reverse: bool = False) -> None:
        self.logs = logs
        self.reverse = reverse

    async def get_devices(self) -> list[Device]:
        devices = [
            Device(
                device_uuid=u,
                last_sync_id=max((t.sync_id for t in log), default=0),
                device_name=u[:4],
                device_os_info="test",
            )
            for u, log in self.logs.items()
        ]
        return devices[::-1] if self.reverse else devices

    async def get_transactions(self, device_uuid: str, since_sync_id: int) -> list[RawTransaction]:
        return [t for t in self.logs[device_uuid] if t.sync_id > since_sync_id]


async def synced(logs: dict[str, list[RawTransaction]], *, reverse: bool = False) -> SyncEngine:
    engine = SyncEngine()
    await engine.sync(MemorySource(logs, reverse=reverse))
    return engine


def applied_in(order: tuple[str, ...], logs: dict[str, list[RawTransaction]]) -> SyncEngine:
    engine = SyncEngine()
    for device in order:
        engine.apply(device, logs[device])
    return engine


def reloaded(engine: SyncEngine) -> SyncEngine:
    return SyncEngine.from_dict(json.loads(json.dumps(engine.to_dict())))


BOTH_ORDERS = [(HIGH, LOW), (LOW, HIGH)]  # (adder, editor)


@pytest.mark.parametrize(("adder", "editor"), BOTH_ORDERS)
@pytest.mark.parametrize("reverse", [False, True])
async def test_an_edit_beats_the_add_it_edits_at_an_equal_timestamp(
    adder: str, editor: str, reverse: bool
) -> None:
    logs = {
        adder: [tx(1, OP_ADD, diaper("added"))],
        editor: [tx(1, OP_UPDATE, diaper("edited"))],
    }
    engine = await synced(logs, reverse=reverse)
    assert engine.records[RID].note == "edited"


@pytest.mark.parametrize(("adder", "deleter"), BOTH_ORDERS)
@pytest.mark.parametrize("reverse", [False, True])
async def test_a_delete_beats_the_add_it_deletes_at_an_equal_timestamp(
    adder: str, deleter: str, reverse: bool
) -> None:
    logs = {
        adder: [tx(1, OP_ADD, diaper("added"))],
        deleter: [tx(1, OP_DELETE, diaper("added"))],
    }
    engine = await synced(logs, reverse=reverse)
    assert RID not in engine.records


@pytest.mark.parametrize(("adder", "editor"), BOTH_ORDERS)
def test_the_edit_wins_when_the_add_arrives_second_in_a_later_sync(adder: str, editor: str) -> None:
    engine = SyncEngine()
    engine.apply(editor, [tx(1, OP_UPDATE, diaper("edited"))])
    engine.apply(adder, [tx(1, OP_ADD, diaper("added"))])
    assert engine.records[RID].note == "edited"


@pytest.mark.parametrize("reverse", [False, True])
async def test_two_edits_with_equal_timestamps_resolve_the_same_in_every_fetch_order(
    reverse: bool,
) -> None:
    logs = {
        LOW: [tx(1, OP_UPDATE, diaper("from low"))],
        HIGH: [tx(1, OP_UPDATE, diaper("from high"))],
    }
    notes = {applied_in(order, logs).records[RID].note for order in itertools.permutations(logs)}
    assert len(notes) == 1
    assert (await synced(logs, reverse=reverse)).records[RID].note in notes


@pytest.mark.parametrize(("deleter", "editor"), BOTH_ORDERS)
def test_a_delete_beats_an_edit_at_an_equal_timestamp_in_both_orders(
    deleter: str, editor: str
) -> None:
    logs = {
        deleter: [tx(1, OP_DELETE, diaper("x"))],
        editor: [tx(1, OP_UPDATE, diaper("edited"))],
    }
    for order in itertools.permutations(logs):
        assert RID not in applied_in(order, logs).records


@pytest.mark.parametrize(("adder", "editor"), BOTH_ORDERS)
def test_a_later_add_from_a_third_device_does_not_beat_the_edit(adder: str, editor: str) -> None:
    logs = {
        adder: [tx(1, OP_ADD, diaper("added"))],
        editor: [tx(1, OP_UPDATE, diaper("edited"))],
        MID: [tx(1, OP_ADD, diaper("added again"))],
    }
    for order in itertools.permutations(logs):
        assert applied_in(order, logs).records[RID].note == "edited"


def test_within_one_device_the_later_number_still_wins_at_an_equal_timestamp() -> None:
    engine = SyncEngine()
    engine.apply(LOW, [tx(1, OP_ADD, diaper("one")), tx(2, OP_UPDATE, diaper("two"))])
    engine.apply(LOW, [tx(3, OP_UPDATE, diaper("three"))])
    assert engine.records[RID].note == "three"


def test_a_strictly_newer_add_still_beats_an_older_edit() -> None:
    engine = SyncEngine()
    engine.apply(LOW, [tx(1, OP_UPDATE, diaper("edited", "2026-10-26 12:00:05 +0000"))])
    engine.apply(HIGH, [tx(1, OP_ADD, diaper("newer add", "2026-10-26 12:00:06 +0000"))])
    assert engine.records[RID].note == "newer add"


def test_a_strictly_older_edit_still_loses_to_an_add_from_the_other_device() -> None:
    engine = SyncEngine()
    engine.apply(HIGH, [tx(1, OP_ADD, diaper("add", "2026-10-26 12:00:06 +0000"))])
    engine.apply(LOW, [tx(1, OP_UPDATE, diaper("older edit", "2026-10-26 12:00:05 +0000"))])
    assert engine.records[RID].note == "add"


# --- raw records whose timestamp failed to parse (version timestamp 0) --------------------


def raw_payload(note: str) -> dict[str, Any]:
    return payload_for("Sleep", RID, note=note, timestamp="not a time")


def test_an_unparseable_timestamp_counts_as_zero_and_the_record_is_kept_raw() -> None:
    engine = SyncEngine()
    engine.apply(LOW, [tx(1, OP_ADD, raw_payload("raw"))])
    record = engine.records[RID]
    assert isinstance(record, UnknownRecord)
    assert record.timestamp is None


@pytest.mark.parametrize(("adder", "editor"), BOTH_ORDERS)
def test_an_edit_beats_an_add_when_both_timestamps_are_unparseable(adder: str, editor: str) -> None:
    logs = {
        adder: [tx(1, OP_ADD, raw_payload("added"))],
        editor: [tx(1, OP_UPDATE, raw_payload("edited"))],
    }
    for order in itertools.permutations(logs):
        record = applied_in(order, logs).records[RID]
        assert isinstance(record, UnknownRecord)
        assert record.raw["note"] == "edited"


@pytest.mark.parametrize(("adder", "deleter"), BOTH_ORDERS)
def test_a_delete_beats_an_add_when_both_timestamps_are_unparseable(
    adder: str, deleter: str
) -> None:
    logs = {
        adder: [tx(1, OP_ADD, raw_payload("added"))],
        deleter: [tx(1, OP_DELETE, raw_payload("added"))],
    }
    for order in itertools.permutations(logs):
        assert RID not in applied_in(order, logs).records


# --- saved state --------------------------------------------------------------------------


@pytest.mark.parametrize(("adder", "editor"), BOTH_ORDERS)
def test_a_saved_edit_still_beats_a_later_add_at_an_equal_timestamp(
    adder: str, editor: str
) -> None:
    engine = SyncEngine()
    engine.apply(editor, [tx(1, OP_UPDATE, diaper("edited"))])
    restored = reloaded(engine)
    restored.apply(adder, [tx(1, OP_ADD, diaper("added"))])
    assert restored.records[RID].note == "edited"


@pytest.mark.parametrize(("adder", "editor"), BOTH_ORDERS)
def test_a_saved_add_still_loses_to_a_later_edit_at_an_equal_timestamp(
    adder: str, editor: str
) -> None:
    engine = SyncEngine()
    engine.apply(adder, [tx(1, OP_ADD, diaper("added"))])
    restored = reloaded(engine)
    restored.apply(editor, [tx(1, OP_UPDATE, diaper("edited"))])
    assert restored.records[RID].note == "edited"


@pytest.mark.parametrize(("adder", "deleter"), BOTH_ORDERS)
def test_a_saved_delete_still_beats_a_later_add_at_an_equal_timestamp(
    adder: str, deleter: str
) -> None:
    engine = SyncEngine()
    engine.apply(deleter, [tx(1, OP_DELETE, diaper("added"))])
    restored = reloaded(engine)
    restored.apply(adder, [tx(1, OP_ADD, diaper("added"))])
    assert RID not in restored.records


@pytest.mark.parametrize(("adder", "deleter"), BOTH_ORDERS)
def test_a_saved_add_loses_to_a_later_delete_at_an_equal_timestamp(
    adder: str, deleter: str
) -> None:
    engine = SyncEngine()
    engine.apply(adder, [tx(1, OP_ADD, diaper("added"))])
    restored = reloaded(engine)
    restored.apply(deleter, [tx(1, OP_DELETE, diaper("added"))])
    assert RID not in restored.records


def test_saved_state_round_trips_with_the_new_version_key() -> None:
    engine = SyncEngine()
    engine.apply(LOW, [tx(1, OP_ADD, diaper("added")), tx(2, OP_UPDATE, diaper("edited"))])
    engine.apply(
        HIGH, [tx(1, OP_DELETE, diaper("gone", object_id="BBBBBBBB-0000-4000-8000-000000000002"))]
    )
    saved = engine.to_dict()
    assert saved["version"] == 4
    assert reloaded(engine).to_dict() == saved


def test_the_saved_version_key_records_the_op_rank() -> None:
    engine = SyncEngine()
    engine.apply(LOW, [tx(1, OP_ADD, diaper("added"))])
    add_rank = engine.to_dict()["records"][0]["version"]["rank"]
    engine.apply(LOW, [tx(2, OP_UPDATE, diaper("edited"))])
    update_rank = engine.to_dict()["records"][0]["version"]["rank"]
    engine.apply(LOW, [tx(3, OP_DELETE, diaper("edited"))])
    delete_rank = engine.to_dict()["tombstones"][0]["version"]["rank"]
    assert add_rank < update_rank < delete_rank


def version_2_state(*, tombstone: bool = False) -> dict[str, Any]:
    """State as the previous release saved it: the version key has no op rank."""
    state: dict[str, Any] = {
        "version": 2,
        "last_seen": {LOW: 1},
        "records": [],
        "tombstones": [],
    }
    entry = {"timestamp": 1793016005.0, "device": LOW, "sync_id": 1}
    if tombstone:
        state["tombstones"] = [{"object_id": RID, "version": entry}]
    else:
        state["records"] = [{"version": entry, "payload": diaper("old save")}]
    return state


def test_a_version_2_state_still_loads_and_is_saved_as_the_current_version() -> None:
    engine = SyncEngine.from_dict(version_2_state())
    assert engine.records[RID].note == "old save"
    saved = engine.to_dict()
    assert saved["version"] == 4
    assert "rank" in saved["records"][0]["version"]


def test_a_version_2_state_keeps_the_merge_rules_for_new_transactions() -> None:
    engine = SyncEngine.from_dict(version_2_state())
    engine.apply(HIGH, [tx(1, OP_UPDATE, diaper("newer", "2026-10-26 12:00:06 +0000"))])
    assert engine.records[RID].note == "newer"
    engine.apply(MID, [tx(1, OP_DELETE, diaper("newer", "2026-10-26 12:00:06 +0000"))])
    assert RID not in engine.records


def test_a_version_2_tombstone_still_blocks_an_equal_timestamp_add() -> None:
    engine = SyncEngine.from_dict(version_2_state(tombstone=True))
    engine.apply(HIGH, [tx(1, OP_ADD, diaper("added"))])
    assert RID not in engine.records
    assert "rank" in engine.to_dict()["tombstones"][0]["version"]


def test_an_unsupported_future_version_still_fails_clearly() -> None:
    with pytest.raises(ValueError, match="version"):
        SyncEngine.from_dict({"version": 5, "last_seen": {}, "records": []})


# --- property: random causal histories, equal timestamps forced often ---------------------

DEVICES = (LOW, MID, HIGH)
BASE = 1793016000  # 2026-10-26 12:00:00 UTC


def stamp_string(offset: int) -> str:
    import datetime as dt

    moment = dt.datetime.fromtimestamp(BASE + offset, dt.UTC)
    return moment.strftime("%Y-%m-%d %H:%M:%S +0000")


def build_history(
    rng: random.Random,
) -> tuple[dict[str, list[RawTransaction]], dict[str, str | None]]:
    """Random records, each with an add, then edits, then maybe a delete.

    Each record's operations form a causal chain: timestamps never go backward along it, and
    about two in three steps repeat the previous timestamp (a device that doesn't bump the stamp
    on an edit, or the one-second resolution). Each step is posted by a random device.
    Returns the device logs and, per record, the expected surviving note (None if deleted).
    """
    counters = {d: 0 for d in DEVICES}
    logs: dict[str, list[RawTransaction]] = {d: [] for d in DEVICES}
    expected: dict[str, str | None] = {}
    # Plan every operation first, then post per device in a causal (timestamp) order.
    plan: list[tuple[int, int, str, int, dict[str, Any]]] = []
    order = 0
    for r in range(rng.randint(1, 5)):
        object_id = f"AAAAAAAA-0000-4000-8000-{r + 1:012d}"
        offset = rng.randint(0, 3)
        ops: list[tuple[int, str, int, str]] = []  # (offset, device, op, note)
        for step in range(rng.randint(1, 5)):
            op = OP_ADD if step == 0 else OP_UPDATE
            if step and rng.random() > 0.65:
                offset += rng.randint(1, 2)
            ops.append((offset, rng.choice(DEVICES), op, f"{object_id[-2:]}-{step}"))
        deleted = rng.random() < 0.3
        if deleted:
            if rng.random() > 0.65:
                offset += 1
            ops.append((offset, rng.choice(DEVICES), OP_DELETE, "deleted"))
        for off, device, op, note in ops:
            order += 1
            plan.append((off, order, device, op, diaper(note, stamp_string(off), object_id)))
        expected[object_id] = None if deleted else _winner(ops)
    for _off, _order, device, op, payload in sorted(plan, key=lambda p: (p[0], p[1])):
        counters[device] += 1
        logs[device].append(tx(counters[device], op, payload))
    return logs, expected


def _winner(ops: list[tuple[int, str, int, str]]) -> str:
    """The note with the highest (timestamp, op rank, device, number), as the spec words it."""
    rank = {OP_ADD: 0, OP_UPDATE: 1}
    best_key: tuple[int, int, str, int] | None = None
    best_note = ""
    seq: dict[str, int] = {}
    for off, device, op, note in sorted(ops, key=lambda o: o[0]):
        seq[device] = seq.get(device, 0) + 1
        key = (off, rank[op], device, seq[device])
        if best_key is None or key > best_key:
            best_key, best_note = key, note
    return best_note


def check_history(logs: dict[str, list[RawTransaction]]) -> dict[str, str | None]:
    """Apply the logs every way a sync could, and require one single outcome."""
    outcomes: set[str] = set()
    state: dict[str, str | None] = {}
    for order in itertools.permutations(DEVICES):
        engine = applied_in(order, logs)
        outcomes.add(json.dumps(engine.to_dict(), sort_keys=True))
        state = {i: getattr(r, "note", None) for i, r in engine.records.items()}
    # Interleaved halves: each device's log arrives in two pages, devices in turn.
    for order in itertools.permutations(DEVICES):
        engine = SyncEngine()
        for half in (0, 1):
            for device in order:
                log = logs[device]
                cut = len(log) // 2
                engine.apply(device, log[:cut] if half == 0 else log[cut:])
        outcomes.add(json.dumps(engine.to_dict(), sort_keys=True))
    # A save and load between the two halves.
    for order in itertools.permutations(DEVICES):
        engine = SyncEngine()
        for device in order:
            engine.apply(device, logs[device][: len(logs[device]) // 2])
        engine = reloaded(engine)
        for device in order:
            engine.apply(device, logs[device])
        outcomes.add(json.dumps(engine.to_dict(), sort_keys=True))
    assert len(outcomes) == 1, "final state depends on the fetch order"
    return state


@pytest.mark.parametrize("seed", range(300))
def test_random_causal_histories_give_one_final_state_in_every_fetch_order(seed: int) -> None:
    rng = random.Random(seed)
    logs, expected = build_history(rng)
    state = check_history(logs)
    assert state == {i: n for i, n in expected.items() if n is not None}
