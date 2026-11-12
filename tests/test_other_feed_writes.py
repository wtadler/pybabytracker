"""Foods, and logging a solid food (invented data only)."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import pytest
from factories import BABY_ID, payload_for
from fake_server import FakeSyncServer
from selection_helpers import posted, rid, seed, synced

from pybabytracker import (
    BUILT_IN_FOODS,
    MAX_OTHER_FEED_AMOUNT,
    AccountChangedError,
    BabyTrackerClient,
    OtherFeed,
    UnknownFoodError,
)

EVENT_TIME = datetime(2026, 11, 10, 17, 45, tzinfo=UTC)
FRUIT = "EasyLogSeed72504c30-8c55-11e2-9e96-0800200c9a66"
PUREE = "38000000-0000-4000-8000-0000000000F0"
# The keys the iPhone app sends for a solid food (and baby).
OTHER_FEED_KEYS = {
    "BCObjectType",
    "feedType",
    "desc",
    "amount",
    "unit",
    "note",
    "pictureLoaded",
    "pictureNote",
    "time",
    "timezone",
    "newFlage",
    "objectID",
    "timestamp",
}


def puree(n: int, **food: Any) -> dict[str, Any]:
    """A solid food of a "Puree" food added in the app, as another phone synced it."""
    payload = payload_for("OtherFeed", rid(n))
    payload["feedType"] = {"objectID": PUREE, "name": "Puree", "bottle": "false"} | food
    payload["desc"] = {"objectID": "MEAL-1", "newFlage": "true"}
    return payload


def test_the_built_in_foods_are_the_apps_nine() -> None:
    names = [c.name for c in BUILT_IN_FOODS]
    assert names == [
        "Cereal",
        "Veggies",
        "Fruit",
        "Meat",
        "Finger Foods",
        "Vitamins",
        "Water",
        "Juice",
        "Milk",
    ]
    drinks = [c.name for c in BUILT_IN_FOODS if c.selection["bottle"] == "true"]
    assert drinks == ["Water", "Juice", "Milk"]
    assert all(c.object_id.startswith("EasyLogSeed") for c in BUILT_IN_FOODS)
    assert all(c.selection["BCObjectType"] == "OtherFeedSelection" for c in BUILT_IN_FOODS)


async def test_logging_a_built_in_food_writes_what_the_iphone_app_syncs(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    record = await engine.add_other_feed(
        client, BABY_ID, "fruit", amount=4, unit="Oz", time=EVENT_TIME
    )

    assert isinstance(record, OtherFeed)
    assert (record.food_name, record.quantity, record.unit) == ("Fruit", 4, "Oz")
    [payload] = posted(fake_server)
    assert set(payload) - {"baby"} == OTHER_FEED_KEYS
    assert payload["feedType"] == {
        "BCObjectType": "OtherFeedSelection",
        "objectID": FRUIT,
        "name": "Fruit",
        "desc": "",
        "bottle": "false",
        "newFlage": "false",
        "timestamp": "1970-01-01 00:00:00 +0000",
    }
    assert payload["amount"] == {
        "BCObjectType": "VolumeMeasure",
        "englishMeasure": "true",
        "value": 4,
    }
    assert payload["unit"] == "Oz"
    desc = payload["desc"]
    assert set(desc) == {"BCObjectType", "newFlage", "objectID", "timestamp"}
    assert (desc["BCObjectType"], desc["newFlage"]) == ("FeedDescription", "true")
    assert desc["objectID"] == record.meal_id
    assert payload["time"] == "2026-11-10 17:45:00 +0000"


async def test_a_count_unit_is_still_written_as_an_english_measure(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    record = await engine.add_other_feed(
        client, BABY_ID, "Finger Foods", amount=0.5, unit=" Pieces ", time=EVENT_TIME
    )

    assert (record.quantity, record.unit) == (0.5, "Pieces")
    [payload] = posted(fake_server)
    assert payload["amount"]["englishMeasure"] == "true"
    assert payload["unit"] == "Pieces"


async def test_foods_of_one_meal_share_its_meal_id(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    first = await engine.add_other_feed(
        client, BABY_ID, "Fruit", amount=2, unit="Oz", time=EVENT_TIME
    )
    second = await engine.add_other_feed(
        client, BABY_ID, "Meat", amount=1, unit="Oz", meal_id=first.meal_id, time=EVENT_TIME
    )
    third = await engine.add_other_feed(
        client, BABY_ID, "Veggies", amount=1, unit="Oz", time=EVENT_TIME
    )

    assert first.meal_id is not None
    assert second.meal_id == first.meal_id
    assert third.meal_id != first.meal_id
    meals = [p["desc"]["objectID"] for p in posted(fake_server)]
    assert meals == [first.meal_id, first.meal_id, third.meal_id]


@pytest.mark.parametrize(
    "food",
    [
        {"BCObjectType": "OtherFeedSelection", "timestamp": "1970-01-01 00:00:00 +0000"},
        # Android sends the food without BCObjectType.
        {"newFlage": False, "bottle": False},
    ],
)
async def test_a_food_added_in_the_app_is_offered_and_logged_as_its_records_carry_it(
    client: BabyTrackerClient, fake_server: FakeSyncServer, food: dict[str, Any]
) -> None:
    seed(fake_server, puree(2, **food))
    engine = await synced(client)

    assert [c.name for c in engine.food_choices()][-1] == "Puree"
    record = await engine.add_other_feed(
        client, BABY_ID, "puree", amount=3, unit="Tsp", time=EVENT_TIME
    )

    assert record.food_name == "Puree"
    [payload] = posted(fake_server)
    assert payload["feedType"]["objectID"] == PUREE
    assert payload["feedType"]["name"] == "Puree"


async def test_a_synced_food_is_offered(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    food = {"BCObjectType": "OtherFeedSelection", "objectID": "F-1", "name": "Avocado"}
    seed(fake_server, food)
    engine = await synced(client)

    assert engine.find_food("avocado").object_id == "F-1"


async def test_a_synced_meal_id_is_read(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server, puree(2))
    engine = await synced(client)

    record = engine.records[rid(2)]
    assert isinstance(record, OtherFeed)
    assert record.meal_id == "MEAL-1"


def test_a_database_row_reads_its_desc_id_as_the_meal() -> None:
    record = OtherFeed.from_payload(payload_for("OtherFeed"))
    assert record.meal_id == "desc-1"


async def test_an_unknown_food_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(UnknownFoodError) as raised:
        await engine.add_other_feed(client, BABY_ID, "Puree", amount=1, unit="Oz")
    assert raised.value.what == "food"
    with pytest.raises(ValueError):
        await engine.add_other_feed(client, BABY_ID, "", amount=1, unit="Oz")
    assert len(fake_server.requests) == before


@pytest.mark.parametrize(
    "bad",
    [
        {"amount": 0},
        {"amount": 0.001},  # zero once written
        {"amount": -1},
        {"amount": -(10**400)},
        {"amount": math.nan},
        {"amount": math.inf},
        {"amount": True},
        {"amount": "2"},
        {"amount": MAX_OTHER_FEED_AMOUNT + 1},
        {"unit": ""},
        {"unit": "   "},
        {"unit": 1},
        {"unit": "x" * 31},
        {"meal_id": ""},
        {"meal_id": "  "},
    ],
)
async def test_a_bad_amount_unit_or_meal_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, bad: dict[str, Any]
) -> None:
    seed(fake_server)
    engine = await synced(client)
    before = len(fake_server.requests)
    args: dict[str, Any] = {"amount": 1, "unit": "Oz"} | bad

    with pytest.raises(ValueError):
        await engine.add_other_feed(client, BABY_ID, "Fruit", time=EVENT_TIME, **args)
    assert len(fake_server.requests) == before


async def test_the_largest_amount_is_allowed(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    record = await engine.add_other_feed(
        client, BABY_ID, "Water", amount=MAX_OTHER_FEED_AMOUNT, unit="ml", time=EVENT_TIME
    )
    assert record.quantity == MAX_OTHER_FEED_AMOUNT


async def test_a_food_after_the_group_is_reset_posts_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    fake_server.account_id = 777  # the group was reset in the app
    fake_server.sessions.clear()

    with pytest.raises(AccountChangedError):
        await engine.add_other_feed(client, BABY_ID, "Fruit", amount=1, unit="Oz", time=EVENT_TIME)
    assert posted(fake_server) == []
