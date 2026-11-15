"""Setting an account up from nothing: a new seed, and a sync group made from it (invented data
only)."""

from __future__ import annotations

import io
import sqlite3
import time
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
import pytest
from conftest import PROBE_UUID
from fake_server import FakeSyncServer

from pybabytracker import (
    BUILT_IN_ACTIVITIES,
    BUILT_IN_FOODS,
    BUILT_IN_MILESTONES,
    BUILT_IN_VACCINES,
    DIAPER_WET,
    AuthenticationError,
    Baby,
    BabyTrackerClient,
    BabyTrackerConnectionError,
    GroupExistsError,
    ProtocolError,
    SyncEngine,
    new_seed,
    read_seed,
)
from pybabytracker import client as client_module

BIRTH = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
SEED = b"PK\x03\x04invented-seed-zip\r\n--not-a-boundary"
BOUNDARY = "__BABY_TRACKER_BOUNDARY____SEED__"

# Every table of a new seed: 13 pick lists, the baby, 20 kinds of record, pictures, a doctor
# visit's medicines, and the Android app's five bookkeeping tables.
TABLES = {
    "Allergen",
    "AllergenSourceSelection",
    "Baby",
    "Bath",
    "BathDesc",
    "Diaper",
    "DoctorSelection",
    "DoctorVisit",
    "DoctorVisitMedicine",
    "FeedDesc",
    "Formula",
    "Growth",
    "HealthQuestion",
    "Journal",
    "Joy",
    "MedicineSelection",
    "Medicine",
    "MergedTransaction",
    "Milestone",
    "MilestoneSelection",
    "Nursing",
    "OtherActivity",
    "OtherActivityDesc",
    "OtherActivityLocationSelection",
    "OtherFeed",
    "OtherFeedSelection",
    "PhotoDownloadList",
    "PhotoList",
    "Picture",
    "Pump",
    "Pumped",
    "ReliveList",
    "Sick",
    "SickDesc",
    "Sleep",
    "SleepDesc",
    "SleepLocationSelection",
    "Temperature",
    "TransactionLog",
    "Vaccine",
    "VaccineSelection",
}


def calls(server: FakeSyncServer) -> list[tuple[str, str]]:
    return [(r.method, r.path) for r in server.requests]


_OPENED: list[sqlite3.Connection] = []


@pytest.fixture(autouse=True)
def close_databases() -> Iterator[None]:
    yield
    while _OPENED:
        _OPENED.pop().close()


def opened(seed: bytes) -> sqlite3.Connection:
    """The seed's database, closed when the test ends."""
    with zipfile.ZipFile(io.BytesIO(seed)) as zipped:
        assert zipped.namelist() == ["EasyLog.db"]
        data = zipped.read("EasyLog.db")
    database = sqlite3.connect(":memory:")
    _OPENED.append(database)
    database.deserialize(data)
    return database


def rows(database: sqlite3.Connection, table: str) -> list[tuple[Any, ...]]:
    return database.execute(f'SELECT * FROM "{table}"').fetchall()


# --- the seed ----------------------------------------------------------------------------


def test_a_new_seed_reads_back_as_its_one_baby() -> None:
    before = time.time()
    seed = new_seed("  Ada ", birth=BIRTH, boy=False, due=BIRTH + timedelta(days=9))

    [baby] = read_seed(seed).records

    assert isinstance(baby, Baby)
    assert (baby.name, baby.gender) == ("Ada", "false")
    assert (baby.dob, baby.due_day) == ("2026-10-01 12:00:00 +0000", "2026-10-10 12:00:00 +0000")
    assert baby.picture_file is None
    assert baby.object_id == baby.object_id.upper() and len(baby.object_id) == 36
    assert baby.timestamp is not None and before - 1 <= baby.timestamp.timestamp() <= time.time()


def test_a_boy_with_no_due_date() -> None:
    seed = new_seed("Ben", birth=BIRTH, boy=True)

    [baby] = read_seed(seed).records
    assert isinstance(baby, Baby)
    assert (baby.gender, baby.due_day) == ("true", None)
    # The app writes 0 for a date it doesn't have.
    assert rows(opened(seed), "Baby")[0][3:6] == (BIRTH.timestamp(), 0.0, 1)


def test_each_new_seed_has_its_own_baby_id() -> None:
    first, second = (read_seed(new_seed("Ada", birth=BIRTH, boy=False)) for _ in range(2))
    assert first.records[0].object_id != second.records[0].object_id


