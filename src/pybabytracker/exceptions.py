"""Exceptions raised by pybabytracker."""

from __future__ import annotations


class BabyTrackerError(Exception):
    """Base class for every error this library raises on purpose."""


class AuthenticationError(BabyTrackerError):
    """The email or password was rejected, or a fresh sign-in did not stick."""


class BabyTrackerConnectionError(BabyTrackerError):
    """The service could not be reached, timed out, or answered with a server error."""


class ProtocolError(BabyTrackerError):
    """The service answered, but not in the shape PROTOCOL.md describes."""


class TransactionDecodeError(BabyTrackerError):
    """A transaction's payload could not be decoded into a record."""
