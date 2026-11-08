# Production operations

The project includes deployment tooling and systemd templates. Installation,
activation, upgrades, backups, and restores change host state and must follow an
approved maintenance procedure. The production installation uses a dedicated
`wow-armory` account, an
application release at `/opt/wow-forever-armory-bot/current`, and private state
under `/var/lib/wow-forever-armory-bot`.

## Installation preparation

For a new host or a separately approved reinstall, create a dedicated unprivileged
`wow-armory` user and group, install a reviewed release and its virtual
environment under `/opt/wow-forever-armory-bot`, and make release files
read-only to that account. The account should write only its state directory.
Create `/etc/wow-forever-armory-bot/config` with mode `0600` and owner
`root:root`; it contains only non-secret configuration, for example:

```dotenv
ARMORY_LOG_LEVEL=INFO
ARMORY_LOG_FORMAT=text
ARMORY_BACKUP_RETENTION=14
```

`ARMORY_ENVIRONMENT=development` and optional `DISCORD_TEST_GUILD_ID` are for
local development only; omit both from the production EnvironmentFile.

Place the token in `/etc/wow-forever-armory-bot/discord-token`, also mode
`0600` and root-owned. systemd passes it as a credential file. The stable
launcher at `/opt/wow-forever-armory-bot/bin/armory-launch` exports only the
credential file path as `DISCORD_BOT_TOKEN_FILE`; the application reads the
credential itself, so the value is not copied into the process environment.
This requires a release supporting `DISCORD_BOT_TOKEN_FILE`; earlier code that
does not support `DISCORD_BOT_TOKEN_FILE` is not a safe rollback target for
this launcher. The approved v0.2.0 base already supports the credential-file
interface.
Never put the token in unit arguments or logs. Install the service and timer templates only after
reviewing paths and account permissions. Run `systemd-analyze verify` on copies
in a staging environment. Do not enable either unit until a separate
production approval.

Future installation and activation commands below change server configuration;
they are documented only and were not run:

```bash
# Configuration-changing: install reviewed templates and reload systemd.
sudo install -o root -g root -m 0644 deploy/systemd/wow-forever-armory-bot.service /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/systemd/wow-forever-armory-backup.service /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/systemd/wow-forever-armory-backup.timer /etc/systemd/system/
sudo install -o root -g root -m 0755 deploy/systemd/armory-launch /opt/wow-forever-armory-bot/bin/armory-launch
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/wow-forever-armory-bot.service /etc/systemd/system/wow-forever-armory-backup.service /etc/systemd/system/wow-forever-armory-backup.timer

# Activating: requires separate production approval.
sudo systemctl enable --now wow-forever-armory-bot.service
sudo systemctl enable --now wow-forever-armory-backup.timer
```

### Code-only upgrade of an installed release

For a reviewed code-only update, stop and disable both the Armory service
and backup timer during an approved maintenance window. Verify that both
are inactive and disabled before invoking the installer. The installer
supports `--upgrade-existing`: it validates the
existing config and credential in place, requires the current link to point to
a complete managed release, requires the new commit to descend from that
release, and atomically switches `current`. It does not invoke the installed
startup database preflight against production state. Instead,
`production_db_snapshot.py` copies the database and relevant WAL through
read-only, no-follow descriptors into a private temporary directory and runs
SQLite integrity/schema checks only on that copy. It validates source metadata
before and after copying and fails closed if the source changes. The installer
does not migrate or write to the production database or its sidecars, and does
not rewrite the config, credential, systemd units, launcher, state directory,
or backup directory. The unit templates and launcher must be byte-identical to
the installed versions; runtime configuration changes need a separately
reviewed installation.

Run only after a release-specific approval, from the clean approved checkout
on `release/v1.0.0`:

```bash
# Configuration-changing: stages code and atomically switches the release link.
sudo /usr/bin/python3.14 deploy/production_install.py \
  --release-sha FULL_REVIEWED_RELEASE_SHA \
  --upgrade-existing
```

