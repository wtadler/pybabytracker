"""The release workflow's tag-vs-version gate, exercised as the workflow runs it."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_version.py"
REAL_VERSION_FILE = Path(__file__).resolve().parent.parent / "src/pybabytracker/_version.py"


def run(tag: str, version: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    version_file = tmp_path / "_version.py"
    version_file.write_text(f'__version__ = "{version}"\n')
    return subprocess.run(
        [sys.executable, str(SCRIPT), tag, "--version-file", str(version_file)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("tag", "version"),
    [
        ("v0.2.0", "0.2.0"),
        ("0.2.0", "0.2.0"),
        ("v0.2.0rc1", "0.2.0rc1"),
        ("0.2.0rc1", "0.2.0rc1"),
        ("v1.0.0.post1", "1.0.0.post1"),
        ("v0.2.0\n", "0.2.0"),
        ("  v0.2.0  ", "0.2.0"),
    ],
)
def test_matching_tag_exits_zero(tag: str, version: str, tmp_path: Path) -> None:
    result = run(tag, version, tmp_path)
    assert result.returncode == 0, result.stderr
    assert f"matches {version}" in result.stdout


@pytest.mark.parametrize(
    ("tag", "version"),
    [
        ("v0.3.0", "0.2.0"),
        ("0.3.0", "0.2.0"),
        ("v0.2.0", "0.2.0rc1"),
        ("v0.2.0rc1", "0.2.0"),
        ("v0.2.0rc2", "0.2.0rc1"),
        ("v0.2", "0.2.0"),
        ("vv0.2.0", "0.2.0"),
        ("V0.2.0", "0.2.0"),
    ],
)
def test_mismatched_tag_exits_nonzero_and_names_both(
    tag: str, version: str, tmp_path: Path
) -> None:
    result = run(tag, version, tmp_path)
    assert result.returncode == 1
    assert version in result.stderr
    assert tag.strip() in result.stderr


@pytest.mark.parametrize("tag", ["", "   ", "v", "\n"])
def test_empty_tag_is_rejected(tag: str, tmp_path: Path) -> None:
    result = run(tag, "0.2.0", tmp_path)
    assert result.returncode == 1
    assert "empty" in result.stderr.lower()


def test_missing_version_in_file_is_rejected(tmp_path: Path) -> None:
    version_file = tmp_path / "_version.py"
    version_file.write_text("# nothing here\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "v0.1.0", "--version-file", str(version_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "__version__" in result.stderr


def test_missing_version_file_is_rejected(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "v0.1.0", "--version-file", str(tmp_path / "nope.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "nope.py" in result.stderr


def test_defaults_to_the_packages_real_version_file() -> None:
    import re

    version = re.search(r'__version__ = "([^"]+)"', REAL_VERSION_FILE.read_text())
    assert version is not None
    ok = subprocess.run(
        [sys.executable, str(SCRIPT), f"v{version.group(1)}"],
        capture_output=True,
        text=True,
        check=False,
        cwd=SCRIPT.parent.parent,
    )
    assert ok.returncode == 0, ok.stderr
    bad = subprocess.run(
        [sys.executable, str(SCRIPT), "v999.0.0"],
        capture_output=True,
        text=True,
        check=False,
        cwd=SCRIPT.parent.parent,
    )
    assert bad.returncode == 1


@pytest.mark.parametrize(
    "version",
    [
        "0.2.0-rc1",
        "0.2.0.rc1",
        "0.2.0RC1",
        "v0.2.0",
        "0.2.0+Local",
        "1.0.0-1",
        "01.2.0",
        "abc",
        "0.2.0 beta",
    ],
)
def test_non_canonical_version_in_file_is_rejected_even_when_tag_matches(
    version: str, tmp_path: Path
) -> None:
    result = run(version, version, tmp_path)
    assert result.returncode == 1
    assert "PEP 440" in result.stderr


@pytest.mark.parametrize(
    "version",
    [
        "0",
        "1.0",
        "0.2.0",
        "2026.10",
        "1!2.0.0",
        "0.2.0a1",
        "0.2.0b2",
        "0.2.0rc1",
        "0.2.0.post1",
        "0.2.0.dev3",
        "0.2.0rc1.post2.dev4",
        "0.2.0+local.1",
    ],
)
def test_canonical_versions_are_accepted(version: str, tmp_path: Path) -> None:
    result = run(f"v{version}", version, tmp_path)
    assert result.returncode == 0, result.stderr
