"""What the library says about its pick lists: nothing from the app's internals, and what is and
isn't known from synced data."""

from __future__ import annotations

from pathlib import Path

import pytest

from pybabytracker import (
    MedicationSelection,
    MilestoneSelection,
    VaccineSelection,
    selections,
)

ROOT = Path(__file__).resolve().parent.parent
# Built from pieces so this file doesn't contain what it searches for.
BANNED = ["BTSQLite" + "OpenHelper", "DefaultSeed" + "Prefix", "(app" + ":"]
UNSEEN = "expected shape, not yet seen in synced data"


def _text_files() -> list[Path]:
    files = [ROOT / "README.md"]
    for folder in ("src", "tests", "docs", "scripts"):
        files += [p for p in (ROOT / folder).rglob("*") if p.suffix in {".py", ".md"}]
    return [p for p in files if p != Path(__file__).resolve()]


@pytest.mark.parametrize("banned", BANNED)
def test_nothing_refers_to_the_apps_internals(banned: str) -> None:
    hits = [str(p.relative_to(ROOT)) for p in _text_files() if banned in p.read_text()]
    assert hits == []


@pytest.mark.parametrize("model", [MilestoneSelection, VaccineSelection, MedicationSelection])
def test_the_selection_transactions_are_marked_as_not_yet_seen(model: type) -> None:
    assert UNSEEN in (model.__doc__ or "")


def test_a_medicines_interval_is_documented_in_minutes() -> None:
    doc = MedicationSelection.__doc__ or ""
    assert "interval" in doc and "minutes" in doc


def test_the_built_in_ids_say_which_were_checked_live() -> None:
    doc = selections.__doc__ or ""
    assert "Back home" in doc and "HepB" in doc
    assert "iOS" in doc and "unverified" in doc


def test_the_readme_marks_the_selection_transactions_as_not_yet_seen() -> None:
    assert UNSEEN in (ROOT / "README.md").read_text()
