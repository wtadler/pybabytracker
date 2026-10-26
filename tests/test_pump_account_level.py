"""A pump belongs to the account, not a baby.

The live service's Pump payloads carry no ``baby`` key. A pump written here must match that
shape, babies must never be derived from pumps, and a pump that does carry a baby still decodes.
"""

from __future__ import annotations

import base64
import inspect
import json
from pathlib import Path
from typing import Any

import pytest
from factories import BABY_ID, BABY_TWO_ID, baby_payload, encode_b64, payload_for
from fake_server import FakeSyncServer
from test_writes import PHONE, add, decode, nth_post, own_posts, rid, seed, synced_engine

from pybabytracker import (
    OP_ADD,
    OP_DELETE,
    SIDE_LEFT,
    SIDE_RIGHT,
    BabyTrackerClient,
    Pump,
    RawTransaction,
    SyncEngine,
    UnknownRecord,
    Volume,
    decode_payload,
    decode_transaction,
    encode_transaction,
)
from pybabytracker.cli import format_latest

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "captured_payloads.json").read_text(encoding="utf-8")
)
PUMP_FIXTURES = [f for f in FIXTURES if f["payload"]["BCObjectType"] == "Pump"]
TOTAL_ONLY = next(f for f in PUMP_FIXTURES if f["label"].startswith("P1"))
PER_SIDE = next(f for f in PUMP_FIXTURES if f["label"] == "C3 pump add")

# The keys of the live service's real Pump payloads: shape only, nothing else is kept.
REAL_PUMP_KEYS = {
    "BCObjectType",
    "amount",
    "finishSide",
    "leftDuration",
    "newFlage",
    "note",
    "objectID",
    "pictureLoaded",
    "pictureNote",
    "rightDuration",
    "sides",
    "time",
    "timestamp",
    "timezone",
}
PER_SIDE_KEYS = REAL_PUMP_KEYS | {"leftAmount", "rightAmount"}

OTHER_PHONE = "BBBB2222-0000-4000-8000-00000000000B"


def raw(fixture: dict[str, Any]) -> RawTransaction:
    return RawTransaction(
        sync_id=fixture["sync_id"], op_code=fixture["opcode"], transaction=fixture["transaction"]
    )


def pump_without_baby(object_id: str, **overrides: Any) -> dict[str, Any]:
    payload = payload_for("Pump", object_id, **overrides)
    del payload["baby"]
    del payload["hasPicture"]
    return payload


# --- reading: the fixtures --------------------------------------------------------------------


def test_there_are_two_pump_fixtures_and_neither_has_a_baby() -> None:
    assert len(PUMP_FIXTURES) == 2
    for fixture in PUMP_FIXTURES:
        assert "baby" not in fixture["payload"]
        assert "baby" not in json.loads(base64.b64decode(fixture["transaction"]))


def test_the_total_only_fixture_has_exactly_the_real_pump_keys() -> None:
    assert set(TOTAL_ONLY["payload"]) == REAL_PUMP_KEYS


def test_the_per_side_fixture_has_the_real_keys_plus_the_two_side_amounts() -> None:
    assert set(PER_SIDE["payload"]) == PER_SIDE_KEYS


@pytest.mark.parametrize("fixture", PUMP_FIXTURES, ids=[f["label"] for f in PUMP_FIXTURES])
def test_a_pump_with_no_baby_decodes_as_a_typed_pump(fixture: dict[str, Any]) -> None:
    record = decode_transaction(fixture["transaction"])
    assert type(record) is Pump
    assert record.baby is None
    assert record.baby_id is None


@pytest.mark.parametrize("fixture", PUMP_FIXTURES, ids=[f["label"] for f in PUMP_FIXTURES])
def test_a_pump_with_no_baby_round_trips_byte_for_byte(fixture: dict[str, Any]) -> None:
    record = decode_transaction(fixture["transaction"])
    assert encode_transaction(record) == fixture["transaction"]
    assert record.to_payload() == fixture["payload"]
    assert list(record.to_payload()) == list(fixture["payload"])


