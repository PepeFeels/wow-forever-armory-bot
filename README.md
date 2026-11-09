# WoW Forever Armory Bot

A community Discord bot for WoW Forever that lets members register and showcase up to **three characters per person** in a shared Guild Armory panel.

Invite the bot, choose a channel, and let guild members add their characters through private, interactive controls.

[**Add WoW Forever Armory Bot to your Discord server**](https://discord.com/oauth2/authorize?client_id=1568542909404549130)

## Features

- **Guild Armory panel** – create a shared panel in a channel chosen by your server administrator.
- **Up to three characters per member** – each Discord member can register up to three unique characters in each server.
- **Character overview images** – character information is presented in a wide image row in the panel channel.
- **Private character management** – members add, replace, and remove characters through interactive controls and private URL forms.
- **Automatic updates and recovery** – the bot updates its own panel messages and reconciles saved entries after reconnecting.
- **Server-specific data** – registrations are kept separate for each Discord server.

Character links must be HTTPS Armory profile URLs on the official worldofwarcraft.blizzard.com website and use a supported locale, region, and character path.

## Add the bot to your server

[**Invite WoW Forever Armory Bot**](https://discord.com/oauth2/authorize?client_id=1568542909404549130)

1. Open the invite link above.
2. Select the Discord server where you want to install the bot.
3. Authorize the requested permissions.
4. Choose the channel where the Armory panel should appear.
5. Ask a server administrator with **Manage Server** permission to run `/armory activate` in that channel.

The bot creates the instruction panel. Members can then use **Add & Edit** to register their characters.

### Required installation scopes

The Discord installation link uses these OAuth2 scopes:

- `bot`
- `applications.commands`

### Bot permissions

Grant the bot these channel permissions:

| Permission | Why it is needed |
|---|---|
| View Channels | Access the channel containing the Armory panel |
| Send Messages | Create the panel and character rows |
| Embed Links | Display linked content where applicable |
| Attach Files | Upload character overview images |
| Read Message History | Find and reconcile existing panel messages |

**Administrator permission is not required.** The person activating the panel must have **Manage Server** permission (or Administrator). The bot does not need permission to manage roles, channels, or other members.

## How members use the panel

1. Find the Armory panel in the channel selected by your server administrator.
2. Select **Add & Edit**.
3. Add a character using its full WoW Forever Armory profile URL.
4. Use the character controls to add another character, replace one, or remove one.

Each member can have up to three characters per Discord server. Character registrations are managed through private interactions; the resulting overview is displayed in the shared panel channel. Registered characters are checked against the Armory about every five minutes; the member image is refreshed only when normalized character data changes, without pinging the member again. The Armory's own last-updated timestamp can lag behind a check, so a successful check does not guarantee newer profile data.

## Privacy and data

- Character URLs are entered through private interaction forms; the bot does not need to read ordinary message content.
- The bot does not require Discord's privileged Message Content or Members intents.
- Registrations are stored separately for each Discord server.
- The bot stores the Discord identifiers and character/profile information needed to maintain the panel and recover its messages.
- The bot retrieves public character profile information from supported official WoW Forever Armory pages.
- Server administrators should make sure members understand that registered character overviews are displayed in the selected shared Discord channel.

If you need a guild's stored data deleted, contact the bot operator and follow the project's data-removal procedure.

## For developers and operators

The bot is built in Python and uses a local SQLite database for persistent Guild Armory state. Its Armory Core validates supported profile URLs, fetches public profile pages over HTTPS, and parses character data. The Discord layer handles commands, persistent controls, panel messages, and recovery.

- [Operations guide](OPERATIONS.md)
- [Troubleshooting](TROUBLESHOOTING.md)
- [Character graphic renderer](GRAPHIC_RENDERER.md)
- [Changelog](CHANGELOG.md)

### Development setup

Use Python 3.14 or newer:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

Configure a development bot token in your private .env file. Never commit a real token or share it in logs, screenshots, command arguments, or public issues.

Example development configuration:

```dotenv
DISCORD_BOT_TOKEN=your-private-test-token
ARMORY_ENVIRONMENT=development
# Optional: use a test guild for faster command propagation during development.
DISCORD_TEST_GUILD_ID=123456789012345678
ARMORY_TIMEOUT=10
ARMORY_DATABASE_PATH=data/guild-armory.sqlite3
```

Run the bot only when you intend to connect to Discord:

```bash
.venv/bin/python -m wow_forever_armory.discord_bot
```

### Tests

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src/wow_forever_armory
.venv/bin/python -m compileall -q src tests
```

Automated tests use temporary databases, synthetic profile data, and mocked Discord/Core calls. They do not connect to Discord or make live Armory requests.

## Security

- Keep bot tokens private. Never commit credentials.
- Use only the permissions required for the panel; Administrator is not needed.
- Keep production database files and backups private. They contain Discord identifiers and character/profile data.
- Use the documented production installation, backup, restore, upgrade, and rollback procedures before changing a live deployment.

## License

See the repository's license file for the terms that apply to this project.
