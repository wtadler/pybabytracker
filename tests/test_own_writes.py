"""``own_writes``: this device's adds that still exist, in the order it wrote them."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from conftest import PROBE_UUID
from factories import payload_for
from fake_server import FakeSyncServer
from test_writes import (  # noqa: F401  (quick_client is a fixture; F811 at use sites)
    PHONE,
    add,
    decode,
    nth_post,
    own_posts,
    quick_client,
    rid,
    seed,
    synced_engine,
)

from pybabytracker import (
    DIAPER_DIRTY,
    DIAPER_WET,
    OP_ADD,
    OP_DELETE,
    OP_UPDATE,
    BabyTrackerClient,
    BabyTrackerConnectionError,
    Diaper,
    RawTransaction,
    SyncEngine,
)
from pybabytracker.models import decode_payload, encode_transaction

LATER = "2030-01-01 00:00:00 +0000"


def edited_by_phone(server: FakeSyncServer, object_id: str, note: str = "edited") -> None:
    """Another device edits a record: an update with a newer timestamp."""
    payload = payload_for("Diaper", object_id, note=note, timestamp=LATER)
    server.add_transaction(PHONE, OP_UPDATE, payload)


def deleted_by_phone(server: FakeSyncServer, object_id: str) -> None:
    server.add_transaction(PHONE, OP_DELETE, payload_for("Diaper", object_id, timestamp=LATER))


async def three_adds(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> tuple[SyncEngine, list[str]]:
    seed(fake_server)
    engine = await synced_engine(client)
    ids = [(await add(engine, client)).object_id for _ in range(3)]
    return engine, ids


# --- order and numbers -------------------------------------------------------------------


async def test_a_fresh_engine_has_no_own_writes() -> None:
    assert SyncEngine(device_uuid=PROBE_UUID).own_writes() == []


async def test_three_adds_are_listed_in_add_order_with_their_numbers(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, ids = await three_adds(client, fake_server)

    assert engine.own_writes() == [(ids[0], 1), (ids[1], 2), (ids[2], 3)]


async def test_another_device_editing_the_newest_leaves_it_last(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, ids = await three_adds(client, fake_server)
    edited_by_phone(fake_server, ids[2])
    edited_by_phone(fake_server, ids[0])  # and the oldest, which must stay first

    await engine.sync(client)

    assert engine.records[ids[2]].note == "edited"  # the edit won the version
    assert engine.own_writes() == [(ids[0], 1), (ids[1], 2), (ids[2], 3)]


async def test_the_accessor_returns_a_fresh_list_each_call(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, ids = await three_adds(client, fake_server)

    engine.own_writes().clear()

    assert len(engine.own_writes()) == 3


async def test_another_devices_adds_are_not_own_writes(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)  # the phone's diaper is rid(1)
    engine = await synced_engine(client)

    assert rid(1) in engine.records
    assert engine.own_writes() == []


# --- deletes -------------------------------------------------------------------------------


async def test_a_record_this_device_deleted_drops_out(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, ids = await three_adds(client, fake_server)

    await engine.delete_record(client, ids[1])

    assert engine.own_writes() == [(ids[0], 1), (ids[2], 3)]
    assert ids[1] in engine.created_ids  # created_ids still remembers it


async def test_a_record_another_device_deleted_drops_out(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, ids = await three_adds(client, fake_server)
    deleted_by_phone(fake_server, ids[2])

    await engine.sync(client)

    assert ids[2] not in engine.records
    assert engine.own_writes() == [(ids[0], 1), (ids[1], 2)]


async def test_an_own_record_deleted_elsewhere_stays_gone_after_save_and_load(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, ids = await three_adds(client, fake_server)
    deleted_by_phone(fake_server, ids[0])
    await engine.sync(client)

    restored = SyncEngine.from_dict(json.loads(json.dumps(engine.to_dict())))

    assert restored.own_writes() == [(ids[1], 2), (ids[2], 3)]
    assert "created_numbers" in engine.to_dict()
    assert ids[0] not in engine.to_dict()["created_numbers"]


async def test_an_add_then_its_own_delete_then_a_re_add_of_the_same_id(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    diaper = await add(engine, client)  # number 1
    await engine.delete_record(client, diaper.object_id)  # number 2
    assert engine.own_writes() == []

    # The same object_id comes back from this device's own log (a re-add at number 3).
    payload = decode(nth_post(fake_server, 0))
    payload["timestamp"] = LATER
    transaction = encode_transaction(decode_payload(payload))
    engine.apply(PROBE_UUID, [RawTransaction(3, OP_ADD, transaction)])

    assert engine.own_writes() == [(diaper.object_id, 3)]


async def test_a_re_add_read_back_by_sync_takes_the_newer_number(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    fake_server.add_device(PROBE_UUID, "Test Device")
    fake_server.add_transaction(PROBE_UUID, OP_ADD, payload_for("Diaper", rid(70)))  # 1
    fake_server.add_transaction(PROBE_UUID, OP_ADD, payload_for("Diaper", rid(71)))  # 2
    fake_server.add_transaction(PROBE_UUID, OP_DELETE, payload_for("Diaper", rid(70)))  # 3
    fake_server.add_transaction(
        PROBE_UUID, OP_ADD, payload_for("Diaper", rid(70), timestamp=LATER)
    )  # 4

    engine = await synced_engine(client)

    assert engine.own_writes() == [(rid(71), 2), (rid(70), 4)]


# --- own log learned during sync ------------------------------------------------------------


async def test_own_adds_read_back_from_the_log_carry_their_log_numbers(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    fake_server.add_device(PROBE_UUID, "Test Device")
    for n in (70, 71, 72):
        fake_server.add_transaction(PROBE_UUID, OP_ADD, payload_for("Diaper", rid(n)))

    engine = await synced_engine(client)

    assert engine.own_writes() == [(rid(70), 1), (rid(71), 2), (rid(72), 3)]


async def test_a_restart_from_stale_state_learns_the_missed_adds_with_their_numbers(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, ids = await three_adds(client, fake_server)
    stale = json.loads(json.dumps(engine.to_dict()))
    later = await add(engine, client)  # number 4, after the state was saved
    restored = SyncEngine.from_dict(stale)
    assert restored.own_writes() == [(ids[0], 1), (ids[1], 2), (ids[2], 3)]

    await restored.sync(client)

    assert restored.own_writes() == [(ids[0], 1), (ids[1], 2), (ids[2], 3), (later.object_id, 4)]


async def test_local_apply_matches_a_replay_of_the_log(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, ids = await three_adds(client, fake_server)
    await engine.delete_record(client, ids[0])

    replayed = await synced_engine(client)

    assert replayed.own_writes() == engine.own_writes() == [(ids[1], 2), (ids[2], 3)]


# --- a post that lands late -------------------------------------------------------------------


async def test_a_post_that_lands_late_keeps_its_own_number_and_place(
    quick_client: BabyTrackerClient,
    fake_server: FakeSyncServer,  # noqa: F811
) -> None:
    seed(fake_server)
    engine = await synced_engine(quick_client)
    fake_server.post_script = ["timeout-late"]
    with pytest.raises(BabyTrackerConnectionError):
        await add(engine, quick_client, DIAPER_WET)
    assert engine.own_writes() == []  # nothing confirmed yet
    await asyncio.sleep(fake_server.post_stall)  # the stalled post lands now
    late_id = decode(own_posts(fake_server)[0])["objectID"]

    second = await add(engine, quick_client, DIAPER_DIRTY)

    assert engine.own_writes() == [(late_id, 1), (second.object_id, 2)]


async def test_a_failed_post_that_never_landed_is_not_listed_and_its_number_is_reused(
    quick_client: BabyTrackerClient,
    fake_server: FakeSyncServer,  # noqa: F811
) -> None:
    seed(fake_server)
    engine = await synced_engine(quick_client)
    first = await add(engine, quick_client)
    fake_server.post_script = ["500"]
    with pytest.raises(BabyTrackerConnectionError):
        await add(engine, quick_client)
    assert engine.own_writes() == [(first.object_id, 1)]

    third = await add(engine, quick_client)

    assert engine.own_writes() == [(first.object_id, 1), (third.object_id, 2)]


# --- save and load ------------------------------------------------------------------------------


async def test_the_order_survives_save_and_load(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, ids = await three_adds(client, fake_server)
    edited_by_phone(fake_server, ids[2])
    await engine.sync(client)

    restored = SyncEngine.from_dict(json.loads(json.dumps(engine.to_dict())))

    assert restored.own_writes() == [(ids[0], 1), (ids[1], 2), (ids[2], 3)]
    assert restored.to_dict()["version"] == 4


async def test_state_saved_without_numbers_loads_and_recovers_what_it_can(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, ids = await three_adds(client, fake_server)
    edited_by_phone(fake_server, ids[1])  # the middle one now carries the phone's version
    await engine.sync(client)
    state = json.loads(json.dumps(engine.to_dict()))
    state.pop("created_numbers", None)  # as saved before this field existed
    assert state["version"] == 4

    restored = SyncEngine.from_dict(state)

    # Records still at this device's own version keep their numbers; the edited one can't be
    # recovered from state, so it sorts as the oldest (number 0).
    assert restored.own_writes() == [(ids[1], 0), (ids[0], 1), (ids[2], 3)]
    assert restored.created_ids == frozenset(ids)


async def test_unrecoverable_numbers_sort_by_object_id_and_are_replaced_by_a_resync(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, ids = await three_adds(client, fake_server)
    for object_id in ids:
        edited_by_phone(fake_server, object_id)
    await engine.sync(client)
    state = json.loads(json.dumps(engine.to_dict()))
    state.pop("created_numbers", None)

    restored = SyncEngine.from_dict(state)
    assert restored.own_writes() == [(i, 0) for i in sorted(ids)]

    # A fresh engine replaying the own log learns the real numbers.
    replayed = await synced_engine(client)
    assert replayed.own_writes() == [(ids[0], 1), (ids[1], 2), (ids[2], 3)]


async def test_old_state_still_deletes_its_own_records(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, ids = await three_adds(client, fake_server)
    state = json.loads(json.dumps(engine.to_dict()))
    state.pop("created_numbers", None)
    restored = SyncEngine.from_dict(state)

    await restored.delete_record(client, ids[2])

    assert restored.own_writes() == [(ids[0], 1), (ids[1], 2)]


# --- cost -----------------------------------------------------------------------------------------


async def test_the_accessor_does_not_re_encode_the_state(
    client: BabyTrackerClient, fake_server: FakeSyncServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, ids = await three_adds(client, fake_server)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("own_writes re-encoded state")

    monkeypatch.setattr(SyncEngine, "to_dict", boom)
    monkeypatch.setattr(Diaper, "to_payload", boom)

    assert [i for i, _ in engine.own_writes()] == ids
