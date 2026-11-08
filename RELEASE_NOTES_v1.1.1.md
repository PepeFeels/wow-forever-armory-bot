# WoW Forever Armory Bot v1.1.1 — Release Candidate

**Status:** Prepared for independent release review; not committed, published,
or deployed.

v1.1.1 is a corrective release focused on Discord startup and Guild Armory
recovery visibility. It distinguishes Gateway connection, READY, setup, and
recovery outcomes; reports accurate synchronized, created, removed, skipped,
and deferred member-row counts; and classifies panel fetch, upsert, and sword
reaction network failures as deferred. Setup and slash-command synchronization
failures are reported without logging exception messages or tracebacks.

## Compatibility and operations

- Package version: 1.1.1.
- No SQLite schema migration is introduced. Existing registrations and backup
  and restore procedures are preserved.
- Recovery is safe to retry after disconnects and avoids creating duplicate
  panel or member-row messages when existing bot-owned messages can be found.
- The original production incident cause is not established by local code
  changes or tests. Separate production evidence is needed to diagnose it.
- Production remains on v1.1.0 pending separately approved release and
  installation procedures. No production services, databases, credentials, or
  configuration were accessed or changed during this preparation.

## Release gate

The installer requires a full approved release SHA, a clean checkout whose
HEAD exactly matches that SHA, and branch `release/v1.1.1`. The release commit
must descend from checkpoint
`e6b8c0ce4e1444d87ff87f29df2802a49bc1ad3f`, the approved v1.1.0 base. Existing
ancestry, exact-SHA, clean-worktree, and installer safety checks remain in
force. The installer does not start or enable services.

Release commit creation, publication, and production installation each require
separate approval.
