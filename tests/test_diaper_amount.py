"""A diaper's amount (invented data only)."""

from __future__ import annotations

from typing import Any

import pytest
from factories import BABY_ID, payload_for
from fake_server import FakeSyncServer
from selection_helpers import posted, rid, seed, synced

from pybabytracker import (
    DIAPER_AMOUNT_HEAVY,
    DIAPER_AMOUNT_NONE,
    DIAPER_AMOUNT_NORMAL,
    DIAPER_AMOUNT_NOT_SET,
    DIAPER_AMOUNTS,
    DIAPER_DRY,
    DIAPER_MIXED,
    DIAPER_STATUSES,
    DIAPER_WET,
    BabyTrackerClient,
    Diaper,
    decode_payload,
)

DIAPER = rid(41)


def iphone_diaper(**overrides: Any) -> dict[str, Any]:
    """A diaper with the fields an iPhone syncs: the values on 13 of the 14 seen 2026-11-12."""
    payload = payload_for("Diaper", DIAPER)
    payload.update(status=2, amount=2, peeColor=5, pooColor=5, texture=5, flag=0, newFlage="true")
    payload.update(overrides)
    return payload


def test_the_amounts_are_the_android_apps_six() -> None:
    assert DIAPER_AMOUNTS == (0, 1, 2, 3, 4, 5)
    # The two seen in synced data.
    assert (DIAPER_AMOUNT_NORMAL, DIAPER_AMOUNT_NOT_SET) == (2, 5)


async def test_a_plain_diaper_still_has_a_normal_amount(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    await engine.add_diaper(client, BABY_ID, DIAPER_WET)

    [payload] = posted(fake_server)
    assert (payload["amount"], payload["peeColor"], payload["pooColor"]) == (2, 5, 5)
    assert (payload["texture"], payload["flag"]) == (5, 0)


@pytest.mark.parametrize("amount", DIAPER_AMOUNTS)
async def test_a_diaper_is_logged_with_the_amount_given_and_nothing_else_changed(
    client: BabyTrackerClient, fake_server: FakeSyncServer, amount: int
) -> None:
    seed(fake_server)
    engine = await synced(client)

    plain = await engine.add_diaper(client, BABY_ID, DIAPER_MIXED)
    record = await engine.add_diaper(client, BABY_ID, DIAPER_MIXED, amount=amount)

    assert isinstance(record, Diaper)
    assert record.amount == amount
    first, second = posted(fake_server)
    assert set(first) == set(second)
    assert second["amount"] == amount
    differing = {k for k in first if first[k] != second[k]} - {"objectID", "time", "timestamp"}
    assert differing <= {"amount"}
    assert plain.amount == DIAPER_AMOUNT_NORMAL


async def test_a_saved_diapers_amount_changes_and_nothing_else(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = iphone_diaper()
    seed(fake_server, original)
    engine = await synced(client)

    updated = await engine.edit_diaper(client, DIAPER, amount=DIAPER_AMOUNT_HEAVY)

    [payload] = posted(fake_server)
    assert {k for k in payload if payload[k] != original[k]} == {
        "amount",
        "newFlage",
        "timestamp",
    }
    assert payload["amount"] == DIAPER_AMOUNT_HEAVY
    assert updated.amount == DIAPER_AMOUNT_HEAVY


@pytest.mark.parametrize("amount", [6, -1, True, "3", 2.0])
async def test_a_bad_amount_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, amount: Any
) -> None:
    seed(fake_server, iphone_diaper())
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(ValueError):
        await engine.add_diaper(client, BABY_ID, DIAPER_WET, amount=amount)
    with pytest.raises(ValueError):
        await engine.edit_diaper(client, DIAPER, amount=amount)
    assert len(fake_server.requests) == before


async def test_a_dry_diaper_is_logged_with_an_amount_of_none_as_the_android_app_saves_one(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    record = await engine.add_diaper(client, BABY_ID, DIAPER_DRY)

    assert (record.status, record.amount) == (DIAPER_DRY, DIAPER_AMOUNT_NONE)
    [payload] = posted(fake_server)
    assert (payload["status"], payload["amount"]) == (3, 0)
    assert (payload["peeColor"], payload["pooColor"], payload["texture"], payload["flag"]) == (
        5,
        5,
        5,
        0,
    )


async def test_a_dry_diaper_with_an_amount_given_keeps_it(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    seed(fake_server)
    engine = await synced(client)

    record = await engine.add_diaper(client, BABY_ID, DIAPER_DRY, amount=DIAPER_AMOUNT_NOT_SET)

    assert record.amount == DIAPER_AMOUNT_NOT_SET


async def test_changing_a_diapers_kind_moves_its_amount_as_the_android_screen_does(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    original = iphone_diaper(status=2, amount=3)
    seed(fake_server, original)
    engine = await synced(client)

    dry = await engine.edit_diaper(client, DIAPER, status=DIAPER_DRY)
    assert (dry.status, dry.amount) == (DIAPER_DRY, DIAPER_AMOUNT_NONE)
    wet = await engine.edit_diaper(client, DIAPER, status=DIAPER_WET)
    assert (wet.status, wet.amount) == (DIAPER_WET, DIAPER_AMOUNT_NORMAL)
    heavy = await engine.edit_diaper(client, DIAPER, amount=DIAPER_AMOUNT_HEAVY)
    mixed = await engine.edit_diaper(client, DIAPER, status=DIAPER_MIXED)
    assert heavy.amount == DIAPER_AMOUNT_HEAVY
    assert (mixed.status, mixed.amount) == (DIAPER_MIXED, DIAPER_AMOUNT_HEAVY)  # left alone
    kept = await engine.edit_diaper(client, DIAPER, status=DIAPER_DRY, amount=DIAPER_AMOUNT_HEAVY)
    assert (kept.status, kept.amount) == (DIAPER_DRY, DIAPER_AMOUNT_HEAVY)


def test_a_dry_diaper_from_another_device_is_read() -> None:
    dry = decode_payload(iphone_diaper(status=DIAPER_DRY, amount=0))
    assert isinstance(dry, Diaper)
    assert (dry.status, dry.amount) == (DIAPER_DRY, DIAPER_AMOUNT_NONE)
    assert DIAPER_STATUSES == (0, 1, 2, 3)
