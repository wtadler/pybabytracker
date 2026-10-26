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

_LOGGER = logging.getLogger(__name__)

_TIME = "time"
_BABY = "baby"
_VOLUME = "volume"

UNIT_OUNCES = "oz"
UNIT_MILLILITERS = "ml"


class Volume(NamedTuple):
    """An amount of liquid: a number and its unit, ``"oz"`` or ``"ml"``."""

    value: float
    unit: str


class VolumeMeasure:
    """A ``VolumeMeasure`` payload object: ``{"BCObjectType", "englishMeasure", "value"}``.

    The raw object is kept as is, so unknown keys and odd values survive a round trip.
    ``englishMeasure`` "true" means ounces; anything else, or no key, means milliliters. That is an
    assumption: only "true" has been observed in captures.
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
        unit = UNIT_OUNCES if self.english_measure == "true" else UNIT_MILLILITERS
        return Volume(number, unit)

    def to_payload(self) -> dict[str, Any]:
        return copy.deepcopy(self.raw)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, VolumeMeasure) and self.raw == other.raw

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"VolumeMeasure({self.raw!r})"


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
    object_type: ClassVar[str] = "OtherFeed"

    desc_id: str | None = _opt("descID")
    type_id: str | None = _opt("typeID")
    amount: float | None = _opt("amount")
    is_english_scale: int | None = _opt("isEnglishScale")
    unit: str | None = _opt("unit")


@_register
@dataclass(kw_only=True)
class Pump(Record):
    object_type: ClassVar[str] = "Pump"

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
    object_type: ClassVar[str] = "Growth"

    weight: float | None = _opt("weight")
    length: float | None = _opt("length")
    head: float | None = _opt("head")
    is_english_length_scale: int | None = _opt("isEnglishLengthScale")
    is_english_weight_scale: int | None = _opt("isEnglishWeightScale")


@_register
@dataclass(kw_only=True)
class Medicine(Record):
    object_type: ClassVar[str] = "Medicine"

    med_id: str | None = _opt("medID")
    amount: float | None = _opt("amount")


@_register
@dataclass(kw_only=True)
class Temperature(Record):
    object_type: ClassVar[str] = "Temperature"

    temp: float | None = _opt("temp")
    is_english_scale: int | None = _opt("isEnglishScale")


@_register
@dataclass(kw_only=True)
class Vaccine(Record):
    object_type: ClassVar[str] = "Vaccine"

    vacc_id: str | None = _opt("vaccID")
    doc_visit_id: str | None = _opt("docVisitID")


@_register
@dataclass(kw_only=True)
class Milestone(Record):
    object_type: ClassVar[str] = "Milestone"

    milestone_selection_id: str | None = _opt("milestoneSelectionID")


@_register
@dataclass(kw_only=True)
class OtherActivity(Record):
    object_type: ClassVar[str] = "OtherActivity"

    desc_id: str | None = _opt("descID")
    location_id: str | None = _opt("locationID")
    duration: int | None = _opt("duration")


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
