"""Typed records for every activity type, and the base64 JSON transaction codec.

A transaction payload is a JSON object whose keys are the columns of the matching
``EasyLog.db`` table in lowerCamelCase. Each model keeps the keys it doesn't know about in
``extra`` and remembers which known keys were present, so decoding then encoding a payload
returns the same JSON.
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import logging
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Any, ClassVar, NamedTuple, Self

from .exceptions import TransactionDecodeError
from .times import format_time, parse_time

OP_ADD = 0
OP_UPDATE = 1
OP_DELETE = 2

DIAPER_WET = 0
DIAPER_DIRTY = 1
DIAPER_MIXED = 2

_LOGGER = logging.getLogger(__name__)

_TIME = "time"
_BABY = "baby"
_VOLUME = "volume"

# The app's finishSide values for nursing and pumps: 0 is not set, 1 left, 2 right. Synced
# records agree: left-only sessions carry 1 and right-only ones 2, from iOS and Android alike.
# A pump with both sides used is usually saved with 0.
SIDE_NONE = 0
SIDE_LEFT = 1
SIDE_RIGHT = 2

UNIT_OUNCES = "oz"
UNIT_MILLILITERS = "ml"
UNIT_POUNDS = "lb"
UNIT_KILOGRAMS = "kg"
UNIT_INCHES = "in"
UNIT_CENTIMETERS = "cm"
UNIT_FAHRENHEIT = "F"
UNIT_CELSIUS = "C"


class Volume(NamedTuple):
    """An amount of liquid: a number and its unit, ``"oz"`` or ``"ml"``."""

    value: float
    unit: str


class Measurement(NamedTuple):
    """A weight, a length, or a temperature: a number and its unit (``"lb"``/``"kg"``,
    ``"in"``/``"cm"``, or ``"F"``/``"C"``)."""

    value: float
    unit: str


def _measurement(field: Any, is_english_scale: Any, units: tuple[str, str]) -> Measurement | None:
    """Resolve a growth field: the synced measure ``{"BCObjectType", "englishMeasure", "value"}``,
    or a plain number with the record's 0/1 scale column (the app's database shape).

    ``units`` is the (US, metric) pair. ``englishMeasure`` "true" (or the Android app's ``true``)
    means the US unit; anything else is metric. A plain number with no 0 or 1 scale has no unit,
    so it gives ``None``, as does anything without a number.
    """
    english_unit, metric_unit = units
    if isinstance(field, dict):
        number = _number(field.get("value"))
        if number is None:
            return None
        english_measure = field.get("englishMeasure")
        # Not ``in ("true", True)``: 1 == True, so that would read the number 1 as the US unit.
        english = english_measure == "true" or english_measure is True
        return Measurement(number, english_unit if english else metric_unit)
    number = _number(field)
    if number is None or isinstance(is_english_scale, bool) or is_english_scale not in (0, 1):
        return None
    return Measurement(number, english_unit if is_english_scale == 1 else metric_unit)


class VolumeMeasure:
    """A ``VolumeMeasure`` payload object: ``{"BCObjectType", "englishMeasure", "value"}``.

    The raw object is kept as is, so unknown keys and odd values survive a round trip.
    ``englishMeasure`` "true" means ounces and "false" means milliliters, both observed in captures
    (2026-10-26; a metric write carries the number as entered). The Android app sends the JSON
    booleans ``true`` and ``false`` instead. Any other value, or no key, is read as milliliters.
    """

    def __init__(
        self,
        value: Any = None,
        english_measure: str | None = "true",
        *,
        raw: dict[str, Any] | None = None,
    ) -> None:
        if raw is not None:
            self.raw = copy.deepcopy(raw)
            return
        self.raw = {"BCObjectType": "VolumeMeasure"}
        if english_measure is not None:
            self.raw["englishMeasure"] = english_measure
        self.raw["value"] = value

    @property
    def value(self) -> Any:
        return self.raw.get("value")

    @property
    def english_measure(self) -> Any:
        return self.raw.get("englishMeasure")

    @property
    def volume(self) -> Volume | None:
        """The number and unit, or ``None`` when the value isn't a number."""
        number = _number(self.value)
        if number is None:
            return None
        # Not ``in ("true", True)``: 1 == True, so that would read the number 1 as ounces.
        english = self.english_measure == "true" or self.english_measure is True
        unit = UNIT_OUNCES if english else UNIT_MILLILITERS
        return Volume(number, unit)

    def to_payload(self) -> dict[str, Any]:
        return copy.deepcopy(self.raw)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, VolumeMeasure) and self.raw == other.raw

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"VolumeMeasure({self.raw!r})"


