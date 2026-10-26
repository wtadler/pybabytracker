from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from conftest import PROBE_UUID
from factories import BABY_ID, BABY_TWO_ID, baby_payload, payload_for
from fake_server import FakeSyncServer

from pybabytracker import OP_ADD
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
