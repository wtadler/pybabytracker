from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import pytest
from conftest import PROBE_UUID
from factories import BABY_ID, BABY_TWO_ID, baby_payload, payload_for
from fake_server import FakeSyncServer

from pybabytracker import OP_ADD, BabyTrackerClient
from pybabytracker.cli import (
    PROBE_DEVICE_NAME,
    CredentialsError,
    load_credentials,
    load_env_file,
    load_or_create_device_id,
    run_cli,
)

PHONE = "AAAA1111-0000-4000-8000-00000000000A"
REPO_ROOT = Path(__file__).resolve().parent.parent


def rid(n: int) -> str:
    return f"AAAAAAAA-0000-4000-8000-{n:012d}"


def test_env_file_parsing(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "\n"
        "BT_EMAIL=parent@example.test\n"
        "export BT_PASSWORD='test pass=word'\n"
        'QUOTED="double quoted"\n'
        "SPACED = spaced value \n"
        "no equals sign\n",
        encoding="utf-8",
    )
    assert load_env_file(env) == {
        "BT_EMAIL": "parent@example.test",
        "BT_PASSWORD": "test pass=word",
        "QUOTED": "double quoted",
        "SPACED": "spaced value",
    }


def test_a_missing_env_file_is_an_empty_mapping(tmp_path: Path) -> None:
    assert load_env_file(tmp_path / "nope.env") == {}


def test_credentials_come_from_the_environment_before_the_env_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("BT_EMAIL=file@example.test\nBT_PASSWORD=file-password\n", encoding="utf-8")
    email, password = load_credentials({"BT_EMAIL": "env@example.test"}, env)
    assert (email, password) == ("env@example.test", "file-password")


def test_missing_credentials_raise_without_naming_values(tmp_path: Path) -> None:
    with pytest.raises(CredentialsError) as excinfo:
        load_credentials({"BT_EMAIL": "env@example.test"}, tmp_path / ".env")
    assert "BT_PASSWORD" in str(excinfo.value)


def test_the_probe_device_id_is_created_once_and_reused(tmp_path: Path) -> None:
    path = tmp_path / ".pybabytracker-device.json"
    first = load_or_create_device_id(path)
    second = load_or_create_device_id(path)
    assert first == second
    assert len(first) == 36
    assert json.loads(path.read_text(encoding="utf-8"))["device_uuid"] == first


def test_a_corrupt_device_file_is_replaced(tmp_path: Path) -> None:
    path = tmp_path / ".pybabytracker-device.json"
    path.write_text("not json", encoding="utf-8")
    new_id = load_or_create_device_id(path)
    assert len(new_id) == 36
    assert load_or_create_device_id(path) == new_id
    assert json.loads(path.read_text(encoding="utf-8"))["device_uuid"] == new_id


def test_the_device_file_and_env_file_are_gitignored() -> None:
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignored
    assert ".pybabytracker-device.json" in ignored


def test_the_probe_device_is_named_home_assistant_dev() -> None:
    assert PROBE_DEVICE_NAME == "Home Assistant (dev)"


def _args(fake: FakeSyncServer, tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--base-url",
        fake.base_url,
        "--env-file",
        str(tmp_path / ".env"),
        "--device-file",
        str(tmp_path / "device.json"),
        *extra,
    ]


def _env(fake: FakeSyncServer, password: str | None = None) -> dict[str, str]:
    return {"BT_EMAIL": fake.email, "BT_PASSWORD": password or fake.password}


def _seed(fake: FakeSyncServer) -> None:
    fake.add_device(PHONE)
    other = baby_payload("Second Baby", BABY_TWO_ID)
    fake.add_transaction(
        PHONE, OP_ADD, payload_for("Diaper", rid(1), time="2026-10-25 10:00:00 +0000", status=0)
    )
    fake.add_transaction(
        PHONE,
        OP_ADD,
        payload_for(
            "Diaper", rid(2), time="2026-10-25 12:00:00 +0000", status=1, note="Légère fuite"
        ),
    )
    fake.add_transaction(PHONE, OP_ADD, payload_for("Sleep", rid(3), duration=1800))
    fake.add_transaction(PHONE, OP_ADD, payload_for("Formula", rid(4), baby=other))


