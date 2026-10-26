from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from factories import ACTIVITY_TYPES, BABY_ID, BABY_TWO_ID, baby_payload, encode_b64, payload_for

from pybabytracker import (
    OP_ADD,
    OP_DELETE,
    OP_UPDATE,
    Baby,
    Device,
    Diaper,
    RawTransaction,
    SyncEngine,
    UnknownRecord,
)

PHONE = "AAAA1111-0000-4000-8000-00000000000A"
TABLET = "BBBB2222-0000-4000-8000-00000000000B"
LAPTOP = "CCCC3333-0000-4000-8000-00000000000C"


def rid(n: int) -> str:
    return f"AAAAAAAA-0000-4000-8000-{n:012d}"


def tx(sync_id: int, op: int, payload: Any) -> RawTransaction:
    return RawTransaction(sync_id=sync_id, op_code=op, transaction=encode_b64(payload))


class MemorySource:
    """A sync source with no HTTP: device logs held in memory."""

    def __init__(self, logs: dict[str, list[RawTransaction]]) -> None:
        self.logs = logs
        self.fetches: list[tuple[str, int]] = []
        self.reverse_listing = False
        self.fail_on: str | None = None
        self.device_listings = 0

    async def get_devices(self) -> list[Device]:
        self.device_listings += 1
        devices = [
            Device(
                device_uuid=uuid,
                last_sync_id=max((t.sync_id for t in log), default=0),
                device_name=uuid[:4],
                device_os_info="test",
            )
            for uuid, log in self.logs.items()
        ]
        return list(reversed(devices)) if self.reverse_listing else devices

    async def get_transactions(self, device_uuid: str, since_sync_id: int) -> list[RawTransaction]:
        self.fetches.append((device_uuid, since_sync_id))
        if device_uuid == self.fail_on:
            raise RuntimeError("simulated failure")
        return [t for t in self.logs[device_uuid] if t.sync_id > since_sync_id]


def test_a_new_engine_is_empty() -> None:
    engine = SyncEngine()
    assert dict(engine.records) == {}
    assert dict(engine.last_seen) == {}
    assert engine.babies() == {}


def test_add_update_and_delete_change_the_current_state() -> None:
    engine = SyncEngine()
    engine.apply(PHONE, [tx(1, OP_ADD, payload_for("Diaper", rid(1), status=0))])
    first = engine.records[rid(1)]
    assert isinstance(first, Diaper)
    assert first.status == 0

    engine.apply(PHONE, [tx(2, OP_UPDATE, payload_for("Diaper", rid(1), status=1))])
    updated = engine.records[rid(1)]
    assert isinstance(updated, Diaper)
    assert updated.status == 1
    assert len(engine.records) == 1

    engine.apply(PHONE, [tx(3, OP_DELETE, payload_for("Diaper", rid(1), status=1))])
    assert rid(1) not in engine.records


def test_three_devices_with_adds_and_deletes_merge_to_exactly_the_survivors() -> None:
    engine = SyncEngine()
    engine.apply(
        PHONE,
        [
            tx(1, OP_ADD, payload_for("Diaper", rid(1))),
            tx(2, OP_ADD, payload_for("Sleep", rid(2))),
            tx(3, OP_ADD, payload_for("Nursing", rid(3))),
        ],
    )
    engine.apply(
        TABLET,
        [
            tx(1, OP_ADD, payload_for("Formula", rid(4))),
            tx(2, OP_DELETE, payload_for("Diaper", rid(1))),  # deletes the phone's record
            tx(3, OP_ADD, payload_for("Pump", rid(5))),
        ],
    )
    engine.apply(
        LAPTOP,
        [
            tx(1, OP_ADD, payload_for("Growth", rid(6))),
            tx(2, OP_DELETE, payload_for("Pump", rid(5))),
            tx(3, OP_DELETE, payload_for("Growth", rid(6))),
            tx(4, OP_ADD, payload_for("Growth", rid(7))),
        ],
    )
    assert set(engine.records) == {rid(2), rid(3), rid(4), rid(7)}
    assert dict(engine.last_seen) == {PHONE: 3, TABLET: 3, LAPTOP: 4}


