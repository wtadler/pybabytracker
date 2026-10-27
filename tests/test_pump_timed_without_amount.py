"""A pump with a length but no volume: what a pump timer's stop writes when no amount is given.

The app writes most pumps with only a total ``amount`` and no per-side keys, and a timed
both-sides pump with equal ``leftDuration`` and ``rightDuration``. A session whose volume isn't
known yet carries the durations and an ``amount`` of 0, as every timed pump the app saved
without a volume does (never a missing key).
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from fake_server import FakeSyncServer
from test_write_types import posted, ready

from pybabytracker import SIDE_LEFT, SIDE_RIGHT, BabyTrackerClient, Pump, Volume, decode_payload


async def test_a_pump_with_only_durations_has_a_zero_amount_and_no_side_amounts(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)

    await engine.add_pump(client, left_minutes=12, right_minutes=12)

    payload = posted(fake_server)
    assert payload["amount"] == {
        "BCObjectType": "VolumeMeasure",
        "englishMeasure": "true",
        "value": 0,
    }
    assert "leftAmount" not in payload
    assert "rightAmount" not in payload
    assert "baby" not in payload
    assert (payload["leftDuration"], payload["rightDuration"]) == (12, 12)
    assert payload["sides"] == 0
    assert payload["BCObjectType"] == "Pump"


async def test_a_pump_with_only_durations_decodes_with_no_volume(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)

    record = await engine.add_pump(client, left_minutes=7, right_minutes=7)

    assert isinstance(record, Pump)
    assert record.volume == Volume(0, "oz")
    assert record.left_volume is None
    assert record.right_volume is None
    assert (record.left_duration, record.right_duration) == (7, 7)
    assert decode_payload(record.to_payload()).to_payload() == record.to_payload()


async def test_a_duration_only_pump_is_the_accounts_latest_pump(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)

    record = await engine.add_pump(client, left_minutes=7, right_minutes=7)

    latest = engine.latest_pump()
    assert latest is not None
    assert latest.to_payload() == record.to_payload()


@pytest.mark.parametrize("finish_side", [None, SIDE_LEFT, SIDE_RIGHT])
async def test_a_duration_only_pump_takes_the_finish_side_like_any_pump(
    finish_side: int | None, client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)

    await engine.add_pump(client, left_minutes=3, right_minutes=3, finish_side=finish_side)

    # Right when both sides were used and none is named, as in every pump.
    assert posted(fake_server)["finishSide"] == (SIDE_RIGHT if finish_side is None else finish_side)


async def test_a_one_sided_duration_only_pump_finishes_on_that_side(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)

    await engine.add_pump(client, left_minutes=9)

    payload = posted(fake_server)
    assert payload["finishSide"] == SIDE_LEFT
    assert (payload["leftDuration"], payload["rightDuration"]) == (9, 0)


async def test_a_pump_with_no_volume_and_no_minutes_is_still_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)
    fake_server.requests.clear()

    with pytest.raises(ValueError, match="volume"):
        await engine.add_pump(client)

    assert fake_server.requests == []


async def test_a_pump_with_zero_minutes_and_no_volume_is_refused(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)
    fake_server.requests.clear()

    with pytest.raises(ValueError, match="volume"):
        await engine.add_pump(client, left_minutes=0, right_minutes=0)

    assert fake_server.requests == []


@pytest.mark.parametrize("minutes", [-1, 241])
async def test_bad_minutes_are_still_refused_on_a_pump_with_no_volume(
    minutes: int, client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)
    fake_server.requests.clear()

    with pytest.raises(ValueError):
        await engine.add_pump(client, left_minutes=minutes, right_minutes=minutes)

    assert fake_server.requests == []


@pytest.mark.parametrize("value", [0, -2, math.nan])
async def test_a_given_total_must_still_be_more_than_zero(
    value: float, client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)
    fake_server.requests.clear()

    with pytest.raises(ValueError):
        await engine.add_pump(client, amount=Volume(value, "oz"), left_minutes=5, right_minutes=5)

    assert fake_server.requests == []


@pytest.mark.parametrize("unit", ["oz", "ml"])
async def test_a_total_with_equal_durations_keeps_the_total_only_shape(
    unit: str, client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)

    await engine.add_pump(client, amount=Volume(4, unit), left_minutes=15, right_minutes=15)

    payload: dict[str, Any] = posted(fake_server)
    assert payload["amount"]["value"] == 4
    assert payload["amount"]["englishMeasure"] == ("true" if unit == "oz" else "false")
    assert "leftAmount" not in payload
    assert "rightAmount" not in payload
    assert "baby" not in payload
    assert (payload["leftDuration"], payload["rightDuration"]) == (15, 15)
    assert payload["sides"] == 0


async def test_a_duration_only_pump_with_sides_volumes_is_unchanged(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)

    await engine.add_pump(client, left_volume=Volume(2, "oz"), right_volume=Volume(3, "oz"))

    payload = posted(fake_server)
    assert payload["amount"]["value"] == 5
    assert payload["leftAmount"]["value"] == 2
    assert payload["rightAmount"]["value"] == 3


async def test_a_zero_amount_pump_uses_the_given_default_unit(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)

    record = await engine.add_pump(client, left_minutes=6, right_minutes=6, default_unit="ml")

    payload = posted(fake_server)
    assert payload["amount"]["englishMeasure"] == "false"
    assert payload["amount"]["value"] == 0
    assert record.volume == Volume(0, "ml")
    assert "leftAmount" not in payload


async def test_a_default_unit_changes_nothing_when_a_volume_is_given(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)

    await engine.add_pump(client, amount=Volume(4, "oz"), default_unit="ml")

    assert posted(fake_server)["amount"]["englishMeasure"] == "true"


async def test_an_unknown_default_unit_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    engine = await ready(client, fake_server)
    fake_server.requests.clear()

    with pytest.raises(ValueError, match="unit"):
        await engine.add_pump(client, left_minutes=5, default_unit="cups")

    assert fake_server.requests == []
