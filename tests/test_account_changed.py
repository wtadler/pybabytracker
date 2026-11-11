"""The engine keeps the sync group's AccountID and refuses a different one, as the app does."""

from __future__ import annotations

from datetime import UTC, datetime

import aiohttp
import pytest
from conftest import PROBE_UUID
from factories import BABY_ID, payload_for
from fake_server import FakeSyncServer

from pybabytracker import (
    DIAPER_WET,
    OP_ADD,
    AccountChangedError,
    AccountResetError,
    AuthenticationError,
    BabyTrackerClient,
    Device,
    RawTransaction,
    SyncEngine,
)

PHONE = "AAAA1111-0000-4000-8000-00000000000A"
EVENT_TIME = datetime(2026, 10, 25, 15, 40, 49, tzinfo=UTC)


def rid(n: int) -> str:
    return f"AAAAAAAA-0000-4000-8000-{n:012d}"


def reset_group(server: FakeSyncServer, account_id: int) -> None:
    """The group is reset in the app: a new AccountID, and every session ends."""
    server.account_id = account_id
    server.sessions.clear()


def new_client(session: aiohttp.ClientSession, server: FakeSyncServer) -> BabyTrackerClient:
    return BabyTrackerClient(
        server.email,
        server.password,
        PROBE_UUID,
        "Test Device",
        session=session,
        base_url=server.base_url,
    )


