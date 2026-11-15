"""Make the seed for a new account: the app's database as a newly installed app has it.

``BabyTrackerClient.create_group`` uploads a seed, and an app uploads its own database. A new
account has no database yet, so ``new_seed`` makes one: the tables and indexes the Android app
creates, the pick-list entries it ships with, and one baby.

How well this is checked: the tables, columns, indexes, and built-in entries are the ones the
Android app creates when it first runs and when it opens its database. The seed of a real
account, downloaded on 2026-11-14, has every one of the 36 record and pick-list tables with the
same columns, the same 58 indexes, and all 69 built-in entries value for value. Its ``Pump``
table has four more columns, which the apps add later and are included here. The five
bookkeeping tables are the Android app's, with the columns the real seed has; that seed also has
four more that only the iPhone app makes, which are left out, as they are from a seed an Android
phone uploads.
"""

from __future__ import annotations

import io
import sqlite3
import time
import uuid
import zipfile
from datetime import datetime

from .seed import SEED_DATABASE
from .selections import (
    _BUILT_IN_ACTIVITIES,
    _BUILT_IN_FOODS,
    _BUILT_IN_MILESTONES,
    _BUILT_IN_VACCINES,
)
from .times import to_unix

_KEY = "ID TEXT PRIMARY KEY NOT NULL UNIQUE, Timestamp REAL DEFAULT 0"
# A pick list's columns, and the columns every record starts with.
_ENTRY = f"{_KEY}, Name TEXT NOT NULL UNIQUE, Description TEXT"
_RECORD = f"{_KEY}, Time REAL NOT NULL, Note TEXT, HasPicture INTEGER"

# Each pick list's table, with the columns it has after a pick list's own.
_ENTRY_TABLES: dict[str, str] = {
    "SickDesc": "",
    "BathDesc": "",
    "SleepDesc": "",
    "FeedDesc": "",
    "OtherActivityDesc": "",
    "MilestoneSelection": "ForAge INTEGER, BabyID TEXT",
    "DoctorSelection": "Address TEXT, ClinicName TEXT, PhoneNumber TEXT",
    "MedicineSelection": "IsPrescription INTEGER, AmountPerTime REAL, Unit TEXT, Interval INTEGER",
    "VaccineSelection": "ForAge INTEGER",
    "OtherFeedSelection": "IsBottle INTEGER",
    "AllergenSourceSelection": "",
    "SleepLocationSelection": "",
    "OtherActivityLocationSelection": "",
}

# Each table of a baby's records, with the columns it has after a record's own and ``BabyID``.
# ``Head READ`` is the app's spelling.
_RECORD_TABLES: dict[str, str] = {
    "Milestone": "MilestoneSelectionID TEXT NOT NULL",
    "OtherActivity": "DescID TEXT, LocationID TEXT, Duration INTEGER",
    "Bath": "DescID TEXT",
    "Diaper": (
        "Status INTEGER NOT NULL, PeeColor INTEGER DEFAULT 5, PooColor INTEGER DEFAULT 5, "
        "Amount INTEGER DEFAULT 5, Texture INTEGER DEFAULT 5, Flag INTEGER DEFAULT 0"
    ),
    "Sleep": "Duration INTEGER DEFAULT 0, LocationID TEXT, DescID TEXT",
    "Nursing": (
        "DescID TEXT, FinishSide INTEGER, LeftDuration INTEGER DEFAULT 0, "
        "RightDuration INTEGER DEFAULT 0, BothDuration INTEGER DEFAULT 0"
    ),
    "OtherFeed": (
        "DescID TEXT, TypeID TEXT NOT NULL, Amount REAL DEFAULT 0, "
        "IsEnglishScale INTEGER DEFAULT 0, Unit TEXT"
    ),
    "Pumped": "DescID TEXT, Amount REAL NOT NULL, IsEnglishScale INTEGER NOT NULL, PumpID TEXT",
    "Formula": "DescID TEXT, Amount REAL NOT NULL, IsEnglishScale INTEGER NOT NULL",
    "Temperature": "Temp REAL NOT NULL, IsEnglishScale INTEGER NOT NULL",
    "Allergen": (
        "SourceID TEXT NOT NULL, Reaction TEXT, Severity INTEGER NOT NULL, "
        "NotSure INTEGER DEFAULT 0"
    ),
    "HealthQuestion": "Question TEXT NOT NULL, Answer TEXT, DoctorVisitID TEXT",
    "Medicine": "MedID TEXT NOT NULL, Amount REAL",
    "Vaccine": "VaccID TEXT NOT NULL, DocVisitID TEXT NOT NULL",
    "Sick": "Symptom TEXT",
    "Growth": (
        "Weight REAL DEFAULT 0, Length REAL DEFAULT 0, Head READ DEFAULT 0, "
        "IsEnglishLengthScale INTEGER DEFAULT 1, IsEnglishWeightScale INTEGER DEFAULT 1"
    ),
    "DoctorVisit": (
        "IsAppointment INTEGER NOT NULL, VisitType INTEGER DEFAULT 0, DoctorID TEXT, "
        "DoctorNote TEXT, Symptom TEXT, GrowthID TEXT, SickID TEXT"
    ),
    "Joy": "",
    "Journal": "",
}

