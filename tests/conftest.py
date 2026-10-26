from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import aiohttp
import pytest_asyncio
from aiohttp.test_utils import TestServer
from fake_server import FakeSyncServer

from pybabytracker import BabyTrackerClient

PROBE_UUID = "99999999-9999-4999-8999-999999999999"

ClientFactory = Callable[..., Awaitable[BabyTrackerClient]]


@pytest_asyncio.fixture
async def fake_server() -> AsyncIterator[FakeSyncServer]:
    fake = FakeSyncServer()
    async with TestServer(fake.app()) as server:
        fake.base_url = str(server.make_url("")).rstrip("/")
        yield fake


@pytest_asyncio.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    # The test server is on 127.0.0.1, which the default cookie jar refuses to keep.
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as client_session:
        yield client_session


@pytest_asyncio.fixture
async def client(
    fake_server: FakeSyncServer, session: aiohttp.ClientSession
) -> BabyTrackerClient:
    return BabyTrackerClient(
        fake_server.email,
        fake_server.password,
        PROBE_UUID,
        "Test Device",
        session=session,
        base_url=fake_server.base_url,
    )
