"""Async client for the sync service: sign in, list devices, read a device's log."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from types import TracebackType
from typing import Any, Self
from urllib.parse import quote

import aiohttp

from ._version import __version__
from .exceptions import (
    AccountChangedError,
    AuthenticationError,
    BabyTrackerConnectionError,
    DownloadRefusedError,
    NoResponseError,
    ProtocolError,
)
from .models import is_account_id, is_plain_file_name

_LOGGER = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://prodapp.babytrackers.com"
DEFAULT_TIMEOUT = 30.0
USER_AGENT = f"pybabytracker/{__version__}"
MAX_PICTURE_BYTES = 20 * 1024 * 1024
MAX_SEED_BYTES = 100 * 1024 * 1024
# A seed can be large, so it gets longer than other requests, as long as data keeps coming.
SEED_TIMEOUT = 600.0


@dataclass(frozen=True, slots=True)
class Device:
    """One device in the account's sync group."""

    device_uuid: str
    last_sync_id: int
    device_name: str
    device_os_info: str


@dataclass(frozen=True, slots=True)
class RawTransaction:
    """One entry from a device's log, with the payload still base64 encoded."""

    sync_id: int
    op_code: int
    transaction: str


class BabyTrackerClient:
    """Talks to the sync service as one device in the account's sync group.

    Pass ``session`` to use an ``aiohttp.ClientSession`` (the client never closes it).
    Without one, the client makes its own on first use and closes it in ``close()``.

    The service authenticates by cookie, so the session must be dedicated to this client. Don't
    share one session between two accounts (in Home Assistant, use
    ``async_create_clientsession``, not the shared ``async_get_clientsession``).

    ``unsafe_cookie_jar`` only affects a session the client makes itself: it lets cookies from
    an IP-address host through, which only a local test server needs.
    """

    def __init__(
        self,
        email: str,
        password: str,
        device_uuid: str,
        device_name: str,
        *,
        session: aiohttp.ClientSession | None = None,
        base_url: str = DEFAULT_BASE_URL,
        device_os_info: str = USER_AGENT,
        request_timeout: float = DEFAULT_TIMEOUT,
        unsafe_cookie_jar: bool = False,
    ) -> None:
        self._email = email
        self._password = password
        self._device_uuid = device_uuid
        self._device_name = device_name
        self._device_os_info = device_os_info
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)
        self._session = session
        self._owns_session = session is None
        self._unsafe_cookie_jar = unsafe_cookie_jar
        self._sign_in_lock = asyncio.Lock()
        self._generation = 0  # counts successful sign-ins
        self._signed_in = False
        self._account_id: int | None = None

    def __repr__(self) -> str:
        return f"BabyTrackerClient(device_uuid={self._device_uuid!r}, base_url={self._base_url!r})"

    @property
    def device_uuid(self) -> str:
        """The device this client signs in and posts as."""
        return self._device_uuid

    @property
    def account_id(self) -> int | None:
        return self._account_id

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the session if this client made it. A passed-in session is left open."""
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def sign_in(self) -> int:
        """Start a session. Raises ``AuthenticationError`` if the credentials are rejected.

        Raises ``AccountChangedError`` if this client already signed in to a different sync
        group, which includes a sign-in made to recover from a 401 inside another call. The
        call that needed it is not retried.
        """
        async with self._sign_in_lock:
            return await self._sign_in()

    async def _sign_in(self) -> int:
        self._signed_in = False
        status, text = await self._send(
            "POST",
            "/session",
            {
                "EmailAddress": self._email,
                "Password": self._password,
                "Device": {
                    "DeviceUUID": self._device_uuid,
                    "DeviceName": self._device_name,
                    "DeviceOSInfo": self._device_os_info,
                },
                "AppInfo": {"AppType": 0, "AccountType": 0},
            },
        )
        if status in (401, 403):
            raise AuthenticationError("the service rejected the email or password")
        _raise_for_status(status)
        data = _parse_json(text)
        account_id = data.get("AccountID") if isinstance(data, dict) else None
        if not is_account_id(account_id):
            raise ProtocolError("sign-in reply had no AccountID")
        if self._account_id is not None and account_id != self._account_id:
            # The sync group was reset. Stay signed out, so nothing more is sent to the new one.
            raise AccountChangedError(self._account_id, account_id)
        self._signed_in = True
        self._generation += 1
        self._account_id = account_id
        return account_id

    async def get_devices(self) -> list[Device]:
        """Every device in the sync group, with its last transaction number."""
        data = await self._request("GET", "/account/device")
        if not isinstance(data, list):
            raise ProtocolError("device list was not a JSON array")
        try:
            return [
                Device(
                    device_uuid=str(entry["DeviceUUID"]),
                    last_sync_id=int(entry["LastSyncID"]),
                    device_name=str(entry["DeviceName"]),
                    device_os_info=str(entry["DeviceOSInfo"]),
                )
                for entry in data
            ]
        except (KeyError, TypeError, ValueError) as err:
            raise ProtocolError(f"malformed device entry: {err!r}") from err

    async def get_transactions(
        self, device_uuid: str, since_sync_id: int = 0
    ) -> list[RawTransaction]:
        """A device's transactions after ``since_sync_id``, in the order the service sends them."""
        path = f"/account/transaction/{quote(device_uuid, safe='')}/{int(since_sync_id)}"
        data = await self._request("GET", path)
        if not isinstance(data, list):
            raise ProtocolError("transaction list was not a JSON array")
        transactions: list[RawTransaction] = []
        for entry in data:
            parsed = _parse_entry(entry)
            if parsed is None:
                _LOGGER.warning("Dropping a log entry with no usable SyncID from %s", device_uuid)
            else:
                transactions.append(parsed)
        if data and not transactions:
            raise ProtocolError("no entry in the transaction list had a usable SyncID")
        return transactions

    async def post_transaction(self, op_code: int, sync_id: int, transaction: str) -> None:
        """Append one transaction to this device's log as number ``sync_id``.

        This is the raw call. It doesn't choose numbers or check whether a failed post landed;
        ``SyncEngine``'s write methods do both. A 401 retry resends the same number, which is
        safe because a 401 means the service didn't accept the post.
        """
        await self._request(
            "POST",
            "/account/transaction",
            {"OPCode": int(op_code), "SyncID": int(sync_id), "Transaction": transaction},
            expect_json=False,
        )

    async def get_picture(self, file_name: str) -> bytes | None:
        """Download a picture file, such as a baby's photo (``Baby.picture_file``).

        Returns ``None`` when the service doesn't have the file: it has answered ``400`` for a
        file it doesn't have (seen for a photo's ".mp4" name), and ``404`` is expected, not yet
        seen, so both mean no file. Any other error status raises, as for the other calls.

        A redirect raises ``DownloadRefusedError`` (a ``ProtocolError``) and is not followed, and
        so does a body over
        ``MAX_PICTURE_BYTES``. A name that is empty, only whitespace, not a string, or has a path
        separator raises ``ValueError`` without a request: names come from other devices' records.
        """
        if not is_plain_file_name(file_name):
            raise ValueError(f"not a picture file name: {file_name!r}")
        path = f"/account/picture/{quote(file_name, safe='')}"
        status, content = await self._authorized(
            lambda: self._send_bytes(path, MAX_PICTURE_BYTES), allow=(400, 404)
        )
        if 300 <= status < 400:
            raise DownloadRefusedError(
                f"the service redirected the picture request (HTTP {status})"
            )
        return None if status in (400, 404) else content

    async def get_seed(self) -> bytes | None:
        """Download the account's seed: a zip with a full copy of the account's database.

        The transaction logs don't hold the whole history. Records from devices that have since
        left the account are only in the seed, so read it before the logs (``read_seed`` and
        ``SyncEngine.sync`` do this). Returns ``None`` when the account has no seed: an empty
        body, or a ``400`` or ``404`` (expected, not yet seen).

        A redirect raises ``DownloadRefusedError`` (a ``ProtocolError``) and is not followed, and
        so does a body over
        ``MAX_SEED_BYTES``. The download may take up to ``SEED_TIMEOUT`` seconds in all, but fails
        if the service goes quiet for the client's usual request timeout.
        """
        limits = aiohttp.ClientTimeout(total=SEED_TIMEOUT, sock_read=self._timeout.total)
        status, content = await self._authorized(
            lambda: self._send_bytes("/account/seed", MAX_SEED_BYTES, limits), allow=(400, 404)
        )
        if 300 <= status < 400:
            raise DownloadRefusedError(f"the service redirected the seed request (HTTP {status})")
        return content if status not in (400, 404) and content else None

    async def _request(
        self, method: str, path: str, body: Any = None, *, expect_json: bool = True
    ) -> Any:
        """Call an endpoint that needs a session.

        A 401 means the session expired: sign in again once and retry. A second 401 raises.
        With ``expect_json`` false, any 2xx answer counts and its body is ignored.
        """
        _, text = await self._authorized(lambda: self._send(method, path, body))
        return _parse_json(text) if expect_json else None

    async def _authorized[T](
        self, send: Callable[[], Awaitable[tuple[int, T]]], *, allow: tuple[int, ...] = ()
    ) -> tuple[int, T]:
        """Run ``send`` with a session, signing in again once on a 401.

        A status in ``allow`` is returned as is; any other error status raises.
        """
        if not self._signed_in:
            async with self._sign_in_lock:
                if not self._signed_in:
                    await self._sign_in()
        for attempt in range(2):
            generation = self._generation
            status, content = await send()
            if status != 401:
                if status not in allow:
                    _raise_for_status(status)
                return status, content
            if attempt == 1:
                self._signed_in = False
                break
            async with self._sign_in_lock:
                # Skip it if another task signed in again while this one waited.
                if self._generation == generation:
                    await self._sign_in()
        raise AuthenticationError("the service rejected the session even after signing in again")

    async def _send(self, method: str, path: str, body: Any = None) -> tuple[int, str]:
        return await self._exchange(method, path, body, lambda response: response.text())

    async def _send_bytes(
        self, path: str, cap: int, limits: aiohttp.ClientTimeout | None = None
    ) -> tuple[int, bytes]:
        read = partial(_read_capped, cap=cap)
        return await self._exchange("GET", path, None, read, allow_redirects=False, limits=limits)

    async def _exchange[T](
        self,
        method: str,
        path: str,
        body: Any,
        read: Callable[[aiohttp.ClientResponse], Awaitable[T]],
        *,
        allow_redirects: bool = True,
        limits: aiohttp.ClientTimeout | None = None,
    ) -> tuple[int, T]:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=self._unsafe_cookie_jar)
            )
        try:
            async with self._session.request(
                method,
                f"{self._base_url}{path}",
                json=body,
                headers={"User-Agent": USER_AGENT},
                timeout=limits or self._timeout,
                allow_redirects=allow_redirects,
            ) as response:
                return response.status, await read(response)
        except aiohttp.ClientConnectorError as err:
            # No connection was made, so the request never reached the service.
            raise BabyTrackerConnectionError(f"could not reach the service: {err!r}") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            # Sent, but no answer came back: the service may or may not have acted on it.
            raise NoResponseError(f"no answer from the service: {err!r}") from err


