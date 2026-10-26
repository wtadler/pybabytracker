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

- Pass your own `aiohttp.ClientSession` (the client never closes it). Without one, the client
  makes its own and closes it in `close()`.
- `AuthenticationError` means the credentials were rejected. `BabyTrackerConnectionError` means
  the service was unreachable or answered with a server error. A 401 mid-session triggers one
  sign-in and a retry before it becomes an `AuthenticationError`.
- Records are typed (`Diaper`, `Sleep`, `Nursing`, and so on). A record type the library
  doesn't know stays raw as an `UnknownRecord` with a logged warning. Decoding then encoding any
  payload returns the same JSON, including keys the library doesn't model.

## Merge rules

Records are keyed by `objectID`. Adds and updates replace, deletes remove (a delete for an
unseen record is a no-op). Each device's log applies in number order, and the record's own
`timestamp` is ignored, so an older-stamped update still applies. Devices apply in device-ID
order within one sync, so two devices editing one record always resolve the same way.

## Command line

```sh
export BT_EMAIL=... BT_PASSWORD=...   # or put them in .env
python -m pybabytracker                # latest record of each type, per baby
python -m pybabytracker --probe-history
```

The tool signs in as a separate device named "Home Assistant (dev)". Its ID is made once and
kept in `.pybabytracker-device.json` (gitignored). `--probe-history` reads every device's log
from 0 and reports how many transactions come back versus its `LastSyncID`, and whether the
probe device now appears in the device list. It only reads.

## Develop

```sh
uv venv --python 3.12 && uv pip install -e ".[dev]"
pytest && mypy && ruff check .
```
