"""Logging a temperature (invented data only)."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import pytest
from factories import BABY_ID
from fake_server import FakeSyncServer
from selection_helpers import posted, seed, synced

from pybabytracker import (
    TEMPERATURE_RANGES,
    UNIT_CELSIUS,
    UNIT_FAHRENHEIT,
    AccountChangedError,
    BabyTrackerClient,
    Measurement,
    Temperature,
)

EVENT_TIME = datetime(2026, 11, 10, 3, 15, tzinfo=UTC)
# The keys the iPhone app sends for a temperature (and baby).
TEMPERATURE_KEYS = {
    "BCObjectType",
    "temperature",
    "note",
    "pictureLoaded",
    "pictureNote",
    "time",
    "timezone",
    "newFlage",
    "objectID",
    "timestamp",
}


async def test_a_fahrenheit_temperature_is_written_as_the_iphone_app_syncs_one(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    record = await engine.add_temperature(
        client, BABY_ID, Measurement(100.4, UNIT_FAHRENHEIT), time=EVENT_TIME, note="Underarm"
    )

    assert isinstance(record, Temperature)
    assert record.reading == Measurement(100.4, UNIT_FAHRENHEIT)
    [payload] = posted(fake_server)
    assert set(payload) - {"baby"} == TEMPERATURE_KEYS
    assert payload["BCObjectType"] == "Temperature"
    assert payload["temperature"] == {
        "BCObjectType": "TemperatureMeasure",
        "englishMeasure": "true",
        "value": 100.4,
    }
    assert (payload["time"], payload["note"]) == ("2026-11-10 03:15:00 +0000", "Underarm")


async def test_a_celsius_temperature_is_written_as_given_with_no_conversion(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    record = await engine.add_temperature(
        client, BABY_ID, Measurement(37, UNIT_CELSIUS), time=EVENT_TIME
    )

    assert record.reading == Measurement(37, UNIT_CELSIUS)
    [payload] = posted(fake_server)
    assert payload["temperature"]["englishMeasure"] == "false"
    assert payload["temperature"]["value"] == 37


async def test_a_logged_temperature_is_applied_locally(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    record = await engine.add_temperature(
        client, BABY_ID, Measurement(98.6, UNIT_FAHRENHEIT), time=EVENT_TIME
    )

    assert engine.records[record.object_id] == record
    assert engine.latest_records(BABY_ID)["Temperature"] == record


@pytest.mark.parametrize(
    "reading",
    [
        Measurement(37, UNIT_FAHRENHEIT),  # Celsius given as Fahrenheit
        Measurement(98.6, UNIT_CELSIUS),  # Fahrenheit given as Celsius
        Measurement(310, "K"),
        Measurement(math.nan, UNIT_FAHRENHEIT),
        Measurement(math.inf, UNIT_CELSIUS),
        Measurement(True, UNIT_CELSIUS),
        Measurement("99", UNIT_FAHRENHEIT),  # type: ignore[arg-type]
        (99.0, UNIT_FAHRENHEIT),
        99.0,
    ],
)
async def test_a_bad_temperature_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, reading: Any
) -> None:
    seed(fake_server)
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(ValueError):
        await engine.add_temperature(client, BABY_ID, reading, time=EVENT_TIME)
    assert len(fake_server.requests) == before


@pytest.mark.parametrize(
    "reading",
    [
        Measurement(value, unit)
        for unit, (low, high) in TEMPERATURE_RANGES.items()
        for value in (round(low - 0.01, 2), round(high + 0.01, 2))
    ],
)
async def test_just_outside_each_range_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, reading: Measurement
) -> None:
    seed(fake_server)
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(ValueError):
        await engine.add_temperature(client, BABY_ID, reading, time=EVENT_TIME)
    assert len(fake_server.requests) == before


@pytest.mark.parametrize(
    "reading",
    [Measurement(value, unit) for unit, pair in TEMPERATURE_RANGES.items() for value in pair],
)
async def test_the_ends_of_each_range_are_allowed(
    client: BabyTrackerClient, fake_server: FakeSyncServer, reading: Measurement
) -> None:
    seed(fake_server)
    engine = await synced(client)

    record = await engine.add_temperature(client, BABY_ID, reading, time=EVENT_TIME)
    assert record.reading == reading


async def test_a_temperature_after_the_group_is_reset_posts_nothing(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)
    fake_server.account_id = 777  # the group was reset in the app
    fake_server.sessions.clear()

    with pytest.raises(AccountChangedError):
        await engine.add_temperature(
            client, BABY_ID, Measurement(99, UNIT_FAHRENHEIT), time=EVENT_TIME
        )
    assert posted(fake_server) == []


async def test_numbers_are_rounded_to_two_decimals_and_whole_ones_written_as_integers(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    await engine.add_temperature(client, BABY_ID, Measurement(98.456, UNIT_FAHRENHEIT))
    await engine.add_temperature(client, BABY_ID, Measurement(37.0, UNIT_CELSIUS))

    first, second = posted(fake_server)
    assert first["temperature"]["value"] == 98.46
    assert second["temperature"]["value"] == 37
    assert isinstance(second["temperature"]["value"], int)
