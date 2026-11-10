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


class DownloadRefusedError(ProtocolError):
    """A download this library won't take: a body over its size cap."""


class AccountChangedError(BabyTrackerError):
    """A sign-in opened a different sync group (``AccountID``) from the one already in use.

    The app compares the ``AccountID`` from each sign-in with the one it stored, and stops
    syncing when they differ: the group was reset or recreated, so the saved records and progress
    belong to the old one. The client raises this for a sign-in that differs from its own earlier
    one, before it retries the request that needed the sign-in, and the engine raises it when the
    ``AccountID`` differs from the one in its state. Join the new group with a new
    ``BabyTrackerClient`` and a new engine (the aiohttp session can be reused).
    Records read before the sign-in are not undone.
    """

    def __init__(
        self, saved: int | None, signed_in: int | None, *, message: str | None = None
    ) -> None:
        super().__init__(
            message
            or f"the account's sync group changed (AccountID {saved} is now {signed_in}); "
            "use a new client and a new engine to join it"
        )
        self.saved = saved
        self.signed_in = signed_in


class AccountResetError(AccountChangedError):
    """The service refused this device because the account's sync group was reset in the app.

    After a reset, the service refuses every device that joined before it at sign-in, whatever
    the password, with a 401 that says the account has been reset. The app joins again as a new
    device, with a new device ID. Do the same: a new device UUID, a new ``BabyTrackerClient``, and
    a new engine. ``saved`` is the ``AccountID`` this client signed in to before, if any;
    ``signed_in`` is ``None``, since the new group's isn't known until a new device signs in.
    """

    def __init__(self, saved: int | None) -> None:
        super().__init__(
            saved,
            None,
            message="the account's sync group was reset in the app, so this device is refused; "
            "join again with a new device UUID, a new client and a new engine",
        )


class TransactionDecodeError(BabyTrackerError):
    """A transaction's payload could not be decoded into a record."""


class WriteError(BabyTrackerError):
    """A write was refused before anything was sent."""


class NotSyncedError(WriteError):
    """The engine has never synced, so it doesn't know the babies or its own last number."""


class UnknownBabyError(WriteError):
    """No baby with that ID is known from sync."""


class UnknownSelectionError(WriteError):
    """No entry of a pick list (a milestone, a vaccine, a medicine, an activity type, a food) has
    that name or ID.

    ``what`` says which list: "milestone", "vaccine", "medicine", "activity", or "food".
    """

    def __init__(self, what: str, message: str) -> None:
        super().__init__(message)
        self.what = what


class UnknownMilestoneError(UnknownSelectionError):
    """No milestone with that name or ID is built in or known from sync."""

    def __init__(self, message: str) -> None:
        super().__init__("milestone", message)


class UnknownMedicineError(UnknownSelectionError):
    """No medicine with that name or ID is known from sync."""

    def __init__(self, message: str) -> None:
        super().__init__("medicine", message)


class UnknownActivityError(UnknownSelectionError):
    """No activity type with that name or ID is built in or known from sync."""

    def __init__(self, message: str) -> None:
        super().__init__("activity", message)


class UnknownFoodError(UnknownSelectionError):
    """No food with that name or ID is built in or known from sync."""

    def __init__(self, message: str) -> None:
        super().__init__("food", message)


class UnknownVaccineError(UnknownSelectionError):
    """No vaccine with that name or ID is built in or known from sync."""

    def __init__(self, message: str) -> None:
        super().__init__("vaccine", message)


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


class InvalidSelectionNameError(ValidationError, ValueError):
    """A pick-list name or ID is empty or isn't text."""

    def __init__(self, what: str, value: object) -> None:
        super().__init__(f"{what} must be a name or an ID, not {value!r}")
        self.what = what
        self.value = value


class AmbiguousSelectionError(ValidationError):
    """A name matches more than one entry of a pick list, so it can't say which is meant.

    Pass the ``object_id`` of the one wanted instead. ``object_ids`` lists the matches.
    """

    def __init__(self, what: str, wanted: str, object_ids: tuple[str, ...]) -> None:
        super().__init__(
            f"{what} {wanted!r} matches {len(object_ids)} entries ({', '.join(object_ids)}): "
            "pass the ID of the one you mean"
        )
        self.what = what
        self.wanted = wanted
        self.object_ids = object_ids


class NoUsualDoseError(ValidationError, ValueError):
    """A dose was logged without an amount for a medicine that has no usual dose to default to."""

    def __init__(self, medicine: str | None = None) -> None:
        super().__init__("this medicine has no usual dose, so give an amount")
        self.medicine = medicine


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