def test_a_total_only_pump_has_a_volume_and_no_side_volumes() -> None:
    record = decode_transaction(TOTAL_ONLY["transaction"])
    assert isinstance(record, Pump)
    assert record.volume == Volume(4.0, "oz")
    assert record.left_volume is None
    assert record.right_volume is None
    assert (record.left_duration, record.right_duration) == (5, 7)


# --- reading stays tolerant ---------------------------------------------------------------------


def test_a_pump_that_carries_a_baby_still_decodes_and_round_trips() -> None:
    payload = payload_for("Pump", rid(1))
    assert "baby" in payload
    record = decode_payload(payload)
    assert type(record) is Pump
    assert record.baby is None  # never read as the pump's baby
    assert record.baby_id is None
    assert record.extra["baby"] == payload["baby"]  # but kept, so the round trip is exact
    assert record.to_payload() == payload
    assert decode_transaction(encode_transaction(record)).to_payload() == payload


def test_a_pump_with_a_baby_id_only_still_decodes() -> None:
    payload = pump_without_baby(rid(1), babyID=BABY_ID)
    record = decode_payload(payload)
    assert type(record) is Pump
    assert record.to_payload() == payload


# --- no baby is derived from a pump ---------------------------------------------------------------


def test_babies_are_not_derived_from_a_pump_that_carries_one() -> None:
    engine = SyncEngine()
    engine.apply(PHONE, [RawTransaction(1, OP_ADD, encode_b64(payload_for("Pump", rid(1))))])
    assert rid(1) in engine.records
    assert engine.babies() == {}


def test_an_account_with_only_pumps_has_no_babies() -> None:
    engine = SyncEngine()
    engine.apply(PHONE, [raw(f) for f in PUMP_FIXTURES])
    assert len(engine.records) == 2
    assert engine.babies() == {}


def test_babies_still_come_from_other_records_when_pumps_are_present() -> None:
    engine = SyncEngine()
    engine.apply(
        PHONE,
        [
            RawTransaction(1, OP_ADD, encode_b64(payload_for("Diaper", rid(1)))),
            RawTransaction(
                2,
                OP_ADD,
                encode_b64(payload_for("Pump", rid(2), baby=baby_payload("Ghost", BABY_TWO_ID))),
            ),
        ],
    )
    assert set(engine.babies()) == {BABY_ID}


def test_latest_records_for_a_baby_never_include_a_pump() -> None:
    engine = SyncEngine()
    engine.apply(
        PHONE,
        [
            RawTransaction(1, OP_ADD, encode_b64(payload_for("Diaper", rid(1)))),
            RawTransaction(2, OP_ADD, encode_b64(payload_for("Pump", rid(2)))),  # carries BABY_ID
            RawTransaction(3, OP_ADD, encode_b64(pump_without_baby(rid(3)))),
        ],
    )
    assert set(engine.latest_records(BABY_ID)) == {"Diaper"}


def test_nothing_assumes_every_record_has_a_baby() -> None:
    engine = SyncEngine()
    engine.apply(PHONE, [raw(f) for f in PUMP_FIXTURES])
    assert engine.babies() == {}
    assert engine.latest_records(BABY_ID) == {}
    assert engine.own_writes() == []
    # The snapshot round trip works with babyless records too.
    restored = SyncEngine.from_dict(json.loads(json.dumps(engine.to_dict())))
    assert {i: r.to_payload() for i, r in restored.records.items()} == {
        i: r.to_payload() for i, r in engine.records.items()
    }


def test_the_cli_summary_leaves_pumps_out_of_every_babys_section() -> None:
    engine = SyncEngine()
    engine.apply(
        PHONE,
        [
            RawTransaction(1, OP_ADD, encode_b64(payload_for("Diaper", rid(1)))),
            RawTransaction(2, OP_ADD, encode_b64(pump_without_baby(rid(2)))),
        ],
    )
    text = "\n".join(format_latest(engine))
    assert "Diaper" in text
    assert "Pump" not in text


# --- latest_pump: the newest pump on the account, whoever logged it ------------------------------


