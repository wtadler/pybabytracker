"""Deleting a baby that has no records (invented data only)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from conftest import PROBE_UUID
from factories import BABY_ID, baby_payload, payload_for
from fake_server import FakeSyncServer
from selection_helpers import PHONE, posted, rid, seed, synced

from pybabytracker import (
    DIAPER_WET,
    OP_DELETE,
    BabyTrackerClient,
    EntryInUseError,
    NotOwnRecordError,
    RecordNotFoundError,
)

BIRTH = datetime(2026, 10, 27, 16, 17, 31, tzinfo=UTC)
EMPTY_BABY = "BA000000-0000-4000-8000-0000000000E1"


def opcodes(server: FakeSyncServer) -> list[int]:
    return [int(e["OPCode"]) for e in server.posted(PROBE_UUID)]


async def test_a_baby_this_device_added_and_never_used_is_deleted(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    baby = await engine.add_baby(client, "Second Baby", birth=BIRTH, boy=True)
    assert engine.baby_users(baby.object_id) == 0

    await engine.delete_record(client, baby.object_id)

    assert opcodes(fake_server) == [0, OP_DELETE]
    added, deleted = posted(fake_server)
    assert deleted == {**added, "newFlage": "false"}
    assert baby.object_id not in engine.babies()
    assert engine.is_deleted(baby.object_id)


async def test_a_baby_this_device_added_is_refused_once_it_has_records(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    baby = await engine.add_baby(client, "Second Baby", birth=BIRTH, boy=True)
    diaper = await engine.add_diaper(client, baby.object_id, DIAPER_WET)
    before = len(fake_server.requests)

    with pytest.raises(EntryInUseError) as raised:
        await engine.delete_record(client, baby.object_id)
    assert (raised.value.what, raised.value.object_id, raised.value.users) == (
        "baby",
        baby.object_id,
        1,
    )
    assert len(fake_server.requests) == before

    await engine.delete_record(client, diaper.object_id)
    await engine.delete_record(client, baby.object_id)  # its records are gone now
    assert opcodes(fake_server) == [0, 0, OP_DELETE, OP_DELETE]


async def test_another_devices_empty_baby_is_deleted_only_with_any_device(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    empty = baby_payload("Empty Baby", EMPTY_BABY)
    seed(fake_server, empty)
    engine = await synced(client)

    with pytest.raises(NotOwnRecordError):
        await engine.delete_record(client, EMPTY_BABY)
    await engine.delete_record(client, EMPTY_BABY.lower(), any_device=True)

    [payload] = posted(fake_server)
    assert payload == {**empty, "newFlage": "false"}
    assert EMPTY_BABY not in engine.babies()
    with pytest.raises(RecordNotFoundError):
        await engine.delete_record(client, EMPTY_BABY, any_device=True)


async def test_a_baby_with_records_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, baby_payload(), payload_for("Diaper", rid(3)))  # two diapers in all
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(EntryInUseError) as raised:
        await engine.delete_record(client, BABY_ID, any_device=True)
    assert (raised.value.what, raised.value.users) == ("baby", 2)
    assert len(fake_server.requests) == before
    assert engine.baby_users(BABY_ID.lower()) == 2


async def test_a_baby_known_only_from_copies_in_its_records_is_refused_as_in_use(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)  # a diaper that embeds the baby, and no Baby record of its own
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(EntryInUseError):
        await engine.delete_record(client, BABY_ID, any_device=True)
    assert len(fake_server.requests) == before


async def test_a_baby_another_device_used_since_the_last_sync_is_not_deleted(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    empty = baby_payload("Empty Baby", EMPTY_BABY)
    seed(fake_server, empty)
    engine = await synced(client)
    fake_server.add_transaction(PHONE, 0, payload_for("Diaper", rid(9), baby=empty))  # unsynced

    with pytest.raises(EntryInUseError):
        await engine.delete_record(client, EMPTY_BABY, any_device=True)
    assert posted(fake_server) == []
