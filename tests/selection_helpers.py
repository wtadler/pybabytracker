"""Helpers shared by the milestone, vaccine, and medicine tests (invented data only)."""

from __future__ import annotations

import base64
import json
from typing import Any

from conftest import PROBE_UUID
from factories import payload_for
from fake_server import FakeSyncServer

from pybabytracker import OP_ADD, BabyTrackerClient, SyncEngine

PHONE = "AAAA1111-0000-4000-8000-00000000000A"


def rid(n: int) -> str:
    return f"AAAAAAAA-0000-4000-8000-{n:012d}"


def seed(server: FakeSyncServer, *payloads: dict[str, Any]) -> None:
    """Another phone has logged a diaper (so the baby is known) and anything else given."""
    server.register_devices = False
    server.add_device(PHONE)
    server.add_transaction(PHONE, OP_ADD, payload_for("Diaper", rid(1)))
    for payload in payloads:
        server.add_transaction(PHONE, OP_ADD, payload)


async def synced(client: BabyTrackerClient) -> SyncEngine:
    engine = SyncEngine(device_uuid=PROBE_UUID)
    await engine.sync(client)
    return engine


def posted(server: FakeSyncServer) -> list[dict[str, Any]]:
    """The payloads this test's device posted, decoded."""
    return [json.loads(base64.b64decode(e["Transaction"])) for e in server.posted(PROBE_UUID)]
