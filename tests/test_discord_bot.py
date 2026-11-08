"""Guild Armory command and event tests with no Discord gateway access."""

import asyncio
import os
import tempfile
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from wow_forever_armory.client import ArmoryClient
from wow_forever_armory.discord_bot import (
    ArmoryBot,
    BotIdentityUnavailable,
    GuildArmoryCommands,
    GuildRecoveryResult,
    MemberRowSyncOutcome,
)
from wow_forever_armory.guild_armory_store import GuildArmoryStore
from wow_forever_armory.guild_armory_views import CharacterSelectView, GuildPanelView
from wow_forever_armory.models import Character

PREFIX = "en-us/forever/eu/armory/character"
PANEL_URL = "https://worldofwarcraft.blizzard.com/en-us/forever/eu/armory/character/"


async def _inline_thread_call(function, *args, **kwargs):
    """Keep Discord adapter tests offline from the default executor lifecycle."""
    return function(*args, **kwargs)


class GuildArmoryBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = GuildArmoryStore(Path(self.temp_dir.name) / "guild.sqlite3")
        await self.store.initialize()
        await self.store.activate_guild(111, 222, 333)
        self.core = ArmoryClient(max_retries=0)
        self.bot = ArmoryBot(self.core, self.store, 999)
        self.render_runner = AsyncMock(side_effect=_inline_thread_call)
        self._render_runner_patch = patch(
            "wow_forever_armory.discord_bot.to_thread", new=self.render_runner
        )
        self._render_runner_patch.start()

    async def asyncTearDown(self) -> None:
        await self.bot.close()
        self._render_runner_patch.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def interaction(
        *, guild_id: int = 111, user_id: int = 456, manage_guild: bool = False
    ) -> AsyncMock:
        interaction = AsyncMock()
        interaction.guild_id = guild_id
        interaction.guild = MagicMock()
        interaction.user.id = user_id
        interaction.user.guild_permissions.manage_guild = manage_guild
        interaction.user.guild_permissions.administrator = False
        interaction.channel = MagicMock(spec=discord.TextChannel)
        interaction.channel.mention = "#armory"
        return interaction

    @staticmethod
    def payload(*, guild_id: int = 111, user_id: int = 456) -> SimpleNamespace:
        return SimpleNamespace(
            guild_id=guild_id,
            user_id=user_id,
            message_id=333,
            emoji=SimpleNamespace(name="⚔️"),
        )

    def mark_owned(self, message: MagicMock, filename: str) -> None:
        self.bot._connection.user = SimpleNamespace(id=1)
        message.author = SimpleNamespace(id=1)
        message.attachments = [SimpleNamespace(filename=filename)]

    async def test_activate_rejects_member_without_manage_permission(self) -> None:
        interaction = self.interaction(manage_guild=False)
        cog = GuildArmoryCommands(self.bot)
        await cog.activate.callback(cog, interaction)
        interaction.response.send_message.assert_awaited_once()
        interaction.response.defer.assert_not_awaited()

    async def test_activate_reuses_configured_channel_and_panel(self) -> None:
        interaction = self.interaction(manage_guild=True)
        channel = interaction.channel
        channel.id = 222
        channel.fetch_message = AsyncMock()
        panel = MagicMock(spec=discord.Message)
        panel.id = 333
        self.mark_owned(panel, "guild-armory-panel-111.png")
        panel.add_reaction = AsyncMock()
        panel.edit = AsyncMock()
        channel.fetch_message.return_value = panel
        with patch(
            "wow_forever_armory.discord_bot.render_intro_panel", return_value=b"png"
        ):
            result = await self.bot.upsert_panel(
                111, channel, await self.store.get_guild(111)
            )
        self.assertIs(result, panel)
        channel.send.assert_not_awaited()
        panel.edit.assert_awaited_once()
        panel.add_reaction.assert_awaited_once_with("⚔️")
        self.assertEqual((await self.store.get_guild(111)).panel_message_id, 333)

    async def test_panel_view_has_add_edit_and_official_link(
        self,
    ) -> None:
        view = GuildPanelView(self.bot, 111)
        buttons = [
            child for child in view.children if isinstance(child, discord.ui.Button)
        ]
        self.assertEqual(len(buttons), 2)
        self.assertEqual(buttons[0].label, "Add & Edit")
        self.assertEqual(buttons[0].custom_id, "guild-armory:manage:111")
        self.assertEqual(
            buttons[1].url,
            "https://worldofwarcraft.blizzard.com/en-us/forever/eu/armory/",
        )
        self.assertIn("(EU)", buttons[1].label)
        self.assertFalse(
            any("Register Privately" in (item.label or "") for item in buttons)
        )

    async def test_add_edit_button_opens_character_management(self) -> None:
        view = GuildPanelView(self.bot, 111)
        self.bot.open_management = AsyncMock()
        interaction = self.interaction()
        await view.children[0].callback(interaction)
        self.bot.open_management.assert_awaited_once_with(interaction, 111)

    async def test_management_menu_supports_adding_and_editing_characters(self) -> None:
        await self.store.finish_flow(
            111,
            456,
            "Hero",
            f"{PREFIX}/hero",
            Character(name="Hero", surname="Woodsong", level=60),
        )
        state = await self.store.member(111, 456)
        view = CharacterSelectView(self.bot, 111, 456, state)
        self.assertTrue(
            any(
                isinstance(item, discord.ui.Button) and item.label == "Add Character"
                for item in view.children
            )
        )
        self.assertTrue(
            any(isinstance(item, discord.ui.Select) for item in view.children)
        )
        selector = next(
            item for item in view.children if isinstance(item, discord.ui.Select)
        )
        self.assertEqual(selector.options[0].label, "Hero Woodsong")

    async def test_bot_uses_only_non_privileged_guild_and_reaction_intents(
        self,
    ) -> None:
        intents = self.bot.intents
        self.assertTrue(intents.guilds)
        self.assertTrue(intents.guild_reactions)
        self.assertFalse(intents.message_content)
        self.assertFalse(intents.members)

    async def test_discord_event_dispatch_runs_connect_then_ready_callbacks(
        self,
    ) -> None:
        await self.bot._async_setup_hook()
        loop = asyncio.get_running_loop()
        events: list[str] = []
        on_connect = AsyncMock(side_effect=lambda: events.append("connect"))
        on_ready = AsyncMock(side_effect=lambda: events.append("ready"))
        with (
            patch.object(self.bot, "on_connect", on_connect),
            patch.object(self.bot, "on_ready", on_ready),
        ):
            existing_tasks = asyncio.all_tasks(loop)
            self.bot.dispatch("connect")
            dispatched_tasks = asyncio.all_tasks(loop) - existing_tasks
            await asyncio.gather(*dispatched_tasks)
            existing_tasks = asyncio.all_tasks(loop)
            self.bot.dispatch("ready")
            dispatched_tasks = asyncio.all_tasks(loop) - existing_tasks
            await asyncio.gather(*dispatched_tasks)
        on_connect.assert_awaited_once_with()
        on_ready.assert_awaited_once_with()
        self.assertEqual(events, ["connect", "ready"])

    async def test_setup_syncs_commands_only_to_test_guild(self) -> None:
        self.bot.add_cog = AsyncMock()
        self.bot.tree.copy_global_to = MagicMock()
        self.bot.tree.sync = AsyncMock()
        with self.assertLogs("wow_forever_armory.discord_bot", level="INFO") as logs:
            await self.bot.setup_hook()
        guild = self.bot.tree.copy_global_to.call_args.kwargs["guild"]
        self.assertEqual(guild.id, 999)
        self.bot.tree.sync.assert_awaited_once_with(guild=guild)
        self.assertIn("discord_bot_setup_started", "\n".join(logs.output))
        self.assertIn("slash_commands_synced_to_test_guild", "\n".join(logs.output))
        self.assertIn("discord_bot_setup_completed", "\n".join(logs.output))

    async def test_setup_hook_failure_is_logged_and_propagated(self) -> None:
        sensitive = "Aeltherion Whispering-Wind 123456789012345678"
        self.bot.store.initialize = AsyncMock(side_effect=RuntimeError(sensitive))
        with self.assertLogs("wow_forever_armory.discord_bot", level="ERROR") as logs:
            with self.assertRaisesRegex(
                RuntimeError, "Discord bot setup failed"
            ) as captured:
                await self.bot.setup_hook()
        output = "\n".join(logs.output)
        self.assertIn("discord_bot_setup_failed", output)
        self.assertNotIn(sensitive, output)
        self.assertNotIn("Traceback", output)
        self.assertTrue(captured.exception.__suppress_context__)
        self.assertNotIn(
            sensitive, "".join(traceback.format_exception(captured.exception))
        )

    async def test_global_command_sync_failure_never_logs_setup_or_ready_success(
        self,
    ) -> None:
        self.bot.test_guild_id = None
        self.bot.add_cog = AsyncMock()
        self.bot.tree.sync = AsyncMock(
            side_effect=RuntimeError(
                "token=synthetic private URL?token=secret discord_id=123456789012345678"
            )
        )
        with patch("wow_forever_armory.discord_bot._LOGGER") as logger:
            with self.assertRaisesRegex(RuntimeError, "Discord bot setup failed"):
                await self.bot.setup_hook()
        self.assertEqual(logger.log.call_args.args[1], "discord_bot_setup_failed")
        self.assertEqual(
            logger.log.call_args.kwargs["extra"],
            {"exception_type": "RuntimeError", "reason": "setup_hook"},
        )
        events = [call.args[0] for call in logger.info.call_args_list]
        self.assertIn("discord_bot_setup_started", events)
        self.assertNotIn("global_slash_commands_synced", events)
        self.assertNotIn("discord_bot_setup_completed", events)
        self.assertNotIn("discord_ready_event_received", events)
        self.assertNotIn("guild_armory_recovery_started", events)

    async def test_setup_globally_syncs_when_no_development_guild_is_configured(
        self,
    ) -> None:
        self.bot.test_guild_id = None
        self.bot.add_cog = AsyncMock()
        self.bot.tree.copy_global_to = MagicMock()
        self.bot.tree.sync = AsyncMock(return_value=[object(), object()])
        await self.bot.setup_hook()
        self.bot.tree.copy_global_to.assert_not_called()
        self.bot.tree.sync.assert_awaited_once_with()

    async def test_create_bot_does_not_require_test_guild_configuration(self) -> None:
        from wow_forever_armory.discord_bot import create_bot

        with patch.dict(os.environ, {}, clear=True):
            bot = create_bot()
        try:
            self.assertIsNone(bot.test_guild_id)
        finally:
            await bot.close()

    async def test_create_bot_rejects_invalid_optional_test_guild(self) -> None:
        from wow_forever_armory.discord_bot import create_bot

        with patch.dict(
            os.environ,
            {
                "ARMORY_ENVIRONMENT": "development",
                "DISCORD_TEST_GUILD_ID": "not-a-guild",
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "positive integer"):
                create_bot()

    async def test_development_mode_honors_test_guild_override(self) -> None:
        from wow_forever_armory.discord_bot import create_bot

        with patch.dict(
            os.environ,
            {
                "ARMORY_ENVIRONMENT": "development",
                "DISCORD_TEST_GUILD_ID": "123456789012345678",
            },
        ):
            bot = create_bot()
        try:
            self.assertEqual(bot.test_guild_id, 123456789012345678)
        finally:
            await bot.close()

    async def test_production_ignores_legacy_test_guild_environment_variable(
        self,
    ) -> None:
        from wow_forever_armory.discord_bot import create_bot

        with patch.dict(
            os.environ,
            {
                "ARMORY_ENVIRONMENT": "production",
                "DISCORD_TEST_GUILD_ID": "123456789012345678",
            },
        ):
            bot = create_bot()
        try:
            self.assertIsNone(bot.test_guild_id)
        finally:
            await bot.close()

    async def test_same_member_has_independent_access_in_two_guilds(self) -> None:
        await self.store.activate_guild(222, 223, 224)
        self.assertEqual((await self.store.get_guild(111)).channel_id, 222)
        self.assertEqual((await self.store.get_guild(222)).channel_id, 223)
        self.assertEqual((await self.store.get_guild_by_panel(333)).guild_id, 111)
        self.assertEqual((await self.store.get_guild_by_panel(224)).guild_id, 222)
        first_guild = await self.store.finish_flow(
            111, 456, "First", f"{PREFIX}/first", Character(name="First")
        )
        second_guild = await self.store.finish_flow(
            222, 456, "Second", f"{PREFIX}/second", Character(name="Second")
        )
        self.assertEqual(first_guild.guild_id, 111)
        self.assertEqual(second_guild.guild_id, 222)
        self.assertEqual(
            (await self.store.member(111, 456)).characters[0].path,
            f"{PREFIX}/first",
        )
        self.assertEqual(
            (await self.store.member(222, 456)).characters[0].path,
            f"{PREFIX}/second",
        )
        with self.assertRaises(ValueError):
            await self.store.remove_character(222, 456, f"{PREFIX}/first")

    async def test_startup_recovery_reuses_panel_and_member_row(self) -> None:
        await self.store.finish_flow(
            111,
            456,
            "Hero",
            f"{PREFIX}/hero",
            Character(name="Hero", level=60),
        )
        await self.store.set_row_message(111, 456, 888)
        panel = MagicMock(spec=discord.Message)
        panel.id = 333
        self.mark_owned(panel, "guild-armory-panel-111.png")
        panel.add_reaction = AsyncMock()
        row = MagicMock(spec=discord.Message)
        row.id = 888
        self.mark_owned(row, "guild-armory-111-456.png")
        row.edit = AsyncMock()
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(side_effect=[panel, row, panel, row])
        channel.send = AsyncMock()
        self.bot.get_armory_channel = AsyncMock(return_value=channel)
        with (
            patch(
                "wow_forever_armory.discord_bot.render_stored_member_overview",
                return_value=b"PNG",
            ),
            self.assertLogs("wow_forever_armory.discord_bot", level="INFO") as logs,
        ):
            await self.bot.on_connect()
            await self.bot.on_ready()
            await self.bot.on_disconnect()
            await self.bot.on_ready()
        self.assertEqual(panel.add_reaction.await_count, 2)
        self.assertEqual(row.edit.await_count, 2)
        channel.send.assert_not_awaited()
        self.assertTrue(self.bot._recovered)
        output = "\n".join(logs.output)
        self.assertIn("discord_gateway_connected", output)
        self.assertIn("discord_ready_event_received", output)
        self.assertIn("guild_armory_recovery_started", output)
        self.assertIn("guild_armory_recovery_completed", output)
        self.assertIn("discord_disconnected_recovery_scheduled", output)

    async def test_failed_recovery_is_retried_on_next_ready(self) -> None:
        config = await self.store.get_guild(111)
        self.bot.store.guilds = AsyncMock(return_value=(config,))
        self.bot._recover_guild = AsyncMock(
            side_effect=[GuildRecoveryResult(False), GuildRecoveryResult(True)]
        )
        await self.bot.on_ready()
        self.assertFalse(self.bot._recovered)
        await self.bot.on_ready()
        self.assertTrue(self.bot._recovered)
        self.assertEqual(self.bot._recover_guild.await_count, 2)

    async def test_recovery_completion_logs_aggregate_counts(self) -> None:
        config = await self.store.get_guild(111)
        self.bot.store.guilds = AsyncMock(return_value=(config,))
        self.bot._recover_guild = AsyncMock(
            return_value=GuildRecoveryResult(
                True,
                panels_recovered=1,
                member_rows_recovered=2,
                member_rows_created=1,
                member_rows_removed=1,
                members_skipped=1,
                member_rows_deferred=1,
                member_state_load_failures=1,
            )
        )
        with patch("wow_forever_armory.discord_bot._LOGGER") as logger:
            await self.bot.on_ready()
        completed = next(
            call
            for call in logger.info.call_args_list
            if call.args and call.args[0] == "guild_armory_recovery_completed"
        )
        self.assertEqual(
            completed.kwargs["extra"],
            {
                "guild_count": 1,
                "guilds_recovered": 1,
                "guilds_incomplete": 0,
                "guilds_deferred": 0,
                "panels_recovered": 1,
                "member_rows_recovered": 2,
                "member_rows_created": 1,
                "member_rows_removed": 1,
                "members_skipped": 1,
                "member_rows_deferred": 1,
                "member_state_load_failures": 1,
                "outcome": "success",
            },
        )

    async def test_recovery_state_load_error_is_logged_as_incomplete(self) -> None:
        self.bot.store.guilds = AsyncMock(side_effect=RuntimeError("fixture"))
        with patch("wow_forever_armory.discord_bot._LOGGER") as logger:
            await self.bot.on_ready()
        logger.log.assert_called_once()
        self.assertEqual(
            logger.log.call_args.args[1], "guild_armory_recovery_state_load_failed"
        )
        self.assertEqual(
            logger.log.call_args.kwargs["extra"],
            {"exception_type": "RuntimeError", "reason": "guild_state_load"},
        )
        logger.error.assert_called_once_with(
            "guild_armory_recovery_completed",
            extra={"outcome": "failed", "reason": "state_load"},
        )
        self.assertFalse(self.bot._recovered)

    async def test_guild_channel_recovery_failure_is_reported(self) -> None:
        config = await self.store.get_guild(111)
        self.bot.get_armory_channel = AsyncMock(side_effect=RuntimeError("fixture"))
        with patch("wow_forever_armory.discord_bot._LOGGER") as logger:
            result = await self.bot._recover_guild(config)
        self.assertFalse(result.recovered)
        logger.log.assert_called_once()
        self.assertEqual(
            logger.log.call_args.args[1], "guild_armory_guild_recovery_failed"
        )
        logger.error.assert_called_once_with(
            "guild_armory_guild_recovery_completed",
            extra={"outcome": "failed", "reason": "channel_lookup"},
        )

    async def test_recovery_failure_for_one_member_does_not_skip_others(self) -> None:
        first = await self.store.finish_flow(
            111, 501, "First", f"{PREFIX}/first", Character(name="First")
        )
        second = await self.store.finish_flow(
            111, 502, "Second", f"{PREFIX}/second", Character(name="Second")
        )
        panel = MagicMock(spec=discord.Message)
        self.mark_owned(panel, "guild-armory-panel-111.png")
        panel.add_reaction = AsyncMock()
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(return_value=panel)
        self.bot.get_armory_channel = AsyncMock(return_value=channel)
        self.bot.sync_member_row = AsyncMock(
            side_effect=[
                RuntimeError("first row unavailable"),
                MemberRowSyncOutcome.UPDATED,
            ]
        )
        recovered = await self.bot._recover_guild(await self.store.get_guild(111))
        self.assertFalse(recovered.recovered)
        self.assertEqual(
            [call.args[0].user_id for call in self.bot.sync_member_row.await_args_list],
            [first.user_id, second.user_id],
        )
        self.assertEqual(recovered.member_rows_deferred, 1)
        self.assertEqual(recovered.member_rows_recovered, 1)

    async def test_empty_member_with_existing_row_is_counted_as_removed(self) -> None:
        await self.store.finish_flow(
            111, 601, "Empty", f"{PREFIX}/empty", Character(name="Empty")
        )
        state = await self.store.remove_character(111, 601, f"{PREFIX}/empty")
        old = MagicMock(spec=discord.Message)
        self.mark_owned(old, "guild-armory-111-601.png")
        old.delete = AsyncMock()
        channel = MagicMock(spec=discord.TextChannel)
        self.bot.get_armory_channel = AsyncMock(return_value=channel)
        self.bot._find_attachment_message = AsyncMock(return_value=old)

        outcome = await self.bot.sync_member_row(state)

        self.assertEqual(outcome, MemberRowSyncOutcome.REMOVED)
        old.delete.assert_awaited_once()
        self.assertIsNone((await self.store.member(111, 601)).row_message_id)

    async def test_empty_member_without_row_is_counted_as_skipped(self) -> None:
        await self.store.finish_flow(
            111, 602, "Empty", f"{PREFIX}/empty-2", Character(name="Empty")
        )
        state = await self.store.remove_character(111, 602, f"{PREFIX}/empty-2")
        self.bot._connection.user = SimpleNamespace(id=1)
        self.bot.get_armory_channel = AsyncMock(
            return_value=MagicMock(spec=discord.TextChannel)
        )
        self.bot._find_attachment_message = AsyncMock(return_value=None)

        outcome = await self.bot.sync_member_row(state)

        self.assertEqual(outcome, MemberRowSyncOutcome.SKIPPED)

    async def test_one_character_existing_row_is_counted_as_updated(self) -> None:
        await self.store.finish_flow(
            111, 603, "One", f"{PREFIX}/one", Character(name="One")
        )
        state = await self.store.member(111, 603)
        row = MagicMock(spec=discord.Message)
        row.id = 1603
        self.mark_owned(row, "guild-armory-111-603.png")
        row.edit = AsyncMock()
        channel = MagicMock(spec=discord.TextChannel)
        self.bot.get_armory_channel = AsyncMock(return_value=channel)
        self.bot._find_attachment_message = AsyncMock(return_value=row)
        with patch(
            "wow_forever_armory.discord_bot.render_stored_member_overview",
            return_value=b"PNG",
        ):
            outcome = await self.bot.sync_member_row(state)
        self.assertEqual(outcome, MemberRowSyncOutcome.UPDATED)
        row.edit.assert_awaited_once()

    async def test_multiple_characters_new_row_is_counted_as_created(self) -> None:
        await self.store.finish_flow(
            111, 604, "Two A", f"{PREFIX}/two-a", Character(name="Two A")
        )
        await self.store.finish_flow(
            111, 604, "Two B", f"{PREFIX}/two-b", Character(name="Two B")
        )
        state = await self.store.member(111, 604)
        self.bot._connection.user = SimpleNamespace(id=1)
        created = MagicMock(spec=discord.Message)
        created.id = 1604
        channel = MagicMock(spec=discord.TextChannel)
        channel.send = AsyncMock(return_value=created)
        self.bot.get_armory_channel = AsyncMock(return_value=channel)
        self.bot._find_attachment_message = AsyncMock(return_value=None)
        with patch(
            "wow_forever_armory.discord_bot.render_stored_member_overview",
            return_value=b"PNG",
        ):
            outcome = await self.bot.sync_member_row(state)
        self.assertEqual(outcome, MemberRowSyncOutcome.CREATED)
        channel.send.assert_awaited_once()

    async def test_recovery_counts_existing_created_removed_and_skipped_rows(
        self,
    ) -> None:
        await self.store.finish_flow(
            111, 611, "Empty Row", f"{PREFIX}/empty-row", Character(name="Empty")
        )
        await self.store.remove_character(111, 611, f"{PREFIX}/empty-row")
        await self.store.finish_flow(
            111, 612, "Empty", f"{PREFIX}/empty-no-row", Character(name="Empty")
        )
        await self.store.remove_character(111, 612, f"{PREFIX}/empty-no-row")
        await self.store.finish_flow(
            111, 613, "One", f"{PREFIX}/one", Character(name="One")
        )
        await self.store.finish_flow(
            111, 614, "Two A", f"{PREFIX}/two-a", Character(name="Two A")
        )
        await self.store.finish_flow(
            111, 614, "Two B", f"{PREFIX}/two-b", Character(name="Two B")
        )
        panel = MagicMock(spec=discord.Message)
        self.mark_owned(panel, "guild-armory-panel-111.png")
        panel.add_reaction = AsyncMock()
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(return_value=panel)
        self.bot.get_armory_channel = AsyncMock(return_value=channel)
        self.bot.sync_member_row = AsyncMock(
            side_effect=[
                MemberRowSyncOutcome.REMOVED,
                MemberRowSyncOutcome.SKIPPED,
                MemberRowSyncOutcome.UPDATED,
                MemberRowSyncOutcome.CREATED,
            ]
        )

        result = await self.bot._recover_guild(await self.store.get_guild(111))

        self.assertTrue(result.recovered)
        self.assertEqual(result.member_rows_recovered, 1)
        self.assertEqual(result.member_rows_created, 1)
        self.assertEqual(result.member_rows_removed, 1)
        self.assertEqual(result.members_skipped, 1)

    async def test_recovery_records_member_state_load_failure(self) -> None:
        config = await self.store.get_guild(111)
        panel = MagicMock(spec=discord.Message)
        self.mark_owned(panel, "guild-armory-panel-111.png")
        panel.add_reaction = AsyncMock()
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(return_value=panel)
        self.bot.get_armory_channel = AsyncMock(return_value=channel)
        self.store.members = AsyncMock(side_effect=RuntimeError("private fixture"))

        result = await self.bot._recover_guild(config)

        self.assertFalse(result.recovered)
        self.assertEqual(result.member_state_load_failures, 1)
        self.assertEqual(result.member_rows_recovered, 0)

    async def test_missing_guild_channel_is_deferred(self) -> None:
        config = await self.store.get_guild(111)
        self.bot.get_armory_channel = AsyncMock(return_value=None)

        result = await self.bot._recover_guild(config)

        self.assertFalse(result.recovered)
        self.assertTrue(result.deferred)
        self.assertEqual(result.member_rows_deferred, 0)

    async def _assert_panel_operation_failure_is_deferred(
        self, operation: str, error: Exception, reason: str
    ) -> None:
        await self.store.activate_guild(222, 223, 224)
        for guild_id, user_id in ((111, 701), (111, 702), (222, 703)):
            await self.store.finish_flow(
                guild_id,
                user_id,
                f"Hero{user_id}",
                f"{PREFIX}/hero-{user_id}",
                Character(name=f"Hero{user_id}"),
            )
        first_config = await self.store.get_guild(111)
        first_channel = MagicMock(spec=discord.TextChannel)
        first_channel.send = AsyncMock()
        first_panel = MagicMock(spec=discord.Message)
        self.mark_owned(first_panel, "guild-armory-panel-111.png")
        first_panel.add_reaction = AsyncMock()
        if operation == "fetch":
            first_channel.fetch_message = AsyncMock(side_effect=error)
        elif operation in {"upsert", "upsert_without_fetch"}:
            if operation == "upsert_without_fetch":
                del first_channel.fetch_message
            else:
                foreign_panel = MagicMock(spec=discord.Message)
                foreign_panel.author = SimpleNamespace(id=2)
                foreign_panel.attachments = []
                first_channel.fetch_message = AsyncMock(return_value=foreign_panel)
            self.bot.upsert_panel = AsyncMock(side_effect=error)
        else:
            first_channel.fetch_message = AsyncMock(return_value=first_panel)
            first_panel.add_reaction = AsyncMock(side_effect=error)

        second_channel = MagicMock(spec=discord.TextChannel)
        second_channel.send = AsyncMock()
        second_panel = MagicMock(spec=discord.Message)
        self.mark_owned(second_panel, "guild-armory-panel-222.png")
        second_panel.add_reaction = AsyncMock()
        second_channel.fetch_message = AsyncMock(return_value=second_panel)

        async def channel_for(channel_id: int):
            return {222: first_channel, 223: second_channel}[channel_id]

        self.bot.get_armory_channel = AsyncMock(side_effect=channel_for)
        self.bot.sync_member_row = AsyncMock(
            return_value=MemberRowSyncOutcome.UPDATED
        )
        with patch("wow_forever_armory.discord_bot._LOGGER") as logger:
            await self.bot.on_ready()

        self.assertEqual(self.bot.sync_member_row.await_count, 3)
        self.assertEqual(self.bot.get_armory_channel.await_count, 2)
        self.assertFalse(self.bot._recovered)
        expected_event = (
            "guild_armory_reaction_restore_deferred"
            if operation == "reaction"
            else "guild_armory_panel_recovery_deferred"
        )
        self.assertTrue(
            any(
                call.args[1] == expected_event
                and call.kwargs["extra"]["reason"] == reason
                for call in logger.log.call_args_list
            ),
            logger.log.call_args_list,
        )
        completed = next(
            call
            for call in logger.error.call_args_list
            if call.args[0] == "guild_armory_recovery_completed"
        )
        self.assertEqual(
            completed.kwargs["extra"],
            {
                "guild_count": 2,
                "guilds_recovered": 1,
                "guilds_incomplete": 1,
                "guilds_deferred": 1,
                "panels_recovered": 1,
                "member_rows_recovered": 3,
                "member_rows_created": 0,
                "member_rows_removed": 0,
                "members_skipped": 0,
                "member_rows_deferred": 0,
                "member_state_load_failures": 0,
                "outcome": "incomplete",
            },
        )
        first_channel.send.assert_not_awaited()
        second_channel.send.assert_not_awaited()
        if operation in {"upsert", "upsert_without_fetch"}:
            self.bot.upsert_panel.assert_awaited_once_with(
                111, first_channel, first_config
            )

    async def test_panel_fetch_timeout_is_deferred_and_other_work_continues(self):
        await self._assert_panel_operation_failure_is_deferred(
            "fetch", TimeoutError("private panel timeout"), "panel_fetch"
        )

    async def test_panel_fetch_api_failure_is_deferred_separately(self):
        error = discord.HTTPException(
            SimpleNamespace(status=503, reason="Unavailable"), "private response"
        )
        await self._assert_panel_operation_failure_is_deferred(
            "fetch", error, "panel_fetch"
        )

    async def test_panel_upsert_timeout_is_deferred_and_other_work_continues(self):
        await self._assert_panel_operation_failure_is_deferred(
            "upsert", TimeoutError("private panel timeout"), "panel_upsert"
        )

    async def test_panel_upsert_api_failure_is_deferred_separately(self):
        error = discord.HTTPException(
            SimpleNamespace(status=503, reason="Unavailable"), "private response"
        )
        await self._assert_panel_operation_failure_is_deferred(
            "upsert", error, "panel_upsert"
        )

    async def test_panel_upsert_without_fetch_method_classifies_timeout_as_deferred(
        self,
    ):
        await self._assert_panel_operation_failure_is_deferred(
            "upsert_without_fetch",
            TimeoutError("private panel timeout"),
            "panel_upsert",
        )

    async def test_sword_reaction_timeout_is_deferred_and_other_work_continues(self):
        await self._assert_panel_operation_failure_is_deferred(
            "reaction", TimeoutError("private reaction timeout"), "reaction_restore"
        )

    async def test_sword_reaction_api_failure_is_deferred_separately(self):
        error = discord.HTTPException(
            SimpleNamespace(status=503, reason="Unavailable"), "private response"
        )
        await self._assert_panel_operation_failure_is_deferred(
            "reaction", error, "reaction_restore"
        )

    async def test_channel_lookup_discord_timeout_and_unexpected_errors_classify(self):
        config = await self.store.get_guild(111)
        cases = (
            (discord.DiscordException("private discord error"), True),
            (TimeoutError("private timeout detail"), True),
            (RuntimeError("private unexpected detail"), False),
        )
        for error, deferred in cases:
            with self.subTest(error_type=type(error).__name__):
                self.bot.get_armory_channel = AsyncMock(side_effect=error)
                result = await self.bot._recover_guild(config)
                self.assertFalse(result.recovered)
                self.assertEqual(result.deferred, deferred)

    async def test_multiple_guild_recovery_continues_and_aggregates_partial_failure(
        self,
    ) -> None:
        second_guild = 222
        await self.store.activate_guild(second_guild, 223, 224)
        configs = await self.store.guilds()
        self.bot.store.guilds = AsyncMock(return_value=configs)
        self.bot._recover_guild = AsyncMock(
            side_effect=[
                GuildRecoveryResult(
                    True,
                    panels_recovered=1,
                    member_rows_recovered=1,
                    member_rows_created=1,
                    member_rows_removed=1,
                    members_skipped=1,
                ),
                GuildRecoveryResult(
                    False,
                    member_rows_deferred=2,
                    member_state_load_failures=1,
                    deferred=True,
                ),
            ]
        )
        with patch("wow_forever_armory.discord_bot._LOGGER") as logger:
            await self.bot.on_ready()
        self.assertEqual(self.bot._recover_guild.await_count, 2)
        self.assertFalse(self.bot._recovered)
        completed = next(
            call
            for call in logger.error.call_args_list
            if call.args[0] == "guild_armory_recovery_completed"
        )
        self.assertEqual(
            completed.kwargs["extra"],
            {
                "guild_count": 2,
                "guilds_recovered": 1,
                "guilds_incomplete": 1,
                "guilds_deferred": 1,
                "panels_recovered": 1,
                "member_rows_recovered": 1,
                "member_rows_created": 1,
                "member_rows_removed": 1,
                "members_skipped": 1,
                "member_rows_deferred": 2,
                "member_state_load_failures": 1,
                "outcome": "incomplete",
            },
        )

    async def test_discord_timeout_and_unexpected_member_errors_continue_recovery(
        self,
    ) -> None:
        first = await self.store.finish_flow(
            111, 621, "First", f"{PREFIX}/first", Character(name="First")
        )
        second = await self.store.finish_flow(
            111, 622, "Second", f"{PREFIX}/second", Character(name="Second")
        )
        panel = MagicMock(spec=discord.Message)
        self.mark_owned(panel, "guild-armory-panel-111.png")
        panel.add_reaction = AsyncMock()
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(return_value=panel)
        self.bot.get_armory_channel = AsyncMock(return_value=channel)
        for error in (
            discord.DiscordException("private Discord response"),
            TimeoutError("private timeout response"),
            RuntimeError("private unexpected response"),
        ):
            with self.subTest(error_type=type(error).__name__):
                self.bot.sync_member_row = AsyncMock(
                    side_effect=[error, MemberRowSyncOutcome.UPDATED]
                )
                with self.assertLogs(
                    "wow_forever_armory.discord_bot", level="WARNING"
                ) as logs:
                    result = await self.bot._recover_guild(
                        await self.store.get_guild(111)
                    )
                self.assertFalse(result.recovered)
                self.assertEqual(result.member_rows_deferred, 1)
                self.assertEqual(result.member_rows_recovered, 1)
                self.assertEqual(self.bot.sync_member_row.await_count, 2)
                self.assertNotIn(str(first.user_id), "\n".join(logs.output))
                self.assertNotIn(str(second.user_id), "\n".join(logs.output))

    async def test_concurrent_row_updates_send_one_message_and_edit_it(self) -> None:
        await self.store.finish_flow(
            111, 456, "Hero", f"{PREFIX}/hero", Character(name="Hero", level=60)
        )
        state = await self.store.member(111, 456)
        sent = MagicMock(spec=discord.Message)
        sent.id = 900
        self.mark_owned(sent, "guild-armory-111-456.png")
        sent.edit = AsyncMock()
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(return_value=sent)
        channel.send = AsyncMock(return_value=sent)
        self.bot.get_armory_channel = AsyncMock(return_value=channel)
        with patch(
            "wow_forever_armory.discord_bot.render_stored_member_overview",
            return_value=b"PNG",
        ):
            await asyncio.gather(
                self.bot.sync_member_row(state), self.bot.sync_member_row(state)
            )
        channel.send.assert_awaited_once()
        sent.edit.assert_awaited_once()
        self.assertEqual((await self.store.member(111, 456)).row_message_id, 900)
        self.assertEqual(self.render_runner.await_count, 2)

    async def test_missing_saved_id_reuses_own_image_and_removes_exact_duplicates(
        self,
    ) -> None:
        await self.store.finish_flow(
            111, 456, "Hero", f"{PREFIX}/hero", Character(name="Hero", level=60)
        )
        state = await self.store.member(111, 456)
        self.bot._connection.user = SimpleNamespace(id=1)
        reused = SimpleNamespace(
            id=901,
            author=SimpleNamespace(id=1),
            attachments=[SimpleNamespace(filename="guild-armory-111-456.png")],
            edit=AsyncMock(),
        )
        duplicate = SimpleNamespace(
            id=902,
            author=SimpleNamespace(id=1),
            attachments=[SimpleNamespace(filename="guild-armory-111-456.png")],
            delete=AsyncMock(),
        )

        async def history(*, limit: int):
            self.assertEqual(limit, 500)
            for item in (reused, duplicate):
                yield item

        channel = MagicMock(spec=discord.TextChannel)
        channel.history = history
        channel.fetch_message = AsyncMock()
        channel.send = AsyncMock()
        self.bot.get_armory_channel = AsyncMock(return_value=channel)
        with patch(
            "wow_forever_armory.discord_bot.render_stored_member_overview",
            return_value=b"PNG",
        ):
            await self.bot.sync_member_row(state)
        reused.edit.assert_awaited_once()
        duplicate.delete.assert_awaited_once()
        channel.send.assert_not_awaited()
        self.assertEqual((await self.store.member(111, 456)).row_message_id, 901)

    async def test_empty_row_cleanup_never_deletes_another_authors_message(
        self,
    ) -> None:
        await self.store.finish_flow(
            111, 456, "Hero", f"{PREFIX}/hero", Character(name="Hero", level=60)
        )
        await self.store.set_row_message(111, 456, 999)
        state = await self.store.remove_character(111, 456, f"{PREFIX}/hero")
        self.bot._connection.user = SimpleNamespace(id=1)
        foreign_message = SimpleNamespace(
            id=999,
            author=SimpleNamespace(id=2),
            attachments=[SimpleNamespace(filename="unrelated.png")],
            delete=AsyncMock(),
        )

        async def history(*, limit: int):
            yield foreign_message

        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(return_value=foreign_message)
        channel.history = history
        self.bot.get_armory_channel = AsyncMock(return_value=channel)
        await self.bot.sync_member_row(state)
        foreign_message.delete.assert_not_awaited()
        self.assertIsNone((await self.store.member(111, 456)).row_message_id)

    async def test_foreign_row_with_expected_filename_is_never_edited(self) -> None:
        await self.store.finish_flow(
            111, 456, "Hero", f"{PREFIX}/hero", Character(name="Hero", level=60)
        )
        await self.store.set_row_message(111, 456, 999)
        state = await self.store.member(111, 456)
        self.bot._connection.user = SimpleNamespace(id=1)
        foreign_message = SimpleNamespace(
            id=999,
            author=SimpleNamespace(id=2),
            attachments=[SimpleNamespace(filename="guild-armory-111-456.png")],
            edit=AsyncMock(),
            delete=AsyncMock(),
        )

        async def history(*, limit: int):
            self.assertEqual(limit, 500)
            yield foreign_message

        created = MagicMock(spec=discord.Message)
        created.id = 1000
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(return_value=foreign_message)
        channel.history = history
        channel.send = AsyncMock(return_value=created)
        self.bot.get_armory_channel = AsyncMock(return_value=channel)
        with patch(
            "wow_forever_armory.discord_bot.render_stored_member_overview",
            return_value=b"PNG",
        ):
            await self.bot.sync_member_row(state)
        foreign_message.edit.assert_not_awaited()
        foreign_message.delete.assert_not_awaited()
        channel.send.assert_awaited_once()

    async def test_missing_bot_identity_never_edits_panel_or_member_image(self) -> None:
        await self.store.finish_flow(
            111, 456, "Hero", f"{PREFIX}/hero", Character(name="Hero", level=60)
        )
        await self.store.set_row_message(111, 456, 888)
        state = await self.store.member(111, 456)
        foreign_panel = MagicMock(spec=discord.Message)
        foreign_panel.id = 333
        foreign_panel.edit = AsyncMock()
        foreign_panel.add_reaction = AsyncMock()
        foreign_row = MagicMock(spec=discord.Message)
        foreign_row.id = 888
        foreign_row.edit = AsyncMock()
        foreign_row.delete = AsyncMock()
        new_panel = MagicMock(spec=discord.Message)
        new_panel.id = 444
        new_panel.add_reaction = AsyncMock()
        new_row = MagicMock(spec=discord.Message)
        new_row.id = 889
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 222
        channel.fetch_message = AsyncMock(side_effect=[foreign_panel, foreign_row])
        channel.send = AsyncMock(side_effect=[new_panel, new_row])
        self.bot.get_armory_channel = AsyncMock(return_value=channel)
        with (
            patch(
                "wow_forever_armory.discord_bot.render_intro_panel", return_value=b"PNG"
            ),
            patch(
                "wow_forever_armory.discord_bot.render_stored_member_overview",
                return_value=b"PNG",
            ),
        ):
            await self.bot.upsert_panel(111, channel, await self.store.get_guild(111))
            with self.assertRaises(BotIdentityUnavailable):
                await self.bot.sync_member_row(state)
        foreign_panel.edit.assert_not_awaited()
        foreign_panel.add_reaction.assert_not_awaited()
        foreign_row.edit.assert_not_awaited()
        foreign_row.delete.assert_not_awaited()
        self.assertEqual(channel.send.await_count, 1)
        self.assertEqual((await self.store.member(111, 456)).row_message_id, 888)

    async def test_registered_row_defers_without_identity_then_syncs_when_restored(
        self,
    ) -> None:
        await self.store.finish_flow(
            111, 456, "Hero", f"{PREFIX}/hero", Character(name="Hero", level=60)
        )
        await self.store.set_row_message(111, 456, 888)
        state = await self.store.member(111, 456)
        owned = MagicMock(spec=discord.Message)
        owned.id = 888
        owned.edit = AsyncMock()
        self.mark_owned(owned, "guild-armory-111-456.png")
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(return_value=owned)
        channel.send = AsyncMock()
        self.bot.get_armory_channel = AsyncMock(return_value=channel)
        self.bot._connection.user = None

        with patch(
            "wow_forever_armory.discord_bot.render_stored_member_overview",
            return_value=b"PNG",
        ):
            with self.assertRaisesRegex(
                BotIdentityUnavailable, "member row sync deferred"
            ):
                await self.bot.sync_member_row(state)
            channel.fetch_message.assert_not_awaited()
            channel.send.assert_not_awaited()
            owned.edit.assert_not_awaited()
            self.mark_owned(owned, "guild-armory-111-456.png")
            await self.bot.sync_member_row(state)
        owned.edit.assert_awaited_once()
        channel.send.assert_not_awaited()
        self.assertEqual(owned.edit.await_args.kwargs["content"], "<@456>")
        self.assertFalse(owned.edit.await_args.kwargs["allowed_mentions"].users)

    async def test_new_member_row_mentions_its_owner_once(self) -> None:
        await self.store.finish_flow(
            111, 456, "Hero", f"{PREFIX}/hero", Character(name="Hero", level=60)
        )
        state = await self.store.member(111, 456)
        self.bot._connection.user = SimpleNamespace(id=1)
        channel = MagicMock(spec=discord.TextChannel)
        self.bot._find_attachment_message = AsyncMock(return_value=None)
        channel.send = AsyncMock(return_value=MagicMock(id=889))
        self.bot.get_armory_channel = AsyncMock(return_value=channel)
        with patch(
            "wow_forever_armory.discord_bot.render_stored_member_overview",
            return_value=b"PNG",
        ):
            await self.bot.sync_member_row(state)
        channel.send.assert_awaited_once()
        kwargs = channel.send.await_args.kwargs
        self.assertEqual(kwargs["content"], "<@456>")
        allowed = kwargs["allowed_mentions"]
        self.assertEqual([user.id for user in allowed.users], [456])
        self.assertFalse(allowed.everyone)
        self.assertFalse(allowed.roles)

    async def test_empty_row_is_untouched_when_bot_identity_is_missing(self) -> None:
        await self.store.finish_flow(
            111, 456, "Hero", f"{PREFIX}/hero", Character(name="Hero", level=60)
        )
        await self.store.set_row_message(111, 456, 888)
        state = await self.store.remove_character(111, 456, f"{PREFIX}/hero")
        existing = MagicMock(spec=discord.Message)
        existing.id = 888
        existing.delete = AsyncMock()
        self.mark_owned(existing, "guild-armory-111-456.png")
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(return_value=existing)
        channel.send = AsyncMock()
        self.bot.get_armory_channel = AsyncMock(return_value=channel)
        self.bot._connection.user = None

        with self.assertRaises(BotIdentityUnavailable):
            await self.bot.sync_member_row(state)
        self.bot.get_armory_channel.assert_not_awaited()
        channel.fetch_message.assert_not_awaited()
        channel.send.assert_not_awaited()
        existing.delete.assert_not_awaited()
        self.assertEqual((await self.store.member(111, 456)).row_message_id, 888)

    async def test_panel_with_foreign_author_is_never_edited_or_reacted_to(
        self,
    ) -> None:
        self.bot._connection.user = SimpleNamespace(id=1)
        foreign_panel = SimpleNamespace(
            id=333,
            author=SimpleNamespace(id=2),
            attachments=[SimpleNamespace(filename="guild-armory-panel-111.png")],
            edit=AsyncMock(),
            add_reaction=AsyncMock(),
            delete=AsyncMock(),
        )
        created = MagicMock(spec=discord.Message)
        created.id = 444
        created.add_reaction = AsyncMock()

        async def history(*, limit: int):
            self.assertEqual(limit, 500)
            yield foreign_panel

        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 222
        channel.fetch_message = AsyncMock(return_value=foreign_panel)
        channel.history = history
        channel.send = AsyncMock(return_value=created)
        with patch(
            "wow_forever_armory.discord_bot.render_intro_panel", return_value=b"PNG"
        ):
            await self.bot.upsert_panel(111, channel, await self.store.get_guild(111))
        foreign_panel.edit.assert_not_awaited()
        foreign_panel.add_reaction.assert_not_awaited()
        foreign_panel.delete.assert_not_awaited()
        created.add_reaction.assert_awaited_once_with("⚔️")
        channel.send.assert_awaited_once()

    async def test_reaction_starts_one_private_flow_and_duplicate_is_idempotent(
        self,
    ) -> None:
        dm = AsyncMock()
        dm.send.return_value = MagicMock(id=777)
        user = MagicMock()
        user.create_dm = AsyncMock(return_value=dm)
        self.bot.fetch_user = AsyncMock(return_value=user)
        payload = self.payload()
        await self.bot.on_raw_reaction_add(payload)
        await self.bot.on_raw_reaction_add(payload)
        dm.send.assert_awaited_once()
        self.assertEqual(await self.store.flow_message(111, 456), 777)
        embed = dm.send.await_args.kwargs["embed"]
        view = dm.send.await_args.kwargs["view"]
        self.assertIn("Add & Edit", embed.description)
        self.assertIn("Armory (EU)", embed.description)
        self.assertIsInstance(view, GuildPanelView)

    async def test_closed_dm_keeps_panel_modal_as_private_fallback(self) -> None:
        user = MagicMock()
        user.create_dm = AsyncMock(
            side_effect=discord.Forbidden(
                MagicMock(status=403, reason="Forbidden"), "closed"
            )
        )
        self.bot.fetch_user = AsyncMock(return_value=user)
        await self.bot.on_raw_reaction_add(self.payload())
        self.assertEqual(await self.store.begin_flow(111, 456, 333), "already_pending")
        view = GuildPanelView(self.bot, 111)
        self.assertEqual(view.children[0].label, "Add & Edit")

    async def test_wrong_reaction_and_wrong_panel_do_nothing(self) -> None:
        self.bot.fetch_user = AsyncMock()
        await self.bot.on_raw_reaction_add(
            SimpleNamespace(
                guild_id=111,
                user_id=456,
                message_id=333,
                emoji=SimpleNamespace(name="👍"),
            )
        )
        await self.bot.on_raw_reaction_add(
            SimpleNamespace(
                guild_id=111,
                user_id=456,
                message_id=444,
                emoji=SimpleNamespace(name="⚔️"),
            )
        )
        self.bot.fetch_user.assert_not_awaited()

    async def test_submission_persists_profile_and_scopes_to_selected_guild(
        self,
    ) -> None:
        await self.store.activate_guild(222, 223, 334)
        self.core.fetch_character = AsyncMock(
            return_value=Character(name="Éowyn", level=60, character_class="Druid")
        )
        member = MagicMock()
        member.id = 456
        member.display_name = "Éowyn"
        guild = MagicMock()
        self.bot._verify_member = AsyncMock(return_value=(guild, member))
        self.bot.sync_member_row = AsyncMock()
        self.bot.update_member_dm = AsyncMock()
        interaction = self.interaction(guild_id=222)
        url = PANEL_URL + "eowyn"
        await self.bot.submit_character_url(interaction, 222, url)
        self.core.fetch_character.assert_awaited_once_with(
            "en-us/forever/eu/armory/character/eowyn"
        )
        state = await self.store.member(222, 456)
        self.assertEqual(state.characters[0].character.name, "Éowyn")
        self.assertIsNone(await self.store.member(111, 456))
        self.bot._verify_member.assert_awaited_once_with(222, 456)
        self.bot.sync_member_row.assert_awaited_once_with(
            state, fresh_paths=frozenset({state.characters[0].path})
        )
        self.assertIn(
            "saved", interaction.edit_original_response.await_args.kwargs["content"]
        )

    async def test_three_limit_duplicates_and_replace_are_reported_privately(
        self,
    ) -> None:
        member = MagicMock()
        member.id = 456
        member.display_name = "Hero"
        self.bot._verify_member = AsyncMock(return_value=(MagicMock(), member))
        self.bot.sync_member_row = AsyncMock()
        self.bot.update_member_dm = AsyncMock()
        self.core.fetch_character = AsyncMock()

        async def fetch(path: str) -> Character:
            return Character(name=path.rsplit("/", 1)[-1], level=60)

        self.core.fetch_character.side_effect = fetch
        urls = [PANEL_URL + f"hero-{index}" for index in range(4)]
        for url in urls[:3]:
            await self.bot.submit_character_url(self.interaction(), 111, url)
        duplicate_interaction = self.interaction()
        await self.bot.submit_character_url(duplicate_interaction, 111, urls[0])
        self.assertIn(
            "already",
            duplicate_interaction.edit_original_response.await_args.kwargs["content"],
        )
        fourth = self.interaction()
        await self.bot.submit_character_url(fourth, 111, urls[3])
        self.assertIn(
            "three characters",
            fourth.edit_original_response.await_args.kwargs["content"],
        )
        replacement = self.interaction()
        await self.bot.submit_character_url(
            replacement,
            111,
            urls[3],
            replace_path="en-us/forever/eu/armory/character/hero-1",
        )
        self.assertIn(
            "saved", replacement.edit_original_response.await_args.kwargs["content"]
        )
        state = await self.store.member(111, 456)
        self.assertEqual(len(state.characters), 3)
        self.assertNotIn("hero-1", [entry.path for entry in state.characters])

    async def test_invalid_private_url_does_not_log_or_echo_url(self) -> None:
        self.bot._verify_member = AsyncMock()
        interaction = self.interaction()
        unsafe = "https://evil.example/private-token"
        await self.bot.submit_character_url(interaction, 111, unsafe)
        interaction.response.defer.assert_awaited_once()
        interaction.edit_original_response.assert_awaited_once()
        text = interaction.edit_original_response.await_args.kwargs["content"]
        self.assertNotIn(unsafe, text)
        self.bot._verify_member.assert_not_awaited()

    async def test_member_row_edits_existing_message_attachment_instead_of_sending(
        self,
    ) -> None:
        state = await self.store.finish_flow(
            111,
            456,
            "Hero",
            f"{PREFIX}/hero",
            Character(name="Hero", level=60),
        )
        await self.store.set_row_message(111, 456, 888)
        state = await self.store.member(111, 456)
        message = MagicMock(spec=discord.Message)
        message.id = 888
        self.mark_owned(message, "guild-armory-111-456.png")
        message.edit = AsyncMock()
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(return_value=message)
        channel.send = AsyncMock()
        self.bot.get_armory_channel = AsyncMock(return_value=channel)
        with patch(
            "wow_forever_armory.discord_bot.render_stored_member_overview",
            return_value=b"PNG",
        ):
            await self.bot.sync_member_row(state)
        message.edit.assert_awaited_once()
        self.assertEqual(len(message.edit.await_args.kwargs["attachments"]), 1)
        channel.send.assert_not_awaited()
        self.assertEqual((await self.store.member(111, 456)).row_message_id, 888)

    async def test_row_renderer_receives_discord_identity_and_fresh_character_path(
        self,
    ) -> None:
        path = f"{PREFIX}/hero"
        await self.store.finish_flow(
            111, 456, "StoredDisplayName", path, Character(name="Hero", level=60)
        )
        await self.store.set_row_message(111, 456, 888)
        state = await self.store.member(111, 456)
        message = MagicMock(spec=discord.Message)
        message.id = 888
        self.mark_owned(message, "guild-armory-111-456.png")
        message.edit = AsyncMock()
        channel = MagicMock(spec=discord.TextChannel)
        channel.fetch_message = AsyncMock(return_value=message)
        self.bot.get_armory_channel = AsyncMock(return_value=channel)
        discord_member = SimpleNamespace(display_name="Current Discord Name")
        guild = MagicMock()
        guild.get_member.return_value = discord_member
        self.bot.get_guild = MagicMock(return_value=guild)

        with patch(
            "wow_forever_armory.discord_bot.render_stored_member_overview",
            return_value=b"PNG",
        ):
            await self.bot.sync_member_row(state, fresh_paths=frozenset({path}))

        self.assertEqual(
            self.render_runner.await_args.kwargs["display_name"],
            "Current Discord Name",
        )
        self.assertEqual(
            self.render_runner.await_args.kwargs["fresh_paths"], frozenset({path})
        )
        message.edit.assert_awaited_once()

    async def test_missing_row_message_is_recreated_once(self) -> None:
        await self.store.finish_flow(
            111,
            456,
            "Hero",
            f"{PREFIX}/hero",
            Character(name="Hero", level=60),
        )
        await self.store.set_row_message(111, 456, 888)
        state = await self.store.member(111, 456)
        self.bot._connection.user = SimpleNamespace(id=1)

        async def history(*, limit: int):
            self.assertEqual(limit, 500)
            if False:
                yield None

        channel = MagicMock(spec=discord.TextChannel)
        channel.history = history
        channel.fetch_message = AsyncMock(
            side_effect=discord.NotFound(
                MagicMock(status=404, reason="Unknown Message"), "deleted"
            )
        )
        sent = MagicMock(spec=discord.Message)
        sent.id = 889
        channel.send = AsyncMock(return_value=sent)
        self.bot.get_armory_channel = AsyncMock(return_value=channel)
        with patch(
            "wow_forever_armory.discord_bot.render_stored_member_overview",
            return_value=b"PNG",
        ):
            await self.bot.sync_member_row(state)
        channel.send.assert_awaited_once()
        self.assertEqual((await self.store.member(111, 456)).row_message_id, 889)

    async def test_dm_submit_rechecks_membership_and_rejects_cross_guild_member(
        self,
    ) -> None:
        self.bot._verify_member = AsyncMock(return_value=None)
        self.core.fetch_character = AsyncMock()
        interaction = self.interaction(guild_id=None)
        await self.bot.submit_character_url(interaction, 111, PANEL_URL + "hero")
        self.bot._verify_member.assert_awaited_once_with(111, 456)
        self.core.fetch_character.assert_not_awaited()
        self.assertIn(
            "member", interaction.edit_original_response.await_args.kwargs["content"]
        )
