"""``is_deleted`` and ``deleted_ids``: which records are deleted, without encoding any state."""

from __future__ import annotations

import json
from typing import Any

import pytest
from factories import encode_b64, payload_for

from pybabytracker import OP_ADD, OP_DELETE, OP_UPDATE, RawTransaction, SyncEngine

PHONE = "AAAA0000-0000-4000-8000-000000000001"
TABLET = "BBBB0000-0000-4000-8000-000000000002"
ONE = "AAAAAAAA-0000-4000-8000-000000000001"
TWO = "AAAAAAAA-0000-4000-8000-000000000002"
BABY = "BBBBBBBB-0000-4000-8000-000000000001"

T1 = "2026-10-26 12:00:01 +0000"
T2 = "2026-10-26 12:00:02 +0000"
T3 = "2026-10-26 12:00:03 +0000"


def tx(sync_id: int, op: int, payload: Any) -> RawTransaction:
    return RawTransaction(sync_id=sync_id, op_code=op, transaction=encode_b64(payload))


def diaper(object_id: str, stamp: str) -> dict[str, Any]:
    return payload_for("Diaper", object_id, timestamp=stamp)


def engine_with(*transactions: RawTransaction, device: str = PHONE) -> SyncEngine:
    engine = SyncEngine()
    engine.apply(device, transactions)
    return engine


def test_a_record_added_then_deleted_is_deleted() -> None:
    engine = engine_with(tx(1, OP_ADD, diaper(ONE, T1)), tx(2, OP_DELETE, diaper(ONE, T2)))
    assert engine.is_deleted(ONE) is True
    assert engine.deleted_ids() == frozenset({ONE})


def test_a_record_added_again_after_its_delete_is_not_deleted() -> None:
    engine = engine_with(
        tx(1, OP_ADD, diaper(ONE, T1)),
        tx(2, OP_DELETE, diaper(ONE, T2)),
        tx(3, OP_ADD, diaper(ONE, T3)),
    )
    assert ONE in engine.records
    assert engine.is_deleted(ONE) is False
    assert engine.deleted_ids() == frozenset()


def test_a_resurrection_from_another_device_is_not_deleted() -> None:
    engine = SyncEngine()
    engine.apply(PHONE, [tx(1, OP_ADD, diaper(ONE, T1)), tx(2, OP_DELETE, diaper(ONE, T2))])
    engine.apply(TABLET, [tx(1, OP_ADD, diaper(ONE, T3))])
    assert engine.is_deleted(ONE) is False


def test_an_add_older_than_the_delete_from_another_device_stays_deleted() -> None:
    engine = SyncEngine()
    engine.apply(PHONE, [tx(1, OP_DELETE, diaper(ONE, T2))])
    engine.apply(TABLET, [tx(1, OP_ADD, diaper(ONE, T1))])
    assert engine.is_deleted(ONE) is True
    assert ONE not in engine.records


def test_a_delete_that_arrives_before_its_add_is_deleted() -> None:
    engine = SyncEngine()
    engine.apply(PHONE, [tx(1, OP_DELETE, diaper(ONE, T2))])
    assert engine.is_deleted(ONE) is True


def test_a_record_never_seen_is_not_deleted() -> None:
    engine = engine_with(tx(1, OP_ADD, diaper(ONE, T1)))
    assert engine.is_deleted(TWO) is False
    assert engine.is_deleted("") is False
    assert SyncEngine().is_deleted(ONE) is False
    assert SyncEngine().deleted_ids() == frozenset()


def test_a_live_record_is_not_deleted() -> None:
    engine = engine_with(tx(1, OP_ADD, diaper(ONE, T1)), tx(2, OP_UPDATE, diaper(ONE, T2)))
    assert engine.is_deleted(ONE) is False


def test_deleted_ids_lists_every_deleted_record_and_no_live_one() -> None:
    engine = engine_with(
        tx(1, OP_ADD, diaper(ONE, T1)),
        tx(2, OP_ADD, diaper(TWO, T1)),
        tx(3, OP_ADD, payload_for("Baby", BABY)),
        tx(4, OP_DELETE, diaper(ONE, T2)),
        tx(5, OP_DELETE, payload_for("Baby", BABY)),
    )
    assert engine.deleted_ids() == frozenset({ONE, BABY})
    assert engine.is_deleted(BABY) is True
    assert engine.is_deleted(TWO) is False


def test_a_deleted_baby_is_reported_deleted() -> None:
    engine = engine_with(
        tx(1, OP_ADD, payload_for("Baby", BABY, timestamp=T1)),
        tx(2, OP_DELETE, payload_for("Baby", BABY, timestamp=T2)),
    )
    assert BABY not in engine.babies()
    assert engine.is_deleted(BABY) is True


def test_deleted_ids_is_a_snapshot_not_a_live_view() -> None:
    engine = engine_with(tx(1, OP_DELETE, diaper(ONE, T2)))
    snapshot = engine.deleted_ids()
    engine.apply(TABLET, [tx(1, OP_ADD, diaper(ONE, T3))])
    assert snapshot == frozenset({ONE})
    assert engine.deleted_ids() == frozenset()


def test_it_survives_save_and_load() -> None:
    engine = engine_with(
        tx(1, OP_ADD, diaper(ONE, T1)),
        tx(2, OP_DELETE, diaper(ONE, T2)),
        tx(3, OP_ADD, diaper(TWO, T1)),
    )
    restored = SyncEngine.from_dict(json.loads(json.dumps(engine.to_dict())))
    assert restored.is_deleted(ONE) is True
    assert restored.is_deleted(TWO) is False
    assert restored.deleted_ids() == frozenset({ONE})


def test_it_does_not_encode_state(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = engine_with(tx(1, OP_ADD, diaper(ONE, T1)), tx(2, OP_DELETE, diaper(ONE, T2)))

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("encoded state")

    monkeypatch.setattr(SyncEngine, "to_dict", boom)
    for record in engine.records.values():
        monkeypatch.setattr(type(record), "to_payload", boom)
    assert engine.is_deleted(ONE) is True
    assert engine.deleted_ids() == frozenset({ONE})
