"""Async client for the Nighp Baby Tracker sync service."""

from ._version import __version__
from .client import BabyTrackerClient, Device, RawTransaction
from .engine import ApplyResult, SyncEngine, SyncResult
from .exceptions import (
    AuthenticationError,
    BabyTrackerConnectionError,
    BabyTrackerError,
    ProtocolError,
    TransactionDecodeError,
)
from .models import (
    OP_ADD,
    OP_DELETE,
    OP_UPDATE,
    Baby,
    Diaper,
    Record,
    Sleep,
    UnknownRecord,
    decode_transaction,
    encode_transaction,
    format_time,
    from_unix,
    parse_time,
    to_unix,
)

__all__ = [
    "OP_ADD",
    "OP_DELETE",
    "OP_UPDATE",
    "ApplyResult",
    "AuthenticationError",
    "Baby",
    "BabyTrackerClient",
    "BabyTrackerConnectionError",
    "BabyTrackerError",
    "Device",
    "Diaper",
    "ProtocolError",
    "RawTransaction",
    "Record",
    "Sleep",
    "SyncEngine",
    "SyncResult",
    "TransactionDecodeError",
    "UnknownRecord",
    "__version__",
    "decode_transaction",
    "encode_transaction",
    "format_time",
    "from_unix",
    "parse_time",
    "to_unix",
]