async def test_the_cli_prints_each_babys_latest_record_of_each_type(
    fake_server: FakeSyncServer, tmp_path: Path
) -> None:
    _seed(fake_server)
    out, err = io.StringIO(), io.StringIO()

    code = await run_cli(_args(fake_server, tmp_path), _env(fake_server), out, err)

    text = out.getvalue()
    assert code == 0, err.getvalue()
    assert "Test Baby" in text
    assert "Second Baby" in text
    assert text.count("Diaper") == 1  # only the latest diaper, not both
    assert "Légère fuite" in text
    assert "Sleep" in text
    assert "Formula" in text
    first_baby_section = text.split("Second Baby")[0]
    assert "Formula" not in first_baby_section
    assert BABY_ID not in err.getvalue()


async def test_the_cli_uses_the_probe_device_and_reuses_it_on_a_second_run(
    fake_server: FakeSyncServer, tmp_path: Path
) -> None:
    _seed(fake_server)
    for _ in range(2):
        await run_cli(_args(fake_server, tmp_path), _env(fake_server), io.StringIO(), io.StringIO())
    sign_ins = fake_server.calls("POST", "/session")
    assert len(sign_ins) == 2
    first, second = (r.body["Device"] for r in sign_ins)
    assert first == second
    assert first["DeviceName"] == "Home Assistant (dev)"
    assert first["DeviceUUID"] != PROBE_UUID


async def test_missing_credentials_exit_2_with_a_message_and_no_network(
    fake_server: FakeSyncServer, tmp_path: Path
) -> None:
    out, err = io.StringIO(), io.StringIO()
    code = await run_cli(_args(fake_server, tmp_path), {}, out, err)
    assert code == 2
    assert "BT_EMAIL" in err.getvalue()
    assert fake_server.requests == []


async def test_a_bad_password_exits_1_without_printing_it(
    fake_server: FakeSyncServer, tmp_path: Path
) -> None:
    out, err = io.StringIO(), io.StringIO()
    code = await run_cli(
        _args(fake_server, tmp_path), _env(fake_server, "wrong-password"), out, err
    )
    assert code == 1
    assert "wrong-password" not in out.getvalue() + err.getvalue()
    assert err.getvalue().strip()


async def test_probe_history_reports_returned_versus_last_sync_id_per_device(
    fake_server: FakeSyncServer, tmp_path: Path
) -> None:
    _seed(fake_server)  # PHONE: 4 transactions, LastSyncID 4
    fake_server.add_device("BBBB2222-0000-4000-8000-00000000000B", "Test Tablet")
    fake_server.add_transaction(
        "BBBB2222-0000-4000-8000-00000000000B", OP_ADD, payload_for("Diaper", rid(9))
    )
    fake_server.add_transaction(
        "BBBB2222-0000-4000-8000-00000000000B", OP_ADD, payload_for("Diaper", rid(10)), sync_id=7
    )
    fake_server.add_device("CCCC3333-0000-4000-8000-00000000000C", "Empty Device")
    out, err = io.StringIO(), io.StringIO()

    code = await run_cli(
        _args(fake_server, tmp_path, "--probe-history"), _env(fake_server), out, err
    )

    text = out.getvalue()
    assert code == 0, err.getvalue()
    assert "returned 4 of LastSyncID 4" in text
    assert "returned 2 of LastSyncID 7" in text
    assert "returned 0 of LastSyncID 0" in text
    assert "Home Assistant (dev) appears in the device list: yes" in text
    # The probe only reads: no log is written back and no latest-record output.
    assert not fake_server.calls("POST", "/account/transaction")


async def test_probe_history_says_when_the_probe_device_did_not_join(
    fake_server: FakeSyncServer, tmp_path: Path
) -> None:
    fake_server.register_devices = False
    fake_server.add_device(PHONE)
    out, err = io.StringIO(), io.StringIO()
    code = await run_cli(
        _args(fake_server, tmp_path, "--probe-history"), _env(fake_server), out, err
    )
    assert code == 0, err.getvalue()
    assert "appears in the device list: no" in out.getvalue()