The release remains stopped and disabled. Before any approved start, verify
`current`, release marker, systemd units, database integrity, and backup
availability. The start preflight independently refuses an absent, empty,
corrupt, or restore-gated database. No migration command is part of this
upgrade path.

For a code rollback, keep both units stopped and disabled, preserve the new
release and all state/backups, and verify that the previous release supports
the installed systemd credential-file interface and current database schema.
Only then may an approved operator atomically repoint `current` and separately
authorize service activation. A pointer rollback alone does not guarantee a
working service. If compatibility is uncertain or fails, leave the service
stopped and disabled while preparing a corrected compatible release.
Never restore an older database snapshot merely to solve a code-startup failure.

To deactivate in an approved rollback, stop and disable both units while
retaining the database and backups:

```bash
# Operationally changes service state; not run by this task.
sudo systemctl disable --now wow-forever-armory-backup.timer
sudo systemctl disable --now wow-forever-armory-bot.service
```

The bot owns an advisory lock at
`<ARMORY_DATABASE_PATH>.runtime.lock` and an inode lock on the database file
for its entire lifetime. Locking the inode also catches hard-link aliases, and
sets the database file mode to `0600`. A second updated bot process using the
same database fails closed. Restore takes both locks and refuses to proceed
while the bot is running. Advisory locks coordinate these programs only; an
administrator must not bypass them by copying database files while the service
is active.

The pre-TASK-016 manually started bot does not hold this new lock. During the
first separately approved migration, the operator must verify and stop that
older process before starting the systemd unit or running restore. This work
does not inspect, stop, or replace that process.

### Legacy database import required before first service start

The service template uses
`/var/lib/wow-forever-armory-bot/guild-armory.sqlite3`. If the legacy process
uses a different path, the unit will otherwise initialize an empty database.
Do not start it until the existing database has been imported and verified at
the configured StateDirectory path. For the current legacy setup, confirm the
source path from the running process's filtered inventory and launch record;
never infer it from a similarly named file.

In an approved migration, create a verified online SQLite backup while the
legacy bot is still running, using a private staging directory writable by its
user. This is the pre-upgrade data checkpoint. Then stop the old process and
verify it exited; install the verified backup as the new StateDirectory
database with owner `wow-armory:wow-armory` and mode `0600`. Run
`operations check` and compare only aggregate guild/member/character counts
before starting systemd. Keep both the original database and migration backup
until panel recovery and member rows have been checked. The original DB and
`data/` directory may need restrictive permissions after the old bot stops;
do not change them during preparation.

Do not configure the service to use a database under `/home`: the unit uses
`ProtectHome=true`, and the dedicated service account must not inherit access
to the developer home directory. Do not create a new empty StateDirectory DB
and then copy characters into it manually.

For that approved migration only, identify the old process by command and
working directory without printing its environment (which may contain the
token). Compare its configured database path with the intended service path
using the launch record. If identity or database path is uncertain, do not
start the service or run restore:

```bash
# Read-only process identification; does not print environment variables.
pgrep -f '[w]ow_forever_armory.discord_bot'
ps -o pid=,user=,comm= -p "$BOT_PID"
readlink -f "/proc/$BOT_PID/cwd"

# Stops the old process gracefully; only in an approved maintenance window.
kill -INT "$BOT_PID"
for _ in $(seq 1 30); do
  if ! kill -0 "$BOT_PID" 2>/dev/null; then break; fi
  sleep 1
done
if kill -0 "$BOT_PID" 2>/dev/null; then
  echo 'Old bot process did not stop; do not start the service.' >&2
  exit 1
fi
```

Verify no old process remains before starting the unit. The restore utility
cannot identify an older process that does not honor the new lock; this manual
migration gate is required before first deployment.

Backup and restore also share `<ARMORY_DATABASE_PATH>.backup.lock`; backup
publication and retention additionally serialize through `.backup.lock` inside
the selected backup directory. Online backups do not take the bot runtime lock,
so SQLite can continue serving the application while the snapshot is made.

## Safe read-only checks

