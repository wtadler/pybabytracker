"""Exceptions raised by pybabytracker."""

from __future__ import annotations

from datetime import datetime, timedelta


class BabyTrackerError(Exception):
    """Base class for every error this library raises on purpose."""


class AuthenticationError(BabyTrackerError):
    """The email or password was rejected, or a fresh sign-in did not stick."""


class BabyTrackerConnectionError(BabyTrackerError):
    """The service could not be reached, timed out, or answered with a server error."""


class NoResponseError(BabyTrackerConnectionError):
    """The request got no HTTP answer (a timeout or a dropped connection)."""


class ProtocolError(BabyTrackerError):
    """The service answered, but not in the shape PROTOCOL.md describes."""


class TransactionDecodeError(BabyTrackerError):
    """A transaction's payload could not be decoded into a record."""


class WriteError(BabyTrackerError):
    """A write was refused before anything was sent."""


class NotSyncedError(WriteError):
    """The engine has never synced, so it doesn't know the babies or its own last number."""


class UnknownBabyError(WriteError):
    """No baby with that ID is known from sync."""


class NotOwnRecordError(WriteError):
    """The record wasn't created by this device, so this device may not delete it."""


class RecordNotFoundError(WriteError):
    """The record this device created is already deleted."""


class WriteOutcomeUnknownError(WriteError):
    """The previous write's outcome isn't known yet."""


class ObjectIdInUseError(ValueError):
    """The object_id passed to an add already belongs to another record."""


class TimerNotRunningError(WriteError):
    """The sleep isn't running (its duration isn't 0), so there's nothing to end."""


class WrongRecordTypeError(WriteError):
    """The record isn't of the type the write needs (ending a timer needs a Sleep)."""


class ValidationError(WriteError):
    """A value was refused before anything was sent.

    Each subclass carries what was wrong, so a caller can pick its own message (a translation key
    and placeholders, say) from the type and the attributes, without parsing ``str(error)``.
    """


class InvalidAmountError(ValidationError):
    """An amount is zero, negative, or not a finite number."""

    def __init__(self, amount: float) -> None:
        super().__init__(f"an amount must be a number more than zero, not {amount!r}")
        self.amount = amount


class AmountTooSmallError(ValidationError):
    """An amount rounds to nothing at the two decimals a volume is written with."""

    def __init__(self, amount: float) -> None:
        super().__init__(f"an amount of {amount!r} rounds to zero at two decimals")
        self.amount = amount


class MinutesTooLongError(ValidationError):
    """A side's minutes are more than a side can hold."""

    def __init__(self, minutes: int, maximum: int) -> None:
        super().__init__(f"{minutes} minutes on a side is more than the {maximum} a side can hold")
        self.minutes = minutes
        self.maximum = maximum


class EndBeforeStartError(ValidationError):
    """A sleep's end is at or before its start."""

    def __init__(self, start: datetime, end: datetime) -> None:
        super().__init__(f"a sleep's end ({end.isoformat()}) must be after its start")
        self.start = start
        self.end = end


class SleepTooLongError(ValidationError):
    """A sleep is longer than a sleep can be."""

    def __init__(self, length: timedelta, maximum: timedelta) -> None:
        super().__init__(f"a sleep of {length} is longer than the {maximum} a sleep can be")
        self.length = length
        self.maximum = maximum


class SleepStartOutOfRangeError(ValidationError):
    """A running sleep's start is too far ahead of now or too long ago."""

    def __init__(self, start: datetime, now: datetime) -> None:
        super().__init__(f"a sleep can't start at {start.isoformat()} when it is {now.isoformat()}")
        self.start = start
        self.now = now