def numeric_code(value: Any) -> int | None:
    """A numeric code, such as a diaper's ``status`` or a ``finishSide``, as an int.

    iOS sends these as numbers and the Android app as strings of digits (``"1"``), so both are
    read; anything else (a bool, a fraction, other text, a string too long to convert) is
    ``None``. Only ASCII digits count, which is all the apps send. The record keeps the raw
    value, so the round trip is unchanged.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        try:
            return int(value)
        except ValueError:  # past Python's int-string limit
            return None
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _volume_of(amount: Any, is_english_scale: Any) -> Volume | None:
    """Resolve a volume field. A plain number (older app versions) takes its unit from the
    record's ``isEnglishScale`` when that is 0 or 1, and has no unit otherwise."""
    if isinstance(amount, VolumeMeasure):
        return amount.volume
    number = _number(amount)
    if number is None or isinstance(is_english_scale, bool):
        return None
    if is_english_scale == 1:
        return Volume(number, UNIT_OUNCES)
    if is_english_scale == 0:
        return Volume(number, UNIT_MILLILITERS)
    return None


def _key(name: str, codec: str | None = None) -> Any:
    """An optional payload field stored under ``name``."""
    return field(default=None, metadata={"key": name, "codec": codec})


@dataclass(kw_only=True)
class Record:
    """Fields every record shares."""

    object_type: ClassVar[str] = ""

    object_id: str = field(metadata={"key": "objectID"})
    time: datetime | None = _key("time", _TIME)
    timestamp: datetime | None = _key("timestamp", _TIME)
    timezone: int | None = _key("timezone")
    note: str | None = _key("note")
    has_picture: int | None = _key("hasPicture")
    picture_note: list[Any] | None = _key("pictureNote")
    new_flag: str | None = _key("newFlage")
    picture_loaded: str | None = _key("pictureLoaded")
    baby: Baby | None = _key("baby", _BABY)
    extra: dict[str, Any] = field(default_factory=dict)
    # Known payload keys that were present when decoded, so absent and null stay distinct.
    present: frozenset[str] = field(default_factory=frozenset, repr=False, compare=False)
    # The payload's key order when decoded, so re-encoding gives the same bytes.
    key_order: tuple[str, ...] = field(default=(), repr=False, compare=False)

    @property
    def record_type(self) -> str:
        return self.object_type

    @property
    def baby_id(self) -> str | None:
        if self.baby is not None:
            return self.baby.object_id
        raw = self.extra.get("babyID")
        return raw if isinstance(raw, str) else None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        kwargs: dict[str, Any] = {}
        present: set[str] = set()
        known = {"BCObjectType"}
        for f in fields(cls):
            key = f.metadata.get("key")
            if key is None:
                continue
            known.add(key)
            if key not in payload:
                continue
            present.add(key)
            try:
                kwargs[f.name] = _decode_value(f.metadata.get("codec"), payload[key])
            except (ValueError, TypeError) as err:
                raise TransactionDecodeError(f"bad value for {key!r}: {err}") from err
        if not isinstance(kwargs.get("object_id"), str):
            raise TransactionDecodeError("payload has no string objectID")
        extra = {k: copy.deepcopy(v) for k, v in payload.items() if k not in known}
        return cls(**kwargs, extra=extra, present=frozenset(present), key_order=tuple(payload))

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"BCObjectType": self.object_type}
        for f in fields(self):
            key = f.metadata.get("key")
            if key is None:
                continue
            value = getattr(self, f.name)
            if value is None and key not in self.present:
                continue
            payload[key] = _encode_value(f.metadata.get("codec"), value)
        payload.update(copy.deepcopy(self.extra))
        return _in_order(payload, self.key_order)

    def describe(self) -> dict[str, Any]:
        """The type-specific fields that are set, by Python field name."""
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if f.metadata.get("key") is not None
            and f.name not in _BASE_FIELD_NAMES
            and getattr(self, f.name) is not None
        }


