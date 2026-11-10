"""Logging a growth measurement (invented data only)."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import pytest
from factories import BABY_ID
from fake_server import FakeSyncServer
from selection_helpers import posted, seed, synced

from pybabytracker import (
    GROWTH_RANGES,
    UNIT_CENTIMETERS,
    UNIT_INCHES,
    UNIT_KILOGRAMS,
    UNIT_POUNDS,
    AccountChangedError,
    BabyTrackerClient,
    Growth,
    Measurement,
)

EVENT_TIME = datetime(2026, 11, 10, 16, 30, tzinfo=UTC)
# The keys every record the apps sync carries (and baby), before any measurement.
BASE_KEYS = {
    "BCObjectType",
    "note",
    "pictureLoaded",
    "pictureNote",
    "time",
    "timezone",
    "newFlage",
    "objectID",
    "timestamp",
}


async def test_a_weight_alone_is_written_with_no_length_or_head(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    record = await engine.add_growth(
        client, BABY_ID, weight=Measurement(9.25, UNIT_POUNDS), time=EVENT_TIME, note="Checkup"
    )

    assert isinstance(record, Growth)
    assert record.weight_reading == Measurement(9.25, UNIT_POUNDS)
    assert (record.length_reading, record.head_reading) == (None, None)
    [payload] = posted(fake_server)
    assert set(payload) - {"baby"} == BASE_KEYS | {"weight"}
    assert payload["BCObjectType"] == "Growth"
    assert payload["weight"] == {
        "BCObjectType": "WeightMeasure",
        "englishMeasure": "true",
        "value": 9.25,
    }
    assert (payload["time"], payload["note"]) == ("2026-11-10 16:30:00 +0000", "Checkup")


async def test_all_three_metric_measurements_are_written_as_given(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    record = await engine.add_growth(
        client,
        BABY_ID,
        weight=Measurement(4.2, UNIT_KILOGRAMS),
        length=Measurement(55, UNIT_CENTIMETERS),
        head=Measurement(37.5, UNIT_CENTIMETERS),
        time=EVENT_TIME,
    )

    assert record.weight_reading == Measurement(4.2, UNIT_KILOGRAMS)
    assert record.length_reading == Measurement(55, UNIT_CENTIMETERS)
    assert record.head_reading == Measurement(37.5, UNIT_CENTIMETERS)
    [payload] = posted(fake_server)
    assert set(payload) - {"baby"} == BASE_KEYS | {"weight", "length", "head"}
    assert payload["weight"]["englishMeasure"] == "false"
    assert payload["length"] == {
        "BCObjectType": "LengthMeasure",
        "englishMeasure": "false",
        "value": 55,
    }
    assert payload["head"]["BCObjectType"] == "LengthMeasure"
    assert payload["head"]["value"] == 37.5


async def test_a_length_in_inches_is_english(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    await engine.add_growth(client, BABY_ID, length=Measurement(21.5, UNIT_INCHES))

    [payload] = posted(fake_server)
    assert set(payload) - {"baby"} == BASE_KEYS | {"length"}
    assert payload["length"]["englishMeasure"] == "true"


async def test_a_logged_growth_is_applied_locally(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    record = await engine.add_growth(
        client, BABY_ID, head=Measurement(14, UNIT_INCHES), time=EVENT_TIME
    )

    assert engine.records[record.object_id] == record
    assert engine.latest_records(BABY_ID)["Growth"] == record


@pytest.mark.parametrize(
    "measures",
    [
        {},
        {"weight": None, "length": None, "head": None},
        {"length": Measurement(21, UNIT_INCHES), "head": Measurement(36, UNIT_CENTIMETERS)},
        {"weight": Measurement(9, UNIT_INCHES)},
        {"length": Measurement(55, UNIT_KILOGRAMS)},
        {"weight": Measurement(9, "oz")},
        {"weight": Measurement(0, UNIT_POUNDS)},
        {"weight": Measurement(-1, UNIT_KILOGRAMS)},
        {"weight": Measurement(150, UNIT_POUNDS)},
        {"head": Measurement(90, UNIT_CENTIMETERS)},
        {"weight": Measurement(math.nan, UNIT_POUNDS)},
        {"weight": Measurement(True, UNIT_POUNDS)},
        {"weight": (9.25, UNIT_POUNDS)},
        {"weight": 9.25},
    ],
)
async def test_bad_growth_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, measures: dict[str, Any]
) -> None:
    seed(fake_server)
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(ValueError):
        await engine.add_growth(client, BABY_ID, time=EVENT_TIME, **measures)
    assert len(fake_server.requests) == before


@pytest.mark.parametrize(
    ("name", "reading"),
    [
        (name, Measurement(value, unit))
        for name, units in GROWTH_RANGES.items()
        for unit, pair in units.items()
        for value in pair
    ],
)
async def test_the_ends_of_each_range_are_allowed(
    client: BabyTrackerClient, fake_server: FakeSyncServer, name: str, reading: Measurement
) -> None:
    seed(fake_server)
    engine = await synced(client)

    measures: dict[str, Any] = {name: reading}
    record = await engine.add_growth(client, BABY_ID, time=EVENT_TIME, **measures)
    assert getattr(record, f"{name}_reading") == reading


async def test_a_growth_after_the_group_is_reset_posts_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    fake_server.account_id = 777  # the group was reset in the app
    fake_server.sessions.clear()

    with pytest.raises(AccountChangedError):
        await engine.add_growth(
            client, BABY_ID, weight=Measurement(10, UNIT_POUNDS), time=EVENT_TIME
        )
    assert posted(fake_server) == []
