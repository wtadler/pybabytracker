"""Object ids are matched whatever their case.

Android writes lowercase UUIDs and iOS uppercase ones (PROTOCOL.md, "Android and iOS
differences"), and the engine keeps every record under the id exactly as received. An id a caller
passes can therefore differ from the stored one only in case: ``end_sleep`` for a sleep started
on an Android phone, a retry of an add, and a delete all have to find the record anyway, and the
engine posts under the id the record already has.
"""

from __future__ import annotations

import pytest
from factories import BABY_ID, payload_for
from fake_server import FakeSyncServer
from test_timers import engine_with_phone_sleep, phone_sleep
from test_writes import (
    PHONE,
    add,
    decode,
    nth_post,
    post_numbers,
    rid,
    seed,
    synced_engine,
)

from pybabytracker import (
    DIAPER_WET,
    OP_ADD,
    OP_DELETE,
    OP_UPDATE,
    BabyTrackerClient,
    NotOwnRecordError,
    ObjectIdInUseError,
    RecordNotFoundError,
)

ANDROID_SLEEP_ID = "6b4baf2e-f1b2-43b3-8d15-29a7decb9db6"
IOS_SLEEP_ID = ANDROID_SLEEP_ID.upper()


# --- end_sleep ---------------------------------------------------------------------------------


