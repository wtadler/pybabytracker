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

### Record types you can add

Every add shares the rules above (numbering, pending posts, `object_id` retries, local apply),
and bad input raises `ValueError` before any request. Payloads match the iPad capture key for
key.

| Method | Writes | Notes |
|---|---|---|
| `add_diaper(client, baby_id, status, ...)` | `Diaper` | `DIAPER_WET`, `DIAPER_DIRTY`, `DIAPER_MIXED` |
| `add_formula(client, baby_id, volume=, ...)` | `Formula` | volume more than zero |
| `add_pumped(client, baby_id, volume=, ...)` | `Pumped` | a bottle of pumped milk |
| `add_nursing(client, baby_id, left_minutes=, right_minutes=, ...)` | `Nursing` | a finished session |
| `add_sleep(client, baby_id, start=, end=, ...)` | `Sleep` | a finished sleep |
| `add_pump(client, amount=, ...)` or `add_pump(client, left_volume=, right_volume=, ...)` | `Pump` | a pumping session; belongs to the account, so no `baby_id` |

- Volumes are `Volume(value, "oz")` or `Volume(value, "ml")`, written as a `VolumeMeasure` with
  `englishMeasure` `"true"` (ounces) or `"false"` (milliliters), rounded to 2 decimals (an integer
  when whole). The number is written as given, with no conversion, as the app does.
- `time` is when the activity started (or, for a bottle or diaper, when it happened), written in
  UTC. `timezone_offset` (seconds east of UTC) defaults to the offset of the time given. The app itself writes the zone's *standard* offset
  all year, ignoring daylight saving, so to match it pass
  `timezone_offset=standard_offset_seconds(tz, time)`: New York is -18000 in July and January.
- Nursing: `time` is the session's start, as in the capture. Give `start_time`, or `end_time`
  (start is end minus the total minutes), or neither (the session just ended). `finish_side` is
  `SIDE_LEFT` or `SIDE_RIGHT`, defaulting to the last side used (right when both were). For a pump that default is a guess
  (the one captured pump with both sides finished left), so pass `finish_side` yourself.
- Sleep: `time` is `start`. `duration` is whole minutes: the length rounded half up (the capture
  stored about 30 seconds as 1), and at least 1, because a duration of 0 means a sleep in
  progress. `end` must be after `start`, and a sleep can't be longer than 24 hours.
- Nursing and pump minutes are whole numbers, at most 240 per side (a guard against seconds
  passed as minutes).
- Pump: a pump belongs to the account, not a baby, so it takes no `baby_id` and its payload has
  no `baby` key, like the app's. Give the total as `amount`, or the side volumes (not both):
  with only `amount` the payload has no per-side amounts, and with sides `amount` is their sum,
  a side left out is written as 0 in the other's unit, and both sides must use one unit.
  With no volume at all, minutes are required and the payload carries the durations and an
  `amount` of 0 in `default_unit` (default `oz`), as the app writes a timed pump with no volume.
  `sides` is written as 0, as captured. Babies are never derived from pumps, and a pump that
  carries a `baby` still decodes (kept raw, not read). `engine.latest_pump()` is the newest pump
  on the account; `latest_records(baby_id)` never includes pumps.

### Sleep timers

The app syncs a running sleep: an add with `duration` 0 at the start, then an update (opcode 1)
to the same record at the stop. Nursing timers stay on the phone until they stop, so a timed
nursing session is written once, finished, with `add_nursing`.

| Method | Writes | Notes |
|---|---|---|
| `start_sleep(client, baby_id, start=None, ...)` | `Sleep` (add) | `duration` 0; `start` defaults to now, no more than a minute ahead or 24 hours back |
| `end_sleep(client, object_id, end=None)` | `Sleep` (update) | ends a running sleep, whichever device started it |

- `end_sleep` sends the record as last saved, changing only `duration` (rounded like
  `add_sleep`), `newFlage` (`"false"`), and `timestamp` (the write time, or one second past the
  stored one when that's ahead of this clock, so the end wins the merge). Key order and unknown
  keys are kept.
- A sleep longer than 24 hours at the stop is **clamped to 1,440 minutes** with a warning, not
  refused, so a forgotten timer can always be stopped. An `end` more than a minute in the future
  is clamped to now, and an end at or up to a minute before the start (a clock running a little
  fast) is written as 1 minute.
- Refused before any request: a record never seen or deleted (`RecordNotFoundError`), one that
  isn't a sleep (`WrongRecordTypeError`), or a sleep another device finished
  (`TimerNotRunningError`). A sleep this device already ended (its end is in this device's log,
  even if a phone edited it since) is returned as is, with nothing posted, so retries are safe.
- The end carries the whole record as this device last saw it, so a phone edit that hasn't synced
  yet (a note, say) is overwritten by it, as the app's own edits overwrite each other.
- A phone delete racing an end: the app's delete keeps the record's old timestamp, so an end
  posted after it (with a newer timestamp) outlives it, and the sleep stays here as ended. How the
  app shows that case is still to be checked on a test device.
- An end is an update, not an add: it doesn't make another device's sleep this device's own, so
  `delete_record` still refuses it. Undo of a sleep this device started works running or ended.

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