def test_a_new_seed_has_the_apps_tables_and_indexes() -> None:
    database = opened(new_seed("Ada", birth=BIRTH, boy=False))

    tables = {
        name
        for (name,) in database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert tables == TABLES
    indexes = dict(
        database.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type='index' AND sql != ''"
        )
    )
    assert len(indexes) == 58
    # The four the app names without "ID", and one of the rest.
    for table in ("Milestone", "OtherActivity", "Bath", "Diaper"):
        assert indexes[f"{table}IndexBaby"] == table
    assert indexes["SleepIndexBabyID"] == "Sleep" and indexes["PumpIndexTime"] == "Pump"
    assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert database.execute("PRAGMA user_version").fetchone() == (0,)


def test_a_new_seed_has_the_columns_the_apps_add_later() -> None:
    database = opened(new_seed("Ada", birth=BIRTH, boy=False))

    def columns(table: str) -> list[tuple[str, str]]:
        return [(c[1], c[2]) for c in database.execute(f'PRAGMA table_info("{table}")')]

    assert columns("Pump")[-4:] == [
        ("LeftAmount", "REAL"),
        ("RightAmount", "REAL"),
        ("LeftDuration", "INTEGER"),
        ("RightDuration", "INTEGER"),
    ]
    assert ("Unit", "TEXT") in columns("OtherFeed")
    # A pump isn't any one baby's, and the app spells the head's type this way.
    assert "BabyID" not in dict(columns("Pump"))
    assert dict(columns("Growth"))["Head"] == "READ"
    assert columns("Joy") == [
        ("ID", "TEXT"),
        ("Timestamp", "REAL"),
        ("Time", "REAL"),
        ("Note", "TEXT"),
        ("HasPicture", "INTEGER"),
        ("BabyID", "TEXT"),
    ]


def test_a_new_seed_has_the_entries_the_app_ships_with() -> None:
    database = opened(new_seed("Ada", birth=BIRTH, boy=False))

    counts = {
        table: len(rows(database, table))
        for table in TABLES
        if table != "Baby" and rows(database, table)
    }
    assert counts == {
        "AllergenSourceSelection": 8,
        "BathDesc": 4,
        "FeedDesc": 3,
        "MilestoneSelection": 20,
        "OtherActivityDesc": 5,
        "OtherActivityLocationSelection": 2,
        "OtherFeedSelection": 9,
        "SleepDesc": 3,
        "SleepLocationSelection": 5,
        "VaccineSelection": 10,
    }
    # The same entries, under the same IDs, that a record of a built-in one embeds.
    for table, built_in in (
        ("MilestoneSelection", BUILT_IN_MILESTONES),
        ("VaccineSelection", BUILT_IN_VACCINES),
        ("OtherActivityDesc", BUILT_IN_ACTIVITIES),
        ("OtherFeedSelection", BUILT_IN_FOODS),
    ):
        assert [(row[0], row[2]) for row in rows(database, table)] == [
            (choice.object_id, choice.name) for choice in built_in
        ]
    assert rows(database, "MilestoneSelection")[0] == (
        "EasyLogSeed3b250600-894f-11e2-9e96-0800200c9a66",
        0.0,
        "Back home",
        " came home",
        0,
        None,
    )
    assert rows(database, "VaccineSelection")[0][3:] == ("Hepatitis B", 0)
    assert [row[4] for row in rows(database, "OtherFeedSelection")] == [0, 0, 0, 0, 0, 0, 1, 1, 1]
    assert all(row[0].startswith("EasyLogSeed") for row in rows(database, "SleepDesc"))


@pytest.mark.parametrize(
    "arguments",
    [
        {"baby_name": ""},
        {"baby_name": "   "},
        {"baby_name": None},
        {"boy": 1},
        {"boy": None},
        {"birth": datetime(2026, 10, 1, 12, 0)},
        {"birth": "2026-10-01"},
        {"birth": None},
        {"due": datetime(2026, 10, 10, 12, 0)},
        {"due": 0},
    ],
)
def test_a_new_seed_refuses_bad_input(arguments: dict[str, Any]) -> None:
    good: dict[str, Any] = {"baby_name": "Ada", "birth": BIRTH, "boy": False, "due": None}
    given = good | arguments
    with pytest.raises(ValueError):
        new_seed(given.pop("baby_name"), **given)


# --- the group ---------------------------------------------------------------------------


