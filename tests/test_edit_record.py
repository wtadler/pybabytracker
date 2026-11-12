"""Editing a saved record's time or note (invented data only)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from conftest import PROBE_UUID
from factories import BABY_ID, payload_for
from fake_server import FakeSyncServer
from selection_helpers import PHONE, posted, rid, seed, synced

from pybabytracker import (
    OP_DELETE,
    OP_UPDATE,
    AccountChangedError,
    BabyTrackerClient,
    Diaper,
    RecordNotFoundError,
    Sleep,
    SyncEngine,
    WrongRecordTypeError,
)

DIAPER = "DB000000-0000-4000-8000-0000000000D1"
NEW_TIME = datetime(2026, 11, 11, 23, 30, 12, tzinfo=UTC)
FOOD = {"BCObjectType": "OtherFeedSelection", "objectID": "F-1", "name": "Avocado"}


def iphone_diaper(**overrides: Any) -> dict[str, Any]:
    """A diaper as an iPhone synced one, with an unknown key."""
    payload = payload_for("Diaper", DIAPER, time="2026-11-12 00:30:12 +0000")
    payload["newFlage"] = "true"
    payload["timezone"] = -21600
    payload["someFutureKey"] = {"kept": True}
    payload.update(overrides)
    return payload


def opcodes(server: FakeSyncServer) -> list[int]:
    return [int(e["OPCode"]) for e in server.posted(PROBE_UUID)]


async def test_moving_a_records_time_changes_only_what_the_iphone_app_changed(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = iphone_diaper()
    seed(fake_server, original)
    engine = await synced(client)

    updated = await engine.edit_record(client, DIAPER, time=NEW_TIME)

    assert opcodes(fake_server) == [OP_UPDATE]
    [payload] = posted(fake_server)
    # As an iPhone's edit of a diaper's time (captured 2026-11-12): the same keys in the same
    # order, with only time, newFlage, and timestamp changed. The zone is left alone.
    assert list(payload) == list(original)
    assert {k for k in payload if payload[k] != original[k]} == {"time", "newFlage", "timestamp"}
    assert payload["time"] == "2026-11-11 23:30:12 +0000"
    assert payload["newFlage"] == "false"
    assert payload["timestamp"] > original["timestamp"]
    assert isinstance(updated, Diaper)
    assert updated.time == NEW_TIME
    assert engine.records[DIAPER] == updated
    assert DIAPER not in engine.created_ids


async def test_a_time_in_another_zone_is_written_in_utc_to_the_second(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_diaper())
    engine = await synced(client)
    central = timezone(timedelta(hours=-6))

    await engine.edit_record(
        client, DIAPER, time=datetime(2026, 11, 11, 17, 30, 12, 999000, tzinfo=central)
    )

    [payload] = posted(fake_server)
    assert payload["time"] == "2026-11-11 23:30:12 +0000"
    assert payload["timezone"] == -21600


async def test_a_note_and_a_zone_can_be_changed(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = iphone_diaper()
    seed(fake_server, original)
    engine = await synced(client)

    updated = await engine.edit_record(client, DIAPER, note="Blowout", timezone_offset=-18000)

    [payload] = posted(fake_server)
    assert {k for k in payload if payload[k] != original[k]} == {
        "note",
        "timezone",
        "newFlage",
        "timestamp",
    }
    assert (payload["note"], payload["timezone"]) == ("Blowout", -18000)
    assert updated.note == "Blowout"


async def test_a_note_can_be_cleared(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_diaper(note="Blowout"))
    engine = await synced(client)

    await engine.edit_record(client, DIAPER, note="")

    [payload] = posted(fake_server)
    assert payload["note"] == ""


async def test_an_android_record_keeps_its_own_values_and_id_case(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    android_id = "52000000-0000-4000-8000-0000000000a2"
    android = payload_for("Diaper", android_id)
    android.update(newFlage=True, pictureLoaded=True, status="0")
    seed(fake_server, android)
    engine = await synced(client)

    await engine.edit_record(client, android_id.upper(), note="Edited")

    [payload] = posted(fake_server)
    assert payload["objectID"] == android_id
    assert payload["newFlage"] is False
    assert (payload["pictureLoaded"], payload["status"]) == (True, "0")


async def test_a_sleeps_start_moves_and_its_length_is_kept(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    sleep = payload_for("Sleep", rid(4), time="2026-11-11 20:00:00 +0000")
    sleep["duration"] = 45
    seed(fake_server, sleep)
    engine = await synced(client)

    updated = await engine.edit_record(
        client, rid(4), time=datetime(2026, 11, 11, 19, 45, tzinfo=UTC)
    )

    assert isinstance(updated, Sleep)
    assert (updated.duration, updated.time) == (45, datetime(2026, 11, 11, 19, 45, tzinfo=UTC))


async def test_a_pump_can_be_edited(client: BabyTrackerClient, fake_server: FakeSyncServer) -> None:
    pump = payload_for("Pump", rid(8))
    pump.pop("baby", None)
    seed(fake_server, pump)
    engine = await synced(client)

    updated = await engine.edit_record(client, rid(8), note="Left only")

    assert updated.note == "Left only"
    assert "baby" not in posted(fake_server)[0]


async def test_an_edit_that_changes_nothing_posts_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_diaper())
    engine = await synced(client)
    first = await engine.edit_record(client, DIAPER, time=NEW_TIME, note="Blowout")
    before = len(fake_server.requests)

    again = await engine.edit_record(client, DIAPER, time=NEW_TIME, note="Blowout")

    assert again == first
    assert len(fake_server.requests) == before
    assert opcodes(fake_server) == [OP_UPDATE]


async def test_the_edit_carries_what_another_device_saved_since_the_last_sync(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_diaper())
    engine = await synced(client)
    newer = iphone_diaper(status=1, newFlage="false", timestamp="2026-10-26 16:00:00 +0000")
    fake_server.add_transaction(PHONE, OP_UPDATE, newer)  # an edit this engine hasn't synced

    await engine.edit_record(client, DIAPER, note="Blowout")

    [payload] = posted(fake_server)
    assert payload["status"] == 1
    assert payload["note"] == "Blowout"


async def test_a_stored_timestamp_ahead_of_this_clock_is_passed_by_a_second(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_diaper(timestamp="2099-01-01 00:00:00 +0000"))
    engine = await synced(client)

    await engine.edit_record(client, DIAPER, note="Blowout")

    [payload] = posted(fake_server)
    assert payload["timestamp"] == "2099-01-01 00:00:01 +0000"


async def test_a_fresh_engine_reading_the_logs_sees_the_edit(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_diaper())
    engine = await synced(client)
    await engine.edit_record(client, DIAPER, time=NEW_TIME, note="Blowout")

    other = SyncEngine()
    await other.sync(client)

    record = other.records[DIAPER]
    assert (record.time, record.note) == (NEW_TIME, "Blowout")


@pytest.mark.parametrize(
    ("object_id", "error"),
    [
        ("NO-SUCH-RECORD", RecordNotFoundError),
        (rid(7), RecordNotFoundError),  # deleted
        (BABY_ID, WrongRecordTypeError),
        ("F-1", WrongRecordTypeError),  # a pick-list entry
        (rid(9), WrongRecordTypeError),  # a type the library doesn't know
    ],
)
async def test_a_record_that_cant_be_edited_is_refused_with_no_request(
    client: BabyTrackerClient,
    fake_server: FakeSyncServer,
    object_id: str,
    error: type[Exception],
) -> None:
    unknown = {"BCObjectType": "Supplement", "objectID": rid(9)}
    seed(fake_server, payload_for("Diaper", rid(7)), FOOD, unknown)
    fake_server.add_transaction(PHONE, OP_DELETE, payload_for("Diaper", rid(7)))
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(error):
        await engine.edit_record(client, object_id, note="Edited")
    assert len(fake_server.requests) == before


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"time": None, "note": None, "timezone_offset": None},
        {"time": datetime(2026, 11, 11, 23, 30)},  # naive
        {"time": "2026-11-11 23:30:12 +0000"},
        {"note": 7},
        {"timezone_offset": True},
        {"timezone_offset": -6.0},
    ],
)
async def test_nothing_to_change_or_a_bad_value_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, changes: dict[str, Any]
) -> None:
    seed(fake_server, iphone_diaper())
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(ValueError):
        await engine.edit_record(client, DIAPER, **changes)
    assert len(fake_server.requests) == before


async def test_an_edit_after_the_group_is_reset_posts_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, iphone_diaper())
    engine = await synced(client)
    fake_server.account_id = 777  # the group was reset in the app
    fake_server.sessions.clear()

    with pytest.raises(AccountChangedError):
        await engine.edit_record(client, DIAPER, note="Edited")
    assert posted(fake_server) == []
