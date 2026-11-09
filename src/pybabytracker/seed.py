"""Read the account's seed: a full copy of the account's database, as of some point.

The transaction logs don't hold the whole history. Records from devices that have since left
the account are only in the seed, so a first sync reads it before the logs. The seed stores
records as table rows, not as the payloads the apps sync, so each row is turned into the payload
its record would sync as and decoded like one. Records the logs also hold come back with a newer
version there, so the logs win.
"""

from __future__ import annotations

import io
import logging
import sqlite3
import zipfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .exceptions import ProtocolError, TransactionDecodeError
from .models import Record, decode_payload
from .times import format_time

_LOGGER = logging.getLogger(__name__)

SEED_DATABASE = "EasyLog.db"
MAX_SEED_DATABASE_BYTES = 500 * 1024 * 1024
# Built-in pick-list entries every app ships. They never sync, so they aren't records.
_BUILT_IN_PREFIX = "EasyLogSeed"

Row = sqlite3.Row


@dataclass(frozen=True, slots=True)
class Seed:
    """What a seed holds.

    ``records`` are the seed's records, decoded as if synced. ``merged`` is how far into each
    device's log the copy goes, as ``{device_uuid: sync_id}``: a log is read from there.
    """

    records: tuple[Record, ...] = ()
    merged: Mapping[str, int] = field(default_factory=dict)


def read_seed(archive: bytes) -> Seed:
    """Decode a seed zip from ``BabyTrackerClient.get_seed``. No I/O; blocking, so run it in an
    executor from async code.

    A zip without the database, or a database that isn't one, raises ``ProtocolError``. A row
    that can't be decoded is skipped with a warning, as an undecodable log entry is.
    """
    database = _database_bytes(archive)
    connection = sqlite3.connect(":memory:")
    try:
        try:
            connection.deserialize(database)
            connection.row_factory = sqlite3.Row
            tables = _tables(connection)
        except sqlite3.DatabaseError as err:
            raise ProtocolError(f"the seed's database can't be read: {err}") from err
        reader = _Reader(connection, tables)
        return Seed(tuple(reader.records()), reader.merged())
    finally:
        connection.close()


def _database_bytes(archive: bytes) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            entries = [
                i for i in zipped.infolist() if i.filename.rsplit("/", 1)[-1] == SEED_DATABASE
            ]
            if not entries:
                raise ProtocolError(f"the seed has no {SEED_DATABASE}")
            entry = entries[0]
            if entry.file_size > MAX_SEED_DATABASE_BYTES:
                raise ProtocolError(
                    f"the seed's database is larger than {MAX_SEED_DATABASE_BYTES} bytes"
                )
            with zipped.open(entry) as source:
                data = source.read(MAX_SEED_DATABASE_BYTES + 1)
    except zipfile.BadZipFile as err:
        raise ProtocolError(f"the seed isn't a zip: {err}") from err
    if len(data) > MAX_SEED_DATABASE_BYTES:
        raise ProtocolError(f"the seed's database is larger than {MAX_SEED_DATABASE_BYTES} bytes")
    return data


def _tables(connection: sqlite3.Connection) -> dict[str, set[str]]:
    names = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return {
        name: {r[1] for r in connection.execute(f"PRAGMA table_info({_quote(name)})")}
        for name in names
    }


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _time(value: Any) -> str | None:
    """A database time (Unix seconds) as a payload time, or ``None`` when it's unset."""
    if value is None or isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return format_time(datetime.fromtimestamp(float(value), UTC))
    except (OverflowError, OSError, ValueError):
        return None


def _stamp(value: Any) -> str:
    """A write time; an unset one is the epoch, as embedded objects carry it."""
    return _time(value) or "1970-01-01 00:00:00 +0000"


def _flag(value: Any) -> str:
    return "true" if value else "false"


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _number(value: Any) -> float | int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return int(value) if float(value).is_integer() else float(value)


def _measure(kind: str, value: Any, english: Any) -> dict[str, Any]:
    return {"BCObjectType": kind, "englishMeasure": _flag(english), "value": _number(value)}