These commands inspect state and do not start or change the service:

```bash
systemctl status wow-forever-armory-bot.service
journalctl -u wow-forever-armory-bot.service --since today
journalctl -u wow-forever-armory-backup.service --since today
df -h /var/lib/wow-forever-armory-bot
sudo -u wow-armory /opt/wow-forever-armory-bot/current/.venv/bin/python \
  -m wow_forever_armory.operations check
```

`check` runs SQLite `PRAGMA integrity_check`. The backup job emits neutral
completion/failure events to journald and does not log paths or character data.
For a crash loop, inspect status, the latest bot journal entries, disk space,
and the integrity check before taking action. Discord connection and restored
guild rows can only be confirmed through the bot's startup/recovery logs and
the separately approved controlled Discord environment.

## Backup and retention

The backup utility uses SQLite's online backup API, so it can take a consistent
copy while SQLite is open. It briefly holds SQLite's `BEGIN IMMEDIATE` writer
reservation during the snapshot so later commits cannot repeatedly restart the
online backup; existing readers continue, while application writes wait for the
copy to finish. It writes a mode `0600` temporary file in the backup directory,
checks the copy with `integrity_check`, fsyncs it, then atomically publishes
it. Backup jobs use nonblocking locks to prevent overlap with another backup or
restore. The timer service has a 30-minute execution limit. If interrupted
before publication, a later run removes only matching unpublished temporary
files while holding the backup locks; completed backups are not treated as
temporary files. The default retention is 14 verified daily copies; set
`ARMORY_BACKUP_RETENTION` to a positive integer. Retention runs only after a
new verified backup has been published and always keeps that latest copy.
Keep an additional off-host backup according to the operator's recovery
objectives; this implementation does not provide off-host replication.

Run an explicit manual backup (this writes a backup file):

```bash
sudo -u wow-armory env ARMORY_DATABASE_PATH=/var/lib/wow-forever-armory-bot/guild-armory.sqlite3 \
  ARMORY_BACKUP_DIR=/var/lib/wow-forever-armory-bot/backups \
  /opt/wow-forever-armory-bot/current/.venv/bin/python \
  -m wow_forever_armory.operations backup
```

The included timer is an example only. A separately approved operator would
install and enable `wow-forever-armory-backup.timer`; do not enable both this
timer and an external scheduler for the same database.

## Restore

Restore is an intentionally explicit, writing operation. First capture the
current service state and identify a known-good backup. Stop the bot through
the approved service procedure, then run `operations check` on the selected
backup if desired. Restore itself verifies the backup, requires
`--confirm-replace`, and acquires the same runtime lock. It makes and verifies
a timestamped pre-restore rollback copy before replacing the database. SQLite's
backup API incorporates committed WAL data in that rollback copy. Any residual
`-wal` and `-shm` sidecars are moved aside with timestamped names before the
replacement; they are never blindly applied to the restored database. If a
restore fails before replacement, moved sidecars are put back where possible.
Do not delete the rollback or quarantined sidecars until the recovered service
has been checked.

If the active database fails SQLite integrity checking, restore preserves its
raw bytes and any residual WAL/SHM files as
`*.pre-restore-corrupt-*.sqlite3*`, then installs the verified backup. This raw
copy is forensic evidence and is **not** a verified or directly restorable
SQLite rollback. The valid pre-restore rollback path is created only when the
existing database passes integrity checking.

Restore writes a mode `0600` `*.restore-in-progress` marker before the
multi-file swap. Startup refuses to run while this marker exists. If a host
failure leaves the marker, inspect the database, `*.pre-restore-*.sqlite3`
rollback copy, and quarantined sidecars while the service remains stopped. Run
`operations check` on candidate databases; choose and atomically install the
known-good file only under the approved recovery procedure. Remove the marker
only after the active database and any sidecars have been reconciled and
verified. If the correct state is uncertain, keep the marker and escalate;
starting the service with unresolved WAL/SHM state is unsafe.

Example command (destructive to the active database; requires separate
approval and a stopped bot):

