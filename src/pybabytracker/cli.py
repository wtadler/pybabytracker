"""Command-line tool: ``python -m pybabytracker``.

Reads ``BT_EMAIL`` and ``BT_PASSWORD`` from the environment or a ``.env`` file, signs in as a
separate dev device, and prints each baby's latest record of each type. With
``--probe-history`` it instead checks whether every device's log can be read in full from
number 0, without writing anything.

``--write-test-diaper`` and ``--delete-test-diaper`` write, so they read only
``BT_TEST_EMAIL`` and ``BT_TEST_PASSWORD`` (a throwaway test account) and never the main
credentials. They keep their own device ID and engine state in separate gitignored files.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from .client import DEFAULT_BASE_URL, BabyTrackerClient, Device
from .engine import SyncEngine
from .exceptions import AuthenticationError, BabyTrackerError
from .models import DIAPER_DIRTY, DIAPER_MIXED, DIAPER_WET, Record
from .times import format_time

PROBE_DEVICE_NAME = "Home Assistant (dev)"
DEFAULT_ENV_FILE = ".env"
DEFAULT_DEVICE_FILE = ".pybabytracker-device.json"
DEFAULT_TEST_DEVICE_FILE = ".pybabytracker-test-device.json"
DEFAULT_TEST_STATE_FILE = ".pybabytracker-test-state.json"
MAIN_CREDENTIALS = ("BT_EMAIL", "BT_PASSWORD")
TEST_CREDENTIALS = ("BT_TEST_EMAIL", "BT_TEST_PASSWORD")
DIAPER_STATUSES = {"wet": DIAPER_WET, "dirty": DIAPER_DIRTY, "mixed": DIAPER_MIXED}


class CredentialsError(Exception):
    """BT_EMAIL or BT_PASSWORD is missing."""


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a simple ``KEY=value`` file. Comments, blank lines, ``export``, and quotes work."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_credentials(
    environ: Mapping[str, str], env_file: Path, names: tuple[str, str] = MAIN_CREDENTIALS
) -> tuple[str, str]:
    """Credentials from the environment, falling back to the ``.env`` file."""
    file_values = load_env_file(env_file)
    merged = {**file_values, **{k: environ[k] for k in names if k in environ}}
    missing = [name for name in names if not merged.get(name)]
    if missing:
        raise CredentialsError(
            f"missing {' and '.join(missing)}: set them in the environment or in {env_file}"
        )
    return merged[names[0]], merged[names[1]]


def load_or_create_device_id(path: Path) -> str:
    """The probe device's stable ID, made once and kept in a local file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        existing = data["device_uuid"]
        if isinstance(existing, str) and existing:
            return existing
    except (OSError, ValueError, KeyError, TypeError):
        pass
    device_id = str(uuid.uuid4()).upper()
    path.write_text(json.dumps({"device_uuid": device_id}) + "\n", encoding="utf-8")
    return device_id


def format_latest(engine: SyncEngine) -> list[str]:
    """One section per baby, with the newest record of each type."""
    lines: list[str] = []
    for baby_id, baby in sorted(engine.babies().items(), key=lambda item: item[1].name or ""):
        lines.append(f"{baby.name or '(unnamed)'}")
        latest = engine.latest_records(baby_id)
        if not latest:
            lines.append("  (no records)")
        for type_name in sorted(latest):
            lines.append("  " + _describe(latest[type_name]))
    return lines


def _describe(record: Record) -> str:
    when = format_time(record.time) if record.time is not None else "(no time)"
    parts = [f"{key}={value}" for key, value in record.describe().items()]
    if record.note:
        parts.append(f"note={record.note!r}")
    return f"{record.record_type:<14}{when}  {' '.join(parts)}".rstrip()


@dataclass(frozen=True)
class DeviceProbe:
    device_uuid: str
    device_name: str
    last_sync_id: int
    returned: int
    lowest_returned: int
    highest_returned: int
    # How many more came back from a follow-up read past the highest number, if one was made.
    follow_up_returned: int | None = None
    # How many different numbers came back (fewer than ``returned`` means duplicates).
    distinct_returned: int | None = None
    # Whether a read from two below the lowest number returned the number just below it, which
    # means the server pages newest first. None when no such read was made.
    pages_backward: bool | None = None


@dataclass(frozen=True)
class ProbeReport:
    devices: list[DeviceProbe]
    probe_device_listed: bool


