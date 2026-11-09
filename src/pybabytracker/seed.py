"""Read the account's seed: a full copy of the account's database, as of some point.

The transaction logs don't hold the whole history. Records from devices that have since left
the account are only in the seed, so a first sync reads it before the logs. The seed stores
records as table rows, not as the payloads the apps sync, so each row is turned into the payload
its record would sync as and decoded like one.
"""

from __future__ import annotations

import io
import logging
import sqlite3
import zipfile
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .exceptions import ProtocolError, TransactionDecodeError
from .models import Record, decode_payload
from .times import format_time

_LOGGER = logging.getLogger(__name__)

SEED_DATABASE = "EasyLog.db"
# A real seed is a few MB; the cap keeps a damaged or hostile one off small hardware.
MAX_SEED_DATABASE_BYTES = 64 * 1024 * 1024
# The longest text or blob a seed's database may give back. Rows are records and pick-list
# entries, so anything near this is damage, not data.
_MAX_VALUE_BYTES = 10 * 1024 * 1024
_EPOCH = "1970-01-01 00:00:00 +0000"
# What a broken zip or database can raise while it is read.
_UNREADABLE = (
    sqlite3.Error,
    zipfile.BadZipFile,
    zlib.error,
    EOFError,
    OSError,
    NotImplementedError,
    RuntimeError,
)

# A row: every column asked for, None for one the table doesn't have.
Row = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Seed:
    """The seed's records, decoded as if synced."""

    records: tuple[Record, ...] = ()


def read_seed(archive: bytes) -> Seed:
    """Decode a seed zip from ``BabyTrackerClient.get_seed``. No I/O, but it blocks, so run it in
    an executor from async code.

    A zip without the database, or a zip or database that can't be read, raises
    ``ProtocolError``. A row that can't be decoded is skipped with a warning, as an undecodable
    log entry is. Only the columns the records need are read, so photo thumbnails stay on disk.
    """
    try:
        database = _database_bytes(archive)
        connection = sqlite3.connect(":memory:")
        # Text that isn't valid UTF-8 is read with replacement characters, not as a failure.
        connection.text_factory = _lenient_text
        try:
            _lock_down(connection)
            connection.deserialize(_rollback_journal(database))
            records = tuple(_Reader(connection).records())
        finally:
            connection.close()
    except ProtocolError:
        raise
    except _UNREADABLE as err:
        raise ProtocolError(f"the seed can't be read: {err!r}") from err
    return Seed(records)


def _database_bytes(archive: bytes) -> bytes:
    try:
        zipped = zipfile.ZipFile(io.BytesIO(archive))
    except zipfile.BadZipFile as err:
        raise ProtocolError(f"the seed isn't a zip: {err}") from err
    with zipped:
        entries = [i for i in zipped.infolist() if i.filename.rsplit("/", 1)[-1] == SEED_DATABASE]
        if not entries:
            raise ProtocolError(f"the seed has no {SEED_DATABASE}")
        if entries[0].file_size > MAX_SEED_DATABASE_BYTES:
            raise ProtocolError(f"the seed's database is over {MAX_SEED_DATABASE_BYTES} bytes")
        with zipped.open(entries[0]) as source:
            data = source.read(MAX_SEED_DATABASE_BYTES + 1)
    if len(data) > MAX_SEED_DATABASE_BYTES:
        raise ProtocolError(f"the seed's database is over {MAX_SEED_DATABASE_BYTES} bytes")
    return data


def _lock_down(connection: sqlite3.Connection) -> None:
    """Treat the database as untrusted: no schema-defined code, and no huge values."""
    connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
    connection.setconfig(sqlite3.SQLITE_DBCONFIG_TRUSTED_SCHEMA, False)
    connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _MAX_VALUE_BYTES)


