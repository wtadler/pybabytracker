"""STUB for the red run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Device:
    device_uuid: str
    last_sync_id: int
    device_name: str
    device_os_info: str


@dataclass(frozen=True)
class RawTransaction:
    sync_id: int
    op_code: int
    transaction: str


class BabyTrackerClient:
    account_id: int | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def sign_in(self) -> int:
        return 0

    async def get_devices(self) -> list[Device]:
        return []

    async def get_transactions(self, device_uuid: str, since: int = 0) -> list[RawTransaction]:
        return []

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> BabyTrackerClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None
