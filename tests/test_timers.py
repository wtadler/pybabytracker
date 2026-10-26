"""Live sleep timers: start_sleep (an add with duration 0) and end_sleep (an update, opcode 1).

The capture (PROTOCOL.md, "Timers") showed the app syncs a running sleep: an add with duration 0
at the start, then an update to the same objectID at the stop with the final whole-minute
duration, ``newFlage`` "false", and a bumped timestamp. Nursing timers don't sync while running,
so they need nothing beyond ``add_nursing``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from conftest import PROBE_UUID
from factories import BABY_ID, payload_for
from fake_server import FakeSyncServer
from test_writes import (
    PHONE,
    add,
    decode,
    nth_post,
    own_posts,
    post_numbers,
    quick_client,  # noqa: F401  (a fixture; F811 at use sites)
    rid,
    seed,
    synced_engine,
)

from pybabytracker import (
    OP_ADD,
    OP_DELETE,
    OP_UPDATE,
    BabyTrackerClient,
    BabyTrackerConnectionError,
    NotOwnRecordError,
    NotSyncedError,
    RecordNotFoundError,
    Sleep,
    SyncEngine,
    TimerNotRunningError,
    UnknownBabyError,
    WriteOutcomeUnknownError,
    WrongRecordTypeError,
)

CAPTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "captured_payloads.json").read_text(encoding="utf-8")
)
IGNORED = ("objectID", "timestamp", "baby")
UTC_MINUS_5 = -18000
SLEEP_ID = "5EE90000-0000-4000-8000-000000000001"
CAPTURED_START_ID = "CAFE0000-0000-4000-8000-000000000005"


def captured_entry(label_prefix: str) -> dict[str, Any]:
    [entry] = [e for e in CAPTURE if e["label"].startswith(label_prefix)]
    result: dict[str, Any] = entry
    return result


def without_ignored(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k not in IGNORED}


def at(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S %z")


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S %z")


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def phone_sleep(
    server: FakeSyncServer,
    object_id: str = SLEEP_ID,
    *,
    start: datetime | None = None,
    duration: int = 0,
    changed: datetime | None = None,
    **overrides: Any,
) -> datetime:
    """The phone posts a sleep (running, by default). Returns its start."""
    begin = start if start is not None else now() - timedelta(minutes=30)
    payload = payload_for(
        "Sleep",
        object_id,
        time=stamp(begin),
        timestamp=stamp(changed if changed is not None else begin),
        duration=duration,
        **overrides,
    )
    server.add_transaction(PHONE, OP_ADD, payload)
    return begin


async def engine_with_phone_sleep(
    client: BabyTrackerClient, server: FakeSyncServer, **kwargs: Any
) -> tuple[SyncEngine, datetime]:
    seed(server)
    begin = phone_sleep(server, **kwargs)
    engine = await synced_engine(client)
    return engine, begin


def sleeps(engine: SyncEngine) -> list[Sleep]:
    return [r for r in engine.records.values() if isinstance(r, Sleep)]


# --- start_sleep ------------------------------------------------------------------------------


async def test_start_sleep_posts_an_add_with_duration_zero_shaped_like_the_captured_start(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)

    sleep = await engine.start_sleep(
        client, BABY_ID, start=at("2026-10-26 16:57:39 +0000"), timezone_offset=UTC_MINUS_5
    )

    entry = nth_post(fake_server, 0)
    assert (entry["SyncID"], entry["OPCode"]) == (1, OP_ADD)
    expected = without_ignored(captured_entry("C5 sleep add")["payload"])
    assert without_ignored(decode(entry)) == expected
    assert isinstance(sleep, Sleep)
    assert sleep.is_running
    assert decode(entry)["objectID"] == sleep.object_id


async def test_start_sleep_defaults_to_now_and_is_running_in_local_state_at_once(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    before = now()

    sleep = await engine.start_sleep(client, BABY_ID)

    stored = engine.records[sleep.object_id]
    assert isinstance(stored, Sleep)
    assert stored.is_running
    assert stored.time is not None
    assert before <= stored.time <= now()
    assert engine.latest_records(BABY_ID)["Sleep"].object_id == sleep.object_id
    assert sleep.object_id in engine.created_ids


async def test_start_sleep_keeps_a_note_and_a_given_object_id(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)

    sleep = await engine.start_sleep(client, BABY_ID, note="Nap", object_id=SLEEP_ID.lower())

    payload = decode(nth_post(fake_server, 0))
    assert payload["objectID"] == SLEEP_ID
    assert payload["note"] == "Nap"
    assert sleep.object_id == SLEEP_ID


@pytest.mark.parametrize(
    "start",
    [
        pytest.param(timedelta(hours=-24, minutes=-1), id="more-than-24-hours-ago"),
        pytest.param(timedelta(minutes=5), id="in-the-future"),
    ],
)
async def test_start_sleep_refuses_a_start_outside_the_24_hour_cap_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, start: timedelta
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    fake_server.requests.clear()

    with pytest.raises(ValueError):
        await engine.start_sleep(client, BABY_ID, start=now() + start)

    assert fake_server.requests == []


async def test_start_sleep_refuses_a_naive_start_and_an_unknown_baby(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)

    with pytest.raises(ValueError):
        await engine.start_sleep(client, BABY_ID, start=datetime(2026, 10, 26, 12, 0))
    with pytest.raises(UnknownBabyError):
        await engine.start_sleep(client, "00000000-0000-4000-8000-000000000000")

    assert post_numbers(fake_server) == []


async def test_a_start_sleep_retry_with_the_same_object_id_returns_the_late_landing(
    quick_client: BabyTrackerClient,  # noqa: F811
    fake_server: FakeSyncServer,
) -> None:
    seed(fake_server)
    engine = await synced_engine(quick_client, grace=5.0)
    fake_server.post_script = ["timeout-late"]
    with pytest.raises(BabyTrackerConnectionError):
        await engine.start_sleep(quick_client, BABY_ID, object_id=SLEEP_ID)
    await asyncio.sleep(fake_server.post_stall)

    sleep = await engine.start_sleep(quick_client, BABY_ID, object_id=SLEEP_ID)

    assert sleep.object_id == SLEEP_ID
    assert sleep.is_running
    assert post_numbers(fake_server) == [1]


# --- end_sleep: the payload -------------------------------------------------------------------


async def test_end_sleep_posts_an_update_shaped_like_the_captured_timer_stop(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    start_entry = captured_entry("C5 sleep add")
    fake_server.add_transaction(PHONE, OP_ADD, start_entry["payload"])
    engine = await synced_engine(client)

    ended = await engine.end_sleep(client, CAPTURED_START_ID, end=at("2026-10-26 16:58:48 +0000"))

    entry = nth_post(fake_server, 0)
    assert (entry["SyncID"], entry["OPCode"]) == (1, OP_UPDATE)
    stop = captured_entry("C5 sleep update at timer stop")
    payload = decode(entry)
    assert without_ignored(payload) == without_ignored(stop["payload"])
    assert payload["objectID"] == CAPTURED_START_ID
    assert payload["baby"] == start_entry["payload"]["baby"]
    assert list(payload) == list(json.loads(base64.b64decode(stop["transaction"])))
    assert ended.duration == 1
    assert not ended.is_running


async def test_end_sleep_changes_only_duration_new_flag_and_timestamp_and_keeps_unknown_keys(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    begin = now() - timedelta(minutes=40)
    payload = payload_for(
        "Sleep",
        SLEEP_ID,
        time=stamp(begin),
        timestamp=stamp(begin),
        duration=0,
        futureField={"nested": [1, 2]},
        locationID="loc-9",
    )
    # An unusual key order, which the update must keep.
    reordered = {k: payload[k] for k in reversed(list(payload))}
    fake_server.add_transaction(PHONE, OP_ADD, reordered)
    engine = await synced_engine(client)

    await engine.end_sleep(client, SLEEP_ID, end=begin + timedelta(minutes=25))

    posted = decode(nth_post(fake_server, 0))
    assert list(posted) == list(reordered)
    changed = {k for k in reordered if posted[k] != reordered[k]}
    assert changed == {"duration", "newFlage", "timestamp"}
    assert posted["duration"] == 25
    assert posted["newFlage"] == "false"
    assert posted["futureField"] == {"nested": [1, 2]}


async def test_end_sleep_bumps_the_timestamp_to_the_write_time(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, begin = await engine_with_phone_sleep(client, fake_server)
    before = now()

    ended = await engine.end_sleep(client, SLEEP_ID)

    posted = decode(nth_post(fake_server, 0))
    written = at(posted["timestamp"])
    assert before <= written <= now()
    assert ended.timestamp == written
    assert begin < written


async def test_end_sleep_beats_a_start_whose_timestamp_is_ahead_of_this_clock(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    ahead = now() + timedelta(hours=2)  # the phone's clock runs fast
    engine, _ = await engine_with_phone_sleep(client, fake_server, changed=ahead)

    await engine.end_sleep(client, SLEEP_ID)

    posted = decode(nth_post(fake_server, 0))
    assert at(posted["timestamp"]) == ahead + timedelta(seconds=1)
    stored = engine.records[SLEEP_ID]
    assert isinstance(stored, Sleep)
    assert not stored.is_running
    replayed = await synced_engine(client)
    assert replayed.records[SLEEP_ID].to_payload() == stored.to_payload()


@pytest.mark.parametrize(
    ("seconds", "minutes"),
    [(1, 1), (29, 1), (89, 1), (90, 2), (149, 2), (150, 3), (3600, 60)],
)
async def test_end_sleep_rounds_to_whole_minutes_half_up_with_a_minimum_of_one(
    client: BabyTrackerClient, fake_server: FakeSyncServer, seconds: int, minutes: int
) -> None:
    engine, begin = await engine_with_phone_sleep(
        client, fake_server, start=now() - timedelta(hours=2)
    )

    ended = await engine.end_sleep(client, SLEEP_ID, end=begin + timedelta(seconds=seconds))

    assert ended.duration == minutes
    assert decode(nth_post(fake_server, 0))["duration"] == minutes


async def test_end_sleep_measures_real_elapsed_time_across_zones(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, begin = await engine_with_phone_sleep(
        client, fake_server, start=now() - timedelta(hours=2)
    )
    later = (begin + timedelta(minutes=45)).astimezone(timezone(timedelta(hours=5, minutes=30)))

    ended = await engine.end_sleep(client, SLEEP_ID, end=later)

    assert ended.duration == 45


async def test_end_sleep_at_exactly_24_hours_writes_1440_minutes(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, begin = await engine_with_phone_sleep(
        client, fake_server, start=now() - timedelta(hours=30)
    )

    ended = await engine.end_sleep(client, SLEEP_ID, end=begin + timedelta(hours=24))

    assert ended.duration == 1440


async def test_end_sleep_past_the_24_hour_cap_clamps_to_24_hours_and_logs_a_warning(
    client: BabyTrackerClient, fake_server: FakeSyncServer, caplog: pytest.LogCaptureFixture
) -> None:
    engine, _ = await engine_with_phone_sleep(
        client, fake_server, start=now() - timedelta(hours=30)
    )

    with caplog.at_level(logging.WARNING, logger="pybabytracker"):
        ended = await engine.end_sleep(client, SLEEP_ID)

    assert ended.duration == 1440
    assert decode(nth_post(fake_server, 0))["duration"] == 1440
    assert any("24 hours" in r.getMessage() for r in caplog.records)


async def test_end_sleep_refuses_an_end_before_the_start_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, begin = await engine_with_phone_sleep(client, fake_server)
    fake_server.requests.clear()

    with pytest.raises(ValueError):
        await engine.end_sleep(client, SLEEP_ID, end=begin - timedelta(minutes=2))
    with pytest.raises(ValueError):
        await engine.end_sleep(client, SLEEP_ID, end=datetime(2026, 10, 26, 12, 0))

    assert fake_server.requests == []
    assert engine.records[SLEEP_ID].duration == 0  # type: ignore[attr-defined]


# --- end_sleep: what it may end ---------------------------------------------------------------


async def test_end_sleep_ends_a_sleep_another_device_started(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, begin = await engine_with_phone_sleep(client, fake_server)

    ended = await engine.end_sleep(client, SLEEP_ID, end=begin + timedelta(minutes=30))

    assert [(e["SyncID"], e["OPCode"]) for e in own_posts(fake_server)] == [(1, OP_UPDATE)]
    stored = engine.records[SLEEP_ID]
    assert isinstance(stored, Sleep)
    assert not stored.is_running
    assert stored.duration == 30
    assert ended.object_id == SLEEP_ID
    assert [s.object_id for s in sleeps(engine)] == [SLEEP_ID]


async def test_end_sleep_ends_a_sleep_this_device_started(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    sleep = await engine.start_sleep(client, BABY_ID, start=now() - timedelta(minutes=12))

    ended = await engine.end_sleep(client, sleep.object_id)

    assert [(e["SyncID"], e["OPCode"]) for e in own_posts(fake_server)] == [
        (1, OP_ADD),
        (2, OP_UPDATE),
    ]
    assert ended.duration == 12
    assert decode(nth_post(fake_server, 1))["objectID"] == sleep.object_id


async def test_end_sleep_accepts_a_lowercase_object_id(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, _ = await engine_with_phone_sleep(client, fake_server)

    ended = await engine.end_sleep(client, SLEEP_ID.lower())

    assert ended.object_id == SLEEP_ID


async def test_end_sleep_refuses_a_finished_sleep_with_no_post(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, _ = await engine_with_phone_sleep(client, fake_server, duration=45)
    fake_server.requests.clear()

    with pytest.raises(TimerNotRunningError):
        await engine.end_sleep(client, SLEEP_ID)

    assert fake_server.requests == []


async def test_end_sleep_refuses_a_deleted_sleep_with_no_post(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    phone_sleep(fake_server)
    fake_server.add_transaction(PHONE, OP_DELETE, payload_for("Sleep", SLEEP_ID, duration=0))
    engine = await synced_engine(client)

    with pytest.raises(RecordNotFoundError):
        await engine.end_sleep(client, SLEEP_ID)

    assert post_numbers(fake_server) == []


async def test_end_sleep_refuses_a_record_that_is_not_a_sleep_with_no_post(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)

    with pytest.raises(WrongRecordTypeError):
        await engine.end_sleep(client, rid(1))  # the phone's diaper

    assert post_numbers(fake_server) == []


async def test_end_sleep_refuses_a_record_never_seen(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)

    with pytest.raises(RecordNotFoundError):
        await engine.end_sleep(client, SLEEP_ID)

    assert post_numbers(fake_server) == []


async def test_end_sleep_before_any_sync_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = SyncEngine(device_uuid=PROBE_UUID)

    with pytest.raises(NotSyncedError):
        await engine.end_sleep(client, SLEEP_ID)

    assert fake_server.requests == []


async def test_ending_again_a_sleep_this_device_ended_returns_it_even_after_a_phone_edit(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    """Ending a sleep HA already ended returns it, and still does once a phone edits it."""
    engine, _ = await engine_with_phone_sleep(client, fake_server)
    first = await engine.end_sleep(client, SLEEP_ID)

    again = await engine.end_sleep(client, SLEEP_ID)

    assert again.to_payload() == first.to_payload()
    assert post_numbers(fake_server) == [1]
    edited = {**decode(nth_post(fake_server, 0)), "note": "edited"}
    edited["timestamp"] = stamp(now() + timedelta(minutes=1))
    fake_server.add_transaction(PHONE, OP_UPDATE, edited)
    await engine.sync(client)
    edited_back = await engine.end_sleep(client, SLEEP_ID)
    assert edited_back.note == "edited"
    assert post_numbers(fake_server) == [1]


# --- concurrent ends --------------------------------------------------------------------------


@pytest.mark.parametrize("phone_wins", [True, False], ids=["phone-newer", "ha-newer"])
async def test_a_phone_and_this_device_ending_the_same_sleep_leave_one_finished_sleep(
    client: BabyTrackerClient, fake_server: FakeSyncServer, phone_wins: bool
) -> None:
    engine, begin = await engine_with_phone_sleep(
        client, fake_server, start=now() - timedelta(hours=2)
    )
    # The phone ends it, but this engine hasn't synced since.
    phone_end = {
        **payload_for("Sleep", SLEEP_ID, time=stamp(begin), timestamp=stamp(begin), duration=0),
        "duration": 77,
        "newFlage": "false",
        "timestamp": stamp(now() + timedelta(hours=1) if phone_wins else begin),
    }
    fake_server.add_transaction(PHONE, OP_UPDATE, phone_end)

    await engine.end_sleep(client, SLEEP_ID, end=begin + timedelta(minutes=90))
    assert [e["OPCode"] for e in own_posts(fake_server)] == [OP_UPDATE]
    await engine.sync(client)

    [only] = sleeps(engine)
    assert only.object_id == SLEEP_ID
    assert not only.is_running
    assert only.duration == (77 if phone_wins else 90)
    replayed = SyncEngine()
    await replayed.sync(client)
    [replayed_only] = sleeps(replayed)
    assert replayed_only.to_payload() == only.to_payload()


async def test_two_concurrent_ends_of_one_sleep_post_once(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, _ = await engine_with_phone_sleep(client, fake_server)
    fake_server.delay = 0.02

    first, second = await asyncio.gather(
        engine.end_sleep(client, SLEEP_ID), engine.end_sleep(client, SLEEP_ID)
    )

    assert post_numbers(fake_server) == [1]
    assert first.to_payload() == second.to_payload()


# --- at-most-once ----------------------------------------------------------------------------


async def test_a_failed_end_is_retried_under_the_same_number_and_lands_once(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, _ = await engine_with_phone_sleep(client, fake_server)
    fake_server.post_script = ["500"]

    with pytest.raises(BabyTrackerConnectionError):
        await engine.end_sleep(client, SLEEP_ID)
    assert engine.records[SLEEP_ID].duration == 0  # type: ignore[attr-defined]
    await engine.end_sleep(client, SLEEP_ID)

    assert post_numbers(fake_server) == [1, 1]
    assert [(e["SyncID"], e["OPCode"]) for e in own_posts(fake_server)] == [(1, OP_UPDATE)]


async def test_a_timed_out_end_that_landed_is_confirmed_and_not_posted_again(
    quick_client: BabyTrackerClient,  # noqa: F811
    fake_server: FakeSyncServer,
) -> None:
    engine, _ = await engine_with_phone_sleep(quick_client, fake_server)
    fake_server.post_script = ["timeout-landed"]

    ended = await engine.end_sleep(quick_client, SLEEP_ID)

    assert post_numbers(fake_server) == [1]
    assert not ended.is_running
    assert not engine.records[SLEEP_ID].is_running  # type: ignore[attr-defined]


async def test_an_end_that_lands_late_is_found_and_the_retry_posts_nothing(
    quick_client: BabyTrackerClient,  # noqa: F811
    fake_server: FakeSyncServer,
) -> None:
    engine, _ = await engine_with_phone_sleep(quick_client, fake_server)
    engine = SyncEngine.from_dict(engine.to_dict(), unknown_outcome_grace=5.0)
    fake_server.post_script = ["timeout-late"]
    with pytest.raises(BabyTrackerConnectionError):
        await engine.end_sleep(quick_client, SLEEP_ID)
    with pytest.raises(WriteOutcomeUnknownError):
        await engine.end_sleep(quick_client, SLEEP_ID)  # too soon: it could still land
    await asyncio.sleep(fake_server.post_stall)  # it lands, as number 1

    ended = await engine.end_sleep(quick_client, SLEEP_ID)

    assert post_numbers(fake_server) == [1]
    assert [(e["SyncID"], e["OPCode"]) for e in own_posts(fake_server)] == [(1, OP_UPDATE)]
    assert ended.to_payload() == decode(nth_post(fake_server, 0))


async def test_an_end_that_never_landed_is_posted_again_after_the_grace_window(
    quick_client: BabyTrackerClient,  # noqa: F811
    fake_server: FakeSyncServer,
) -> None:
    engine, _ = await engine_with_phone_sleep(quick_client, fake_server)
    engine = SyncEngine.from_dict(engine.to_dict(), unknown_outcome_grace=0.3)
    fake_server.post_script = ["timeout-lost"]
    with pytest.raises(BabyTrackerConnectionError):
        await engine.end_sleep(quick_client, SLEEP_ID)
    await asyncio.sleep(0.35)

    await engine.end_sleep(quick_client, SLEEP_ID)

    assert post_numbers(fake_server) == [1, 1]
    assert [(e["SyncID"], e["OPCode"]) for e in own_posts(fake_server)] == [(1, OP_UPDATE)]


async def test_a_pending_end_survives_a_restart(
    quick_client: BabyTrackerClient,  # noqa: F811
    fake_server: FakeSyncServer,
) -> None:
    engine, _ = await engine_with_phone_sleep(quick_client, fake_server)
    engine = SyncEngine.from_dict(engine.to_dict(), unknown_outcome_grace=5.0)
    fake_server.post_script = ["timeout-lost"]
    with pytest.raises(BabyTrackerConnectionError):
        await engine.end_sleep(quick_client, SLEEP_ID)

    restored = SyncEngine.from_dict(
        json.loads(json.dumps(engine.to_dict())), unknown_outcome_grace=5.0
    )
    with pytest.raises(WriteOutcomeUnknownError):
        await restored.end_sleep(quick_client, SLEEP_ID)
    assert post_numbers(fake_server) == [1]


# --- numbering, own writes, replay, undo -----------------------------------------------------


async def test_an_end_shares_the_numbering_and_is_not_an_own_add(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, _ = await engine_with_phone_sleep(client, fake_server)
    diaper = await add(engine, client)

    await engine.end_sleep(client, SLEEP_ID)
    later = await add(engine, client)

    assert [(e["SyncID"], e["OPCode"]) for e in own_posts(fake_server)] == [
        (1, OP_ADD),
        (2, OP_UPDATE),
        (3, OP_ADD),
    ]
    assert SLEEP_ID not in engine.created_ids
    assert engine.own_writes() == [(diaper.object_id, 1), (later.object_id, 3)]


async def test_ending_this_devices_sleep_keeps_its_place_in_own_writes(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    sleep = await engine.start_sleep(client, BABY_ID)
    diaper = await add(engine, client)

    await engine.end_sleep(client, sleep.object_id)

    assert engine.own_writes() == [(sleep.object_id, 1), (diaper.object_id, 2)]
    restored = SyncEngine.from_dict(json.loads(json.dumps(engine.to_dict())))
    assert restored.own_writes() == engine.own_writes()


async def test_local_apply_of_a_start_and_ends_matches_a_replay_of_the_log(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, _ = await engine_with_phone_sleep(client, fake_server)
    own = await engine.start_sleep(client, BABY_ID, start=now() - timedelta(minutes=5))
    await engine.end_sleep(client, own.object_id)
    await engine.end_sleep(client, SLEEP_ID)

    replayed = await synced_engine(client)

    local, replay = engine.to_dict(), replayed.to_dict()
    assert local["records"] == replay["records"]
    assert local["tombstones"] == replay["tombstones"]
    assert local["last_seen"] == replay["last_seen"]
    assert replayed.created_ids == engine.created_ids == {own.object_id}
    assert replayed.own_writes() == engine.own_writes()
    ranks = {r["payload"]["objectID"]: r["version"]["rank"] for r in local["records"]}
    assert ranks[SLEEP_ID] == ranks[own.object_id] == 1


async def test_the_next_sync_does_not_refetch_this_devices_end(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, _ = await engine_with_phone_sleep(client, fake_server)
    await engine.end_sleep(client, SLEEP_ID)

    result = await engine.sync(client)

    assert post_numbers(fake_server) == [1]
    assert result.devices_fetched == 0


async def test_undo_of_a_running_sleep_this_device_started_deletes_it(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    sleep = await engine.start_sleep(client, BABY_ID)

    await engine.delete_record(client, sleep.object_id)

    assert [(e["SyncID"], e["OPCode"]) for e in own_posts(fake_server)] == [
        (1, OP_ADD),
        (2, OP_DELETE),
    ]
    assert sleep.object_id not in engine.records
    with pytest.raises(RecordNotFoundError):
        await engine.end_sleep(client, sleep.object_id)


async def test_undo_of_an_ended_sleep_this_device_started_sends_the_ended_record(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    sleep = await engine.start_sleep(client, BABY_ID, start=now() - timedelta(minutes=8))
    await engine.end_sleep(client, sleep.object_id)
    ended = decode(nth_post(fake_server, 1))

    await engine.delete_record(client, sleep.object_id)

    entry = nth_post(fake_server, 2)
    assert entry["OPCode"] == OP_DELETE
    assert decode(entry) == ended  # newFlage is already "false"; the timestamp isn't bumped
    assert sleep.object_id not in engine.records


async def test_a_sleep_another_device_started_cannot_be_undone_even_after_ending_it(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, _ = await engine_with_phone_sleep(client, fake_server)
    await engine.end_sleep(client, SLEEP_ID)

    with pytest.raises(NotOwnRecordError):
        await engine.delete_record(client, SLEEP_ID)

    assert post_numbers(fake_server) == [1]


# --- round 1: clock skew, a retry after a phone edit, a phone delete racing an end ----------


async def test_ending_a_phone_sleep_whose_start_is_ahead_of_this_clock_writes_one_minute(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, _ = await engine_with_phone_sleep(
        client, fake_server, start=now() + timedelta(seconds=30)
    )

    ended = await engine.end_sleep(client, SLEEP_ID)

    assert ended.duration == 1
    assert decode(nth_post(fake_server, 0))["duration"] == 1


async def test_ending_a_sleep_this_device_started_slightly_ahead_writes_one_minute(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    sleep = await engine.start_sleep(client, BABY_ID, start=now() + timedelta(seconds=30))

    ended = await engine.end_sleep(client, sleep.object_id)

    assert ended.duration == 1


async def test_an_end_exactly_at_the_start_writes_one_minute(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, begin = await engine_with_phone_sleep(client, fake_server)

    ended = await engine.end_sleep(client, SLEEP_ID, end=begin)

    assert ended.duration == 1


async def test_an_end_far_in_the_future_is_clamped_to_now_with_a_warning(
    client: BabyTrackerClient, fake_server: FakeSyncServer, caplog: pytest.LogCaptureFixture
) -> None:
    engine, begin = await engine_with_phone_sleep(
        client, fake_server, start=now() - timedelta(minutes=20)
    )

    with caplog.at_level(logging.WARNING, logger="pybabytracker"):
        ended = await engine.end_sleep(client, SLEEP_ID, end=now() + timedelta(hours=3))

    assert ended.duration == 20
    assert any("future" in r.getMessage() for r in caplog.records)


async def test_an_end_within_a_minute_ahead_is_kept(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine, begin = await engine_with_phone_sleep(
        client, fake_server, start=now() - timedelta(minutes=20)
    )

    ended = await engine.end_sleep(client, SLEEP_ID, end=begin + timedelta(minutes=20, seconds=40))

    assert ended.duration == 21


async def test_a_retry_after_a_late_end_and_a_synced_phone_edit_returns_the_sleep(
    quick_client: BabyTrackerClient,  # noqa: F811
    fake_server: FakeSyncServer,
) -> None:
    engine, _ = await engine_with_phone_sleep(quick_client, fake_server)
    engine = SyncEngine.from_dict(engine.to_dict(), unknown_outcome_grace=5.0)
    fake_server.post_script = ["timeout-late"]
    with pytest.raises(BabyTrackerConnectionError):
        await engine.end_sleep(quick_client, SLEEP_ID)
    await asyncio.sleep(fake_server.post_stall)  # HA's end lands
    edited = {**decode(nth_post(fake_server, 0)), "note": "edited on the phone"}
    edited["timestamp"] = stamp(now() + timedelta(minutes=1))
    fake_server.add_transaction(PHONE, OP_UPDATE, edited)
    await engine.sync(quick_client)  # brings in HA's own end and the phone's edit

    ended = await engine.end_sleep(quick_client, SLEEP_ID)

    assert post_numbers(fake_server) == [1]
    assert ended.note == "edited on the phone"
    assert not ended.is_running
    restored = SyncEngine.from_dict(json.loads(json.dumps(engine.to_dict())))
    assert (await restored.end_sleep(quick_client, SLEEP_ID)).object_id == SLEEP_ID
    assert post_numbers(fake_server) == [1]


async def test_a_phone_delete_racing_this_devices_end_leaves_the_ended_sleep(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    """Pins today's merge: the app's delete carries the record's unbumped timestamp, so this
    device's later end (a newer timestamp) outlives it, here and on a replay. The app's own
    handling is checked on the test iPad in #20."""
    engine, begin = await engine_with_phone_sleep(client, fake_server)
    deleted = payload_for("Sleep", SLEEP_ID, time=stamp(begin), timestamp=stamp(begin), duration=0)
    fake_server.add_transaction(PHONE, OP_DELETE, {**deleted, "newFlage": "false"})

    await engine.end_sleep(client, SLEEP_ID)
    await engine.sync(client)

    [only] = sleeps(engine)
    assert only.object_id == SLEEP_ID
    assert not only.is_running
    replayed = SyncEngine()
    await replayed.sync(client)
    assert [s.to_payload() for s in sleeps(replayed)] == [only.to_payload()]
