"""Stub."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

LEFT = "left"
RIGHT = "right"
SIDES = (LEFT, RIGHT)


def other_side(side: str) -> str:
    return side


@dataclass(frozen=True, slots=True)
class NursingTimer:
    object_id: str
    started: datetime
    side: str
    side_started: datetime
    left_seconds: float = 0.0
    right_seconds: float = 0.0

    @classmethod
    def start(cls, object_id: str, side: str, now: datetime) -> NursingTimer:
        return cls(object_id, now, side, now)

    def switched(self, side: str, now: datetime) -> NursingTimer:
        return self

    def is_overdue(self, now: datetime, after: timedelta) -> bool:
        return False

    def totals(self, now: datetime) -> tuple[float, float]:
        return 0.0, 0.0

    def minutes(self, now: datetime) -> tuple[int, int]:
        return 0, 0

    def to_dict(self) -> dict[str, Any]:
        return {}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> NursingTimer:
        return cls("", datetime.min, LEFT, datetime.min)


@dataclass(frozen=True, slots=True)
class PumpTimer:
    object_id: str
    started: datetime

    @classmethod
    def start(cls, object_id: str, now: datetime) -> PumpTimer:
        return cls(object_id, now)

    def is_overdue(self, now: datetime, after: timedelta) -> bool:
        return False

    def minutes(self, now: datetime) -> int:
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PumpTimer:
        return cls("", datetime.min)
