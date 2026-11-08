"""Offline checks for installation gates and the database startup guard."""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from deploy import production_db_snapshot, production_install
from deploy.production_install import (
    InstallError,
    _assert_units_stopped_and_disabled,
    _check_repository,
    _copy_unit,
    _install_current_link,
    _install_release,
    _require_supported_runtime,
    _validate_logo_asset,
    _verify_upgrade_database,
    validate_config,
    validate_inputs,
)


def _load_preflight():
    script = Path(__file__).parents[1] / "deploy/systemd/armory-db-preflight.py"
    loader = SourceFileLoader("armory_db_preflight", str(script))
    spec = importlib.util.spec_from_loader("armory_db_preflight", loader)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not import database preflight")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


preflight = _load_preflight()


def _create_preflight_database(path: Path, *, guild_count: int = 0) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE guilds (guild_id INTEGER PRIMARY KEY);"
            "CREATE TABLE members (guild_id INTEGER, user_id INTEGER);"
            "CREATE TABLE characters (guild_id INTEGER, user_id INTEGER);"
            "CREATE TABLE flows (guild_id INTEGER, user_id INTEGER);"
        )
        connection.executemany(
            "INSERT INTO guilds VALUES (?)",
            ((guild_id,) for guild_id in range(1, guild_count + 1)),
        )
    path.chmod(0o600)


