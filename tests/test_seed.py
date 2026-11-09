"""The account's seed: reading its database, and merging it under the logs."""

from __future__ import annotations

import io
import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from factories import encode_b64
from fake_server import FakeSyncServer

from pybabytracker import (
    OP_ADD,
    OP_DELETE,
    OP_UPDATE,
    Baby,
    BabyTrackerClient,
    BabyTrackerConnectionError,
    Bath,
    Device,
    Diaper,
    Formula,
    Growth,
    Medication,
    MedicationSelection,
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
OLD_DEVICE = "bbbbbbbb-0000-4000-8000-000000000002"


def rid(n: int) -> str:
    return f"CCCCCCCC-0000-4000-8000-{n:012d}"


def database(**tables: list[dict[str, Any]]) -> bytes:
    """A seed database with the app's schema and the given rows."""
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA)
    for table, rows in tables.items():
        for given in rows:
            # A column given as None is left out: NULL, or not in that table at all.
            row = {column: value for column, value in given.items() if value is not None}
            columns = ", ".join(f'"{c}"' for c in row)
            marks = ", ".join("?" for _ in row)
            connection.execute(
                f'INSERT INTO "{table}" ({columns}) VALUES ({marks})', list(row.values())
            )
    connection.commit()
    data = connection.serialize()
    connection.close()
    return data


def archive(data: bytes, name: str = "EasyLog.db") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr(name, data)
    return buffer.getvalue()


def baby_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "ID": BABY,
        "Timestamp": T0,
        "Name": "Ada",
        "DOB": T0 - 86400 * 90,
        "DueDay": T0 - 86400 * 80,
    }
    return row | {"Gender": 1, "Picture": "PHOTO-1"} | overrides


def activity(n: int, minutes: float = 0, **columns: Any) -> dict[str, Any]:
    row = {"ID": rid(n), "Timestamp": T0 + minutes * 60 + 5, "Time": T0 + minutes * 60}
    return row | {"Note": f"note {n}", "HasPicture": 0, "BabyID": BABY} | columns


def seeded(**tables: list[dict[str, Any]]) -> Seed:
    tables.setdefault("Baby", [baby_row()])
    return read_seed(archive(database(**tables)))


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
    assert only(seed, Sleep).baby == Baby.from_payload(
        {k: v for k, v in baby.to_payload().items() if k != "timestamp"}
        | {"timestamp": "1970-01-01 00:00:00 +0000"}
    )


def test_a_nursing_keeps_its_sides_and_minutes() -> None:
    seed = seeded(
        Nursing=[activity(1, FinishSide=2, LeftDuration=7, RightDuration=3, BothDuration=1)]
    )

    nursing = only(seed, Nursing)
    assert (nursing.finish_side, nursing.left_duration, nursing.right_duration) == (2, 7, 3)
    assert nursing.both_duration == 1


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
    assert only(seed, MedicationSelection).name == "Drops"


def test_built_in_pick_list_entries_are_embedded_but_are_not_records() -> None:
    milestone = {"ID": "EasyLogSeedM1", "Timestamp": 0, "Name": "Rolls over", "Description": ""}
    vaccine = {"ID": "EasyLogSeedV1", "Timestamp": 0, "Name": "HepB", "Description": ""}
    seed = seeded(
        MilestoneSelection=[milestone | {"ForAge": 4}],
        VaccineSelection=[vaccine | {"ForAge": 0}],
        Milestone=[activity(1, MilestoneSelectionID="EasyLogSeedM1")],
        Vaccine=[activity(2, VaccID="EasyLogSeedV1", DocVisitID="NotUsed")],
    )

    assert only(seed, Milestone).milestone_name == "Rolls over"
    assert only(seed, Vaccine).vaccine_name == "HepB"
    assert not [r for r in seed.records if r.object_id.startswith("EasyLogSeed")]


def test_a_food_an_activity_and_a_bath_name_their_pick_list_entry() -> None:
    food = {"ID": "FOOD-1", "Timestamp": T0, "Name": "Pear", "Description": "", "IsBottle": 0}
    kind = {"ID": "ACT-1", "Timestamp": T0, "Name": "Tummy time", "Description": ""}
    bath = {"ID": "EasyLogSeedB1", "Timestamp": 0, "Name": "Sponge bath", "Description": ""}
    seed = seeded(
        OtherFeedSelection=[food],
        OtherActivityDesc=[kind],
        BathDesc=[bath],
        OtherFeed=[activity(1, TypeID="FOOD-1", Amount=2, IsEnglishScale=1, Unit="Tbsp")],
        OtherActivity=[activity(2, DescID="ACT-1", Duration=15)],
        Bath=[activity(3, DescID="EasyLogSeedB1")],
    )

    feed = only(seed, OtherFeed)
    assert (feed.food_name, feed.quantity, feed.unit) == ("Pear", 2, "Tbsp")
    assert only(seed, OtherActivity).activity_name == "Tummy time"
    assert only(seed, OtherActivity).duration == 15
    bath_record = only(seed, Bath)
    assert bath_record.extra["bathType"]["name"] == "Sponge bath"


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


def test_a_temperature_keeps_its_scale() -> None:
    seed = seeded(Temperature=[activity(1, Temp=37.5, IsEnglishScale=0)])

    reading = only(seed, Temperature).reading
    assert reading is not None
    assert (reading.value, reading.unit) == (37.5, "C")


