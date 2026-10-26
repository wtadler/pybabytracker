"""Paging through long device logs during one sync."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

import pytest
from factories import payload_for
from fake_server import FakeSyncServer
from test_engine import MemorySource, tx

from pybabytracker import (
    OP_ADD,
    BabyTrackerClient,
    BabyTrackerConnectionError,
    Device,
    RawTransaction,
    SyncEngine,
)

PHONE = "AAAA1111-0000-4000-8000-00000000000A"
TABLET = "BBBB2222-0000-4000-8000-00000000000B"
LAPTOP = "CCCC3333-0000-4000-8000-00000000000C"


def rid(n: int) -> str:
    return f"AAAAAAAA-0000-4000-8000-{n:012d}"


def fill(server: FakeSyncServer, uuid: str, count: int, start: int = 1) -> None:
    """Add ``count`` diaper adds to a device, numbered from ``start``."""
    for n in range(start, start + count):
        server.add_transaction(uuid, OP_ADD, payload_for("Diaper", rid(n + _offset(uuid))))


def _offset(uuid: str) -> int:
    return {PHONE: 0, TABLET: 100_000, LAPTOP: 200_000}.get(uuid, 300_000)


def reads(server: FakeSyncServer, uuid: str) -> list[str]:
    return [r.path for r in server.calls("GET", f"/account/transaction/{uuid}/")]


def memory_logs(counts: dict[str, int]) -> dict[str, list[RawTransaction]]:
    return {
        uuid: [
            tx(n, OP_ADD, payload_for("Diaper", rid(n + _offset(uuid)))) for n in range(1, c + 1)
        ]
        for uuid, c in counts.items()
    }


def _no_hook(uuid: str, since: int) -> None:
    return None


class PagedMemorySource(MemorySource):
    """A memory source that returns at most ``page_size`` entries per read."""

    def __init__(self, logs: dict[str, list[RawTransaction]], page_size: int) -> None:
        super().__init__(logs)
        self.page_size = page_size
        self.after_fetch: Callable[[str, int], None] = _no_hook

    async def get_transactions(self, device_uuid: str, since_sync_id: int) -> list[RawTransaction]:
        page = (await super().get_transactions(device_uuid, since_sync_id))[: self.page_size]
        self.after_fetch(device_uuid, since_sync_id)
        return page


async def test_a_long_log_is_read_in_pages_of_500_in_one_sync(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.add_device(PHONE)
    fill(fake_server, PHONE, 1602)
    fake_server.page_size = 500
    engine = SyncEngine()

    result = await engine.sync(client)

    assert result.transactions_applied == 1602
    assert len(engine.records) == 1602
    assert engine.last_seen[PHONE] == 1602
    assert reads(fake_server, PHONE) == [
        f"/account/transaction/{PHONE}/{since}" for since in (0, 500, 1000, 1500)
    ]


async def test_a_log_of_exactly_one_page_needs_one_read(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.add_device(PHONE)
    fill(fake_server, PHONE, 500)
    fake_server.page_size = 500

    await SyncEngine().sync(client)

    assert len(reads(fake_server, PHONE)) == 1


async def test_small_pages_across_three_devices_reach_every_last_sync_id(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    for uuid, count in ((PHONE, 5), (TABLET, 3), (LAPTOP, 1)):
        fake_server.add_device(uuid)
        fill(fake_server, uuid, count)
    fake_server.page_size = 2
    engine = SyncEngine()

    result = await engine.sync(client)

    assert dict(engine.last_seen) == {PHONE: 5, TABLET: 3, LAPTOP: 1}
    assert result.devices_fetched == 3
    assert result.transactions_applied == 9
    assert len(reads(fake_server, PHONE)) == 3
    assert len(reads(fake_server, TABLET)) == 2
    assert len(reads(fake_server, LAPTOP)) == 1


async def test_a_page_shorter_than_the_page_size_before_last_sync_id_keeps_reading() -> None:
    # Numbers have gaps, so the first page is short but the device is still behind.
    log = [tx(n, OP_ADD, payload_for("Diaper", rid(n))) for n in (1, 2, 5, 6, 9, 10)]
    source = PagedMemorySource({PHONE: log}, page_size=3)
    # Pages are (1, 2, 5), (6, 9, 10): both full. Force a short one with page_size=4.
    source.page_size = 4
    engine = SyncEngine()

    await engine.sync(source)

    assert engine.last_seen[PHONE] == 10
    assert source.fetches == [(PHONE, 0), (PHONE, 6)]


async def test_a_short_page_followed_by_an_empty_read_stops_at_the_gap() -> None:
    class ShortSource(PagedMemorySource):
        async def get_devices(self) -> list[Device]:
            return [Device(PHONE, 20, "p", "o")]  # claims 20, only 6 exist

    log = [tx(n, OP_ADD, payload_for("Diaper", rid(n))) for n in range(1, 7)]
    source = ShortSource({PHONE: log}, page_size=4)
    engine = SyncEngine()

    await engine.sync(source)

    assert engine.last_seen[PHONE] == 6
    assert source.fetches == [(PHONE, 0), (PHONE, 4), (PHONE, 6)]


async def test_last_sync_id_growing_during_the_loop_is_picked_up_by_the_next_sync() -> None:
    logs = memory_logs({PHONE: 6})
    source = PagedMemorySource(logs, page_size=2)

    def grow(uuid: str, since: int) -> None:
        if since == 0:
            logs[PHONE].extend(tx(n, OP_ADD, payload_for("Diaper", rid(n))) for n in (7, 8))

    source.after_fetch = grow
    engine = SyncEngine()

    await engine.sync(source)
    # The listing said 6, so this sync stops there even though the log grew.
    assert engine.last_seen[PHONE] == 6
    assert source.fetches == [(PHONE, 0), (PHONE, 2), (PHONE, 4)]

    source.after_fetch = lambda uuid, since: None
    source.fetches.clear()
    await engine.sync(source)
    assert engine.last_seen[PHONE] == 8
    assert source.fetches == [(PHONE, 6)]


async def test_a_device_returning_entries_above_its_last_sync_id_is_applied_and_stops() -> None:
    class UnderstatedSource(PagedMemorySource):
        async def get_devices(self) -> list[Device]:
            return [Device(PHONE, 2, "p", "o")]

    source = UnderstatedSource(memory_logs({PHONE: 5}), page_size=10)
    engine = SyncEngine()

    result = await engine.sync(source)

    assert result.transactions_applied == 5
    assert engine.last_seen[PHONE] == 5
    assert source.fetches == [(PHONE, 0)]


async def test_a_server_that_keeps_returning_the_same_page_stops_after_one_repeat(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first_page = [tx(n, OP_ADD, payload_for("Diaper", rid(n))) for n in (1, 2)]

    class StuckSource(MemorySource):
        async def get_devices(self) -> list[Device]:
            return [Device(PHONE, 10, "p", "o")]

        async def get_transactions(
            self, device_uuid: str, since_sync_id: int
        ) -> list[RawTransaction]:
            self.fetches.append((device_uuid, since_sync_id))
            return first_page

    source = StuckSource({})
    engine = SyncEngine()

    with caplog.at_level(logging.WARNING, logger="pybabytracker.engine"):
        result = await engine.sync(source)

    assert source.fetches == [(PHONE, 0), (PHONE, 2)]
    assert result.transactions_applied == 2
    assert engine.last_seen[PHONE] == 2
    assert any(PHONE in r.message and r.levelno == logging.WARNING for r in caplog.records)


async def test_a_stuck_device_does_not_stop_the_devices_after_it() -> None:
    stuck_page = [tx(1, OP_ADD, payload_for("Diaper", rid(1)))]
    good = memory_logs({TABLET: 3})[TABLET]

    class MixedSource(MemorySource):
        async def get_devices(self) -> list[Device]:
            return [Device(PHONE, 9, "p", "o"), Device(TABLET, 3, "t", "o")]

        async def get_transactions(
            self, device_uuid: str, since_sync_id: int
        ) -> list[RawTransaction]:
            self.fetches.append((device_uuid, since_sync_id))
            return stuck_page if device_uuid == PHONE else good

    engine = SyncEngine()
    await engine.sync(MixedSource({}))

    assert engine.last_seen == {PHONE: 1, TABLET: 3}


async def test_an_empty_read_before_last_sync_id_ends_the_device_without_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class EmptySource(MemorySource):
        async def get_devices(self) -> list[Device]:
            return [Device(PHONE, 5, "p", "o")]

    source = EmptySource({PHONE: []})
    with caplog.at_level(logging.WARNING, logger="pybabytracker.engine"):
        await SyncEngine().sync(source)
    assert source.fetches == [(PHONE, 0)]
    assert not caplog.records


async def test_a_server_error_partway_through_the_pages_keeps_progress_and_resumes(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.add_device(PHONE)
    fill(fake_server, PHONE, 7)
    fake_server.page_size = 3
    fake_server.fail_paths[f"/{PHONE}/6"] = 500
    engine = SyncEngine()

    with pytest.raises(BabyTrackerConnectionError):
        await engine.sync(client)
    assert engine.last_seen[PHONE] == 6
    assert len(engine.records) == 6

    fake_server.fail_paths.clear()
    fake_server.requests.clear()
    result = await engine.sync(client)

    assert engine.last_seen[PHONE] == 7
    assert result.transactions_applied == 1
    assert reads(fake_server, PHONE) == [f"/account/transaction/{PHONE}/6"]


async def test_saved_state_at_1500_fetches_only_from_1500_onward(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.add_device(PHONE)
    fill(fake_server, PHONE, 1500)
    fake_server.page_size = 500
    engine = SyncEngine()
    engine.apply(PHONE, memory_logs({PHONE: 1500})[PHONE])
    saved = json.loads(json.dumps(engine.to_dict()))
    assert saved["last_seen"][PHONE] == 1500

    fill(fake_server, PHONE, 102, start=1501)
    fake_server.requests.clear()
    restored = SyncEngine.from_dict(saved)
    result = await restored.sync(client)

    assert result.transactions_applied == 102
    assert reads(fake_server, PHONE) == [f"/account/transaction/{PHONE}/1500"]


async def test_a_sync_with_every_device_caught_up_makes_no_reads(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    for uuid in (PHONE, TABLET):
        fake_server.add_device(uuid)
        fill(fake_server, uuid, 5)
    fake_server.page_size = 2
    engine = SyncEngine()
    for uuid, log in memory_logs({PHONE: 5, TABLET: 5}).items():
        engine.apply(uuid, log)

    result = await engine.sync(client)

    assert result.devices_fetched == 0
    assert not fake_server.calls("GET", "/account/transaction/")


async def test_a_401_between_pages_signs_in_again_and_paging_continues(
    client: BabyTrackerClient, fake_server: FakeSyncServer
) -> None:
    fake_server.add_device(PHONE)
    fill(fake_server, PHONE, 6)
    fake_server.page_size = 2
    await client.sign_in()
    sign_ins_before = len(fake_server.calls("POST", "/session"))

    class ExpiringSource:
        def __init__(self) -> None:
            self.reads = 0

        async def get_devices(self) -> list[Device]:
            return await client.get_devices()

        async def get_transactions(
            self, device_uuid: str, since_sync_id: int
        ) -> list[RawTransaction]:
            self.reads += 1
            if self.reads == 2:
                fake_server.expire_sessions()
            return await client.get_transactions(device_uuid, since_sync_id)

    engine = SyncEngine()
    result = await engine.sync(ExpiringSource())

    assert result.transactions_applied == 6
    assert engine.last_seen[PHONE] == 6
    assert len(fake_server.calls("POST", "/session")) == sign_ins_before + 1