async def test_the_first_sync_keeps_the_account_id_and_it_survives_a_save(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.add_device(PHONE)
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Diaper", rid(1)))
    engine = SyncEngine(device_uuid=PROBE_UUID)

    await engine.sync(client)

    assert engine.account_id == 424242
    assert SyncEngine.from_dict(engine.to_dict()).account_id == 424242


async def test_a_sync_after_a_reset_raises_and_applies_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.add_device(PHONE)
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Diaper", rid(1)))
    engine = SyncEngine(device_uuid=PROBE_UUID)
    await engine.sync(client)

    reset_group(fake_server, 777)
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Sleep", rid(2)))

    with pytest.raises(AccountChangedError) as caught:
        await engine.sync(client)
    assert (caught.value.saved, caught.value.signed_in) == (424242, 777)
    assert set(engine.records) == {rid(1)}
    assert engine.account_id == 424242


async def test_a_write_after_a_reset_raises_and_posts_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.add_device(PHONE)
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Diaper", rid(1)))
    engine = SyncEngine(device_uuid=PROBE_UUID)
    await engine.sync(client)

    reset_group(fake_server, 777)

    with pytest.raises(AccountChangedError):
        await engine.add_diaper(client, BABY_ID, DIAPER_WET, time=EVENT_TIME, timezone_offset=0)
    assert fake_server.posted(PROBE_UUID) == []


async def test_a_state_saved_before_the_account_id_was_kept_adopts_the_next_one(
    client: BabyTrackerClient, fake_server: FakeSyncServer, session: aiohttp.ClientSession
) -> None:
    fake_server.add_device(PHONE)
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Diaper", rid(1)))
    engine = SyncEngine(device_uuid=PROBE_UUID)
    await engine.sync(client)
    saved = engine.to_dict()
    del saved["account_id"]

    restored = SyncEngine.from_dict(saved)
    assert restored.account_id is None
    reset_group(fake_server, 777)
    await restored.sync(new_client(session, fake_server))

    assert restored.account_id == 777


async def test_a_new_client_and_a_new_engine_join_the_new_group(
    client: BabyTrackerClient, fake_server: FakeSyncServer, session: aiohttp.ClientSession
) -> None:
    fake_server.add_device(PHONE)
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Diaper", rid(1)))
    await SyncEngine(device_uuid=PROBE_UUID).sync(client)
    reset_group(fake_server, 777)

    fresh = SyncEngine(device_uuid=PROBE_UUID)
    await fresh.sync(new_client(session, fake_server))

    assert fresh.account_id == 777
    assert set(fresh.records) == {rid(1)}


async def test_a_reset_between_listing_devices_and_reading_applies_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.add_device(PHONE)
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Diaper", rid(1)))
    engine = SyncEngine(device_uuid=PROBE_UUID)
    await engine.sync(client)
    before = engine.to_dict()["last_seen"]
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Sleep", rid(2)))
    fake_server.reset_on_request = ("GET", "/account/transaction", 777)

    with pytest.raises(AccountChangedError):
        await engine.sync(client)

    assert set(engine.records) == {rid(1)}
    assert engine.to_dict()["last_seen"] == before
    assert engine.account_id == 424242


async def test_a_reset_just_before_a_write_posts_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.add_device(PHONE)
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Diaper", rid(1)))
    engine = SyncEngine(device_uuid=PROBE_UUID)
    await engine.sync(client)
    fake_server.reset_on_request = ("POST", "/account/transaction", 777)

    with pytest.raises(AccountChangedError):
        await engine.add_diaper(client, BABY_ID, DIAPER_WET, time=EVENT_TIME, timezone_offset=0)

    assert fake_server.posted(PROBE_UUID) == []
    assert len(fake_server.calls("POST", "/account/transaction")) == 1  # the 401 only


async def test_a_reset_during_a_writes_catch_up_read_posts_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.add_device(PHONE)
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Diaper", rid(1)))
    engine = SyncEngine(device_uuid=PROBE_UUID)
    await engine.sync(client)
    # Another process has posted as this device, so the write must read its log first.
    fake_server.add_device(PROBE_UUID)
    fake_server.add_transaction(PROBE_UUID, OP_ADD, payload_for("Sleep", rid(2)))
    fake_server.reset_on_request = ("GET", "/account/transaction", 777)

    with pytest.raises(AccountChangedError):
        await engine.add_diaper(client, BABY_ID, DIAPER_WET, time=EVENT_TIME, timezone_offset=0)

    assert len(fake_server.posted(PROBE_UUID)) == 1  # only the seeded entry
    assert set(engine.records) == {rid(1)}


async def test_the_client_refuses_a_sign_in_to_a_different_group(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    assert await client.sign_in() == 424242
    reset_group(fake_server, 777)

    with pytest.raises(AccountChangedError) as caught:
        await client.get_devices()

    assert (caught.value.saved, caught.value.signed_in) == (424242, 777)
    assert client.account_id == 424242
    assert fake_server.calls("GET", "/account/device")[-1].path == "/account/device"
    assert len(fake_server.calls("GET", "/account/device")) == 1  # never retried


async def test_a_reused_client_keeps_refusing_after_a_reset(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.add_device(PHONE)
    await SyncEngine(device_uuid=PROBE_UUID).sync(client)
    reset_group(fake_server, 777)

    for _ in range(2):
        with pytest.raises(AccountChangedError):
            await SyncEngine(device_uuid=PROBE_UUID).sync(client)


async def test_a_refused_post_leaves_no_pending_write(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.add_device(PHONE)
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Diaper", rid(1)))
    engine = SyncEngine(device_uuid=PROBE_UUID)
    await engine.sync(client)
    fake_server.reset_on_request = ("POST", "/account/transaction", 777)

    with pytest.raises(AccountChangedError):
        await engine.add_diaper(client, BABY_ID, DIAPER_WET, time=EVENT_TIME, timezone_offset=0)

    assert not engine.to_dict()["pending"]


async def test_an_explicit_sign_in_to_a_different_group_is_refused(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    await client.sign_in()
    reset_group(fake_server, 777)

    with pytest.raises(AccountChangedError):
        await client.sign_in()


async def test_a_401_re_sign_in_to_the_same_group_still_works(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.add_device(PHONE)
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Diaper", rid(1)))
    engine = SyncEngine(device_uuid=PROBE_UUID)
    await engine.sync(client)
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Sleep", rid(2)))
    fake_server.expire_sessions()

    await engine.sync(client)
    assert set(engine.records) == {rid(1), rid(2)}

    fake_server.post_script.append("401")
    await engine.add_diaper(client, BABY_ID, DIAPER_WET, time=EVENT_TIME, timezone_offset=0)
    assert len(fake_server.posted(PROBE_UUID)) == 1


async def test_a_fresh_client_against_restored_state_from_another_group_applies_nothing(
    session: aiohttp.ClientSession, fake_server: FakeSyncServer
) -> None:
    fake_server.add_device(PHONE)
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Diaper", rid(1)))
    first = BabyTrackerClient(
        fake_server.email, fake_server.password, PROBE_UUID, "Test Device",
        session=session, base_url=fake_server.base_url,
    )  # fmt: skip
    engine = SyncEngine(device_uuid=PROBE_UUID)
    await engine.sync(first)
    restored = SyncEngine.from_dict(engine.to_dict())
    reset_group(fake_server, 777)
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Sleep", rid(2)))
    fresh = BabyTrackerClient(
        fake_server.email, fake_server.password, PROBE_UUID, "Test Device",
        session=session, base_url=fake_server.base_url,
    )  # fmt: skip

    with pytest.raises(AccountChangedError):
        await restored.sync(fresh)

    assert set(restored.records) == {rid(1)}


async def test_a_saved_account_id_of_true_is_not_an_account_id() -> None:
    state = SyncEngine().to_dict()
    state["account_id"] = True

    assert SyncEngine.from_dict(state).account_id is None


class _NoSignIn:
    """A source that never signs in, so it has no account_id."""

    async def get_devices(self) -> list[Device]:
        return []

    async def get_transactions(self, device_uuid: str, since_sync_id: int) -> list[RawTransaction]:
        return []


async def test_a_source_without_an_account_id_is_trusted() -> None:
    engine = SyncEngine()

    await engine.sync(_NoSignIn())

    assert engine.account_id is None


# --- a device from before a reset is refused at sign-in -----------------------------------------


async def test_a_device_from_before_a_reset_is_refused_as_a_reset_not_a_bad_password(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.reset_devices.add(PROBE_UUID)

    with pytest.raises(AccountResetError) as caught:
        await client.sign_in()

    assert isinstance(caught.value, AccountChangedError)
    assert not isinstance(caught.value, AuthenticationError)
    assert caught.value.signed_in is None


async def test_a_wrong_password_is_still_an_authentication_error(
    session: aiohttp.ClientSession, fake_server: FakeSyncServer
) -> None:
    fake_server.reset_devices.add(PROBE_UUID)
    client = BabyTrackerClient(
        fake_server.email,
        "wrong",
        PROBE_UUID,
        "Test Device",
        session=session,
        base_url=fake_server.base_url,
    )

    with pytest.raises(AuthenticationError) as caught:
        await client.sign_in()
    assert not isinstance(caught.value, AccountChangedError)


async def test_a_sync_after_a_reset_that_refuses_the_device_raises_and_applies_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.add_device(PHONE)
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Diaper", rid(1)))
    engine = SyncEngine(device_uuid=PROBE_UUID)
    await engine.sync(client)

    fake_server.reset_devices.add(PROBE_UUID)
    fake_server.sessions.clear()
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Sleep", rid(2)))

    with pytest.raises(AccountResetError) as caught:
        await engine.sync(client)
    assert caught.value.saved == 424242
    assert set(engine.records) == {rid(1)}


async def test_a_new_device_joins_after_a_reset(
    session: aiohttp.ClientSession, fake_server: FakeSyncServer
) -> None:
    fake_server.reset_devices.add(PROBE_UUID)
    fake_server.add_device(PHONE)
    fake_server.add_transaction(PHONE, OP_ADD, payload_for("Diaper", rid(1)))
    joined = BabyTrackerClient(
        fake_server.email,
        fake_server.password,
        "NEW-DEVICE-0000-4000-8000-000000000001",
        "Test Device",
        session=session,
        base_url=fake_server.base_url,
    )

    engine = SyncEngine(device_uuid="NEW-DEVICE-0000-4000-8000-000000000001")
    await engine.sync(joined)

    assert set(engine.records) == {rid(1)}


# --- which refused sign-ins count as a reset ----------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "Account has been reset. Please login again",
        "ACCOUNT HAS BEEN RESET. PLEASE LOGIN AGAIN",
        "<html><body><p>Account has been reset.</p></body></html>",
        "Account  has\n  been\treset. Please login again",
    ],
)
async def test_a_refused_sign_in_that_says_the_account_was_reset_is_a_reset(
    client: BabyTrackerClient, fake_server: FakeSyncServer, body: str
) -> None:
    fake_server.session_status = 401
    fake_server.session_text = body
    fake_server.session_content_type = "text/html"

    with pytest.raises(AccountResetError):
        await client.sign_in()


@pytest.mark.parametrize(
    "body",
    [
        "Your password has been reset. Please login again",
        '{"error": "bad credentials"}',
        "",
    ],
)
async def test_a_refused_sign_in_that_does_not_say_the_account_was_reset_is_a_bad_password(
    client: BabyTrackerClient, fake_server: FakeSyncServer, body: str
) -> None:
    fake_server.session_status = 401
    fake_server.session_text = body

    with pytest.raises(AuthenticationError) as caught:
        await client.sign_in()
    assert not isinstance(caught.value, AccountChangedError)


async def test_a_plain_wrong_password_is_an_authentication_error(
    session: aiohttp.ClientSession, fake_server: FakeSyncServer
) -> None:
    client = BabyTrackerClient(
        fake_server.email,
        "wrong",
        PROBE_UUID,
        "Test Device",
        session=session,
        base_url=fake_server.base_url,
    )

    with pytest.raises(AuthenticationError) as caught:
        await client.sign_in()
    assert not isinstance(caught.value, AccountChangedError)


# --- the errors' fields and messages ------------------------------------------------------------


def test_an_account_changed_error_can_carry_its_own_message() -> None:
    err = AccountChangedError(1, 2, message="told otherwise")

    assert str(err) == "told otherwise"
    assert (err.saved, err.signed_in) == (1, 2)


def test_an_account_changed_error_names_both_account_ids_by_default() -> None:
    err = AccountChangedError(1, 2)

    assert "AccountID 1 is now 2" in str(err)


def test_an_account_reset_error_keeps_the_saved_id_and_says_the_device_is_refused() -> None:
    err = AccountResetError(424242)

    assert (err.saved, err.signed_in) == (424242, None)
    assert "reset" in str(err)
    assert "is now" not in str(err)