TABLET = "BBBB2222-0000-4000-8000-00000000000B"


async def _probe(fake: FakeSyncServer, tmp_path: Path) -> str:
    out, err = io.StringIO(), io.StringIO()
    code = await run_cli(_args(fake, tmp_path, "--probe-history"), _env(fake), out, err)
    assert code == 0, err.getvalue()
    return out.getvalue()


def _device_with(fake: FakeSyncServer, count: int) -> None:
    fake.add_device(TABLET, "Test Tablet")
    for i in range(count):
        fake.add_transaction(TABLET, OP_ADD, payload_for("Diaper", rid(100 + i)))


async def test_probe_verdict_full(fake_server: FakeSyncServer, tmp_path: Path) -> None:
    _device_with(fake_server, 5)
    text = await _probe(fake_server, tmp_path)
    assert "returned 5 of LastSyncID 5, lowest 1, highest 5: full history" in text


async def test_probe_verdict_paged(fake_server: FakeSyncServer, tmp_path: Path) -> None:
    _device_with(fake_server, 5)
    fake_server.page_size = 2
    text = await _probe(fake_server, tmp_path)
    assert "returned 2 of LastSyncID 5" in text
    assert "paged (2 per request)" in text
    assert "NOT the full history" not in text.split("returned 2 of")[1].splitlines()[0]


async def test_probe_verdict_trimmed(fake_server: FakeSyncServer, tmp_path: Path) -> None:
    _device_with(fake_server, 5)
    fake_server.trim_below = 3
    text = await _probe(fake_server, tmp_path)
    assert "returned 3 of LastSyncID 5, lowest 3, highest 5" in text
    assert "starts at 3 (older numbers trimmed)" in text


async def test_probe_verdict_gaps(fake_server: FakeSyncServer, tmp_path: Path) -> None:
    fake_server.add_device(TABLET, "Test Tablet")
    for n in (1, 2, 4, 5):
        fake_server.add_transaction(TABLET, OP_ADD, payload_for("Diaper", rid(n)), sync_id=n)
    text = await _probe(fake_server, tmp_path)
    assert "returned 4 of LastSyncID 5, lowest 1, highest 5" in text
    assert "gaps" in text


# --- newest-first paging, duplicates, and the reworded gaps verdict -----------------------


def _log_numbers(fake: FakeSyncServer, numbers: list[int]) -> None:
    fake.add_device(TABLET, "Test Tablet")
    for n in numbers:
        fake.add_transaction(TABLET, OP_ADD, payload_for("Diaper", rid(100 + n)), sync_id=n)


def _tablet_line(text: str) -> str:
    return next(line for line in text.splitlines() if "Test Tablet" in line)


def _tablet_reads(fake: FakeSyncServer) -> list[str]:
    return [r.path for r in fake.calls("GET", f"/account/transaction/{TABLET}/")]


async def test_a_server_that_returns_only_the_newest_entries_is_paged_not_trimmed(
    fake_server: FakeSyncServer, tmp_path: Path
) -> None:
    _device_with(fake_server, 10)
    fake_server.page_size = 3
    fake_server.newest_first = True
    line = _tablet_line(await _probe(fake_server, tmp_path))
    assert "returned 3 of LastSyncID 10, lowest 8, highest 10" in line
    assert "paged (newest first)" in line
    assert "trimmed" not in line


async def test_the_newest_first_check_reads_from_two_below_the_lowest_number(
    fake_server: FakeSyncServer, tmp_path: Path
) -> None:
    _device_with(fake_server, 10)
    fake_server.page_size = 3
    fake_server.newest_first = True
    await _probe(fake_server, tmp_path)
    assert _tablet_reads(fake_server) == [
        f"/account/transaction/{TABLET}/0",
        f"/account/transaction/{TABLET}/6",
    ]


async def test_a_server_that_really_trims_still_reports_the_start_number(
    fake_server: FakeSyncServer, tmp_path: Path
) -> None:
    _device_with(fake_server, 10)
    fake_server.trim_below = 4
    line = _tablet_line(await _probe(fake_server, tmp_path))
    assert "starts at 4 (older numbers trimmed)" in line
    assert "paged" not in line
    assert _tablet_reads(fake_server)[-1] == f"/account/transaction/{TABLET}/2"


