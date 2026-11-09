"""The account's seed: reading its database, and merging it under the logs."""

from __future__ import annotations

import io
import sqlite3
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from conftest import PROBE_UUID
from factories import encode_b64
from fake_server import FakeSyncServer

from pybabytracker import (
    OP_ADD,
    OP_DELETE,
    OP_UPDATE,
    AccountChangedError,
    AuthenticationError,
    Baby,
    BabyTrackerClient,
    BabyTrackerConnectionError,
    Bath,
    Device,
    Diaper,
    Formula,
    Growth,
    Medication,
    Milestone,
    Nursing,
    OtherActivity,
    OtherFeed,
    ProtocolError,
    Pump,
    RawTransaction,
    Record,
    Seed,
    Sleep,
    SyncEngine,
    Temperature,
    Vaccine,
    read_seed,
)
from pybabytracker import client as client_module
from pybabytracker import seed as seed_module

SCHEMA = (Path(__file__).parent / "fixtures" / "seed_schema.sql").read_text(encoding="utf-8")

BABY = "11111111-AAAA-4AAA-8AAA-000000000001"
GONE_BABY = "99999999-AAAA-4AAA-8AAA-000000000009"
T0 = datetime(2025, 6, 1, 12, 0, tzinfo=UTC).timestamp()
DEVICE = "aaaaaaaa-0000-4000-8000-000000000001"


def rid(n: int) -> str:
    return f"CCCCCCCC-0000-4000-8000-{n:012d}"


def connection_for(
    tables: dict[str, list[dict[str, Any]]], replaced: dict[str, str] | None = None
) -> sqlite3.Connection:
    """The app's schema with the given rows; ``replaced`` swaps in other table definitions."""
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA)
    for table, definition in (replaced or {}).items():
        connection.execute(f'DROP TABLE "{table}"')
        connection.execute(definition)
    for table, rows in tables.items():
        for given in rows:
            # A column given as None is left out: NULL, or not in that table at all.
            row = {column: value for column, value in given.items() if value is not None}
            columns = ", ".join(f'"{c}"' for c in row)
            marks = ", ".join("?" for _ in row)
            sql = f'INSERT INTO "{table}" ({columns}) VALUES ({marks})'
            connection.execute(sql, list(row.values()))
    connection.commit()
    return connection


def database(**tables: list[dict[str, Any]]) -> bytes:
    """A seed database with the app's schema and the given rows."""
    connection = connection_for(tables)
    data = connection.serialize()
    connection.close()
    return data


def archive(data: bytes, name: str = "EasyLog.db") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr(name, data)
    return buffer.getvalue()


def baby_row(**overrides: Any) -> dict[str, Any]:
    row = {"ID": BABY, "Timestamp": T0, "Name": "Ada", "DOB": T0 - 86400 * 90}
    return row | {"DueDay": T0 - 86400 * 80, "Gender": 1, "Picture": "PHOTO-1"} | overrides


def activity(n: int, minutes: float = 0, **columns: Any) -> dict[str, Any]:
    row = {"ID": rid(n), "Timestamp": T0 + minutes * 60 + 5, "Time": T0 + minutes * 60}
    return row | {"Note": f"note {n}", "HasPicture": 0, "BabyID": BABY} | columns


def seed_zip(**tables: list[dict[str, Any]]) -> bytes:
    tables.setdefault("Baby", [baby_row()])
    return archive(database(**tables))


def seeded(**tables: list[dict[str, Any]]) -> Seed:
    return read_seed(seed_zip(**tables))


def only[R](seed: Seed, kind: type[R]) -> R:
    found = [r for r in seed.records if type(r) is kind]
    assert len(found) == 1, found
    return found[0]


# --- reading the database ----------------------------------------------------------------------


