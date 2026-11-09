"""Activities: the activity types a record can name, and logging one (tummy time, a bath)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from factories import BABY_ID, payload_for
from fake_server import FakeSyncServer
from selection_helpers import posted, rid, seed, synced

from pybabytracker import (
    BUILT_IN_ACTIVITIES,
    AccountChangedError,
    BabyTrackerClient,
    OtherActivity,
    UnknownActivityError,
)
from pybabytracker.builders import MAX_ACTIVITY_MINUTES

EVENT_TIME = datetime(2026, 11, 8, 22, 9, tzinfo=UTC)
TUMMY_TIME = "EasyLogSeed3559dca0-8cbf-11e2-9e96-0800200c9a66"
BATH_TYPE = "92000000-0000-4000-8000-0000000000BA"
# The keys the iPhone app sends for an activity (and baby).
ACTIVITY_KEYS = {
    "BCObjectType",
    "desc",
    "duration",
    "note",
    "pictureLoaded",
    "pictureNote",
    "time",
    "timezone",
    "newFlage",
    "objectID",
    "timestamp",
}


def bath(n: int, **desc: Any) -> dict[str, Any]:
    """An activity of a "Bath" type added in the app, as another phone synced it."""
    payload = payload_for("OtherActivity", rid(n))
    payload["desc"] = {"objectID": BATH_TYPE, "name": "Bath", "newFlage": "false"} | desc
    payload["duration"] = 10
    return payload


def test_the_built_in_activities_are_the_apps_five() -> None:
    names = [c.name for c in BUILT_IN_ACTIVITIES]
    assert names == ["Crawling", "Cruising", "Tummy Time", "TV Time", "Story Time"]
    assert all(c.object_id.startswith("EasyLogSeed") for c in BUILT_IN_ACTIVITIES)
    assert all(
        c.selection["BCObjectType"] == "OtherActivityDescription" for c in BUILT_IN_ACTIVITIES
    )


async def test_logging_a_built_in_activity_embeds_the_apps_seed(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    record = await engine.add_activity(client, BABY_ID, "tummy time", duration=15, time=EVENT_TIME)

    assert isinstance(record, OtherActivity)
    assert (record.activity_name, record.duration) == ("Tummy Time", 15)
    [payload] = posted(fake_server)
    assert set(payload) - {"baby"} == ACTIVITY_KEYS
    assert payload["desc"]["objectID"] == TUMMY_TIME
    assert payload["duration"] == 15
    assert payload["time"] == "2026-11-08 22:09:00 +0000"


async def test_an_untimed_activity_has_a_duration_of_zero(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    await engine.add_activity(client, BABY_ID, "Story Time", time=EVENT_TIME)

    [payload] = posted(fake_server)
    assert payload["duration"] == 0


@pytest.mark.parametrize(
    "desc",
    [
        {"BCObjectType": "OtherActivityDescription", "timestamp": "1970-01-01 00:00:00 +0000"},
        # Android sends the type without BCObjectType, with its own timestamp.
        {"newFlage": False, "timestamp": "2025-04-08 04:41:42 +0000"},
    ],
)
async def test_a_type_added_in_the_app_is_offered_and_logged_as_its_records_carry_it(
    client: BabyTrackerClient, fake_server: FakeSyncServer, desc: dict[str, Any]
) -> None:
    seed(fake_server, bath(2, **desc))
    engine = await synced(client)

    assert [c.name for c in engine.activity_choices()][-1] == "Bath"
    record = await engine.add_activity(client, BABY_ID, "bath", time=EVENT_TIME, note="Bubbles")

    assert (record.activity_name, record.note) == ("Bath", "Bubbles")
    [payload] = posted(fake_server)
    assert payload["desc"]["objectID"] == BATH_TYPE
    assert payload["desc"]["name"] == "Bath"


async def test_a_synced_activity_type_is_offered(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    kind = {"BCObjectType": "OtherActivityDescription", "objectID": "T-1", "name": "Swim"}
    seed(fake_server, kind)
    engine = await synced(client)

    assert engine.find_activity("swim").object_id == "T-1"


async def test_an_unknown_activity_or_bad_duration_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(UnknownActivityError) as raised:
        await engine.add_activity(client, BABY_ID, "Bath", time=EVENT_TIME)
    assert raised.value.what == "activity"
    with pytest.raises(ValueError):
        await engine.add_activity(client, BABY_ID, "", time=EVENT_TIME)
    bad: list[Any] = [-1, MAX_ACTIVITY_MINUTES + 1, 1.5, True]
    for duration in bad:
        with pytest.raises(ValueError):
            await engine.add_activity(
                client, BABY_ID, "Crawling", duration=duration, time=EVENT_TIME
            )
    assert len(fake_server.requests) == before


async def test_a_day_long_activity_is_allowed(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    record = await engine.add_activity(
        client, BABY_ID, "Crawling", duration=MAX_ACTIVITY_MINUTES, time=EVENT_TIME
    )
    assert record.duration == MAX_ACTIVITY_MINUTES


async def test_an_activity_after_the_group_is_reset_posts_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    fake_server.account_id = 777  # the group was reset in the app
    fake_server.sessions.clear()

    with pytest.raises(AccountChangedError):
        await engine.add_activity(client, BABY_ID, "Crawling", time=EVENT_TIME)
    assert posted(fake_server) == []


async def test_an_activity_refused_by_a_reset_at_the_post_leaves_nothing_pending(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    fake_server.reset_on_request = ("POST", "/account/transaction", 777)

    with pytest.raises(AccountChangedError):
        await engine.add_activity(client, BABY_ID, "Crawling", time=EVENT_TIME)
    assert posted(fake_server) == []
    assert not engine.to_dict()["pending"]
