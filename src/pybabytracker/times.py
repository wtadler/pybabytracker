"""Time formats used by the sync service and the app's local database."""

from __future__ import annotations

from datetime import UTC, datetime

_PAYLOAD_FORMAT = "%Y-%m-%d %H:%M:%S %z"


def parse_time(value: str) -> datetime:
    """Parse a payload time such as ``2026-10-25 15:40:49 +0000`` into an aware datetime."""
    parsed = datetime.strptime(value, _PAYLOAD_FORMAT)
    return parsed


def format_time(value: datetime) -> str:
    """Format an aware datetime the way payloads write times."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.strftime(_PAYLOAD_FORMAT)


def from_unix(seconds: float) -> datetime:
    """Convert a database ``Time`` column (Unix seconds) to an aware UTC datetime."""
    return datetime.fromtimestamp(seconds, tz=UTC)


def to_unix(value: datetime) -> float:
    """Convert an aware datetime to Unix seconds."""
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.timestamp()
