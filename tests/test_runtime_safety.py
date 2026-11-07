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
                ):
                    await discord_bot.run_bot()
            create_bot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
