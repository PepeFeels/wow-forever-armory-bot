"""Mocked startup safety tests; these never start a Discord connection."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from wow_forever_armory import discord_bot
from wow_forever_armory.operations import (
    database_file_lock_path,
    exclusive_file_lock,
    restore_marker_path,
    runtime_lock_path,
)


class RuntimeSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_running_bot_refuses_database_with_interrupted_restore_marker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "armory.sqlite3"
            restore_marker_path(database).write_text("operator review")
            environment = {
                "DISCORD_BOT_TOKEN": "synthetic-test-token",
                "ARMORY_DATABASE_PATH": str(database),
            }
            with (
                patch.dict(os.environ, environment, clear=True),
                patch("wow_forever_armory.discord_bot.configure_logging"),
                patch("wow_forever_armory.discord_bot.create_bot") as create_bot,
            ):
                with self.assertRaisesRegex(RuntimeError, "operator review"):
                    await discord_bot.run_bot()
            create_bot.assert_not_called()

    async def test_second_bot_process_fails_before_creating_discord_client(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "armory.sqlite3"
            environment = {
                "DISCORD_BOT_TOKEN": "synthetic-test-token",
                "ARMORY_DATABASE_PATH": str(database),
            }
            with (
                patch.dict(os.environ, environment, clear=True),
                patch("wow_forever_armory.discord_bot.configure_logging"),
                patch("wow_forever_armory.discord_bot.create_bot") as create_bot,
            ):
                with exclusive_file_lock(runtime_lock_path(database)):
                    with self.assertRaisesRegex(RuntimeError, "already owns"):
                        await discord_bot.run_bot()
            create_bot.assert_not_called()

    async def test_separate_process_lock_blocks_bot_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "armory.sqlite3"
            database_alias = Path(temp_dir) / "armory-hardlink.sqlite3"
            ready = Path(temp_dir) / "lock-ready"
            lock_path = runtime_lock_path(database)
            inode_lock = database_file_lock_path(database)
            code = f"""
import time
from pathlib import Path
from wow_forever_armory.operations import exclusive_file_lock
with exclusive_file_lock({str(lock_path)!r}):
    with exclusive_file_lock({str(inode_lock)!r}):
        Path({str(ready)!r}).touch()
        time.sleep(20)