_BASE_FIELD_NAMES = {f.name for f in fields(Record)}


def _in_order(payload: dict[str, Any], order: tuple[str, ...]) -> dict[str, Any]:
    """``payload`` with the keys in ``order`` first, then any others in their own order."""
    if not order:
        return payload
    ordered = {key: payload[key] for key in order if key in payload}
    ordered.update((key, value) for key, value in payload.items() if key not in ordered)
    return ordered


def _decode_value(codec: str | None, value: Any) -> Any:
    if value is None:
        return None
    if codec == _TIME:
        if not isinstance(value, str):
            raise TypeError("time must be a string")
        return parse_time(value)
    if codec == _BABY:
        if not isinstance(value, dict):
            raise TypeError("baby must be an object")
        return Baby.from_payload(value)
    if codec == _VOLUME and isinstance(value, dict):
        return VolumeMeasure(raw=value)
    return copy.deepcopy(value)


def _encode_value(codec: str | None, value: Any) -> Any:
    if value is None:
        return None
    if codec == _TIME:
        return format_time(value)
    if codec == _BABY or isinstance(value, VolumeMeasure):
        return value.to_payload()
    return copy.deepcopy(value)


RECORD_TYPES: dict[str, type[Record]] = {}


def _register[R: type[Record]](cls: R) -> R:
    RECORD_TYPES[cls.__name__] = cls
    return cls


def _opt(name: str) -> Any:
    return _key(name)


def _vol(name: str) -> Any:
    """A volume field: a ``VolumeMeasure`` from the sync service, or a plain number."""
    return _key(name, _VOLUME)


@_register
@dataclass(kw_only=True)
class Baby(Record):
    object_type: ClassVar[str] = "Baby"

    name: str | None = _opt("name")
    dob: str | None = _opt("dob")
    due_day: str | None = _opt("dueDay")
    gender: str | None = _opt("gender")
    picture_name: str | None = _opt("pictureName")


@_register
@dataclass(kw_only=True)
class Diaper(Record):
    object_type: ClassVar[str] = "Diaper"

    status: int | None = _opt("status")
    pee_color: int | None = _opt("peeColor")
    poo_color: int | None = _opt("pooColor")
    amount: int | None = _opt("amount")
    texture: int | None = _opt("texture")
    flag: int | None = _opt("flag")


@_register
@dataclass(kw_only=True)
class Nursing(Record):
    object_type: ClassVar[str] = "Nursing"

    desc_id: str | None = _opt("descID")
    finish_side: int | None = _opt("finishSide")
    left_duration: int | None = _opt("leftDuration")
    right_duration: int | None = _opt("rightDuration")
    both_duration: int | None = _opt("bothDuration")
    is_detailed: str | None = _opt("isDetailed")
    state: int | None = _opt("state")
    pair_session_state: int | None = _opt("pairSessionState")


@_register
@dataclass(kw_only=True)
class NursingSession(Nursing):
    """A nursing session from the Android app's timer, which syncs under this type name. It
    has the ``Nursing`` fields plus ``detailed`` and the per-side timestamps, kept in ``extra``."""

    object_type: ClassVar[str] = "NursingSession"


