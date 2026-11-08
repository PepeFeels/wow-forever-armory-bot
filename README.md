# WoW Forever Armory Bot

v1.1.2 release candidate for the WoW Forever Armory Bot. It displays public WoW Forever Armory profiles and provides an admin-activated, persistent Guild Armory over the Armory Core, with per-guild registration and observable, idempotent recovery. This hotfix gives the Discord bot logger a stable name so INFO lifecycle events also work when launched with `python -m`. Production remains on v1.1.0; publication and production upgrade require separate approval and host verification.

## Architecture

- `client.py`, `parser.py`, and `models.py` remain the Armory Core. The Core fetches only validated relative paths over HTTPS, refuses redirects, applies timeouts, and parses public profile data.
- `guild_armory_urls.py` accepts only HTTPS URLs on `worldofwarcraft.blizzard.com` with the supported locale/region/character path. It converts the URL to a Core path and asks the Core to validate it before any request.
- `guild_armory_store.py` persists active guild panels, pending flows, members, row message IDs, character paths, normalized profile snapshots, and timestamps in SQLite. Short local database operations are serialized; write transactions do not span Discord or Armory requests.
- Startup and recovery events distinguish Discord Gateway connection, READY, setup, and recovery outcomes. Recovery counters separate synchronized, created, removed, skipped, and deferred member rows. Failure logs omit exception messages and tracebacks.
- `guild_armory_images.py` renders the English instruction panel and one wide PNG row per member locally with Pillow. The intro background and nine HD class icons are bundled; no remote images or fonts are downloaded.
- `graphic_renderer.py` renders English character overview PNGs for one to three authorized characters. It uses bundled DejaVu fonts and an optional separately provisioned WoW Forever logo, and is called from the existing member-row synchronization path.
- `guild_armory_views.py` contains persistent registration and management buttons and private URL modals. `discord_bot.py` connects commands, reaction events, persistence, Core requests, and row-message recovery.

## Development setup

Use Python 3.14 or newer:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
cp .env.example .env
set -a
. ./.env
set +a
```

Production dependency versions are pinned in `deploy/requirements-runtime.lock`
with SHA-256 hashes for CPython 3.14 on Linux x86_64. The build backend is
separately locked in `deploy/requirements-build.lock`. The production installer
uses `--require-hashes` and rejects unsupported Python/platform combinations.
Regenerate and review both locks when updating dependencies; do not hand-edit
versions without their artifact hashes.

Source the local `.env` into the process environment as shown, or export the same variables another way. Set a real development bot token locally. For faster development command propagation, you may optionally set a controlled test guild ID. Keep `.env` private; it is ignored by Git.

```dotenv
DISCORD_BOT_TOKEN=your-private-test-token
ARMORY_ENVIRONMENT=development
# Optional development-only fast command sync; used only in development mode.
DISCORD_TEST_GUILD_ID=123456789012345678
ARMORY_TIMEOUT=10
ARMORY_DATABASE_PATH=data/guild-armory.sqlite3
```

`ARMORY_ENVIRONMENT` defaults to `production`. `DISCORD_TEST_GUILD_ID` is optional and is honored only when `ARMORY_ENVIRONMENT=development`; then commands are copied and synced to that guild for fast iteration. In production, guild-specific sync is disabled even if a legacy `DISCORD_TEST_GUILD_ID` remains in the process environment. Production globally syncs application commands, so the bot works in every guild where it is installed and authorized. Global command changes can take time to propagate through Discord. Adding another guild requires only inviting the existing application with the documented OAuth scopes and permissions, not changing Python code, the systemd unit, or server environment files. `ARMORY_DATABASE_PATH` defaults to `data/guild-armory.sqlite3`, which is ignored by Git.

## Logging and diagnostics

When the bot is started with `.venv/bin/python -m wow_forever_armory.discord_bot`, it configures standard-library logging to `stderr`. The default level is `INFO`; set `ARMORY_LOG_LEVEL` to `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. An invalid level safely falls back to `INFO` with a warning. `ARMORY_LOG_FORMAT=text` is the default readable UTC ISO 8601 format; set it to `json` for JSON records with timestamps, levels, logger names, events, and selected safe numeric metadata. Invalid formats fall back to text.

Example local settings:

```dotenv
ARMORY_LOG_LEVEL=INFO
ARMORY_LOG_FORMAT=text
```

The bot and discord.py logs flow through separate Armory-owned handlers to the terminal's standard error. Root propagation is disabled for those logger namespaces to prevent a second copy through root handlers. A future systemd unit can let journald capture stderr; after such a unit is separately approved and created, logs can be read with `journalctl -u <unit-name> -f`. This project does not create a unit or log file. Application events do not include tokens, authorization values, submitted URLs, character paths, DM contents, or database records. The Armory-owned formatter also masks recognized tokens, authorization headers, URLs, and character paths in exceptions.

If external handlers are attached directly to `wow_forever_armory` or `discord`, they remain unchanged and receive their own copy of each record. An external console handler can therefore print a duplicate line, and its formatter may expose exception text that the Armory-owned handler masks. The Armory formatter does not sanitize records for those external handlers. Review or remove such external handlers separately if duplicate output or their handling of exception details is unsuitable; the bot does not modify them.