```bash
BACKUP_FILE='/var/lib/wow-forever-armory-bot/backups/guild-armory-YYYYMMDDTHHMMSS.ffffffZ.sqlite3'
sudo -u wow-armory env ARMORY_DATABASE_PATH=/var/lib/wow-forever-armory-bot/guild-armory.sqlite3 \
  /opt/wow-forever-armory-bot/current/.venv/bin/python \
  -m wow_forever_armory.operations restore \
  --backup "$BACKUP_FILE" \
  --confirm-replace
```

After restoring, run the integrity check, start the service only with approval,
then verify startup recovery, panel/member rows, and Discord connection. If
health checks fail, stop the service and restore the preserved
`*.pre-restore-*.sqlite3` copy using the same explicit procedure. A rollback is
also a database replacement and must not be performed while the bot holds its
runtime lock.

## Upgrade and rollback runbook

Every step below is for a future separately approved maintenance window; this
task did not perform it.

1. Read the installed version and reviewed release SHA. Check service status,
   recent journald output, disk space, and current SQLite integrity.
2. Create a verified online backup and confirm a recent backup is readable.
3. Stage the new release beside the current release; do not overwrite the
   current release in place. Preserve the previous release directory/symlink.
4. Stop and disable both `wow-forever-armory-bot.service` and
   `wow-forever-armory-backup.timer`. Verify both are inactive and disabled.
   Confirm no Armory process remains before installation.
5. From the clean, approved release checkout, run the documented
   `--upgrade-existing` installer. Do not manually replace the stable launcher
   or systemd units. The installer verifies a database snapshot, stages the
   release, and atomically switches `current` without starting either unit.
6. Verify the release marker, `current`, database integrity, backup availability,
   and systemd configuration. Only after separate approval, activate the bot
   and backup timer and verify Discord connectivity, startup recovery,
   panel/member behavior, and service health.
7. If health checks fail, stop and disable both units and preserve the current
   database, backups, and release artifacts. Verify credential-file and schema
   compatibility before any pointer rollback. If the previous release is
   compatible, an approved operator may atomically restore its pointer and
   separately authorize activation. Otherwise keep the service stopped and
   prepare a corrected release. Database restoration is a separate, explicitly
   approved recovery operation, never an automatic response to startup failure.

The v1.0.0 code has no schema migration framework. Keep upgrades
backward-compatible with its current SQLite schema until a migration strategy
is designed and reviewed. File-level SQLite backups do not protect against
host loss; copy verified backups off-host through a separately reviewed
mechanism.

## Security limits and unresolved observations

The unit templates use systemd credentials, a dedicated account, restrictive
umask, a private state directory, read-only system paths, and restricted
address families. These settings need validation against the target Ubuntu and
systemd version before installation. Backup files contain Discord IDs,
character paths, and public profile snapshots; keep the state directory private
and encrypt off-host copies. SQLite corruption requires restoring a verified
copy; do not use ad-hoc edits or copy a live database with ordinary `cp`.

The process lock protects against duplicate updated bot instances on one local
filesystem using flock. Canonical paths and hard-link aliases are covered by
the sidecar and database-inode locks. Multi-host filesystems may not provide
the required flock semantics and are not supported. The service unit does not
manage unrelated/manual bot processes that ignore these locks.

TASK 013C's observation that normal-runtime Armory INFO output was not yet
verified remains open until journald output is inspected on the production
host. Offline tests confirm the project handler emits INFO, but tests do not
prove normal service capture. discord.py/Python 3.14 deprecation warnings also
remain and should be reviewed when the upstream APIs are updated.

## TASK 016B review record

The independent hardening pass identified and addressed these implementation
gaps:

- A path-only process lock could be bypassed through database path aliases.
  Startup and restore now combine a canonical sidecar lock with `flock` on the
  database inode; tests cover symlink paths and a second process using a hard
  link. The database file is restricted to mode `0600`.
- The pre-existing manual bot does not acquire either new lock. The migration
  runbook now requires identifying it without printing its environment,
  matching its database path to the launch record, stopping it gracefully,
  and verifying it exited before starting the service or restoring.
