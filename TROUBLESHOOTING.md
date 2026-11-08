# Troubleshooting

Use these checks only on the host and account with approved access. They are
read-only unless marked **state-changing**. Never print the Discord token,
systemd credential contents, process environment, DM contents, or full
registration URLs.

## Service does not start

Read-only checks:

```bash
sudo systemctl status wow-forever-armory-bot.service --no-pager
sudo systemctl show wow-forever-armory-bot.service \
  --property=ActiveState --property=SubState --property=Result \
  --property=NRestarts --property=UnitFileState --no-pager
sudo journalctl -u wow-forever-armory-bot.service -n 100 --no-pager
```

Check that the service is using the intended `current` release and that its
credential declaration names the configured systemd credential. Inspect only
credential file metadata (owner, group, mode, size); do not use `cat`, `head`,
or shell tracing on the credential file. A startup error mentioning a missing
credential file should be resolved by an approved credential provisioning
procedure, not by placing the token in an environment file or command line.

## Repeated restarts or Discord connection failures

Check `NRestarts`, `Result`, and recent service journal records. Verify host
network availability and Discord service status through the approved
operational channel. Do not restart unrelated services or weaken TLS, firewall,
or systemd restrictions. A controlled restart changes service state and should
be performed only during an approved Armory maintenance window.

## Backup timer or backup failure

Read-only checks:

```bash
sudo systemctl status wow-forever-armory-backup.timer --no-pager
sudo systemctl list-timers wow-forever-armory-backup.timer --no-pager
sudo journalctl -u wow-forever-armory-backup.service -n 100 --no-pager
sudo find /var/lib/wow-forever-armory-bot/backups -maxdepth 1 \
  -type f -name 'guild-armory-*.sqlite3' \
  -printf '%TY-%Tm-%TdT%TH:%TM:%TS %u:%g %m %s bytes\n'
```

If no scheduled run has completed, treat automatic backup as pending. A manual
backup writes a new file and should be done only under the documented backup
procedure. Do not remove old backups to make space before confirming another
verified copy is available.

## Database or recovery warning

Do not open the live database with ad hoc SQLite commands while WAL/SHM sidecars
exist. Do not delete, rename, checkpoint, vacuum, or copy live sidecars. Keep
the service stopped if its startup gate reports a restore marker or uncertain
database state. Do not use an ad hoc SQLite command against the live path.
Keep the production service stopped and follow the approved snapshot-check or
backup/restore procedure. Preserve the database, sidecars, backup files,
restore marker, and rollback copies for operator review.

## Guild panel or member row did not recover

Check the bot's sanitized recovery events and verify channel permissions for
view history, send messages, attach files, and edit its own messages. Recovery
is scoped to stored guild/channel/message references. Do not manually delete
panel or member messages owned by another account, and do not reset SQLite
records to force reconstruction.

## Rollback

For an approved code rollback, first stop and disable both the Armory bot
service and backup timer. Preserve the current database, backups, and both
release directories. Verify that the recorded prior release supports the
installed systemd credential-file interface and current SQLite schema.

Only after these checks may an approved operator atomically point `current`
back to the prior release using [OPERATIONS.md](OPERATIONS.md). Service
activation requires separate approval; switching the pointer does not
automatically restore a working service.

A code-only rollback does not require restoring the database when its state
and schema remain compatible. If the new release has accepted registrations,
first preserve a fresh verified backup and assess compatibility. Never
overwrite newer registrations with an older backup without explicit owner
approval. If compatibility is uncertain, leave the service stopped and
disabled while preparing a corrected release.