class _Reader:
    def __init__(self, connection: sqlite3.Connection, tables: dict[str, set[str]]) -> None:
        self._db = connection
        self._tables = tables
        self._babies = {r["ID"]: self._baby(r) for r in self._rows("Baby")}
        self._pictures: dict[str, list[dict[str, Any]]] = {}
        for row in self._rows("Picture"):
            self._pictures.setdefault(_text(row["ActivityID"]), []).append(self._picture(row))
        self._selections: dict[str, dict[str, dict[str, Any]]] = {}

    def _rows(self, table: str) -> Iterator[sqlite3.Row]:
        if table not in self._tables:
            return iter(())
        return iter(self._db.execute(f"SELECT * FROM {_quote(table)}"))

    def _col(self, row: sqlite3.Row, name: str) -> Any:
        # ``in row`` would look through the values, not the column names.
        return row[name] if name in row.keys() else None  # noqa: SIM118

    def merged(self) -> dict[str, int]:
        merged: dict[str, int] = {}
        for row in self._rows("MergedTransaction"):
            device, sync_id = row["DeviceID"], row["SyncID"]
            if isinstance(device, str) and device and isinstance(sync_id, int) and sync_id > 0:
                merged[device] = sync_id
        return merged

    def records(self) -> Iterator[Record]:
        yield from self._babies_as_records()
        for table, (record_type, build) in _SELECTIONS.items():
            for row in self._rows(table):
                if _text(row["ID"]).startswith(_BUILT_IN_PREFIX):
                    continue
                try:
                    entry = {**self._selection(row), **build(row)}
                except IndexError as err:  # a column this table doesn't have
                    _LOGGER.warning("Skipping seed row %s in %s: %s", row["ID"], table, err)
                    continue
                yield from self._decode(table, row, entry, record_type)
        for table, (record_type, activity) in _ACTIVITIES.items():
            for row in self._rows(table):
                try:
                    payload = self._activity(row)
                    if payload is None:
                        continue
                    payload.update(activity(self, row))
                except IndexError as err:  # a column this table doesn't have
                    _LOGGER.warning("Skipping seed row %s in %s: %s", row["ID"], table, err)
                    continue
                yield from self._decode(table, row, payload, record_type)

    def _decode(
        self, table: str, row: sqlite3.Row, payload: dict[str, Any], record_type: str
    ) -> Iterator[Record]:
        payload["BCObjectType"] = record_type
        try:
            yield decode_payload(payload)
        except TransactionDecodeError as err:
            _LOGGER.warning("Skipping seed row %s in %s: %s", row["ID"], table, err)

    def _babies_as_records(self) -> Iterator[Record]:
        for row in self._rows("Baby"):
            payload = dict(self._babies[row["ID"]])
            payload["timestamp"] = _stamp(row["Timestamp"])
            yield from self._decode("Baby", row, payload, "Baby")

    def _baby(self, row: sqlite3.Row) -> dict[str, Any]:
        """A baby as records embed it."""
        baby: dict[str, Any] = {"BCObjectType": "Baby", "objectID": row["ID"]}
        baby["name"] = _text(row["Name"])
        if (dob := _time(self._col(row, "DOB"))) is not None:
            baby["dob"] = dob
        if (due := _time(self._col(row, "DueDay"))) is not None:
            baby["dueDay"] = due
        baby["gender"] = _flag(self._col(row, "Gender"))
        baby["pictureName"] = _text(self._col(row, "Picture"))
        baby["newFlage"] = "false"
        baby["timestamp"] = "1970-01-01 00:00:00 +0000"
        return baby

    def _picture(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "objectID": row["ID"],
            "fileName": _text(row["FileName"]),
            "mediaType": _number(self._col(row, "Type")),
            "deleted": "false",
            "timestamp": _stamp(row["Timestamp"]),
        }

    def _selection(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "objectID": row["ID"],
            "name": _text(row["Name"]),
            "desc": _text(self._col(row, "Description")),
            "newFlage": "false",
            "timestamp": _stamp(row["Timestamp"]),
        }

    def embedded(self, table: str, object_id: Any, kind: str) -> dict[str, Any] | None:
        """A pick-list entry as records embed it, or ``None`` when it isn't in the seed."""
        if not isinstance(object_id, str) or not object_id:
            return None
        if table not in self._selections:
            self._selections[table] = {}
            build = _SELECTIONS.get(table, ("", lambda _row: {}))[1]
            for row in self._rows(table):
                self._selections[table][row["ID"]] = {**self._selection(row), **build(row)}
        entry = self._selections[table].get(object_id)
        if entry is None:
            return None
        return {**entry, "BCObjectType": kind}

    def _activity(self, row: sqlite3.Row) -> dict[str, Any] | None:
        time = _time(row["Time"])
        if time is None:
            return None
        payload: dict[str, Any] = {
            "objectID": row["ID"],
            "time": time,
            "timestamp": _stamp(row["Timestamp"]),
            "note": _text(self._col(row, "Note")),
            "newFlage": "false",
            "pictureLoaded": "true",
            "pictureNote": list(self._pictures.get(row["ID"], [])),
        }
        baby_id = self._col(row, "BabyID")
        if baby_id is not None:
            baby = self._babies.get(baby_id)
            if baby is None:
                # The app keeps no record of a baby that isn't in its database.
                return None
            payload["baby"] = dict(baby)
        return payload