"""
            holder = subprocess.Popen(
                [sys.executable, "-c", code],
                cwd=Path.cwd(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    if holder.poll() is not None:
                        self.fail("lock holder process exited unexpectedly")
                    await asyncio.sleep(0.01)
                self.assertTrue(ready.exists())
                os.link(database, database_alias)
                environment = {
                    "DISCORD_BOT_TOKEN": "synthetic-test-token",
                    "ARMORY_DATABASE_PATH": str(database_alias),
                }
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch("wow_forever_armory.discord_bot.configure_logging"),
                    patch("wow_forever_armory.discord_bot.create_bot") as create_bot,
                ):
                    with self.assertRaisesRegex(RuntimeError, "already owns"):
                        await discord_bot.run_bot()
                create_bot.assert_not_called()
            finally:
                holder.terminate()
                holder.wait(timeout=5)

    async def test_systemd_credential_file_is_loaded_for_mocked_client_start(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "armory.sqlite3"
            bot = unittest.mock.MagicMock()
            bot.start = AsyncMock()
            credential = Path(temp_dir) / "credential"
            credential.write_text("fixture-token\n")
            credential.chmod(0o600)

            class BotContext:
                async def __aenter__(self) -> unittest.mock.MagicMock:
                    return bot

                async def __aexit__(self, *args: object) -> None:
                    return None

            environment = {
                "DISCORD_BOT_TOKEN_FILE": f"{temp_dir}/credential",
                "ARMORY_DATABASE_PATH": str(database),
            }
            with (
                patch.dict(os.environ, environment, clear=True),
                patch("wow_forever_armory.discord_bot.configure_logging") as configure,
                patch(
                    "wow_forever_armory.discord_bot.create_bot",
                    return_value=BotContext(),
                ),
            ):
                await discord_bot.run_bot()

            bot.start.assert_awaited_once_with("fixture-token")
            configure.assert_called_once_with(secrets=("fixture-token",))
            self.assertEqual(database.stat().st_mode & 0o777, 0o600)

    async def test_systemd_0440_root_credential_is_loaded_from_credential_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            credentials = root / "credentials"
            credentials.mkdir()
            credential = credentials / "discord-token"
            credential.write_text("fixture-token\n", encoding="utf-8")
            credential.chmod(0o440)
            database = root / "armory.sqlite3"
            bot = unittest.mock.MagicMock()
            bot.start = AsyncMock()

            class BotContext:
                async def __aenter__(self) -> unittest.mock.MagicMock:
                    return bot

                async def __aexit__(self, *args: object) -> None:
                    return None

            real_fstat = os.fstat

            def systemd_fstat(descriptor: int) -> SimpleNamespace:
                actual = real_fstat(descriptor)
                # The unprivileged test runner cannot chown fixtures to root.
                # Model systemd's observed root owner while preserving the real
                # file's regular-file type and 0440 mode.
                return SimpleNamespace(st_mode=actual.st_mode, st_uid=0)

            environment = {
                "CREDENTIALS_DIRECTORY": str(credentials),
                "DISCORD_BOT_TOKEN_FILE": str(credential),
                "ARMORY_DATABASE_PATH": str(database),
            }
            with (
                patch.dict(os.environ, environment, clear=True),
                patch(
                    "wow_forever_armory.discord_bot.os.fstat", side_effect=systemd_fstat
                ),
                patch("wow_forever_armory.discord_bot.configure_logging") as configure,
                patch.object(discord_bot, "_LOGGER") as logger,
                patch(
                    "wow_forever_armory.discord_bot.create_bot",
                    return_value=BotContext(),
                ),
            ):
                await discord_bot.run_bot()

            bot.start.assert_awaited_once_with("fixture-token")
            configure.assert_called_once_with(secrets=("fixture-token",))
            self.assertNotIn(
                "fixture-token", " ".join(str(call) for call in logger.mock_calls)
            )

    async def test_world_readable_credential_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            credential = Path(temp_dir) / "credential"
            credential.write_text("synthetic-test-token")
            credential.chmod(0o644)
            environment = {
                "DISCORD_BOT_TOKEN_FILE": str(credential),
                "ARMORY_DATABASE_PATH": str(Path(temp_dir) / "armory.sqlite3"),
            }
            with (
                patch.dict(os.environ, environment, clear=True),
                patch("wow_forever_armory.discord_bot.configure_logging"),
                patch("wow_forever_armory.discord_bot.create_bot") as create_bot,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "credential file is unavailable"
                ) as failure:
                    await discord_bot.run_bot()
            self.assertNotIn("synthetic-test-token", str(failure.exception))
            create_bot.assert_not_called()

    def test_credential_permission_policy_is_narrow_and_path_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            credentials = root / "credentials"
            credential = credentials / "discord-token"

            safe_cases = (
                (root / "ordinary-token", os.getuid(), 0o600, None),
                (credential, 0, 0o440, str(credentials)),
            )
            for path, owner_uid, mode, systemd_directory in safe_cases:
                with self.subTest(path=str(path), mode=oct(mode)):
                    self.assertTrue(
                        discord_bot._credential_permissions_are_safe(
                            path,
                            owner_uid=owner_uid,
                            mode=mode,
                            credentials_directory=systemd_directory,
                        )
                    )

            unsafe_cases = (
                (credential, os.getuid(), 0o440, str(credentials)),
                (credential, 0, 0o644, str(credentials)),
                (root / "other-token", 0, 0o440, str(credentials)),
                (credential, 0, 0o460, str(credentials)),
                (root / "ordinary-token", 0, 0o440, None),
            )
            for path, owner_uid, mode, systemd_directory in unsafe_cases:
                with self.subTest(path=str(path), mode=oct(mode)):
                    self.assertFalse(
                        discord_bot._credential_permissions_are_safe(
                            path,
                            owner_uid=owner_uid,
                            mode=mode,
                            credentials_directory=systemd_directory,
                        )
                    )

    def test_credential_reader_rejects_symbolic_links(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original = root / "token"
            original.write_text("synthetic-token\n", encoding="utf-8")
            original.chmod(0o600)
            link = root / "discord-token"
            link.symlink_to(original)
            with patch.dict(
                os.environ, {"CREDENTIALS_DIRECTORY": str(root)}, clear=True
            ):
                with self.assertRaises(OSError):
                    discord_bot._read_credential_file(link)

    def test_credential_reader_permission_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            credential = directory / "discord-token"
            credential.write_text("synthetic-test-token\n", encoding="utf-8")

            cases = (
                (0o600, None, True),
                (0o440, None, False),
                (0o440, str(directory), True),
                (0o644, str(directory), False),
            )

            for mode, systemd_directory, should_pass in cases:
                with self.subTest(mode=oct(mode), systemd=bool(systemd_directory)):
                    credential.chmod(mode)
                    environment = {}
                    if systemd_directory is not None:
                        environment["CREDENTIALS_DIRECTORY"] = systemd_directory

                    with patch.dict(os.environ, environment, clear=True):
                        if should_pass:
                            if systemd_directory is None:
                                actual = discord_bot._read_credential_file(credential)
                            else:
                                real_fstat = os.fstat

                                def root_owned_fstat(descriptor: int, fstat=real_fstat):
                                    result = fstat(descriptor)
                                    return SimpleNamespace(
                                        st_mode=result.st_mode, st_uid=0
                                    )

                                with patch(
                                    "wow_forever_armory.discord_bot.os.fstat",
                                    side_effect=root_owned_fstat,
                                ):
                                    actual = discord_bot._read_credential_file(
                                        credential
                                    )
                            self.assertEqual(actual, "synthetic-test-token")
                        else:
                            with self.assertRaises(ValueError):
                                discord_bot._read_credential_file(credential)


if __name__ == "__main__":
    unittest.main()