def test_a_seed_record_reads_like_the_synced_record() -> None:
    seed = seeded(Diaper=[activity(1, Status=2, Amount=3, PeeColor=5, PooColor=1, Texture=5)])

    diaper = only(seed, Diaper)
    assert diaper.object_id == rid(1)
    assert diaper.time == datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    assert diaper.timestamp == datetime(2025, 6, 1, 12, 0, 5, tzinfo=UTC)
    assert (diaper.status, diaper.amount, diaper.poo_color, diaper.note) == (2, 3, 1, "note 1")
    assert diaper.baby_id == BABY
    assert diaper.baby is not None and diaper.baby.name == "Ada"


def test_the_baby_is_a_record_and_embedded_in_each_of_its_records() -> None:
    seed = seeded(Sleep=[activity(1, Duration=45)])

    baby = only(seed, Baby)
    assert (baby.name, baby.gender, baby.picture_file) == ("Ada", "true", "PHOTO-1.jpg")
    assert baby.dob == "2025-03-03 12:00:00 +0000"
    embedded = only(seed, Sleep).baby
    assert embedded is not None
    assert (embedded.object_id, embedded.name, embedded.dob) == (BABY, "Ada", baby.dob)


def test_a_date_of_zero_is_unset() -> None:
    seed = seeded(Baby=[baby_row(DueDay=0.0)])

    baby = only(seed, Baby)
    assert baby.due_day is None
    assert "dueDay" not in baby.to_payload()


def test_a_nursing_keeps_its_sides_and_minutes() -> None:
    sides = {"FinishSide": 2, "LeftDuration": 7, "RightDuration": 3, "BothDuration": 1}
    seed = seeded(Nursing=[activity(1, **sides)])

    nursing = only(seed, Nursing)
    assert (nursing.finish_side, nursing.left_duration, nursing.right_duration) == (2, 7, 3)
    assert nursing.both_duration == 1
    assert nursing.desc_id is None


@pytest.mark.parametrize(("english", "unit"), [(1, "oz"), (0, "ml")])
def test_a_bottle_amount_becomes_a_volume_measure(english: int, unit: str) -> None:
    seed = seeded(Formula=[activity(1, Amount=4.5, IsEnglishScale=english)])

    volume = only(seed, Formula).volume
    assert volume is not None
    assert (volume.value, volume.unit) == (4.5, unit)


def test_a_pump_keeps_both_sides_and_only_a_real_label() -> None:
    pump_columns = {"FinishSide": 1, "Sides": 0, "Amount": 7, "IsEnglishScale": 1, "LeftAmount": 3}
    pump_columns |= {"RightAmount": 4, "LeftDuration": 10, "RightDuration": 12}
    seed = seeded(
        Pump=[
            activity(1, BabyID=None, Label="", **pump_columns),
            activity(2, 5, BabyID=None, Label="Freezer", **pump_columns),
        ]
    )

    pumps = sorted((r for r in seed.records if isinstance(r, Pump)), key=lambda p: p.object_id)
    assert [p.label for p in pumps] == [None, "Freezer"]
    first = pumps[0]
    assert (first.finish_side, first.left_duration, first.right_duration) == (1, 10, 12)
    assert first.left_volume is not None and first.left_volume.value == 3
    assert first.baby is None


def test_a_medicine_dose_embeds_its_medicine() -> None:
    medicine = {"ID": "MED-1", "Timestamp": T0, "Name": "Drops", "Description": ""}
    medicine |= {"IsPrescription": 0, "AmountPerTime": 0.5, "Unit": "ml", "Interval": 240}
    seed = seeded(MedicineSelection=[medicine], Medicine=[activity(1, MedID="MED-1", Amount=0.5)])

    dose = only(seed, Medication)
    assert (dose.medicine_name, dose.amount) == ("Drops", 0.5)
    assert dose.medication_selection is not None
    assert dose.medication_selection["interval"] == 240


