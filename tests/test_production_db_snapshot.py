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


def create_database(
    path: Path,
    *,
    guild_count: int = 1,
    member_count: int = 1,
    character_count: int = 3,
) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            "CREATE TABLE guilds (id INTEGER);"
            "CREATE TABLE members (id INTEGER);"
            "CREATE TABLE characters (id INTEGER);"
            "CREATE TABLE flows (id INTEGER);"
            "INSERT INTO flows VALUES (1);"
        )
        connection.executemany(
            "INSERT INTO guilds VALUES (?)",
            ((number,) for number in range(guild_count)),
        )
        connection.executemany(
            "INSERT INTO members VALUES (?)",
            ((number,) for number in range(member_count)),
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

    def verify(
        self, database: Path, *, expected_sha256: str | None = None
    ) -> tuple[int, int, int]:
        return snapshot.verify_database_snapshot(
            database,
            expected_owner=os.getuid(),
            expected_group=os.getgid(),
            expected_sha256=expected_sha256,
        )

    def test_missing_backup_fails_without_creating_a_snapshot(self) -> None:
        missing = self.root / "missing-backup.sqlite3"
        with patch.object(snapshot, "SNAPSHOT_ROOT", self.snapshot_root):
            with self.assertRaises(snapshot.SnapshotVerificationError):
                self.verify(missing)
        self.assertFalse(missing.exists())
        self.assertEqual(list(self.snapshot_root.iterdir()), [])

    def test_expected_copy_hash_is_enforced_before_sqlite_open(self) -> None:
        database = self.root / "armory.sqlite3"
        create_database(database)
        with (
            patch.object(snapshot, "SNAPSHOT_ROOT", self.snapshot_root),
            patch.object(snapshot.sqlite3, "connect") as connect,
        ):
            with self.assertRaisesRegex(
                snapshot.SnapshotVerificationError, "copy hash differs"
            ):
                self.verify(database, expected_sha256="0" * 64)
        connect.assert_not_called()
        self.assertEqual(list(self.snapshot_root.iterdir()), [])

    def test_expected_copy_hash_matches_verified_snapshot(self) -> None:
        database = self.root / "armory.sqlite3"
        create_database(database)
        original = sha256(database)
        with patch.object(snapshot, "SNAPSHOT_ROOT", self.snapshot_root):
            self.assertEqual(self.verify(database, expected_sha256=original), (1, 1, 3))
        self.assertEqual(sha256(database), original)
        self.assertEqual(list(self.snapshot_root.iterdir()), [])

    def test_empty_guild_member_and_character_state_is_accepted(self) -> None:
        database = self.root / "empty-guild-armory.sqlite3"
        create_database(database, guild_count=0, member_count=0, character_count=0)
        with patch.object(snapshot, "SNAPSHOT_ROOT", self.snapshot_root):
            self.assertEqual(self.verify(database), (0, 0, 0))

    def test_one_or_more_guilds_are_accepted_with_aggregate_counts(self) -> None:
        database = self.root / "multiple-guild-armory.sqlite3"
        create_database(database, guild_count=2, member_count=3, character_count=5)
        with patch.object(snapshot, "SNAPSHOT_ROOT", self.snapshot_root):
            self.assertEqual(self.verify(database), (2, 3, 5))

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

    def test_source_file_descriptors_require_no_atime_and_no_follow(self) -> None:
        database = self.root / "armory.sqlite3"
        create_database(database)
        self.assertTrue(hasattr(os, "O_NOATIME"))
        with patch.object(snapshot.os, "open", wraps=os.open) as open_file:
            descriptor = snapshot._open_readonly(database)
            os.close(descriptor)
        flags = open_file.call_args.args[1]
        self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
        self.assertTrue(flags & os.O_NOFOLLOW)
        self.assertTrue(flags & os.O_NOATIME)

    def test_snapshot_refuses_platform_without_no_atime_support(self) -> None:
        database = self.root / "armory.sqlite3"
        create_database(database)
        with patch.object(snapshot.os, "O_NOATIME", None):
            with self.assertRaisesRegex(snapshot.SnapshotVerificationError, "no-atime"):
                snapshot._open_readonly(database)

    def test_no_atime_open_error_fails_without_retrying_without_flag(self) -> None:
        database = self.root / "armory.sqlite3"
        create_database(database)
        real_open = os.open
        calls = []

        def deny_noatime(path, flags, *args, **kwargs):
            calls.append((path, flags))
            if flags & os.O_NOATIME:
                raise PermissionError("synthetic O_NOATIME denial")
            return real_open(path, flags, *args, **kwargs)

        with patch.object(snapshot.os, "open", side_effect=deny_noatime):
            with self.assertRaises(PermissionError):
                snapshot._open_readonly(database)
        file_calls = [call for call in calls if call[0] == database.name]
        self.assertEqual(len(file_calls), 1)
        self.assertTrue(file_calls[0][1] & os.O_NOATIME)

    def test_snapshot_preserves_source_atime_when_filesystem_supports_noatime(
        self,
    ) -> None:
        database = self.root / "armory.sqlite3"
        create_database(database)
        old_atime_ns = 1_000_000_000
        os.utime(database, ns=(old_atime_ns, database.stat().st_mtime_ns))
        before = database.stat()
        with patch.object(snapshot, "SNAPSHOT_ROOT", self.snapshot_root):
            self.assertEqual(self.verify(database), (1, 1, 3))
        after = database.stat()
        self.assertEqual(after.st_atime_ns, before.st_atime_ns)

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

    def test_source_change_during_integrity_check_is_detected(self) -> None:
        database = self.root / "armory.sqlite3"
        create_database(database)
        original_hash = sha256(database)
        original_verify = snapshot._verify_snapshot

        def verify_then_change(copy: Path) -> tuple[int, int, int]:
            result = original_verify(copy)
            database.write_bytes(database.read_bytes() + b"external-change")
            return result

        with (
            patch.object(snapshot, "SNAPSHOT_ROOT", self.snapshot_root),
            patch.object(snapshot, "_verify_snapshot", side_effect=verify_then_change),
        ):
            with self.assertRaisesRegex(
                snapshot.SnapshotVerificationError,
                "changed during isolated integrity check",
            ):
                self.verify(database)
        self.assertNotEqual(sha256(database), original_hash)
        self.assertEqual(list(self.snapshot_root.iterdir()), [])

    def test_interrupted_copy_cleans_snapshot_and_preserves_source(self) -> None:
        database = self.root / "armory.sqlite3"
        create_database(database)
        before_hash = sha256(database)
        before_metadata = database.stat()
        with (
            patch.object(snapshot, "SNAPSHOT_ROOT", self.snapshot_root),
            patch.object(
                snapshot,
                "_copy_readonly_source",
                side_effect=OSError("synthetic interrupted copy"),
            ),
        ):
            with self.assertRaises(snapshot.SnapshotVerificationError):
                self.verify(database)
        after_metadata = database.stat()
        self.assertEqual(sha256(database), before_hash)
        self.assertEqual(
            (
                after_metadata.st_size,
                after_metadata.st_mtime_ns,
                after_metadata.st_mode,
            ),
            (
                before_metadata.st_size,
                before_metadata.st_mtime_ns,
                before_metadata.st_mode,
            ),
        )
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

    def test_symlinked_parent_directory_is_rejected(self) -> None:
        database = self.root / "armory.sqlite3"
        create_database(database)
        linked_parent = self.root / "linked-parent"
        linked_parent.symlink_to(self.root, target_is_directory=True)
        with patch.object(snapshot, "SNAPSHOT_ROOT", self.snapshot_root):
            with self.assertRaises(snapshot.SnapshotVerificationError):
                self.verify(linked_parent / database.name)
        self.assertEqual(list(self.snapshot_root.iterdir()), [])

    def test_database_and_wal_hardlinks_are_rejected(self) -> None:
        database = self.root / "armory.sqlite3"
        create_database(database)
        alias = self.root / "database-alias.sqlite3"
        os.link(database, alias)
        with patch.object(snapshot, "SNAPSHOT_ROOT", self.snapshot_root):
            with self.assertRaisesRegex(
                snapshot.SnapshotVerificationError, "unsafe hardlink"
            ):
                self.verify(database)
        alias.unlink()

        wal = Path(f"{database}-wal")
        wal.write_bytes(b"")
        wal.chmod(0o600)
        wal_alias = self.root / "wal-alias"
        os.link(wal, wal_alias)
        with patch.object(snapshot, "SNAPSHOT_ROOT", self.snapshot_root):
            with self.assertRaisesRegex(
                snapshot.SnapshotVerificationError, "unsafe hardlink"
            ):
                self.verify(database)
        self.assertEqual(list(self.snapshot_root.iterdir()), [])

    def test_incorrect_database_owner_is_rejected(self) -> None:
        database = self.root / "armory.sqlite3"
        create_database(database)
        with patch.object(snapshot, "SNAPSHOT_ROOT", self.snapshot_root):
            with self.assertRaisesRegex(
                snapshot.SnapshotVerificationError, "metadata is unsafe"
            ):
                snapshot.verify_database_snapshot(
                    database,
                    expected_owner=os.getuid() + 1,
                    expected_group=os.getgid(),
                )
        self.assertEqual(list(self.snapshot_root.iterdir()), [])

    def test_missing_required_tables_and_corrupt_sqlite_are_rejected(self) -> None:
        missing_tables = self.root / "missing-tables.sqlite3"
        with sqlite3.connect(missing_tables) as connection:
            connection.execute("CREATE TABLE guilds (id INTEGER)")
        missing_tables.chmod(0o600)

        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_bytes(b"not a SQLite database")
        corrupt.chmod(0o600)

        with patch.object(snapshot, "SNAPSHOT_ROOT", self.snapshot_root):
            with self.subTest(database="missing tables"):
                with self.assertRaisesRegex(
                    snapshot.SnapshotVerificationError, "snapshot schema is incomplete"
                ):
                    self.verify(missing_tables)
            with self.subTest(database="corrupt"):
                with self.assertRaises(snapshot.SnapshotVerificationError):
                    self.verify(corrupt)
        self.assertEqual(list(self.snapshot_root.iterdir()), [])

    def test_inconsistent_shm_sidecar_is_rejected(self) -> None:
        database = self.root / "armory.sqlite3"
        create_database(database)
        shm = Path(f"{database}-shm")
        shm.write_bytes(b"bad")
        shm.chmod(0o600)
        with patch.object(snapshot, "SNAPSHOT_ROOT", self.snapshot_root):
            with self.assertRaisesRegex(
                snapshot.SnapshotVerificationError, "SHM size is inconsistent"
            ):
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