def test_a_delete_for_a_record_never_seen_is_a_no_op() -> None:
    engine = SyncEngine()
    engine.apply(PHONE, [tx(1, OP_ADD, payload_for("Diaper", rid(1)))])
    engine.apply(TABLET, [tx(1, OP_DELETE, payload_for("Diaper", rid(99)))])
    assert set(engine.records) == {rid(1)}
    assert engine.last_seen[TABLET] == 1


def test_an_add_after_a_delete_for_the_same_id_brings_the_record_back() -> None:
    engine = SyncEngine()
    engine.apply(
        PHONE,
        [
            tx(1, OP_ADD, payload_for("Diaper", rid(1), status=0)),
            tx(2, OP_DELETE, payload_for("Diaper", rid(1), status=0)),
            tx(3, OP_ADD, payload_for("Diaper", rid(1), status=1)),
        ],
    )
    record = engine.records[rid(1)]
    assert isinstance(record, Diaper)
    assert record.status == 1


def test_an_update_with_an_older_timestamp_still_applies_in_log_order() -> None:
    # Documented choice: the log order is the truth, not the record's own timestamp field.
    engine = SyncEngine()
    engine.apply(
        PHONE,
        [
            tx(
                1,
                OP_ADD,
                payload_for("Diaper", rid(1), status=0, timestamp="2026-10-26 12:00:00 +0000"),
            ),
            tx(
                2,
                OP_UPDATE,
                payload_for("Diaper", rid(1), status=1, timestamp="2026-10-26 08:00:00 +0000"),
            ),
        ],
    )
    record = engine.records[rid(1)]
    assert isinstance(record, Diaper)
    assert record.status == 1


async def test_two_devices_editing_one_record_resolve_the_same_way_every_time() -> None:
    logs = {
        PHONE: [tx(1, OP_ADD, payload_for("Diaper", rid(1), status=0, note="phone"))],
        TABLET: [tx(1, OP_UPDATE, payload_for("Diaper", rid(1), status=1, note="tablet"))],
    }
    results = []
    for reverse in (False, True):
        source = MemorySource(logs)
        source.reverse_listing = reverse
        engine = SyncEngine()
        await engine.sync(source)
        results.append(engine.records[rid(1)].note)
    # Devices apply in device-ID order, so the tablet (higher ID) wins either way.
    assert results == ["tablet", "tablet"]