def test_pick_list_entries_are_embedded_but_are_not_records() -> None:
    """A seed entry as a record of its own would beat a newer copy a log's record embeds."""
    milestone = {"ID": "EasyLogSeedM1", "Timestamp": 0, "Name": "Rolls over", "Description": ""}
    vaccine = {"ID": "VACC-1", "Timestamp": T0, "Name": "Custom shot", "Description": ""}
    medicine = {"ID": "MED-1", "Timestamp": T0, "Name": "Drops", "Description": ""}
    seed = seeded(
        MilestoneSelection=[milestone | {"ForAge": 4}],
        VaccineSelection=[vaccine | {"ForAge": 0}],
        MedicineSelection=[medicine],
        Milestone=[activity(1, MilestoneSelectionID="EasyLogSeedM1")],
        Vaccine=[activity(2, VaccID="VACC-1", DocVisitID="NotUsed")],
    )

    assert only(seed, Milestone).milestone_name == "Rolls over"
    assert only(seed, Vaccine).vaccine_name == "Custom shot"
    assert {type(r) for r in seed.records} == {Baby, Milestone, Vaccine}


def test_a_custom_milestone_keeps_the_baby_it_is_for() -> None:
    milestone = {"ID": "MS-1", "Timestamp": T0, "Name": "First swim", "Description": ""}
    seed = seeded(
        MilestoneSelection=[milestone | {"ForAge": 0, "BabyID": BABY}],
        Milestone=[activity(1, MilestoneSelectionID="MS-1")],
    )

    kind = only(seed, Milestone).milestone_type
    assert kind is not None and kind["babyID"] == BABY


def test_an_entry_missing_from_the_seed_keeps_its_id_without_a_nameless_copy() -> None:
    seed = seeded(
        Medicine=[activity(1, MedID="GONE-MED", Amount=1)],
        Milestone=[activity(2, MilestoneSelectionID="GONE-MS")],
    )

    dose = only(seed, Medication)
    assert dose.medication_selection is None
    assert dose.extra["medID"] == "GONE-MED"
    milestone = only(seed, Milestone)
    assert milestone.milestone_type is None
    assert milestone.milestone_selection_id == "GONE-MS"


def test_a_food_and_an_activity_name_their_pick_list_entry() -> None:
    food = {"ID": "FOOD-1", "Timestamp": T0, "Name": "Pear", "Description": "", "IsBottle": 0}
    kind = {"ID": "ACT-1", "Timestamp": T0, "Name": "Bath", "Description": ""}
    feed_columns: dict[str, Any] = {"TypeID": "FOOD-1", "Amount": 2, "IsEnglishScale": 1}
    feed_columns["Unit"] = "Tbsp"
    seed = seeded(
        OtherFeedSelection=[food],
        OtherActivityDesc=[kind],
        OtherFeed=[activity(1, DescID="DESC-1", **feed_columns)],
        OtherActivity=[activity(2, DescID="ACT-1", Duration=15, LocationID="LOC-1")],
    )

    feed = only(seed, OtherFeed)
    assert (feed.food_name, feed.quantity, feed.unit) == ("Pear", 2, "Tbsp")
    assert feed.desc is not None and feed.desc["objectID"] == "DESC-1"
    assert feed.desc["BCObjectType"] == "FeedDescription"
    other = only(seed, OtherActivity)
    assert (other.activity_name, other.duration, other.location_id) == ("Bath", 15, "LOC-1")


def test_a_food_without_an_amount_has_none() -> None:
    seed = seeded(OtherFeed=[activity(1, TypeID="X", Amount=0, IsEnglishScale=1, Unit="")])
    assert only(seed, OtherFeed).amount is None


def test_plain_id_references_are_kept() -> None:
    seed = seeded(
        Sleep=[activity(1, Duration=30, DescID="SD-1", LocationID="SL-1")],
        Formula=[activity(2, Amount=3, IsEnglishScale=1, DescID="FD-1")],
        Bath=[activity(3, DescID="BD-1")],
    )

    sleep = only(seed, Sleep)
    assert (sleep.desc_id, sleep.location_id) == ("SD-1", "SL-1")
    assert only(seed, Formula).desc_id == "FD-1"
    assert only(seed, Bath).desc_id == "BD-1"