async def _read_capped(response: aiohttp.ClientResponse, *, cap: int) -> bytes:
    """The body of ``response``, or ``DownloadRefusedError`` when it's over ``cap`` bytes.

    A redirect or an error status has its body skipped, since only the status is used.
    """
    if response.status >= 300:
        return b""
    if response.content_length is not None and response.content_length > cap:
        raise DownloadRefusedError(f"the download is larger than {cap} bytes")
    body = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        body += chunk
        if len(body) > cap:
            raise DownloadRefusedError(f"the download is larger than {cap} bytes")
    return bytes(body)


def _parse_entry(entry: Any) -> RawTransaction | None:
    """A log entry, or None with no usable SyncID.

    An entry whose other fields are bad still comes back, as a placeholder (opcode -1, empty
    payload) that the engine skips, so the sync number still advances past it.
    """
    if not isinstance(entry, dict):
        return None
    try:
        sync_id = int(entry["SyncID"])
    except (KeyError, TypeError, ValueError):
        return None
    try:
        payload = entry["Transaction"]
        if not isinstance(payload, str):
            raise TypeError("Transaction is not a string")
        return RawTransaction(sync_id, int(entry["OPCode"]), payload)
    except (KeyError, TypeError, ValueError):
        _LOGGER.warning("Log entry %s is malformed; it will be skipped", sync_id)
        return RawTransaction(sync_id, -1, "")


def _raise_for_status(status: int) -> None:
    if status >= 500:
        raise BabyTrackerConnectionError(f"the service answered with HTTP {status}")
    if status >= 400:
        raise ProtocolError(f"the service answered with HTTP {status}")


def _parse_json(text: str) -> Any:
    # The service labels its JSON as text/html, so parse the text ourselves.
    try:
        return json.loads(text)
    except ValueError as err:
        raise ProtocolError("the service did not answer with JSON") from err
