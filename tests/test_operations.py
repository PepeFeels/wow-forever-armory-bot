"""Offline tests for SQLite backup, restore, and cross-process locks."""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from wow_forever_armory import operations
from wow_forever_armory.operations import (
    LockUnavailable,
    OperationError,
    backup_operation_lock_path,
    create_backup,
    exclusive_file_lock,
    restore_backup,
    restore_marker_path,
    runtime_lock_path,
    verify_database,
)


def _database(path: Path, value: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS sample (value TEXT NOT NULL)")
        connection.execute("DELETE FROM sample")
        connection.execute("INSERT INTO sample VALUES (?)", (value,))


def _value(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return str(connection.execute("SELECT value FROM sample").fetchone()[0])


class OperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "armory.sqlite3"
        self.backups = self.root / "backups"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_online_backup_is_integral_private_and_readable(self) -> None:
        _database(self.database, "before")
        with sqlite3.connect(self.database) as live:
            live.execute("PRAGMA journal_mode=WAL")
            live.execute("INSERT INTO sample VALUES ('while-open')")
            live.commit()
            backup = create_backup(self.database, self.backups)
            self.assertEqual(
                live.execute("SELECT count(*) FROM sample").fetchone()[0], 2
            )
        verify_database(backup)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.backups.stat().st_mode & 0o777, 0o700)
        self.assertEqual(_value(backup), "before")
        with sqlite3.connect(backup) as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM sample").fetchone()[0], 2
            )

    def test_online_backup_remains_consistent_during_concurrent_commits(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("CREATE TABLE payload (value BLOB NOT NULL)")
            connection.executemany(
                "INSERT INTO payload VALUES (?)", [(b"x" * 4096,) for _ in range(4096)]
            )

        ready = threading.Event()
        stop = threading.Event()
        attempt_times: list[float] = []
        commit_times: list[float] = []
        writer_errors: list[Exception] = []

        def write_repeatedly() -> None:
            try:
                with sqlite3.connect(self.database, timeout=10) as writer:
                    writer.execute("PRAGMA journal_mode=WAL")
                    ready.set()
                    for _ in range(100):
                        if stop.is_set():
                            break
                        attempt_times.append(time.monotonic())
                        writer.execute("INSERT INTO payload VALUES (?)", (b"y" * 4096,))
                        writer.commit()
                        commit_times.append(time.monotonic())
                        time.sleep(0.001)
            except Exception as exc:  # captured to assert in the owning thread
                writer_errors.append(exc)
                ready.set()

        writer = threading.Thread(target=write_repeatedly, daemon=True)
        writer.start()
        self.assertTrue(ready.wait(timeout=5))
        start = time.monotonic()
        try:
            backup = create_backup(self.database, self.backups)
        finally:
            end = time.monotonic()
            stop.set()
            writer.join(timeout=5)
        self.assertFalse(writer.is_alive())
        self.assertEqual(writer_errors, [])
        self.assertTrue(any(start <= attempted <= end for attempted in attempt_times))
        verify_database(backup)
        with sqlite3.connect(backup) as connection:
            rows = connection.execute("SELECT count(*) FROM payload").fetchone()[0]
        self.assertGreaterEqual(rows, 4096)
        self.assertLessEqual(rows, 4096 + len(commit_times))

    def test_backup_does_not_create_missing_source_database(self) -> None:
        missing = self.root / "missing.sqlite3"
        with self.assertRaises(OperationError):
            create_backup(missing, self.backups)
        self.assertFalse(missing.exists())
        self.assertEqual(list(self.backups.glob("guild-armory-*.sqlite3")), [])

    def test_backup_cleans_only_orphaned_temporary_files(self) -> None:
        _database(self.database, "saved")
        orphan = self.backups / ".guild-armory-interrupted.sqlite3.abcd.tmp"
        unrelated = self.backups / "operator-note.tmp"
        self.backups.mkdir(mode=0o700)
        orphan.write_bytes(b"partial")
        unrelated.write_bytes(b"keep")
        create_backup(self.database, self.backups)
        self.assertFalse(orphan.exists())
        self.assertTrue(unrelated.exists())

    def test_backup_failure_does_not_publish_partial_file_or_prune(self) -> None:
        _database(self.database, "saved")
        existing = create_backup(self.database, self.backups)
        with patch(
            "wow_forever_armory.operations._backup_to",
            side_effect=OperationError("test failure"),
        ):
            with self.assertRaises(OperationError):
                create_backup(self.database, self.backups, retention=1)
        self.assertTrue(existing.exists())
        self.assertEqual(list(self.backups.glob("guild-armory-*.sqlite3")), [existing])
        self.assertEqual(list(self.backups.glob("*.tmp")), [])

    def test_filesystem_failure_leaves_previous_backup_intact(self) -> None:
        _database(self.database, "saved")
        existing = create_backup(self.database, self.backups)
        with patch(
            "wow_forever_armory.operations.tempfile.mkstemp",
            side_effect=OSError("disk full test"),
        ):
            with self.assertRaises(OperationError):
                create_backup(self.database, self.backups)
        self.assertTrue(existing.exists())
        self.assertEqual(_value(existing), "saved")

    def test_invalid_retention_is_rejected_without_deleting_backups(self) -> None:
        _database(self.database, "saved")
        existing = create_backup(self.database, self.backups)
        with self.assertRaisesRegex(OperationError, "at least one"):
            create_backup(self.database, self.backups, retention=0)
        self.assertTrue(existing.exists())

    def test_invalid_retention_environment_is_rejected_cleanly(self) -> None:
        with patch.dict("os.environ", {"ARMORY_BACKUP_RETENTION": "invalid"}):
            args = operations._parser().parse_args(["backup"])
        self.assertEqual(args.retention, 0)
        with self.assertRaisesRegex(OperationError, "at least one"):
            create_backup(self.database, self.backups, retention=args.retention)

    def test_lock_permission_error_is_reported_without_acquiring_lock(self) -> None:
        with patch(
            "wow_forever_armory.operations.os.open",
            side_effect=PermissionError("synthetic permission failure"),
        ):
            with self.assertRaisesRegex(OperationError, "operational lock"):
                with exclusive_file_lock(self.root / "denied.lock"):
                    self.fail("lock unexpectedly acquired")

    def test_backup_directory_permission_error_preserves_existing_copy(self) -> None:
        _database(self.database, "saved")
        existing = create_backup(self.database, self.backups)
        with patch(
            "wow_forever_armory.operations.os.chmod",
            side_effect=PermissionError("synthetic permission failure"),
        ):
            with self.assertRaises(OperationError):
                create_backup(self.database, self.backups)
        self.assertTrue(existing.exists())
        self.assertEqual(_value(existing), "saved")

    def test_backup_refuses_symlink_directory_without_changing_target(self) -> None:
        _database(self.database, "saved")
        target = self.root / "shared-backups"
        target.mkdir(mode=0o750)
        target.chmod(0o750)
        alias = self.root / "backup-alias"
        alias.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(OperationError, "symbolic link"):
            create_backup(self.database, alias)
        self.assertEqual(target.stat().st_mode & 0o777, 0o750)
        self.assertEqual(list(target.iterdir()), [])

    def test_retention_keeps_latest_verified_backup(self) -> None:
        _database(self.database, "saved")
        first = create_backup(self.database, self.backups)
        second = create_backup(self.database, self.backups)
        third = create_backup(self.database, self.backups, retention=2)
        paths = list(self.backups.glob("guild-armory-*.sqlite3"))
        self.assertEqual(len(paths), 2)
        self.assertIn(second, paths)
        self.assertIn(third, paths)
        self.assertNotIn(first, paths)
        self.assertGreaterEqual(len(paths), 1)

    def test_parallel_backup_job_is_rejected(self) -> None:
        _database(self.database, "saved")
        with exclusive_file_lock(self.backups / ".backup.lock"):
            with self.assertRaises(LockUnavailable):
                create_backup(self.database, self.backups)

    def test_backup_and_restore_share_database_maintenance_lock(self) -> None:
        _database(self.database, "current")
        backup = create_backup(self.database, self.backups)
        with exclusive_file_lock(backup_operation_lock_path(self.database)):
            with self.assertRaises(LockUnavailable):
                create_backup(self.database, self.backups)
            with self.assertRaises(LockUnavailable):
                restore_backup(self.database, backup, confirmed=True)

    def test_symlink_database_paths_share_canonical_locks_and_restore_target(
        self,
    ) -> None:
        _database(self.database, "old")
        alias = self.root / "armory-alias.sqlite3"
        alias.symlink_to(self.database)
        self.assertEqual(runtime_lock_path(alias), runtime_lock_path(self.database))
        self.assertEqual(
            backup_operation_lock_path(alias), backup_operation_lock_path(self.database)
        )
        self.assertEqual(restore_marker_path(alias), restore_marker_path(self.database))
        backup = create_backup(self.database, self.backups)
        _database(self.database, "new")
        restore_backup(alias, backup, confirmed=True)
        self.assertTrue(alias.is_symlink())
        self.assertEqual(_value(self.database), "old")

    def test_restore_requires_explicit_confirmation_and_valid_backup(self) -> None:
        _database(self.database, "current")
        backup = create_backup(self.database, self.backups)
        with self.assertRaisesRegex(OperationError, "explicit replacement"):
            restore_backup(self.database, backup)
        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_bytes(b"not a database")
        with self.assertRaises(OperationError):
            restore_backup(self.database, corrupt, confirmed=True)
        self.assertEqual(_value(self.database), "current")

    def test_restore_replaces_corrupt_database_after_preserving_raw_copy(self) -> None:
        _database(self.database, "known-good")
        backup = create_backup(self.database, self.backups)
        corrupt_bytes = b"corrupt sqlite fixture"
        self.database.write_bytes(corrupt_bytes)

        raw_copy = restore_backup(self.database, backup, confirmed=True)

        self.assertIsNotNone(raw_copy)
        self.assertIn("pre-restore-corrupt", raw_copy.name)
        self.assertEqual(raw_copy.read_bytes(), corrupt_bytes)
        self.assertEqual(raw_copy.stat().st_mode & 0o777, 0o600)
        verify_database(self.database)
        self.assertEqual(_value(self.database), "known-good")

    def test_restore_refuses_when_runtime_lock_is_held(self) -> None:
        _database(self.database, "current")
        backup = create_backup(self.database, self.backups)
        with exclusive_file_lock(runtime_lock_path(self.database)):
            with self.assertRaises(LockUnavailable):
                restore_backup(self.database, backup, confirmed=True)
        self.assertEqual(_value(self.database), "current")

    def test_restore_refuses_when_backup_job_lock_is_held(self) -> None:
        _database(self.database, "current")
        backup = create_backup(self.database, self.backups)
        with exclusive_file_lock(self.backups / ".backup.lock"):
            with self.assertRaises(LockUnavailable):
                restore_backup(self.database, backup, confirmed=True)
        self.assertEqual(_value(self.database), "current")

    def test_restore_preserves_rollback_and_replaces_database_atomically(self) -> None:
        _database(self.database, "old")
        backup = create_backup(self.database, self.backups)
        _database(self.database, "new")
        rollback = restore_backup(self.database, backup, confirmed=True)
        self.assertIsNotNone(rollback)
        self.assertTrue(rollback.is_file())
        self.assertEqual(rollback.stat().st_mode & 0o777, 0o600)
        verify_database(rollback)
        verify_database(self.database)
        self.assertEqual(_value(self.database), "old")
        self.assertEqual(_value(rollback), "new")
        self.assertEqual(self.database.stat().st_mode & 0o777, 0o600)

    def test_rollback_copy_can_be_restored_as_a_verified_sqlite_database(self) -> None:
        _database(self.database, "old")
        backup = create_backup(self.database, self.backups)
        with sqlite3.connect(self.database) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("UPDATE sample SET value='new'")
        rollback = restore_backup(self.database, backup, confirmed=True)
        self.assertIsNotNone(rollback)
        verify_database(rollback)
        self.assertEqual(_value(self.database), "old")
        restore_backup(self.database, rollback, confirmed=True)
        verify_database(self.database)
        self.assertEqual(_value(self.database), "new")

    def test_restore_quarantines_existing_wal_and_shm_files(self) -> None:
        _database(self.database, "old")
        backup = create_backup(self.database, self.backups)
        _database(self.database, "new")
        wal = Path(f"{self.database}-wal")
        shm = Path(f"{self.database}-shm")
        original_backup = operations._backup_to
        calls = 0

        def leave_residual_sidecars(source: Path, destination: Path) -> None:
            nonlocal calls
            original_backup(source, destination)
            calls += 1
            if calls == 1:
                wal.write_bytes(b"residual WAL fixture")
                shm.write_bytes(b"residual SHM fixture")

        with patch(
            "wow_forever_armory.operations._backup_to",
            side_effect=leave_residual_sidecars,
        ):
            restore_backup(self.database, backup, confirmed=True)
        self.assertFalse(wal.exists())
        self.assertFalse(shm.exists())
        preserved = list(self.root.glob("armory.sqlite3.pre-restore-*-wal"))
        preserved += list(self.root.glob("armory.sqlite3.pre-restore-*-shm"))
        self.assertEqual(len(preserved), 2)
        self.assertEqual(_value(self.database), "old")

    def test_restore_failure_before_replace_restores_sidecars_and_database(
        self,
    ) -> None:
        _database(self.database, "old")
        backup = create_backup(self.database, self.backups)
        _database(self.database, "new")
        wal = Path(f"{self.database}-wal")
        shm = Path(f"{self.database}-shm")
        original_backup = operations._backup_to
        calls = 0

        def leave_residual_sidecars(source: Path, destination: Path) -> None:
            nonlocal calls
            original_backup(source, destination)
            calls += 1
            if calls == 1:
                wal.write_bytes(b"residual WAL fixture")
                shm.write_bytes(b"residual SHM fixture")

        original_replace = operations.os.replace

        def fail_database_replace(source: str | Path, destination: str | Path) -> None:
            if Path(destination) == self.database:
                raise OSError("synthetic atomic replace failure")
            original_replace(source, destination)

        with (
            patch(
                "wow_forever_armory.operations._backup_to",
                side_effect=leave_residual_sidecars,
            ),
            patch(
                "wow_forever_armory.operations.os.replace",
                side_effect=fail_database_replace,
            ),
        ):
            with self.assertRaises(OperationError):
                restore_backup(self.database, backup, confirmed=True)

        self.assertTrue(wal.exists())
        self.assertTrue(shm.exists())
        self.assertFalse(restore_marker_path(self.database).exists())
        self.assertEqual(_value(self.database), "new")

    def test_restore_removes_stale_temp_files_only_without_marker(self) -> None:
        _database(self.database, "old")
        backup = create_backup(self.database, self.backups)
        restore_temp = self.root / ".armory.sqlite3.abandoned.restore.tmp"
        rollback_temp = (
            self.root / ".armory.sqlite3.pre-restore-abandoned.sqlite3.x.tmp"
        )
        unrelated = self.root / "operator-note.tmp"
        restore_temp.write_bytes(b"partial")
        rollback_temp.write_bytes(b"partial")
        unrelated.write_bytes(b"keep")

        restore_backup(self.database, backup, confirmed=True)

        self.assertFalse(restore_temp.exists())
        self.assertFalse(rollback_temp.exists())
        self.assertTrue(unrelated.exists())

    def test_failure_after_atomic_replace_leaves_fail_closed_marker(self) -> None:
        _database(self.database, "old")
        backup = create_backup(self.database, self.backups)
        _database(self.database, "new")
        original_fsync = operations._fsync_directory
        calls = 0

        def fail_after_replace(path: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("simulated directory sync failure")
            original_fsync(path)

        with patch(
            "wow_forever_armory.operations._fsync_directory",
            side_effect=fail_after_replace,
        ):
            with self.assertRaises(OperationError):
                restore_backup(self.database, backup, confirmed=True)
        marker = restore_marker_path(self.database)
        self.assertTrue(marker.exists())
        verify_database(self.database)
        self.assertEqual(_value(self.database), "old")
        with self.assertRaisesRegex(OperationError, "interrupted restore"):
            restore_backup(self.database, backup, confirmed=True)

    def test_integrity_check_rejects_missing_and_corrupt_files(self) -> None:
        with self.assertRaises(OperationError):
            verify_database(self.root / "missing.sqlite3")
        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_bytes(b"broken")
        with self.assertRaises(OperationError):
            verify_database(corrupt)


if __name__ == "__main__":
    unittest.main()