def test_a_record_lists_its_pictures() -> None:
    picture = {
        "ID": "PIC-1",
        "Timestamp": T0,
        "ActivityID": rid(1),
        "Type": 0,
        "FileName": "FILE-1",
    }
    seed = seeded(Milestone=[activity(1, MilestoneSelectionID="X")], Picture=[picture])

    assert [p.file_name for p in only(seed, Milestone).pictures] == ["FILE-1"]


def test_a_record_of_a_baby_not_in_the_seed_is_left_out() -> None:
    seed = seeded(Diaper=[activity(1, Status=0), activity(2, BabyID=GONE_BABY, Status=0)])

    assert [r.object_id for r in seed.records if isinstance(r, Diaper)] == [rid(1)]


def test_the_merged_positions_are_where_each_log_is_read_from() -> None:
    seed = seeded(
        MergedTransaction=[
            {"DeviceID": OLD_DEVICE, "SyncID": 44},
            {"DeviceID": DEVICE, "SyncID": 3},
            {"DeviceID": "", "SyncID": 9},
        ]
    )

    assert seed.merged == {OLD_DEVICE: 44, DEVICE: 3}


def test_an_empty_seed_has_only_its_babies() -> None:
    seed = seeded()
    assert [type(r) for r in seed.records] == [Baby]
    assert seed.merged == {}


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


def test_a_database_over_the_cap_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seed_module, "MAX_SEED_DATABASE_BYTES", 1000)
    with pytest.raises(ProtocolError):
        read_seed(archive(database()))


def test_a_row_missing_a_column_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    data = database(Baby=[baby_row()], Sleep=[activity(1, Duration=5)])
    connection = sqlite3.connect(":memory:")
    connection.deserialize(data)
    connection.execute('ALTER TABLE "Sleep" DROP COLUMN "Duration"')
    connection.commit()
    seed = read_seed(archive(connection.serialize()))

    assert not [r for r in seed.records if isinstance(r, Sleep)]
    assert "Skipping seed row" in caplog.text


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


def seed_zip(**tables: list[dict[str, Any]]) -> bytes:
    tables.setdefault("Baby", [baby_row()])
    return archive(database(**tables))


async def test_the_first_sync_reads_the_seed_once() -> None:
    source = SeedSource(seed_zip(Nursing=[activity(1, LeftDuration=7, FinishSide=1)]))
    engine = SyncEngine()

    await engine.sync(source)
    await engine.sync(source)

    assert source.seed_reads == 1
    assert isinstance(engine.records[rid(1)], Nursing)
    assert isinstance(engine.latest_records(BABY)["Nursing"], Nursing)


async def test_an_edit_in_a_log_wins_over_the_seed() -> None:
    seeded_sleep = only(seeded(Sleep=[activity(1, Duration=30)]), Sleep)
    edited = payload(seeded_sleep, duration=40, timestamp="2025-06-02 00:00:00 +0000")
    source = SeedSource(
        seed_zip(Sleep=[activity(1, Duration=30)]), {DEVICE: [tx(1, OP_UPDATE, edited)]}
    )
    engine = SyncEngine()

    await engine.sync(source)

    record = engine.records[rid(1)]
    assert isinstance(record, Sleep) and record.duration == 40


async def test_the_same_record_in_a_log_wins_even_with_the_seeds_timestamp() -> None:
    seeded_sleep = only(seeded(Sleep=[activity(1, Duration=30)]), Sleep)
    source = SeedSource(
        seed_zip(Sleep=[activity(1, Duration=30)]),
        {DEVICE: [tx(1, OP_ADD, payload(seeded_sleep, duration=31))]},
    )
    engine = SyncEngine()

    await engine.sync(source)

    record = engine.records[rid(1)]
    assert isinstance(record, Sleep) and record.duration == 31


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
        del state["seeded"]  # an install from before seeds were read
        engine = SyncEngine.from_dict(state)
        await engine.sync(SeedSource(seed_zip(Diaper=[activity(1, Status=0)]), log))

    assert rid(1) not in engine.records
    assert engine.is_deleted(rid(1))


async def test_a_log_is_read_from_where_the_seeds_copy_of_it_ends() -> None:
    diaper = only(seeded(Diaper=[activity(1, Status=0)]), Diaper)
    newer = payload(diaper, objectID=rid(2))
    source = SeedSource(
        seed_zip(MergedTransaction=[{"DeviceID": DEVICE, "SyncID": 1}]),
        {DEVICE: [tx(1, OP_ADD, payload(diaper)), tx(2, OP_ADD, newer)]},
    )
    engine = SyncEngine()

    await engine.sync(source)

    assert source.fetches == [(DEVICE, 1)]
    assert rid(2) in engine.records
    assert rid(1) not in engine.records


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


async def test_an_unreadable_seed_is_warned_about_and_the_logs_still_sync(
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


async def test_a_connection_failure_on_the_seed_fails_the_sync_and_is_tried_again() -> None:
    source = SeedSource(seed_zip(Diaper=[activity(1, Status=0)]))
    source.fail = BabyTrackerConnectionError("down")
    engine = SyncEngine()

    with pytest.raises(BabyTrackerConnectionError):
        await engine.sync(source)
    source.fail = None
    await engine.sync(source)

    assert source.seed_reads == 2
    assert rid(1) in engine.records


async def test_a_source_without_a_seed_syncs_as_before() -> None:
    engine = SyncEngine()
    await engine.sync(SeedSource(None))
    assert engine.to_dict()["seeded"] is True


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


async def test_a_first_sync_through_the_client_loads_the_seed(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.seed = seed_zip(Nursing=[activity(1, LeftDuration=7, FinishSide=1)])
    engine = SyncEngine()

    await engine.sync(client)

    assert isinstance(engine.records[rid(1)], Nursing)