def test_a_growth_has_only_the_measures_taken() -> None:
    seed = seeded(
        Growth=[
            activity(
                1, Weight=10.5, Length=0, Head=14, IsEnglishWeightScale=1, IsEnglishLengthScale=0
            )
        ]
    )

    growth = only(seed, Growth)
    assert growth.weight_reading is not None and growth.weight_reading.value == 10.5
    assert growth.weight_reading.unit == "lb"
    assert growth.length_reading is None
    assert growth.head_reading is not None and growth.head_reading.unit == "cm"


@pytest.mark.parametrize(("temp", "english", "expected"), [(37.5, 0, (37.5, "C")), (0, 1, None)])
def test_a_temperature_keeps_its_scale_and_zero_is_unset(
    temp: float, english: int, expected: tuple[float, str] | None
) -> None:
    seed = seeded(Temperature=[activity(1, Temp=temp, IsEnglishScale=english)])

    reading = only(seed, Temperature).reading
    assert (None if reading is None else (reading.value, reading.unit)) == expected


def test_a_record_lists_its_pictures() -> None:
    picture = {"ID": "PIC-1", "Timestamp": T0, "ActivityID": rid(1), "Type": 0}
    picture |= {"FileName": "FILE-1", "Thumbnail": b"\xff" * 64}
    seed = seeded(Milestone=[activity(1, MilestoneSelectionID="X")], Picture=[picture])

    assert [p.file_name for p in only(seed, Milestone).pictures] == ["FILE-1"]


def test_a_record_of_a_baby_not_in_the_seed_is_left_out() -> None:
    seed = seeded(Diaper=[activity(1, Status=0), activity(2, BabyID=GONE_BABY, Status=0)])

    assert [r.object_id for r in seed.records if isinstance(r, Diaper)] == [rid(1)]


def test_an_empty_seed_has_only_its_babies() -> None:
    assert [type(r) for r in seeded().records] == [Baby]


def test_an_older_schema_without_later_columns_keeps_its_rows() -> None:
    """Columns the app added later fall back to their defaults instead of dropping rows."""
    old_tables = {
        "Pump": 'CREATE TABLE "Pump" ("ID" TEXT PRIMARY KEY, "Timestamp" REAL, "Time" REAL, '
        '"Note" TEXT, "HasPicture" INTEGER, "FinishSide" INTEGER, "Sides" INTEGER, '
        '"Amount" REAL, "IsEnglishScale" INTEGER, "Label" TEXT, "PumpedID" TEXT)',
        "Diaper": 'CREATE TABLE "Diaper" ("ID" TEXT PRIMARY KEY, "Timestamp" REAL, "Time" REAL, '
        '"Note" TEXT, "HasPicture" INTEGER, "BabyID" TEXT, "Status" INTEGER)',
    }
    pump: dict[str, Any] = {"FinishSide": 2, "Sides": 0, "Amount": 5, "IsEnglishScale": 1}
    rows = {"Baby": [baby_row()], "Pump": [activity(1, BabyID=None, **pump)]}
    rows["Diaper"] = [activity(2, Status=1)]
    with closing(connection_for(rows, old_tables)) as connection:
        seed = read_seed(archive(connection.serialize()))

    old_pump = only(seed, Pump)
    assert (old_pump.finish_side, old_pump.left_duration) == (2, 0)
    assert old_pump.volume is not None and old_pump.volume.value == 5
    diaper = only(seed, Diaper)
    assert (diaper.status, diaper.amount, diaper.pee_color) == (1, 5, 5)


@pytest.mark.parametrize(
    "data",
    [
        b"not a zip",
        archive(database(), name="Other.db"),
        archive(b"not a database"),
    ],
)
def test_a_seed_that_is_not_one_raises(data: bytes) -> None:
    with pytest.raises(ProtocolError):
        read_seed(data)


def test_a_damaged_zip_stream_raises_protocol_error() -> None:
    good = seed_zip(Diaper=[activity(1, Status=0)])
    # Damage the compressed bytes, after the local header, before the central directory.
    damaged = bytearray(good)
    for i in range(60, 120):
        damaged[i] ^= 0xFF
    with pytest.raises(ProtocolError):
        read_seed(bytes(damaged))


