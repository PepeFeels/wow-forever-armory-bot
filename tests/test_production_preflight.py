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

EXPECTED_SHA = "d" * 40


class ProductionPreflightTests(unittest.TestCase):
    def _release_tree(self, root: Path, system_python: Path) -> Path:
        release = root / "release"
        bin_dir = release / ".venv/bin"
        bin_dir.mkdir(parents=True)
        (release / "app.py").write_text("pass\n", encoding="ascii")
        (release / "app.py").chmod(0o644)
        system_python.write_text("#!/bin/sh\n", encoding="ascii")
        system_python.chmod(0o755)
        (bin_dir / "python").symlink_to(system_python)
        return release

    def _release_is_safe(self, release: Path, system_python: Path) -> bool:
        with (
            patch.object(production_preflight, "SYSTEM_PYTHON", str(system_python)),
            patch.object(
                production_preflight.Preflight,
                "_trusted_directory_chain",
                return_value=True,
            ),
        ):
            return production_preflight.Preflight._release_tree_is_safe(
                release,
                os.getuid(),
                os.getgid(),
                owner=os.getuid(),
            )

    def test_release_accepts_venv_interpreter_link_to_system_python(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system_python = root / "system-python"
            release = self._release_tree(root, system_python)
            self.assertTrue(self._release_is_safe(release, system_python))

    def test_release_rejects_symlink_escaping_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system_python = root / "system-python"
            release = self._release_tree(root, system_python)
            outside = root / "outside-data"
            outside.write_text("private\n", encoding="ascii")
            outside.chmod(0o644)
            (release / "unexpected-link").symlink_to(outside)
            self.assertFalse(self._release_is_safe(release, system_python))

    def test_release_rejects_broken_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system_python = root / "system-python"
            release = self._release_tree(root, system_python)
            (release / "broken-link").symlink_to(root / "missing")
            self.assertFalse(self._release_is_safe(release, system_python))

    def test_release_rejects_unexpected_file_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system_python = root / "system-python"
            release = self._release_tree(root, system_python)
            self.assertFalse(
                production_preflight.Preflight._release_tree_is_safe(
                    release,
                    os.getuid(),
                    os.getgid(),
                    owner=os.getuid() + 1,
                )
            )

    def test_release_rejects_unsafe_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system_python = root / "system-python"
            release = self._release_tree(root, system_python)
            (release / "app.py").chmod(0o666)
            self.assertFalse(self._release_is_safe(release, system_python))

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
            checker = production_preflight.Preflight(Path(temporary), EXPECTED_SHA)
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
            checker = production_preflight.Preflight(Path(temporary), EXPECTED_SHA)
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
        checker = production_preflight.Preflight(Path("/unused"), EXPECTED_SHA)
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

    def test_database_check_rejects_unknown_check_name(self) -> None:
        checker = production_preflight.Preflight(Path("/unused"), EXPECTED_SHA)
        status, detail = checker.database(Path("/unused.sqlite3"), "typo")
        self.assertEqual(status, "UNKNOWN")
        self.assertIn("identifier", detail)

    def test_corrupt_database_is_reported_as_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "corrupt.sqlite3"
            database.write_bytes(b"not a sqlite database")
            database.chmod(0o600)
            checker = production_preflight.Preflight(Path(temporary), EXPECTED_SHA)
            checker.service_uid = os.getuid()
            checker.service_gid = os.getgid()
            checker.unit_states_checked = True
            checker.unit_states = {
                production_preflight.SERVICE: ("inactive", "disabled")
            }
            status, _detail = checker.database(database, "production_database")
            self.assertEqual(status, "FAIL")

    def test_root_sqlite_worker_drops_to_the_service_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "database.sqlite3"
            self._database(database)
            checker = production_preflight.Preflight(Path(temporary), EXPECTED_SHA)
            checker.service_uid = os.getuid()
            checker.service_gid = os.getgid()
            checker.unit_states_checked = True
            checker.unit_states = {
                production_preflight.SERVICE: ("inactive", "disabled")
            }
            process_result = subprocess.CompletedProcess([], 0, "PASS", "")
            with (
                patch.object(os, "geteuid", return_value=0),
                patch.object(
                    subprocess,
                    "run",
                    return_value=process_result,
                ) as run,
                patch.object(
                    checker,
                    "_run",
                    return_value=subprocess.CompletedProcess([], 3, "inactive", ""),
                ),
            ):
                status, _detail = checker.database(database, "production_database")
            self.assertEqual(status, "PASS")
            kwargs = run.call_args.kwargs
            self.assertEqual(kwargs["user"], os.getuid())
            self.assertEqual(kwargs["group"], os.getgid())
            self.assertEqual(kwargs["extra_groups"], ())
            self.assertTrue(kwargs["pass_fds"])
            self.assertIn("immutable=1", run.call_args.args[0][4])

    def test_root_sqlite_check_rejects_database_path_replacement_race(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "database.sqlite3"
            self._database(database)
            checker = production_preflight.Preflight(root, EXPECTED_SHA)
            checker.service_uid = os.getuid()
            checker.service_gid = os.getgid()
            checker.unit_states_checked = True
            checker.unit_states = {
                production_preflight.SERVICE: ("inactive", "disabled")
            }
            old_database = root / "replaced.sqlite3"

            def replace_path(
                *_args: object, **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                database.rename(old_database)
                self._database(database)
                return subprocess.CompletedProcess([], 0, "PASS", "")

            with (
                patch.object(os, "geteuid", return_value=0),
                patch.object(subprocess, "run", side_effect=replace_path),
                patch.object(
                    checker,
                    "_run",
                    return_value=subprocess.CompletedProcess([], 3, "inactive", ""),
                ),
            ):
                status, detail = checker.database(database, "production_database")
            self.assertEqual(status, "UNKNOWN")
            self.assertIn("changed", detail)

    def test_root_sqlite_check_rechecks_writer_state_after_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "database.sqlite3"
            self._database(database)
            checker = production_preflight.Preflight(root, EXPECTED_SHA)
            checker.service_uid = os.getuid()
            checker.service_gid = os.getgid()
            checker.unit_states_checked = True
            checker.unit_states = {
                production_preflight.SERVICE: ("inactive", "disabled")
            }
            with (
                patch.object(os, "geteuid", return_value=0),
                patch.object(
                    subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, "PASS", ""),
                ),
                patch.object(
                    checker,
                    "_run",
                    return_value=subprocess.CompletedProcess([], 0, "active", ""),
                ),
            ):
                status, detail = checker.database(database, "production_database")
            self.assertEqual(status, "UNKNOWN")
            self.assertIn("writer state changed", detail)

    def test_complete_backup_check_path_validates_backup_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            backup_dir = state / "backups"
            backup_dir.mkdir(parents=True, mode=0o700)
            database = state / "guild-armory.sqlite3"
            backup = backup_dir / "guild-armory-20261114.sqlite3"
            self._database(database)
            self._database(backup)
            audit = root / "audit"
            audit.mkdir(mode=0o700)
            before = {
                path.name: (
                    path.read_bytes(),
                    path.stat().st_mode,
                    path.stat().st_size,
                    path.stat().st_mtime_ns,
                    path.stat().st_ctime_ns,
                )
                for path in root.rglob("*")
                if path.is_file()
            }
            checker = production_preflight.Preflight(root, EXPECTED_SHA)
            checker.service_uid = os.getuid()
            checker.service_gid = os.getgid()
            checker.unit_states_checked = True
            checker.unit_states = {
                production_preflight.SERVICE: ("active", "enabled"),
                production_preflight.BACKUP_SERVICE: ("inactive", "static"),
            }
            with (
                patch.object(production_preflight, "STATE", state),
                patch.object(production_preflight, "AUDIT", audit),
            ):
                checker.database_checks()
            after = {
                path.name: (
                    path.read_bytes(),
                    path.stat().st_mode,
                    path.stat().st_size,
                    path.stat().st_mtime_ns,
                    path.stat().st_ctime_ns,
                )
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(checker.checks["production_database"].status, "UNKNOWN")
            self.assertEqual(checker.checks["latest_backup"].status, "PASS")
            self.assertEqual(after, before)
            self.assertFalse(Path(str(backup) + "-wal").exists())
            self.assertFalse(Path(str(backup) + "-shm").exists())

    def test_missing_v2_pin_and_target_are_expected_unknowns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checker = production_preflight.Preflight(Path(temporary), EXPECTED_SHA)
            with patch.object(
                production_preflight, "PIN", Path(temporary) / "missing-pin"
            ):
                checker.deployment_pin()
            checker.installed_artifacts()
            self.assertEqual(checker.checks["deployment_pin"].status, "UNKNOWN")
            self.assertIn(
                "not been provisioned", checker.checks["deployment_pin"].detail
            )
            self.assertEqual(checker.checks["v2_target_release"].status, "UNKNOWN")

    def test_missing_production_checkout_is_unknown_not_current_health_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "not-provisioned"
            checker = production_preflight.Preflight(Path(temporary), EXPECTED_SHA)
            checker.checkout(path)
            result = checker.checks["production_checkout"]
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.group, "v2.0.0 deployment prerequisites")

    def test_clean_old_production_checkout_is_unknown_without_v2_pin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            old_sha = "a" * 40
            checker = production_preflight.Preflight(path, EXPECTED_SHA)

            def fake_git(_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
                if args == ("rev-parse", "--show-toplevel"):
                    output = str(path)
                elif args == ("branch", "--show-current"):
                    output = "release/v1.1.4"
                elif args == ("rev-parse", "HEAD"):
                    output = old_sha
                elif args == ("rev-parse", "refs/heads/release/v1.1.4"):
                    output = old_sha
                else:
                    output = ""
                return subprocess.CompletedProcess(args, 0, output, "")

            with (
                patch.object(checker, "_trusted_checkout_tree", return_value=True),
                patch.object(checker, "_git", side_effect=fake_git),
            ):
                checker.checkout(path)
            result = checker.checks["production_checkout"]
            self.assertEqual(result.status, "UNKNOWN")
            self.assertIn("pin is not provisioned", result.detail)

    def test_candidate_git_requires_exact_expected_commit_sha(self) -> None:
        checker = production_preflight.Preflight(
            Path("/trusted/candidate"), EXPECTED_SHA
        )

        def fake_git(_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
            if args == ("branch", "--show-current"):
                output = production_preflight.BRANCH
            elif args == ("rev-parse", "HEAD"):
                output = EXPECTED_SHA
            elif args == ("rev-parse", f"refs/heads/{production_preflight.BRANCH}"):
                output = EXPECTED_SHA
            else:
                output = ""
            return subprocess.CompletedProcess(args, 0, output, "")

        with (
            patch.object(checker, "_trusted_checkout_tree", return_value=True),
            patch.object(checker, "_git", side_effect=fake_git),
        ):
            checker.candidate_git()
        self.assertEqual(checker.checks["candidate_git"].status, "PASS")

        wrong = production_preflight.Preflight(Path("/trusted/candidate"), "e" * 40)
        with (
            patch.object(wrong, "_trusted_checkout_tree", return_value=True),
            patch.object(wrong, "_git", side_effect=fake_git),
        ):
            wrong.candidate_git()
        self.assertEqual(wrong.checks["candidate_git"].status, "FAIL")

    def test_mutable_candidate_path_is_rejected_before_git_subprocess(self) -> None:
        checker = production_preflight.Preflight(
            Path("/developer/checkout"), EXPECTED_SHA
        )
        with (
            patch.object(checker, "_trusted_checkout_tree", return_value=False),
            patch.object(checker, "_git") as git,
        ):
            checker.candidate_git()
        self.assertEqual(checker.checks["candidate_git"].status, "FAIL")
        git.assert_not_called()

    def test_checkout_tree_rejects_symbolic_link_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "checkout"
            (checkout / ".git").mkdir(parents=True)
            alias = root / "checkout-alias"
            alias.symlink_to(checkout, target_is_directory=True)
            self.assertFalse(
                production_preflight.Preflight._trusted_checkout_tree(alias)
            )

    def test_candidate_git_filter_configuration_is_rejected(self) -> None:
        checker = production_preflight.Preflight(
            Path("/trusted/candidate"), EXPECTED_SHA
        )

        def fake_git(_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
            if args and args[0] == "branch":
                output = production_preflight.BRANCH
            elif args[:2] == ("rev-parse", "HEAD"):
                output = EXPECTED_SHA
            elif args[:2] == ("rev-parse", f"refs/heads/{production_preflight.BRANCH}"):
                output = EXPECTED_SHA
            elif args[:2] == ("config", "--local"):
                output = "filter.untrusted.clean"
            else:
                output = ""
            return subprocess.CompletedProcess(args, 0, output, "")

        with (
            patch.object(checker, "_trusted_checkout_tree", return_value=True),
            patch.object(checker, "_git", side_effect=fake_git),
        ):
            checker.candidate_git()
        self.assertEqual(checker.checks["candidate_git"].status, "FAIL")
        self.assertIn("filter", checker.checks["candidate_git"].detail)

    def test_root_source_gate_rejects_developer_owned_script_path(self) -> None:
        with patch.object(os, "geteuid", return_value=0):
            self.assertFalse(production_preflight.Preflight._trusted_execution_source())

    def test_run_orchestrates_all_checks_and_separates_result_groups(self) -> None:
        from contextlib import ExitStack

        checker = production_preflight.Preflight(
            Path("/trusted/candidate"), EXPECTED_SHA
        )

        def add(name: str, status: str) -> None:
            checker.add(name, status, "test result")

        def runtime() -> None:
            add("python_and_architecture", "PASS")
            add("service_account_and_group", "PASS")

        def checkout(_path: Path, *, candidate: bool = False) -> tuple[str, bool]:
            name = "candidate_checkout" if candidate else "production_checkout"
            add(name, "PASS" if candidate else "UNKNOWN")
            return EXPECTED_SHA, candidate

        def release_pointer() -> None:
            add("current_release", "PASS")
            add("installed_release_permissions", "PASS")

        def files_and_hashes() -> None:
            add("protected_files", "PASS")
            add("installed_helper_hashes", "UNKNOWN")

        def database_checks() -> None:
            add("production_database", "UNKNOWN")
            add("latest_backup", "PASS")

        def incident_journal() -> None:
            add("deployment_recovery_ack", "PASS")
            add("failed_v1_1_4_manifest", "UNKNOWN")

        stages = {
            "candidate_git": lambda: add("candidate_git", "PASS"),
            "runtime": runtime,
            "deployment_pin": lambda: add("deployment_pin", "UNKNOWN"),
            "checkout": checkout,
            "systemd": lambda: add("systemd_states", "PASS"),
            "release_pointer": release_pointer,
            "files_and_hashes": files_and_hashes,
            "installed_artifacts": lambda: add("v2_target_release", "UNKNOWN"),
            "database_checks": database_checks,
            "incident_journal": incident_journal,
        }
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(checker, "_trusted_execution_source", return_value=True)
            )
            for name, callback in stages.items():
                stack.enter_context(patch.object(checker, name, side_effect=callback))
            result = checker.run()

        self.assertEqual(len(result), 17)
        self.assertEqual(
            result["deployment_pin"].group,
            "v2.0.0 deployment prerequisites",
        )
        self.assertEqual(
            result["systemd_states"].group,
            "Current production health",
        )
        self.assertEqual(result["preflight_execution_source"].status, "PASS")

    def test_untrusted_run_returns_unknown_without_launching_subprocesses(self) -> None:
        checker = production_preflight.Preflight(
            Path("/developer/checkout"), EXPECTED_SHA
        )
        with (
            patch.object(checker, "_trusted_execution_source", return_value=False),
            patch.object(checker, "_run") as run,
        ):
            result = checker.run()
        run.assert_not_called()
        self.assertEqual(result["preflight_execution_source"].status, "UNKNOWN")
        self.assertEqual(result["deployment_pin"].status, "UNKNOWN")
        self.assertEqual(result["systemd_states"].status, "UNKNOWN")

    def test_database_calls_supply_both_path_and_check_identifier(self) -> None:
        tree = ast.parse(
            Path(production_preflight.__file__).read_text(encoding="utf-8")
        )
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "database"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
        ]
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(all(len(call.args) == 2 for call in calls))

    def test_existing_target_release_is_reported_without_modification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary) / "opt/wow-forever-armory-bot"
            target_sha = "a" * 40
            target = base / "releases" / target_sha
            target.mkdir(parents=True)
            target_file = target / "existing-data"
            target_file.write_text("preserve", encoding="ascii")
            checker = production_preflight.Preflight(Path(temporary), EXPECTED_SHA)
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
            checker = production_preflight.Preflight(Path(temporary), EXPECTED_SHA)
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
        checker = production_preflight.Preflight(Path("/unused"), EXPECTED_SHA)
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