@_register
@dataclass(kw_only=True)
class Formula(Record):
    object_type: ClassVar[str] = "Formula"

    desc_id: str | None = _opt("descID")
    amount: VolumeMeasure | float | None = _vol("amount")
    is_english_scale: int | None = _opt("isEnglishScale")

    @property
    def volume(self) -> Volume | None:
        return _volume_of(self.amount, self.is_english_scale)


@_register
@dataclass(kw_only=True)
class Pumped(Record):
    object_type: ClassVar[str] = "Pumped"

    desc_id: str | None = _opt("descID")
    amount: VolumeMeasure | float | None = _vol("amount")
    is_english_scale: int | None = _opt("isEnglishScale")
    pump_id: str | None = _opt("pumpID")

    @property
    def volume(self) -> Volume | None:
        return _volume_of(self.amount, self.is_english_scale)


@_register
@dataclass(kw_only=True)
class OtherFeed(Record):
    """A solid food or other feed. The synced payload embeds the food as ``feedType`` (with its
    ``name``) and gives the amount as a ``VolumeMeasure`` whose number counts ``unit``, which is
    free text such as "Oz", "Pieces", or "Tsp".

    Read the amount with ``quantity`` and ``unit``, not ``amount.volume``: that reads
    ``englishMeasure`` as ounces or milliliters, which is wrong when ``unit`` is "Pieces"."""

    object_type: ClassVar[str] = "OtherFeed"

    feed_type: dict[str, Any] | None = _opt("feedType")
    desc: dict[str, Any] | None = _opt("desc")
    # The app's database columns, kept for payloads that carry them.
    desc_id: str | None = _opt("descID")
    type_id: str | None = _opt("typeID")
    amount: VolumeMeasure | float | None = _vol("amount")
    is_english_scale: int | None = _opt("isEnglishScale")
    unit: str | None = _opt("unit")

    @property
    def food_name(self) -> str | None:
        """The embedded food's name, when it has one."""
        return _selection_name(self.feed_type)

    @property
    def quantity(self) -> float | None:
        """How many of ``unit`` were eaten, from the measure or a plain number."""
        if isinstance(self.amount, VolumeMeasure):
            return _number(self.amount.value)
        return _number(self.amount)


@_register
@dataclass(kw_only=True)
class Pump(Record):
    """A pumping session. It belongs to the account, not a baby: the live service's pumps carry no
    ``baby`` key. A pump that does carry one still decodes, and the key is kept in ``extra`` so
    the round trip stays exact, but it is never read as the pump's baby."""

    object_type: ClassVar[str] = "Pump"

    baby: Baby | None = field(default=None, init=False, repr=False)
    finish_side: int | None = _opt("finishSide")
    sides: int | None = _opt("sides")
    amount: VolumeMeasure | float | None = _vol("amount")
    is_english_scale: int | None = _opt("isEnglishScale")
    label: str | None = _opt("label")
    pumped_id: str | None = _opt("pumpedID")
    left_amount: VolumeMeasure | float | None = _vol("leftAmount")
    right_amount: VolumeMeasure | float | None = _vol("rightAmount")
    left_duration: int | None = _opt("leftDuration")
    right_duration: int | None = _opt("rightDuration")

    @property
    def volume(self) -> Volume | None:
        return _volume_of(self.amount, self.is_english_scale)

    @property
    def left_volume(self) -> Volume | None:
        return _volume_of(self.left_amount, self.is_english_scale)

    @property
    def right_volume(self) -> Volume | None:
        return _volume_of(self.right_amount, self.is_english_scale)

    @property
    def baby_id(self) -> str | None:
        # A pump belongs to the account: a stray baby or babyID key is kept in extra, never read.
        return None


