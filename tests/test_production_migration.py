"""Temporary-database migration rehearsals with no live process or Discord."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sqlite3
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

from deploy import production_migration as migration
from wow_forever_armory.operations import (
    LockUnavailable,
    exclusive_file_lock,
    restore_marker_path,
    runtime_lock_path,
    verify_database,
)


def _load_preflight():
    script = Path(__file__).parents[1] / "deploy/systemd/armory-db-preflight.py"
    loader = SourceFileLoader("armory_migration_db_preflight", str(script))
    spec = importlib.util.spec_from_loader("armory_migration_db_preflight", loader)
    if spec is None:
        raise RuntimeError("could not import database preflight")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _create_guild_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE guilds (guild_id INTEGER PRIMARY KEY, channel_id INTEGER, "
            "panel_message_id INTEGER);"
            "CREATE TABLE members (guild_id INTEGER, user_id INTEGER, "
            "row_message_id INTEGER, manage_message_id INTEGER);"
            "CREATE TABLE characters (guild_id INTEGER, user_id INTEGER, path TEXT);"
            "CREATE TABLE flows (guild_id INTEGER, user_id INTEGER);"
            "INSERT INTO guilds VALUES (123, 456, 789);"
            "INSERT INTO members VALUES (123, 456, 790, 791);"
            "INSERT INTO characters VALUES (123, 456, 'fixture-path-0');"
            "INSERT INTO characters VALUES (123, 456, 'fixture-path-1');"
            "INSERT INTO characters VALUES (123, 456, 'fixture-path-2');"
        )


def _counts(path: Path) -> tuple[int, int, int]:
    with sqlite3.connect(path) as connection:
        values = connection.execute(
            "SELECT (SELECT count(*) FROM guilds), "
            "(SELECT count(*) FROM members), "
            "(SELECT count(*) FROM characters)"
        ).fetchone()
    return tuple(values)


def _discord_references(path: Path) -> tuple[int, ...]:
    with sqlite3.connect(path) as connection:
        guild = connection.execute(
            "SELECT channel_id, panel_message_id FROM guilds"
        ).fetchone()
        member = connection.execute(
            "SELECT row_message_id, manage_message_id FROM members"
        ).fetchone()
    return (*guild, *member)


class ProductionMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.source = self.root / "legacy.sqlite3"
        self.target = self.root / "state/guild-armory.sqlite3"
        self.backups = self.root / "state/backups/migration"
        _create_guild_database(self.source)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _migrate(self):
        return migration.migrate_database(
            self.source,
            self.source,
            self.target,
            self.backups,
            service_owner=(os.getuid(), os.getgid()),
            proc_root=self.proc,
        )

    def test_complete_migration_retains_source_backup_and_matching_data(self) -> None:
        original_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        original_stat = self.source.stat()
        backup, counts = self._migrate()

        verify_database(self.source)
        verify_database(backup)
        verify_database(self.target)
        self.assertEqual(counts, (1, 1, 3))
        self.assertEqual(_counts(backup), counts)
        self.assertEqual(_counts(self.target), counts)
        self.assertEqual(
            _discord_references(self.target), _discord_references(self.source)
        )
        self.assertEqual(
            hashlib.sha256(self.source.read_bytes()).hexdigest(), original_hash
        )
        self.assertEqual(self.source.stat().st_mode, original_stat.st_mode)
        self.assertTrue(backup.exists())
        self.assertFalse(restore_marker_path(self.target).exists())
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)

    def test_migration_holds_shared_runtime_lock_during_backup_and_import(self) -> None:
        real_create_backup = migration.create_backup
        lock_was_held = False

        def observe_lock(*args, **kwargs):
            nonlocal lock_was_held
            with self.assertRaises(LockUnavailable):
                with exclusive_file_lock(runtime_lock_path(self.target)):
                    self.fail("migration runtime lock was not held")
            lock_was_held = True
            return real_create_backup(*args, **kwargs)

        with patch.object(migration, "create_backup", side_effect=observe_lock):
            self._migrate()
        self.assertTrue(lock_was_held)

    def test_running_bot_blocks_migration_before_backup_or_target_creation(
        self,
    ) -> None:
        process = self.proc / "42"
        process.mkdir()
        (process / "cmdline").write_bytes(
            b"python\0-m\0wow_forever_armory.discord_bot\0"
        )
        with self.assertRaisesRegex(RuntimeError, "still running"):
            self._migrate()
        self.assertFalse(self.target.exists())
        self.assertFalse(self.backups.exists())

    def test_mismatched_source_confirmation_fails_closed(self) -> None:
        wrong_source = self.root / "other.sqlite3"
        _create_guild_database(wrong_source)
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            migration.migrate_database(
                self.source,
                wrong_source,
                self.target,
                self.backups,
                proc_root=self.proc,
            )
        self.assertFalse(self.target.exists())

    def test_legacy_inspection_reports_only_selected_safe_metadata(self) -> None:
        process = self.proc / "43"
        process.mkdir()
        (process / "cmdline").write_bytes(
            b"python\0-m\0wow_forever_armory.discord_bot\0"
        )
        (process / "cwd").symlink_to(self.root, target_is_directory=True)
        (process / "environ").write_bytes(
            b"ARMORY_DATABASE_PATH=legacy.sqlite3\0"
            b"DISCORD_TEST_GUILD_ID=123456789012345678\0"
            b"DISCORD_BOT_TOKEN=synthetic-never-print\0"
        )
        details = migration.legacy_process_details(
            43, self.proc, expected_guild_id="123456789012345678"
        )
        self.assertEqual(details["database_path"], str(self.source.resolve()))
        self.assertTrue(details["guild_setting_present"])
        self.assertTrue(details["guild_matches_expected"])
        self.assertTrue(details["token_variable_present"])
        self.assertNotIn("synthetic-never-print", repr(details))

    def test_existing_target_is_never_overwritten(self) -> None:
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"operator data")
        before = self.target.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            self._migrate()
        self.assertEqual(self.target.read_bytes(), before)
        self.assertFalse(self.backups.exists())

    def test_interrupted_import_keeps_startup_marker_and_blocks_preflight(self) -> None:
        with patch.object(
            migration.shutil,
            "copyfileobj",
            side_effect=OSError("synthetic copy failure"),
        ):
            with self.assertRaises(OSError):
                self._migrate()
        self.assertTrue(restore_marker_path(self.target).exists())
        self.assertFalse(self.target.exists())
        guard = _load_preflight()
        self.assertFalse(guard.database_is_ready(self.target))
        self.assertTrue(self.source.exists())


if __name__ == "__main__":
    unittest.main()