def test_latest_pump_is_none_when_there_is_no_pump() -> None:
    engine = SyncEngine()
    engine.apply(PHONE, [RawTransaction(1, OP_ADD, encode_b64(payload_for("Diaper", rid(1))))])
    assert engine.latest_pump() is None


def test_latest_pump_is_the_newest_by_event_time_across_devices() -> None:
    engine = SyncEngine()
    engine.apply(
        PHONE,
        [
            RawTransaction(
                1,
                OP_ADD,
                encode_b64(pump_without_baby(rid(1), time="2026-10-25 10:00:00 +0000")),
            ),
            RawTransaction(
                2,
                OP_ADD,
                encode_b64(pump_without_baby(rid(2), time="2026-10-25 12:00:00 +0000")),
            ),
        ],
    )
    engine.apply(
        OTHER_PHONE,
        [
            RawTransaction(
                1,
                OP_ADD,
                encode_b64(pump_without_baby(rid(3), time="2026-10-25 11:00:00 +0000")),
            ),
        ],
    )
    latest = engine.latest_pump()
    assert latest is not None
    assert latest.object_id == rid(2)


def test_latest_pump_ignores_a_deleted_pump_and_other_record_types() -> None:
    engine = SyncEngine()
    newest = pump_without_baby(rid(2), time="2026-10-25 12:00:00 +0000")
    engine.apply(
        PHONE,
        [
            RawTransaction(
                1, OP_ADD, encode_b64(pump_without_baby(rid(1), time="2026-10-25 10:00:00 +0000"))
            ),
            RawTransaction(2, OP_ADD, encode_b64(newest)),
            RawTransaction(
                3,
                OP_ADD,
                encode_b64(payload_for("Pumped", rid(3), time="2026-10-25 13:00:00 +0000")),
            ),
            RawTransaction(4, OP_DELETE, encode_b64({**newest, "newFlage": "false"})),
        ],
    )
    latest = engine.latest_pump()
    assert latest is not None
    assert latest.object_id == rid(1)


# --- writing --------------------------------------------------------------------------------------


