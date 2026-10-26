# pybabytracker

Async Python client for the Nighp Baby Tracker sync service, built for a Home Assistant
integration. It is unofficial and identifies itself honestly (`pybabytracker/<version>`).

## Use

```python
import aiohttp
from pybabytracker import BabyTrackerClient, SyncEngine

async with aiohttp.ClientSession() as session:
    client = BabyTrackerClient(
        "parent@example.com", "password", "STABLE-DEVICE-UUID", "Home Assistant", session=session
    )
    engine = SyncEngine()  # or SyncEngine.from_dict(saved_state)
    await engine.sync(client)  # fetches only devices that are ahead of what was seen
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
```

The tool signs in as a separate device named "Home Assistant (dev)". Its ID is made once and
kept in `.pybabytracker-device.json` (gitignored). `--probe-history` reads every device's log
from 0 and reports how many transactions come back versus its `LastSyncID`, the lowest and
highest numbers returned, and a verdict (full, paged, paged newest first, starts at K with older
numbers trimmed, duplicates, or gaps), then whether the probe device now appears in the device list. It only reads.

## Develop

```sh
uv venv --python 3.12 && uv pip install -e ".[dev]"
pytest && mypy && ruff check .
```