def test_a_corrupt_database_page_raises_protocol_error() -> None:
    data = bytearray(
        database(Baby=[baby_row()], Diaper=[activity(n, Status=0) for n in range(300)])
    )
    for i in range(len(data) // 2, len(data) // 2 + 4096):
        data[i] = 0xAB
    with pytest.raises(ProtocolError):
        read_seed(archive(bytes(data)))


def test_a_database_saved_in_wal_mode_reads() -> None:
    data = bytearray(database(Baby=[baby_row()], Diaper=[activity(1, Status=0)]))
    data[18] = data[19] = 2  # the header bytes a WAL-mode database has

    assert only(read_seed(archive(bytes(data))), Diaper).object_id == rid(1)


def test_a_column_the_schema_computes_is_never_read() -> None:
    """A generated column could compute anything, so only stored columns are read."""
    hostile = (
        'CREATE TABLE "Diaper" ("ID" TEXT PRIMARY KEY, "Timestamp" REAL, "Time" REAL, '
        '"HasPicture" INTEGER, "BabyID" TEXT, "Status" INTEGER, '
        '"Note" BLOB GENERATED ALWAYS AS (zeroblob(50000000)) VIRTUAL)'
    )
    rows = {"Baby": [baby_row()], "Diaper": [activity(1, Status=0, Note=None)]}
    with closing(connection_for(rows, {"Diaper": hostile})) as connection:
        data = connection.serialize()

    assert only(read_seed(archive(data)), Diaper).note == ""


def test_a_stored_value_over_the_limit_raises_protocol_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(seed_module, "_MAX_VALUE_BYTES", 1000)
    data = database(Baby=[baby_row()], Diaper=[activity(1, Status=0, Note="x" * 2000)])
    with pytest.raises(ProtocolError):
        read_seed(archive(data))


def test_a_database_over_the_cap_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seed_module, "MAX_SEED_DATABASE_BYTES", 1000)
    with pytest.raises(ProtocolError):
        read_seed(seed_zip())


# --- merging under the logs --------------------------------------------------------------------


def tx(sync_id: int, op: int, payload: dict[str, Any]) -> RawTransaction:
    return RawTransaction(sync_id, op, encode_b64(payload))


def payload(record: Record, **changes: Any) -> dict[str, Any]:
    return record.to_payload() | changes


class SeedSource:
    """Device logs and a seed, held in memory."""

    def __init__(self, seed: bytes | None, logs: dict[str, list[RawTransaction]] | None = None):
        self.seed = seed
        self.logs = logs or {}
        self.seed_reads = 0
        self.fetches: list[tuple[str, int]] = []
        self.fail: Exception | None = None

    async def get_seed(self) -> bytes | None:
        self.seed_reads += 1
        if self.fail is not None:
            raise self.fail
        return self.seed

    async def get_devices(self) -> list[Device]:
        return [
            Device(uuid, max((t.sync_id for t in log), default=0), uuid[:4], "test")
            for uuid, log in self.logs.items()
        ]

    async def get_transactions(self, device_uuid: str, since_sync_id: int) -> list[RawTransaction]:
        self.fetches.append((device_uuid, since_sync_id))
        return [t for t in self.logs[device_uuid] if t.sync_id > since_sync_id]


class LogsOnly:
    """A source that can't fetch a seed."""

    async def get_devices(self) -> list[Device]:
        return []

    async def get_transactions(self, device_uuid: str, since_sync_id: int) -> list[RawTransaction]:
        return []


async def test_the_first_sync_reads_the_seed_once() -> None:
    source = SeedSource(seed_zip(Nursing=[activity(1, LeftDuration=7, FinishSide=1)]))
    engine = SyncEngine()

    first = await engine.sync(source)
    second = await engine.sync(source)

    assert source.seed_reads == 1
    assert (first.seed_read, second.seed_read) == (True, False)
    assert isinstance(engine.records[rid(1)], Nursing)
    assert isinstance(engine.latest_records(BABY)["Nursing"], Nursing)


