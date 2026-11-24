# TASK-303 – Backup and audit repair

## Production findings

- The running release is `2e6198f0733404430615cd83f8239e2c97253cfb` at
  `/opt/wow-forever-armory-bot/current`.
- The bot and backup timer were active/enabled. The backup service was
  failed/static because its startup preflight rejected the deployment audit.
- The live database is
  `/var/lib/wow-forever-armory-bot/guild-armory.sqlite3`. An online SQLite
  backup created while the bot stayed active contains 1 guild, 4 members and
  5 characters, and passed the isolated snapshot integrity check.
- The old backup contained 4 characters. It was retained, along with all other
  existing backup files.

## Audit cause and correction

The audit validator checked an activation event's manifest hash against a
`manifest_raw` value left over from the earlier journal-validation loop. With
more than one deployment manifest, that value could belong to another release.
The TASK-302 activation record itself matched its journal, acknowledgment,
release and manifest, but the validator rejected it. As a result the approved
normal-operation transition was not recognized, and the validator compared the
live database to the older TASK-267 database hash.

Activation validation now resolves the manifest by the release SHA in that
activation's archived journal, reads that manifest through the existing
no-follow stable-read helper, and checks the event against its hash. Historical
TASK-267 attestations, review records, reports, backup hashes and preflight
hashes remain pinned and verified. The live database may change after a
verified activation only while its ownership, mode, inode and sidecar safety
checks continue to pass.

Regression coverage exercises per-release manifest selection with multiple
manifests and confirms that a database with five character rows is accepted
after the trusted transition but rejected before it.

## Verified online backup

- Path:
  `/var/lib/wow-forever-armory-bot/backups/active/guild-armory-20261123T211029.025689Z.sqlite3`
- SHA-256:
  `2e33b5635f25b6bb31908edd5f22dc4cd28d4b911915af3da7846452be1cd448`
- Mode/owner/group: `0600`, uid `979`, gid `973` (`wow-armory:wow-armory`)
- Isolated SQLite integrity: `ok`
- Counts: 1 guild, 4 members, 5 characters

The backup was created with the application's SQLite online-backup operation
while the bot process remained running. Retention was set high for this
one-time operation, so no older backup was pruned.

## Candidate verification

The bundle and candidate commit are recorded in the task history. Targeted
tests cover the audit chain, startup preflight and release metadata. A
root-owned staged copy of the corrected audit validator and its matching
startup preflight passed:

```text
Armory database metadata and recovery audit: PASS for --unit backup
```

The changed source hashes are listed in `TASK-301-RECOVERY.md` only as the
historical TASK-301/TASK-302 values. The current TASK-303 source hashes are:

| File | SHA-256 |
| --- | --- |
| `deploy/recovery_audit.py` | `337a179787a799fd67ec8d6f9f1e298e1b55e5b0accc21472289cd24dd159dbc` |
| `deploy/systemd/armory-db-preflight.py` | `1be6aec43cadd7c11a96622eac06e73236d76dcca830c25db0c5f43b7853f9a9` |
| `deploy/production_admin.py` | `3c52dbb2768d6e747107ba09290f88714554226839366edfddb51a6689cf9ee5` |
| `deploy/production_wrapper.py` | `31bfee8d6216a67fe46946d480d5976e70392d29a5c1a4a73c873e2b88743afa` |
| `deploy/production_preflight.py` | `99695b0fb0ca4228545c42399f84203296575b2f9b5d8a242fde8bd934b470b1` |

## Installation boundary

Install only the hash-verified shared audit validator, startup preflight,
production admin tool and deployment wrapper from root-owned staging. Preserve
the currently installed copies as hash-named, read-only files first. Verify
the new helper pair with `--unit backup`, then run the backup service once and
confirm the resulting backup. Keep the bot process and its systemd state
untouched; no application release switch or bot restart is part of this repair.
