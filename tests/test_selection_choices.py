"""Pick lists that records embed: which copy of an entry wins, which baby sees it, duplicate names,
and what happens to bad input. Covers milestones, vaccines, and medicines alike."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

import pytest
from factories import BABY_ID, BABY_TWO_ID, baby_payload, payload_for
from fake_server import FakeSyncServer
from selection_helpers import posted, rid, seed, synced

from pybabytracker import (
    BUILT_IN_MILESTONES,
    BUILT_IN_VACCINES,
    AmbiguousSelectionError,
    BabyTrackerClient,
    InvalidSelectionNameError,
    SelectionChoice,
    SyncEngine,
    UnknownBabyError,
    UnknownMedicineError,
    UnknownMilestoneError,
    UnknownVaccineError,
    ValidationError,
    WriteError,
)
from pybabytracker.builders import draft_medication
from pybabytracker.selections import choice_from_selection

EVENT_TIME = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
OLD = "2026-11-01 10:00:00 +0000"
MIDDLE = "2026-11-02 10:00:00 +0000"
NEW = "2026-11-03 10:00:00 +0000"
BABY_TWO = baby_payload(object_id=BABY_TWO_ID)
BACK_HOME = BUILT_IN_MILESTONES[0].object_id
HEPB = BUILT_IN_VACCINES[0].object_id


@dataclass(frozen=True)
class Kind:
    name: str
    record_type: str
    key: str
    selection_type: str
    choices: Callable[[SyncEngine], list[SelectionChoice]]
    find: Callable[[SyncEngine, Any], SelectionChoice]
    add: Callable[[BabyTrackerClient, SyncEngine, Any, Any], Awaitable[Any]]
    unknown: type[WriteError]
    extra: dict[str, Any]


async def _add_milestone(
    client: BabyTrackerClient, engine: SyncEngine, baby_id: Any, wanted: Any
) -> Any:
    return await engine.add_milestone(client, baby_id, wanted, time=EVENT_TIME)


async def _add_vaccine(
    client: BabyTrackerClient, engine: SyncEngine, baby_id: Any, wanted: Any
) -> Any:
    return await engine.add_vaccine(client, baby_id, wanted, time=EVENT_TIME)


async def _add_medicine(
    client: BabyTrackerClient, engine: SyncEngine, baby_id: Any, wanted: Any
) -> Any:
    return await engine.add_medication(client, baby_id, wanted, amount=1, time=EVENT_TIME)


MILESTONE = Kind(
    "milestone",
    "Milestone",
    "milestoneType",
    "MilestoneSelection",
    lambda e: e.milestone_choices(BABY_ID),
    lambda e, w: e.find_milestone(w, BABY_ID),
    _add_milestone,
    UnknownMilestoneError,
    {"desc": " did it", "forAge": 0, "used": "false"},
)
VACCINE = Kind(
    "vaccine",
    "Vaccine",
    "vaccineType",
    "VaccineSelection",
    lambda e: e.vaccine_choices(),
    lambda e, w: e.find_vaccine(w),
    _add_vaccine,
    UnknownVaccineError,
    {"desc": "A test vaccine", "forAge": 0},
)
MEDICINE = Kind(
    "medicine",
    "Medication",
    "medicationSelection",
    "MedicationSelection",
    lambda e: e.medicine_choices(),
    lambda e, w: e.find_medicine(w),
    _add_medicine,
    UnknownMedicineError,
    {"unit": "ml", "amountPerTime": 5, "interval": 240},
)
KINDS = [MILESTONE, VACCINE, MEDICINE]
IDS = [k.name for k in KINDS]


def sel(kind: Kind, object_id: str, name: str, **overrides: Any) -> dict[str, Any]:
    """An entry of the kind's pick list, with invented values."""
    return {
        "BCObjectType": kind.selection_type,
        "objectID": object_id,
        "name": name,
        "newFlage": "false",
        "timestamp": "1970-01-01 00:00:00 +0000",
        **kind.extra,
        **overrides,
    }