@_register
@dataclass(kw_only=True)
class Sleep(Record):
    object_type: ClassVar[str] = "Sleep"

    duration: int | None = _opt("duration")
    location_id: str | None = _opt("locationID")
    desc_id: str | None = _opt("descID")

    @property
    def is_running(self) -> bool:
        """Whether the sleep timer is still going: the app posts a duration of 0 at the start."""
        return self.duration == 0


@_register
@dataclass(kw_only=True)
class Growth(Record):
    """A growth measurement. The apps sync ``weight`` as a ``WeightMeasure`` and ``length`` and
    ``head`` as ``LengthMeasure`` objects; the app's database stores plain numbers with
    ``isEnglishWeightScale`` and ``isEnglishLengthScale``. The fields keep whichever arrived, and
    the ``*_reading`` properties read both. A value is as sent: the apps store a 32-bit float."""

    object_type: ClassVar[str] = "Growth"

    weight: dict[str, Any] | float | None = _opt("weight")
    length: dict[str, Any] | float | None = _opt("length")
    head: dict[str, Any] | float | None = _opt("head")
    is_english_length_scale: int | None = _opt("isEnglishLengthScale")
    is_english_weight_scale: int | None = _opt("isEnglishWeightScale")

    @property
    def weight_reading(self) -> Measurement | None:
        """The weight in pounds or kilograms, or ``None`` when there isn't one."""
        return _measurement(
            self.weight, self.is_english_weight_scale, (UNIT_POUNDS, UNIT_KILOGRAMS)
        )

    @property
    def length_reading(self) -> Measurement | None:
        """The length in inches or centimeters, or ``None`` when there isn't one."""
        return _measurement(
            self.length, self.is_english_length_scale, (UNIT_INCHES, UNIT_CENTIMETERS)
        )

    @property
    def head_reading(self) -> Measurement | None:
        """The head circumference in inches or centimeters, or ``None`` when there isn't one."""
        return _measurement(
            self.head, self.is_english_length_scale, (UNIT_INCHES, UNIT_CENTIMETERS)
        )


@_register
@dataclass(kw_only=True)
class Medicine(Record):
    object_type: ClassVar[str] = "Medicine"

    med_id: str | None = _opt("medID")
    amount: float | None = _opt("amount")


def _selection_name(selection: object) -> str | None:
    """The ``name`` of an embedded selection object, when it has a string one."""
    name = selection.get("name") if isinstance(selection, dict) else None
    return name if isinstance(name, str) else None


@_register
@dataclass(kw_only=True)
class Medication(Record):
    """A dose of a medicine, as the apps sync one. The medicine itself (``name``, ``unit``,
    ``amountPerTime``, ``interval``, ``objectID``) is embedded as ``medicationSelection``."""

    object_type: ClassVar[str] = "Medication"

    medication_selection: dict[str, Any] | None = _opt("medicationSelection")
    amount: float | None = _opt("amount")

    @property
    def medicine_name(self) -> str | None:
        """The embedded medicine's name, when it has one."""
        return _selection_name(self.medication_selection)


@_register
@dataclass(kw_only=True)
class Temperature(Record):
    """A temperature. The apps sync it as ``temperature``, a ``TemperatureMeasure``; the app's
    database stores ``temp`` with ``isEnglishScale``. ``reading`` reads either. The value is as
    sent: the apps store a 32-bit float, so 100.4 can arrive as 100.4000015258789."""

    object_type: ClassVar[str] = "Temperature"

    temperature: dict[str, Any] | None = _opt("temperature")
    temp: float | None = _opt("temp")
    is_english_scale: int | None = _opt("isEnglishScale")

    @property
    def reading(self) -> Measurement | None:
        """The temperature in Fahrenheit or Celsius, or ``None`` when there isn't one."""
        units = (UNIT_FAHRENHEIT, UNIT_CELSIUS)
        if self.temperature is not None:
            return _measurement(self.temperature, None, units)
        return _measurement(self.temp, self.is_english_scale, units)