def _lenient_text(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def _rollback_journal(database: bytes) -> bytes | bytearray:
    """The database marked as using a rollback journal. A database saved in WAL mode can't be
    opened from memory otherwise, and a seed has no WAL file to go with it anyway."""
    if len(database) >= 20 and (database[18] == 2 or database[19] == 2):
        patched = bytearray(database)
        patched[18] = patched[19] = 1
        return patched  # no second copy: SQLite reads a bytearray as well
    return database


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _time(value: Any) -> str | None:
    """A database time (Unix seconds) as a payload time, or ``None`` when it's unset (the app
    writes 0 for a date it doesn't have)."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    try:
        return format_time(datetime.fromtimestamp(float(value), UTC))
    except (OverflowError, OSError, ValueError):
        return None


def _flag(value: Any) -> str:
    return "true" if value else "false"


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _number(value: Any, default: int = 0) -> float | int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return int(value) if float(value).is_integer() else float(value)


def _measure(kind: str, value: Any, english: Any) -> dict[str, Any]:
    return {"BCObjectType": kind, "englishMeasure": _flag(english), "value": _number(value)}


@dataclass(frozen=True, slots=True)
class _Table:
    record_type: str
    columns: tuple[str, ...]
    build: Callable[[_Reader, Row], dict[str, Any]]


# Columns every record table has. Pump has no BabyID: pumping belongs to the account.
_ACTIVITY_COLUMNS = ("ID", "Timestamp", "Time", "Note", "BabyID")
_SELECTION_COLUMNS = ("ID", "Timestamp", "Name", "Description")


class _Reader:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection
        listed = connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        self._tables = {
            name: {r[1] for r in connection.execute(f"PRAGMA table_info({_quote(name)})")}
            for (name,) in listed.fetchall()
        }
        baby_columns = ("ID", "Timestamp", "Name", "DOB", "DueDay", "Gender", "Picture")
        self._baby_rows = list(self._select("Baby", baby_columns))
        self._babies = {row["ID"]: self._baby(row) for row in self._baby_rows}
        self._pictures: dict[str, list[dict[str, Any]]] = {}
        picture_columns = ("ID", "Timestamp", "ActivityID", "Type", "FileName")
        for row in self._select("Picture", picture_columns):
            if isinstance(row["ActivityID"], str) and row["FileName"]:
                self._pictures.setdefault(row["ActivityID"], []).append(self._picture(row))
        self._selections: dict[str, dict[str, dict[str, Any]]] = {}

    def _select(self, table: str, columns: tuple[str, ...]) -> Iterator[Row]:
        """Each row of ``table`` with ``columns``; one the table lacks comes back as None. A
        table without an ID column, or missing, has no rows."""
        have = self._tables.get(table, set())
        present = [c for c in columns if c in have]
        if "ID" in columns and "ID" not in present:
            return
        sql = f"SELECT {', '.join(map(_quote, present))} FROM {_quote(table)}"
        for values in self._db.execute(sql):
            row = dict.fromkeys(columns)
            row.update(zip(present, values, strict=True))
            if "ID" not in row or (isinstance(row["ID"], str) and row["ID"]):
                yield row

    def records(self) -> Iterator[Record]:
        for row in self._baby_rows:
            baby = dict(self._babies[row["ID"]])
            baby["timestamp"] = _time(row["Timestamp"]) or _EPOCH
            yield from self._decode("Baby", row, baby)
        for name, table in _TABLES.items():
            for row in self._select(name, _ACTIVITY_COLUMNS + table.columns):
                try:
                    payload = self._activity(row)
                    if payload is None:
                        continue
                    payload.update(table.build(self, row))
                except (LookupError, TypeError, ValueError) as err:
                    _LOGGER.warning("Skipping seed row %s in %s: %r", row["ID"], name, err)
                    continue
                payload["BCObjectType"] = table.record_type
                yield from self._decode(name, row, payload)

    def _decode(self, table: str, row: Row, payload: dict[str, Any]) -> Iterator[Record]:
        try:
            yield decode_payload(payload)
        except TransactionDecodeError as err:
            _LOGGER.warning("Skipping seed row %s in %s: %s", row["ID"], table, err)

    def _baby(self, row: Row) -> dict[str, Any]:
        """A baby as records embed it."""
        baby: dict[str, Any] = {"BCObjectType": "Baby", "objectID": row["ID"]}
        baby["name"] = _text(row["Name"])
        if (dob := _time(row["DOB"])) is not None:
            baby["dob"] = dob
        if (due := _time(row["DueDay"])) is not None:
            baby["dueDay"] = due
        baby["gender"] = _flag(row["Gender"])
        baby["pictureName"] = _text(row["Picture"])
        baby["newFlage"] = "false"
        baby["timestamp"] = _EPOCH
        return baby

    def _picture(self, row: Row) -> dict[str, Any]:
        return {
            "objectID": row["ID"],
            "fileName": _text(row["FileName"]),
            "mediaType": _number(row["Type"]),
            "deleted": "false",
            "timestamp": _time(row["Timestamp"]) or _EPOCH,
        }

    def entry(self, table: str, object_id: Any) -> dict[str, Any] | None:
        """A pick-list entry's own fields, or ``None`` when the seed doesn't have it."""
        if not isinstance(object_id, str) or not object_id:
            return None
        if table not in self._selections:
            extra, build = _PICK_LISTS.get(table, ((), _nothing))
            entries: dict[str, dict[str, Any]] = {}
            for row in self._select(table, _SELECTION_COLUMNS + extra):
                entries[row["ID"]] = {
                    "objectID": row["ID"],
                    "name": _text(row["Name"]),
                    "desc": _text(row["Description"]),
                    "newFlage": "false",
                    "timestamp": _time(row["Timestamp"]) or _EPOCH,
                    **build(row),
                }
            self._selections[table] = entries
        return self._selections[table].get(object_id)

    def embed(
        self, payload: dict[str, Any], key: str, table: str, kind: str, object_id: Any, id_key: str
    ) -> None:
        """Embed a pick-list entry under ``key``, as synced records carry it. An entry the seed
        doesn't have keeps its ID under ``id_key`` instead, so no nameless copy is invented."""
        entry = self.entry(table, object_id)
        if entry is not None:
            payload[key] = {**entry, "BCObjectType": kind}
        elif isinstance(object_id, str) and object_id:
            payload[id_key] = object_id

    def _activity(self, row: Row) -> dict[str, Any] | None:
        time = _time(row["Time"])
        if time is None:
            return None
        payload: dict[str, Any] = {
            "objectID": row["ID"],
            "time": time,
            "timestamp": _time(row["Timestamp"]) or _EPOCH,
            "note": _text(row["Note"]),
            "newFlage": "false",
            "pictureLoaded": "true",
            "pictureNote": list(self._pictures.get(row["ID"], [])),
        }
        if row["BabyID"] is not None:
            baby = self._babies.get(row["BabyID"])
            if baby is None:
                # The app keeps no record of a baby that isn't in its database.
                return None
            payload["baby"] = dict(baby)
        return payload


def _nothing(_row: Row) -> dict[str, Any]:
    return {}


def _medicine_entry(row: Row) -> dict[str, Any]:
    return {
        "amountPerTime": _number(row["AmountPerTime"]),
        "interval": _number(row["Interval"]),
        "unit": _text(row["Unit"]),
        "prescription": bool(row["IsPrescription"]),
    }


def _age_entry(row: Row) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    if row["ForAge"] is not None:
        entry["forAge"] = _number(row["ForAge"])
    if row.get("BabyID"):
        entry["babyID"] = row["BabyID"]
    return entry


def _food_entry(row: Row) -> dict[str, Any]:
    return {"bottle": _flag(row["IsBottle"])}


# Pick-list tables: their own columns, and how those read.
_PICK_LISTS: dict[str, tuple[tuple[str, ...], Callable[[Row], dict[str, Any]]]] = {
    "MedicineSelection": (("IsPrescription", "AmountPerTime", "Unit", "Interval"), _medicine_entry),
    "MilestoneSelection": (("ForAge", "BabyID"), _age_entry),
    "VaccineSelection": (("ForAge",), _age_entry),
    "OtherFeedSelection": (("IsBottle",), _food_entry),
}


def _ids(row: Row, *pairs: tuple[str, str]) -> dict[str, Any]:
    """References the app stores as plain IDs, under the keys the library reads them by."""
    return {key: row[column] for key, column in pairs if _text(row[column])}


def _volume(row: Row, column: str = "Amount") -> dict[str, Any]:
    return _measure("VolumeMeasure", row[column], row["IsEnglishScale"])


def _diaper(_r: _Reader, row: Row) -> dict[str, Any]:
    # The column defaults: 5 is "not set" for the amount, colors and texture.
    return {
        "status": _number(row["Status"]),
        "amount": _number(row["Amount"], 5),
        "peeColor": _number(row["PeeColor"], 5),
        "pooColor": _number(row["PooColor"], 5),
        "texture": _number(row["Texture"], 5),
        "flag": _number(row["Flag"]),
    }


def _nursing(_r: _Reader, row: Row) -> dict[str, Any]:
    return {
        "finishSide": _number(row["FinishSide"]),
        "leftDuration": _number(row["LeftDuration"]),
        "rightDuration": _number(row["RightDuration"]),
        "bothDuration": _number(row["BothDuration"]),
    } | _ids(row, ("descID", "DescID"))


def _bottle(_r: _Reader, row: Row) -> dict[str, Any]:
    return {"amount": _volume(row)} | _ids(row, ("descID", "DescID"))


def _pumped(r: _Reader, row: Row) -> dict[str, Any]:
    return _bottle(r, row) | _ids(row, ("pumpID", "PumpID"))


def _other_feed(r: _Reader, row: Row) -> dict[str, Any]:
    payload: dict[str, Any] = {"unit": _text(row["Unit"])}
    if _number(row["Amount"]) > 0:
        payload["amount"] = _volume(row)
    r.embed(
        payload, "feedType", "OtherFeedSelection", "OtherFeedSelection", row["TypeID"], "typeID"
    )
    if desc := _text(row["DescID"]):
        # The app syncs a food's description as an object; often only its ID is set.
        entry = r.entry("FeedDesc", desc) or {"objectID": desc, "timestamp": _EPOCH}
        payload["desc"] = {**entry, "newFlage": "false", "BCObjectType": "FeedDescription"}
    return payload


def _pump(_r: _Reader, row: Row) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "amount": _volume(row),
        "finishSide": _number(row["FinishSide"]),
        "sides": _number(row["Sides"]),
        "leftAmount": _volume(row, "LeftAmount"),
        "rightAmount": _volume(row, "RightAmount"),
        "leftDuration": _number(row["LeftDuration"]),
        "rightDuration": _number(row["RightDuration"]),
    }
    if label := _text(row["Label"]):
        payload["label"] = label
    return payload


