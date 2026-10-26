"""STUB for the red run."""

from __future__ import annotations

from pathlib import Path
from typing import Any, TextIO

PROBE_DEVICE_NAME = "Home Assistant (dev)"


class CredentialsError(Exception): ...


def load_env_file(path: Path) -> dict[str, str]:
    return {"x": "y"}


def load_credentials(environ: Any, env_file: Path) -> tuple[str, str]:
    return ("", "")


def load_or_create_device_id(path: Path) -> str:
    return ""


async def run_cli(argv: list[str], environ: Any, out: TextIO, err: TextIO) -> int:
    return 99
