# WoW Forever Armory Bot v1.1.4 — Hotfix Candidate

**Status:** Prepared on `hotfix/v1.1.4-empty-snapshot-verification` for review.
It is not committed, tagged, or deployed.

## Change

The isolated production database snapshot verifier now accepts valid,
initialized databases with zero guild, member, and character rows. This
completes the zero-guild allowance introduced in the v1.1.3 startup preflight
fix, which was not sufficient by itself because the installer snapshot gate
still rejected empty guild state.

SQLite integrity, required-table, source ownership and permission, file
signature, WAL/SHM consistency, snapshot isolation, and source-change checks
remain enforced. No dummy records, schema migration, or production data change
is introduced.

## Release gate

Package version is 1.1.4. The installer requires a clean checkout on
`release/v1.1.4` at the exact approved full SHA, descending from the published
v1.1.3 base commit
`c99e21f9afc6b9e4d5df0a8668a31f9ab9031dce`.

Production remains on v1.1.2 because the v1.1.3 deployment was safely blocked.
No production database, credential, configuration, service, or other bot was
accessed or changed during this preparation.
