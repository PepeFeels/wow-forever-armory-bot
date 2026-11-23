# TASK-301 – deployment review and recovery preparation

## Code findings

The previous-release-only name for the saved database preflight collided with an existing immutable evidence file. The wrapper now uses `armory-db-preflight.<previous-release-sha>.<content-sha256>.py`; creation is exclusive and an existing destination is never overwritten. `recovery_audit.py` and `production_admin.py` recognize the new content-bound name while retaining compatibility with historical filenames. The audit validator does not rewrite or reinterpret older records.

The journal's `service_state` and `backup_timer_state` represent the intended post-deployment state (`active/enabled`), not the state observed before an install. Prior code did not persist the initial values. New journal schema v2 records `initial_unit_states` separately. This explains how an older journal can say active/enabled while systemd checks report inactive/disabled: the fields represented different moments, but their names did not make that distinction clear.

Before the correction, a pre-shutdown failure after `backup_verified` could not transition to `aborted_before_service_changes`; the exception handler then fell through to manual recovery. The wrapper now allows this transition only after attempting timer restoration and verifying the complete observed unit-state mapping against its original snapshot. A mismatch or restoration error remains fail-closed and requires recovery. Timer-restoration exceptions are recorded by exception type, without including private error text.

## Tests

The deployment/recovery and release-metadata suite passed: **326 passed, 75 subtests passed** across wrapper, administrative recovery, preflight, database snapshot/install/migration, recovery audit, recovery compatibility, and pin tests.

The whole repository suite does not currently collect because `tests/test_command_sync.py` imports missing `wow_forever_armory.command_sync`. Excluding that module before updating the release pins yielded 498 passes, 142 passing subtests, and four failures: two Discord setup/sync expectations and two release-metadata pins. The release-metadata pins are now updated and pass; the two Discord test failures are outside TASK-301 deployment/recovery scope and remain before claiming a fully passing repository suite.

## Production evidence and exact safe next action

The confirmed facts provided for TASK-301 are: current release `dcceeb85464e09e34b112e41b16a833226c72270`, candidate `fa15f4e3095fad60ff48a059f7ce07414633d07d`, journal `manual_recovery_required` interrupted at `backup_verified`, and bot/timer `inactive/disabled`. The historical preflight hash is `c1c4f067fa2b465726d374ecb06acfae82a1ec234dc0f38c4734a6c422089803`; the approved candidate preflight hash supplied for comparison is `48c6b711e2518f1086101728412c126812369234735e4758c33eb3a4f981ea8f`.

The journal bytes, database integrity, registered backup checksum, and registered characters have not been independently verified in this workspace. Do not run recovery, migration, restore, installation, or service activation until those checks have been reviewed. After verifying the committed diagnostic script SHA-256 from this report, the administrator can run this single command from the shared workspace on `nololinux-server`:

```sh
sudo /usr/bin/python3 -I /home/codex-dev/projects/wow-forever-armory-bot/TASK-301/deploy/task301_readonly_diagnostics.py
```

It emits a JSON report containing selected non-secret journal fields and its hash, active release marker, SQLite integrity and table row counts, backup hashes/integrity, preflight inventory, systemd state, and Armory process count. It does not write to the database, journal, backups, units, or secrets. It hashes the backup referenced by the journal and reports whether it matches the registered checksum. SQLite verification uses the bundled snapshot verifier only after its source SHA-256 matches the reviewed constant.

## Recovery decision

The supported `production_admin.py acknowledge-recovery` path is currently restricted to a fail-closed incident interrupted at `installer_started` and requires a deployment manifest. It does **not** accept a `manual_recovery_required` journal interrupted at `backup_verified` without that manifest. The current production incident therefore cannot safely be acknowledged through the existing command. Do not edit the active journal, fabricate a manifest, or bypass the validator. A dedicated hash-bound pre-shutdown recovery path must first be implemented and tested against the actual redacted journal schema and registered backup evidence. Until then the exact production acknowledgment invocation cannot safely be supplied.

After that workflow is approved and the evidence matches, the planned sequence is: hash-bind a review record to the active journal; use the reviewed root-owned `production_admin.py` recovery command to archive and acknowledge without changing journal bytes; rerun the read-only audit/preflight; stage the candidate with release and installer pins; run the production wrapper once; verify the generated backup and manifest; finalize and activate using the administrative workflow; then check one bot process, systemd active/enabled states, Discord connection, command response, registered characters, and an Armory refresh. Never restore the backup unless the live database fails integrity and a separate reviewed restore has a verified recovery path.

**Recommendation: NO-GO for production recovery and deployment** until the admin returns the diagnostic output and the pre-shutdown journal acknowledgment workflow exists and passes hash-bound recovery tests.

