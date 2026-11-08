# WoW Forever Armory Bot v1.1.0 — Release Candidate

**Status:** Prepared for independent review; not published or deployed.

v1.1.0 updates the Discord interface to English and provides an Add & Edit
workflow while preserving sword reaction registration. Member overview messages
identify their owner, and automatic image updates suppress repeat mention
notifications. Character overview graphics are more compact, display profession
progress, use available first and last names, adapt long names, and include nine
bundled high-resolution Classic class icons.

## Requirements and packaging

- CPython 3.14 on Linux x86_64.
- Package version: 1.1.0.
- The existing build backend and dependency locks remain in use. All nine class
  icon PNGs are included as package data without runtime downloads.
- Discord application token delivery, SQLite state, and backup permissions
  follow the existing v1.0.0 operational requirements.

## Compatibility and upgrade notes

The character snapshot format adds an optional surname field. Existing snapshots
without that field remain readable. No SQLite schema migration is introduced;
upgrades should retain the existing database and verified backup procedures.

The installer accepts a clean exact-SHA checkout on `feature/v1.1.0` whose
history descends from v1.0.0 base commit
`4ac05712259e26560d1818746f5cee90e6793f78`. The installer retains its stopped
and disabled unit requirements, isolated database snapshot verification, and
atomic release switch. Installation does not start or enable services.

Production remains on v1.0.0. Any production installation or activation requires
separate approval and the reviewed procedure in [OPERATIONS.md](OPERATIONS.md).

## Verification

Automated checks use local fixtures, synthetic profiles, temporary databases,
and mocked Discord interactions. They do not connect to Discord or the Armory
API. Host-level installation, backup and restore rehearsal, service health, and
Discord verification remain part of the separately approved production review.