async def test_a_newer_edit_in_a_log_wins_over_the_seed() -> None:
    seeded_sleep = only(seeded(Sleep=[activity(1, Duration=30)]), Sleep)
    edited = payload(seeded_sleep, duration=40, timestamp="2025-06-02 00:00:00 +0000")
    log = {DEVICE: [tx(1, OP_UPDATE, edited)]}
    engine = SyncEngine()

    await engine.sync(SeedSource(seed_zip(Sleep=[activity(1, Duration=30)]), log))

    record = engine.records[rid(1)]
    assert isinstance(record, Sleep) and record.duration == 40


async def test_the_same_record_in_a_log_wins_even_with_the_seeds_timestamp() -> None:
    seeded_sleep = only(seeded(Sleep=[activity(1, Duration=30)]), Sleep)
    log = {DEVICE: [tx(1, OP_ADD, payload(seeded_sleep, duration=31))]}
    engine = SyncEngine()

    await engine.sync(SeedSource(seed_zip(Sleep=[activity(1, Duration=30)]), log))

    record = engine.records[rid(1)]
    assert isinstance(record, Sleep) and record.duration == 31


async def test_an_older_log_copy_loses_to_the_seed_as_between_devices() -> None:
    seeded_sleep = only(seeded(Sleep=[activity(1, Duration=30)]), Sleep)
    older = payload(seeded_sleep, duration=20, timestamp="2025-05-01 00:00:00 +0000")
    engine = SyncEngine()

    await engine.sync(
        SeedSource(seed_zip(Sleep=[activity(1, Duration=30)]), {DEVICE: [tx(1, OP_ADD, older)]})
    )

    record = engine.records[rid(1)]
    assert isinstance(record, Sleep) and record.duration == 30


@pytest.mark.parametrize("seed_first", [True, False])
async def test_a_delete_in_a_log_removes_a_seed_record_in_either_order(seed_first: bool) -> None:
    seeded_diaper = only(seeded(Diaper=[activity(1, Status=0)]), Diaper)
    log = {DEVICE: [tx(1, OP_DELETE, payload(seeded_diaper))]}
    engine = SyncEngine()
    if seed_first:
        await engine.sync(SeedSource(seed_zip(Diaper=[activity(1, Status=0)]), log))
    else:
        await engine.sync(SeedSource(None, log))
        state = engine.to_dict()
        state["seeded"] = False  # an install from before seeds were read
        engine = SyncEngine.from_dict(state)
        await engine.sync(SeedSource(seed_zip(Diaper=[activity(1, Status=0)]), log))

    assert rid(1) not in engine.records
    assert engine.is_deleted(rid(1))


async def test_every_log_is_read_from_the_start_whatever_the_seed_says() -> None:
    """The app empties the seed's list of merged logs when it loads a seed and reads every log
    from the start, so this device's own records and any delete are always seen."""
    diaper = only(seeded(Diaper=[activity(1, Status=0)]), Diaper)
    source = SeedSource(
        seed_zip(MergedTransaction=[{"DeviceID": DEVICE, "SyncID": 5}]),
        {DEVICE: [tx(1, OP_ADD, payload(diaper)), tx(2, OP_DELETE, payload(diaper))]},
    )
    engine = SyncEngine(device_uuid=DEVICE)

    await engine.sync(source)

    assert source.fetches == [(DEVICE, 0)]
    assert engine.is_deleted(rid(1))
    assert engine.last_seen[DEVICE] == 2


async def test_this_devices_own_adds_are_still_its_own_after_a_seed() -> None:
    diaper = only(seeded(Diaper=[activity(1, Status=0)]), Diaper)
    source = SeedSource(
        seed_zip(
            Diaper=[activity(1, Status=0)], MergedTransaction=[{"DeviceID": DEVICE, "SyncID": 1}]
        ),
        {DEVICE: [tx(1, OP_ADD, payload(diaper))]},
    )
    engine = SyncEngine(device_uuid=DEVICE)

    await engine.sync(source)

    assert rid(1) in engine.created_ids
    assert [object_id for object_id, _ in engine.own_writes()] == [rid(1)]