def _medication_selection(row: Row) -> dict[str, Any]:
    return {
        "amountPerTime": _number(row["AmountPerTime"]),
        "interval": _number(row["Interval"]),
        "unit": _text(row["Unit"]),
        "prescription": bool(row["IsPrescription"]),
    }


def _for_age(row: Row) -> dict[str, Any]:
    return {"forAge": _number(row["ForAge"])} if row["ForAge"] is not None else {}


def _other_feed_selection(row: Row) -> dict[str, Any]:
    return {"bottle": _flag(row["IsBottle"])}


def _nothing(_row: Row) -> dict[str, Any]:
    return {}


# Pick-list tables: the type each syncs as, and its own fields.
_SELECTIONS: dict[str, tuple[str, Callable[[Row], dict[str, Any]]]] = {
    "MedicineSelection": ("MedicationSelection", _medication_selection),
    "MilestoneSelection": ("MilestoneSelection", _for_age),
    "VaccineSelection": ("VaccineSelection", _for_age),
    "OtherFeedSelection": ("OtherFeedSelection", _other_feed_selection),
    "OtherActivityDesc": ("OtherActivityDescription", _nothing),
    "BathDesc": ("BathDesc", _nothing),
}


def _volume(row: sqlite3.Row, column: str = "Amount") -> dict[str, Any]:
    return _measure("VolumeMeasure", row[column], row["IsEnglishScale"])


def _diaper(_r: _Reader, row: sqlite3.Row) -> dict[str, Any]:
    keys = (
        ("status", "Status"),
        ("amount", "Amount"),
        ("peeColor", "PeeColor"),
        ("pooColor", "PooColor"),
        ("texture", "Texture"),
        ("flag", "Flag"),
    )
    return {key: _number(row[column]) for key, column in keys}


def _nursing(_r: _Reader, row: sqlite3.Row) -> dict[str, Any]:
    keys = (
        ("finishSide", "FinishSide"),
        ("leftDuration", "LeftDuration"),
        ("rightDuration", "RightDuration"),
        ("bothDuration", "BothDuration"),
    )
    return {key: _number(row[column]) for key, column in keys}


def _bottle(_r: _Reader, row: sqlite3.Row) -> dict[str, Any]:
    return {"amount": _volume(row)}


def _other_feed(r: _Reader, row: sqlite3.Row) -> dict[str, Any]:
    payload: dict[str, Any] = {"amount": _volume(row), "unit": _text(row["Unit"])}
    if (food := r.embedded("OtherFeedSelection", row["TypeID"], "OtherFeedSelection")) is not None:
        payload["feedType"] = food
    return payload


def _pump(_r: _Reader, row: sqlite3.Row) -> dict[str, Any]:
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


def _sleep(_r: _Reader, row: sqlite3.Row) -> dict[str, Any]:
    return {"duration": _number(row["Duration"])}