@_register
@dataclass(kw_only=True)
class Vaccine(Record):
    """A vaccine given. The apps sync the vaccine itself (``name``, ``forAge``, ``objectID``)
    embedded as ``vaccineType``. ``vaccID`` and ``docVisitID`` are the local database's columns."""

    object_type: ClassVar[str] = "Vaccine"

    vaccine_type: dict[str, Any] | None = _opt("vaccineType")
    vacc_id: str | None = _opt("vaccID")
    doc_visit_id: str | None = _opt("docVisitID")

    @property
    def vaccine_name(self) -> str | None:
        """The embedded vaccine's name, when it has one."""
        return _selection_name(self.vaccine_type)


@_register
@dataclass(kw_only=True)
class Milestone(Record):
    """A milestone reached. The apps sync the milestone itself (``name``, ``forAge``,
    ``objectID``) embedded as ``milestoneType``. ``milestoneSelectionID`` is the local database's
    column."""

    object_type: ClassVar[str] = "Milestone"

    milestone_type: dict[str, Any] | None = _opt("milestoneType")
    has_new_type: bool | str | None = _opt("hasNewType")
    milestone_selection_id: str | None = _opt("milestoneSelectionID")

    @property
    def milestone_name(self) -> str | None:
        """The embedded milestone's name, when it has one."""
        return _selection_name(self.milestone_type)


@_register
@dataclass(kw_only=True)
class MilestoneSelection(Record):
    """A milestone a user added to the app's pick list, synced as a transaction of its own (app:
    ``MilestoneSelection``). ``babyID`` is set when it was added for one baby only; it stays in
    ``extra``, where ``baby_id`` reads it. The built-in milestones never sync: see
    ``BUILT_IN_MILESTONES``."""

    object_type: ClassVar[str] = "MilestoneSelection"

    name: str | None = _opt("name")
    desc: str | None = _opt("desc")
    for_age: int | None = _opt("forAge")
    used: bool | str | None = _opt("used")


@_register
@dataclass(kw_only=True)
class MedicationSelection(Record):
    """A medicine on the app's pick list, synced as a transaction of its own (app:
    ``MedicationSelection``). The prescription flag is ``prescription`` on Android and
    ``isPresciption`` on iOS; both stay as sent (the iOS one in ``extra``)."""

    object_type: ClassVar[str] = "MedicationSelection"

    name: str | None = _opt("name")
    desc: str | None = _opt("desc")
    unit: str | None = _opt("unit")
    amount_per_time: float | None = _opt("amountPerTime")
    interval: int | None = _opt("interval")
    prescription: bool | str | None = _opt("prescription")


@_register
@dataclass(kw_only=True)
class VaccineSelection(Record):
    """A vaccine a user added to the app's pick list, synced as a transaction of its own (app:
    ``VaccineSelection``). The built-in vaccines never sync: see ``BUILT_IN_VACCINES``."""

    object_type: ClassVar[str] = "VaccineSelection"

    name: str | None = _opt("name")
    desc: str | None = _opt("desc")
    for_age: int | None = _opt("forAge")


@_register
@dataclass(kw_only=True)
class OtherActivity(Record):
    """Any other activity (tummy time, a walk, a bath logged as an activity). The apps sync the
    activity's kind (``name``, ``objectID``) embedded as ``desc``. ``descID`` and ``locationID``
    are the local database's columns."""

    object_type: ClassVar[str] = "OtherActivity"

    desc: dict[str, Any] | None = _opt("desc")
    desc_id: str | None = _opt("descID")
    location_id: str | None = _opt("locationID")
    duration: int | None = _opt("duration")

    @property
    def activity_name(self) -> str | None:
        """The embedded activity kind's name, when it has one."""
        return _selection_name(self.desc)


@_register
@dataclass(kw_only=True)
class Bath(Record):
    object_type: ClassVar[str] = "Bath"

    desc_id: str | None = _opt("descID")


