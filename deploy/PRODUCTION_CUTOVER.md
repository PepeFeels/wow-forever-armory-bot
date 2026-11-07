# Manual production cutover review procedure

This is a reviewed operator checklist, not an executable cutover script. It is
intentionally manual because the legacy process does not honor the new runtime
lock. Do not run it without a separately approved maintenance window. The
commands below can change host state and require the release, source database
path, backup destination, and rollback owner to be confirmed first.

## Before the window — read-only checks

On the Ubuntu host, confirm the production checkout is on
`feature/v0.2.0-discord`, clean, and exactly at the approved SHA:

```bash
git -C /home/codex-dev/projects/wow-forever-armory-bot status --short --branch
git -C /home/codex-dev/projects/wow-forever-armory-bot rev-parse HEAD
```

Inspect the live process without printing its environment or token. Use the
release's `production_migration.py --inspect-legacy` command and record its PID,
start time, user, working directory, and resolved database path. Compare that
path with the intended source. Confirm service and timer units are inactive
and disabled, the target database does not exist, disk space is sufficient,
and the backup destination is on the private StateDirectory filesystem.

Check the Python runtime against the lock target (CPython 3.14, Linux x86_64),
and inspect installed unit files with `systemd-analyze verify`. Verify that the
systemd credential is available to the service as a file, and that the
application receives only `DISCORD_BOT_TOKEN_FILE`. If using the logo, verify
permission to use it and install it as a root-owned group-readable regular PNG
at `/etc/wow-forever-armory-bot/wow_forever_logo.png`, mode `0640`, group
`wow-armory`; otherwise omit `ARMORY_LOGO_PATH` and expect the header space to
remain blank.

## During the approved window

The commands below are templates for the approved host and are not executed in
this task. Set each value from verified host inspection; do not guess paths.
`RELEASE_SHA` must be the full SHA produced after the candidate is reviewed and
integrated onto the installer-required branch.

```bash
REPO=/home/codex-dev/projects/wow-forever-armory-bot
RELEASE_SHA='REPLACE_WITH_REVIEWED_FULL_SHA'
PYTHON314='REPLACE_WITH_ABSOLUTE_CPYTHON_3_14_PATH'
SOURCE_DB='REPLACE_WITH_RECORDED_LEGACY_DATABASE_PATH'
TARGET_DB=/var/lib/wow-forever-armory-bot/guild-armory.sqlite3
MIGRATION_BACKUPS=/var/lib/wow-forever-armory-bot/backups/migration
LEGACY_USER='REPLACE_WITH_VERIFIED_PROCESS_USER'
LEGACY_PYTHON='REPLACE_WITH_VERIFIED_LEGACY_PYTHON_PATH'
PRESTOP_BACKUP_DIR='REPLACE_WITH_PRIVATE_BACKUP_DIRECTORY'
```

The pre-stop online backup uses the legacy process's verified user and paths:

```bash
sudo -u "$LEGACY_USER" env \
  ARMORY_DATABASE_PATH="$SOURCE_DB" \
  ARMORY_BACKUP_DIR="$PRESTOP_BACKUP_DIR" \
  "$LEGACY_PYTHON" -m wow_forever_armory.operations backup
```

Install the exact release and dormant units before stopping the old process.
This installer does not start or enable anything:

```bash
sudo "$PYTHON314" "$REPO/deploy/production_install.py" \
  --release-sha "$RELEASE_SHA" \
  --config-source /run/armory-install/config \
  --credential-source /run/armory-install/discord-token
```

After the separately approved graceful stop and process-exit check, make the
final backup and migrate into the new state path:

```bash
sudo "$PYTHON314" "$REPO/deploy/production_migration.py" \
  --source-database "$SOURCE_DB" \
  --expected-source-database "$SOURCE_DB" \
  --target-database "$TARGET_DB" \
  --backup-directory "$MIGRATION_BACKUPS"
```

Before starting, run the read-only integrity check as the service user, then
inspect the dormant units. Start and enable only after approval:

```bash
sudo -u wow-armory env ARMORY_DATABASE_PATH="$TARGET_DB" \
  /opt/wow-forever-armory-bot/current/.venv/bin/python \
  -m wow_forever_armory.operations check
sudo systemd-analyze verify \
  /etc/systemd/system/wow-forever-armory-bot.service \
  /etc/systemd/system/wow-forever-armory-backup.service \
  /etc/systemd/system/wow-forever-armory-backup.timer
sudo systemctl start wow-forever-armory-bot.service
sudo systemctl status wow-forever-armory-bot.service
sudo journalctl -u wow-forever-armory-bot.service --since '10 minutes ago'
```

After Discord and data recovery checks pass, enable restart-after-reboot and
the scheduled backup:

```bash
sudo systemctl enable wow-forever-armory-bot.service
sudo systemctl enable --now wow-forever-armory-backup.timer
```

1. Create an online SQLite backup of the legacy database while the old process
   is active. Verify `integrity_check`, compare full table rows or approved
   aggregate/reference checks, and retain it outside the project checkout.
2. Record the exact old process identity again. Gracefully stop only that
   verified process using its known supervisor. Confirm it exited. Do not use
   a broad `pkill` or stop other services.
3. The migration tool makes a final online SQLite backup after verifying the
   old process has exited. Verify that backup's integrity, row contents, and
   Discord references before proceeding.
4. Run `production_install.py` for the reviewed full release SHA before stopping
   the old process. It installs a versioned release and dormant units but does
   not enable or start them.
5. After the old process has stopped, run `production_migration.py` with both the observed source path and the
   same path explicitly confirmed. It refuses a running Armory process or an
   existing target; it creates a fresh backup, verifies the snapshot, and
   atomically publishes a new StateDirectory database. Verify target
   integrity, full row equality, owner, mode `0600`, and startup preflight.
6. Start exactly one service only after those checks. Verify Discord login,
   global command sync, guild and member-row recovery, and journald output.
   Enable autostart and the backup timer only after the service is healthy.
7. Run a scheduled backup once and verify its integrity, mode, ownership, and
   readable database contents. Retain both pre-stop and final migration
   backups.

## Failure and rollback

- **Before the old process stops:** abort without changing it. Keep the verified
  online backup and investigate.
- **After stop, before migration completes:** keep the new service stopped.
  Preserve the original database, all backups, temporary files, and any
  `.restore-in-progress` marker. A migration failure is fail-closed; do not
  remove the marker by hand. If source integrity is good, restart only the
  legacy release against the unchanged original database after confirming no
  new instance is running.
- **After migration, before first service health is confirmed:** stop/leave the
  new unit disabled. Preserve the migrated database and backups. Verify the
  original database and backup separately. To return to the legacy process,
  point its existing launch method at the untouched original database and
  start it only after the systemd unit is confirmed stopped. Never point both
  versions at the same file.
- **After the new service has accepted registrations:** first preserve a fresh
  backup of the new database. Do not restore an older snapshot over newer
  registrations unless the owner explicitly accepts that loss. Prefer
  reverting only the application release while keeping the current compatible
  database; if schema compatibility is uncertain, stop and perform a reviewed
  data migration rather than guessing.

No automatic rollback overwrites the active database. An interrupted restore
marker remains a startup gate until an operator verifies the active database,
rollback copy, and quarantined WAL/SHM sidecars.

## Release integration gate

The production installer only accepts a clean exact-SHA checkout on
`feature/v0.2.0-discord`. This graphics remediation branch must first be
reviewed and integrated onto that branch by an approved Git operation. Do not
merge, push, deploy, or change the production checkout as part of this review.