async def probe_history(client: BabyTrackerClient, probe_device_uuid: str) -> ProbeReport:
    """Read every device's log from 0 and compare with its ``LastSyncID``. Writes nothing."""
    devices = await client.get_devices()
    probes: list[DeviceProbe] = []
    for device in sorted(devices, key=lambda d: d.device_uuid):
        transactions = await client.get_transactions(device.device_uuid, 0)
        numbers = [t.sync_id for t in transactions]
        highest = max(numbers, default=0)
        follow_up: int | None = None
        if highest < device.last_sync_id:
            follow_up = len(await client.get_transactions(device.device_uuid, highest))
        lowest = min(numbers, default=0)
        pages_backward: bool | None = None
        if lowest > 2:
            # A server that trimmed its history has nothing below the lowest number. One that
            # answers with only its newest entries gives the page just below when asked for it.
            below = await client.get_transactions(device.device_uuid, lowest - 2)
            pages_backward = any(t.sync_id == lowest - 1 for t in below)
        probes.append(
            DeviceProbe(
                device.device_uuid,
                device.device_name,
                device.last_sync_id,
                len(transactions),
                lowest,
                highest,
                follow_up,
                len(set(numbers)),
                pages_backward,
            )
        )
    listed = any(d.device_uuid == probe_device_uuid for d in devices)
    return ProbeReport(probes, listed)


def probe_verdict(probe: DeviceProbe) -> str:
    """Name what the read from 0 showed: full, paged, trimmed, duplicates, or gaps."""
    if probe.last_sync_id == 0 and probe.returned == 0:
        return "full history (the log is empty)"
    if probe.returned == 0:
        return "nothing came back although LastSyncID is above 0"
    parts: list[str] = []
    if probe.highest_returned < probe.last_sync_id:
        if probe.follow_up_returned:
            parts.append(f"paged ({probe.returned} per request)")
        else:
            parts.append("the log stops below LastSyncID")
    if probe.lowest_returned > 1:
        if probe.pages_backward:
            parts.append("paged (newest first)")
        else:
            parts.append(f"starts at {probe.lowest_returned} (older numbers trimmed)")
    distinct = probe.returned if probe.distinct_returned is None else probe.distinct_returned
    if probe.returned > distinct:
        parts.append(f"duplicates: {probe.returned} returned, {distinct} different numbers")
    # Gaps are only told apart from a short or trimmed read when nothing else explains them.
    unexplained = not parts or all(part.startswith("duplicates") for part in parts)
    if unexplained and distinct != probe.last_sync_id:
        parts.append(f"gaps: {distinct} of {probe.last_sync_id} numbers present")
    return "; ".join(parts) or "full history"


def format_probe(report: ProbeReport) -> list[str]:
    lines = []
    for probe in report.devices:
        lines.append(
            f"{probe.device_name} ({probe.device_uuid}): returned {probe.returned} of "
            f"LastSyncID {probe.last_sync_id}, lowest {probe.lowest_returned}, "
            f"highest {probe.highest_returned}: {probe_verdict(probe)}"
        )
    answer = "yes" if report.probe_device_listed else "no"
    lines.append(f"{PROBE_DEVICE_NAME} appears in the device list: {answer}")
    return lines


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pybabytracker",
        description="Print each baby's latest record of each type from a Baby Tracker account.",
    )
    parser.add_argument(
        "--probe-history",
        action="store_true",
        help="read every device's log from 0 and report how many transactions come back "
        "versus its LastSyncID (read-only)",
    )
    parser.add_argument(
        "--write-test-diaper",
        action="store_true",
        help="add one diaper for the first baby on the test account (BT_TEST_EMAIL), as the "
        "dev device, and report what the device list shows",
    )
    parser.add_argument(
        "--delete-test-diaper",
        metavar="OBJECT_ID",
        help="delete a diaper the dev device wrote on the test account",
    )
    parser.add_argument("--status", choices=sorted(DIAPER_STATUSES), default="wet")
    parser.add_argument("--test-device-file", type=Path, default=Path(DEFAULT_TEST_DEVICE_FILE))
    parser.add_argument("--state-file", type=Path, default=Path(DEFAULT_TEST_STATE_FILE))
    parser.add_argument("--env-file", type=Path, default=Path(DEFAULT_ENV_FILE))
    parser.add_argument("--device-file", type=Path, default=Path(DEFAULT_DEVICE_FILE))
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=argparse.SUPPRESS)
    return parser


