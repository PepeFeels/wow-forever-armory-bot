"""Offline checks for installation gates and the database startup guard."""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from deploy.production_install import InstallError, _check_repository, validate_inputs


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


class ProductionInstallTests(unittest.TestCase):
    def test_installer_requires_exact_clean_release_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            subprocess.run(
                [
                    "git",
                    "init",
                    "--quiet",
                    "--initial-branch=feature/v0.2.0-discord",
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

    def test_production_config_does_not_pin_a_guild(self) -> None:
        example = (
            Path(__file__).parents[1] / "deploy/systemd/armory-config.example"
        ).read_text(encoding="utf-8")
        self.assertNotIn("DISCORD_TEST_GUILD_ID=", example)
        self.assertFalse(hasattr(preflight, "guild_configuration_is_valid"))

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

    def test_preflight_rejects_missing_empty_uninitialized_and_gated_db(self) -> None:
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

            initialized = root / "initialized.sqlite3"
            with sqlite3.connect(initialized) as connection:
                connection.executescript(
                    "CREATE TABLE guilds (guild_id INTEGER PRIMARY KEY);"
                    "CREATE TABLE members (guild_id INTEGER, user_id INTEGER);"
                    "CREATE TABLE characters (guild_id INTEGER, user_id INTEGER);"
                    "CREATE TABLE flows (guild_id INTEGER, user_id INTEGER);"
                    "INSERT INTO guilds VALUES (1);"
                )
            initialized.chmod(0o600)
            self.assertTrue(preflight.database_is_ready(initialized))
            Path(f"{initialized}.restore-in-progress").write_text("review")
            self.assertFalse(preflight.database_is_ready(initialized))

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
        self.assertTrue(
            (root / "deploy/systemd/armory-db-preflight.py").stat().st_mode & 0o111
        )


if __name__ == "__main__":
    unittest.main()
