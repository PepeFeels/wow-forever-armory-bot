# TASK-301 – deployment review and recovery preparation

## Code findings

The previous-release-only name for the saved database preflight collided with an existing immutable evidence file. The wrapper now uses `armory-db-preflight.<previous-release-sha>.<content-sha256>.py`; creation is exclusive and an existing destination is never overwritten. `recovery_audit.py` and `production_admin.py` recognize the new content-bound name while retaining compatibility with historical filenames. The audit validator does not rewrite or reinterpret older records.

The journal's `service_state` and `backup_timer_state` represent the intended post-deployment state (`active/enabled`), not the state observed before an install. Prior code did not persist the initial values. New journal schema v2 records `initial_unit_states` separately. This explains how an older journal can say active/enabled while systemd checks report inactive/disabled: the fields represented different moments, but their names did not make that distinction clear.

Before the correction, a pre-shutdown failure after `backup_verified` could not transition to `aborted_before_service_changes`; the exception handler then fell through to manual recovery. The wrapper now allows this transition only after attempting timer restoration and verifying the complete observed unit-state mapping against its original snapshot. A mismatch or restoration error remains fail-closed and requires recovery. Timer-restoration exceptions are recorded by exception type, without including private error text.

## Tests

The deployment/recovery and release-metadata suite passed: **335 passed, 75 subtests passed**. The full repository suite passed: **520 passed, 142 subtests passed**. Tests ran with `PYTHONPATH=src` so they import this candidate checkout. Earlier missing-module and Discord setup/sync errors came from the shared virtual environment importing an older, separate checkout; this candidate contains `wow_forever_armory.command_sync`, and no Discord code changes were needed. The test run reported only upstream `asyncio.iscoroutinefunction` deprecation warnings.

## Production evidence and exact safe next action

The administrator's read-only diagnostic output confirms current release `dcceeb85464e09e34b112e41b16a833226c72270`, candidate `fa15f4e3095fad60ff48a059f7ce07414633d07d`, journal hash `b957b1735570ad821ddbc5f4f8bcaa82ca635e1393394a439879bef531a7f4ca`, phase `manual_recovery_required` interrupted at `backup_verified`, and null database-signature/preflight fields. It reports production DB SHA-256 `4b7e10b846c31fd080f21449478475f783cba00964f0b532b62606c356e1a998`, integrity `ok`, counts (guilds/members/characters) `1/4/4`; registered backup SHA-256 `697b031ced2bf45818eafeb8b4d2556784da04873b1565ce6e1678007a112b5c`, matching the journal, integrity `ok`, same counts; historical preflight SHA-256 `c1c4f067fa2b465726d374ecb06acfae82a1ec234dc0f38c4734a6c422089803`; previous-release pointer/marker; bot and timer inactive/disabled; backup service inactive/static; and zero Armory processes. The candidate preflight SHA-256 reported for the approved earlier candidate is `48c6b711e2518f1086101728412c126812369234735e4758c33eb3a4f981ea8f`.

## Recovery decision

The ordinary `acknowledge-recovery` path requires a manifest and cannot process this interrupted phase. This commit adds the narrowly pinned command `acknowledge-pre-shutdown-recovery`. It accepts only the exact journal SHA above and rechecks the current release pointer/marker, production DB SHA and isolated SQLite snapshot, registered backup SHA and snapshot, historical preflight SHA, all three systemd states, and zero Armory processes. It also binds SQLite WAL/SHM/journal sidecar identities across the repeated snapshots. Any mismatch stops before removing the active journal name. On success, it hard-links the exact journal bytes to `deployment-state-<sha>.json`, writes a schema 3 acknowledgment with fresh evidence and administrator approval, then removes only the active journal name; the original journal remains byte-identical in the archive. Both `recovery_audit.py` and startup preflight verify the archive/ack pair without a deployment manifest. The database and backup are read-only.

After the corrected candidate and root-owned admin tool have been staged and independently hash-checked, the administrator recovery invocation is:

```sh
sudo /opt/wow-forever-armory-bot/bin/production_admin.py acknowledge-pre-shutdown-recovery \
  --journal-sha256 b957b1735570ad821ddbc5f4f8bcaa82ca635e1393394a439879bef531a7f4ca \
  --approved-by nolo-admin \
  --approval-reference TASK-301
```

Do not run this command as part of this task turn. After it succeeds, independently run the recovery audit/preflight before staging the deployment pin for the corrected release commit. Then run the production wrapper once, check the new backup and manifest, finalize and activate with the administrative workflow, and verify one bot process, active/enabled units, Discord connection, command response, retained registered characters, and an Armory refresh. Do not restore the backup: both current database and registered backup have matching counts and `ok` integrity. A restore would be unnecessary and destructive.

**Recommendation: GO to stage and review the hash-pinned recovery command; GO to execute its acknowledgment only after the administrator verifies the staged source hashes and records the approval. NO-GO for deployment and service activation until recovery succeeds and startup preflight passes.** No privileged production command was run in this task turn.

## Corrected candidate source hashes

| File | SHA-256 |
| --- | --- |
| `deploy/production_admin.py` | `99ffe29ed335494e8277e5d18f74adff6afb2c4ec32effacdd0e41cd8dd7dafe` |
| `deploy/production_wrapper.py` | `f8a67bb81e88883c2bc989553720ad5c678083fb6e4378e641669f3aee70672c` |
| `deploy/production_preflight.py` | `4b6b664e5c19e3ce1284f722713618ca38de8e24fce431e697a6036492e33ea4` |
| `deploy/recovery_audit.py` | `02465785fa96cf13fce668e8df59d35dfa0bd2d7eb227153ff74f6b7697de5fc` |
| `deploy/systemd/armory-db-preflight.py` | `3ab1bf66ba00aee7e269dc1dea5a799fa886d49f1ebb09f56fa99cc3beea6da6` |
| `deploy/production_db_snapshot.py` | `564fb40f430c9f96434ab03a3e008a631a7cb7e556f1f63b16f4e9357c2111c8` |
| `deploy/task301_readonly_diagnostics.py` | `da23624be35d1353f50a2efdff03e3c464569db84de6608ecc0fe0c3841d654d` |