def _sleep(_r: _Reader, row: Row) -> dict[str, Any]:
    ids = _ids(row, ("descID", "DescID"), ("locationID", "LocationID"))
    return {"duration": _number(row["Duration"])} | ids


def _temperature(_r: _Reader, row: Row) -> dict[str, Any]:
    if _number(row["Temp"]) <= 0:
        return {}
    return {"temperature": _measure("TemperatureMeasure", row["Temp"], row["IsEnglishScale"])}


def _growth(_r: _Reader, row: Row) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    weight, length, head = (_number(row[c]) for c in ("Weight", "Length", "Head"))
    # The column default is English (1) for both scales.
    english_weight = 1 if row["IsEnglishWeightScale"] is None else row["IsEnglishWeightScale"]
    english_length = 1 if row["IsEnglishLengthScale"] is None else row["IsEnglishLengthScale"]
    if weight > 0:
        payload["weight"] = _measure("WeightMeasure", weight, english_weight)
    if length > 0:
        payload["length"] = _measure("LengthMeasure", length, english_length)
    if head > 0:
        payload["head"] = _measure("LengthMeasure", head, english_length)
    return payload


def _medication(r: _Reader, row: Row) -> dict[str, Any]:
    payload: dict[str, Any] = {"amount": _number(row["Amount"])}
    r.embed(
        payload,
        "medicationSelection",
        "MedicineSelection",
        "MedicationSelection",
        row["MedID"],
        "medID",
    )
    return payload


