"""Offline tests for the installer's isolated production DB verification."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy import production_db_snapshot as snapshot


def create_database(path: Path, *, character_count: int = 3) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            "CREATE TABLE guilds (id INTEGER);"
            "CREATE TABLE members (id INTEGER);"
            "CREATE TABLE characters (id INTEGER);"
            "CREATE TABLE flows (id INTEGER);"
            "INSERT INTO guilds VALUES (1);"
            "INSERT INTO members VALUES (1);"
            "INSERT INTO flows VALUES (1);"
        )
        connection.executemany(
            "INSERT INTO characters VALUES (?)",
            ((number,) for number in range(character_count)),
        )
        connection.commit()
    finally:
        connection.close()
    path.chmod(0o600)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ProductionDatabaseSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_context = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_context.name)
        self.snapshot_root = self.root / "snapshots"
        self.snapshot_root.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.temp_context.cleanup()

    def verify(self, database: Path) -> tuple[int, int, int]:
        return snapshot.verify_database_snapshot(
            database,
            expected_owner=os.getuid(),
            expected_group=os.getgid(),
        )

    def test_integrity_and_counts_run_only_on_private_snapshot(self) -> None:
        database = self.root / "armory.sqlite3"
        create_database(database)
        original_bytes = database.read_bytes()
        original_connect = sqlite3.connect
        opened: list[Path] = []

        def connect(database_uri: str, **kwargs):
            snapshot_path = Path(database_uri.split("?", 1)[0].removeprefix("file:"))
            opened.append(snapshot_path)
            self.assertTrue(snapshot_path.is_relative_to(self.snapshot_root))
            self.assertNotEqual(snapshot_path, database)
            return original_connect(database_uri, **kwargs)

        with (
            patch.object(snapshot, "SNAPSHOT_ROOT", self.snapshot_root),
            patch.object(snapshot.sqlite3, "connect", side_effect=connect),
        ):
            self.assertEqual(self.verify(database), (1, 1, 3))

        self.assertEqual(len(opened), 1)
        self.assertEqual(database.read_bytes(), original_bytes)
        self.assertEqual(list(self.snapshot_root.iterdir()), [])

    def test_wal_committed_rows_are_verified_and_original_sidecars_unchanged(
        self,
    ) -> None:
        database = self.root / "armory.sqlite3"
        create_database(database, character_count=2)
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("INSERT INTO characters VALUES (99)")
            connection.commit()
            wal = Path(f"{database}-wal")
            shm = Path(f"{database}-shm")
            self.assertGreater(wal.stat().st_size, 32)
            originals = {path: sha256(path) for path in (database, wal, shm)}
            with patch.object(snapshot, "SNAPSHOT_ROOT", self.snapshot_root):
                self.assertEqual(self.verify(database), (1, 1, 3))
            self.assertEqual(
                {path: sha256(path) for path in (database, wal, shm)}, originals
            )
            self.assertEqual(list(self.snapshot_root.iterdir()), [])
        finally:
            connection.close()

    def test_zero_byte_wal_and_existing_shm_are_safe_snapshot_inputs(self) -> None:
        database = self.root / "armory.sqlite3"
        create_database(database)
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.commit()
            wal = Path(f"{database}-wal")
            shm = Path(f"{database}-shm")
            self.assertTrue(wal.exists())
            self.assertEqual(wal.stat().st_size, 0)
            self.assertTrue(shm.exists())
            originals = {path: sha256(path) for path in (database, wal, shm)}
            with patch.object(snapshot, "SNAPSHOT_ROOT", self.snapshot_root):
                self.assertEqual(self.verify(database), (1, 1, 3))
            self.assertEqual(
                {path: sha256(path) for path in (database, wal, shm)}, originals
            )
        finally:
            connection.close()

    def test_invalid_wal_checksum_fails_before_snapshot_database_open(self) -> None:
        database = self.root / "armory.sqlite3"
        create_database(database, character_count=2)
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("INSERT INTO characters VALUES (99)")
            connection.commit()
            wal = Path(f"{database}-wal")
            with wal.open("r+b") as stream:
                stream.seek(40)
                value = stream.read(1)
                stream.seek(40)
                stream.write(bytes([value[0] ^ 1]))
                stream.flush()
            with (
                patch.object(snapshot, "SNAPSHOT_ROOT", self.snapshot_root),
                patch.object(snapshot.sqlite3, "connect") as connect,
            ):
                with self.assertRaises(snapshot.SnapshotVerificationError):
                    self.verify(database)
            connect.assert_not_called()
            self.assertEqual(list(self.snapshot_root.iterdir()), [])
        finally:
            connection.close()

    def test_source_change_during_snapshot_aborts_and_cleans_private_copy(self) -> None:
        database = self.root / "armory.sqlite3"
        create_database(database)
        original_copy = snapshot._copy_readonly_source
        changed = False

        def copy_then_change(source: Path, target: Path, signature: tuple[int, ...]):
            nonlocal changed
            original_copy(source, target, signature)
            if source == database and not changed:
                database.write_bytes(database.read_bytes() + b"x")
                changed = True

        with (
            patch.object(snapshot, "SNAPSHOT_ROOT", self.snapshot_root),
            patch.object(
                snapshot, "_copy_readonly_source", side_effect=copy_then_change
            ),
            patch.object(snapshot, "_verify_snapshot") as verify_snapshot,
        ):
            with self.assertRaises(snapshot.SnapshotVerificationError):
                self.verify(database)
        verify_snapshot.assert_not_called()
        self.assertEqual(list(self.snapshot_root.iterdir()), [])

    def test_snapshot_cleanup_runs_when_sqlite_check_fails(self) -> None:
        database = self.root / "armory.sqlite3"
        create_database(database)
        with (
            patch.object(snapshot, "SNAPSHOT_ROOT", self.snapshot_root),
            patch.object(
                snapshot,
                "_verify_snapshot",
                side_effect=snapshot.SnapshotVerificationError("synthetic failure"),
            ),
        ):
            with self.assertRaises(snapshot.SnapshotVerificationError):
                self.verify(database)
        self.assertEqual(list(self.snapshot_root.iterdir()), [])

    def test_symlink_and_unsafe_database_permissions_are_rejected(self) -> None:
        database = self.root / "armory.sqlite3"
        create_database(database)
        link = self.root / "linked.sqlite3"
        link.symlink_to(database)
        with patch.object(snapshot, "SNAPSHOT_ROOT", self.snapshot_root):
            with self.assertRaises(snapshot.SnapshotVerificationError):
                self.verify(link)
            database.chmod(0o644)
            with self.assertRaises(snapshot.SnapshotVerificationError):
                self.verify(database)
        self.assertEqual(list(self.snapshot_root.iterdir()), [])

    def test_rollback_journal_fails_closed_without_sqlite_open(self) -> None:
        database = self.root / "armory.sqlite3"
        create_database(database)
        Path(f"{database}-journal").write_bytes(b"synthetic journal")
        with (
            patch.object(snapshot, "SNAPSHOT_ROOT", self.snapshot_root),
            patch.object(snapshot.sqlite3, "connect") as connect,
        ):
            with self.assertRaisesRegex(
                snapshot.SnapshotVerificationError, "rollback journal"
            ):
                self.verify(database)
        connect.assert_not_called()
        self.assertEqual(list(self.snapshot_root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