# The tables that fit neither pattern. A pump isn't any one baby's. The last five are an app's
# own bookkeeping, which it makes when it opens a database without them.
_OTHER_TABLES: dict[str, str] = {
    "Baby": (
        f"{_KEY}, Name TEXT NOT NULL UNIQUE, DOB REAL, DueDay REAL, Gender INTEGER, Picture TEXT"
    ),
    "Picture": (
        f"{_KEY}, ActivityID TEXT NOT NULL, Type INTEGER, FileName TEXT NOT NULL, Thumbnail BLOB"
    ),
    "Pump": (
        f"{_RECORD}, FinishSide INTEGER, Sides INTEGER, Amount REAL, IsEnglishScale INTEGER, "
        "Label TEXT, PumpedID TEXT, LeftAmount REAL DEFAULT 0, RightAmount REAL DEFAULT 0, "
        "LeftDuration INTEGER DEFAULT 0, RightDuration INTEGER DEFAULT 0"
    ),
    "DoctorVisitMedicine": f"{_KEY}, DoctorVisitID TEXT NOT NULL, MedID TEXT NOT NULL",
    "TransactionLog": (
        "ID INTEGER PRIMARY KEY AUTOINCREMENT, OpCode INTEGER NOT NULL, Log BLOB NOT NULL"
    ),
    "PhotoList": "PhotoFile TEXT PRIMARY KEY NOT NULL UNIQUE",
    "PhotoDownloadList": (
        "PhotoFile TEXT PRIMARY KEY NOT NULL UNIQUE, DownloadCount INTEGER DEFAULT 0"
    ),
    "MergedTransaction": "DeviceID TEXT PRIMARY KEY NOT NULL UNIQUE, SyncID INTEGER",
    "ReliveList": "ID TEXT PRIMARY KEY NOT NULL UNIQUE",
}

# The record tables whose index on ``BabyID`` the app names "...IndexBaby", not "...IndexBabyID".
_SHORT_BABY_INDEX = frozenset({"Milestone", "OtherActivity", "Bath", "Diaper"})
# The other indexes: table, the end of the index's name, and the column.
_OTHER_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("Baby", "Name", "Name"),
    ("Pump", "Time", "Time"),
    ("MilestoneSelection", "ForBaby", "BabyID"),
    ("VaccineSelection", "ForAge", "ForAge"),
    ("OtherFeedSelection", "ForIsBottle", "IsBottle"),
    ("Picture", "ActivityID", "ActivityID"),
    ("DoctorVisitMedicine", "DocVisit", "DoctorVisitID"),
)

# The entries the app ships with on the pick lists this library has no other use for: table,
# then each entry's ID and name.
_OTHER_BUILT_INS: dict[str, tuple[tuple[str, str], ...]] = {
    "BathDesc": (
        ("EasyLogSeeda1b71e20-86a3-11e2-9e96-0800200c9a66", "Sponge Bath"),
        ("EasyLogSeedb0bec620-86a3-11e2-9e96-0800200c9a66", "Bubble Bath"),
        ("EasyLogSeed1d710590-bc09-11e2-9e96-0800200c9a66", "Shower"),
        ("EasyLogSeedc0395430-86a3-11e2-9e96-0800200c9a66", "Washed Hair"),
    ),
    "SleepDesc": (
        ("EasyLogSeed9f8f8700-bc08-11e2-9e96-0800200c9a66", "Falling asleep naturally"),
        ("EasyLogSeed420e28b0-798d-11e2-b92a-0800200c9a66", "Falling asleep when feeding"),
        ("EasyLogSeed76ab2220-798e-11e2-b92a-0800200c9a66", "Crying it out"),
    ),
    "FeedDesc": (
        ("EasyLogSeeda1c40f10-758f-11e2-bcfd-0800200c9a66", "Spit up"),
        ("EasyLogSeed1e3fbc10-7590-11e2-bcfd-0800200c9a66", "Burped"),
        ("EasyLogSeed4c6bf4a0-7590-11e2-bcfd-0800200c9a66", "Burped & spit up"),
    ),
    "AllergenSourceSelection": (
        ("EasyLogSeedd137d210-7f6f-11e2-9e96-0800200c9a66", "Milk"),
        ("EasyLogSeeddbb3fe80-7f6f-11e2-9e96-0800200c9a66", "Egg"),
        ("EasyLogSeede31f1a60-7f6f-11e2-9e96-0800200c9a66", "Fish"),
        ("EasyLogSeede9ffd090-7f6f-11e2-9e96-0800200c9a66", "Shellfish"),
        ("EasyLogSeedfc0ffda0-7f6f-11e2-9e96-0800200c9a66", "Wheat"),
        ("EasyLogSeedf17e7470-7f6f-11e2-9e96-0800200c9a66", "Tree nut"),
        ("EasyLogSeed03bed940-7f70-11e2-9e96-0800200c9a66", "Peanut"),
        ("EasyLogSeed0bbda9a0-7f70-11e2-9e96-0800200c9a66", "Soybean"),
    ),
    "SleepLocationSelection": (
        ("EasyLogSeeddaf20cc0-798a-11e2-b92a-0800200c9a66", "Bassinet"),
        ("EasyLogSeed5cf28970-798b-11e2-b92a-0800200c9a66", "Car Seat"),
        ("EasyLogSeed6b6d33b0-798b-11e2-b92a-0800200c9a66", "Crib"),
        ("EasyLogSeeda13471c0-798b-11e2-b92a-0800200c9a66", "Our bed"),
        ("EasyLogSeedc69d2a60-798b-11e2-b92a-0800200c9a66", "Stroller"),
    ),
    "OtherActivityLocationSelection": (
        ("EasyLogSeed72f04f90-8cbf-11e2-9e96-0800200c9a66", "Our bed"),
        ("EasyLogSeed7a672b40-8cbf-11e2-9e96-0800200c9a66", "Outdoors"),
    ),
}