## Guild flow

An administrator with **Manage Server** (or Administrator) runs `/armory activate` in the channel that will contain both the instruction panel and all member image rows. Activation is guild-only and idempotent. Repeating it reuses the saved panel; if the panel message was deleted, the bot recreates it in the same channel.

Members react with ⚔️ to receive private instructions. The panel's **Add & Edit** button opens character management, where members can add characters and edit or remove existing ones. The modal accepts only a full HTTPS character URL on `worldofwarcraft.blizzard.com` with a supported locale, region, and Armory character path. The Core blocks redirects and validates the final relative path.

Each member can keep up to three unique characters per guild. Their data appears in one wide image message in the panel's channel, with the registering member mentioned when that message is first created. Automatic image updates do not ping them again. Later additions, replacements, and removals edit that same message. **Add & Edit** and `/armory manage` provide private list, add, replace, and remove controls. Component IDs carry the originating guild, and the bot verifies membership before changing data, so DM actions are not guessed across a member's guilds.

### Add another Discord server

Invite the same Discord application to the server using OAuth2 scopes `bot` and `applications.commands`; grant only the channel and message permissions listed below. No guild ID needs to be added to Python code, the production environment file, or systemd. In production, leave `DISCORD_TEST_GUILD_ID` unset so global slash commands are available in every installed guild after Discord's propagation delay. A server administrator then runs `/armory activate` in the desired channel. Panel IDs, member registrations, authorization checks, and row-message references are keyed by guild ID, so the same Discord user has separate state in each server. The initial production guild ID is used for deployment verification only, not as an allowlist.

All character URLs are submitted in a private modal; the bot does not read ordinary message content and does not enable the privileged Message Content or Members intents. The bot needs standard guild/reaction gateway intents and channel permissions to view the channel/history, send messages, attach files, embed links, add reactions, and edit its own messages. No privileged Discord Developer Portal intent is required.

When the bot reconnects, it restores persistent component handlers and reconciles saved panel/member rows. Character snapshots and IDs are stored locally in SQLite so rows can be recreated without repeating Armory requests. Back up and restrict access to the database; it contains Discord IDs, character paths, and public profile snapshots. It contains no Discord token.

Run only when you are ready to connect to the configured development application and test guild:

```bash
.venv/bin/python -m wow_forever_armory.discord_bot
```

Do not run this command for offline tests. It connects to Discord and syncs either the optional development guild or global commands.

## Security and operational limits

- Keep the bot token in a secret manager or private local environment. Never commit a real token or put it in command arguments.
- Armory submissions are checked against a single official HTTPS hostname and strict path shape. The existing Core URL validator enforces host boundaries; its HTTP client refuses redirects.
- Every user action is scoped to the selected guild and Discord user. SQLite enforces unique character paths, three slots, and guild/user ownership. A small in-process semaphore bounds concurrent Armory requests.
- SQLite and the rendered image files stay local; images are sent as Discord attachments and are not stored as files on disk.
- The request semaphore and member-operation locks are process-local. Bot startup also holds an advisory filesystem lock to reject another local process using the same database path. Multi-host coordination is not implemented.
- If Discord is unavailable after a registration commits, the stored snapshot lets startup recovery repair the row message. If a channel is permanently inaccessible or a database is lost, an administrator must restore permissions or the database backup.

Production installation, SQLite backup/restore, monitoring, upgrade and rollback guidance are in [OPERATIONS.md](OPERATIONS.md). The repository includes reviewed systemd templates and an installer; running it changes host state and requires an approved maintenance procedure. This documentation update does not install or activate anything.

Version history, the v1.1.2 release-candidate notes, and historical v1.1.1, v1.1.0, and v1.0.0 release notes are in [CHANGELOG.md](CHANGELOG.md), [RELEASE_NOTES_v1.1.2.md](RELEASE_NOTES_v1.1.2.md), [RELEASE_NOTES_v1.1.1.md](RELEASE_NOTES_v1.1.1.md), [RELEASE_NOTES_v1.1.0.md](RELEASE_NOTES_v1.1.0.md), [RELEASE_NOTES_v1.0.0.md](RELEASE_NOTES_v1.0.0.md), and [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

The character overview renderer's supported fields, unavailable-data placeholders, bundled asset licenses, preview generation, and development review process are documented in [GRAPHIC_RENDERER.md](GRAPHIC_RENDERER.md). The renderer does not expand the Armory scraper or retrieve portraits/statistics/PvP data from other sources.

## Tests and checks

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src/wow_forever_armory
.venv/bin/python -m compileall -q src tests
```

All automated tests use temporary SQLite files, synthetic profiles, and mocked Discord/Core calls. They make no network requests or gateway connections.

## Armory Core example

```python
import asyncio
from wow_forever_armory import ArmoryClient


async def main() -> None:
    client = ArmoryClient(timeout=10, max_retries=2)
    character = await client.fetch_character(
        "en-us/forever/eu/armory/character/pepe-feels"
    )
    print(character.name, character.active_specialization)


asyncio.run(main())
```

The Core accepts known relative paths and does not search for characters. A one-request live Core smoke test remains available through `python -m wow_forever_armory.smoke`, but is not run by this task or the offline suite.
