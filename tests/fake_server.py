"""An aiohttp stand-in for the sync service, used to test the client and engine."""

from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web
from factories import wire


@dataclass
class RecordedRequest:
    method: str
    path: str
    body: Any
    user_agent: str | None


@dataclass
class FakeDevice:
    uuid: str
    name: str = "Test Phone"
    os_info: str = "TestOS 1"
    log: list[dict[str, Any]] = field(default_factory=list)
    last_sync_override: int | None = None

    @property
    def last_sync_id(self) -> int:
        if self.last_sync_override is not None:
            return self.last_sync_override
        return max((int(entry.get("SyncID", 0)) for entry in self.log), default=0)


class FakeSyncServer:
    def __init__(
        self,
        *,
        email: str = "parent@example.test",
        password: str = "test-password",
        register_devices: bool = True,
    ) -> None:
        self.email = email
        self.password = password
        self.register_devices = register_devices
        self.devices: dict[str, FakeDevice] = {}
        # Session cookie -> the device that signed in with it.
        self.sessions: dict[str, tuple[str, str, str]] = {}
        self.requests: list[RecordedRequest] = []
        # Status to return from the data endpoints instead of real data.
        self.force_status: int | None = None
        # Reject every data call with 401, even with a valid session.
        self.always_unauthorized = False
        # Fail only the data calls whose path ends with this (for partial-failure tests).
        self.fail_paths: dict[str, int] = {}
        self.delay = 0.0
        # Status to return from POST /session (None means normal behavior).
        self.session_status: int | None = None
        # Return at most this many log entries per request.
        self.page_size: int | None = None
        # Leave out log entries numbered below this (an older history the server trimmed).
        self.trim_below: int | None = None
        # Page backward from the newest entry. A read from 0 returns the newest ``page_size``
        # entries; a read from S returns the newest ``page_size`` entries numbered up to S + 1,
        # so the page just below the one before it.
        self.newest_first = False
        self.raw_devices_body: str | None = None
        # What each successive POST /account/transaction does; "ok" once the list is empty.
        #   "500": answer 500 without recording.
        #   "401": expire every session and answer 401 without recording.
        #   "timeout-landed": record, then stall past the client's timeout.
        #   "timeout-lost": stall past the client's timeout, never record.
        #   "timeout-late": stall past the client's timeout, then record (lands late).
        self.post_script: list[str] = []
        self.post_stall = 1.0
        self.base_url = ""

    # --- test setup helpers -------------------------------------------------

    def add_device(self, uuid: str, name: str = "Test Phone") -> FakeDevice:
        device = FakeDevice(uuid=uuid, name=name)
        self.devices[uuid] = device
        return device

    def add_transaction(
        self, device_uuid: str, op_code: int, payload: Any, sync_id: int | None = None
    ) -> int:
        device = self.devices[device_uuid]
        number = sync_id if sync_id is not None else device.last_sync_id + 1
        device.log.append(wire(number, op_code, payload))
        return number

    def add_raw_transaction(
        self, device_uuid: str, sync_id: int, op_code: int, transaction: str
    ) -> None:
        self.devices[device_uuid].log.append(
            {"SyncID": sync_id, "OPCode": op_code, "Transaction": transaction}
        )

    def expire_sessions(self) -> None:
        self.sessions.clear()

    def posted(self, device_uuid: str) -> list[dict[str, Any]]:
        """Every entry in a device's log, in the order it arrived."""
        device = self.devices.get(device_uuid)
        return [] if device is None else list(device.log)

    def calls(self, method: str, prefix: str) -> list[RecordedRequest]:
        return [r for r in self.requests if r.method == method and r.path.startswith(prefix)]

    # --- aiohttp app --------------------------------------------------------

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/session", self._post_session)
        app.router.add_get("/account/device", self._get_devices)
        app.router.add_get("/account/transaction/{uuid}/{since}", self._get_transactions)
        app.router.add_post("/account/transaction", self._post_transaction)
        return app

    @staticmethod
    def _json(data: Any, status: int = 200) -> web.Response:
        # The real service labels JSON as text/html.
        return web.Response(text=json.dumps(data), status=status, content_type="text/html")

    async def _record(self, request: web.Request) -> None:
        body: Any = None
        if request.can_read_body:
            body = json.loads(await request.text())
        self.requests.append(
            RecordedRequest(request.method, request.path, body, request.headers.get("User-Agent"))
        )

    async def _post_session(self, request: web.Request) -> web.Response:
        await self._record(request)
        if self.session_status is not None:
            return web.Response(status=self.session_status, text="forced")
        body = self.requests[-1].body
        if body["EmailAddress"] != self.email or body["Password"] != self.password:
            return self._json({"error": "bad credentials"}, status=401)
        device = body["Device"]
        if self.register_devices and device["DeviceUUID"] not in self.devices:
            new = self.add_device(device["DeviceUUID"], device["DeviceName"])
            new.os_info = device["DeviceOSInfo"]
        token = secrets.token_hex(8)
        self.sessions[token] = (device["DeviceUUID"], device["DeviceName"], device["DeviceOSInfo"])
        response = self._json({"AccountID": 424242})
        response.set_cookie("PHPSESSID", token)
        return response

    async def _guard(self, request: web.Request) -> web.Response | None:
        await self._record(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.always_unauthorized or request.cookies.get("PHPSESSID") not in self.sessions:
            return web.Response(status=401, text="Unauthorized")
        if self.force_status is not None:
            return web.Response(status=self.force_status, text="forced")
        for suffix, status in self.fail_paths.items():
            if request.path.endswith(suffix):
                return web.Response(status=status, text="forced")
        return None

    async def _get_devices(self, request: web.Request) -> web.Response:
        if (rejected := await self._guard(request)) is not None:
            return rejected
        if self.raw_devices_body is not None:
            return web.Response(text=self.raw_devices_body, content_type="text/html")
        return self._json(
            [
                {
                    "DeviceUUID": d.uuid,
                    "LastSyncID": d.last_sync_id,
                    "DeviceName": d.name,
                    "DeviceOSInfo": d.os_info,
                }
                for d in self.devices.values()
            ]
        )

    async def _get_transactions(self, request: web.Request) -> web.Response:
        if (rejected := await self._guard(request)) is not None:
            return rejected
        device = self.devices.get(request.match_info["uuid"])
        if device is None:
            return self._json([])
        since = int(request.match_info["since"])
        if self.newest_first and since > 0:
            entries = [e for e in device.log if "SyncID" not in e or int(e["SyncID"]) <= since + 1]
        else:
            entries = [e for e in device.log if "SyncID" not in e or int(e["SyncID"]) > since]
        if self.trim_below is not None:
            entries = [
                e for e in entries if "SyncID" not in e or int(e["SyncID"]) >= self.trim_below
            ]
        if self.page_size is not None:
            entries = entries[-self.page_size :] if self.newest_first else entries[: self.page_size]
        return self._json(entries)

    async def _post_transaction(self, request: web.Request) -> web.Response:
        if (rejected := await self._guard(request)) is not None:
            return rejected
        body = self.requests[-1].body
        session_device = self.sessions[request.cookies["PHPSESSID"]]
        action = self.post_script.pop(0) if self.post_script else "ok"
        if action == "500":
            return web.Response(status=500, text="forced")
        if action == "401":
            self.expire_sessions()
            return web.Response(status=401, text="Unauthorized")
        if action == "timeout-late":
            # Scheduled apart from the handler, which aiohttp cancels when the client gives up.
            asyncio.get_running_loop().call_later(
                self.post_stall, self._record_post, session_device, body
            )
        if action in ("timeout-lost", "timeout-late"):
            await asyncio.sleep(self.post_stall)
            return web.Response(status=201, text="")
        self._record_post(session_device, body)
        if action == "timeout-landed":
            await asyncio.sleep(self.post_stall)
        return web.Response(status=201, text="")

    def _record_post(self, session_device: tuple[str, str, str], body: Any) -> None:
        uuid, name, os_info = session_device
        # The real service lists a device only once it has posted (PROTOCOL.md, Probe findings).
        device = self.devices.get(uuid)
        if device is None:
            device = self.add_device(uuid, name)
            device.os_info = os_info
        device.log.append(
            {"SyncID": body["SyncID"], "OPCode": body["OPCode"], "Transaction": body["Transaction"]}
        )
