"""Hash-bound tests for the TASK-301 pre-shutdown recovery path."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from deploy import production_admin as admin
from deploy import production_db_snapshot as snapshot
from deploy import recovery_audit


class Task301PreShutdownRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        snapshot_root_patch = patch.object(snapshot, "SNAPSHOT_ROOT", self.root)
        snapshot_root_patch.start()
        self.addCleanup(snapshot_root_patch.stop)
        self.uid, self.gid = os.getuid(), os.getgid()
        self.audit = self.root / "audit"
        self.audit.mkdir(mode=0o700)
        self.base = self.root / "install"
        self.releases = self.base / "releases"
        self.previous = "dcceeb85464e09e34b112e41b16a833226c72270"
        self.release = "fa15f4e3095fad60ff48a059f7ce07414633d07d"
        previous_dir = self.releases / self.previous
        previous_dir.mkdir(parents=True, mode=0o755)
        marker = previous_dir / ".installation-complete"
        marker.write_text(self.previous + "\n", encoding="ascii")
        marker.chmod(0o444)
        (self.base / "current").symlink_to(f"releases/{self.previous}")
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.database = self.state / "guild-armory.sqlite3"
        self.backup_dir = self.state / "backups" / "active"
        self.backup_dir.mkdir(parents=True, mode=0o700)
        self.backup = self.backup_dir / "guild-armory-20261123T173904.584416Z.sqlite3"
        self._create_database(self.database, user_version=0)
        self._create_database(self.backup, user_version=1)
        self.db_hash = hashlib.sha256(self.database.read_bytes()).hexdigest()
        self.backup_hash = hashlib.sha256(self.backup.read_bytes()).hexdigest()
        self.preflight = self.audit / f"armory-db-preflight.{self.previous}.py"
        self.preflight.write_bytes(b"historical preflight evidence\n")
        self.preflight.chmod(0o444)
        self.preflight_hash = hashlib.sha256(self.preflight.read_bytes()).hexdigest()
        self.journal_path = self.audit / "deployment-state.json"
        journal = {
            # The interrupted deployment already had a deployment ID, but
            # failed before its installation-result file could be published.
            "deployment_id": "4aec20d12ac86e5cc7510aa039e68eed",
            "phase": "manual_recovery_required",
            "interrupted_phase": "backup_verified",
            "approved_release_sha": self.release,
            "previous_release_sha": self.previous,
            "database_backup": str(self.backup),
            "database_backup_sha256": self.backup_hash,
            "database_signature": None,
            "shared_preflight_backup": None,
            "shared_preflight_sha256": None,
            "final_unit_states": {
                admin.SERVICE_NAME: ["inactive", "disabled"],
                admin.TIMER_NAME: ["inactive", "disabled"],
                admin.BACKUP_SERVICE: ["inactive", "static"],
            },
        }
        self.journal_raw = (json.dumps(journal, sort_keys=True) + "\n").encode()
        self.journal_path.write_bytes(self.journal_raw)
        self.journal_path.chmod(0o600)
        self.journal_hash = hashlib.sha256(self.journal_raw).hexdigest()
        self.states = {
            admin.SERVICE_NAME: ["inactive", "disabled"],
            admin.TIMER_NAME: ["inactive", "disabled"],
            admin.BACKUP_SERVICE: ["inactive", "static"],
        }
        self.pin_mocks = (
            patch.object(admin, "TASK301_JOURNAL_SHA256", self.journal_hash),
            patch.object(admin, "TASK301_DATABASE_SHA256", self.db_hash),
            patch.object(admin, "TASK301_BACKUP_SHA256", self.backup_hash),
            patch.object(admin, "TASK301_HISTORICAL_PREFLIGHT_SHA256", self.preflight_hash),
            patch.object(admin, "TASK301_BACKUP", self.backup),
            patch.object(admin, "TASK301_HISTORICAL_PREFLIGHT", self.preflight),
            patch.object(admin, "AUDIT_DIR", self.audit),
            patch.object(admin, "COMPLETION_DATABASE", self.database),
            patch.object(recovery_audit, "TASK301_JOURNAL_SHA256", self.journal_hash),
            patch.object(recovery_audit, "TASK301_DATABASE_SHA256", self.db_hash),
            patch.object(recovery_audit, "TASK301_BACKUP_SHA256", self.backup_hash),
            patch.object(recovery_audit, "TASK301_HISTORICAL_PREFLIGHT_SHA256", self.preflight_hash),
            patch.object(recovery_audit, "TASK301_BACKUP_PATH", self.backup),
            patch.object(recovery_audit, "TASK301_HISTORICAL_PREFLIGHT_PATH", self.preflight),
            patch.object(recovery_audit, "TASK301_RELEASE_SHA", self.release),
            patch.object(recovery_audit, "TASK301_PREVIOUS_SHA", self.previous),
            patch.object(recovery_audit, "TASK301_BASE", self.base),
        )
        for mock in self.pin_mocks:
            mock.start()
            self.addCleanup(mock.stop)

    @staticmethod
    def _create_database(path: Path, *, user_version: int) -> None:
        import sqlite3

        connection = sqlite3.connect(path)
        try:
            connection.executescript(
                "CREATE TABLE guilds (id INTEGER);"
                "CREATE TABLE members (id INTEGER);"
                "CREATE TABLE characters (id INTEGER);"
                "CREATE TABLE flows (id INTEGER);"
                "INSERT INTO guilds VALUES (1);"
                "INSERT INTO members VALUES (1),(2),(3),(4);"
                "INSERT INTO characters VALUES (1),(2),(3),(4);"
                f"PRAGMA user_version={user_version};"
            )
            connection.commit()
        finally:
            connection.close()
        path.chmod(0o600)

    def _run(self, **overrides):
        options = {
            "expected_journal_sha256": self.journal_hash,
            "approved_by": "prod-admin",
            "approval_reference": "CHANGE-301",
            "owner_uid": self.uid,
            "owner_gid": self.gid,
            "base": self.base,
            "database": self.database,
            "backup": self.backup,
            "historical_preflight": self.preflight,
            "service_uid": self.uid,
            "service_gid": self.gid,
            "state_reader": lambda: self.states,
            "process_reader": lambda: 0,
            "snapshot_verifier": snapshot.verify_database_snapshot,
            "history_verifier": lambda path, uid, gid: recovery_audit.verify_audit_directory(
                path, owner=uid, group=gid, acquire_lock=False
            ),
            "clock": lambda: datetime(2026, 11, 23, tzinfo=UTC),
        }
        options.update(overrides)
        return admin.acknowledge_task301_pre_shutdown(self.audit, **options)

    def test_exact_null_field_journal_is_archived_and_acknowledged_without_db_write(self):
        before_db = self.database.read_bytes()
        before_backup = self.backup.read_bytes()
        journal_sha, ack_sha = self._run()
        archive = self.audit / f"deployment-state-{self.journal_hash}.json"
        ack_path = self.audit / f"deployment-recovery-ack-{self.journal_hash}.json"
        self.assertEqual(journal_sha, self.journal_hash)
        self.assertEqual(hashlib.sha256(ack_path.read_bytes()).hexdigest(), ack_sha)
        self.assertEqual(archive.read_bytes(), self.journal_raw)
        self.assertFalse(self.journal_path.exists())
        self.assertEqual(self.database.read_bytes(), before_db)
        self.assertEqual(self.backup.read_bytes(), before_backup)
        self.assertFalse((self.audit / f"deployment-{self.release}.json").exists())
        recovery_audit.verify_audit_directory(
            self.audit, owner=self.uid, group=self.gid, acquire_lock=False
        )

    def test_pre_shutdown_id_without_installation_result_is_not_an_orphan(self):
        self._run()
        self.assertFalse(
            (self.audit / "deployment-installation-4aec20d12ac86e5cc7510aa039e68eed.json").exists()
        )
        recovery_audit.verify_audit_directory(
            self.audit, owner=self.uid, group=self.gid, acquire_lock=False
        )

    def test_pre_shutdown_id_with_contradictory_installation_result_fails_closed(self):
        self._run()
        result = self.audit / "deployment-installation-4aec20d12ac86e5cc7510aa039e68eed.json"
        result.write_text("{}\n", encoding="utf-8")
        result.chmod(0o600)
        with self.assertRaisesRegex(
            recovery_audit.AuditError,
            "pre-shutdown incident has contradictory installation evidence",
        ):
            recovery_audit.verify_audit_directory(
                self.audit, owner=self.uid, group=self.gid, acquire_lock=False
            )

    def test_admin_parser_exposes_the_narrow_hash_bound_recovery_command(self):
        args = admin._parser().parse_args(
            [
                "acknowledge-pre-shutdown-recovery",
                "--journal-sha256",
                self.journal_hash,
                "--approved-by",
                "nolo-admin",
                "--approval-reference",
                "TASK-301",
            ]
        )
        self.assertEqual(args.command, "acknowledge-pre-shutdown-recovery")
        self.assertEqual(args.journal_sha256, self.journal_hash)

    def test_admin_parser_exposes_the_explicit_pin_rotation_command(self):
        args = admin._parser().parse_args(
            [
                "rotate-pin",
                "--expected-current-pin-sha256",
                "0" * 64,
                "--expected-current-release-sha",
                "a" * 40,
                "--expected-previous-sha",
                "b" * 40,
                "--release-sha",
                "c" * 40,
                "--previous-sha",
                "b" * 40,
                "--installer-sha256",
                "d" * 64,
                "--approved-by",
                "release-admin",
                "--approval-reference",
                "TASK-302-DEPLOY",
            ]
        )
        self.assertEqual(args.command, "rotate-pin")

    def test_sqlite_sidecars_are_metadata_bound_and_unsafe_files_fail_closed(self):
        wal = Path(f"{self.database}-wal")
        wal.write_bytes(b"stable sidecar bytes")
        wal.chmod(0o600)
        first = admin._task301_sqlite_sidecars(
            self.database, owner_uid=self.uid, owner_gid=self.gid
        )
        self.assertIsInstance(first["-wal"], list)
        self.assertIsNone(first["-shm"])
        wal.write_bytes(b"changed sidecar bytes")
        wal.chmod(0o600)
        second = admin._task301_sqlite_sidecars(
            self.database, owner_uid=self.uid, owner_gid=self.gid
        )
        self.assertNotEqual(first["-wal"], second["-wal"])
        wal.chmod(0o644)
        with self.assertRaisesRegex(admin.AdminError, "sidecar metadata"):
            admin._task301_sqlite_sidecars(
                self.database, owner_uid=self.uid, owner_gid=self.gid
            )

    def test_retry_after_success_is_idempotent(self):
        first = self._run()
        second = self._run(
            state_reader=lambda: self.fail("fresh checks must not rerun after ack"),
            process_reader=lambda: self.fail("fresh checks must not rerun after ack"),
        )
        self.assertEqual(first[0], second[0])
        self.assertEqual(second[1], first[1])
        self.assertEqual(
            recovery_audit.verify_audit_directory(
                self.audit, owner=self.uid, group=self.gid, acquire_lock=False
            ),
            1,
        )

    def test_stale_database_or_running_process_fails_without_archive(self):
        with self.assertRaises(admin.AdminError):
            self._run(snapshot_verifier=lambda *args, **kwargs: (1, 3, 4))
        self.assertTrue(self.journal_path.exists())
        self.assertFalse((self.audit / f"deployment-state-{self.journal_hash}.json").exists())
        with self.assertRaisesRegex(admin.AdminError, "process is still running"):
            self._run(process_reader=lambda: 1)
        self.assertTrue(self.journal_path.exists())

    def test_stopped_service_state_is_rechecked_and_required(self):
        changed = dict(self.states)
        changed[admin.SERVICE_NAME] = ["active", "enabled"]
        with self.assertRaisesRegex(admin.AdminError, "stopped state"):
            self._run(state_reader=lambda: changed)
        self.assertTrue(self.journal_path.exists())

    def test_wrong_journal_hash_and_backup_bytes_fail_closed(self):
        with self.assertRaises(admin.AdminError):
            self._run(expected_journal_sha256="0" * 64)
        self.backup.write_bytes(self.backup.read_bytes() + b"tampered")
        self.backup.chmod(0o600)
        with self.assertRaisesRegex(admin.AdminError, "backup hash"):
            self._run()
        self.assertTrue(self.journal_path.exists())
        self.assertFalse((self.audit / f"deployment-state-{self.journal_hash}.json").exists())

    def test_interrupted_ack_publication_resumes_from_immutable_archive(self):
        with patch.object(admin, "_write_new_at", side_effect=OSError("simulated")):
            with self.assertRaises(OSError):
                self._run()
        archive = self.audit / f"deployment-state-{self.journal_hash}.json"
        self.assertTrue(self.journal_path.exists())
        self.assertEqual(archive.read_bytes(), self.journal_raw)
        self._run()
        self.assertFalse(self.journal_path.exists())
        recovery_audit.verify_audit_directory(
            self.audit, owner=self.uid, group=self.gid, acquire_lock=False
        )

    def test_tampered_ack_is_rejected_by_audit(self):
        self._run()
        ack = self.audit / f"deployment-recovery-ack-{self.journal_hash}.json"
        value = json.loads(ack.read_text())
        value["evidence"]["database_counts"] = [1, 3, 4]
        ack.write_text(json.dumps(value, sort_keys=True) + "\n")
        ack.chmod(0o600)
        with self.assertRaises(recovery_audit.AuditError):
            recovery_audit.verify_audit_directory(
                self.audit, owner=self.uid, group=self.gid, acquire_lock=False
            )


if __name__ == "__main__":
    unittest.main()
