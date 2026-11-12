"""Adding a milestone and a vaccine to the pick list (invented data only)."""

from __future__ import annotations

from typing import Any

import pytest
from factories import BABY_ID
from fake_server import FakeSyncServer
from selection_helpers import posted, seed, synced

from pybabytracker import (
    MAX_PICK_LIST_DESCRIPTION_LENGTH,
    MAX_PICK_LIST_NAME_LENGTH,
    AccountChangedError,
    BabyTrackerClient,
    MilestoneSelection,
    SelectionExistsError,
    VaccineSelection,
)

SEED_TIME = "1970-01-01 00:00:00 +0000"
# A built-in milestone's embedded copy has these keys and ``desc`` on iOS; a new one has no
# description to give.
MILESTONE_KEYS = {"BCObjectType", "objectID", "name", "forAge", "used", "newFlage", "timestamp"}
VACCINE_KEYS = {"BCObjectType", "objectID", "name", "forAge", "newFlage", "timestamp"}


async def test_a_new_milestone_is_posted_with_only_its_own_keys(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    milestone = await engine.add_milestone_type(client, "  First swim ")

    assert isinstance(milestone, MilestoneSelection)
    [payload] = posted(fake_server)
    assert set(payload) == MILESTONE_KEYS
    assert {k: payload[k] for k in MILESTONE_KEYS - {"objectID", "timestamp"}} == {
        "BCObjectType": "MilestoneSelection",
        "name": "First swim",
        "forAge": 0,
        "used": "false",
        "newFlage": "true",
    }
    assert payload["timestamp"] != SEED_TIME


async def test_a_new_milestone_is_offered_to_every_baby_and_embedded_as_the_iphone_does(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    milestone = await engine.add_milestone_type(client, "First swim")
    assert engine.find_milestone("first swim", BABY_ID).object_id == milestone.object_id
    record = await engine.add_milestone(client, BABY_ID, milestone.object_id)

    assert record.milestone_name == "First swim"
    _, reached = posted(fake_server)
    assert reached["milestoneType"] == {
        "BCObjectType": "MilestoneSelection",
        "objectID": milestone.object_id,
        "name": "First swim",
        "forAge": 0,
        "used": "false",
        "newFlage": "false",
        "timestamp": SEED_TIME,
    }


async def test_a_new_vaccine_is_posted_with_only_its_own_keys(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    vaccine = await engine.add_vaccine_type(client, " RSV ")

    assert isinstance(vaccine, VaccineSelection)
    [payload] = posted(fake_server)
    assert set(payload) == VACCINE_KEYS
    assert (payload["BCObjectType"], payload["name"], payload["forAge"], payload["newFlage"]) == (
        "VaccineSelection",
        "RSV",
        0,
        "true",
    )


async def test_a_vaccines_description_is_written_when_given(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    vaccine = await engine.add_vaccine_type(
        client, "RSV", description=" Respiratory syncytial virus "
    )
    record = await engine.add_vaccine(client, BABY_ID, "rsv")

    new, given = posted(fake_server)
    assert set(new) == VACCINE_KEYS | {"desc"}
    assert new["desc"] == "Respiratory syncytial virus"
    assert record.vaccine_name == "RSV"
    assert given["vaccineType"] == {
        **new,
        "objectID": vaccine.object_id,
        "newFlage": "false",
        "timestamp": SEED_TIME,
    }


@pytest.mark.parametrize(
    ("method", "name", "what"),
    [
        ("add_milestone_type", "first smile", "milestone"),  # built in
        ("add_milestone_type", "  FIRST SWIM ", "milestone"),  # added just before
        ("add_vaccine_type", "hepb", "vaccine"),  # built in
        ("add_vaccine_type", "rsv", "vaccine"),  # added just before
    ],
)
async def test_a_name_already_on_the_list_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, method: str, name: str, what: str
) -> None:
    seed(fake_server)
    engine = await synced(client)
    await engine.add_milestone_type(client, "First swim")
    await engine.add_vaccine_type(client, "RSV")
    before = len(fake_server.requests)

    with pytest.raises(SelectionExistsError) as raised:
        await getattr(engine, method)(client, name)
    assert raised.value.what == what
    assert len(fake_server.requests) == before


BAD_NAMES: list[Any] = ["", "   ", 7, None, "x" * (MAX_PICK_LIST_NAME_LENGTH + 1)]


@pytest.mark.parametrize("method", ["add_milestone_type", "add_vaccine_type"])
@pytest.mark.parametrize("name", BAD_NAMES)
async def test_a_bad_name_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, method: str, name: Any
) -> None:
    seed(fake_server)
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(ValueError):
        await getattr(engine, method)(client, name)
    assert len(fake_server.requests) == before


@pytest.mark.parametrize("description", [7, None, "x" * (MAX_PICK_LIST_DESCRIPTION_LENGTH + 1)])
async def test_a_bad_vaccine_description_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, description: Any
) -> None:
    seed(fake_server)
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(ValueError):
        await engine.add_vaccine_type(client, "RSV", description=description)
    assert len(fake_server.requests) == before


@pytest.mark.parametrize("method", ["add_milestone_type", "add_vaccine_type"])
async def test_a_retry_posts_nothing_more_and_a_reset_group_posts_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer, method: str
) -> None:
    seed(fake_server)
    engine = await synced(client)
    object_id = "C0000000-0000-4000-8000-0000000000AB"

    first = await getattr(engine, method)(client, "Test Entry", object_id=object_id)
    again = await getattr(engine, method)(client, "Test Entry", object_id=object_id.lower())
    assert again == first
    assert len(posted(fake_server)) == 1

    fake_server.account_id = 777  # the group was reset in the app
    fake_server.sessions.clear()
    with pytest.raises(AccountChangedError):
        await getattr(engine, method)(client, "Another Entry")
    assert len(posted(fake_server)) == 1
