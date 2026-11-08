# WoW Forever Armory Bot v1.1.2 — Release Candidate

**Status:** Prepared on the development branch for independent review; not
committed, published, tagged, or deployed.

v1.1.2 corrects the Discord bot logger name for module execution. The app is
launched with `python -m wow_forever_armory.discord_bot`; that execution mode
sets `__name__` to `__main__`. The bot now uses the stable logger name
`wow_forever_armory.discord_bot`, which reaches the existing configured
application handler. Logging levels, handlers, formatting, and sanitization
are unchanged.

The regression test runs the module in a subprocess with no token configured.
It checks that startup INFO output is visible before the expected safe
missing-token exit, without making Discord or database calls. Existing tests
cover setup-hook, READY, recovery, and exception sanitization behavior.

No production service, database, credential, or configuration was accessed or
changed. Whether the original PROD invocation used the affected published
source remains to be verified separately.

## Release gate

The installer requires a full approved release SHA, a clean checkout whose
HEAD exactly matches that SHA, and branch `release/v1.1.2`. The release commit
must descend from checkpoint
`de1ffb89a366ae1e4ba73db2a30804698d218e40`, the approved v1.1.1 base. Existing
exact-SHA, clean-worktree, ancestry, stopped/disabled service, runtime-file,
database-preservation, and atomic-switch checks remain in force. The installer
does not start or restart services.

Release commit creation, publication, and production installation require
separate approval.