class ProductionInstallTests(unittest.TestCase):
    def test_upgrade_requires_units_confirmed_stopped_and_disabled(self) -> None:
        stopped = SimpleNamespace(returncode=3, stdout="inactive\n", stderr="")
        disabled = SimpleNamespace(returncode=1, stdout="disabled\n", stderr="")
        with patch(
            "deploy.production_install.subprocess.run",
            side_effect=[stopped, stopped, stopped, disabled, disabled],
        ):
            _assert_units_stopped_and_disabled(upgrade_existing=True)

        active = SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        with patch("deploy.production_install.subprocess.run", return_value=active):
            with self.assertRaisesRegex(InstallError, "stopped"):
                _assert_units_stopped_and_disabled(upgrade_existing=True)

        enabled = SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")
        with patch(
            "deploy.production_install.subprocess.run",
            side_effect=[stopped, stopped, stopped, enabled],
        ):
            with self.assertRaisesRegex(InstallError, "enabled"):
                _assert_units_stopped_and_disabled(upgrade_existing=True)

        unavailable = SimpleNamespace(returncode=1, stdout="", stderr="no bus")
        with patch(
            "deploy.production_install.subprocess.run", return_value=unavailable
        ):
            with self.assertRaisesRegex(InstallError, "stopped"):
                _assert_units_stopped_and_disabled(upgrade_existing=True)

    def test_upgrade_path_does_not_invoke_database_migration(self) -> None:
        import inspect

        from deploy.production_install import install

        source = inspect.getsource(install)
        self.assertNotIn("production_migration", source)
        self.assertIn("if not upgrade_existing", source)

    def test_upgrade_uses_snapshot_and_cannot_invoke_installed_db_preflight(
        self,
    ) -> None:
        import ast
        import inspect

        from deploy.production_install import install

        tree = ast.parse(inspect.getsource(install))
        run_calls = [
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_run"
        ]
        self.assertIn("_verify_upgrade_database(", inspect.getsource(install))
        source_calls = {
            name: min(
                node.lineno
                for node in ast.walk(ast.parse(inspect.getsource(install)))
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == name
            )
            for name in (
                "_verify_upgrade_database",
                "_install_release",
                "_install_current_link",
            )
        }
        self.assertLess(
            source_calls["_verify_upgrade_database"],
            source_calls["_install_release"],
        )
        self.assertLess(
            source_calls["_verify_upgrade_database"],
            source_calls["_install_current_link"],
        )
        self.assertFalse(
            any("armory-db-preflight" in call for call in run_calls), run_calls
        )

    def test_upgrade_snapshot_failure_fails_closed(self) -> None:
        with (
            patch("deploy.production_install._assert_no_armory_process") as process,
            patch(
                "deploy.production_install.verify_database_snapshot",
                side_effect=OSError("synthetic snapshot failure"),
            ) as verify,
        ):
            with self.assertRaisesRegex(InstallError, "snapshot verification failed"):
                _verify_upgrade_database(
                    Path("/temporary/database.sqlite3"), uid=10, gid=10
                )
        process.assert_called_once_with()
        verify.assert_called_once_with(
            Path("/temporary/database.sqlite3"), expected_owner=10, expected_group=10
        )

    def test_complete_upgrade_flow_accepts_empty_initialized_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = Path(__file__).parents[1]
            base = root / "opt/wow-forever-armory-bot"
            etc = root / "etc/wow-forever-armory-bot"
            state = root / "var/lib/wow-forever-armory-bot"
            bin_dir = base / "bin"
            releases = base / "releases"
            etc.mkdir(parents=True)
            state.mkdir(parents=True)
            bin_dir.mkdir(parents=True)
            releases.mkdir()

            old_release = releases / ("a" * 40)
            old_release.mkdir()
            (old_release / ".installation-complete").write_text("a" * 40)
            (base / "current").symlink_to(old_release)

            config = etc / "config"
            credential = etc / "discord-token"
            config.write_text("ARMORY_LOG_LEVEL=INFO\n", encoding="utf-8")
            credential.write_text("synthetic-test-credential\n", encoding="utf-8")

            launcher_source = repo / "deploy/systemd/armory-launch"
            preflight_source = repo / "deploy/systemd/armory-db-preflight.py"
            installed_launcher = bin_dir / "armory-launch"
            installed_launcher.write_bytes(launcher_source.read_bytes())
            installed_preflight = bin_dir / "armory-db-preflight"
            installed_preflight.write_bytes(b"previous preflight")
            installed_preflight.chmod(0o755)

            database = state / "guild-armory.sqlite3"
            _create_preflight_database(database, guild_count=0)
            snapshot_root = root / "private-snapshots"
            snapshot_root.mkdir(mode=0o700)
            service_account = SimpleNamespace(
                pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir="/nonexistent"
            )
            service_group = SimpleNamespace(gr_gid=os.getgid())
            release_sha = "b" * 40

            with (
                patch.object(production_install, "BASE", base),
                patch.object(production_install, "ETC", etc),
                patch.object(production_install, "STATE", state),
                patch.object(production_install, "UNIT_FILES", ()),
                patch.object(
                    production_install,
                    "os",
                    SimpleNamespace(geteuid=lambda: 0),
                ),
                patch.object(production_install, "_check_repository"),
                patch.object(production_install, "validate_inputs"),
                patch.object(production_install, "_require_supported_runtime"),
                patch.object(
                    production_install, "_assert_units_stopped_and_disabled"
                ),
                patch.object(
                    production_install.pwd,
                    "getpwnam",
                    return_value=service_account,
                ),
                patch.object(
                    production_install.grp,
                    "getgrnam",
                    return_value=service_group,
                ),
                patch.object(
                    production_install, "_configured_logo_path", return_value=None
                ),
                patch.object(
                    production_install, "_read_regular_file", return_value=b"old"
                ),
                patch.object(production_install, "_mkdir_owned"),
                patch.object(production_install, "_assert_no_armory_process"),
                patch.object(production_install, "_install_release"),
                patch.object(production_install, "_copy_unit") as copy_unit,
                patch.object(
                    production_install, "_install_current_link"
                ) as switch_link,
                patch.object(
                    production_install,
                    "_verify_upgrade_database",
                    wraps=production_install._verify_upgrade_database,
                ) as verify_database,
                patch.object(production_db_snapshot, "SNAPSHOT_ROOT", snapshot_root),
            ):
                production_install.install(
                    repo,
                    config,
                    credential,
                    release_sha,
                    upgrade_existing=True,
                )

            verify_database.assert_called_once_with(
                state / "guild-armory.sqlite3", uid=os.getuid(), gid=os.getgid()
            )
            copy_unit.assert_called_once_with(
                preflight_source, installed_preflight, 0o755
            )
            switch_link.assert_called_once()

    def test_upgrade_process_guard_detects_bot_and_fails_closed_on_proc_error(
        self,
    ) -> None:
        from deploy.production_install import _assert_no_armory_process

        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            bot = proc / "456"
            bot.mkdir()
            (bot / "cmdline").write_bytes(
                b"python\0-m\0wow_forever_armory.discord_bot\0"
            )
            (bot / "environ").write_bytes(b"DISCORD_BOT_TOKEN=synthetic")
            with self.assertRaisesRegex(InstallError, "still running"):
                _assert_no_armory_process(proc)

        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            process = proc / "123"
            process.mkdir()
            with patch.object(Path, "read_bytes", side_effect=PermissionError):
                with self.assertRaisesRegex(InstallError, "verify Armory process"):
                    _assert_no_armory_process(proc)

    def test_upgrade_atomically_switches_managed_release_and_keeps_old_release(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            releases = root / "releases"
            old_sha = "a" * 40
            new_sha = "b" * 40
            old_release = releases / old_sha
            new_release = releases / new_sha
            old_release.mkdir(parents=True)
            new_release.mkdir()
            (old_release / ".installation-complete").write_text(old_sha)
            (new_release / ".installation-complete").write_text(new_sha)
            (root / "current").symlink_to(old_release)
            with (
                patch("deploy.production_install.BASE", root),
                patch("deploy.production_install._run") as run,
            ):
                _install_current_link(
                    new_release, upgrade_existing=True, repo=Path(temporary)
                )
            self.assertEqual((root / "current").resolve(), new_release)
            self.assertTrue(old_release.is_dir())
            run.assert_called_once()

    def test_upgrade_refuses_unapproved_release_switch_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            releases = root / "releases"
            old_release = releases / ("a" * 40)
            new_release = releases / ("b" * 40)
            old_release.mkdir(parents=True)
            new_release.mkdir()
            (old_release / ".installation-complete").write_text("a" * 40)
            (root / "current").symlink_to(old_release)
            with patch("deploy.production_install.BASE", root):
                with self.assertRaisesRegex(InstallError, "refusing to replace"):
                    _install_current_link(new_release)
            self.assertEqual((root / "current").resolve(), old_release)

    def test_installer_requires_exact_clean_release_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            subprocess.run(
                [
                    "git",
                    "init",
                    "--quiet",
                    "--initial-branch=release/v1.1.4",
                    str(repo),
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.name", "Fixture"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "config",
                    "user.email",
                    "fixture@example.invalid",
                ],
                check=True,
            )
            (repo / "fixture.txt").write_text("fixture", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "fixture.txt"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "--quiet", "-m", "fixture"],
                check=True,
            )
            head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            with patch("deploy.production_install.BASE_CHECKPOINT_SHA", head):
                _check_repository(repo, head)
                with self.assertRaisesRegex(InstallError, "does not match"):
                    _check_repository(repo, "0" * 40)
                with self.assertRaisesRegex(InstallError, "full lowercase"):
                    _check_repository(repo, "HEAD")
                subprocess.run(
                    ["git", "-C", str(repo), "branch", "--move", "wrong-branch"],
                    check=True,
                )
                with self.assertRaisesRegex(InstallError, "approved release branch"):
                    _check_repository(repo, head)

    def test_production_config_does_not_pin_a_guild(self) -> None:
        example = (
            Path(__file__).parents[1] / "deploy/systemd/armory-config.example"
        ).read_text(encoding="utf-8")
        self.assertNotIn("DISCORD_TEST_GUILD_ID=", example)
        self.assertFalse(hasattr(preflight, "guild_configuration_is_valid"))

    def test_optional_logo_path_is_allowlisted_for_external_provisioning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config"
            config.write_text(
                "ARMORY_LOGO_PATH=/etc/wow-forever-armory-bot/wow_forever_logo.png\n",
                encoding="utf-8",
            )
            config.chmod(0o600)
            validate_config(config, expected_owner=os.getuid())
            config.write_text("ARMORY_LOGO_PATH=/tmp/unreviewed.png\n")
            config.chmod(0o600)
            with self.assertRaisesRegex(InstallError, "unsupported local logo"):
                validate_config(config, expected_owner=os.getuid())

    def test_logo_asset_requires_root_service_group_and_exact_private_mode(
        self,
    ) -> None:
        asset = Mock()
        asset.is_symlink.return_value = False
        asset.is_file.return_value = True
        asset.stat.return_value = SimpleNamespace(
            st_uid=0, st_gid=321, st_mode=stat.S_IFREG | 0o640
        )
        _validate_logo_asset(asset, service_uid=654, service_gid=321)
        asset.stat.return_value = SimpleNamespace(
            st_uid=0, st_gid=0, st_mode=stat.S_IFREG | 0o644
        )
        with self.assertRaisesRegex(InstallError, "ownership or permissions"):
            _validate_logo_asset(asset, service_uid=654, service_gid=321)

    def test_incomplete_release_is_preserved_for_operator_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            release = root / "release"
            release.mkdir()
            sentinel = release / "operator-review.txt"
            sentinel.write_text("preserve this file", encoding="utf-8")
            with self.assertRaisesRegex(
                InstallError, "preserve it for operator review"
            ):
                _install_release(repo, release, os.getuid(), os.getgid(), "a" * 40)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve this file")

    def test_dependency_locks_pin_every_dependency_and_hash(self) -> None:
        root = Path(__file__).parents[1] / "deploy"
        for name in ("requirements-build.lock", "requirements-runtime.lock"):
            entries = [
                line.strip()
                for line in (root / name).read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            self.assertTrue(entries)
            logical = " ".join(entries).replace("\\", "").split()
            self.assertEqual(len(logical) % 2, 0)
            for index in range(0, len(logical), 2):
                self.assertRegex(logical[index], r"^[A-Za-z0-9_.-]+==[^=]+$")
                self.assertRegex(logical[index + 1], r"^--hash=sha256:[0-9a-f]{64}$")

    def test_release_python_guard_is_explicitly_limited_to_locked_target(self) -> None:
        source = (Path(__file__).parents[1] / "deploy/production_install.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('version != "3.14:x86_64:linux"', source)
        project = (Path(__file__).parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn('requires-python = ">=3.14,<3.15"', project)
        with patch(
            "deploy.production_install.subprocess.run",
            return_value=SimpleNamespace(stdout="3.13:x86_64:linux"),
        ):
            with self.assertRaisesRegex(InstallError, "supports CPython 3.14"):
                _require_supported_runtime(Path("python"))

    def test_config_guild_must_match_independently_supplied_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config"
            credential = root / "token"
            config.write_text(
                "ARMORY_LOG_LEVEL=INFO\n",
                encoding="utf-8",
            )
            config.chmod(0o600)
            credential.write_text("synthetic-fixture\n", encoding="utf-8")
            credential.chmod(0o600)

            validate_inputs(config, credential, expected_owner=os.getuid())

    def test_insecure_or_empty_credential_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config"
            credential = root / "token"
            config.write_text("ARMORY_LOG_LEVEL=INFO\n")
            config.chmod(0o600)
            credential.write_text("synthetic-fixture\n")
            credential.chmod(0o644)
            with self.assertRaisesRegex(InstallError, "permissions are unsafe"):
                validate_inputs(config, credential, expected_owner=os.getuid())
            credential.write_bytes(b"")
            credential.chmod(0o600)
            with self.assertRaisesRegex(InstallError, "token line"):
                validate_inputs(config, credential, expected_owner=os.getuid())

    def test_config_rejects_duplicate_guild_and_token_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config"
            credential = Path(temporary) / "token"
            credential.write_text("synthetic-fixture\n", encoding="ascii")
            credential.chmod(0o600)
            for contents in (
                "ARMORY_LOG_LEVEL=INFO\nARMORY_LOG_LEVEL=ERROR\n",
                "DISCORD_TEST_GUILD_ID=123456789012345678\n",
            ):
                config.write_text(contents, encoding="utf-8")
                config.chmod(0o600)
                with self.assertRaises(InstallError):
                    validate_inputs(config, credential, expected_owner=os.getuid())

    def test_preflight_rejects_missing_empty_and_uninitialized_db(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing.sqlite3"
            self.assertFalse(preflight.database_is_ready(missing))

            empty = root / "empty.sqlite3"
            empty.touch()
            self.assertFalse(preflight.database_is_ready(empty))

            uninitialized = root / "uninitialized.sqlite3"
            with sqlite3.connect(uninitialized):
                pass
            self.assertFalse(preflight.database_is_ready(uninitialized))

    def test_preflight_accepts_valid_zero_and_one_guild_databases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for guild_count in (0, 1):
                with self.subTest(guild_count=guild_count):
                    database = root / f"guilds-{guild_count}.sqlite3"
                    _create_preflight_database(database, guild_count=guild_count)
                    self.assertTrue(preflight.database_is_ready(database))

    def test_preflight_rejects_missing_required_tables_and_corrupt_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing_tables = root / "missing-tables.sqlite3"
            with sqlite3.connect(missing_tables) as connection:
                connection.execute("CREATE TABLE guilds (guild_id INTEGER)")
            missing_tables.chmod(0o600)
            self.assertFalse(preflight.database_is_ready(missing_tables))

            corrupt = root / "corrupt.sqlite3"
            corrupt.write_bytes(b"not a SQLite database")
            corrupt.chmod(0o600)
            self.assertFalse(preflight.database_is_ready(corrupt))

    def test_preflight_rejects_wrong_owner_and_unsafe_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "private.sqlite3"
            _create_preflight_database(database)
            self.assertFalse(
                preflight.database_is_ready(
                    database, expected_owner=os.getuid() + 1
                )
            )
            database.chmod(0o640)
            self.assertFalse(preflight.database_is_ready(database))

    def test_preflight_restore_marker_blocks_initialized_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "initialized.sqlite3"
            _create_preflight_database(database)
            Path(f"{database}.restore-in-progress").write_text("review")
            self.assertFalse(preflight.database_is_ready(database))

    def test_installed_preflight_copy_keeps_empty_database_fix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = (
                Path(__file__).parents[1]
                / "deploy/systemd/armory-db-preflight.py"
            )
            destination = root / "bin/armory-db-preflight"
            destination.parent.mkdir()
            temporary_copy = destination.with_name(".armory-db-preflight.installing")
            with (
                patch("deploy.production_install.os.chown"),
                patch("deploy.production_install.os.chmod") as chmod,
            ):
                _copy_unit(source, destination, 0o755)
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertFalse(temporary_copy.exists())
            chmod.assert_called_once_with(temporary_copy, 0o755)

            loader = SourceFileLoader("installed_armory_db_preflight", str(destination))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            self.assertIsNotNone(spec)
            installed = importlib.util.module_from_spec(spec)
            loader.exec_module(installed)
            database = root / "empty-guilds.sqlite3"
            _create_preflight_database(database)
            self.assertTrue(installed.database_is_ready(database))

    def test_startup_preflight_is_guild_agnostic(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "ARMORY_DATABASE_PATH": "/temporary/verified.sqlite3",
                    "DISCORD_TEST_GUILD_ID": "123456789012345678",
                },
            ),
            patch.object(preflight, "armory_process_present", return_value=False),
            patch.object(preflight, "database_is_ready", return_value=True),
            patch.object(
                preflight.pwd,
                "getpwnam",
                return_value=SimpleNamespace(pw_uid=os.getuid()),
            ),
        ):
            self.assertEqual(preflight.main(), 0)

    def test_startup_preflight_requires_database_path(self) -> None:
        with (
            patch.dict(os.environ, {"ARMORY_DATABASE_PATH": ""}),
            patch.object(preflight, "armory_process_present", return_value=False),
            patch.object(
                preflight.pwd,
                "getpwnam",
                return_value=SimpleNamespace(pw_uid=os.getuid()),
            ),
        ):
            self.assertEqual(preflight.main(), 78)

    def test_preflight_finds_existing_armory_module_without_reading_env(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            process = proc / "101"
            process.mkdir()
            (process / "cmdline").write_bytes(
                b"python\0-m\0wow_forever_armory.discord_bot\0"
            )
            (process / "environ").write_bytes(
                b"DISCORD_BOT_TOKEN=synthetic-never-print\0"
            )
            self.assertTrue(preflight.armory_process_present(proc))

    def test_existing_armory_process_still_blocks_preflight_database_check(self):
        with (
            patch.dict(os.environ, {"ARMORY_DATABASE_PATH": "/unused/database"}),
            patch.object(preflight, "armory_process_present", return_value=True),
            patch.object(preflight, "database_is_ready") as database_is_ready,
        ):
            self.assertEqual(preflight.main(), 78)
            database_is_ready.assert_not_called()

    def test_bot_unit_uses_root_preflight_and_nonstarting_install_files(self) -> None:
        root = Path(__file__).parents[1]
        unit = (root / "deploy/systemd/wow-forever-armory-bot.service").read_text(
            encoding="utf-8"
        )
        installer = (root / "deploy/production_install.py").read_text(encoding="utf-8")
        self.assertIn(
            "ExecStartPre=+/opt/wow-forever-armory-bot/bin/armory-db-preflight",
            unit,
        )
        self.assertIn("LoadCredential=discord-token:", unit)
        self.assertNotIn('"enable"', installer)
        self.assertNotIn('"start"', installer)
        self.assertIn('deploy / "systemd/armory-db-preflight.py"', installer)
        self.assertIn('BASE / "bin/armory-db-preflight"', installer)
        self.assertIn(
            "_copy_unit(preflight_source, preflight_destination, 0o755)", installer
        )
        self.assertTrue(
            (root / "deploy/systemd/armory-db-preflight.py").stat().st_mode & 0o111
        )
        self.assertIn('deploy / "systemd/armory-db-preflight.py"', installer)
        self.assertIn('BASE / "bin/armory-db-preflight"', installer)


if __name__ == "__main__":
    unittest.main()
