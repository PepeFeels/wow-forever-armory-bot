"""Safety checks for the administrator read-only production preflight."""

from __future__ import annotations

import ast
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy import production_preflight


class ProductionPreflightTests(unittest.TestCase):
    def _database(self, path: Path) -> None:
        with sqlite3.connect(path) as connection:
            for table in ("guilds", "members", "characters", "flows"):
                connection.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        path.chmod(0o600)

    def test_database_integrity_uses_read_only_connection_without_new_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "guild-armory.sqlite3"
            self._database(database)
            before = {item.name for item in database.parent.iterdir()}
            checker = production_preflight.Preflight(Path(temporary))
            checker.service_uid = os.getuid()
            checker.service_gid = os.getgid()
            checker.unit_states_checked = True
            checker.unit_states = {
                production_preflight.SERVICE: ("inactive", "disabled")
            }
            status, detail = checker.database(database, "production_database")
            after = {item.name for item in database.parent.iterdir()}
            self.assertEqual(status, "PASS")
            self.assertIn("read-only", detail)
            self.assertEqual(after, before)

    def test_database_with_wal_is_unknown_without_checkpoint_or_sidecar_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "guild-armory.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE guilds (id INTEGER PRIMARY KEY)")
            connection.commit()
            database.chmod(0o600)
            sidecars = [Path(str(database) + suffix) for suffix in ("-wal", "-shm")]
            before = {
                item.name: item.read_bytes() for item in sidecars if item.exists()
            }
            checker = production_preflight.Preflight(Path(temporary))
            checker.service_uid = os.getuid()
            checker.service_gid = os.getgid()
            checker.unit_states_checked = True
            checker.unit_states = {
                production_preflight.SERVICE: ("inactive", "disabled")
            }
            status, detail = checker.database(database, "production_database")
            after = {item.name: item.read_bytes() for item in sidecars if item.exists()}
            connection.close()
            self.assertEqual(status, "UNKNOWN")
            self.assertIn("sidecars", detail)
            self.assertEqual(after, before)

    def test_active_service_makes_live_database_check_unknown(self) -> None:
        checker = production_preflight.Preflight(Path("/unused"))
        checker.service_uid = os.getuid()
        checker.service_gid = os.getgid()
        checker.unit_states_checked = True
        checker.unit_states = {production_preflight.SERVICE: ("active", "enabled")}
        with patch.object(sqlite3, "connect") as connect:
            status, detail = checker.database(
                Path("/not-opened"), "production_database"
            )
        self.assertEqual(status, "UNKNOWN")
        self.assertIn("active writer", detail)
        connect.assert_not_called()

    def test_existing_target_release_is_reported_without_modification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary) / "opt/wow-forever-armory-bot"
            target_sha = "a" * 40
            target = base / "releases" / target_sha
            target.mkdir(parents=True)
            target_file = target / "existing-data"
            target_file.write_text("preserve", encoding="ascii")
            checker = production_preflight.Preflight(Path(temporary))
            checker.pin = {
                "release_sha": target_sha,
                "previous_sha": "b" * 40,
                "installer_sha256": "c" * 64,
            }
            before = target_file.read_bytes()
            with patch.object(production_preflight, "BASE", base):
                checker.installed_artifacts()
            self.assertEqual(checker.checks["v2_target_release"].status, "FAIL")
            self.assertEqual(target_file.read_bytes(), before)
            self.assertFalse((target / ".installation-complete").exists())

    def test_manual_recovery_journal_is_reported_as_pending_without_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary)
            journal = audit / "deployment-state.json"
            journal.write_text("preserved evidence", encoding="ascii")
            checker = production_preflight.Preflight(Path(temporary))
            with (
                patch.object(production_preflight, "AUDIT", audit),
                patch.object(production_preflight.Preflight, "_metadata"),
                patch.object(
                    production_preflight.Preflight,
                    "_secure_json",
                    return_value={"phase": "manual_recovery_required"},
                ),
            ):
                checker.incident_journal()
            self.assertEqual(checker.checks["deployment_recovery_ack"].status, "FAIL")
            self.assertEqual(journal.read_text(encoding="ascii"), "preserved evidence")

    def test_systemd_check_only_uses_observation_commands(self) -> None:
        checker = production_preflight.Preflight(Path("/unused"))
        seen: list[tuple[str, ...]] = []

        def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
            seen.append(tuple(argv))
            state = "active" if argv[1] == "is-active" else "enabled"
            if argv[2] == production_preflight.BACKUP_SERVICE:
                state = "inactive" if argv[1] == "is-active" else "static"
            return subprocess.CompletedProcess(argv, 0, state, "")

        with patch.object(checker, "_run", side_effect=fake_run):
            checker.systemd()
        self.assertEqual(checker.checks["systemd_states"].status, "PASS")
        self.assertTrue(seen)
        self.assertTrue(all(argv[1] in {"is-active", "is-enabled"} for argv in seen))

    def test_source_contains_no_file_or_service_mutation_calls(self) -> None:
        source = Path(production_preflight.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden = {
            "chmod",
            "chown",
            "mkdir",
            "makedirs",
            "replace",
            "rename",
            "unlink",
            "remove",
            "write_text",
            "write_bytes",
            "system",
            "Popen",
        }
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertFalse(forbidden & calls, sorted(forbidden & calls))
        self.assertIn("query_only=ON", source)
        self.assertIn("immutable=1", source)
        self.assertNotIn("os.environ", source)
        self.assertNotIn("os.environb", source)


if __name__ == "__main__":
    unittest.main()