async def test_a_trim_that_starts_at_two_is_reported_without_the_extra_read(
    fake_server: FakeSyncServer, tmp_path: Path
) -> None:
    _device_with(fake_server, 6)
    fake_server.trim_below = 2
    line = _tablet_line(await _probe(fake_server, tmp_path))
    assert "starts at 2 (older numbers trimmed)" in line
    assert _tablet_reads(fake_server) == [f"/account/transaction/{TABLET}/0"]


async def test_a_full_log_needs_no_extra_read(fake_server: FakeSyncServer, tmp_path: Path) -> None:
    _device_with(fake_server, 6)
    line = _tablet_line(await _probe(fake_server, tmp_path))
    assert "full history" in line
    assert _tablet_reads(fake_server) == [f"/account/transaction/{TABLET}/0"]


async def test_newest_first_with_a_page_as_large_as_the_log_is_full_history(
    fake_server: FakeSyncServer, tmp_path: Path
) -> None:
    _device_with(fake_server, 6)
    fake_server.page_size = 6
    fake_server.newest_first = True
    assert "full history" in _tablet_line(await _probe(fake_server, tmp_path))


async def test_the_newest_first_check_writes_nothing(
    fake_server: FakeSyncServer, tmp_path: Path
) -> None:
    _device_with(fake_server, 10)
    fake_server.page_size = 3
    fake_server.newest_first = True
    await _probe(fake_server, tmp_path)
    assert not fake_server.calls("POST", "/account/transaction")


async def test_duplicate_numbers_are_reported_as_duplicates_not_gaps(
    fake_server: FakeSyncServer, tmp_path: Path
) -> None:
    _log_numbers(fake_server, [1, 2, 3, 3, 4])
    line = _tablet_line(await _probe(fake_server, tmp_path))
    assert "returned 5 of LastSyncID 4" in line
    assert "duplicates" in line
    assert "gaps" not in line
    assert "full history" not in line


async def test_duplicates_and_gaps_in_one_log_are_both_reported(
    fake_server: FakeSyncServer, tmp_path: Path
) -> None:
    _log_numbers(fake_server, [1, 2, 2, 4, 5])
    line = _tablet_line(await _probe(fake_server, tmp_path))
    assert "duplicates" in line
    assert "gaps: 4 of 5 numbers present" in line


async def test_the_gaps_verdict_names_how_many_numbers_are_present(
    fake_server: FakeSyncServer, tmp_path: Path
) -> None:
    _log_numbers(fake_server, [1, 2, 4, 5])
    line = _tablet_line(await _probe(fake_server, tmp_path))
    assert "gaps: 4 of 5 numbers present" in line
    assert "duplicates" not in line
    assert "every number from 1" not in line


async def test_a_log_with_no_duplicates_or_gaps_mentions_neither(
    fake_server: FakeSyncServer, tmp_path: Path
) -> None:
    _device_with(fake_server, 5)
    line = _tablet_line(await _probe(fake_server, tmp_path))
    assert "duplicates" not in line
    assert "gaps" not in line


# --- the fake server's newest-first option -----------------------------------------------


async def test_the_fake_newest_first_option_returns_the_newest_page_from_zero(
    fake_server: FakeSyncServer, client: BabyTrackerClient
) -> None:
    _device_with(fake_server, 10)
    fake_server.page_size = 3
    fake_server.newest_first = True
    assert [t.sync_id for t in await client.get_transactions(TABLET, 0)] == [8, 9, 10]


async def test_the_fake_newest_first_option_pages_backward_from_a_later_number(
    fake_server: FakeSyncServer, client: BabyTrackerClient
) -> None:
    _device_with(fake_server, 10)
    fake_server.page_size = 3
    fake_server.newest_first = True
    assert [t.sync_id for t in await client.get_transactions(TABLET, 6)] == [5, 6, 7]


