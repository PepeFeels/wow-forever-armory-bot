# WoW Forever Armory Bot

Python foundation for a standalone, multi-guild Discord bot that reads **public** WoW Forever Armory character pages. This repository currently contains only the Armory data layer. It does not run a bot or store guild configuration.

## Architecture

- `parser.py` locates the `character-profile-mount-initial-state` script with Python's HTML parser. The script begins with `var characterProfileInitialState =`, followed by a JSON object and semicolon. The parser decodes only that JSON and rejects trailing executable content. It does not execute JavaScript.
- `models.py` holds optional normalized character, specialization, and profession values. Every profession tier is retained.
- `client.py` fetches a caller supplied relative profile path over HTTPS with an explicit timeout, bounded retries for transient HTTP errors, `Retry-After` handling, and structured log fields. It never uses credentials.

The decoded object has `character`, `summary`, and `supportedArmoryTabs` keys. The character's `class.name` and `race.name` supply display names. The active entry in `character.specs` supplies specialization ID, name, and spent points. `character.professions.primaries` and `.secondaries` contain professions with `tiers`; each tier supplies `skillPoints` and `maxSkillPoints`. `character.lastUpdatedTimestamp` supplies `iso8601` and millisecond `epoch` values. The parser reads a valid ISO timestamp as aware UTC, falls back to the epoch in milliseconds when needed, and leaves invalid or absent values as `None`.

For a profession with one tier, `skill` and `max_skill` are convenient aliases for that tier. For multiple tiers, those aliases are `None`; read each entry in `tiers` to avoid losing or combining distinct ranks.

## Local setup

Use Python 3.14 or newer. Runtime use and tests require only the standard library. From this repository:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

An editable install makes the package importable by pytest and other tools without setting `PYTHONPATH`.

## Example

```python
import asyncio
from wow_forever_armory import ArmoryClient

async def main() -> None:
    client = ArmoryClient(timeout=10, max_retries=2)
    # Supply a public character path relative to the configured base URL.
    character = await client.fetch_character(
        "en-us/forever/eu/armory/character/pepe-feels"
    )
    print(character.name, character.active_specialization)

asyncio.run(main())
```

The client does not discover characters or infer a profile URL from a name. The example path is a public test character; its values will change over time.

To perform an **explicit**, one-request live smoke test, run:

```bash
.venv/bin/python -m wow_forever_armory.smoke \
  en-us/forever/eu/armory/character/pepe-feels
```

The smoke command is never invoked by the offline tests or CI. It prints a concise normalized summary and does not save the HTML page. It disables retries, so an HTTP failure or rate limit stops the command after one request.

## Tests and checks

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src tests
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src/wow_forever_armory
```

The Ruff, mypy, and pytest commands require those development tools in the local virtual environment. All tests use synthetic HTML and mocked HTTP responses; they make no network requests.

## Current limitations

- The field paths are based on the verified public character snapshot supplied for this task. Offline fixtures are minimal and do not cover every Armory field or future schema changes.
- `fetch_character` accepts a known relative URL path; search and URL discovery are not implemented.
- The standard library HTTP request runs in a worker thread. Cancellation of the coroutine cannot stop an in-flight socket call immediately, although the socket has a timeout.
- There is no persistent cache or cross-process rate limiter. Callers should keep polling infrequent and honor Armory restrictions.
- No Discord commands, guild configuration, or persistence are included yet.

## Next step

Add Discord commands and per-guild character configuration on top of this data layer, with a small opt-in live check after Armory schema changes.
