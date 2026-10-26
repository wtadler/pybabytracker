"""Writes of formula, pumped, nursing, sleep, and pump records against the fake sync server.

Each type's payload is compared with the payload the app produced in the iPad capture
(``fixtures/captured_payloads.json``): the same keys and values, apart from the record's ID,
write timestamp, note, and embedded baby.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from factories import BABY_ID
from fake_server import FakeSyncServer
from test_writes import (
    decode,
    nth_post,
    own_posts,
    post_numbers,
    quick_client,  # noqa: F401  (a fixture; F811 at use sites)
    seed,
    synced_engine,
)

from pybabytracker import (
    SIDE_LEFT,
    SIDE_RIGHT,
    BabyTrackerClient,
    BabyTrackerConnectionError,
    Formula,
    Nursing,
    Pump,
    Pumped,
    Sleep,
    SyncEngine,
    UnknownBabyError,
    Volume,
    VolumeMeasure,
)

CAPTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "captured_payloads.json").read_text(encoding="utf-8")
)
CAPTURED = {entry["label"]: entry for entry in CAPTURE}
IGNORED = ("objectID", "timestamp", "note", "baby")
UTC_MINUS_5 = -18000


def captured(label_prefix: str, opcode: int = 0) -> dict[str, Any]:
    """The captured payload (minus the keys a write fills in itself) for a label."""
    [entry] = [e for e in CAPTURE if e["label"].startswith(label_prefix) and e["opcode"] == opcode]
    return {k: v for k, v in entry["payload"].items() if k not in IGNORED}


def captured_keys(label_prefix: str) -> set[str]:
    [entry] = [e for e in CAPTURE if e["label"].startswith(label_prefix) and e["opcode"] == 0]
    return set(entry["payload"])


def posted(server: FakeSyncServer, index: int = 0) -> dict[str, Any]:
    return decode(nth_post(server, index))


def comparable(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k not in IGNORED}


def at(text: str) -> datetime:
    """A time from the capture's format, as an aware UTC datetime."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S %z")


# One call per type, with inputs chosen to reproduce the captured record.
Writer = Callable[[SyncEngine, BabyTrackerClient, dict[str, Any]], Awaitable[Any]]


async def write_formula(
    engine: SyncEngine, client: BabyTrackerClient, extra: dict[str, Any]
) -> Any:
    return await engine.add_formula(
        client,
        BABY_ID,
        volume=Volume(4, "oz"),
        time=at("2026-10-26 16:54:35 +0000"),
        timezone_offset=UTC_MINUS_5,
        **extra,
    )


async def write_pumped(engine: SyncEngine, client: BabyTrackerClient, extra: dict[str, Any]) -> Any:
    return await engine.add_pumped(
        client,
        BABY_ID,
        volume=Volume(3, "oz"),
        time=at("2026-10-26 16:54:55 +0000"),
        timezone_offset=UTC_MINUS_5,
        **extra,
    )


async def write_pump(engine: SyncEngine, client: BabyTrackerClient, extra: dict[str, Any]) -> Any:
    return await engine.add_pump(
        client,
        BABY_ID,
        left_volume=Volume(2, "oz"),
        right_volume=Volume(5, "oz"),
        left_minutes=1,
        right_minutes=1,
        finish_side=SIDE_LEFT,
        time=at("2026-10-26 16:55:15 +0000"),
        timezone_offset=UTC_MINUS_5,
        **extra,
    )


async def write_nursing(
    engine: SyncEngine, client: BabyTrackerClient, extra: dict[str, Any]
) -> Any:
    return await engine.add_nursing(
        client,
        BABY_ID,
        left_minutes=1,
        right_minutes=1,
        finish_side=SIDE_RIGHT,
        start_time=at("2026-10-26 16:55:52 +0000"),
        timezone_offset=UTC_MINUS_5,
        **extra,
    )


