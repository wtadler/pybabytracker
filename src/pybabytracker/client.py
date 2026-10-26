"""Async client for the sync service: sign in, list devices, read a device's log."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self
from urllib.parse import quote

import aiohttp

from ._version import __version__
from .exceptions import AuthenticationError, BabyTrackerConnectionError, ProtocolError

_LOGGER = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://prodapp.babytrackers.com"
DEFAULT_TIMEOUT = 30.0
USER_AGENT = f"pybabytracker/{__version__}"


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
        """Start a session. Raises ``AuthenticationError`` if the credentials are rejected."""
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
        if not isinstance(account_id, int):
            raise ProtocolError("sign-in reply had no AccountID")
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

    async def _request(self, method: str, path: str, body: Any = None) -> Any:
        """Call an endpoint that needs a session.

        A 401 means the session expired: sign in again once and retry. A second 401 raises.
        """
        if not self._signed_in:
            async with self._sign_in_lock:
                if not self._signed_in:
                    await self._sign_in()
        for attempt in range(2):
            generation = self._generation
            status, text = await self._send(method, path, body)
            if status != 401:
                _raise_for_status(status)
                return _parse_json(text)
            if attempt == 1:
                self._signed_in = False
                break
            async with self._sign_in_lock:
                # Skip it if another task signed in again while this one waited.
                if self._generation == generation:
                    await self._sign_in()
        raise AuthenticationError("the service rejected the session even after signing in again")

    async def _send(self, method: str, path: str, body: Any = None) -> tuple[int, str]:
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
                timeout=self._timeout,
            ) as response:
                return response.status, await response.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise BabyTrackerConnectionError(f"could not reach the service: {err!r}") from err


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