- SQLite online backup may repeatedly restart if writes commit during the
  snapshot. Backup now takes a short `BEGIN IMMEDIATE` reservation, allowing
  readers while later writes wait. A real SQLite writer-thread test verifies
  that backup completes with consistent data while a writer is active.
- Opening a missing SQLite path for a lock could create an empty file before
  backup. Backup now checks source existence first, with a regression test.
- Restore previously could not proceed when the current database was corrupt.
  It now preserves raw database and residual sidecar bytes as a clearly named
  forensic copy, then installs only a verified backup. Healthy databases get
  a verified SQLite rollback copy that tests restore and integrity-check.
- Interrupted backup/restore attempts could leave private temporary files.
  Under the relevant locks, the next run removes only the tool's strict temp
  filename patterns; a restore marker still blocks automatic cleanup and bot
  startup where state is uncertain.
- The credential-file path previously accepted broadly readable files.
  Startup now requires a bounded, regular file with no group/other permissions
  and rejects symlinks where supported. Tests verify mode rejection. Backup,
  raw preservation, rollback, lock, and temp files are covered with restrictive
  permissions or scoped ownership.

Systemd templates passed syntax verification on temporary copies. The test
substituted the future release interpreter path because that path is not
installed in this development environment. Full verification against the
target Ubuntu host, actual account ownership, and credential delivery remains
part of the approved deployment review.

## TASK 019 final installation gate

`deploy/production_install.py` is an administrator-only, fail-closed installer.
It is preparation code and must not be run until reviewed and committed. It
requires the full approved release SHA and checks that it exactly matches the
clean checkout's HEAD on `release/v1.0.0`. It also verifies the release
is based on checkpoint `9f5548d3763b42d38fff0613ef2cfd6c3b560d07`. It builds
the versioned release from that exact SHA, creates the locked `wow-armory` account and
private directories, copies the supplied config and private credential,
installs the unit and timer, verifies their syntax, and reloads systemd. It
does **not** enable or start anything. It refuses to overwrite a different
`current` release or proceed if a target unit is already active or enabled.

The project owner confirmed PROD Guild ID `1549012880215638086` for initial
deployment verification. The runtime, installer, and systemd preflight are not
pinned to that guild. Production configuration must omit
`DISCORD_TEST_GUILD_ID`; production mode globally syncs application commands
even if a legacy test-guild variable remains in the process environment,
allowing every authorized guild where the application is installed
to use them. Global command changes may take time to propagate. The config source must be a
root-owned private regular file. The credential source must be root-owned mode
`0600`; the installer copies it without displaying its contents. Do not store
the token in the EnvironmentFile. The systemd credential is passed through the
stable launcher as `DISCORD_BOT_TOKEN_FILE`; the token value is not copied into
the bot process environment. A rollback candidate must support this file-based
credential interface; do not roll back to a launcher/application combination
that expects only `DISCORD_BOT_TOKEN`.

The release locks runtime and build dependencies in
`deploy/requirements-runtime.lock` and `deploy/requirements-build.lock` with
artifact hashes. They target CPython 3.14 on Linux x86_64. The installer checks
the interpreter and platform before creating a release, then installs only
hash-verified dependencies and builds the package without an unpinned isolated
build environment. Do not widen the supported Python/platform target without
generating and reviewing matching artifact hashes.

The official logo is optional and separately provisioned. If its rights are
confirmed, install it at
`/etc/wow-forever-armory-bot/wow_forever_logo.png`, owned by `root:wow-armory`
with mode `0640`, and set `ARMORY_LOGO_PATH` in the non-secret config. The
installer checks the path, ownership, mode, and service-account readability.
If the path is omitted, rendering leaves the logo region blank.

The installer refuses an existing versioned release directory without a valid
completion marker. It preserves that directory for operator review; inspect it
and quarantine it manually under an approved maintenance procedure before any
retry. It never recursively removes incomplete release content.

