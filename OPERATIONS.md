# Operational preparation (not deployed)

This document and the unit files under `deploy/systemd/` are deployment
templates. Nothing in this repository installs, enables, starts, or changes a
system service. The examples assume a dedicated `wow-armory` account, an
application release at `/opt/wow-forever-armory-bot/current`, and private state
under `/var/lib/wow-forever-armory-bot`.

## Installation preparation

Before a separately approved deployment, create a dedicated unprivileged
`wow-armory` user and group, install a reviewed release and its virtual
environment under `/opt/wow-forever-armory-bot`, and make release files
read-only to that account. The account should write only its state directory.
Create `/etc/wow-forever-armory-bot/config` with mode `0600` and owner
`root:root`; it contains only non-secret configuration, for example:

```dotenv
DISCORD_TEST_GUILD_ID=123456789012345678
ARMORY_LOG_LEVEL=INFO
ARMORY_LOG_FORMAT=text
ARMORY_BACKUP_RETENTION=14
```

Place the token in `/etc/wow-forever-armory-bot/discord-token`, also mode
`0600` and root-owned. systemd passes it as a credential file. The stable
launcher at `/opt/wow-forever-armory-bot/bin/armory-launch` reads that file and
passes the token privately in the bot process environment. This supports both
the current code and rollback to the earlier v0.2.0 code, which reads
`DISCORD_BOT_TOKEN`. The launcher never prints the token. Never put the token
in unit arguments or logs. Install the service and timer templates only after
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
4. Stop the bot with `systemctl stop wow-forever-armory-bot.service` and verify
   it is inactive. The process lock provides a second guard for restore.
5. Keep the stable launcher in `/opt/wow-forever-armory-bot/bin` unchanged.
   Atomically switch the `current` symlink to the staged release, then start
   the service. Create a temporary symlink in the same parent and rename it
   over `current` with `mv -Tf`. Do not run a second bot process for health
   checks.
6. Verify `systemctl is-active`, startup/connection and recovery logs, database
   integrity, and controlled-guild panel/member behavior.
7. If health checks fail, stop the service, switch `current` back to the prior
   release, and start it. Restore the pre-upgrade database backup only if the
   new version changed data incompatibly and the prior release cannot use the
   current database. Verify integrity and recovery afterward.

The current v0.2.0 code has no schema migration framework. Keep upgrades
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
verified remains open. Offline tests confirm the project handler emits INFO,
but no live bot was started in this task, so normal journald capture has not
been proven. Validate it during a separately approved service test. The
previously observed 53 discord.py/Python 3.14 deprecation warnings also remain
documented.

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
