"""Checks on what a caller asks to write, before anything is sent.

Each check raises a ``ValidationError`` subclass that carries what was wrong, so a caller can
choose its own message from the type and attributes. The limits are the ones the builders hold
(``MAX_MINUTES_PER_SIDE``, ``MAX_SLEEP``, ``START_SKEW``): these functions let a caller refuse a
bad request at its own door, in its own words, before it reaches a builder's ``ValueError``.
"""

from __future__ import annotations

import math
from datetime import datetime

from .builders import MAX_MINUTES_PER_SIDE, MAX_SLEEP, START_SKEW
from .exceptions import (
    AmountTooSmallError,
    EndBeforeStartError,
    InvalidAmountError,
    MinutesTooLongError,
    SleepStartOutOfRangeError,
    SleepTooLongError,
)


def check_amount(amount: float) -> float:
    """Return ``amount`` if it is a finite number that is still more than zero when written.

    Raises ``InvalidAmountError`` for zero, a negative, or a non-finite number, and
    ``AmountTooSmallError`` for one that rounds to zero at the two decimals a volume is written
    with.
    """
    if not math.isfinite(amount) or amount <= 0:
        raise InvalidAmountError(amount)
    if round(amount, 2) <= 0:
        raise AmountTooSmallError(amount)
    return amount


def check_pump_amounts(left: float | None, right: float | None) -> None:
    """Check a pump given by side amounts (``None`` for a side not given).

    A given side must be finite and not negative (``InvalidAmountError``). Together the sides must
    be more than zero (``InvalidAmountError``), and each side is written to two decimals, so
    sides that round to nothing together are ``AmountTooSmallError``.
    """
    for side in (left, right):
        if side is not None and (not math.isfinite(side) or side < 0):
            raise InvalidAmountError(side)
    check_amount((left or 0) + (right or 0))
    if round(left or 0, 2) + round(right or 0, 2) <= 0:
        raise AmountTooSmallError((left or 0) + (right or 0))


def check_minutes(*minutes: int) -> None:
    """Raise ``MinutesTooLongError`` if any side's minutes are more than a side can hold."""
    longest = max(minutes, default=0)
    if longest > MAX_MINUTES_PER_SIDE:
        raise MinutesTooLongError(longest, MAX_MINUTES_PER_SIDE)


def check_end_after_start(start: datetime, end: datetime) -> None:
    """Raise ``EndBeforeStartError`` unless ``end`` is after ``start``."""
    if end <= start:
        raise EndBeforeStartError(start, end)


def check_sleep_range(start: datetime, end: datetime) -> None:
    """Check a finished sleep: it ends after it starts and lasts at most 24 hours.

    Raises ``EndBeforeStartError``, then ``SleepTooLongError``.
    """
    check_end_after_start(start, end)
    if end - start > MAX_SLEEP:
        raise SleepTooLongError(end - start, MAX_SLEEP)


def check_sleep_start(start: datetime, now: datetime) -> None:
    """Raise ``SleepStartOutOfRangeError`` for a running sleep's start too far from ``now``.

    A start may be up to a minute ahead (clocks disagree a little) and up to 24 hours back.
    """
    if start - now > START_SKEW or now - start > MAX_SLEEP:
        raise SleepStartOutOfRangeError(start, now)
