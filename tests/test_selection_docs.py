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


# A medicine synced as its own transaction was captured on 2026-11-11, so it is no longer unseen.
@pytest.mark.parametrize("model", [MilestoneSelection, VaccineSelection])
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


def test_a_medicine_is_no_longer_marked_as_not_yet_seen() -> None:
    assert UNSEEN not in (MedicationSelection.__doc__ or "")


def _sentence_with(text: str, phrase: str) -> str:
    flat = " ".join(text.split())
    start = flat.rfind(". ", 0, flat.index(phrase)) + 2
    return flat[start : flat.index(".", flat.index(phrase))]


def test_the_readme_does_not_list_a_medicine_among_the_unseen_transactions() -> None:
    sentence = _sentence_with((ROOT / "README.md").read_text(), UNSEEN)
    assert "MilestoneSelection" in sentence and "VaccineSelection" in sentence
    assert "MedicationSelection" not in sentence


def test_the_selections_docstring_does_not_call_a_medicine_embed_only() -> None:
    doc = " ".join((selections.__doc__ or "").split())
    sentence = _sentence_with(doc, UNSEEN)
    assert "medicine" not in sentence
    assert "arrives only embedded" not in doc
    assert "medicine" in doc and "transaction of its own" in doc