async def test_the_fake_returns_oldest_first_pages_unless_newest_first_is_set(
    fake_server: FakeSyncServer, client: BabyTrackerClient
) -> None:
    _device_with(fake_server, 10)
    fake_server.page_size = 3
    assert [t.sync_id for t in await client.get_transactions(TABLET, 0)] == [1, 2, 3]


# --- writing a test diaper ----------------------------------------------------------------


def _write_args(fake: FakeSyncServer, tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--base-url",
        fake.base_url,
        "--env-file",
        str(tmp_path / ".env"),
        "--test-device-file",
        str(tmp_path / "test-device.json"),
        "--state-file",
        str(tmp_path / "state.json"),
        *extra,
    ]


def _test_env(fake: FakeSyncServer) -> dict[str, str]:
    return {"BT_TEST_EMAIL": fake.email, "BT_TEST_PASSWORD": fake.password}


async def _write(fake: FakeSyncServer, tmp_path: Path, *extra: str) -> str:
    out, err = io.StringIO(), io.StringIO()
    code = await run_cli(_write_args(fake, tmp_path, *extra), _test_env(fake), out, err)
    assert code == 0, err.getvalue()
    return out.getvalue()


def _test_device_id(tmp_path: Path) -> str:
    device_id: str = json.loads((tmp_path / "test-device.json").read_text())["device_uuid"]
    return device_id


async def test_write_test_diaper_needs_the_test_account_credentials_not_the_main_ones(
    fake_server: FakeSyncServer, tmp_path: Path
) -> None:
    out, err = io.StringIO(), io.StringIO()
    code = await run_cli(
        _write_args(fake_server, tmp_path, "--write-test-diaper"), _env(fake_server), out, err
    )
    assert code == 2
    assert "BT_TEST_EMAIL" in err.getvalue()
    assert fake_server.requests == []


async def test_write_test_diaper_adds_one_diaper_as_the_dev_device_and_reports_the_device_list(
    fake_server: FakeSyncServer, tmp_path: Path
) -> None:
    fake_server.register_devices = False
    _seed(fake_server)

    text = await _write(fake_server, tmp_path, "--write-test-diaper")

    device_id = _test_device_id(tmp_path)
    assert device_id != PROBE_UUID
    [session] = {r.body["Device"]["DeviceName"] for r in fake_server.calls("POST", "/session")}
    assert session == "Home Assistant (dev)"
    [entry] = fake_server.posted(device_id)
    assert entry["SyncID"] == 1
    assert "before the write: Home Assistant (dev) is not in the device list" in text
    assert "after the write: Home Assistant (dev) is in the device list, LastSyncID 1" in text
    assert "own log fetched: no" in text
    assert "applied: 0" in text
    assert "wet diaper" in text
    assert (tmp_path / "state.json").exists()


async def test_delete_test_diaper_deletes_the_diaper_the_dev_device_wrote(
    fake_server: FakeSyncServer, tmp_path: Path
) -> None:
    fake_server.register_devices = False
    _seed(fake_server)
    await _write(fake_server, tmp_path, "--write-test-diaper", "--status", "dirty")
    device_id = _test_device_id(tmp_path)
    object_id = json.loads(base64.b64decode(fake_server.posted(device_id)[0]["Transaction"]))[
        "objectID"
    ]

    text = await _write(fake_server, tmp_path, "--delete-test-diaper", object_id)

    assert [e["OPCode"] for e in fake_server.posted(device_id)] == [0, 2]
    assert "after the write: Home Assistant (dev) is in the device list, LastSyncID 2" in text
    assert "own log fetched: no" in text


async def test_delete_test_diaper_refuses_another_devices_record(
    fake_server: FakeSyncServer, tmp_path: Path
) -> None:
    _seed(fake_server)
    out, err = io.StringIO(), io.StringIO()
    code = await run_cli(
        _write_args(fake_server, tmp_path, "--delete-test-diaper", rid(1)),
        _test_env(fake_server),
        out,
        err,
    )
    assert code == 1
    assert "wasn't created by this device" in err.getvalue()
    assert not fake_server.calls("POST", "/account/transaction")


def test_the_test_device_and_state_files_are_gitignored() -> None:
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".pybabytracker-test-device.json" in ignored
    assert ".pybabytracker-test-state.json" in ignored
