# Contributing

## What belongs here

The library aims to read and write every record type the Baby Tracker app syncs, whether or not
[ha-baby-tracker](https://github.com/wtadler/ha-baby-tracker) uses it yet. New record types and
new writes are welcome even when nothing calls them. The integration is deliberately narrower, so
"the library supports it" doesn't mean the integration will.

A new type or write belongs here if:

- **It's checked against real data.** Base it on payloads the app sent, or on the account's seed,
  and pin their keys and value shapes in tests. Say in the pull request where the payloads came
  from, and whether the change has run against the live service.
- **Bad input is refused before any request.** A write that the service or the app would reject,
  or would store wrongly, raises before anything is posted.
- **It follows the existing patterns:** `add_*` methods on `SyncEngine`, pick-list choices looked
  up by ID (as for activities and vaccines), and the sync-group checks on every write path.

The decision, and the discussion behind it, is in
[wtadler/ha-baby-tracker#161](https://github.com/wtadler/ha-baby-tracker/issues/161).

## Development

```sh
uv venv --python 3.12 && uv pip install -e ".[dev]"
pytest && mypy && ruff check . && ruff format --check .
```

CI runs the same checks on Python 3.12, 3.13, and 3.14.

## Versions

- **Patch** (0.5.1): bug fixes, including a new exception that subclasses an existing one.
- **Minor** (0.6.0): new record types, new writes, and anything that changes behavior callers
  rely on.
- **1.0** comes once Home Assistant core depends on the library, when its public API should stay
  stable.

Releases batch several changes rather than following each pull request. The steps are under
"Release" in the [README](README.md#release).