def embed(
    kind: Kind,
    n: int,
    selection: dict[str, Any],
    *,
    time: str = OLD,
    timestamp: str = OLD,
    baby: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A record of the kind embedding ``selection``."""
    fields: dict[str, Any] = {kind.key: selection}
    return payload_for(
        kind.record_type, rid(n), time=time, timestamp=timestamp, baby=baby, **fields
    )


def only(engine: SyncEngine, kind: Kind, object_id: str) -> SelectionChoice:
    [choice] = [c for c in kind.choices(engine) if c.object_id == object_id]
    return choice


# --- M4: the newest record's copy wins, whatever order records arrived in ---------------------


@pytest.mark.parametrize("newest_first", [False, True], ids=["old-first", "new-first"])
@pytest.mark.parametrize("kind", KINDS, ids=IDS)
async def test_the_newest_records_copy_of_an_entry_wins_whatever_the_arrival_order(
    client: BabyTrackerClient, fake_server: FakeSyncServer, kind: Kind, newest_first: bool
) -> None:
    old = sel(kind, "CUSTOM-1", "Old name")
    new = sel(kind, "CUSTOM-1", "New name")
    records = [embed(kind, 2, old, timestamp=OLD), embed(kind, 3, new, timestamp=NEW)]
    seed(fake_server, *(records[::-1] if newest_first else records))
    engine = await synced(client)

    choice = only(engine, kind, "CUSTOM-1")

    assert choice.name == "New name"
    assert dict(choice.selection) == new
    assert kind.find(engine, "new name") is not None
    with pytest.raises(kind.unknown):
        kind.find(engine, "old name")


@pytest.mark.parametrize("newest_first", [False, True], ids=["old-first", "new-first"])
@pytest.mark.parametrize("kind", KINDS, ids=IDS)
async def test_a_records_write_timestamp_beats_its_event_time(
    client: BabyTrackerClient, fake_server: FakeSyncServer, kind: Kind, newest_first: bool
) -> None:
    # A dose logged late for an earlier time still embeds the current entry.
    stale = sel(kind, "CUSTOM-1", "Stale")
    current = sel(kind, "CUSTOM-1", "Current")
    records = [
        embed(kind, 2, stale, time=NEW, timestamp=OLD),
        embed(kind, 3, current, time=OLD, timestamp=NEW),
    ]
    seed(fake_server, *(records[::-1] if newest_first else records))
    engine = await synced(client)

    assert only(engine, kind, "CUSTOM-1").name == "Current"


@pytest.mark.parametrize("newest_first", [False, True], ids=["old-first", "new-first"])
@pytest.mark.parametrize("kind", KINDS, ids=IDS)
async def test_an_equal_write_timestamp_falls_back_to_the_event_time(
    client: BabyTrackerClient, fake_server: FakeSyncServer, kind: Kind, newest_first: bool
) -> None:
    earlier = sel(kind, "CUSTOM-1", "Earlier")
    later = sel(kind, "CUSTOM-1", "Later")
    records = [
        embed(kind, 2, earlier, time=OLD, timestamp=MIDDLE),
        embed(kind, 3, later, time=NEW, timestamp=MIDDLE),
    ]
    seed(fake_server, *(records[::-1] if newest_first else records))
    engine = await synced(client)

    assert only(engine, kind, "CUSTOM-1").name == "Later"


@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
@pytest.mark.parametrize("kind", KINDS, ids=IDS)
async def test_records_tied_on_time_and_timestamp_give_the_same_copy_in_any_order(
    client: BabyTrackerClient, fake_server: FakeSyncServer, kind: Kind, reverse: bool
) -> None:
    records = [
        embed(kind, 2, sel(kind, "CUSTOM-1", "From two")),
        embed(kind, 3, sel(kind, "CUSTOM-1", "From three")),
    ]
    seed(fake_server, *(records[::-1] if reverse else records))
    engine = await synced(client)

    # The tie goes to the record with the greater object ID.
    assert only(engine, kind, "CUSTOM-1").name == "From three"


@pytest.mark.parametrize("kind", KINDS, ids=IDS)
async def test_a_synced_selection_beats_a_newer_embedded_copy(
    client: BabyTrackerClient, fake_server: FakeSyncServer, kind: Kind
) -> None:
    synced_copy = sel(kind, "CUSTOM-1", "Synced name", timestamp=OLD)
    embedded = sel(kind, "CUSTOM-1", "Embedded name")
    seed(fake_server, embed(kind, 2, embedded, timestamp=NEW), synced_copy)
    engine = await synced(client)

    choice = only(engine, kind, "CUSTOM-1")

    assert choice.name == "Synced name"
    assert dict(choice.selection) == synced_copy


@pytest.mark.parametrize("newest_first", [False, True], ids=["old-first", "new-first"])
async def test_the_newest_copy_of_a_used_built_in_replaces_its_template(
    client: BabyTrackerClient, fake_server: FakeSyncServer, newest_first: bool
) -> None:
    old = {**BUILT_IN_MILESTONES[0].selection, "desc": " came home (old)"}
    new = {**BUILT_IN_MILESTONES[0].selection, "desc": " came home (new)"}
    records = [
        embed(MILESTONE, 2, old, timestamp=OLD),
        embed(MILESTONE, 3, new, timestamp=NEW),
    ]
    seed(fake_server, *(records[::-1] if newest_first else records))
    engine = await synced(client)

    choice = only(engine, MILESTONE, BACK_HOME)

    assert choice.built_in
    assert choice.selection["desc"] == " came home (new)"
    assert [c.object_id for c in MILESTONE.choices(engine)][0] == BACK_HOME


# --- M3: an entry added for one baby stays with that baby ------------------------------------


async def test_a_custom_milestone_used_only_for_another_baby_is_not_offered(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    swim = sel(MILESTONE, "CUSTOM-1", "First swim", babyID=BABY_TWO_ID)
    seed(fake_server, swim, embed(MILESTONE, 2, swim, baby=BABY_TWO))
    engine = await synced(client)
    before = len(fake_server.requests)

    assert "First swim" not in [c.name for c in engine.milestone_choices(BABY_ID)]
    assert "First swim" in [c.name for c in engine.milestone_choices(BABY_TWO_ID)]
    assert "First swim" in [c.name for c in engine.milestone_choices()]
    with pytest.raises(UnknownMilestoneError):
        await engine.add_milestone(client, BABY_ID, "First swim", time=EVENT_TIME)
    with pytest.raises(UnknownMilestoneError):
        await engine.add_milestone(client, BABY_ID, "custom-1", time=EVENT_TIME)
    assert len(fake_server.requests) == before
    assert posted(fake_server) == []


async def test_an_embedded_custom_milestone_follows_the_baby_whose_record_embeds_it(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    # No synced selection says whether it is for one baby, so only the baby that used it sees it.
    swim = sel(MILESTONE, "CUSTOM-1", "First swim")
    seed(fake_server, embed(MILESTONE, 2, swim, baby=BABY_TWO))
    engine = await synced(client)

    assert "First swim" not in [c.name for c in engine.milestone_choices(BABY_ID)]
    assert "First swim" in [c.name for c in engine.milestone_choices(BABY_TWO_ID)]
    assert "First swim" in [c.name for c in engine.milestone_choices()]


async def test_an_embedded_milestone_that_names_another_baby_is_not_offered(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    swim = sel(MILESTONE, "CUSTOM-1", "First swim", babyID=BABY_TWO_ID)
    seed(fake_server, embed(MILESTONE, 2, swim))  # embedded in a record of the default baby
    engine = await synced(client)

    assert "First swim" not in [c.name for c in engine.milestone_choices(BABY_ID)]


async def test_a_built_in_milestone_used_for_another_baby_is_still_offered(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    copy = {**BUILT_IN_MILESTONES[0].selection, "timestamp": "1970-01-01 00:00:00 +0000"}
    seed(fake_server, embed(MILESTONE, 2, copy, baby=BABY_TWO))
    engine = await synced(client)

    choice = only(engine, MILESTONE, BACK_HOME)

    assert choice.built_in
    assert dict(choice.selection) == copy


@pytest.mark.parametrize("scope", ["", None], ids=["empty", "null"])
async def test_a_milestone_with_no_baby_is_offered_to_every_baby(
    client: BabyTrackerClient, fake_server: FakeSyncServer, scope: str | None
) -> None:
    swim = sel(MILESTONE, "CUSTOM-1", "First swim", babyID=scope)
    seed(fake_server, swim)
    engine = await synced(client)

    for baby_id in (BABY_ID, BABY_TWO_ID, None):
        assert "First swim" in [c.name for c in engine.milestone_choices(baby_id)]


async def test_a_milestone_added_for_this_baby_is_offered_and_embedded_as_synced(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    swim = sel(MILESTONE, "CUSTOM-1", "First swim", babyID=BABY_ID)
    seed(fake_server, swim, embed(MILESTONE, 2, swim))
    engine = await synced(client)

    await engine.add_milestone(client, BABY_ID, "first swim", time=EVENT_TIME)

    [payload] = posted(fake_server)
    assert payload["milestoneType"] == swim


# --- M5: a name that matches two entries is never resolved silently ---------------------------


@pytest.mark.parametrize("kind", KINDS, ids=IDS)
async def test_a_name_shared_by_two_entries_is_ambiguous_and_an_id_is_not(
    client: BabyTrackerClient, fake_server: FakeSyncServer, kind: Kind
) -> None:
    first = sel(kind, "CUSTOM-1", "Twin")
    second = sel(kind, "CUSTOM-2", " TWIN ")
    seed(fake_server, embed(kind, 2, first), embed(kind, 3, second))
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(AmbiguousSelectionError) as caught:
        kind.find(engine, "twin")
    assert set(caught.value.object_ids) == {"CUSTOM-1", "CUSTOM-2"}
    assert "CUSTOM-1" in str(caught.value) and "CUSTOM-2" in str(caught.value)
    assert isinstance(caught.value, WriteError)
    with pytest.raises(AmbiguousSelectionError):
        await kind.add(client, engine, BABY_ID, "Twin")
    assert len(fake_server.requests) == before
    assert posted(fake_server) == []

    assert kind.find(engine, "CUSTOM-1").selection["name"] == "Twin"
    assert kind.find(engine, "custom-2").object_id == "CUSTOM-2"
    await kind.add(client, engine, BABY_ID, "CUSTOM-2")
    [payload] = posted(fake_server)
    assert payload[kind.key]["objectID"] == "CUSTOM-2"


@pytest.mark.parametrize("kind", KINDS, ids=IDS)
async def test_one_entry_embedded_many_times_is_not_ambiguous(
    client: BabyTrackerClient, fake_server: FakeSyncServer, kind: Kind
) -> None:
    one = sel(kind, "CUSTOM-1", "Solo")
    seed(fake_server, embed(kind, 2, one), embed(kind, 3, one, timestamp=NEW))
    engine = await synced(client)

    assert kind.find(engine, "solo").object_id == "CUSTOM-1"


async def test_a_custom_milestone_named_like_a_built_in_is_ambiguous_by_name_only(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, sel(MILESTONE, "CUSTOM-1", "Back home"))
    engine = await synced(client)

    with pytest.raises(AmbiguousSelectionError) as caught:
        engine.find_milestone("back home", BABY_ID)
    assert set(caught.value.object_ids) == {BACK_HOME, "CUSTOM-1"}
    assert engine.find_milestone(BACK_HOME, BABY_ID).built_in
    assert not engine.find_milestone("custom-1", BABY_ID).built_in


async def test_a_custom_vaccine_named_like_a_built_in_is_ambiguous_by_name_only(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, sel(VACCINE, "CUSTOM-1", "Influenza"))
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(AmbiguousSelectionError):
        await engine.add_vaccine(client, BABY_ID, "Influenza", time=EVENT_TIME)
    assert len(fake_server.requests) == before
    assert engine.find_vaccine("custom-1").object_id == "CUSTOM-1"


async def test_two_medicines_of_one_name_never_post_a_dose_against_either(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    drops_ml = sel(MEDICINE, "M-ML", "Test Drops", unit="ml", amountPerTime=3)
    drops_mg = sel(MEDICINE, "M-MG", "Test Drops", unit="mg", amountPerTime=120)
    seed(fake_server, embed(MEDICINE, 2, drops_ml), embed(MEDICINE, 3, drops_mg))
    engine = await synced(client)

    with pytest.raises(AmbiguousSelectionError):
        await engine.add_medication(client, BABY_ID, "test drops", time=EVENT_TIME)
    with pytest.raises(AmbiguousSelectionError):
        await engine.add_medication(client, BABY_ID, "test drops", amount=2, time=EVENT_TIME)
    assert posted(fake_server) == []

    await engine.add_medication(client, BABY_ID, "M-MG", time=EVENT_TIME)
    [payload] = posted(fake_server)
    assert payload["amount"] == 120
    assert payload["medicationSelection"] == drops_mg


# --- M6: the default dose is the newest copy's usual dose -------------------------------------


@pytest.mark.parametrize("newest_first", [False, True], ids=["old-first", "new-first"])
async def test_the_default_dose_follows_the_newest_dose_that_embeds_the_medicine(
    client: BabyTrackerClient, fake_server: FakeSyncServer, newest_first: bool
) -> None:
    old = sel(MEDICINE, "M-1", "Test Drops", amountPerTime=25)
    new = sel(MEDICINE, "M-1", "Test Drops", amountPerTime=30)
    records = [embed(MEDICINE, 2, old, timestamp=OLD), embed(MEDICINE, 3, new, timestamp=NEW)]
    seed(fake_server, *(records[::-1] if newest_first else records))
    engine = await synced(client)

    await engine.add_medication(client, BABY_ID, "test drops", time=EVENT_TIME)

    [payload] = posted(fake_server)
    assert payload["amount"] == 30
    assert payload["medicationSelection"] == new


async def test_a_synced_medicine_selection_sets_the_default_dose_over_embedded_copies(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    embedded = sel(MEDICINE, "M-1", "Test Drops", amountPerTime=30)
    current = sel(MEDICINE, "M-1", "Test Drops", amountPerTime=40)
    seed(fake_server, embed(MEDICINE, 2, embedded, timestamp=NEW), current)
    engine = await synced(client)

    await engine.add_medication(client, BABY_ID, "M-1", time=EVENT_TIME)

    [payload] = posted(fake_server)
    assert payload["amount"] == 40


@pytest.mark.parametrize(
    "usual",
    ["30", "", 0, -5, None, True, False, [30], {"v": 3}, 0.001, 1e999],
    ids=lambda v: repr(v),
)
async def test_a_usual_dose_that_isnt_a_positive_number_means_no_usual_dose(
    client: BabyTrackerClient, fake_server: FakeSyncServer, usual: Any
) -> None:
    odd = sel(MEDICINE, "M-1", "Test Drops", amountPerTime=usual)
    seed(fake_server, embed(MEDICINE, 2, odd))
    engine = await synced(client)

    with pytest.raises(ValueError, match="give an amount"):
        await engine.add_medication(client, BABY_ID, "test drops", time=EVENT_TIME)
    assert posted(fake_server) == []

    # With an amount of its own, the dose is fine.
    await engine.add_medication(client, BABY_ID, "test drops", amount=2.5, time=EVENT_TIME)
    [payload] = posted(fake_server)
    assert payload["amount"] == 2.5


def test_a_usual_dose_of_nan_means_no_usual_dose() -> None:
    odd = sel(MEDICINE, "M-1", "Test Drops", amountPerTime=math.nan)
    with pytest.raises(ValueError, match="give an amount"):
        draft_medication(odd, None, time=EVENT_TIME, timezone_offset=0, note="")


async def test_a_whole_or_fractional_usual_dose_is_written_as_the_app_would(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    half = sel(MEDICINE, "M-1", "Test Drops", amountPerTime=2.5)
    seed(fake_server, embed(MEDICINE, 2, half))
    engine = await synced(client)

    await engine.add_medication(client, BABY_ID, "m-1", time=EVENT_TIME)
    await engine.add_medication(client, BABY_ID, "m-1", amount=2.0, time=EVENT_TIME)
    await engine.add_medication(client, BABY_ID, "m-1", amount=0.126, time=EVENT_TIME)

    amounts = [p["amount"] for p in posted(fake_server)]
    assert amounts == [2.5, 2, 0.13]
    assert isinstance(amounts[1], int)


# --- bad input --------------------------------------------------------------------------------

BAD_NAMES: list[Any] = [
    "",
    " ",
    " \t\n ",
    None,
    5,
    0,
    True,
    False,
    1.5,
    b"Back home",
    ["First smile"],
    {},
]


@pytest.mark.parametrize("wanted", BAD_NAMES, ids=lambda v: repr(v))
@pytest.mark.parametrize("kind", KINDS, ids=IDS)
async def test_an_empty_or_non_text_name_raises_the_librarys_typed_error(
    client: BabyTrackerClient, fake_server: FakeSyncServer, kind: Kind, wanted: Any
) -> None:
    seed(fake_server, embed(kind, 2, sel(kind, "CUSTOM-1", "Real")))
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(InvalidSelectionNameError) as caught:
        kind.find(engine, wanted)
    assert isinstance(caught.value, ValueError)  # as before, for callers that catch it
    assert isinstance(caught.value, ValidationError)
    assert caught.value.value is wanted
    with pytest.raises(InvalidSelectionNameError):
        await kind.add(client, engine, BABY_ID, wanted)
    assert len(fake_server.requests) == before


@pytest.mark.parametrize(
    "wanted",
    ["EasyLogSeed00000000-0000-0000-0000-000000000000", "no-such-id", "Real!", "Re", "Real Real"],
)
@pytest.mark.parametrize("kind", KINDS, ids=IDS)
async def test_an_unknown_name_or_id_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, kind: Kind, wanted: str
) -> None:
    seed(fake_server, embed(kind, 2, sel(kind, "CUSTOM-1", "Real")))
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(kind.unknown):
        kind.find(engine, wanted)
    with pytest.raises(kind.unknown):
        await kind.add(client, engine, BABY_ID, wanted)
    assert len(fake_server.requests) == before


async def test_a_built_in_id_from_another_list_is_unknown(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    with pytest.raises(UnknownMilestoneError):
        engine.find_milestone(HEPB, BABY_ID)
    with pytest.raises(UnknownVaccineError):
        engine.find_vaccine(BACK_HOME)
    with pytest.raises(UnknownMedicineError):
        engine.find_medicine(BACK_HOME)


@pytest.mark.parametrize("baby_id", [None, "", " ", 5, True, "NOT-A-BABY", BABY_TWO_ID])
@pytest.mark.parametrize("kind", KINDS, ids=IDS)
async def test_a_write_for_no_baby_or_an_unknown_baby_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, kind: Kind, baby_id: Any
) -> None:
    seed(fake_server, embed(kind, 2, sel(kind, "CUSTOM-1", "Real")))
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(UnknownBabyError):
        await kind.add(client, engine, baby_id, "CUSTOM-1")
    assert len(fake_server.requests) == before
    assert posted(fake_server) == []


@pytest.mark.parametrize(
    "amount",
    [
        "2.5",
        "",
        [1],
        (1,),
        {"v": 1},
        b"1",
        True,
        False,
        0,
        0.0,
        -1,
        -0.5,
        math.nan,
        math.inf,
        -math.inf,
    ]
    + [0.001, 0.004],
    ids=lambda v: repr(v),
)
async def test_a_dose_amount_of_the_wrong_type_or_value_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, amount: Any
) -> None:
    seed(fake_server, embed(MEDICINE, 2, sel(MEDICINE, "M-1", "Test Drops")))
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(ValueError):
        await engine.add_medication(client, BABY_ID, "M-1", amount=amount, time=EVENT_TIME)
    assert len(fake_server.requests) == before
    assert posted(fake_server) == []


async def test_a_dose_of_an_unknown_medicine_with_a_bad_amount_names_the_medicine_first(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    with pytest.raises(UnknownMedicineError):
        await engine.add_medication(client, BABY_ID, "Nothing", amount=-1, time=EVENT_TIME)


# --- what a synced or embedded entry must look like to be listed ------------------------------


@pytest.mark.parametrize(
    "selection",
    [
        None,
        "text",
        5,
        [],
        {},
        {"name": "No ID"},
        {"objectID": "", "name": "Empty ID"},
        {"objectID": 7, "name": "Int ID"},
        {"objectID": None, "name": "Null ID"},
        {"objectID": "X"},
        {"objectID": "X", "name": None},
        {"objectID": "X", "name": 5},
        {"objectID": "X", "name": ["a"]},
    ],
    ids=lambda v: repr(v),
)
def test_an_entry_without_a_string_id_and_name_is_not_a_choice(selection: object) -> None:
    assert choice_from_selection(selection) is None


def test_an_entry_can_be_any_mapping_and_is_copied() -> None:
    original = {"objectID": "X", "name": "Thing", "extra": [1]}
    choice = choice_from_selection(MappingProxyType(original))

    assert choice is not None
    assert (choice.object_id, choice.name, choice.built_in) == ("X", "Thing", False)
    assert dict(choice.selection) == original


@pytest.mark.parametrize("kind", KINDS, ids=IDS)
async def test_entries_with_a_null_or_missing_name_are_left_out_of_the_list(
    client: BabyTrackerClient, fake_server: FakeSyncServer, kind: Kind
) -> None:
    nameless = {k: v for k, v in sel(kind, "CUSTOM-1", "x").items() if k != "name"}
    seed(
        fake_server,
        embed(kind, 2, nameless),
        embed(kind, 3, sel(kind, "CUSTOM-2", None)),  # type: ignore[arg-type]
        embed(kind, 4, sel(kind, "CUSTOM-3", "Fine")),
        sel(kind, "CUSTOM-4", None),  # type: ignore[arg-type]
    )
    engine = await synced(client)

    ids = {c.object_id for c in kind.choices(engine)}

    assert "CUSTOM-3" in ids
    assert not ids & {"CUSTOM-1", "CUSTOM-2", "CUSTOM-4"}


@pytest.mark.parametrize("kind", KINDS, ids=IDS)
async def test_a_record_that_embeds_nothing_is_ignored(
    client: BabyTrackerClient, fake_server: FakeSyncServer, kind: Kind
) -> None:
    null: dict[str, Any] = {kind.key: None}
    seed(
        fake_server,
        payload_for(kind.record_type, rid(2), **null),
        {k: v for k, v in payload_for(kind.record_type, rid(3)).items() if k != kind.key},
        sel(kind, "CUSTOM-1", "Fine"),
    )
    engine = await synced(client)

    assert "CUSTOM-1" in {c.object_id for c in kind.choices(engine)}


def test_a_choice_reads_its_unit_when_it_has_a_text_one() -> None:
    assert choice_from_selection({"objectID": "X", "name": "Y", "unit": "ml"}).unit == "ml"  # type: ignore[union-attr]
    for odd in (None, 5, True, ["ml"]):
        choice = choice_from_selection({"objectID": "X", "name": "Y", "unit": odd})
        assert choice is not None and choice.unit is None
    choice = choice_from_selection({"objectID": "X", "name": "Y"})
    assert choice is not None and choice.unit is None
