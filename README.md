# pybabytracker

Async Python client for the Nighp Baby Tracker sync service, built for a Home Assistant
integration. It is unofficial and identifies itself honestly (`pybabytracker/<version>`).

## Use

```python
import aiohttp
from pybabytracker import DIAPER_WET, BabyTrackerClient, SyncEngine

async with aiohttp.ClientSession() as session:
    client = BabyTrackerClient(
        "parent@example.com", "password", "STABLE-DEVICE-UUID", "Home Assistant", session=session
    )
    engine = SyncEngine(device_uuid="STABLE-DEVICE-UUID")  # or SyncEngine.from_dict(saved_state)
    await engine.sync(client)  # fetches only devices that are ahead of what was seen
    diaper = await engine.add_diaper(client, baby_id, DIAPER_WET)  # posts as this device
    await engine.delete_record(client, diaper.object_id)  # only records this device created
    saved_state = engine.to_dict()  # JSON-safe, for the caller to persist
```

- Pass your own `aiohttp.ClientSession` (the client never closes it), and make it dedicated to
  this client: the service authenticates by cookie, so two accounts must not share a session (in
  Home Assistant, use `async_create_clientsession`). Without one, the client makes its own and
  closes it in `close()`.
- `AuthenticationError` means the credentials were rejected. `BabyTrackerConnectionError` means
  the service was unreachable or answered with a server error. A 401 mid-session triggers one
  sign-in and a retry before it becomes an `AuthenticationError`.
- Records are typed (`Diaper`, `Sleep`, `Nursing`, and so on). A record type the library
  doesn't know stays raw as an `UnknownRecord` with a logged warning. Decoding then encoding any
  payload returns the same JSON, including keys the library doesn't model.

## Writes

The engine posts as its own device (`device_uuid`, which must match the client's):

- A write needs a completed sync first (`NotSyncedError` otherwise), because the payload embeds
  the baby record as known from sync (`UnknownBabyError` for a baby it hasn't seen).
- Numbers are at-most-once. The next number is one more than the highest of this device's
  `LastSyncID` on the server, its own log already applied, and its last confirmed post. Before
  choosing, the engine applies any of its own log it hasn't seen (another process using the same
  device ID).
- When a post fails, the engine reads its own log at that number. If the same transaction is
  there, the write succeeded. Otherwise the error is raised and the post stays pending; the next
  write settles it first. After a definite HTTP error, a number nothing holds is reused. After
  no answer at all (`NoResponseError`: a timeout or dropped connection), the post may still
  land, so the number is reused only once `unknown_outcome_grace` seconds (default 90) have
  passed with nothing under it; until then a write raises `WriteOutcomeUnknownError` without
  posting. A 401 signs in again and resends the same number. Nothing is resent blindly.
- To retry an add safely, pass the same `object_id` again: if the earlier attempt landed after
  all, its record is returned instead of a second copy being posted.
- If this device's own log is shorter than its `LastSyncID`, a write is refused
  (`ProtocolError`) rather than skipping the missing entries.
- A confirmed write is applied locally at once, so the next sync doesn't fetch it back.
- Writes and syncs share a lock, so concurrent writes get consecutive numbers.
- `delete_record` only deletes records this device added (`NotOwnRecordError`), and raises
  `RecordNotFoundError` if one is already deleted. Like the app, the delete sends the record as
  last saved with only `newFlage` set to `"false"`; its timestamp is not bumped.
- Save `to_dict()` after every write, including one that raised: a pending post lives only in
  that state. Use one device ID per running process; two processes writing as one device can
  collide on a number while `LastSyncID` lags.

The service lists a device only after its first post; on the test account, the dev device
appeared with `LastSyncID` 1 right after its first add, with no confirmation step.

## Merge rules

Records are keyed by `objectID`. Each stored record carries a version made of the record's
`timestamp`, the device, and the sync number. Within one device's log, later numbers always win.
Across devices the result doesn't depend on which device is fetched first:

- An add or update replaces a stored record if its (timestamp, device, number) is newer.
- A delete removes the record unless the record's timestamp is newer than the delete's, and it
  leaves a tombstone, so a delete that arrives before the add it targets still wins.
- An add or update from another device brings a deleted record back only with a strictly newer
  timestamp than the tombstone. Within one device, a later add always wins.

Tombstones are saved by `to_dict()`. State saved by an older version (1) loads with none.
A payload that can't be decoded at all is skipped with a warning, and the sync moves past it. A
fresh `SyncEngine` replays every log, so it recovers skipped payloads once a fix ships.

## Command line

```sh
export BT_EMAIL=... BT_PASSWORD=...   # or put them in .env
python -m pybabytracker                # latest record of each type, per baby
python -m pybabytracker --probe-history

# Writes go to a throwaway test account only (BT_TEST_EMAIL, BT_TEST_PASSWORD):
python -m pybabytracker --write-test-diaper [--status wet|dirty|mixed]
python -m pybabytracker --delete-test-diaper OBJECT_ID
```

The tool signs in as a separate device named "Home Assistant (dev)". Its ID is made once and
kept in `.pybabytracker-device.json` (gitignored). `--probe-history` reads every device's log
from 0 and reports how many transactions come back versus its `LastSyncID`, the lowest and
highest numbers returned, and a verdict (full, paged, paged newest first, starts at K with older
numbers trimmed, duplicates, or gaps), then whether the probe device now appears in the device list. It only reads.

`--write-test-diaper` adds one diaper for the test account's first baby and reports whether the
dev device is in the device list before and after, with its `LastSyncID`, and whether the next
sync fetched its own log back. `--delete-test-diaper` deletes one the dev device wrote. Both
read only the `BT_TEST_*` credentials, and keep their device ID and engine state in
`.pybabytracker-test-device.json` and `.pybabytracker-test-state.json` (both gitignored).

## Develop

```sh
uv venv --python 3.12 && uv pip install -e ".[dev]"
pytest && mypy && ruff check .
```
