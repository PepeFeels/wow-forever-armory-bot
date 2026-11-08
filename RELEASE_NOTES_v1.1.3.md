# WoW Forever Armory Bot v1.1.3 — Release Candidate

**Status:** Prepared on `hotfix/v1.1.3-empty-guild-preflight` for review. It is
not committed, published, tagged, or deployed.

## Change

The startup database preflight now accepts an initialized, structurally valid
database with zero guild rows. This supports intentionally cleared production
state without inserting dummy guilds or changing the SQLite schema.

All existing preflight checks remain: configured and existing database path,
non-empty file, expected service-account ownership, private permissions,
SQLite integrity, required tables, restore-in-progress marker rejection, and
duplicate Armory process detection.

## Release gate

Package version is 1.1.3. The production installer requires a clean checkout
on `release/v1.1.3` at the exact approved full SHA, descending from the
production v1.1.2 checkpoint
`0cb6377c1cb89e2594974e1c547c7d886ba7b8d4`.

The installer continues to preserve production database, backups,
configuration, credentials, permissions, and installed runtime files during a
code-only upgrade. It switches the release link atomically and does not enable,
start, or restart services.

No production service, database, credential, or configuration was accessed or
changed during this preparation.