The unit runs `armory-db-preflight` (installed from
`deploy/systemd/armory-db-preflight.py`) with systemd's `+` privilege prefix
before the launcher. This short, root-owned helper needs host process visibility to
detect the old manual bot; it prints no environment values. It refuses to
continue if another Armory module process exists or process inspection fails.
It opens the configured database read-only and refuses a missing, zero-byte, invalid, uninitialized, or
restore-gated database. It requires the Guild Armory tables and at least one
registered guild. This prevents first start from creating an empty database
in place of migrated data.

Example invocation (configuration-changing; not run):

```bash
sudo /usr/bin/python3 \
  /home/codex-dev/projects/wow-forever-armory-bot/deploy/production_install.py \
  --release-sha <approved-full-commit-sha> \
  --config-source /run/armory-install/config \
  --credential-source /run/armory-install/discord-token
```

Stage the non-secret config from `deploy/systemd/armory-config.example`. It
contains no guild pin. Read the token through a hidden prompt; never
put it in a command argument, shell history, or log:

```bash
sudo install -d -o root -g root -m 0700 /run/armory-install
sudo install -o root -g root -m 0600 deploy/systemd/armory-config.example /run/armory-install/config
sudo /bin/bash -c 'umask 077; read -r -s -p "Discord token: " token; printf "\n" >&2; printf "%s\n" "$token" > /run/armory-install/discord-token; unset token'
```

After verifying installation, remove the temporary credential source from
`/run/armory-install`. The installed root-owned credential remains at
`/etc/wow-forever-armory-bot/discord-token` for systemd `LoadCredential`.

The script installs systemd files and runs `daemon-reload`, but intentionally
does not enable or start the service or timer. Verify installed paths and
ownership afterward. The source checkout must be clean and its HEAD must equal
`--release-sha`; use the full SHA from the reviewed and pushed checkpoint.
These scripts require a separately approved installation before they can be
run.

### Legacy process inspection and database import

Before the maintenance window, record the legacy process identity, working
directory and database path while it is running. The read-only command below
prints the selected database path and whether a credential variable exists.
It reports whether the running process has a test-guild override and whether
it matches an optional expected value; it never prints environment contents or
token values. The expected-guild comparison is observational only and is not
an installation or startup gate:

```bash
/home/codex-dev/projects/wow-forever-armory-bot/.venv/bin/python \
  /home/codex-dev/projects/wow-forever-armory-bot/deploy/production_migration.py \
  --inspect-legacy --expected-guild-id 1549012880215638086
```

After a separately approved graceful stop, run the migration script as root
with both the recorded source path and the same path explicitly confirmed. It
refuses to proceed if an Armory module process is running, the paths differ,
the source is invalid, or the StateDirectory database already exists. It holds
the updated bot's runtime lock and leaves the startup restore marker throughout
the import. It creates a final online SQLite backup in a dedicated migration
backup directory, checks integrity and aggregate guild/member/character
counts, then atomically publishes the verified copy. The original database
and pre-existing backups remain in place. A failed or interrupted import
leaves the marker so startup remains blocked pending operator review.

Example migration command (writes StateDirectory and backup files; not run):

```bash
sudo /opt/wow-forever-armory-bot/current/.venv/bin/python \
  /home/codex-dev/projects/wow-forever-armory-bot/deploy/production_migration.py \
  --source-database /home/codex-dev/projects/wow-forever-armory-bot/data/guild-armory.sqlite3 \
  --expected-source-database /home/codex-dev/projects/wow-forever-armory-bot/data/guild-armory.sqlite3
```

The expected source path must come from pre-stop inspection, not a guess. Keep
the service disabled and stopped until migration output, database ownership,
permissions and preflight have been checked. Do not remove a restore marker or
replace the StateDirectory database automatically after a failed import.

For rollback, stop the new unit, preserve its current database and backup,
switch the release pointer back, and use the current DB if its schema is
compatible. Do not restore the pre-migration snapshot over registrations made
after cutover unless that data loss is explicitly accepted.
