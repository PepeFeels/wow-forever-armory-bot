# WoW Forever Armory Bot v1.0.0 — Release Candidate

**Status:** Prepared for independent review; not published or deployed.

v1.0.0 packages the Armory Core and the persistent, multi-guild Discord Guild
Armory as a stable-release candidate. It includes private character
registration and management, per-guild ownership isolation, persistent member
image rows, startup recovery, local character overview rendering, structured
logging, and operational backup/restore tooling.

## Requirements

- CPython 3.14 on Linux x86_64.
- Discord application token delivered as a systemd credential file.
- SQLite state and backup directories with the documented private ownership
  and permissions.
- Optional WoW Forever logo provisioned separately only where its use is
  authorized.

## Upgrade notes

The production installer accepts a clean `release/v1.0.0` checkout at the exact
reviewed commit and performs a code-only upgrade. It verifies the database by
copying it and any WAL into a private temporary snapshot; SQLite runs only on
that snapshot. It leaves the active database, sidecars, configuration,
credentials, and installed systemd files unchanged. The service and backup
timer remain stopped and disabled after installation.

The release has no automatic SQLite schema migration. Follow [OPERATIONS.md](OPERATIONS.md)
and verify the latest backup and rollback release before any approved host
change. Do not start or enable services as part of release preparation.

## Production verification status

Verified on 2026-11-08 UTC:

- Scheduled backup completed successfully at 00:26:48 UTC.
- The generated backup is owned by wow-armory, has permissions 0600,
  and passed SQLite integrity verification.
- An isolated restore-test database passed integrity verification and
  contained the expected two character rows.

Still awaiting final verification:

- Controlled Armory-only restart and Discord reconnect.
- Final service health, INFO journald capture, startup recovery,
  and absence of unexpected restart loops.

The release remains unpublished and undeployed.