async def write_sleep(engine: SyncEngine, client: BabyTrackerClient, extra: dict[str, Any]) -> Any:
    return await engine.add_sleep(
        client,
        BABY_ID,
        start=at("2026-10-26 16:57:39 +0000"),
        end=at("2026-10-26 16:58:48 +0000"),
        timezone_offset=UTC_MINUS_5,
        **extra,
    )


TYPES: dict[str, tuple[Writer, str, type[Any]]] = {
    "formula": (write_formula, "C1 formula add", Formula),
    "pumped": (write_pumped, "C2 pumped bottle add", Pumped),
    "pump": (write_pump, "C3 pump add", Pump),
    "nursing": (write_nursing, "C4 nursing add", Nursing),
    "sleep": (write_sleep, "C5 sleep add", Sleep),
}
ALL = list(TYPES)


async def ready(client: BabyTrackerClient, fake_server: FakeSyncServer) -> SyncEngine:
    seed(fake_server)
    return await synced_engine(client)


# --- AC1: each type's payload equals the captured one ------------------------------------


@pytest.mark.parametrize("name", ALL)
async def test_each_new_types_payload_equals_the_captured_one_key_for_key(
    name: str, client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    write, label, _ = TYPES[name]
    engine = await ready(client, fake_server)

    await write(engine, client, {"note": "n"})

    payload = posted(fake_server)
    expected = captured(label)
    if name == "sleep":
        expected["duration"] = 1  # the capture's add is at timer start; its stop update has 1
    assert comparable(payload) == expected
    assert set(payload) == set(captured_keys(label))  # including the write-filled keys
    assert nth_post(fake_server, 0)["OPCode"] == 0


@pytest.mark.parametrize("name", ALL)
async def test_each_new_type_fills_the_keys_a_write_owns(
    name: str, client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    write, _, record_type = TYPES[name]
    engine = await ready(client, fake_server)
    before = datetime.now(UTC).replace(microsecond=0)

    record = await write(
        engine, client, {"note": "bottle note", "object_id": "abab0000-0000-4000-8000-000000000009"}
    )

    after = datetime.now(UTC)
    payload = posted(fake_server)
    assert isinstance(record, record_type)
    assert payload["objectID"] == "ABAB0000-0000-4000-8000-000000000009"
    assert payload["note"] == "bottle note"
    assert payload["baby"]["objectID"] == BABY_ID
    assert before <= at(payload["timestamp"]) <= after
    assert engine.records[payload["objectID"]].to_payload() == payload


@pytest.mark.parametrize("name", ALL)
async def test_each_new_type_defaults_to_a_new_uppercase_object_id_and_an_empty_note(
    name: str, client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    write, _, _ = TYPES[name]
    engine = await ready(client, fake_server)

    record = await write(engine, client, {})

    payload = posted(fake_server)
    assert payload["objectID"] == record.object_id == record.object_id.upper()
    assert len(record.object_id) == 36
    assert payload["note"] == ""


# --- volumes ---------------------------------------------------------------------------


async def test_an_ounce_volume_is_a_volume_measure_with_english_measure_true(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)

    await engine.add_formula(client, BABY_ID, volume=Volume(4, "oz"))

    assert posted(fake_server)["amount"] == {
        "BCObjectType": "VolumeMeasure",
        "englishMeasure": "true",
        "value": 4,
    }


@pytest.mark.parametrize(
    "call",
    [
        ("add_formula", {"volume": Volume(120, "ml")}),
        ("add_pumped", {"volume": Volume(90, "ml")}),
        ("add_pump", {"left_volume": Volume(60, "ml"), "left_minutes": 15}),
        ("add_pump", {"left_volume": Volume(2, "oz"), "right_volume": Volume(60, "ml")}),
    ],
    ids=["formula", "pumped", "pump", "pump-mixed"],
)
async def test_a_milliliter_write_is_refused_with_no_request(
    call: tuple[str, dict[str, Any]], client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)
    fake_server.requests.clear()
    method, kwargs = call

    with pytest.raises(ValueError, match="metric volumes aren't supported"):
        await getattr(engine, method)(client, BABY_ID, **kwargs)

    assert fake_server.requests == []


async def test_a_milliliter_record_from_another_device_still_reads_as_milliliters(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    from conftest import PROBE_UUID  # noqa: F401
    from factories import payload_for
    from test_writes import PHONE, rid

    from pybabytracker import OP_ADD

    seed(fake_server)
    fake_server.add_transaction(
        PHONE,
        OP_ADD,
        payload_for(
            "Formula",
            rid(2),
            amount={"BCObjectType": "VolumeMeasure", "englishMeasure": "false", "value": 120},
        ),
    )
    engine = await synced_engine(client)

    record = engine.records[rid(2)]
    assert isinstance(record, Formula)
    assert record.volume == Volume(120, "ml")


async def test_every_written_volume_is_rounded_to_two_decimals(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)

    await engine.add_formula(client, BABY_ID, volume=Volume(0.1 + 0.2, "oz"))
    await engine.add_pump(
        client,
        BABY_ID,
        left_volume=Volume(1.005, "oz"),
        right_volume=Volume(2.3333333, "oz"),
        left_minutes=5,
    )

    assert posted(fake_server, 0)["amount"]["value"] == 0.3
    pump = posted(fake_server, 1)
    assert pump["leftAmount"]["value"] == 1.0
    assert pump["rightAmount"]["value"] == 2.33
    assert pump["amount"]["value"] == 3.33


async def test_a_volume_that_rounds_to_zero_is_refused(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)
    fake_server.requests.clear()

    with pytest.raises(ValueError):
        await engine.add_formula(client, BABY_ID, volume=Volume(0.004, "oz"))

    assert fake_server.requests == []


async def test_a_fractional_volume_keeps_its_fraction_and_a_whole_one_is_an_integer(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)

    await engine.add_formula(client, BABY_ID, volume=Volume(2.5, "oz"))
    await engine.add_formula(client, BABY_ID, volume=Volume(4.0, "oz"))

    first, second = posted(fake_server, 0), posted(fake_server, 1)
    assert first["amount"]["value"] == 2.5
    assert second["amount"]["value"] == 4
    assert isinstance(second["amount"]["value"], int)
    assert '"value":4}' in json.dumps(second["amount"], separators=(",", ":"))


async def test_a_pumped_bottle_takes_the_same_volume_encoding(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)

    record = await engine.add_pumped(client, BABY_ID, volume=Volume(3, "oz"))

    assert posted(fake_server)["BCObjectType"] == "Pumped"
    assert posted(fake_server)["amount"]["englishMeasure"] == "true"
    assert isinstance(record.amount, VolumeMeasure)


# --- times and offsets ---------------------------------------------------------------------


@pytest.mark.parametrize("name", ["formula", "pumped"])
async def test_a_bottles_time_defaults_to_now_and_its_offset_to_the_times_own(
    name: str, client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)
    local = datetime(2026, 10, 25, 10, 40, 49, tzinfo=timezone(timedelta(hours=-5)))
    method = engine.add_formula if name == "formula" else engine.add_pumped

    await method(client, BABY_ID, volume=Volume(4, "oz"), time=local)
    before = datetime.now(UTC).replace(microsecond=0)
    await method(client, BABY_ID, volume=Volume(4, "oz"))

    first, second = posted(fake_server, 0), posted(fake_server, 1)
    assert first["time"] == "2026-10-25 15:40:49 +0000"
    assert first["timezone"] == UTC_MINUS_5
    assert before <= at(second["time"]) <= datetime.now(UTC)


# --- sleep -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "minutes"),
    [(69, 1), (29, 1), (30, 1), (89, 1), (90, 2), (600, 10), (601, 10), (3600 + 31, 61), (5, 1)],
)
async def test_a_sleeps_duration_is_whole_minutes_rounded_half_up_with_a_minimum_of_one(
    seconds: int, minutes: int, client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)
    start = datetime(2026, 10, 26, 20, 0, 0, tzinfo=UTC)

    await engine.add_sleep(client, BABY_ID, start=start, end=start + timedelta(seconds=seconds))

    assert posted(fake_server)["duration"] == minutes


async def test_a_sleeps_time_is_its_start_in_utc_and_its_offset_defaults_from_the_start(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)
    zone = timezone(timedelta(hours=-5))
    start = datetime(2026, 10, 26, 20, 0, 0, tzinfo=zone)

    record = await engine.add_sleep(client, BABY_ID, start=start, end=start + timedelta(hours=2))

    payload = posted(fake_server)
    assert payload["time"] == "2026-10-27 01:00:00 +0000"
    assert payload["timezone"] == UTC_MINUS_5
    assert payload["duration"] == 120
    assert record.is_running is False


async def test_a_sleep_with_ends_in_different_zones_measures_real_elapsed_time(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)
    start = datetime(2026, 10, 26, 20, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
    end = datetime(2026, 10, 27, 2, 0, 0, tzinfo=UTC)  # 21:00 at the start's offset

    await engine.add_sleep(client, BABY_ID, start=start, end=end)

    assert posted(fake_server)["duration"] == 60


# --- nursing ---------------------------------------------------------------------------------


async def test_an_end_time_places_the_nursing_start_that_many_minutes_earlier(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)
    end = datetime(2026, 10, 26, 12, 30, 0, tzinfo=UTC)

    await engine.add_nursing(
        client, BABY_ID, left_minutes=10, right_minutes=5, finish_side=SIDE_RIGHT, end_time=end
    )

    payload = posted(fake_server)
    assert payload["time"] == "2026-10-26 12:15:00 +0000"
    assert (payload["leftDuration"], payload["rightDuration"]) == (10, 5)
    assert payload["bothDuration"] == 0


async def test_nursing_with_no_time_ends_now(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)
    before = datetime.now(UTC).replace(microsecond=0)

    await engine.add_nursing(client, BABY_ID, left_minutes=10)

    start = at(posted(fake_server)["time"])
    assert before - timedelta(minutes=10) <= start <= datetime.now(UTC) - timedelta(minutes=10)


@pytest.mark.parametrize(
    ("left", "right", "finish", "expected"),
    [
        (5, 0, None, SIDE_LEFT),
        (0, 5, None, SIDE_RIGHT),
        (5, 5, None, SIDE_RIGHT),
        (5, 5, SIDE_LEFT, SIDE_LEFT),
        (5, 0, SIDE_RIGHT, SIDE_RIGHT),
    ],
)
async def test_the_finish_side_defaults_to_the_last_side_nursed_and_an_explicit_one_wins(
    left: int,
    right: int,
    finish: int | None,
    expected: int,
    client: BabyTrackerClient,
    fake_server: FakeSyncServer,
) -> None:
    engine = await ready(client, fake_server)
    extra: dict[str, Any] = {} if finish is None else {"finish_side": finish}

    await engine.add_nursing(client, BABY_ID, left_minutes=left, right_minutes=right, **extra)

    assert posted(fake_server)["finishSide"] == expected


async def test_a_nursing_record_decodes_with_both_durations_and_the_side(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)

    record = await engine.add_nursing(client, BABY_ID, left_minutes=7, right_minutes=3)

    assert isinstance(record, Nursing)
    assert (record.left_duration, record.right_duration, record.finish_side) == (7, 3, 1)
    assert record.is_detailed == "true"
    assert record.state == 5


# --- pump ------------------------------------------------------------------------------------


async def test_a_pumps_total_is_the_sum_of_its_sides(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)

    await engine.add_pump(
        client,
        BABY_ID,
        left_volume=Volume(2.5, "oz"),
        right_volume=Volume(5, "oz"),
        left_minutes=10,
        right_minutes=12,
    )

    payload = posted(fake_server)
    assert payload["amount"]["value"] == 7.5
    assert payload["leftAmount"]["value"] == 2.5
    assert payload["rightAmount"]["value"] == 5
    assert (payload["leftDuration"], payload["rightDuration"]) == (10, 12)


async def test_a_one_sided_pump_writes_zero_for_the_other_side_in_the_same_unit(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)

    await engine.add_pump(
        client, BABY_ID, left_volume=Volume(6, "oz"), left_minutes=15, finish_side=SIDE_LEFT
    )

    payload = posted(fake_server)
    measure = {"BCObjectType": "VolumeMeasure", "englishMeasure": "true"}
    assert payload["amount"] == {**measure, "value": 6}
    assert payload["rightAmount"] == {**measure, "value": 0}
    assert payload["rightDuration"] == 0
    assert payload["finishSide"] == SIDE_LEFT
    assert payload["sides"] == 0


async def test_a_pump_decodes_back_to_the_volumes_written(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)

    record = await engine.add_pump(
        client, BABY_ID, left_volume=Volume(3, "oz"), right_volume=Volume(4, "oz")
    )

    assert isinstance(record, Pump)
    assert record.volume == Volume(7, "oz")
    assert record.left_volume == Volume(3, "oz")
    assert record.right_volume == Volume(4, "oz")


# --- AC: every new type is at-most-once ---------------------------------------------------------


@pytest.mark.parametrize("name", ALL)
async def test_a_failed_post_is_retried_under_the_same_number_and_lands_once(
    name: str, client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    write, _, _ = TYPES[name]
    engine = await ready(client, fake_server)
    fake_server.post_script = ["500"]

    with pytest.raises(BabyTrackerConnectionError):
        await write(engine, client, {})
    assert own_posts(fake_server) == []
    record = await write(engine, client, {})

    assert post_numbers(fake_server) == [1, 1]
    assert [e["SyncID"] for e in own_posts(fake_server)] == [1]
    assert record.object_id in engine.records


@pytest.mark.parametrize("name", ALL)
async def test_a_timed_out_post_that_landed_is_confirmed_and_not_posted_again(
    name: str,
    quick_client: BabyTrackerClient,  # noqa: F811
    fake_server: FakeSyncServer,
) -> None:
    write, _, _ = TYPES[name]
    engine = await ready(quick_client, fake_server)
    fake_server.post_script = ["timeout-landed"]

    record = await write(engine, quick_client, {})

    assert post_numbers(fake_server) == [1]
    assert record.object_id in engine.created_ids
    await write(engine, quick_client, {})
    assert [e["SyncID"] for e in own_posts(fake_server)] == [1, 2]


@pytest.mark.parametrize("name", ALL)
async def test_a_retry_with_the_same_object_id_returns_the_late_landing_instead_of_posting(
    name: str,
    quick_client: BabyTrackerClient,  # noqa: F811
    fake_server: FakeSyncServer,
) -> None:
    write, _, record_type = TYPES[name]
    engine = await ready(quick_client, fake_server)
    object_id = "EEEEEEEE-0000-4000-8000-000000000001"
    fake_server.post_script = ["timeout-late"]
    with pytest.raises(BabyTrackerConnectionError):
        await write(engine, quick_client, {"object_id": object_id})
    await asyncio.sleep(fake_server.post_stall)  # it lands

    record = await write(engine, quick_client, {"object_id": object_id})

    assert isinstance(record, record_type)
    assert record.object_id == object_id
    assert post_numbers(fake_server) == [1]
    assert engine.records[object_id].to_payload() == posted(fake_server)


@pytest.mark.parametrize("name", ALL)
async def test_concurrent_writes_of_a_type_get_consecutive_distinct_numbers(
    name: str, client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    write, _, _ = TYPES[name]
    engine = await ready(client, fake_server)
    fake_server.delay = 0.02

    records = await asyncio.gather(*(write(engine, client, {}) for _ in range(3)))

    assert sorted(e["SyncID"] for e in own_posts(fake_server)) == [1, 2, 3]
    assert len({r.object_id for r in records}) == 3


async def test_mixed_types_share_one_numbering_and_a_retry_of_another_type_is_refused(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)
    object_id = "EEEEEEEE-0000-4000-8000-000000000002"

    await engine.add_diaper(client, BABY_ID, 0)
    await write_formula(engine, client, {"object_id": object_id})
    await write_sleep(engine, client, {})

    assert [e["SyncID"] for e in own_posts(fake_server)] == [1, 2, 3]
    with pytest.raises(ValueError, match="another type"):
        await write_pumped(engine, client, {"object_id": object_id})
    assert len(own_posts(fake_server)) == 3


@pytest.mark.parametrize("name", ALL)
async def test_a_new_type_for_an_unknown_baby_is_refused_without_a_post(
    name: str, client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)
    by_name: dict[str, dict[str, Any]] = {
        "formula": {"volume": Volume(4, "oz")},
        "pumped": {"volume": Volume(4, "oz")},
        "pump": {"left_volume": Volume(4, "oz"), "left_minutes": 5},
        "nursing": {"left_minutes": 5},
        "sleep": {
            "start": datetime(2026, 10, 26, 1, tzinfo=UTC),
            "end": datetime(2026, 10, 26, 2, tzinfo=UTC),
        },
    }
    kwargs = by_name[name]

    with pytest.raises(UnknownBabyError):
        await getattr(engine, f"add_{name}")(client, "NOPE", **kwargs)

    assert own_posts(fake_server) == []


# --- bad input: refused with no request ------------------------------------------------------

NOW = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)
NAIVE = datetime(2026, 10, 26, 12, 0, 0)  # noqa: DTZ001

BAD_CALLS: dict[str, tuple[str, dict[str, Any]]] = {
    "negative bottle": ("add_formula", {"volume": Volume(-1, "oz")}),
    "zero bottle": ("add_formula", {"volume": Volume(0, "oz")}),
    "nan bottle": ("add_formula", {"volume": Volume(math.nan, "oz")}),
    "infinite bottle": ("add_pumped", {"volume": Volume(math.inf, "oz")}),
    "unknown unit": ("add_formula", {"volume": Volume(4, "cups")}),
    "unit in the wrong case": ("add_formula", {"volume": Volume(4, "OZ")}),
    "plain number for a volume": ("add_pumped", {"volume": 4}),
    "boolean volume": ("add_formula", {"volume": Volume(True, "oz")}),
    "naive bottle time": ("add_formula", {"volume": Volume(4, "oz"), "time": NAIVE}),
    "naive pumped time": ("add_pumped", {"volume": Volume(4, "oz"), "time": NAIVE}),
    "sleep end before start": ("add_sleep", {"start": NOW, "end": NOW - timedelta(minutes=5)}),
    "sleep end equals start": ("add_sleep", {"start": NOW, "end": NOW}),
    "naive sleep start": ("add_sleep", {"start": NAIVE, "end": NOW}),
    "naive sleep end": ("add_sleep", {"start": NOW, "end": NAIVE}),
    "nursing with no minutes": ("add_nursing", {"left_minutes": 0, "right_minutes": 0}),
    "negative nursing minutes": ("add_nursing", {"left_minutes": -1, "right_minutes": 5}),
    "fractional nursing minutes": ("add_nursing", {"left_minutes": 2.5}),
    "boolean nursing minutes": ("add_nursing", {"left_minutes": True}),
    "nursing start and end together": (
        "add_nursing",
        {"left_minutes": 5, "start_time": NOW, "end_time": NOW + timedelta(minutes=5)},
    ),
    "naive nursing start": ("add_nursing", {"left_minutes": 5, "start_time": NAIVE}),
    "naive nursing end": ("add_nursing", {"left_minutes": 5, "end_time": NAIVE}),
    "nursing finish side 2": ("add_nursing", {"left_minutes": 5, "finish_side": 2}),
    "pump with no volume": ("add_pump", {"left_minutes": 5}),
    "negative pump side": (
        "add_pump",
        {"left_volume": Volume(-1, "oz"), "right_volume": Volume(5, "oz")},
    ),
    "zero pump total": (
        "add_pump",
        {"left_volume": Volume(0, "oz"), "right_volume": Volume(0, "oz")},
    ),
    "pump sides in different units": (
        "add_pump",
        {"left_volume": Volume(2, "oz"), "right_volume": Volume(60, "ml")},
    ),
    "pump unknown unit": ("add_pump", {"left_volume": Volume(2, "pt")}),
    "negative pump minutes": ("add_pump", {"left_volume": Volume(2, "oz"), "right_minutes": -3}),
    "naive pump time": ("add_pump", {"left_volume": Volume(2, "oz"), "time": NAIVE}),
    "nursing over 240 minutes a side": ("add_nursing", {"left_minutes": 241}),
    "nursing right over 240": ("add_nursing", {"left_minutes": 5, "right_minutes": 241}),
    "pump over 240 minutes a side": (
        "add_pump",
        {"left_volume": Volume(2, "oz"), "left_minutes": 241},
    ),
    "sleep over 24 hours": (
        "add_sleep",
        {"start": NOW, "end": NOW + timedelta(hours=24, seconds=1)},
    ),
    "pump finish side 3": ("add_pump", {"left_volume": Volume(2, "oz"), "finish_side": 3}),
}


@pytest.mark.parametrize("case", list(BAD_CALLS))
async def test_bad_input_is_refused_with_no_request_at_all(
    case: str, client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)
    fake_server.requests.clear()
    method, kwargs = BAD_CALLS[case]

    with pytest.raises((ValueError, TypeError)):
        await getattr(engine, method)(client, BABY_ID, **kwargs)

    assert fake_server.requests == []


@pytest.mark.parametrize("case", ["negative bottle", "sleep end before start", "unknown unit"])
async def test_a_refused_write_does_not_use_up_a_number(
    case: str, client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)
    method, kwargs = BAD_CALLS[case]
    with pytest.raises(ValueError):
        await getattr(engine, method)(client, BABY_ID, **kwargs)

    await write_formula(engine, client, {})

    assert [e["SyncID"] for e in own_posts(fake_server)] == [1]


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("add_nursing", {"left_minutes": 240, "right_minutes": 240}),
        ("add_pump", {"left_volume": Volume(2, "oz"), "left_minutes": 240}),
        ("add_sleep", {"start": NOW, "end": NOW + timedelta(hours=24)}),
    ],
    ids=["nursing", "pump", "sleep"],
)
async def test_the_largest_allowed_duration_is_accepted(
    method: str, kwargs: dict[str, Any], client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)

    await getattr(engine, method)(client, BABY_ID, **kwargs)

    assert len(own_posts(fake_server)) == 1


# --- own_writes and undo -----------------------------------------------------------------------


async def test_own_writes_lists_every_new_type_in_the_order_written(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)

    written = [
        (await TYPES[name][0](engine, client, {})).object_id
        for name in ["sleep", "formula", "pump", "nursing", "pumped"]
    ]

    assert engine.own_writes() == [(oid, n) for n, oid in enumerate(written, start=1)]
    assert set(written) <= engine.created_ids


@pytest.mark.parametrize("name", ALL)
async def test_each_new_type_can_be_deleted_by_this_device(
    name: str, client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    write, _, _ = TYPES[name]
    engine = await ready(client, fake_server)
    record = await write(engine, client, {})

    await engine.delete_record(client, record.object_id)

    assert record.object_id not in engine.records
    assert nth_post(fake_server, 1)["OPCode"] == 2
    assert engine.own_writes() == []
    # The delete carries the record as saved, with only newFlage flipped.
    assert posted(fake_server, 1) == {**posted(fake_server, 0), "newFlage": "false"}


async def test_the_next_sync_does_not_reapply_the_new_types_writes(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)
    await write_sleep(engine, client, {})
    await write_pump(engine, client, {})

    result = await engine.sync(client)

    assert result.transactions_applied == 0
    assert len(engine.own_writes()) == 2