def _temperature(_r: _Reader, row: sqlite3.Row) -> dict[str, Any]:
    return {"temperature": _measure("TemperatureMeasure", row["Temp"], row["IsEnglishScale"])}


def _growth(_r: _Reader, row: sqlite3.Row) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    weight, length, head = (_number(row[c]) for c in ("Weight", "Length", "Head"))
    if weight > 0:
        payload["weight"] = _measure("WeightMeasure", weight, row["IsEnglishWeightScale"])
    english_length = row["IsEnglishLengthScale"]
    if length > 0:
        payload["length"] = _measure("LengthMeasure", length, english_length)
    if head > 0:
        payload["head"] = _measure("LengthMeasure", head, english_length)
    return payload


def _medication(r: _Reader, row: sqlite3.Row) -> dict[str, Any]:
    payload: dict[str, Any] = {"amount": _number(row["Amount"])}
    selection = r.embedded("MedicineSelection", row["MedID"], "MedicationSelection")
    if selection is not None:
        payload["medicationSelection"] = selection
    return payload


def _milestone(r: _Reader, row: sqlite3.Row) -> dict[str, Any]:
    kind = r.embedded("MilestoneSelection", row["MilestoneSelectionID"], "MilestoneSelection")
    return {"milestoneType": kind} if kind is not None else {}


def _vaccine(r: _Reader, row: sqlite3.Row) -> dict[str, Any]:
    kind = r.embedded("VaccineSelection", row["VaccID"], "VaccineSelection")
    return {"vaccineType": kind} if kind is not None else {}


def _other_activity(r: _Reader, row: sqlite3.Row) -> dict[str, Any]:
    payload: dict[str, Any] = {"duration": _number(row["Duration"])}
    desc = r.embedded("OtherActivityDesc", row["DescID"], "OtherActivityDescription")
    if desc is not None:
        payload["desc"] = desc
    return payload


def _bath(r: _Reader, row: sqlite3.Row) -> dict[str, Any]:
    kind = r.embedded("BathDesc", row["DescID"], "BathDesc")
    return {"bathType": kind} if kind is not None else {}


def _columns(*pairs: tuple[str, str]) -> Callable[[_Reader, sqlite3.Row], dict[str, Any]]:
    """Fields copied as they are, for types the library reads by their stored names."""

    def build(_r: _Reader, row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[column] for key, column in pairs if row[column] is not None}

    return build


def _base(_r: _Reader, _row: sqlite3.Row) -> dict[str, Any]:
    return {}


# Record tables: the type each syncs as, and its own fields.
_ACTIVITIES: dict[str, tuple[str, Callable[[_Reader, sqlite3.Row], dict[str, Any]]]] = {
    "Diaper": ("Diaper", _diaper),
    "Nursing": ("Nursing", _nursing),
    "Formula": ("Formula", _bottle),
    "Pumped": ("Pumped", _bottle),
    "OtherFeed": ("OtherFeed", _other_feed),
    "Pump": ("Pump", _pump),
    "Sleep": ("Sleep", _sleep),
    "Temperature": ("Temperature", _temperature),
    "Growth": ("Growth", _growth),
    "Medicine": ("Medication", _medication),
    "Milestone": ("Milestone", _milestone),
    "Vaccine": ("Vaccine", _vaccine),
    "OtherActivity": ("OtherActivity", _other_activity),
    "Bath": ("Bath", _bath),
    "Joy": ("Joy", _base),
    "Journal": ("Journal", _base),
    "Sick": ("Sick", _columns(("symptom", "Symptom"))),
    "Allergen": (
        "Allergen",
        _columns(
            ("sourceID", "SourceID"),
            ("reaction", "Reaction"),
            ("severity", "Severity"),
            ("notSure", "NotSure"),
        ),
    ),
    "HealthQuestion": (
        "HealthQuestion",
        _columns(
            ("question", "Question"), ("answer", "Answer"), ("doctorVisitID", "DoctorVisitID")
        ),
    ),
    "DoctorVisit": (
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
    ),
}
