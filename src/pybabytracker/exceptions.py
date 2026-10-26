"""Exceptions raised by pybabytracker."""

from __future__ import annotations


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
