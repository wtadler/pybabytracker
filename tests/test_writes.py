"""Writes: adds and deletes posted as this device, against the fake sync server."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from conftest import PROBE_UUID
from factories import BABY_ID, BABY_TWO_ID, baby_payload, payload_for
from fake_server import FakeSyncServer

from pybabytracker import (
    DIAPER_DIRTY,
    DIAPER_MIXED,
    DIAPER_WET,
    OP_ADD,
    OP_DELETE,
    AuthenticationError,
    BabyTrackerClient,
    BabyTrackerConnectionError,
    Diaper,
    NoResponseError,
    NotOwnRecordError,
    NotSyncedError,
    ProtocolError,
    RecordNotFoundError,
    SyncEngine,
    UnknownBabyError,
    WriteOutcomeUnknownError,
)

PHONE = "AAAA1111-0000-4000-8000-00000000000A"
EVENT_TIME = datetime(2026, 10, 25, 15, 40, 49, tzinfo=UTC)


def rid(n: int) -> str:
    return f"AAAAAAAA-0000-4000-8000-{n:012d}"


def decode(entry: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(base64.b64decode(entry["Transaction"]))
    return payload


def seed(server: FakeSyncServer) -> None:
    """Another phone has logged one diaper, so the engine learns the baby from sync."""
    server.register_devices = False  # like the real service: listed only after a first post
    server.add_device(PHONE)
    server.add_transaction(PHONE, OP_ADD, payload_for("Diaper", rid(1)))


def own_posts(server: FakeSyncServer) -> list[dict[str, Any]]:
    return server.posted(PROBE_UUID)


def nth_post(server: FakeSyncServer, index: int) -> dict[str, Any]:
    posts = own_posts(server)
    assert len(posts) > index, f"expected at least {index + 1} posts, got {len(posts)}"
    return posts[index]


def post_numbers(server: FakeSyncServer) -> list[int]:
    return [r.body["SyncID"] for r in server.calls("POST", "/account/transaction")]


async def synced_engine(client: BabyTrackerClient, grace: float = 90.0) -> SyncEngine:
    engine = SyncEngine(device_uuid=PROBE_UUID, unknown_outcome_grace=grace)
    await engine.sync(client)
    return engine


async def add(engine: SyncEngine, client: BabyTrackerClient, status: int = DIAPER_WET) -> Diaper:
    diaper = await engine.add_diaper(
        client, BABY_ID, status, time=EVENT_TIME, timezone_offset=-18000
    )
    assert isinstance(diaper, Diaper), f"add_diaper returned {diaper!r}"
    return diaper


@pytest_asyncio.fixture
async def quick_client(
    fake_server: FakeSyncServer, session: aiohttp.ClientSession
) -> AsyncIterator[BabyTrackerClient]:
    """A client whose requests time out fast, for the stalled-POST tests."""
    fake_server.post_stall = 0.6
    yield BabyTrackerClient(
        fake_server.email,
        fake_server.password,
        PROBE_UUID,
        "Test Device",
        session=session,
        base_url=fake_server.base_url,
        request_timeout=0.2,
    )


# --- the client's post ------------------------------------------------------------------


async def test_the_client_posts_the_documented_body_and_accepts_an_empty_201(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    await client.post_transaction(OP_ADD, 1, "e30=")

    assert own_posts(fake_server) == [{"SyncID": 1, "OPCode": 0, "Transaction": "e30="}]
    [request] = fake_server.calls("POST", "/account/transaction")
    assert request.body == {"OPCode": 0, "SyncID": 1, "Transaction": "e30="}


async def test_the_client_reports_its_device_id(client: BabyTrackerClient) -> None:
    assert client.device_uuid == PROBE_UUID


# --- AC1: an add matches the app's format field for field --------------------------------


async def test_a_new_diaper_posts_an_add_whose_payload_matches_the_app_field_for_field(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    before = datetime.now(UTC).replace(microsecond=0)

    diaper = await engine.add_diaper(
        client, BABY_ID, DIAPER_MIXED, time=EVENT_TIME, timezone_offset=-18000
    )

    after = datetime.now(UTC)
    assert len(own_posts(fake_server)) == 1
    entry = nth_post(fake_server, 0)
    assert entry["OPCode"] == OP_ADD
    assert isinstance(diaper, Diaper)
    payload = decode(entry)
    captured = payload_for("Diaper")  # the shape of the app's captured diaper add
    assert set(payload) == set(captured)
    stamp = datetime.strptime(payload.pop("timestamp"), "%Y-%m-%d %H:%M:%S %z")
    assert before <= stamp <= after
    assert payload == {
        "BCObjectType": "Diaper",
        "objectID": diaper.object_id,
        "time": "2026-10-25 15:40:49 +0000",
        "timezone": -18000,
        "status": 2,
        "amount": 2,
        "peeColor": 5,
        "pooColor": 5,
        "texture": 5,
        "flag": 0,
        "note": "",
        "pictureNote": [],
        "newFlage": "true",
        "pictureLoaded": "true",
        "baby": baby_payload(),
    }


async def test_a_new_diapers_object_id_is_an_uppercase_uuid4(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)

    diaper = await add(engine, client)

    object_id = decode(nth_post(fake_server, 0))["objectID"]
    assert object_id == diaper.object_id
    assert object_id == object_id.upper()
    assert uuid.UUID(object_id).version == 4
    assert (await add(engine, client)).object_id != object_id


async def test_each_diaper_type_is_posted_with_its_status(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    for status in (DIAPER_WET, DIAPER_DIRTY, DIAPER_MIXED):
        await add(engine, client, status)
    assert [decode(e)["status"] for e in own_posts(fake_server)] == [0, 1, 2]


async def test_a_local_event_time_is_written_in_utc_with_its_offset(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    local = datetime(2026, 10, 25, 10, 40, 49, tzinfo=timezone(timedelta(hours=-5)))

    await engine.add_diaper(client, BABY_ID, DIAPER_WET, time=local)

    payload = decode(nth_post(fake_server, 0))
    assert payload["time"] == "2026-10-25 15:40:49 +0000"
    assert payload["timezone"] == -18000


async def test_the_embedded_baby_is_the_full_baby_record_known_from_sync(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    renamed = baby_payload("Renamed Baby", extraField="kept", timestamp="2026-10-20 08:00:00 +0000")
    fake_server.add_transaction(PHONE, OP_ADD, renamed)
    engine = await synced_engine(client)

    await add(engine, client)

    assert decode(nth_post(fake_server, 0))["baby"] == renamed


async def test_a_new_diaper_appears_in_local_state_at_once(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)

    diaper = await add(engine, client)

    assert engine.records[diaper.object_id].to_payload() == decode(nth_post(fake_server, 0))
    assert diaper.object_id in engine.created_ids
    assert engine.latest_records(BABY_ID)["Diaper"].object_id == diaper.object_id


# --- AC2: numbering ---------------------------------------------------------------------


async def test_a_first_write_on_a_new_device_is_number_1_and_joins_the_device_list(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    assert PROBE_UUID not in {d.device_uuid for d in await client.get_devices()}

    await add(engine, client)

    devices = {d.device_uuid: d.last_sync_id for d in await client.get_devices()}
    assert devices.get(PROBE_UUID) == 1
    assert post_numbers(fake_server) == [1]


async def test_two_writes_in_a_row_continue_from_the_devices_last_number_on_the_server(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    fake_server.add_device(PROBE_UUID, "Test Device")
    for n in range(1, 8):
        fake_server.add_transaction(PROBE_UUID, OP_ADD, payload_for("Diaper", rid(100 + n)))
    engine = await synced_engine(client)

    await add(engine, client)
    await add(engine, client)

    assert post_numbers(fake_server) == [8, 9]


async def test_numbering_continues_after_a_restart_from_saved_state(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    await add(engine, client)
    await add(engine, client)

    restored = SyncEngine.from_dict(json.loads(json.dumps(engine.to_dict())))
    await add(restored, client)

    assert post_numbers(fake_server) == [1, 2, 3]


async def test_numbering_uses_the_local_number_when_the_server_lags_behind(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    await add(engine, client)
    await add(engine, client)
    assert PROBE_UUID in fake_server.devices
    # The device list hasn't caught up with the last post yet.
    fake_server.devices[PROBE_UUID].last_sync_override = 1

    await add(engine, client)

    assert post_numbers(fake_server) == [1, 2, 3]


async def test_numbering_follows_the_server_when_another_process_wrote_as_this_device(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    await add(engine, client)
    assert PROBE_UUID in fake_server.devices
    # Another process with the same device ID posted numbers 2 and 3.
    fake_server.add_transaction(PROBE_UUID, OP_ADD, payload_for("Diaper", rid(50)))
    fake_server.add_transaction(PROBE_UUID, OP_ADD, payload_for("Diaper", rid(51)))

    await add(engine, client)

    assert post_numbers(fake_server) == [1, 4]
    # Its records are applied, not skipped over, and count as this device's own.
    assert {rid(50), rid(51)} <= set(engine.records)
    assert {rid(50), rid(51)} <= engine.created_ids
    assert engine.last_seen[PROBE_UUID] == 4


async def test_concurrent_writes_get_consecutive_distinct_numbers(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    fake_server.delay = 0.02  # let the writes overlap

    diapers = await asyncio.gather(*(add(engine, client) for _ in range(4)))

    assert sorted(e["SyncID"] for e in own_posts(fake_server)) == [1, 2, 3, 4]
    assert len({d.object_id for d in diapers}) == 4
    assert all(d.object_id in engine.records for d in diapers)


# --- AC3: a failed write doesn't use up its number ---------------------------------------


async def test_a_server_error_on_post_raises_and_the_retry_uses_the_same_number(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    fake_server.post_script = ["500"]

    with pytest.raises(BabyTrackerConnectionError):
        await add(engine, client)
    assert own_posts(fake_server) == []
    diaper = await add(engine, client)

    assert post_numbers(fake_server) == [1, 1]
    assert [e["SyncID"] for e in own_posts(fake_server)] == [1]
    assert diaper.object_id in engine.records


async def test_a_failed_number_is_reused_by_the_next_write_even_for_another_record(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    await add(engine, client)
    fake_server.post_script = ["500"]
    with pytest.raises(BabyTrackerConnectionError):
        await add(engine, client, DIAPER_WET)

    await add(engine, client, DIAPER_DIRTY)

    assert [(e["SyncID"], decode(e)["status"]) for e in own_posts(fake_server)] == [
        (1, DIAPER_WET),
        (2, DIAPER_DIRTY),
    ]


async def test_a_failed_write_is_not_in_local_state(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    fake_server.post_script = ["500"]
    with pytest.raises(BabyTrackerConnectionError):
        await add(engine, client)
    assert set(engine.records) == {rid(1)}
    assert engine.created_ids == frozenset()


async def test_a_failed_number_is_still_reused_after_a_restart(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    await add(engine, client)
    fake_server.post_script = ["500"]
    with pytest.raises(BabyTrackerConnectionError):
        await add(engine, client)

    restored = SyncEngine.from_dict(json.loads(json.dumps(engine.to_dict())))
    await add(restored, client)

    assert [e["SyncID"] for e in own_posts(fake_server)] == [1, 2]


async def test_a_401_on_post_signs_in_again_and_retries_with_the_same_number(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    fake_server.post_script = ["401"]

    diaper = await add(engine, client)

    assert post_numbers(fake_server) == [1, 1]
    assert [e["SyncID"] for e in own_posts(fake_server)] == [1]
    assert len(fake_server.calls("POST", "/session")) == 2
    assert diaper.object_id in engine.records


async def test_a_post_rejected_twice_with_401_raises_and_frees_the_number(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    fake_server.post_script = ["401", "401"]

    with pytest.raises(AuthenticationError):
        await add(engine, client)
    await add(engine, client)

    assert [e["SyncID"] for e in own_posts(fake_server)] == [1]


async def test_a_post_that_timed_out_but_landed_is_confirmed_not_posted_again(
    quick_client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(quick_client)
    fake_server.post_script = ["timeout-landed"]

    diaper = await add(engine, quick_client)

    assert post_numbers(fake_server) == [1]
    assert diaper.object_id in engine.records
    assert diaper.object_id in engine.created_ids
    await add(engine, quick_client)
    assert [e["SyncID"] for e in own_posts(fake_server)] == [1, 2]


async def test_a_post_that_timed_out_and_did_not_land_raises_and_the_retry_reuses_it(
    quick_client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(quick_client, grace=0.3)
    fake_server.post_script = ["timeout-lost"]

    with pytest.raises(BabyTrackerConnectionError):
        await add(engine, quick_client)
    assert own_posts(fake_server) == []
    with pytest.raises(WriteOutcomeUnknownError):
        await add(engine, quick_client)  # too soon: the stalled post could still land
    await asyncio.sleep(0.35)
    await add(engine, quick_client)

    assert post_numbers(fake_server) == [1, 1]
    assert [e["SyncID"] for e in own_posts(fake_server)] == [1]


async def test_a_stalled_post_landing_after_the_retrys_check_never_shares_its_number(
    quick_client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(quick_client, grace=5.0)
    fake_server.post_script = ["timeout-late"]
    with pytest.raises(BabyTrackerConnectionError):
        await add(engine, quick_client, DIAPER_WET)

    # The caller retries at once, before the stalled post lands: refused, nothing posted.
    with pytest.raises(WriteOutcomeUnknownError):
        await add(engine, quick_client, DIAPER_DIRTY)
    assert post_numbers(fake_server) == [1]

    await asyncio.sleep(fake_server.post_stall)  # now it lands, as number 1
    await add(engine, quick_client, DIAPER_DIRTY)

    assert [(e["SyncID"], decode(e)["status"]) for e in own_posts(fake_server)] == [
        (1, DIAPER_WET),
        (2, DIAPER_DIRTY),
    ]
    assert post_numbers(fake_server) == [1, 2]


async def test_the_unknown_outcome_window_survives_a_restart(
    quick_client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(quick_client, grace=5.0)
    fake_server.post_script = ["timeout-lost"]
    with pytest.raises(BabyTrackerConnectionError):
        await add(engine, quick_client)

    restored = SyncEngine.from_dict(
        json.loads(json.dumps(engine.to_dict())), unknown_outcome_grace=5.0
    )
    with pytest.raises(WriteOutcomeUnknownError):
        await add(restored, quick_client)
    assert post_numbers(fake_server) == [1]


async def test_a_definite_server_error_is_not_an_unknown_outcome(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client, grace=600.0)
    fake_server.post_script = ["500"]
    with pytest.raises(BabyTrackerConnectionError) as excinfo:
        await add(engine, client)
    assert not isinstance(excinfo.value, NoResponseError)

    await add(engine, client)  # no waiting: a 500 answer means it didn't land

    assert post_numbers(fake_server) == [1, 1]


async def test_the_client_reports_no_answer_apart_from_an_http_error(
    quick_client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.post_script = ["timeout-lost", "500"]
    with pytest.raises(NoResponseError):
        await quick_client.post_transaction(OP_ADD, 1, "e30=")
    with pytest.raises(BabyTrackerConnectionError) as excinfo:
        await quick_client.post_transaction(OP_ADD, 1, "e30=")
    assert not isinstance(excinfo.value, NoResponseError)


async def test_a_retry_with_the_same_object_id_returns_the_late_landing_instead_of_posting(
    quick_client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(quick_client, grace=5.0)
    object_id = "EEEEEEEE-0000-4000-8000-000000000001"
    fake_server.post_script = ["timeout-late"]
    with pytest.raises(BabyTrackerConnectionError):
        await engine.add_diaper(quick_client, BABY_ID, DIAPER_WET, object_id=object_id)
    await asyncio.sleep(fake_server.post_stall)  # it lands

    diaper = await engine.add_diaper(quick_client, BABY_ID, DIAPER_WET, object_id=object_id)

    assert diaper.object_id == object_id
    assert post_numbers(fake_server) == [1]
    assert [decode(e)["objectID"] for e in own_posts(fake_server)] == [object_id]
    assert engine.records[object_id].to_payload() == decode(nth_post(fake_server, 0))


async def test_a_retry_with_the_same_object_id_posts_it_when_nothing_landed(
    quick_client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(quick_client, grace=0.3)
    object_id = "EEEEEEEE-0000-4000-8000-000000000002"
    fake_server.post_script = ["timeout-lost"]
    with pytest.raises(BabyTrackerConnectionError):
        await engine.add_diaper(quick_client, BABY_ID, DIAPER_WET, object_id=object_id)
    await asyncio.sleep(0.35)

    diaper = await engine.add_diaper(quick_client, BABY_ID, DIAPER_WET, object_id=object_id)

    assert diaper.object_id == object_id
    assert [(e["SyncID"], decode(e)["objectID"]) for e in own_posts(fake_server)] == [
        (1, object_id)
    ]


async def test_a_write_is_refused_when_the_own_log_is_shorter_than_last_sync_id(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    await add(engine, client)
    fake_server.devices[PROBE_UUID].last_sync_override = 5  # but the log holds only 1

    with pytest.raises(ProtocolError):
        await add(engine, client)

    assert post_numbers(fake_server) == [1]
    assert engine.last_seen[PROBE_UUID] == 1


async def test_a_post_that_landed_after_the_check_is_found_before_the_next_write(
    quick_client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(quick_client)
    fake_server.post_script = ["timeout-late"]
    with pytest.raises(BabyTrackerConnectionError):
        await add(engine, quick_client, DIAPER_WET)
    await asyncio.sleep(fake_server.post_stall)  # the stalled post lands now
    [late] = own_posts(fake_server)

    await add(engine, quick_client, DIAPER_DIRTY)

    # The late one keeps number 1; the next write takes 2 instead of reusing it.
    assert [(e["SyncID"], decode(e)["status"]) for e in own_posts(fake_server)] == [
        (1, DIAPER_WET),
        (2, DIAPER_DIRTY),
    ]
    late_id = decode(late)["objectID"]
    assert late_id in engine.records
    assert late_id in engine.created_ids


# --- AC4 and AC5: deletes ---------------------------------------------------------------


async def test_deleting_a_record_another_device_created_is_refused_without_a_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    fake_server.requests.clear()

    with pytest.raises(NotOwnRecordError):
        await engine.delete_record(client, rid(1))

    assert fake_server.requests == []
    assert rid(1) in engine.records


async def test_deleting_an_unknown_record_is_refused(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    with pytest.raises(NotOwnRecordError):
        await engine.delete_record(client, rid(999))
    assert not fake_server.calls("POST", "/account/transaction")


async def test_deleting_an_own_record_posts_a_delete_with_the_full_record(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    diaper = await add(engine, client)
    added = decode(nth_post(fake_server, 0))

    await engine.delete_record(client, diaper.object_id)

    entry = nth_post(fake_server, 1)
    assert (entry["SyncID"], entry["OPCode"]) == (2, OP_DELETE)
    # Like the app: the record as last saved, timestamp not bumped, only newFlage changed.
    assert decode(entry) == {**added, "newFlage": "false"}
    assert diaper.object_id not in engine.records


async def test_deleting_an_already_deleted_record_is_refused_without_a_post(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    diaper = await add(engine, client)
    await engine.delete_record(client, diaper.object_id)

    with pytest.raises(RecordNotFoundError):
        await engine.delete_record(client, diaper.object_id)

    assert post_numbers(fake_server) == [1, 2]


async def test_deleting_an_own_record_another_device_already_deleted_is_refused(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    diaper = await add(engine, client)
    gone = {**decode(nth_post(fake_server, 0)), "timestamp": "2030-01-01 00:00:00 +0000"}
    fake_server.add_transaction(PHONE, OP_DELETE, gone)
    await engine.sync(client)

    with pytest.raises(RecordNotFoundError):
        await engine.delete_record(client, diaper.object_id)
    assert post_numbers(fake_server) == [1]


async def test_created_ids_survive_a_restart_so_undo_still_works(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    diaper = await add(engine, client)

    restored = SyncEngine.from_dict(json.loads(json.dumps(engine.to_dict())))
    await restored.delete_record(client, diaper.object_id)

    assert [e["OPCode"] for e in own_posts(fake_server)] == [OP_ADD, OP_DELETE]
    assert diaper.object_id not in restored.records


# --- refusals before any request ----------------------------------------------------------


async def test_a_write_before_any_sync_is_refused_without_a_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = SyncEngine(device_uuid=PROBE_UUID)

    with pytest.raises(NotSyncedError):
        await add(engine, client)

    assert fake_server.requests == []


async def test_a_write_for_a_baby_not_known_from_sync_is_refused(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    fake_server.requests.clear()

    with pytest.raises(UnknownBabyError):
        await engine.add_diaper(client, BABY_TWO_ID, DIAPER_WET)

    assert fake_server.requests == []


async def test_an_engine_without_a_device_id_cannot_write(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = SyncEngine()
    await engine.sync(client)
    with pytest.raises(ValueError):
        await add(engine, client)
    assert not fake_server.calls("POST", "/account/transaction")


async def test_an_engine_refuses_a_client_with_a_different_device_id(
    fake_server: FakeSyncServer, session: aiohttp.ClientSession
) -> None:
    seed(fake_server)
    other = BabyTrackerClient(
        fake_server.email,
        fake_server.password,
        "DDDD4444-0000-4000-8000-00000000000D",
        "Other",
        session=session,
        base_url=fake_server.base_url,
    )
    engine = SyncEngine(device_uuid=PROBE_UUID)
    await engine.sync(other)
    with pytest.raises(ValueError):
        await add(engine, other)
    assert not fake_server.calls("POST", "/account/transaction")


# --- the device's own log coming back ----------------------------------------------------


async def test_the_next_sync_does_not_fetch_or_reapply_this_devices_own_writes(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    diaper = await add(engine, client)
    other = await add(engine, client)
    await engine.delete_record(client, other.object_id)
    before = engine.to_dict()
    fake_server.requests.clear()

    result = await engine.sync(client)

    assert not fake_server.calls("GET", f"/account/transaction/{PROBE_UUID}")
    assert result.transactions_applied == 0
    assert engine.to_dict() == before
    assert diaper.object_id in engine.records


async def test_own_writes_missing_from_stale_state_come_back_once_through_the_log(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    stale = json.loads(json.dumps(engine.to_dict()))
    diaper = await add(engine, client)

    restored = SyncEngine.from_dict(stale)
    first = await restored.sync(client)
    second = await restored.sync(client)

    assert first.transactions_applied == 1
    assert second.transactions_applied == 0
    assert restored.records[diaper.object_id] == engine.records[diaper.object_id]
    assert diaper.object_id in restored.created_ids
    # And the next number still follows the server.
    await add(restored, client)
    assert [e["SyncID"] for e in own_posts(fake_server)] == [1, 2]


async def test_own_log_entries_applied_by_sync_count_as_created(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    fake_server.add_device(PROBE_UUID, "Test Device")
    fake_server.add_transaction(PROBE_UUID, OP_ADD, payload_for("Diaper", rid(70)))
    engine = await synced_engine(client)

    await engine.delete_record(client, rid(70))

    assert [(e["SyncID"], e["OPCode"]) for e in own_posts(fake_server)] == [
        (1, OP_ADD),
        (2, OP_DELETE),
    ]


# --- state from before writes, and bad input ----------------------------------------------


async def test_version_2_state_loads_with_a_device_id_and_can_write(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    old = SyncEngine()
    await old.sync(client)
    state = old.to_dict()
    for key in ("device", "synced", "last_posted", "pending", "created"):
        state.pop(key)
    state["version"] = 2

    engine = SyncEngine.from_dict(state, device_uuid=PROBE_UUID)
    await add(engine, client)

    assert [e["SyncID"] for e in own_posts(fake_server)] == [1]


async def test_loading_state_saved_by_another_device_is_refused(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    with pytest.raises(ValueError):
        SyncEngine.from_dict(engine.to_dict(), device_uuid="DDDD4444-0000-4000-8000-00000000000D")


async def test_an_unknown_diaper_status_or_a_naive_time_is_refused_without_a_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    fake_server.requests.clear()
    with pytest.raises(ValueError):
        await engine.add_diaper(client, BABY_ID, 7)
    with pytest.raises(ValueError):
        await engine.add_diaper(client, BABY_ID, DIAPER_WET, time=datetime(2026, 10, 25, 12))
    assert fake_server.requests == []


async def test_when_the_landing_check_also_fails_the_number_stays_pending(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    fake_server.post_script = ["500"]
    fake_server.fail_paths = {f"/account/transaction/{PROBE_UUID}/0": 500}
    with pytest.raises(BabyTrackerConnectionError):
        await add(engine, client)
    assert engine.to_dict()["pending"] is not None
    fake_server.fail_paths = {}

    await add(engine, client)

    assert post_numbers(fake_server) == [1, 1]
    assert [e["SyncID"] for e in own_posts(fake_server)] == [1]


async def test_a_late_landing_is_found_even_when_the_device_list_lags_behind_it(
    quick_client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(quick_client)
    await add(engine, quick_client, DIAPER_WET)
    fake_server.post_script = ["timeout-late"]
    with pytest.raises(BabyTrackerConnectionError):
        await add(engine, quick_client, DIAPER_MIXED)
    await asyncio.sleep(fake_server.post_stall)  # number 2 lands now
    fake_server.devices[PROBE_UUID].last_sync_override = 1  # but LastSyncID still says 1

    await add(engine, quick_client, DIAPER_DIRTY)

    assert [(e["SyncID"], decode(e)["status"]) for e in own_posts(fake_server)] == [
        (1, DIAPER_WET),
        (2, DIAPER_MIXED),
        (3, DIAPER_DIRTY),
    ]
    assert engine.last_seen[PROBE_UUID] == 3
    assert len(engine.created_ids) == 3


async def test_a_number_taken_by_another_process_is_not_reused_when_the_list_lags(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    await add(engine, client)
    fake_server.post_script = ["500"]
    with pytest.raises(BabyTrackerConnectionError):
        await add(engine, client)
    # Meanwhile another process with this device ID took number 2; the list still says 1.
    fake_server.add_transaction(PROBE_UUID, OP_ADD, payload_for("Diaper", rid(60)))
    fake_server.devices[PROBE_UUID].last_sync_override = 1

    await add(engine, client)

    assert [e["SyncID"] for e in own_posts(fake_server)] == [1, 2, 3]
    assert rid(60) in engine.records


async def test_a_delete_keeps_the_records_own_timestamp_like_the_app(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    fake_server.add_device(PROBE_UUID, "Test Device")
    saved = payload_for("Diaper", rid(80), timestamp="2026-10-01 08:00:00 +0000")
    fake_server.add_transaction(PROBE_UUID, OP_ADD, saved)
    engine = await synced_engine(client)

    await engine.delete_record(client, rid(80))

    entry = nth_post(fake_server, 1)
    assert entry["OPCode"] == OP_DELETE
    assert decode(entry) == {**saved, "newFlage": "false"}
    assert rid(80) not in engine.records


# --- state version 4: both version 3 shapes load -----------------------------------------


async def test_a_version_3_state_with_op_ranks_and_no_write_state_loads_and_can_write(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Diaper", rid(2)))
    fake_server.add_transaction(PHONE, OP_DELETE, payload_for("Diaper", rid(1)))
    old = SyncEngine()
    await old.sync(client)
    state = old.to_dict()
    for key in ("device", "synced", "last_posted", "pending", "created"):
        state.pop(key)
    state["version"] = 3
    assert state["tombstones"][0]["version"]["rank"] == 2

    engine = SyncEngine.from_dict(state, device_uuid=PROBE_UUID)
    assert engine.to_dict()["tombstones"] == old.to_dict()["tombstones"]
    await add(engine, client)

    assert engine.to_dict()["version"] == 4
    assert [e["SyncID"] for e in own_posts(fake_server)] == [1]


async def test_a_version_3_state_with_write_state_and_no_op_ranks_loads_and_keeps_it(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    diaper = await add(engine, client)
    fake_server.post_script = ["500"]
    with pytest.raises(BabyTrackerConnectionError):
        await add(engine, client)
    state = json.loads(json.dumps(engine.to_dict()))
    state["version"] = 3
    for entry in state["records"]:
        entry["version"].pop("rank")

    restored = SyncEngine.from_dict(state)

    assert restored.device_uuid == PROBE_UUID
    assert restored.created_ids == {diaper.object_id}
    assert restored.to_dict()["pending"] == engine.to_dict()["pending"]
    assert all(e["version"]["rank"] == 0 for e in restored.to_dict()["records"])
    await add(restored, client)  # the failed number 2 is reused
    await restored.delete_record(client, diaper.object_id)
    assert [(e["SyncID"], e["OPCode"]) for e in own_posts(fake_server)] == [
        (1, OP_ADD),
        (2, OP_ADD),
        (3, OP_DELETE),
    ]


async def test_local_apply_of_own_writes_matches_a_replay_of_the_log(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    kept = await add(engine, client)
    gone = await add(engine, client)
    await engine.delete_record(client, gone.object_id)

    replayed = await synced_engine(client)

    local, replay = engine.to_dict(), replayed.to_dict()
    assert local["records"] == replay["records"]
    assert local["tombstones"] == replay["tombstones"]
    assert local["last_seen"] == replay["last_seen"]
    assert replayed.created_ids == engine.created_ids == {kept.object_id, gone.object_id}
    [tombstone] = local["tombstones"]
    assert tombstone["version"]["rank"] == 2


async def test_catching_up_this_devices_own_log_pages_through_it(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    await add(engine, client)
    for n in range(5):  # another process with this device ID wrote 2 through 6
        fake_server.add_transaction(PROBE_UUID, OP_ADD, payload_for("Diaper", rid(200 + n)))
    fake_server.page_size = 2

    await add(engine, client)

    assert post_numbers(fake_server)[-1] == 7
    assert engine.last_seen[PROBE_UUID] == 7
    assert {rid(200 + n) for n in range(5)} <= set(engine.records)