async def test_add_pump_takes_no_baby(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    assert "baby_id" not in inspect.signature(SyncEngine.add_pump).parameters
    with pytest.raises(TypeError):
        await engine.add_pump(client, BABY_ID, left_volume=Volume(4, "oz"))  # type: ignore[call-arg,arg-type]
    assert own_posts(fake_server) == []


async def test_add_pump_with_a_total_posts_exactly_the_real_pump_keys(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)

    record = await engine.add_pump(client, amount=Volume(4, "oz"), left_minutes=5, right_minutes=7)

    payload = decode(nth_post(fake_server, 0))
    assert set(payload) == REAL_PUMP_KEYS
    assert "baby" not in payload
    assert "leftAmount" not in payload
    assert "rightAmount" not in payload
    assert payload["amount"] == {
        "BCObjectType": "VolumeMeasure",
        "englishMeasure": "true",
        "value": 4,
    }
    assert (payload["leftDuration"], payload["rightDuration"]) == (5, 7)
    assert payload["sides"] == 0
    assert payload["newFlage"] == "true"
    assert payload["pictureLoaded"] == "true"
    assert payload["pictureNote"] == []
    assert isinstance(record, Pump)
    assert record.baby is None
    assert record.volume == Volume(4, "oz")
    assert engine.records[record.object_id].to_payload() == payload


async def test_add_pump_with_per_side_amounts_adds_only_the_two_side_keys(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)

    await engine.add_pump(
        client, left_volume=Volume(2, "oz"), right_volume=Volume(5, "oz"), finish_side=SIDE_LEFT
    )

    payload = decode(nth_post(fake_server, 0))
    assert set(payload) == PER_SIDE_KEYS
    assert "baby" not in payload
    assert payload["amount"]["value"] == 7
    assert payload["leftAmount"]["value"] == 2
    assert payload["rightAmount"]["value"] == 5


async def test_add_pump_matches_the_total_only_fixture_apart_from_what_a_write_fills_in(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    from test_write_types import at

    seed(fake_server)
    engine = await synced_engine(client)

    await engine.add_pump(
        client,
        amount=Volume(4, "oz"),
        left_minutes=5,
        right_minutes=7,
        finish_side=SIDE_RIGHT,
        time=at(TOTAL_ONLY["payload"]["time"]),
        timezone_offset=-18000,
    )

    payload = decode(nth_post(fake_server, 0))
    expected = {
        k: v for k, v in TOTAL_ONLY["payload"].items() if k not in ("objectID", "timestamp", "note")
    }
    assert {
        k: v for k, v in payload.items() if k not in ("objectID", "timestamp", "note")
    } == expected


async def test_add_pump_works_before_any_baby_is_known(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.register_devices = False
    engine = await synced_engine(client)
    assert engine.babies() == {}

    record = await engine.add_pump(client, amount=Volume(3, "oz"))

    assert isinstance(record, Pump)
    assert len(own_posts(fake_server)) == 1


async def test_a_written_pump_is_an_own_write_and_creates_no_baby(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)

    record = await engine.add_pump(client, amount=Volume(3, "oz"))

    assert engine.own_writes() == [(record.object_id, 1)]
    assert record.object_id in engine.created_ids
    assert set(engine.babies()) == {BABY_ID}
    assert engine.latest_pump() == record
    assert set(engine.latest_records(BABY_ID)) == {"Diaper"}


async def test_own_writes_orders_a_pump_among_babied_writes(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)

    first = await add(engine, client)
    pump = await engine.add_pump(client, amount=Volume(3, "oz"))
    last = await add(engine, client)

    assert engine.own_writes() == [(first.object_id, 1), (pump.object_id, 2), (last.object_id, 3)]


async def test_a_written_pump_can_be_deleted_and_the_delete_has_no_baby(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    record = await engine.add_pump(client, amount=Volume(3, "oz"))

    await engine.delete_record(client, record.object_id)

    payload = decode(nth_post(fake_server, 1))
    assert nth_post(fake_server, 1)["OPCode"] == OP_DELETE
    assert "baby" not in payload
    assert payload["newFlage"] == "false"
    assert record.object_id not in engine.records
    assert engine.latest_pump() is None


async def test_a_pump_logged_by_another_device_is_the_latest_pump(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    fake_server.add_transaction(
        PHONE, OP_ADD, pump_without_baby(rid(7), time="2030-01-01 00:00:00 +0000")
    )
    engine = await synced_engine(client)

    mine = await engine.add_pump(client, amount=Volume(3, "oz"))

    latest = engine.latest_pump()
    assert latest is not None
    assert latest.object_id == rid(7)
    assert latest.object_id != mine.object_id


async def test_a_pump_survives_a_state_snapshot(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    record = await engine.add_pump(client, amount=Volume(3, "oz"))

    restored = SyncEngine.from_dict(json.loads(json.dumps(engine.to_dict())))

    assert restored.records[record.object_id].to_payload() == record.to_payload()
    assert restored.own_writes() == engine.own_writes()
    assert restored.babies().keys() == {BABY_ID}


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"left_minutes": 5},
        {"amount": Volume(0, "oz")},
        {"amount": Volume(4, "oz"), "left_volume": Volume(2, "oz")},
        {"amount": Volume(4, "oz"), "right_volume": Volume(2, "oz")},
        {"amount": Volume(-1, "oz")},
        {"amount": Volume(60, "ml")},
        {"amount": 4},
    ],
    ids=[
        "nothing",
        "minutes only",
        "zero total",
        "total with left",
        "total with right",
        "negative total",
        "milliliters",
        "plain number",
    ],
)
async def test_a_bad_pump_is_refused_with_no_request(
    kwargs: dict[str, Any], client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    fake_server.requests.clear()

    with pytest.raises(ValueError):
        await engine.add_pump(client, **kwargs)

    assert fake_server.requests == []


async def test_decoding_what_add_pump_posted_gives_back_the_same_bytes(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced_engine(client)
    await engine.add_pump(client, amount=Volume(4, "oz"), left_minutes=3)

    transaction = nth_post(fake_server, 0)["Transaction"]

    record = decode_transaction(transaction)
    assert not isinstance(record, UnknownRecord)
    assert encode_transaction(record) == transaction