@_register
@dataclass(kw_only=True)
class Allergen(Record):
    object_type: ClassVar[str] = "Allergen"

    source_id: str | None = _opt("sourceID")
    reaction: str | None = _opt("reaction")
    severity: int | None = _opt("severity")
    not_sure: int | None = _opt("notSure")


@_register
@dataclass(kw_only=True)
class HealthQuestion(Record):
    object_type: ClassVar[str] = "HealthQuestion"

    question: str | None = _opt("question")
    answer: str | None = _opt("answer")
    doctor_visit_id: str | None = _opt("doctorVisitID")


@_register
@dataclass(kw_only=True)
class Sick(Record):
    object_type: ClassVar[str] = "Sick"

    symptom: str | None = _opt("symptom")


@_register
@dataclass(kw_only=True)
class DoctorVisit(Record):
    object_type: ClassVar[str] = "DoctorVisit"

    is_appointment: int | None = _opt("isAppointment")
    visit_type: int | None = _opt("visitType")
    doctor_id: str | None = _opt("doctorID")
    doctor_note: str | None = _opt("doctorNote")
    symptom: str | None = _opt("symptom")
    growth_id: str | None = _opt("growthID")
    sick_id: str | None = _opt("sickID")


@_register
@dataclass(kw_only=True)
class Joy(Record):
    object_type: ClassVar[str] = "Joy"


@_register
@dataclass(kw_only=True)
class Journal(Record):
    object_type: ClassVar[str] = "Journal"


@dataclass(kw_only=True)
class UnknownRecord(Record):
    """A record of a type this library doesn't model. The raw payload is kept as is."""

    type_name: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def record_type(self) -> str:
        return self.type_name

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        object_id = payload.get("objectID")
        if not isinstance(object_id, str):
            raise TransactionDecodeError("payload has no string objectID")
        type_name = payload.get("BCObjectType")
        return cls(
            object_id=object_id,
            time=_try_parse_time(payload.get("time")),
            timestamp=_try_parse_time(payload.get("timestamp")),
            type_name=type_name if isinstance(type_name, str) else "",
            raw=copy.deepcopy(payload),
        )

    def to_payload(self) -> dict[str, Any]:
        return copy.deepcopy(self.raw)

    def describe(self) -> dict[str, Any]:
        return {}


def _try_parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_time(value)
    except ValueError:
        return None


def decode_payload(payload: dict[str, Any]) -> Record:
    """Turn a payload dict into the matching record, or an ``UnknownRecord``.

    A known type whose values don't fit the model (an odd time format, say) is also kept as an
    ``UnknownRecord`` with a warning, as long as it has an ``objectID``, so nothing is lost and
    the round trip stays exact.
    """
    cls = RECORD_TYPES.get(str(payload.get("BCObjectType")))
    if cls is None:
        return UnknownRecord.from_payload(payload)
    try:
        return cls.from_payload(payload)
    except TransactionDecodeError as err:
        if not isinstance(payload.get("objectID"), str):
            raise
        _LOGGER.warning(
            "Keeping %s record %s raw because it didn't decode: %s",
            cls.__name__,
            payload["objectID"],
            err,
        )
        return UnknownRecord.from_payload(payload)


def decode_transaction(transaction: str) -> Record:
    """Decode a transaction's base64 JSON string into a record."""
    try:
        raw = base64.b64decode(transaction, validate=True)
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, TypeError) as err:
        raise TransactionDecodeError(f"transaction is not base64 JSON: {err}") from err
    if not isinstance(payload, dict):
        raise TransactionDecodeError("transaction JSON is not an object")
    return decode_payload(payload)


def encode_transaction(record: Record) -> str:
    """Encode a record as the base64 JSON string the service stores."""
    text = json.dumps(record.to_payload(), ensure_ascii=False, separators=(",", ":"))
    return base64.b64encode(text.encode("utf-8")).decode("ascii")