def _milestone(r: _Reader, row: Row) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    selection = row["MilestoneSelectionID"]
    r.embed(
        payload,
        "milestoneType",
        "MilestoneSelection",
        "MilestoneSelection",
        selection,
        "milestoneSelectionID",
    )
    return payload


def _vaccine(r: _Reader, row: Row) -> dict[str, Any]:
    payload: dict[str, Any] = _ids(row, ("docVisitID", "DocVisitID"))
    r.embed(payload, "vaccineType", "VaccineSelection", "VaccineSelection", row["VaccID"], "vaccID")
    return payload


def _other_activity(r: _Reader, row: Row) -> dict[str, Any]:
    payload: dict[str, Any] = {"duration": _number(row["Duration"])}
    payload |= _ids(row, ("locationID", "LocationID"))
    r.embed(
        payload, "desc", "OtherActivityDesc", "OtherActivityDescription", row["DescID"], "descID"
    )
    return payload


def _columns(*pairs: tuple[str, str]) -> Callable[[_Reader, Row], dict[str, Any]]:
    """Fields copied as they are, for types the library reads by their stored names."""

    def build(_r: _Reader, row: Row) -> dict[str, Any]:
        return {key: row[column] for key, column in pairs if row[column] is not None}

    return build


def _table(
    record_type: str, build: Callable[[_Reader, Row], dict[str, Any]], *columns: str
) -> _Table:
    return _Table(record_type, columns, build)