def test_malformed_transactions_are_skipped_and_logged_without_failing_the_sync(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = SyncEngine()
    missing_id = payload_for("Diaper", rid(2))
    del missing_id["objectID"]
    transactions = [
        tx(1, OP_ADD, payload_for("Diaper", rid(1))),
        RawTransaction(2, OP_ADD, "!!! not base64 !!!"),
        RawTransaction(3, OP_ADD, encode_b64("just a string")),
        RawTransaction(4, OP_ADD, "aGVsbG8gdGhlcmU="),  # base64 of non-JSON text
        tx(5, OP_ADD, [1, 2, 3]),
        tx(6, OP_ADD, missing_id),
        tx(7, OP_ADD, payload_for("Diaper", rid(7))),
    ]
    with caplog.at_level(logging.WARNING, logger="pybabytracker"):
        result = engine.apply(PHONE, transactions)
    assert set(engine.records) == {rid(1), rid(7)}
    assert engine.last_seen[PHONE] == 7
    assert result.applied == 2
    assert result.skipped == 5
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 5


def test_an_unknown_record_type_is_kept_raw_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = SyncEngine()
    payload = {"BCObjectType": "Teleporter", "objectID": rid(1), "mass": 12}
    with caplog.at_level(logging.WARNING, logger="pybabytracker"):
        engine.apply(PHONE, [tx(1, OP_ADD, payload), tx(2, OP_ADD, payload_for("Diaper", rid(2)))])
    record = engine.records[rid(1)]
    assert isinstance(record, UnknownRecord)
    assert record.to_payload() == payload
    assert any("Teleporter" in r.getMessage() for r in caplog.records)
    assert rid(2) in engine.records
    # It survives a save and load, and can still be deleted.
    reloaded = SyncEngine.from_dict(engine.to_dict())
    assert reloaded.records[rid(1)].to_payload() == payload
    reloaded.apply(PHONE, [tx(3, OP_DELETE, payload)])
    assert rid(1) not in reloaded.records


def test_an_unknown_opcode_is_skipped_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    engine = SyncEngine()
    with caplog.at_level(logging.WARNING, logger="pybabytracker"):
        engine.apply(PHONE, [tx(1, 7, payload_for("Diaper", rid(1)))])
    assert rid(1) not in engine.records
    assert engine.last_seen[PHONE] == 1
    assert caplog.records


def test_last_seen_tracks_the_highest_number_per_device_whatever_the_delivery_order() -> None:
    engine = SyncEngine()
    engine.apply(
        PHONE,
        [
            tx(3, OP_ADD, payload_for("Diaper", rid(3))),
            tx(1, OP_ADD, payload_for("Diaper", rid(1))),
            tx(2, OP_DELETE, payload_for("Diaper", rid(1))),
        ],
    )
    # Applied in number order: add 1, delete 1, add 3.
    assert set(engine.records) == {rid(3)}
    assert engine.last_seen[PHONE] == 3


def test_transactions_at_or_below_the_last_seen_number_are_not_applied_twice() -> None:
    engine = SyncEngine()
    engine.apply(PHONE, [tx(1, OP_ADD, payload_for("Diaper", rid(1), note="first"))])
    engine.apply(PHONE, [tx(1, OP_ADD, payload_for("Diaper", rid(1), note="replayed"))])
    assert engine.records[rid(1)].note == "first"


def test_an_empty_log_page_changes_nothing() -> None:
    engine = SyncEngine()
    engine.apply(PHONE, [tx(1, OP_ADD, payload_for("Diaper", rid(1)))])
    before = engine.to_dict()
    result = engine.apply(PHONE, [])
    assert result.applied == 0
    assert engine.to_dict() == before
    assert set(engine.records) == {rid(1)}
    assert dict(engine.last_seen) == {PHONE: 1}


def test_only_devices_ahead_of_what_was_seen_are_fetched() -> None:
    engine = SyncEngine()
    engine.apply(
        PHONE,
        [
            tx(1, OP_ADD, payload_for("Diaper", rid(1))),
            tx(2, OP_ADD, payload_for("Diaper", rid(2))),
        ],
    )

    def dev(uuid: str, last: int) -> Device:
        return Device(device_uuid=uuid, last_sync_id=last, device_name="x", device_os_info="y")

    ahead = engine.devices_to_fetch([dev(PHONE, 2), dev(TABLET, 5), dev(LAPTOP, 0)])
    assert [d.device_uuid for d in ahead] == [TABLET]
    assert [d.device_uuid for d in engine.devices_to_fetch([dev(PHONE, 3)])] == [PHONE]
    assert engine.devices_to_fetch([]) == []


async def test_sync_with_no_devices_does_nothing() -> None:
    source = MemorySource({})
    result = await SyncEngine().sync(source)
    assert source.device_listings == 1
    assert source.fetches == []
    assert result.devices_fetched == 0


async def test_sync_skips_devices_with_last_sync_id_zero() -> None:
    source = MemorySource({PHONE: []})
    result = await SyncEngine().sync(source)
    assert source.device_listings == 1
    assert source.fetches == []
    assert result.devices_fetched == 0


async def test_sync_with_an_empty_page_for_a_device_that_claims_to_be_ahead() -> None:
    class LyingSource(MemorySource):
        async def get_devices(self) -> list[Device]:
            return [Device(PHONE, 5, "p", "o")]

    source = LyingSource({PHONE: []})
    engine = SyncEngine()
    result = await engine.sync(source)
    assert source.fetches == [(PHONE, 0)]
    assert result.transactions_applied == 0
    assert dict(engine.last_seen) == {}


async def test_a_saved_state_fetches_only_what_is_newer() -> None:
    logs = {
        PHONE: [tx(i, OP_ADD, payload_for("Diaper", rid(i))) for i in (1, 2, 3)],
        TABLET: [tx(1, OP_ADD, payload_for("Sleep", rid(10)))],
    }
    first_source = MemorySource(logs)
    engine = SyncEngine()
    await engine.sync(first_source)
    assert sorted(first_source.fetches) == [(PHONE, 0), (TABLET, 0)]

    saved = json.loads(json.dumps(engine.to_dict()))
    logs[PHONE].append(tx(4, OP_ADD, payload_for("Diaper", rid(4))))

    second_source = MemorySource(logs)
    restored = SyncEngine.from_dict(saved)
    result = await restored.sync(second_source)

    assert second_source.fetches == [(PHONE, 3)]
    assert result.transactions_applied == 1
    assert set(restored.records) == {rid(1), rid(2), rid(3), rid(4), rid(10)}


async def test_a_failing_device_keeps_the_progress_of_the_ones_before_it() -> None:
    logs = {
        PHONE: [tx(1, OP_ADD, payload_for("Diaper", rid(1)))],
        TABLET: [tx(1, OP_ADD, payload_for("Diaper", rid(2)))],
    }
    source = MemorySource(logs)
    source.fail_on = TABLET
    engine = SyncEngine()
    with pytest.raises(RuntimeError):
        await engine.sync(source)
    assert dict(engine.last_seen) == {PHONE: 1}
    source.fail_on = None
    await engine.sync(source)
    assert set(engine.records) == {rid(1), rid(2)}


def test_save_and_load_gives_identical_state() -> None:
    engine = SyncEngine()
    engine.apply(
        PHONE,
        [
            tx(1, OP_ADD, payload_for("Diaper", rid(1), note="Légère fuite 💧")),
            tx(2, OP_ADD, {"BCObjectType": "Teleporter", "objectID": rid(2), "n": [1, None]}),
            tx(3, OP_ADD, payload_for("Sleep", rid(3), futureField={"a": 1})),
        ],
    )
    engine.apply(TABLET, [tx(1, OP_ADD, payload_for(ACTIVITY_TYPES[0], rid(4)))])

    saved = engine.to_dict()
    text = json.dumps(saved)  # must be JSON-safe
    restored = SyncEngine.from_dict(json.loads(text))

    assert dict(restored.records) == dict(engine.records)
    assert dict(restored.last_seen) == dict(engine.last_seen)
    assert restored.to_dict() == saved
    assert len(restored.records) == 4
    assert saved["last_seen"] == {PHONE: 3, TABLET: 1}


def test_loading_an_unsupported_version_fails_clearly() -> None:
    with pytest.raises(ValueError, match="version"):
        SyncEngine.from_dict({"version": 999, "last_seen": {}, "records": []})


def test_babies_come_from_embedded_objects_and_baby_records() -> None:
    engine = SyncEngine()
    engine.apply(
        PHONE,
        [
            tx(1, OP_ADD, payload_for("Diaper", rid(1))),
            tx(
                2,
                OP_ADD,
                payload_for("Diaper", rid(2), baby=baby_payload("Other Baby", BABY_TWO_ID)),
            ),
        ],
    )
    babies = engine.babies()
    assert {b.name for b in babies.values()} == {"Test Baby", "Other Baby"}
    # A Baby record in the log overrides the embedded copy.
    engine.apply(TABLET, [tx(1, OP_UPDATE, baby_payload("Renamed Baby", BABY_ID))])
    assert engine.babies()[BABY_ID].name == "Renamed Baby"
    assert all(isinstance(b, Baby) for b in engine.babies().values())


def test_latest_records_picks_the_newest_of_each_type_for_a_baby() -> None:
    engine = SyncEngine()
    engine.apply(
        PHONE,
        [
            tx(1, OP_ADD, payload_for("Diaper", rid(1), time="2026-10-25 10:00:00 +0000")),
            tx(2, OP_ADD, payload_for("Diaper", rid(2), time="2026-10-25 12:00:00 +0000")),
            tx(3, OP_ADD, payload_for("Diaper", rid(3), time="2026-10-25 11:00:00 +0000")),
            tx(4, OP_ADD, payload_for("Sleep", rid(4))),
            tx(
                5,
                OP_ADD,
                payload_for("Diaper", rid(5), baby=baby_payload("Other Baby", BABY_TWO_ID)),
            ),
        ],
    )
    latest = engine.latest_records(BABY_ID)
    assert set(latest) == {"Diaper", "Sleep"}
    assert latest["Diaper"].object_id == rid(2)
    assert engine.latest_records(BABY_TWO_ID)["Diaper"].object_id == rid(5)
    assert engine.latest_records("no-such-baby") == {}
