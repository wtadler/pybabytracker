"""Nursing and pump timers: the state a client keeps while a session runs.

The app doesn't sync a nursing timer while it runs: one finished record arrives when it stops
(PROTOCOL.md, "Timers"). So a client that offers a nursing timer keeps it itself and writes one
``add_nursing`` at the stop. ``NursingTimer`` is that state, kept as an immutable value: starting,
switching sides, and reading the minutes to write all return or compute from a new value, and
``to_dict``/``from_dict`` give a form to save between runs. Storing it, and deciding when to
start, switch, or stop, stay with the caller.

Each side's time is rounded to whole minutes, half up. A session stopped so soon that both sides
round to 0 (under 30 seconds) is written as 1 minute on the side it ended on, rather than
refused: the press was deliberate. A side longer than 240 minutes (a forgotten timer) is written
as 240, the most a record takes, and logged.

``PumpTimer`` is the same for a pump session, which belongs to the account: one length, written
as equal minutes on both sides, rounded and capped like a nursing side, and never under 1.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from .builders import MAX_MINUTES_PER_SIDE

_LOGGER = logging.getLogger(__name__)

LEFT = "left"
RIGHT = "right"
SIDES = (LEFT, RIGHT)


def other_side(side: str) -> str:
    """Return the other side of the body."""
    return RIGHT if side == LEFT else LEFT


def _whole_minutes(seconds: float) -> int:
    return math.floor(seconds / 60 + 0.5)


def _aware_time(value: object, name: str) -> datetime:
    """Read a saved ISO time, which must carry an offset. Raises ``ValueError`` otherwise."""
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError as err:
        raise ValueError(f"bad {name}") from err
    if moment.tzinfo is None:
        raise ValueError(f"naive {name}")
    return moment


@dataclass(frozen=True, slots=True)
class NursingTimer:
    """A running nursing session: when it began, the side now, and the time on finished sides.

    ``left_seconds`` and ``right_seconds`` count only the stretches before ``side_started``; the
    current side's running time is ``now - side_started``. ``object_id`` is the record the stop
    writes, chosen at the start so a retried stop can't write twice.
    """

    object_id: str
    started: datetime
    side: str
    side_started: datetime
    left_seconds: float = 0.0
    right_seconds: float = 0.0

    @classmethod
    def start(cls, object_id: str, side: str, now: datetime) -> NursingTimer:
        """Start a timer on ``side`` at ``now``."""
        return cls(object_id, now, side, now)

    def switched(self, side: str, now: datetime) -> NursingTimer:
        """Return the timer on ``side`` from ``now``, with the current stretch added to its side.

        Callers pass the other side: starting on the running side changes nothing, so it never
        gets here.
        """
        left, right = self.totals(now)
        return replace(self, side=side, side_started=now, left_seconds=left, right_seconds=right)

    def is_overdue(self, now: datetime, after: timedelta) -> bool:
        """Whether the timer has run for ``after`` or longer since it started.

        A side switch doesn't restart this: it counts from the start of the session.
        """
        return now - self.started >= after

    def totals(self, now: datetime) -> tuple[float, float]:
        """Seconds on each side, counting the current stretch up to ``now``."""
        running = max(0.0, (now - self.side_started).total_seconds())
        if self.side == LEFT:
            return self.left_seconds + running, self.right_seconds
        return self.left_seconds, self.right_seconds + running

    def minutes(self, now: datetime) -> tuple[int, int]:
        """Whole minutes per side to write at ``now`` (see the module notes)."""
        left_s, right_s = self.totals(now)
        left, right = _whole_minutes(left_s), _whole_minutes(right_s)
        if left + right == 0:
            return (1, 0) if self.side == LEFT else (0, 1)
        if left > MAX_MINUTES_PER_SIDE or right > MAX_MINUTES_PER_SIDE:
            _LOGGER.warning(
                "Nursing timer %s ran past %s minutes on a side; writing %s",
                self.object_id,
                MAX_MINUTES_PER_SIDE,
                MAX_MINUTES_PER_SIDE,
            )
        return min(left, MAX_MINUTES_PER_SIDE), min(right, MAX_MINUTES_PER_SIDE)

    def to_dict(self) -> dict[str, Any]:
        """Return the timer as saveable data."""
        return {
            "object_id": self.object_id,
            "started": self.started.isoformat(),
            "side": self.side,
            "side_started": self.side_started.isoformat(),
            "left_seconds": self.left_seconds,
            "right_seconds": self.right_seconds,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> NursingTimer:
        """Rebuild a saved timer.

        Raises ``ValueError`` (or ``KeyError``/``TypeError``) on bad data.
        """
        side = data["side"]
        if side not in SIDES:
            raise ValueError(f"unknown side {side!r}")
        started = _aware_time(data["started"], "time")
        side_started = _aware_time(data["side_started"], "time")
        left = float(data.get("left_seconds", 0.0))
        right = float(data.get("right_seconds", 0.0))
        if not (math.isfinite(left) and math.isfinite(right)) or left < 0 or right < 0:
            raise ValueError("bad seconds")
        return cls(str(data["object_id"]), started, side, side_started, left, right)


@dataclass(frozen=True, slots=True)
class PumpTimer:
    """A running pump session on the account: when it began, and the record its stop writes.

    ``object_id`` is chosen at the start so a retried stop can't write twice.
    """

    object_id: str
    started: datetime

    @classmethod
    def start(cls, object_id: str, now: datetime) -> PumpTimer:
        """Start a timer at ``now``."""
        return cls(object_id, now)

    def is_overdue(self, now: datetime, after: timedelta) -> bool:
        """Whether the timer has run for ``after`` or longer since it started."""
        return now - self.started >= after

    def minutes(self, now: datetime) -> int:
        """Whole minutes to write at ``now``, on both sides alike.

        Rounded half up, at least 1 (a deliberate press is never refused for being short), and at
        most 240, the most a record takes (a forgotten timer).
        """
        minutes = max(1, _whole_minutes(max(0.0, (now - self.started).total_seconds())))
        if minutes > MAX_MINUTES_PER_SIDE:
            _LOGGER.warning(
                "Pump timer %s ran past %s minutes; writing %s",
                self.object_id,
                MAX_MINUTES_PER_SIDE,
                MAX_MINUTES_PER_SIDE,
            )
        return min(minutes, MAX_MINUTES_PER_SIDE)

    def to_dict(self) -> dict[str, Any]:
        """Return the timer as saveable data."""
        return {"object_id": self.object_id, "started": self.started.isoformat()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PumpTimer:
        """Rebuild a saved timer.

        Raises ``ValueError`` (or ``KeyError``/``TypeError``) on bad data.
        """
        object_id = str(data["object_id"])
        if not object_id:
            raise ValueError("no record ID")
        return cls(object_id, _aware_time(data["started"], "time"))
