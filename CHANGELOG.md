# Changelog

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
