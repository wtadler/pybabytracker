"""A diaper's amount (invented data only)."""

from __future__ import annotations

from typing import Any

import pytest
from factories import BABY_ID, payload_for
from fake_server import FakeSyncServer
from selection_helpers import posted, rid, seed, synced

from pybabytracker import (
    DIAPER_AMOUNT_HEAVY,
    DIAPER_AMOUNT_NORMAL,
    DIAPER_AMOUNT_NOT_SET,
    DIAPER_AMOUNTS,
    DIAPER_DRY,
    DIAPER_MIXED,
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


@pytest.mark.parametrize("amount", [6, -1, True, "3", 2.0, None])
async def test_a_bad_amount_is_refused_with_no_request(
    client: BabyTrackerClient, fake_server: FakeSyncServer, amount: Any
) -> None:
    seed(fake_server, iphone_diaper())
    engine = await synced(client)
    before = len(fake_server.requests)

    with pytest.raises(ValueError):
        await engine.add_diaper(client, BABY_ID, DIAPER_WET, amount=amount)
    if amount is not None:  # None means "leave it" for an edit
        with pytest.raises(ValueError):
            await engine.edit_diaper(client, DIAPER, amount=amount)
    assert len(fake_server.requests) == before


async def test_a_dry_diaper_is_read_but_not_written(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    dry = decode_payload(iphone_diaper(status=DIAPER_DRY))
    assert isinstance(dry, Diaper)
    assert dry.status == DIAPER_DRY

    seed(fake_server, iphone_diaper())
    engine = await synced(client)
    before = len(fake_server.requests)
    with pytest.raises(ValueError):
        await engine.add_diaper(client, BABY_ID, DIAPER_DRY)
    with pytest.raises(ValueError):
        await engine.edit_diaper(client, DIAPER, status=DIAPER_DRY)
    assert len(fake_server.requests) == before
