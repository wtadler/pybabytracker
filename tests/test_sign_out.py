"""Signing out."""

from __future__ import annotations

import aiohttp
import pytest
from conftest import PROBE_UUID
from fake_server import FakeSyncServer

from pybabytracker import (
    BabyTrackerClient,
    BabyTrackerConnectionError,
    NoResponseError,
    ProtocolError,
)


def session_calls(server: FakeSyncServer) -> list[str]:
    return [r.method for r in server.requests if r.path == "/session"]


async def test_signing_out_ends_the_session_on_the_service(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    await client.sign_in()
    assert len(fake_server.sessions) == 1

    await client.sign_out()

    assert session_calls(fake_server) == ["POST", "DELETE"]
    assert fake_server.sessions == {}
    [delete] = [r for r in fake_server.requests if r.method == "DELETE"]
    assert str(delete.user_agent).startswith("pybabytracker/")


async def test_the_client_signs_in_again_on_its_next_call(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    await client.get_devices()
    await client.sign_out()

    await client.get_devices()

    assert session_calls(fake_server) == ["POST", "DELETE", "POST"]
    # One clean sign-in, not a 401 and a retry.
    assert len(fake_server.calls("GET", "/account/device")) == 2


async def test_nothing_is_sent_when_the_client_is_not_signed_in(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    await client.sign_out()
    assert fake_server.requests == []

    await client.sign_in()
    await client.sign_out()
    await client.sign_out()
    assert session_calls(fake_server) == ["POST", "DELETE"]


async def test_a_session_that_had_already_ended_counts_as_signed_out(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    await client.sign_in()
    fake_server.expire_sessions()

    await client.sign_out()  # the service answers 401

    # No sign-in to recover from the 401, and nothing more to send.
    assert session_calls(fake_server) == ["POST", "DELETE"]
    await client.sign_out()
    assert session_calls(fake_server) == ["POST", "DELETE"]


@pytest.mark.parametrize(
    ("status", "error"), [(500, BabyTrackerConnectionError), (400, ProtocolError)]
)
async def test_an_error_status_raises_and_leaves_the_client_signed_in(
    client: BabyTrackerClient,
    fake_server: FakeSyncServer,
    status: int,
    error: type[Exception],
) -> None:
    await client.sign_in()
    fake_server.force_status = status

    with pytest.raises(error):
        await client.sign_out()

    fake_server.force_status = None
    await client.sign_out()  # still signed in, so this one is sent
    assert session_calls(fake_server) == ["POST", "DELETE", "DELETE"]
    assert fake_server.sessions == {}


async def test_no_answer_raises_no_response(
    fake_server: FakeSyncServer, session: aiohttp.ClientSession
) -> None:
    quick = BabyTrackerClient(
        fake_server.email,
        fake_server.password,
        PROBE_UUID,
        "Test Device",
        session=session,
        base_url=fake_server.base_url,
        request_timeout=0.2,
    )
    await quick.sign_in()
    fake_server.delay = 0.6

    with pytest.raises(NoResponseError):
        await quick.sign_out()