async def test_end_sleep_finds_a_sleep_a_phone_started_with_a_lowercase_id(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, _ = await engine_with_phone_sleep(client, fake_server, object_id=ANDROID_SLEEP_ID)

    ended = await engine.end_sleep(client, ANDROID_SLEEP_ID)

    assert not ended.is_running
    entry = nth_post(fake_server, 0)
    assert (entry["SyncID"], entry["OPCode"]) == (1, OP_UPDATE)
    assert decode(entry)["objectID"] == ANDROID_SLEEP_ID
    assert ended.object_id == ANDROID_SLEEP_ID
    assert ANDROID_SLEEP_ID in engine.records
    assert IOS_SLEEP_ID not in engine.records  # no second copy under another case


async def test_end_sleep_takes_the_id_in_either_case_and_posts_the_stored_one(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, _ = await engine_with_phone_sleep(client, fake_server, object_id=ANDROID_SLEEP_ID)

    await engine.end_sleep(client, IOS_SLEEP_ID)  # the caller's case differs from the stored one

    assert decode(nth_post(fake_server, 0))["objectID"] == ANDROID_SLEEP_ID


async def test_end_sleep_takes_a_lowercase_id_for_an_uppercase_sleep(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, _ = await engine_with_phone_sleep(client, fake_server, object_id=IOS_SLEEP_ID)

    await engine.end_sleep(client, ANDROID_SLEEP_ID)

    assert decode(nth_post(fake_server, 0))["objectID"] == IOS_SLEEP_ID


async def test_end_sleep_for_an_unknown_id_is_still_refused_without_a_post(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, _ = await engine_with_phone_sleep(client, fake_server, object_id=ANDROID_SLEEP_ID)

    with pytest.raises(RecordNotFoundError):
        await engine.end_sleep(client, "00000000-0000-4000-8000-000000000000")

    assert post_numbers(fake_server) == []


async def test_end_sleep_in_the_other_case_of_a_deleted_sleep_says_it_was_deleted(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, _ = await engine_with_phone_sleep(client, fake_server, object_id=ANDROID_SLEEP_ID)
    gone = {**payload_for("Sleep", ANDROID_SLEEP_ID), "timestamp": "2030-01-01 00:00:00 +0000"}
    fake_server.add_transaction(PHONE, OP_DELETE, gone)
    await engine.sync(client)

    with pytest.raises(RecordNotFoundError, match="was deleted"):
        await engine.end_sleep(client, IOS_SLEEP_ID)

    assert post_numbers(fake_server) == []


async def test_end_sleep_retried_in_the_other_case_returns_the_ended_sleep_without_a_post(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, _ = await engine_with_phone_sleep(client, fake_server, object_id=ANDROID_SLEEP_ID)
    first = await engine.end_sleep(client, ANDROID_SLEEP_ID)

    again = await engine.end_sleep(client, IOS_SLEEP_ID)

    assert again.object_id == first.object_id == ANDROID_SLEEP_ID
    assert not again.is_running
    assert post_numbers(fake_server) == [1]


async def test_an_exact_case_match_wins_when_two_stored_ids_differ_only_in_case(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    phone_sleep(fake_server, ANDROID_SLEEP_ID)
    phone_sleep(fake_server, IOS_SLEEP_ID)
    engine = await synced_engine(client)
    assert {ANDROID_SLEEP_ID, IOS_SLEEP_ID} <= set(engine.records)

    await engine.end_sleep(client, IOS_SLEEP_ID)
    await engine.end_sleep(client, ANDROID_SLEEP_ID)

    assert decode(nth_post(fake_server, 0))["objectID"] == IOS_SLEEP_ID
    assert decode(nth_post(fake_server, 1))["objectID"] == ANDROID_SLEEP_ID


# --- add ---------------------------------------------------------------------------------------


async def test_a_retry_with_the_same_object_id_in_another_case_returns_the_existing_record(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    object_id = "EEEEEEEE-0000-4000-8000-0000000000AB"
    first = await engine.add_diaper(client, BABY_ID, DIAPER_WET, object_id=object_id)

    again = await engine.add_diaper(client, BABY_ID, DIAPER_WET, object_id=object_id.lower())

    assert again.object_id == first.object_id == object_id
    assert again.to_payload() == first.to_payload()
    assert post_numbers(fake_server) == [1]


async def test_a_new_object_id_is_still_written_in_uppercase(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)

    diaper = await engine.add_diaper(
        client, BABY_ID, DIAPER_WET, object_id="eeeeeeee-0000-4000-8000-0000000000ab"
    )

    assert diaper.object_id == "EEEEEEEE-0000-4000-8000-0000000000AB"
    assert decode(nth_post(fake_server, 0))["objectID"] == diaper.object_id


async def test_an_object_id_another_device_holds_in_lowercase_is_refused_in_any_case(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    android_diaper = rid(2).lower()
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Diaper", android_diaper))
    engine = await synced_engine(client)

    with pytest.raises(ObjectIdInUseError):
        await engine.add_diaper(client, BABY_ID, DIAPER_WET, object_id=android_diaper.upper())

    assert post_numbers(fake_server) == []


async def test_an_add_in_the_other_case_of_this_devices_deleted_record_is_refused(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    object_id = "EEEEEEEE-0000-4000-8000-0000000000AB"
    await engine.add_diaper(client, BABY_ID, DIAPER_WET, object_id=object_id)
    gone = {**decode(nth_post(fake_server, 0)), "timestamp": "2030-01-01 00:00:00 +0000"}
    fake_server.add_transaction(PHONE, OP_DELETE, gone)
    await engine.sync(client)

    with pytest.raises(RecordNotFoundError):
        await engine.add_diaper(client, BABY_ID, DIAPER_WET, object_id=object_id.lower())

    assert post_numbers(fake_server) == [1]


async def test_an_add_in_the_other_case_of_another_devices_deleted_id_is_refused(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    android_diaper = rid(2).lower()
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Diaper", android_diaper))
    gone = {**payload_for("Diaper", android_diaper), "timestamp": "2030-01-01 00:00:00 +0000"}
    fake_server.add_transaction(PHONE, OP_DELETE, gone)
    engine = await synced_engine(client)
    assert android_diaper not in engine.records

    with pytest.raises(ObjectIdInUseError):
        await engine.add_diaper(client, BABY_ID, DIAPER_WET, object_id=android_diaper.upper())

    assert post_numbers(fake_server) == []


# --- delete_record -----------------------------------------------------------------------------


async def test_deleting_an_own_record_by_its_id_in_another_case_posts_the_stored_id(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    diaper = await add(engine, client)

    await engine.delete_record(client, diaper.object_id.lower())

    entry = nth_post(fake_server, 1)
    assert (entry["SyncID"], entry["OPCode"]) == (2, OP_DELETE)
    assert decode(entry)["objectID"] == diaper.object_id
    assert diaper.object_id not in engine.records


async def test_another_devices_record_is_still_not_deletable_in_any_case(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    android_diaper = rid(2).lower()
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Diaper", android_diaper))
    engine = await synced_engine(client)

    with pytest.raises(NotOwnRecordError):
        await engine.delete_record(client, android_diaper)
    with pytest.raises(NotOwnRecordError):
        await engine.delete_record(client, android_diaper.upper())

    assert post_numbers(fake_server) == []
