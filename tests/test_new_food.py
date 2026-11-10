"""Adding a food to the pick list, and how a solid food embeds one (invented data only)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from factories import BABY_ID
from fake_server import FakeSyncServer
from selection_helpers import posted, seed, synced

from pybabytracker import (
    MAX_FOOD_NAME_LENGTH,
    AccountChangedError,
    BabyTrackerClient,
    OtherFeedSelection,
    SelectionExistsError,
)

EVENT_TIME = datetime(2026, 11, 10, 18, 0, tzinfo=UTC)
SEED_TIME = "1970-01-01 00:00:00 +0000"
FRUIT = "EasyLogSeed72504c30-8c55-11e2-9e96-0800200c9a66"
# The keys both apps send for a new food: no record fields at all.
FOOD_KEYS = {"BCObjectType", "objectID", "name", "bottle", "newFlage", "timestamp"}
IPHONE_FOOD = {
    "BCObjectType": "OtherFeedSelection",
    "objectID": "F0000000-0000-4000-8000-000000000001",
    "name": "Oats",
    "bottle": "false",
    "newFlage": "true",
    "timestamp": "2026-02-25 16:33:51 +0000",
}
# Android sends JSON bools and keeps the food's own timestamp.
ANDROID_FOOD = {
    "BCObjectType": "OtherFeedSelection",
    "objectID": "f0000000-0000-4000-8000-000000000002",
    "name": "Puree",
    "bottle": False,
    "newFlage": True,
    "timestamp": "2026-02-25 23:40:08 +0000",
}


async def test_a_new_food_is_posted_with_only_its_own_keys(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    food = await engine.add_food(client, "  Avocado ")

    assert isinstance(food, OtherFeedSelection)
    assert (food.name, food.bottle, food.new_flag) == ("Avocado", "false", "true")
    [payload] = posted(fake_server)
    assert set(payload) == FOOD_KEYS
    assert payload["BCObjectType"] == "OtherFeedSelection"
    assert (payload["name"], payload["bottle"], payload["newFlage"]) == ("Avocado", "false", "true")
    assert payload["objectID"] == food.object_id
    assert payload["timestamp"] != SEED_TIME


async def test_a_drink_is_offered_on_the_bottle_screen(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    await engine.add_food(client, "Coconut Water", bottle=True)

    [payload] = posted(fake_server)
    assert payload["bottle"] == "true"


async def test_a_new_food_is_offered_and_a_solid_food_embeds_it_as_the_iphone_does(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    food = await engine.add_food(client, "Avocado")
    assert engine.find_food("avocado").object_id == food.object_id
    record = await engine.add_other_feed(
        client, BABY_ID, food.object_id, amount=2, unit="Tbs", time=EVENT_TIME
    )

    assert record.food_name == "Avocado"
    food_post, feed_post = posted(fake_server)
    assert food_post["BCObjectType"] == "OtherFeedSelection"
    assert feed_post["feedType"] == {
        "BCObjectType": "OtherFeedSelection",
        "objectID": food.object_id,
        "name": "Avocado",
        "bottle": "false",
        "newFlage": "false",
        "timestamp": SEED_TIME,
    }


@pytest.mark.parametrize("food", [IPHONE_FOOD, ANDROID_FOOD])
async def test_a_synced_food_decodes_and_is_embedded_typed_old_and_not_new(
    client: BabyTrackerClient, fake_server: FakeSyncServer, food: dict[str, Any]
) -> None:
    seed(fake_server, food)
    engine = await synced(client)

    synced_food = engine.records[food["objectID"]]
    assert isinstance(synced_food, OtherFeedSelection)
    assert synced_food.name == food["name"]
    await engine.add_other_feed(client, BABY_ID, food["name"], amount=1, unit="Oz")

    [payload] = posted(fake_server)
    embedded = payload["feedType"]
    assert embedded["objectID"] == food["objectID"]
    assert embedded["BCObjectType"] == "OtherFeedSelection"
    assert (embedded["newFlage"], embedded["timestamp"]) == ("false", SEED_TIME)
    assert embedded["bottle"] == food["bottle"]


async def test_a_built_in_food_is_embedded_unchanged(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    await engine.add_other_feed(client, BABY_ID, "Fruit", amount=1, unit="Oz")

    [payload] = posted(fake_server)
    assert payload["feedType"] == dict(engine.find_food(FRUIT).selection)


@pytest.mark.parametrize(
    ("name", "existing"),
    [("fruit", FRUIT), ("  FRUIT ", FRUIT), ("oats", IPHONE_FOOD["objectID"])],
)
async def test_a_name_already_on_the_list_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, name: str, existing: str
) -> None:
    seed(fake_server, IPHONE_FOOD)
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(SelectionExistsError) as raised:
        await engine.add_food(client, name)
    assert (raised.value.what, raised.value.object_id) == ("food", existing)
    assert len(fake_server.requests) == before


@pytest.mark.parametrize(
    "bad",
    [
        {"name": ""},
        {"name": "   "},
        {"name": 7},
        {"name": None},
        {"name": "x" * (MAX_FOOD_NAME_LENGTH + 1)},
        {"name": "Avocado", "bottle": "true"},
        {"name": "Avocado", "bottle": 1},
    ],
)
async def test_a_bad_name_or_bottle_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, bad: dict[str, Any]
) -> None:
    seed(fake_server)
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(ValueError):
        await engine.add_food(client, **bad)
    assert len(fake_server.requests) == before


async def test_the_longest_name_is_allowed(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    food = await engine.add_food(client, "x" * MAX_FOOD_NAME_LENGTH)
    assert food.name == "x" * MAX_FOOD_NAME_LENGTH


async def test_retrying_a_food_that_landed_returns_it_without_a_second_post(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    object_id = "F0000000-0000-4000-8000-0000000000AA"

    first = await engine.add_food(client, "Avocado", object_id=object_id)
    again = await engine.add_food(client, "Avocado", object_id=object_id)

    assert again == first
    assert len(posted(fake_server)) == 1


async def test_a_food_after_the_group_is_reset_posts_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    fake_server.account_id = 777  # the group was reset in the app
    fake_server.sessions.clear()

    with pytest.raises(AccountChangedError):
        await engine.add_food(client, "Avocado")
    assert posted(fake_server) == []
