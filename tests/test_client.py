from __future__ import annotations

import socket

import aiohttp
import pytest
from aiohttp.test_utils import TestServer
from conftest import PROBE_UUID
from factories import payload_for
from fake_server import FakeSyncServer

from pybabytracker import (
    AuthenticationError,
    BabyTrackerClient,
    BabyTrackerConnectionError,
    BabyTrackerError,
    ProtocolError,
    __version__,
)

PHONE = "AAAA1111-0000-4000-8000-00000000000A"
TABLET = "BBBB2222-0000-4000-8000-00000000000B"


async def test_sign_in_returns_the_account_id(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    assert await client.sign_in() == 424242
    assert client.account_id == 424242


async def test_sign_in_sends_the_documented_body_and_an_honest_user_agent(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    await client.sign_in()
    [request] = fake_server.calls("POST", "/session")
    assert request.body == {
        "EmailAddress": fake_server.email,
        "Password": fake_server.password,
        "Device": {
            "DeviceUUID": PROBE_UUID,
            "DeviceName": "Test Device",
            "DeviceOSInfo": f"pybabytracker/{__version__}",
        },
        "AppInfo": {"AppType": 0, "AccountType": 0},
    }
    assert request.user_agent == f"pybabytracker/{__version__}"


async def test_device_list_returns_every_device_with_its_last_number(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.add_device(PHONE, "Test Phone")
    fake_server.add_device(TABLET, "Test Tablet")
    for _ in range(3):
        fake_server.add_transaction(PHONE, 0, payload_for("Diaper"))
    fake_server.add_transaction(TABLET, 0, payload_for("Diaper"))

    devices = {d.device_uuid: d for d in await client.get_devices()}

    assert devices[PHONE].last_sync_id == 3
    assert devices[PHONE].device_name == "Test Phone"
    assert devices[TABLET].last_sync_id == 1
    # Signing in registered the probe device in the fake sync group too.
    assert set(devices) == {PHONE, TABLET, PROBE_UUID}


async def test_the_first_call_signs_in_by_itself(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    await client.get_devices()
    assert len(fake_server.calls("POST", "/session")) == 1


async def test_an_empty_device_list_is_fine(session: aiohttp.ClientSession) -> None:
    fake = FakeSyncServer(register_devices=False)
    async with TestServer(fake.app()) as server:
        client = BabyTrackerClient(
            fake.email,
            fake.password,
            PROBE_UUID,
            "Test Device",
            session=session,
            base_url=str(server.make_url("")).rstrip("/"),
        )
        assert await client.get_devices() == []
        assert len(fake.calls("GET", "/account/device")) == 1


async def test_a_device_with_last_sync_id_zero_is_listed(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.add_device(PHONE)
    devices = {d.device_uuid: d for d in await client.get_devices()}
    assert devices[PHONE].last_sync_id == 0


async def test_a_bad_password_raises_an_authentication_error(
    fake_server: FakeSyncServer, session: aiohttp.ClientSession
) -> None:
    client = BabyTrackerClient(
        fake_server.email,
        "wrong-password",
        PROBE_UUID,
        "Test Device",
        session=session,
        base_url=fake_server.base_url,
    )
    with pytest.raises(AuthenticationError):
        await client.sign_in()
    # A rejected sign-in is not retried.
    assert len(fake_server.calls("POST", "/session")) == 1


async def test_credentials_do_not_leak_into_errors_or_repr(
    fake_server: FakeSyncServer, session: aiohttp.ClientSession
) -> None:
    client = BabyTrackerClient(
        fake_server.email,
        "wrong-password",
        PROBE_UUID,
        "Test Device",
        session=session,
        base_url=fake_server.base_url,
    )
    with pytest.raises(AuthenticationError) as excinfo:
        await client.sign_in()
    for text in (str(excinfo.value), repr(excinfo.value), repr(client)):
        assert "wrong-password" not in text


async def test_authentication_error_is_distinct_from_connection_error() -> None:
    assert not issubclass(AuthenticationError, BabyTrackerConnectionError)
    assert not issubclass(BabyTrackerConnectionError, AuthenticationError)
    assert issubclass(AuthenticationError, BabyTrackerError)
    assert issubclass(BabyTrackerConnectionError, BabyTrackerError)


async def test_an_expired_session_signs_in_again_once_and_retries(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.add_device(PHONE)
    await client.get_devices()
    fake_server.expire_sessions()

    devices = await client.get_devices()

    assert PHONE in {d.device_uuid for d in devices}
    assert len(fake_server.calls("POST", "/session")) == 2
    assert len(fake_server.calls("GET", "/account/device")) == 3  # ok, 401, retry ok


async def test_a_second_401_raises_an_authentication_error(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.always_unauthorized = True
    with pytest.raises(AuthenticationError):
        await client.get_devices()
    # One sign-in up front, one after the first 401, and no more.
    assert len(fake_server.calls("POST", "/session")) == 2
    assert len(fake_server.calls("GET", "/account/device")) == 2


async def test_a_500_raises_a_connection_error(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.force_status = 500
    with pytest.raises(BabyTrackerConnectionError):
        await client.get_devices()
    assert len(fake_server.calls("GET", "/account/device")) == 1


async def test_a_500_on_sign_in_raises_a_connection_error(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.session_status = 500
    with pytest.raises(BabyTrackerConnectionError):
        await client.sign_in()


async def test_a_refused_connection_raises_a_connection_error(
    session: aiohttp.ClientSession,
) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    client = BabyTrackerClient(
        "a@example.test",
        "test-password",
        PROBE_UUID,
        "Test Device",
        session=session,
        base_url=f"http://127.0.0.1:{port}",
    )
    with pytest.raises(BabyTrackerConnectionError):
        await client.sign_in()
    with pytest.raises(BabyTrackerConnectionError):
        await client.get_devices()


async def test_a_timeout_raises_a_connection_error(
    fake_server: FakeSyncServer, session: aiohttp.ClientSession
) -> None:
    client = BabyTrackerClient(
        fake_server.email,
        fake_server.password,
        PROBE_UUID,
        "Test Device",
        session=session,
        base_url=fake_server.base_url,
        request_timeout=0.05,
    )
    await client.sign_in()
    fake_server.delay = 0.5
    with pytest.raises(BabyTrackerConnectionError):
        await client.get_devices()


async def test_a_non_json_reply_raises_a_protocol_error(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.raw_devices_body = "<html>maintenance</html>"
    with pytest.raises(ProtocolError):
        await client.get_devices()


async def test_a_device_entry_missing_fields_raises_a_protocol_error(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.raw_devices_body = '[{"DeviceUUID": "only-this"}]'
    with pytest.raises(ProtocolError):
        await client.get_devices()


async def test_transactions_after_a_number_come_back_in_order(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.add_device(PHONE)
    for i in range(4):
        fake_server.add_transaction(PHONE, 0, payload_for("Diaper", f"ID-{i}"))

    everything = await client.get_transactions(PHONE, 0)
    later = await client.get_transactions(PHONE, 2)

    assert [t.sync_id for t in everything] == [1, 2, 3, 4]
    assert [t.sync_id for t in later] == [3, 4]
    assert everything[0].op_code == 0
    assert isinstance(everything[0].transaction, str)
    assert fake_server.calls("GET", f"/account/transaction/{PHONE}/2")


async def test_an_empty_log_page_is_an_empty_list(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.add_device(PHONE)
    assert await client.get_transactions(PHONE, 0) == []
    fake_server.add_transaction(PHONE, 0, payload_for("Diaper"))
    assert await client.get_transactions(PHONE, 1) == []
    assert len(fake_server.calls("GET", f"/account/transaction/{PHONE}/")) == 2


async def test_an_injected_session_is_not_closed_by_the_client(
    client: BabyTrackerClient, session: aiohttp.ClientSession, fake_server: FakeSyncServer
) -> None:
    async with client:
        await client.get_devices()
    await client.close()
    assert not session.closed
    assert fake_server.calls("GET", "/account/device")


async def test_a_session_the_client_made_itself_is_closed_on_close(
    fake_server: FakeSyncServer,
) -> None:
    client = BabyTrackerClient(
        fake_server.email,
        fake_server.password,
        PROBE_UUID,
        "Test Device",
        base_url=fake_server.base_url,
    )
    await client.get_devices()
    own_session = client._session
    assert own_session is not None
    assert not own_session.closed
    await client.close()
    assert own_session.closed
