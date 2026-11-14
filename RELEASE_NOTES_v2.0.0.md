# WoW Forever Armory Bot v2.0.0 — Release Candidate

**Status:** Prepared from `a1a6a2ad95302f7e3c4900f18e91691f43631e18` for
review. This candidate is not deployed, pushed, or tagged.

## Included changes

- Preserves the complete graphics changes from the failed v1.1.4 deployment
  candidate.
- Builds the versioned Python environment with safe, service-readable
  permissions even when the deployment wrapper starts with `umask 077`.
- Restores the wrapper's restrictive umask after release construction.
- Prevents Python bytecode caches from being written into the trusted source
  checkout.
- Reports allowlisted installer failure categories and exit codes through a
  dedicated descriptor; raw command output and secrets are not passed to the
  deployment journal.
- Includes the verified production wrapper source and validates its release
  pin from a root-owned mode `0600` policy file.
- Uses a reviewed setuptools 84.1.0 build-backend pin with a verified wheel
  hash; runtime dependencies were not changed by the audit.

## Compatibility and data

- The release keeps the existing SQLite schema and performs no migration.
- Code-only upgrade preserves the installed config, Discord credential,
  systemd units, database, and backups.
- The service remains stopped and disabled after installation. Activation and
  deployment require separate approval.
- The candidate remains compatible with the existing Discord credential-file
  interface and multi-guild Armory behavior.

## Release gate

The package version is `2.0.0`. The installer requires a clean exact-SHA
checkout on `release/v2.0.0`, descending from
`a1a6a2ad95302f7e3c4900f18e91691f43631e18`. The final candidate SHA and
installer hash must be placed in a root-owned deployment pin after the
candidate commit is reviewed. This note does not authorize installing that
pin, running the wrapper, or deploying the release.
