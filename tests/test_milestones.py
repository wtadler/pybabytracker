"""Milestones: the pick list a record can name, and logging one."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from factories import BABY_ID, BABY_TWO_ID, payload_for
from fake_server import FakeSyncServer
from selection_helpers import posted, rid, seed, synced

from pybabytracker import (
    BUILT_IN_MILESTONES,
    BabyTrackerClient,
    Milestone,
    MilestoneSelection,
    UnknownMilestoneError,
    decode_payload,
)

EVENT_TIME = datetime(2026, 11, 7, 23, 28, 53, tzinfo=UTC)
BACK_HOME = "EasyLogSeed3b250600-894f-11e2-9e96-0800200c9a66"
FIRST_SMILE = "EasyLogSeed56cc56b0-894f-11e2-9e96-0800200c9a66"

# The keys an Android phone is reported to send for a milestone (no capture is kept here, so
# this is the expected shape, not yet seen in synced data here), besides baby.
ANDROID_MILESTONE_KEYS = {
    "BCObjectType",
    "hasNewType",
    "milestoneType",
    "note",
    "pictureLoaded",
    "pictureNote",
    "time",
    "timezone",
    "newFlage",
    "objectID",
    "timestamp",
}
ANDROID_SELECTION_KEYS = {"forAge", "used", "desc", "name", "newFlage", "objectID", "timestamp"}


def selection(object_id: str, name: str, **overrides: Any) -> dict[str, Any]:
    """A MilestoneSelection a user added in the app, with invented values."""
    return {
        "BCObjectType": "MilestoneSelection",
        "objectID": object_id,
        "name": name,
        "desc": f" {name.lower()}",
        "forAge": 0,
        "used": "false",
        "newFlage": "true",
        "timestamp": "2026-11-01 10:00:00 +0000",
        **overrides,
    }


def test_the_built_in_milestones_are_the_apps_twenty() -> None:
    assert len(BUILT_IN_MILESTONES) == 20
    assert BUILT_IN_MILESTONES[0].name == "Back home"
    assert BUILT_IN_MILESTONES[0].object_id == BACK_HOME
    assert all(c.object_id.startswith("EasyLogSeed") and c.built_in for c in BUILT_IN_MILESTONES)
    assert len({c.object_id for c in BUILT_IN_MILESTONES}) == 20
    assert len({c.name for c in BUILT_IN_MILESTONES}) == 20


def test_a_synced_milestone_selection_decodes() -> None:
    record = decode_payload(selection("CUSTOM-1", "First swim", babyID=BABY_ID))
    assert isinstance(record, MilestoneSelection)
    assert (record.name, record.for_age, record.baby_id) == ("First swim", 0, BABY_ID)


async def test_logging_a_built_in_milestone_by_name_embeds_the_apps_seed(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    record = await engine.add_milestone(
        client, BABY_ID, "  first SMILE ", time=EVENT_TIME, timezone_offset=-21600
    )

    assert isinstance(record, Milestone)
    assert record.milestone_name == "First smile"
    [payload] = posted(fake_server)
    assert set(payload) - {"baby"} == ANDROID_MILESTONE_KEYS
    embedded = payload["milestoneType"]
    assert embedded["objectID"] == FIRST_SMILE
    assert embedded["desc"] == "'s first smile"
    assert set(embedded) - {"BCObjectType"} == ANDROID_SELECTION_KEYS
    assert payload["hasNewType"] == "false"
    assert payload["baby"]["objectID"] == BABY_ID
    assert engine.records[record.object_id] == record


async def test_a_milestone_can_be_named_by_its_id(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    record = await engine.add_milestone(client, BABY_ID, BACK_HOME.lower(), time=EVENT_TIME)

    assert record.milestone_name == "Back home"


async def test_a_milestone_added_in_the_app_can_be_logged_and_is_embedded_as_the_iphone_does(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    custom = selection("CUSTOM-1", "First swim", desc=" swam")
    seed(fake_server, custom)
    engine = await synced(client)

    await engine.add_milestone(client, BABY_ID, "first swim", time=EVENT_TIME)

    [payload] = posted(fake_server)
    # As the iPhone app embeds a food or a medicine synced on its own (#62, #69): everything as
    # synced, but not new and dated 1970.
    assert payload["milestoneType"] == {
        **custom,
        "newFlage": "false",
        "timestamp": "1970-01-01 00:00:00 +0000",
    }


async def test_a_milestone_added_for_another_baby_is_not_offered(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, selection("CUSTOM-1", "First swim", babyID=BABY_TWO_ID))
    engine = await synced(client)

    assert "First swim" not in [c.name for c in engine.milestone_choices(BABY_ID)]
    assert "First swim" in [c.name for c in engine.milestone_choices(BABY_TWO_ID)]
    assert "First swim" in [c.name for c in engine.milestone_choices()]
    with pytest.raises(UnknownMilestoneError):
        await engine.add_milestone(client, BABY_ID, "First swim", time=EVENT_TIME)
    assert posted(fake_server) == []


async def test_the_choices_list_built_ins_first_then_added_ones_by_name(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, selection("CUSTOM-2", "zoo trip"), selection("CUSTOM-1", "Apple"))
    engine = await synced(client)

    names = [c.name for c in engine.milestone_choices(BABY_ID)]

    assert names[:20] == [c.name for c in BUILT_IN_MILESTONES]
    assert names[20:] == ["Apple", "zoo trip"]


async def test_a_used_built_in_keeps_the_copy_the_app_embedded(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    # The shape reported for an Android phone: no BCObjectType, and JSON booleans (expected
    # shape, not yet seen in synced data here).
    android_copy = {
        "forAge": 0,
        "used": False,
        "desc": " came home",
        "name": "Back home",
        "newFlage": False,
        "objectID": BACK_HOME,
        "timestamp": "1970-01-01 00:00:00 +0000",
    }
    seed(fake_server, payload_for("Milestone", rid(2), milestoneType=android_copy))
    engine = await synced(client)

    [back_home] = [c for c in engine.milestone_choices(BABY_ID) if c.object_id == BACK_HOME]

    assert back_home.built_in
    assert back_home.selection == android_copy


async def test_an_unknown_milestone_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    requests_before = len(fake_server.requests)

    with pytest.raises(UnknownMilestoneError):
        await engine.add_milestone(client, BABY_ID, "Learns to fly", time=EVENT_TIME)
    with pytest.raises(ValueError):
        await engine.add_milestone(client, BABY_ID, "  ", time=EVENT_TIME)

    assert len(fake_server.requests) == requests_before


async def test_a_milestone_for_an_unknown_baby_is_refused(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    with pytest.raises(Exception, match="no baby"):
        await engine.add_milestone(client, "NOT-A-BABY", "First smile", time=EVENT_TIME)
    assert posted(fake_server) == []
