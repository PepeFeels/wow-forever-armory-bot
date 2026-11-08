# Changelog

## v1.1.1 — release candidate

Prepared on `release/v1.1.1` for independent release review. This corrective
release adds Discord setup and recovery observability, accurate member-row
counters, privacy-safe error reporting, explicit setup/synchronization failure
handling, and consistent deferred classification for panel network and timeout
failures. Recovery remains idempotent across reconnects. The original PROD
incident cause is not established by these changes; production remains on
v1.1.0 pending separate approval and production evidence.

### Compatibility and operational notes

- Package metadata identifies this candidate as version 1.1.1.
- No database schema migration or production configuration change is included.
- The installer requires a clean exact-SHA checkout on `release/v1.1.1` whose
  history descends from the approved v1.1.0 checkpoint
  `e6b8c0ce4e1444d87ff87f29df2802a49bc1ad3f`.

## v1.1.0 — released

Originally prepared for independent review on `feature/v1.1.0`, then published
from approved release commit
`e6b8c0ce4e1444d87ff87f29df2802a49bc1ad3f`. It is the production baseline for
the v1.1.1 corrective release preparation.

### Included

- English Discord interface with the Add & Edit flow and preserved sword
  reaction registration.
- Member-owned overview messages with controlled initial mentions and silent
  automatic image updates.
- Compact one-to-three-character graphics with profession progress bars,
  available surnames, adaptive character-name sizing, and nine bundled HD
  Classic class icons.
- Backward-compatible character snapshot parsing and persistence; no SQLite
  schema migration is introduced.

### Compatibility and operational notes

- Package metadata identified this release as version 1.1.0.
- The installer requires a clean exact-SHA checkout on `feature/v1.1.0` based on
  the v1.0.0 base commit `4ac05712259e26560d1818746f5cee90e6793f78`.
- At v1.1.0 preparation time, production remained on v1.0.0 pending its
  separately approved release procedure.

## v1.0.0 — release candidate

This is the first stable-release candidate, prepared from the verified v0.2.0
production code line. It is not published or deployed yet. Scheduled backup,
backup integrity, and isolated restore verification have passed on the host.
Controlled restart, Discord reconnect, and final service checks remain pending.

### Included

- Multi-guild Guild Armory registration with guild/member ownership isolation,
  up to three characters per member, private management controls, and recovery
  of existing panel and member-row messages.
- Armory Core profile retrieval, strict official Armory URL/path validation,
  bounded asynchronous requests, and readable Discord embeds.
- Local character overview rendering for one to three characters, optional
  separately provisioned logo, bundled licensed fonts, and missing-data
  placeholders.
- SQLite persistence, serialized updates, online backups, explicit restore,
  integrity gates, rollback preservation, and process locks.
- Systemd templates, credential-file handling, structured logging, and a
  code-only release installer with snapshot-based database verification.

### Compatibility and operational notes

- CPython 3.14 on Linux x86_64 is the verified production dependency target.
- No automatic schema migration is included. Keep upgrades backward-compatible
  with the current SQLite schema and verify a backup before installation.
- Production command sync is global and is not restricted to the first guild.
- The official WoW Forever logo remains a separately provisioned asset; it is
  not included in the public package.
- Automated tests are offline and use temporary databases and mocked Discord
  interactions. Scheduled backup and isolated restore verification have passed
  on the host. Controlled restart and Discord reconnect remain pending.
