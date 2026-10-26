"""The client and engine together, against the fake sync server."""

from __future__ import annotations

import json

import pytest
from factories import baby_payload, payload_for
from fake_server import FakeSyncServer

from pybabytracker import (
    OP_ADD,
    OP_DELETE,
    OP_UPDATE,
    BabyTrackerClient,
    BabyTrackerConnectionError,
    SyncEngine,
    UnknownRecord,
)

PHONE = "AAAA1111-0000-4000-8000-00000000000A"
TABLET = "BBBB2222-0000-4000-8000-00000000000B"
LAPTOP = "CCCC3333-0000-4000-8000-00000000000C"


def rid(n: int) -> str:
    return f"AAAAAAAA-0000-4000-8000-{n:012d}"


def seed_three_devices(server: FakeSyncServer) -> None:
    for uuid in (PHONE, TABLET, LAPTOP):
        server.add_device(uuid)
    server.add_transaction(PHONE, OP_ADD, payload_for("Diaper", rid(1)))
    server.add_transaction(PHONE, OP_ADD, payload_for("Sleep", rid(2)))
    server.add_transaction(TABLET, OP_ADD, payload_for("Formula", rid(3)))
    server.add_transaction(TABLET, OP_DELETE, payload_for("Diaper", rid(1)))
    server.add_transaction(LAPTOP, OP_ADD, payload_for("Nursing", rid(4)))
    server.add_transaction(LAPTOP, OP_UPDATE, payload_for("Nursing", rid(4), note="edited"))
    server.add_transaction(LAPTOP, OP_ADD, payload_for("Pump", rid(5)))
    server.add_transaction(LAPTOP, OP_DELETE, payload_for("Pump", rid(5)))


async def test_a_full_backfill_from_three_devices_leaves_exactly_the_survivors(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed_three_devices(fake_server)
    engine = SyncEngine()

    result = await engine.sync(client)

    assert set(engine.records) == {rid(2), rid(3), rid(4)}
    assert engine.records[rid(4)].note == "edited"
    assert dict(engine.last_seen) == {PHONE: 2, TABLET: 2, LAPTOP: 4}
    assert result.devices_fetched == 3
    # The probe device itself is in the list but has an empty log, so it is not fetched.
    assert not fake_server.calls("GET", "/account/transaction/99999999")


async def test_a_saved_state_then_a_sync_fetches_only_newer_transactions(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed_three_devices(fake_server)
    engine = SyncEngine()
    await engine.sync(client)
    saved = json.loads(json.dumps(engine.to_dict()))

    fake_server.add_transaction(TABLET, OP_ADD, payload_for("Diaper", rid(6)))
    fake_server.requests.clear()

    restored = SyncEngine.from_dict(saved)
    await restored.sync(client)

    fetches = fake_server.calls("GET", "/account/transaction/")
    assert [r.path for r in fetches] == [f"/account/transaction/{TABLET}/2"]
    assert set(restored.records) == {rid(2), rid(3), rid(4), rid(6)}


async def test_a_sync_with_nothing_new_fetches_no_logs(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed_three_devices(fake_server)
    engine = SyncEngine()
    await engine.sync(client)
    fake_server.requests.clear()

    result = await engine.sync(client)

    assert result.devices_fetched == 0
    assert not fake_server.calls("GET", "/account/transaction/")
    assert len(fake_server.calls("GET", "/account/device")) == 1


async def test_bad_and_unknown_transactions_do_not_stop_a_real_sync(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.add_device(PHONE)
    fake_server.add_transaction(
        PHONE, OP_ADD, payload_for("Diaper", rid(1), note="Légère fuite 💧")
    )
    fake_server.add_raw_transaction(PHONE, 2, OP_ADD, "%%% broken %%%")
    fake_server.add_transaction(
        PHONE, OP_ADD, {"BCObjectType": "Teleporter", "objectID": rid(3)}, sync_id=3
    )
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Sleep", rid(4)), sync_id=4)
    engine = SyncEngine()

    result = await engine.sync(client)

    assert set(engine.records) == {rid(1), rid(3), rid(4)}
    assert engine.records[rid(1)].note == "Légère fuite 💧"
    assert isinstance(engine.records[rid(3)], UnknownRecord)
    assert result.transactions_skipped == 1
    assert engine.last_seen[PHONE] == 4


async def test_a_server_error_mid_sync_keeps_earlier_progress_and_recovers(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed_three_devices(fake_server)
    fake_server.fail_paths[f"/{LAPTOP}/0"] = 500
    engine = SyncEngine()

    with pytest.raises(BabyTrackerConnectionError):
        await engine.sync(client)
    assert dict(engine.last_seen) == {PHONE: 2, TABLET: 2}

    fake_server.fail_paths.clear()
    await engine.sync(client)
    assert set(engine.records) == {rid(2), rid(3), rid(4)}


async def test_the_session_expiring_during_a_sync_is_survived(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed_three_devices(fake_server)
    engine = SyncEngine()
    await client.sign_in()
    fake_server.expire_sessions()

    await engine.sync(client)

    assert set(engine.records) == {rid(2), rid(3), rid(4)}


async def test_babies_are_discovered_from_the_synced_records(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.add_device(PHONE)
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Diaper", rid(1)))
    fake_server.add_transaction(
        PHONE, OP_ADD, payload_for("Diaper", rid(2), baby=baby_payload("Second Baby", "B2"))
    )
    engine = SyncEngine()
    await engine.sync(client)
    assert {b.name for b in engine.babies().values()} == {"Test Baby", "Second Baby"}