async def run_cli(argv: Sequence[str], environ: Mapping[str, str], out: TextIO, err: TextIO) -> int:
    """Run the tool. Returns the exit code: 0 ok, 1 service error, 2 missing credentials."""
    args = _parser().parse_args(argv)
    if args.write_test_diaper or args.delete_test_diaper:
        return await _run_test_write(args, environ, out, err)
    try:
        email, password = load_credentials(environ, args.env_file)
    except CredentialsError as error:
        print(f"error: {error}", file=err)
        return 2
    device_id = load_or_create_device_id(args.device_file)
    async with BabyTrackerClient(
        email,
        password,
        device_id,
        PROBE_DEVICE_NAME,
        base_url=args.base_url,
        # A custom base URL means a local test server, which may be an IP address.
        unsafe_cookie_jar=args.base_url != DEFAULT_BASE_URL,
    ) as client:
        try:
            if args.probe_history:
                lines = format_probe(await probe_history(client, device_id))
            else:
                engine = SyncEngine()
                await engine.sync(client)
                lines = format_latest(engine)
        except AuthenticationError:
            print("error: the service rejected the email or password", file=err)
            return 1
        except BabyTrackerError as error:
            print(f"error: {error}", file=err)
            return 1
    for line in lines:
        print(line, file=out)
    return 0


def _device_line(when: str, devices: list[Device], device_id: str) -> str:
    own = next((d for d in devices if d.device_uuid == device_id), None)
    if own is None:
        return f"{when}: {PROBE_DEVICE_NAME} is not in the device list"
    return f"{when}: {PROBE_DEVICE_NAME} is in the device list, LastSyncID {own.last_sync_id}"


def _load_engine(path: Path, device_id: str) -> SyncEngine:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return SyncEngine(device_uuid=device_id)
    return SyncEngine.from_dict(data, device_uuid=device_id)


async def _run_test_write(
    args: argparse.Namespace, environ: Mapping[str, str], out: TextIO, err: TextIO
) -> int:
    """Add or delete one diaper on the test account, then check the echo on the next sync."""
    try:
        email, password = load_credentials(environ, args.env_file, TEST_CREDENTIALS)
    except CredentialsError as error:
        print(f"error: {error}", file=err)
        return 2
    device_id = load_or_create_device_id(args.test_device_file)
    engine = _load_engine(args.state_file, device_id)
    lines: list[str] = []
    async with BabyTrackerClient(
        email,
        password,
        device_id,
        PROBE_DEVICE_NAME,
        base_url=args.base_url,
        unsafe_cookie_jar=args.base_url != DEFAULT_BASE_URL,
    ) as client:
        try:
            lines.append(_device_line("before the write", await client.get_devices(), device_id))
            await engine.sync(client)
            if args.delete_test_diaper:
                await engine.delete_record(client, args.delete_test_diaper)
                lines.append(f"deleted {args.delete_test_diaper}")
            else:
                babies = sorted(engine.babies().values(), key=lambda b: (b.name or "", b.object_id))
                if not babies:
                    print("error: the test account has no baby to log a diaper for", file=err)
                    return 1
                diaper = await engine.add_diaper(
                    client, babies[0].object_id, DIAPER_STATUSES[args.status]
                )
                assert diaper.time is not None
                lines.append(
                    f"added a {args.status} diaper {diaper.object_id} for "
                    f"{babies[0].name or '(unnamed)'} at {format_time(diaper.time)} "
                    f"({diaper.time.astimezone():%Y-%m-%d %H:%M:%S %Z} local)"
                )
            devices = await client.get_devices()
            lines.append(_device_line("after the write", devices, device_id))
            own_due = any(d.device_uuid == device_id for d in engine.devices_to_fetch(devices))
            records_before = len(engine.records)
            result = await engine.sync(client)
            lines.append(
                f"next sync: own log fetched: {'yes' if own_due else 'no'}, "
                f"applied: {result.transactions_applied}, "
                f"records {records_before} -> {len(engine.records)}"
            )
        except AuthenticationError:
            print("error: the service rejected the email or password", file=err)
            return 1
        except BabyTrackerError as error:
            print(f"error: {error}", file=err)
            return 1
        finally:
            args.state_file.write_text(json.dumps(engine.to_dict()) + "\n", encoding="utf-8")
    for line in lines:
        print(line, file=out)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(
        run_cli(sys.argv[1:] if argv is None else argv, os.environ, sys.stdout, sys.stderr)
    )
