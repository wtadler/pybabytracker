"""Async client for the sync service: sign in, list devices, read a device's log."""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self
from urllib.parse import quote

import aiohttp

from ._version import __version__
from .exceptions import AuthenticationError, BabyTrackerConnectionError, ProtocolError

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

    Pass ``session`` to share an ``aiohttp.ClientSession`` (the client never closes it).
    Without one, the client makes its own on first use and closes it in ``close()``.
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
        try:
            return [
                RawTransaction(
                    sync_id=int(entry["SyncID"]),
                    op_code=int(entry["OPCode"]),
                    transaction=str(entry["Transaction"]),
                )
                for entry in data
            ]
        except (KeyError, TypeError, ValueError) as err:
            raise ProtocolError(f"malformed transaction entry: {err!r}") from err

    async def _request(self, method: str, path: str, body: Any = None) -> Any:
        """Call an endpoint that needs a session.

        A 401 means the session expired: sign in again once and retry. A second 401 raises.
        """
        if not self._signed_in:
            await self.sign_in()
        for attempt in range(2):
            status, text = await self._send(method, path, body)
            if status != 401:
                _raise_for_status(status)
                return _parse_json(text)
            self._signed_in = False
            if attempt == 1:
                break
            await self.sign_in()
        raise AuthenticationError("the service rejected the session even after signing in again")

    async def _send(self, method: str, path: str, body: Any = None) -> tuple[int, str]:
        if self._session is None:
            # Unsafe cookie jar so a base URL that is an IP address (a local test server) works.
            self._session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
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