async def test_state_saved_before_seeds_reads_the_seed_on_the_next_sync() -> None:
    engine = SyncEngine()
    await engine.sync(SeedSource(None))
    state = engine.to_dict()
    del state["seeded"]

    loaded = SyncEngine.from_dict(state)
    source = SeedSource(seed_zip(Diaper=[activity(1, Status=0)]))
    await loaded.sync(source)

    assert source.seed_reads == 1
    assert rid(1) in loaded.records


async def test_a_loaded_seed_survives_a_save_and_load() -> None:
    engine = SyncEngine()
    await engine.sync(SeedSource(seed_zip(Diaper=[activity(1, Status=0)])))

    loaded = SyncEngine.from_dict(engine.to_dict())
    source = SeedSource(seed_zip())
    await loaded.sync(source)

    assert source.seed_reads == 0
    assert rid(1) in loaded.records


async def test_an_unreadable_seed_is_warned_about_once_and_the_logs_still_sync(
    caplog: pytest.LogCaptureFixture,
) -> None:
    diaper = only(seeded(Diaper=[activity(1, Status=0)]), Diaper)
    source = SeedSource(b"not a zip", {DEVICE: [tx(1, OP_ADD, payload(diaper))]})
    engine = SyncEngine()

    await engine.sync(source)
    await engine.sync(source)

    assert rid(1) in engine.records
    assert source.seed_reads == 1
    assert "older history is missing" in caplog.text