def _tables() -> dict[str, str]:
    """Every table and its column list."""
    tables = {name: f"{_ENTRY}, {more}" if more else _ENTRY for name, more in _ENTRY_TABLES.items()}
    for name, more in _RECORD_TABLES.items():
        tables[name] = f"{_RECORD}, BabyID TEXT NOT NULL" + (f", {more}" if more else "")
    return tables | _OTHER_TABLES


def _indexes() -> list[tuple[str, str, str]]:
    """Every index: its table, the end of its name, and its column."""
    indexes = [(table, "Name", "Name") for table in _ENTRY_TABLES]
    for table in _RECORD_TABLES:
        indexes.append((table, "Time", "Time"))
        indexes.append((table, "Baby" if table in _SHORT_BABY_INDEX else "BabyID", "BabyID"))
    return indexes + list(_OTHER_INDEXES)


def _create(database: sqlite3.Connection) -> None:
    for table, columns in _tables().items():
        quoted = ", ".join(
            f'"{name}" {rest}' for name, _, rest in (c.partition(" ") for c in columns.split(", "))
        )
        database.execute(f'CREATE TABLE "{table}" ({quoted})')
    for table, end, column in _indexes():
        database.execute(f'CREATE INDEX "{table}Index{end}" ON "{table}" ("{column}" ASC)')
    entry = 'INSERT INTO "{}" ("ID", "Timestamp", "Name", "Description"{}) VALUES (?, 0, ?, ?{})'
    database.executemany(
        entry.format("MilestoneSelection", ', "ForAge"', ", 0"), _BUILT_IN_MILESTONES
    )
    database.executemany(entry.format("VaccineSelection", ', "ForAge"', ", 0"), _BUILT_IN_VACCINES)
    database.executemany(
        entry.format("OtherFeedSelection", ', "IsBottle"', ", ?"),
        [(object_id, name, "", int(bottle)) for object_id, name, bottle in _BUILT_IN_FOODS],
    )
    plain = {"OtherActivityDesc": _BUILT_IN_ACTIVITIES, **_OTHER_BUILT_INS}
    for table, rows in plain.items():
        database.executemany(
            entry.format(table, "", ""), [(object_id, name, "") for object_id, name in rows]
        )


def new_seed(baby_name: str, *, birth: datetime, boy: bool, due: datetime | None = None) -> bytes:
    """The seed for a new account with one baby: a zip for ``BabyTrackerClient.create_group``.

    The app's new-baby screen asks for a name, a ``birth`` date (aware), and whether the baby is
    a ``boy``; ``due`` is optional. The baby gets a new ID, which ``SyncEngine.babies()`` gives
    after a sync of the new account. Bad input raises ``ValueError``. No I/O.
    """
    if not isinstance(baby_name, str) or not baby_name.strip():
        raise ValueError("a baby's name must be text")
    if not isinstance(boy, bool):
        raise ValueError("boy must be True or False")
    if not isinstance(birth, datetime) or not isinstance(due, datetime | None):
        raise ValueError("birth and due must be datetimes")
    born = to_unix(birth)
    due_day = to_unix(due) if due is not None else 0.0  # the app writes 0 for a date it lacks
    database = sqlite3.connect(":memory:")
    try:
        _create(database)
        database.execute(
            'INSERT INTO "Baby" ("ID", "Timestamp", "Name", "DOB", "DueDay", "Gender") '
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()).upper(), time.time(), baby_name.strip(), born, due_day, int(boy)),
        )
        database.commit()
        data = database.serialize()
    finally:
        database.close()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr(SEED_DATABASE, data)
    return archive.getvalue()