async def test_a_group_is_created_with_the_apps_three_steps(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.has_group = False
    old_account = fake_server.account_id

    await client.create_group(SEED)

    assert calls(fake_server) == [
        ("POST", "/staging_account"),
        ("POST", "/staging_account/seed"),
        ("PUT", "/staging_account"),
    ]
    start, upload = fake_server.requests[0], fake_server.requests[1]
    # The first step carries the sign-in body.
    assert start.body == {
        "EmailAddress": fake_server.email,
        "Password": fake_server.password,
        "Device": {
            "DeviceUUID": PROBE_UUID,
            "DeviceName": "Test Device",
            "DeviceOSInfo": start.body["Device"]["DeviceOSInfo"],
        },
        "AppInfo": {"AppType": 0, "AccountType": 0},
    }
    # The seed is framed as the app frames it.
    assert (
        upload.body
        == (
            f"\r\n--{BOUNDARY}\r\n"
            'Content-Disposition: form-data; name="seed"; filename="seed.zip"\r\n'
            "Content-Type: application/zip\r\n\r\n"
        ).encode()
        + SEED
        + f"\r\n--{BOUNDARY}--\r\n".encode()
    )
    assert fake_server.has_group and fake_server.seed == SEED
    assert fake_server.account_id == old_account + 1


async def test_the_client_signs_in_to_the_new_group_on_its_next_call(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.has_group = False
    await client.create_group(bytearray(SEED))
    assert client.account_id is None

    assert await client.get_seed() == SEED

    assert client.account_id == fake_server.account_id


async def test_an_account_is_set_up_from_nothing_and_written_to(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.has_group = False

    await client.create_group(new_seed("Ada", birth=BIRTH, boy=False))
    engine = SyncEngine(device_uuid=PROBE_UUID)
    result = await engine.sync(client)

    assert result.seed_read
    [(baby_id, baby)] = engine.babies().items()
    assert baby.name == "Ada"
    diaper = await engine.add_diaper(client, baby_id, DIAPER_WET)
    assert diaper.baby is not None and diaper.baby.object_id == baby_id
    assert [entry["OPCode"] for entry in fake_server.devices[PROBE_UUID].log] == [0]


async def test_an_account_that_has_a_group_is_left_alone(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.seed = b"PK\x03\x04the-old-seed"

    with pytest.raises(GroupExistsError):
        await client.create_group(SEED)

    assert calls(fake_server) == [("POST", "/staging_account")]
    assert fake_server.seed == b"PK\x03\x04the-old-seed"


async def test_a_wrong_password_for_an_account_that_exists_raises(
    fake_server: FakeSyncServer, session: aiohttp.ClientSession
) -> None:
    client = BabyTrackerClient(
        fake_server.email,
        "not-the-password",
        PROBE_UUID,
        "Test Device",
        session=session,
        base_url=fake_server.base_url,
    )

    with pytest.raises(AuthenticationError):
        await client.create_group(SEED)

    assert calls(fake_server) == [("POST", "/staging_account")]


@pytest.mark.parametrize(
    "seed",
    [b"", b"not a zip", b"\xff\xd8\xff\xe0a-jpeg", "PK\x03\x04text", None, 7, [80, 75, 3, 4]],
)
async def test_a_seed_that_is_not_a_zip_is_refused_before_any_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, seed: Any
) -> None:
    fake_server.has_group = False

    with pytest.raises(ValueError):
        await client.create_group(seed)

    assert fake_server.requests == []


async def test_a_seed_over_the_cap_is_refused_before_any_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_server.has_group = False
    monkeypatch.setattr(client_module, "MAX_SEED_BYTES", len(SEED) - 1)

    with pytest.raises(ValueError):
        await client.create_group(SEED)

    assert fake_server.requests == []


@pytest.mark.parametrize(
    ("status", "error"),
    [(500, BabyTrackerConnectionError), (400, ProtocolError), (302, ProtocolError)],
)
async def test_a_failed_seed_upload_stops_before_the_last_step(
    client: BabyTrackerClient,
    fake_server: FakeSyncServer,
    status: int,
    error: type[Exception],
) -> None:
    fake_server.has_group = False
    fake_server.fail_paths["/staging_account/seed"] = status

    with pytest.raises(error):
        await client.create_group(SEED)

    assert calls(fake_server) == [("POST", "/staging_account"), ("POST", "/staging_account/seed")]
    assert not fake_server.has_group and fake_server.seed is None


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (424, ProtocolError),
        (409, GroupExistsError),
        (401, AuthenticationError),
        (500, BabyTrackerConnectionError),
    ],
)
async def test_a_refused_last_step_makes_no_group_and_can_be_tried_again(
    client: BabyTrackerClient,
    fake_server: FakeSyncServer,
    status: int,
    error: type[Exception],
) -> None:
    fake_server.has_group = False
    fake_server.finish_status = status

    with pytest.raises(error):
        await client.create_group(SEED)
    assert not fake_server.has_group and fake_server.seed is None

    fake_server.finish_status = None
    await client.create_group(SEED)
    assert fake_server.has_group and fake_server.seed == SEED


async def test_a_refused_first_step_raises(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.has_group = False
    fake_server.force_status = 503

    with pytest.raises(BabyTrackerConnectionError):
        await client.create_group(SEED)

    assert calls(fake_server) == [("POST", "/staging_account")]