@pytest.mark.parametrize(
    "failure",
    [BabyTrackerConnectionError("down"), ProtocolError("the service answered with HTTP 429")],
)
async def test_a_failed_download_lets_the_logs_sync_and_is_tried_again(
    failure: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    diaper = only(seeded(Diaper=[activity(1, Status=0)]), Diaper)
    source = SeedSource(
        seed_zip(Sleep=[activity(2, Duration=30)]), {DEVICE: [tx(1, OP_ADD, payload(diaper))]}
    )
    source.fail = failure
    engine = SyncEngine()

    result = await engine.sync(source)
    assert rid(1) in engine.records
    assert engine.to_dict()["seeded"] is False
    assert result.seed_read is False
    await engine.sync(source)
    source.fail = None
    await engine.sync(source)

    assert source.seed_reads == 3
    assert rid(2) in engine.records
    assert caplog.text.count("Couldn't download the account's seed") == 1


async def test_a_rejected_sign_in_on_the_seed_fails_the_sync() -> None:
    source = SeedSource(seed_zip())
    source.fail = AuthenticationError("rejected")

    with pytest.raises(AuthenticationError):
        await SyncEngine().sync(source)


async def test_a_source_without_a_seed_syncs_and_reads_one_later() -> None:
    engine = SyncEngine()
    await engine.sync(LogsOnly())
    assert engine.to_dict()["seeded"] is False

    source = SeedSource(seed_zip(Diaper=[activity(1, Status=0)]))
    await engine.sync(source)
    assert rid(1) in engine.records


async def test_an_account_with_no_seed_is_not_asked_again() -> None:
    source = SeedSource(None)
    engine = SyncEngine()

    assert (await engine.sync(source)).seed_read is True
    await engine.sync(source)

    assert source.seed_reads == 1


# --- downloading it ----------------------------------------------------------------------------


async def test_the_client_downloads_the_seed(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.seed = seed_zip()
    assert await client.get_seed() == fake_server.seed


@pytest.mark.parametrize("status", [400, 404])
async def test_no_seed_is_none(
    client: BabyTrackerClient, fake_server: FakeSyncServer, status: int
) -> None:
    fake_server.missing_seed_status = status
    assert await client.get_seed() is None


async def test_an_empty_seed_is_none(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.seed = b""
    assert await client.get_seed() is None


async def test_a_redirected_seed_raises(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.seed_redirect = "/elsewhere/seed.zip"
    with pytest.raises(ProtocolError):
        await client.get_seed()


async def test_a_seed_past_the_size_cap_raises(
    client: BabyTrackerClient, fake_server: FakeSyncServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "MAX_SEED_BYTES", 1000)
    fake_server.seed = b"x" * 1001
    with pytest.raises(ProtocolError):
        await client.get_seed()


async def test_a_seed_has_longer_than_other_requests(
    client: BabyTrackerClient, fake_server: FakeSyncServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_server.seed = seed_zip()
    seen: list[Any] = []
    original = client_module.BabyTrackerClient._exchange

    async def spy(self: BabyTrackerClient, *args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("limits"))
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(client_module.BabyTrackerClient, "_exchange", spy)
    await client.get_seed()

    assert seen[-1] is not None and seen[-1].total == client_module.SEED_TIMEOUT


async def test_a_first_sync_through_the_client_loads_the_seed(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.seed = seed_zip(Nursing=[activity(1, LeftDuration=7, FinishSide=1)])
    engine = SyncEngine()

    await engine.sync(client)

    assert isinstance(engine.records[rid(1)], Nursing)


async def test_a_failed_seed_download_through_the_client_still_syncs_the_logs(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.fail_paths["/account/seed"] = 429
    engine = SyncEngine()

    await engine.sync(client)

    assert engine.to_dict()["seeded"] is False


# --- a different sync group --------------------------------------------------------------------


async def synced_before_seeds(client: BabyTrackerClient, fake_server: FakeSyncServer) -> SyncEngine:
    """State synced from group 424242, saved before seeds were read."""
    fake_server.add_device(DEVICE)
    fake_server.add_transaction(
        DEVICE, OP_ADD, payload(only(seeded(Diaper=[activity(1, Status=0)]), Diaper))
    )
    engine = SyncEngine()
    await engine.sync(client)
    state = engine.to_dict()
    state["seeded"] = False
    return SyncEngine.from_dict(state)


async def test_a_reset_during_the_seed_download_merges_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await synced_before_seeds(client, fake_server)
    fake_server.seed = seed_zip(Sleep=[activity(2, Duration=30)])
    fake_server.add_transaction(
        DEVICE, OP_ADD, payload(only(seeded(Diaper=[activity(3, Status=1)]), Diaper))
    )
    fake_server.reset_on_request = ("GET", "/account/seed", 777)

    with pytest.raises(AccountChangedError):
        await engine.sync(client)

    assert rid(1) in engine.records
    assert rid(2) not in engine.records
    assert rid(3) not in engine.records
    assert engine.to_dict()["seeded"] is False
    assert engine.account_id == 424242


async def test_a_seed_from_a_different_group_is_not_merged(
    client: BabyTrackerClient, fake_server: FakeSyncServer, session: aiohttp.ClientSession
) -> None:
    engine = await synced_before_seeds(client, fake_server)
    fake_server.account_id = 777
    fake_server.sessions.clear()
    fake_server.seed = seed_zip(Sleep=[activity(2, Duration=30)])
    fresh = BabyTrackerClient(
        fake_server.email, fake_server.password, PROBE_UUID, "Test Device",
        session=session, base_url=fake_server.base_url,
    )  # fmt: skip

    with pytest.raises(AccountChangedError):
        await engine.sync(fresh)

    assert rid(1) in engine.records
    assert rid(2) not in engine.records
    assert rid(3) not in engine.records
    assert engine.to_dict()["seeded"] is False
    assert engine.account_id == 424242


async def test_the_first_seed_read_adopts_the_groups_account_id(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.seed = seed_zip(Sleep=[activity(2, Duration=30)])
    engine = SyncEngine()

    await engine.sync(client)

    assert rid(2) in engine.records
    assert engine.account_id == 424242


async def test_a_group_change_on_the_seed_download_fails_the_sync(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = SeedSource(seed_zip(Sleep=[activity(2, Duration=30)]))
    source.fail = AccountChangedError(424242, 777)
    engine = SyncEngine()

    with pytest.raises(AccountChangedError):
        await engine.sync(source)

    assert engine.to_dict()["seeded"] is False
    assert "Couldn't download the account's seed" not in caplog.text