# Record tables: the type each syncs as, its own columns, and how those read.
_TABLES: dict[str, _Table] = {
    "Diaper": _table(
        "Diaper", _diaper, "Status", "Amount", "PeeColor", "PooColor", "Texture", "Flag"
    ),
    "Nursing": _table(
        "Nursing",
        _nursing,
        "FinishSide",
        "LeftDuration",
        "RightDuration",
        "BothDuration",
        "DescID",
    ),
    "Formula": _table("Formula", _bottle, "Amount", "IsEnglishScale", "DescID"),
    "Pumped": _table("Pumped", _pumped, "Amount", "IsEnglishScale", "DescID", "PumpID"),
    "OtherFeed": _table(
        "OtherFeed", _other_feed, "TypeID", "DescID", "Amount", "IsEnglishScale", "Unit"
    ),
    "Pump": _table(
        "Pump",
        _pump,
        "FinishSide",
        "Sides",
        "Amount",
        "IsEnglishScale",
        "Label",
        "LeftAmount",
        "RightAmount",
        "LeftDuration",
        "RightDuration",
    ),
    "Sleep": _table("Sleep", _sleep, "Duration", "DescID", "LocationID"),
    "Temperature": _table("Temperature", _temperature, "Temp", "IsEnglishScale"),
    "Growth": _table(
        "Growth",
        _growth,
        "Weight",
        "Length",
        "Head",
        "IsEnglishWeightScale",
        "IsEnglishLengthScale",
    ),
    "Medicine": _table("Medication", _medication, "MedID", "Amount"),
    "Milestone": _table("Milestone", _milestone, "MilestoneSelectionID"),
    "Vaccine": _table("Vaccine", _vaccine, "VaccID", "DocVisitID"),
    "OtherActivity": _table("OtherActivity", _other_activity, "DescID", "LocationID", "Duration"),
    # The app syncs a bath with no fields of its own; its type is kept as a plain ID.
    "Bath": _table("Bath", _columns(("descID", "DescID")), "DescID"),
    "Joy": _table("Joy", _columns()),
    "Journal": _table("Journal", _columns()),
    "Sick": _table("Sick", _columns(("symptom", "Symptom")), "Symptom"),
    "Allergen": _table(
        "Allergen",
        _columns(
            ("sourceID", "SourceID"),
            ("reaction", "Reaction"),
            ("severity", "Severity"),
            ("notSure", "NotSure"),
        ),
        "SourceID",
        "Reaction",
        "Severity",
        "NotSure",
    ),
    "HealthQuestion": _table(
        "HealthQuestion",
        _columns(
            ("question", "Question"), ("answer", "Answer"), ("doctorVisitID", "DoctorVisitID")
        ),
        "Question",
        "Answer",
        "DoctorVisitID",
    ),
    "DoctorVisit": _table(
        "DoctorVisit",
        _columns(
            ("isAppointment", "IsAppointment"),
            ("visitType", "VisitType"),
            ("doctorID", "DoctorID"),
            ("doctorNote", "DoctorNote"),
            ("symptom", "Symptom"),
            ("growthID", "GrowthID"),
            ("sickID", "SickID"),
        ),
        "IsAppointment",
        "VisitType",
        "DoctorID",
        "DoctorNote",
        "Symptom",
        "GrowthID",
        "SickID",
    ),
}
