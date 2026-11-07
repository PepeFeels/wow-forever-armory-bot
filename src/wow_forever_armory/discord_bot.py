"""Guild Armory Discord application, event and persistence orchestration."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from asyncio import to_thread
from io import BytesIO
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from .client import ArmoryClient, ArmoryClientError, ArmoryHTTPError, ArmoryTimeoutError
from .graphic_renderer import render_stored_member_overview
from .guild_armory_images import render_intro_panel
from .guild_armory_store import (
    CharacterLimitReached,
    CharacterNotFound,
    DuplicateCharacter,
    GuildArmoryStore,
    GuildConfig,
    MemberState,
)
from .guild_armory_urls import validate_armory_url
from .guild_armory_views import CharacterSelectView, GuildPanelView
from .logging_config import configure_logging
from .operations import (
    LockUnavailable,
    database_file_lock_path,
    exclusive_file_lock,
    restore_marker_path,
    runtime_lock_path,
)
from .parser import ArmoryParseError

_LOGGER = logging.getLogger(__name__)
_SWORD = "⚔️"
_LOCK_STRIPES = 32
_MAX_TOKEN_FILE_BYTES = 4096


class BotIdentityUnavailable(RuntimeError):
    """Discord row synchronization cannot run without the authenticated bot user."""


class GuildArmoryCommands(commands.Cog):
    """Guild-scoped administration and private character management commands."""

    def __init__(self, bot: ArmoryBot) -> None:
        self.bot = bot

    armory = app_commands.Group(name="armory", description="Guild character armory")

    @armory.command(
        name="activate", description="Activate the Guild Armory in this channel"
    )
    @app_commands.guild_only()
    async def activate(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        guild_id = interaction.guild_id
        permissions = getattr(interaction.user, "guild_permissions", None)
        if (
            guild is None
            or guild_id is None
            or permissions is None
            or not (permissions.manage_guild or permissions.administrator)
        ):
            await interaction.response.send_message(
                "Tjänsten kan bara aktiveras av en serveradministratör.",
                ephemeral=True,
            )
            return
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "Välj en kanal där boten får skicka meddelanden.", ephemeral=True
            )
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.DiscordException, TimeoutError:
            return
        async with self.bot.activation_lock(guild_id):
            try:
                config = await self.bot.store.get_guild(guild_id)
                target_channel: discord.TextChannel | discord.Thread = channel
                if config is not None:
                    active_channel = await self.bot.get_armory_channel(
                        config.channel_id
                    )
                    if active_channel is None:
                        await self.bot.edit_ephemeral(
                            interaction,
                            "Den aktiva kanalen hittas inte. Kontakta en "
                            "administratör innan du aktiverar på nytt.",
                        )
                        return
                    target_channel = active_channel
                panel = await self.bot.upsert_panel(guild_id, target_channel, config)
                await self.bot.edit_ephemeral(
                    interaction,
                    f"Guild Armory är aktiv i {target_channel.mention}. "
                    f"Panel: {panel.jump_url}",
                )
            except discord.Forbidden, discord.HTTPException:
                _LOGGER.warning(
                    "Guild Armory activation failed due to Discord permissions/API"
                )
                await self.bot.edit_ephemeral(
                    interaction,
                    "Panelen kunde inte uppdateras. Kontrollera botens "
                    "kanalbehörigheter och försök igen.",
                )
            except Exception:
                _LOGGER.exception("Unexpected Guild Armory activation failure")
                await self.bot.edit_ephemeral(
                    interaction, "Aktiveringen misslyckades. Försök igen senare."
                )
            else:
                _LOGGER.info("guild_armory_activated")

    @armory.command(
        name="manage", description="Hantera dina Guild Armory-karaktärer privat"
    )
    @app_commands.guild_only()
    async def manage(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Öppna hanteringen från den server där karaktären registrerades.",
                ephemeral=True,
            )
            return
        await self.bot.open_management(interaction, interaction.guild_id)


class ArmoryBot(commands.Bot):
    """Discord adapter; Core handles all Armory networking and parsing."""

    def __init__(
        self,
        armory_client: ArmoryClient,
        store: GuildArmoryStore,
        test_guild_id: int | None = None,
    ) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=self._intents(),
        )
        self.armory_client = armory_client
        self.store = store
        self.test_guild_id = test_guild_id
        self.armory_semaphore = asyncio.Semaphore(3)
        self.graphic_semaphore = asyncio.Semaphore(2)
        self._member_locks = tuple(asyncio.Lock() for _ in range(_LOCK_STRIPES))
        self._row_locks = tuple(asyncio.Lock() for _ in range(_LOCK_STRIPES))
        self._activation_locks = tuple(asyncio.Lock() for _ in range(_LOCK_STRIPES))
        self._recovered = False
        self._recovery_lock = asyncio.Lock()

    @staticmethod
    def _intents() -> discord.Intents:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_reactions = True
        return intents

    async def setup_hook(self) -> None:
        _LOGGER.info("discord_bot_setup_started")
        await self.store.initialize()
        await self.add_cog(GuildArmoryCommands(self))
        configs = await self.store.guilds()
        for config in configs:
            self.add_view(GuildPanelView(self, config.guild_id))
        if self.test_guild_id is not None:
            test_guild = discord.Object(id=self.test_guild_id)
            self.tree.copy_global_to(guild=test_guild)
            await self.tree.sync(guild=test_guild)
            _LOGGER.info("slash_commands_synced_to_test_guild")
        else:
            synced = await self.tree.sync()
            _LOGGER.info("global_slash_commands_synced", extra={"count": len(synced)})

    def activation_lock(self, guild_id: int) -> asyncio.Lock:
        return self._activation_locks[guild_id % _LOCK_STRIPES]

    def member_lock(self, guild_id: int, user_id: int) -> asyncio.Lock:
        index = (guild_id ^ (user_id * 31)) % _LOCK_STRIPES
        return self._member_locks[index]

    def row_lock(self, guild_id: int, user_id: int) -> asyncio.Lock:
        index = (guild_id ^ (user_id * 31)) % _LOCK_STRIPES
        return self._row_locks[index]

    def guild_label(self, guild_id: int) -> str:
        guild = self.get_guild(guild_id)
        name = guild.name if guild is not None else f"server {guild_id}"
        return discord.utils.escape_markdown(name).replace("@", "@\u200b")[:100]

    async def get_armory_channel(
        self, channel_id: int
    ) -> discord.TextChannel | discord.Thread | None:
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.NotFound:
                return None
        return (
            channel
            if isinstance(channel, (discord.TextChannel, discord.Thread))
            else None
        )

    async def upsert_panel(
        self,
        guild_id: int,
        channel: discord.TextChannel | discord.Thread,
        config: GuildConfig | None,
    ) -> discord.Message:
        view = GuildPanelView(self, guild_id)
        panel: discord.Message | None = None
        fetch_message = getattr(channel, "fetch_message", None)
        if config is not None and config.channel_id == getattr(channel, "id", None):
            if fetch_message is not None:
                try:
                    panel = await fetch_message(config.panel_message_id)
                except discord.NotFound:
                    panel = None
                if panel is not None and not self._is_bot_attachment(
                    panel, f"guild-armory-panel-{guild_id}.png"
                ):
                    panel = None
        if panel is None:
            panel = await self._find_attachment_message(
                channel, f"guild-armory-panel-{guild_id}.png"
            )
        image_data = render_intro_panel()
        file = discord.File(
            BytesIO(image_data), filename=f"guild-armory-panel-{guild_id}.png"
        )
        if panel is None:
            panel = await channel.send(
                file=file,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await panel.edit(
                attachments=[file],
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        if config is None or config.panel_message_id != panel.id:
            channel_id = getattr(channel, "id", None)
            if channel_id is None:
                raise RuntimeError("Armory channel does not have an ID")
            await self.store.activate_guild(guild_id, channel_id, panel.id)
        try:
            await panel.add_reaction(_SWORD)
        except discord.DiscordException:
            _LOGGER.warning("Guild Armory panel reaction could not be added")
        return panel

    async def _find_attachment_message(
        self, channel: discord.TextChannel | discord.Thread, filename: str
    ) -> discord.Message | None:
        if self.user is None:
            return None
        matches: list[discord.Message] = []
        async for message in channel.history(limit=500):
            if message.author.id == self.user.id and any(
                attachment.filename == filename for attachment in message.attachments
            ):
                matches.append(message)
        if not matches:
            return None
        for duplicate in matches[1:]:
            try:
                await duplicate.delete()
            except discord.DiscordException:
                _LOGGER.warning("Duplicate Guild Armory image could not be removed")
        return matches[0]

    async def edit_ephemeral(self, interaction: discord.Interaction, text: str) -> None:
        try:
            await interaction.edit_original_response(content=text)
        except discord.DiscordException, TimeoutError:
            _LOGGER.warning("Guild Armory interaction response could not be edited")

    async def on_raw_reaction_add(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        if payload.guild_id is None or payload.user_id == (
            self.user.id if self.user else None
        ):
            return
        if payload.emoji.name not in {"⚔", "⚔️"}:
            return
        try:
            config = await self.store.get_guild_by_panel(payload.message_id)
            if config is None or config.guild_id != payload.guild_id:
                return
            result = await self.store.begin_flow(
                config.guild_id, payload.user_id, config.panel_message_id
            )
            if result != "started":
                return
            user = self.get_user(payload.user_id) or await self.fetch_user(
                payload.user_id
            )
            dm = await user.create_dm()
            embed = discord.Embed(
                title="Registrera i Guild Armory",
                description=(
                    f"Server: **{self.guild_label(config.guild_id)}**. "
                    "Öppna formuläret med knappen och klistra in hela din "
                    "karaktärslänk från WoW Armory. Länken skickas privat. "
                    "Du kan registrera upp till tre karaktärer. "
                    "Använd länken WoW Forever Armory (EU) nedan för att hitta "
                    "din karaktär."
                ),
                color=discord.Color.blurple(),
            )
            message = await dm.send(
                embed=embed,
                view=GuildPanelView(self, config.guild_id),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.store.set_flow_message(
                config.guild_id, payload.user_id, message.id
            )
        except discord.Forbidden, discord.NotFound:
            # The persistent panel's private modal is the fallback when DMs are closed.
            return
        except discord.HTTPException, TimeoutError:
            _LOGGER.warning("Guild Armory reaction flow could not start")
        except Exception:
            _LOGGER.exception("Unexpected Guild Armory reaction flow failure")

    async def _verify_member(
        self, guild_id: int, user_id: int
    ) -> tuple[discord.Guild, discord.Member] | None:
        guild = self.get_guild(guild_id)
        if guild is None:
            return None
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound, discord.Forbidden, discord.HTTPException:
            return None
        return guild, member

    async def submit_character_url(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        value: str,
        *,
        replace_path: str | None = None,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.DiscordException, TimeoutError:
            return
        result = "Registreringen misslyckades. Kontrollera länken och försök igen."
        try:
            path = validate_armory_url(value, self.armory_client)
            verified = await self._verify_member(guild_id, interaction.user.id)
            if verified is None:
                result = (
                    "Du behöver vara medlem i den här servern "
                    "för att ändra dess Armory."
                )
            else:
                guild, member = verified
                config = await self.store.get_guild(guild_id)
                if config is None:
                    result = "Guild Armory är inte längre aktiv i den här servern."
                else:
                    async with self.member_lock(guild_id, member.id):
                        if replace_path is None:
                            if await self.store.has_character(
                                guild_id, member.id, path
                            ):
                                _LOGGER.info(
                                    "character_registration_rejected_duplicate"
                                )
                                result = (
                                    "Den här karaktären finns redan i din Guild Armory."
                                )
                                await self.edit_ephemeral(interaction, result)
                                return
                            flow = await self.store.begin_flow(
                                guild_id,
                                member.id,
                                config.panel_message_id,
                                allow_registered=True,
                            )
                            if flow == "already_full":
                                _LOGGER.info("character_registration_rejected_limit")
                                result = (
                                    "Du har redan tre karaktärer. Ta bort en "
                                    "innan du lägger till fler."
                                )
                                await self.edit_ephemeral(interaction, result)
                                return
                            if flow == "unconfigured":
                                result = "Guild Armory är inte aktiv."
                                await self.edit_ephemeral(interaction, result)
                                return
                        try:
                            async with self.armory_semaphore:
                                character = await asyncio.wait_for(
                                    self.armory_client.fetch_character(path), timeout=45
                                )
                        except TimeoutError:
                            result = (
                                "WoW Armory tog för lång tid på sig. "
                                "Försök igen senare."
                            )
                            await self.edit_ephemeral(interaction, result)
                            return
                        if character.name is None and character.level is None:
                            result = "Ingen karaktär hittades på den Armory-länken."
                            await self.edit_ephemeral(interaction, result)
                            return
                        pending_dm_id = await self.store.flow_message(
                            guild_id, member.id
                        )
                        state = await self.store.finish_flow(
                            guild_id,
                            member.id,
                            member.display_name,
                            path,
                            character,
                            replace_path=replace_path,
                        )
                        if replace_path is None:
                            _LOGGER.info(
                                "character_saved_to_database",
                                extra={
                                    "operation": "added",
                                    "character_count": len(state.characters),
                                },
                            )
                        else:
                            _LOGGER.info(
                                "character_saved_to_database",
                                extra={
                                    "operation": "replaced",
                                    "character_count": len(state.characters),
                                },
                            )
                        row_updated = True
                        try:
                            await self.sync_member_row(
                                state, fresh_paths=frozenset({path})
                            )
                        except BotIdentityUnavailable:
                            row_updated = False
                            _LOGGER.warning(
                                "member_image_update_deferred: bot identity unavailable"
                            )
                        except discord.DiscordException, OSError, TimeoutError:
                            row_updated = False
                            _LOGGER.warning(
                                "member_image_update_deferred: Discord unavailable"
                            )
                        except Exception:
                            row_updated = False
                            _LOGGER.exception("member_image_update_failed")
                        await self.update_member_dm(member, state, pending_dm_id)
                        if row_updated:
                            result = (
                                "Karaktären har sparats och raden har uppdaterats "
                                "i Guild Armory-kanalen."
                            )
                        else:
                            result = (
                                "Karaktären har sparats. Raden uppdateras "
                                "när kanalen är tillgänglig."
                            )
        except ArmoryParseError:
            result = (
                "WoW Armory returnerade oläsbara karaktärsdata. Försök igen senare."
            )
        except CharacterLimitReached:
            _LOGGER.info("character_registration_rejected_limit")
            result = (
                "Du har redan tre karaktärer. Ta bort en innan du lägger till fler."
            )
        except DuplicateCharacter:
            _LOGGER.info("character_registration_rejected_duplicate")
            result = "Den här karaktären finns redan i din Guild Armory."
        except CharacterNotFound:
            result = "Karaktären som skulle ersättas finns inte längre i din lista."
        except ValueError:
            result = (
                "Ange en fullständig karaktärslänk från den officiella "
                "WoW Armory-sidan."
            )
        except ArmoryHTTPError as exc:
            _LOGGER.warning(
                "Armory profile request failed", extra={"status": exc.status}
            )
            result = (
                "Karaktären hittades inte i WoW Armory."
                if exc.status == 404
                else "WoW Armory är tillfälligt otillgängligt. Försök igen senare."
            )
        except ArmoryTimeoutError:
            _LOGGER.warning("Armory profile request timed out")
            result = "WoW Armory tog för lång tid på sig. Försök igen senare."
        except ArmoryClientError:
            _LOGGER.warning("Armory profile request failed")
            result = "WoW Armory kunde inte nås. Försök igen senare."
        except discord.DiscordException, OSError, TimeoutError:
            result = "Guild Armory kunde inte uppdateras just nu. Försök igen senare."
            _LOGGER.warning("Guild Armory registration encountered a service failure")
        except Exception:
            result = "Registreringen misslyckades. Försök igen senare."
            _LOGGER.exception("Unexpected Guild Armory registration failure")
        await self.edit_ephemeral(interaction, result)

    async def sync_member_row(
        self,
        state: MemberState,
        *,
        fresh_paths: frozenset[str] = frozenset(),
    ) -> None:
        async with self.row_lock(state.guild_id, state.user_id):
            self._require_bot_identity()
            await self._sync_member_row(state, fresh_paths=fresh_paths)

    def _require_bot_identity(self) -> discord.ClientUser:
        user = self.user
        if user is None:
            raise BotIdentityUnavailable(
                "Discord bot identity is unavailable; member row sync deferred"
            )
        return user

    async def _sync_member_row(
        self,
        state: MemberState,
        *,
        fresh_paths: frozenset[str] = frozenset(),
    ) -> None:
        latest_state = await self.store.member(state.guild_id, state.user_id)
        if latest_state is not None:
            state = latest_state
        config = await self.store.get_guild(state.guild_id)
        if config is None:
            raise RuntimeError("Guild Armory configuration missing")
        channel = await self.get_armory_channel(config.channel_id)
        if channel is None:
            raise RuntimeError("Guild Armory channel is unavailable")
        if not state.characters:
            filename = f"guild-armory-{state.guild_id}-{state.user_id}.png"
            old: discord.Message | None = None
            fetch_message = getattr(channel, "fetch_message", None)
            if state.row_message_id is not None and fetch_message is not None:
                try:
                    candidate = await fetch_message(state.row_message_id)
                    if self._is_bot_attachment(candidate, filename):
                        old = candidate
                except discord.NotFound:
                    pass
            if old is None:
                old = await self._find_attachment_message(channel, filename)
            if old is not None:
                self._require_bot_identity()
                if self._is_bot_attachment(old, filename):
                    try:
                        await old.delete()
                    except discord.NotFound:
                        pass
            await self.store.set_row_message(state.guild_id, state.user_id, None)
            _LOGGER.info("member_image_removed")
            return
        guild = self.get_guild(state.guild_id)
        member = guild.get_member(state.user_id) if guild is not None else None
        display_name = member.display_name if member is not None else state.display_name
        async with self.graphic_semaphore:
            png = await to_thread(
                render_stored_member_overview,
                state,
                display_name=display_name,
                fresh_paths=fresh_paths,
            )
        filename = f"guild-armory-{state.guild_id}-{state.user_id}.png"
        message: discord.Message | None = None
        fetch_message = getattr(channel, "fetch_message", None)
        if (
            state.row_message_id is not None
            and state.row_message_id != 0
            and fetch_message
        ):
            try:
                message = await fetch_message(state.row_message_id)
            except discord.NotFound:
                message = None
            if message is not None and not self._is_bot_attachment(message, filename):
                message = None
        if message is None:
            message = await self._find_attachment_message(channel, filename)
        if message is None:
            self._require_bot_identity()
            message = await channel.send(
                content="Guild Armory",
                file=discord.File(BytesIO(png), filename=filename),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            self._require_bot_identity()
            if not self._is_bot_attachment(message, filename):
                self._require_bot_identity()
                message = await channel.send(
                    content="Guild Armory",
                    file=discord.File(BytesIO(png), filename=filename),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await message.edit(
                    content="Guild Armory",
                    attachments=[discord.File(BytesIO(png), filename=filename)],
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        await self.store.set_row_message(state.guild_id, state.user_id, message.id)
        _LOGGER.info("member_image_updated")

    def _is_bot_attachment(self, message: discord.Message, filename: str) -> bool:
        return bool(
            self.user is not None
            and message.author.id == self.user.id
            and any(item.filename == filename for item in message.attachments)
        )

    async def update_member_dm(
        self,
        member: discord.Member,
        state: MemberState,
        preferred_message_id: int | None,
    ) -> None:
        try:
            dm = await member.create_dm()
            message_id = state.manage_message_id or preferred_message_id
            message = None
            if message_id is not None:
                try:
                    message = await dm.fetch_message(message_id)
                except discord.NotFound:
                    message = None
            content = (
                f"Server: {self.guild_label(state.guild_id)}. "
                f"Din Guild Armory är uppdaterad "
                f"({len(state.characters)} av 3 karaktärer)."
                if state.characters
                else f"Server: {self.guild_label(state.guild_id)}. "
                "Din Guild Armory är tom. Lägg till en karaktär när du vill."
            )
            view = GuildPanelView(self, state.guild_id)
            if message is None:
                message = await dm.send(
                    content,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await message.edit(
                    content=content,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            await self.store.set_manage_message(
                state.guild_id, state.user_id, message.id
            )
        except discord.Forbidden, discord.NotFound:
            return
        except discord.HTTPException, TimeoutError:
            _LOGGER.warning("Guild Armory private management message could not update")

    async def open_management(
        self, interaction: discord.Interaction, guild_id: int
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.DiscordException, TimeoutError:
            return
        try:
            verified = await self._verify_member(guild_id, interaction.user.id)
            state = await self.store.member(guild_id, interaction.user.id)
            if verified is None or state is None:
                text = (
                    "Ingen registrerade karaktärer hittades för dig i den här servern."
                )
                view: discord.ui.View = GuildPanelView(self, guild_id)
            elif not state.characters:
                text = (
                    "Din lista är tom. Lägg till en karaktär via det privata "
                    "formuläret."
                )
                view = GuildPanelView(self, guild_id)
            else:
                text = f"Du har {len(state.characters)} av 3 möjliga karaktärer."
                view = CharacterSelectView(self, guild_id, interaction.user.id, state)
            await interaction.edit_original_response(
                content=text, view=view, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.DiscordException, TimeoutError:
            _LOGGER.warning("Guild Armory management view could not be shown")
        except Exception:
            _LOGGER.exception("Unexpected Guild Armory management failure")
            await self.edit_ephemeral(
                interaction, "Hanteringen kunde inte öppnas. Försök igen senare."
            )

    async def remove_character(
        self, interaction: discord.Interaction, guild_id: int, path: str
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.DiscordException, TimeoutError:
            return
        try:
            verified = await self._verify_member(guild_id, interaction.user.id)
            if verified is None:
                await self.edit_ephemeral(
                    interaction, "Du är inte medlem i den här servern."
                )
                return
            _, member = verified
            async with self.member_lock(guild_id, member.id):
                state = await self.store.remove_character(guild_id, member.id, path)
                _LOGGER.info(
                    "character_removed",
                    extra={"character_count": len(state.characters)},
                )
                await self.sync_member_row(state)
                await self.update_member_dm(member, state, state.manage_message_id)
                await self.edit_ephemeral(
                    interaction,
                    "Karaktären har tagits bort."
                    if state.characters
                    else "Karaktären har tagits bort. Din rad är nu tom.",
                )
        except CharacterNotFound:
            await self.edit_ephemeral(
                interaction, "Karaktären finns inte längre i din lista."
            )
        except BotIdentityUnavailable:
            _LOGGER.warning("Character removal deferred until bot identity returns")
            await self.edit_ephemeral(
                interaction, "Borttagningen kunde inte slutföras just nu."
            )
        except discord.DiscordException, OSError, TimeoutError:
            _LOGGER.warning(
                "Guild Armory character removal encountered a service failure"
            )
            await self.edit_ephemeral(
                interaction, "Borttagningen kunde inte slutföras just nu."
            )
        except Exception:
            _LOGGER.exception("Unexpected Guild Armory character removal failure")
            await self.edit_ephemeral(
                interaction, "Borttagningen kunde inte slutföras just nu."
            )

    async def on_ready(self) -> None:
        _LOGGER.info("discord_connected")
        async with self._recovery_lock:
            if self._recovered:
                return
            all_recovered = True
            try:
                configs = await self.store.guilds()
            except Exception:
                _LOGGER.exception("Could not load Guild Armory recovery state")
                return
            _LOGGER.info(
                "guild_armory_recovery_started", extra={"guild_count": len(configs)}
            )
            recovered_guilds = 0
            for config in configs:
                try:
                    if not await self._recover_guild(config):
                        all_recovered = False
                    else:
                        recovered_guilds += 1
                except discord.DiscordException, OSError, TimeoutError:
                    all_recovered = False
                    _LOGGER.warning("Guild Armory recovery deferred for a guild")
                except BotIdentityUnavailable:
                    all_recovered = False
                    _LOGGER.warning("Guild Armory recovery deferred until bot identity")
                except Exception:
                    all_recovered = False
                    _LOGGER.exception("Unexpected Guild Armory recovery failure")
            self._recovered = all_recovered
            _LOGGER.info(
                "guild_armory_recovery_completed",
                extra={
                    "guild_count": len(configs),
                    "guilds_recovered": recovered_guilds,
                },
            )

    async def on_disconnect(self) -> None:
        async with self._recovery_lock:
            self._recovered = False
        _LOGGER.info("discord_disconnected_recovery_scheduled")

    async def _recover_guild(self, config: GuildConfig) -> bool:
        channel = await self.get_armory_channel(config.channel_id)
        if channel is None:
            return False
        recovered = True
        panels_recovered = 0
        member_rows_recovered = 0
        async with self.activation_lock(config.guild_id):
            fetch_message = getattr(channel, "fetch_message", None)
            if fetch_message is None:
                try:
                    await self.upsert_panel(config.guild_id, channel, config)
                except Exception:
                    recovered = False
                    _LOGGER.exception("Guild Armory panel recovery failed")
                else:
                    panels_recovered = 1
            else:
                try:
                    panel = await fetch_message(config.panel_message_id)
                    if not self._is_bot_attachment(
                        panel, f"guild-armory-panel-{config.guild_id}.png"
                    ):
                        panel = await self.upsert_panel(
                            config.guild_id, channel, config
                        )
                    try:
                        await panel.add_reaction(_SWORD)
                    except discord.DiscordException:
                        recovered = False
                        _LOGGER.warning("Guild Armory reaction could not be restored")
                    else:
                        panels_recovered = 1
                except discord.NotFound:
                    try:
                        await self.upsert_panel(config.guild_id, channel, config)
                        panels_recovered = 1
                    except Exception:
                        recovered = False
                        _LOGGER.exception("Guild Armory panel recovery failed")
                except discord.DiscordException:
                    recovered = False
                    _LOGGER.warning("Guild Armory panel recovery deferred")
                except Exception:
                    recovered = False
                    _LOGGER.exception("Unexpected Guild Armory panel recovery failure")
        for state in await self.store.members(config.guild_id):
            try:
                await self.sync_member_row(state)
            except BotIdentityUnavailable:
                recovered = False
                _LOGGER.warning(
                    "Guild Armory member row recovery deferred until identity"
                )
            except discord.DiscordException, OSError, TimeoutError:
                recovered = False
                _LOGGER.warning("Guild Armory member row recovery deferred")
            except Exception:
                recovered = False
                _LOGGER.exception("Unexpected Guild Armory member row recovery failure")
            else:
                member_rows_recovered += 1
        _LOGGER.info(
            "guild_armory_guild_recovery_completed",
            extra={
                "panels_recovered": panels_recovered,
                "member_rows_recovered": member_rows_recovered,
            },
        )
        return recovered


def create_bot() -> ArmoryBot:
    """Build an offline bot instance from public configuration and env secrets."""
    runtime_environment = os.getenv("ARMORY_ENVIRONMENT", "production").lower()
    if runtime_environment not in {"production", "development"}:
        raise RuntimeError("ARMORY_ENVIRONMENT must be production or development")
    raw_test_guild_id = (
        os.getenv("DISCORD_TEST_GUILD_ID")
        if runtime_environment == "development"
        else None
    )
    test_guild_id: int | None = None
    if raw_test_guild_id:
        try:
            test_guild_id = int(raw_test_guild_id)
        except ValueError as exc:
            raise RuntimeError(
                "DISCORD_TEST_GUILD_ID must be a positive integer when set"
            ) from exc
        if test_guild_id <= 0:
            raise RuntimeError("DISCORD_TEST_GUILD_ID must be a positive integer")
    timeout = float(os.getenv("ARMORY_TIMEOUT", "10"))
    database_path = Path(os.getenv("ARMORY_DATABASE_PATH", "data/guild-armory.sqlite3"))
    return ArmoryBot(
        ArmoryClient(timeout=timeout), GuildArmoryStore(database_path), test_guild_id
    )


def _read_credential_file(path: Path) -> str:
    """Read a bounded private token file or trusted systemd credential."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("credential file must be a regular file")

        mode = stat.S_IMODE(info.st_mode)
        if not _credential_permissions_are_safe(
            path,
            owner_uid=info.st_uid,
            mode=mode,
            credentials_directory=os.getenv("CREDENTIALS_DIRECTORY"),
        ):
            raise ValueError("credential file must be private")
        with os.fdopen(descriptor, "r", encoding="utf-8") as credential_file:
            descriptor = -1
            value = credential_file.read(_MAX_TOKEN_FILE_BYTES + 1)
        if len(value.encode("utf-8")) > _MAX_TOKEN_FILE_BYTES:
            raise ValueError("credential file exceeds the permitted size")
        return value.strip()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _credential_permissions_are_safe(
    path: Path,
    *,
    owner_uid: int,
    mode: int,
    credentials_directory: str | None,
) -> bool:
    """Allow group-read only for root-owned 0440 systemd credentials."""
    if mode & 0o077 == 0:
        return True
    return (
        credentials_directory is not None
        and owner_uid == 0
        and mode == 0o440
        and path == Path(credentials_directory) / "discord-token"
    )


async def run_bot() -> None:
    token = os.getenv("DISCORD_BOT_TOKEN")
    token_file = os.getenv("DISCORD_BOT_TOKEN_FILE")
    if not token and token_file:
        try:
            token = _read_credential_file(Path(token_file))
        except OSError, UnicodeError, ValueError:
            raise RuntimeError(
                "configured Discord credential file is unavailable"
            ) from None
    configure_logging(secrets=(token,) if token else ())
    _LOGGER.info("bot_startup_started")
    if not token:
        _LOGGER.error("bot_token_missing")
        raise RuntimeError("DISCORD_BOT_TOKEN or DISCORD_BOT_TOKEN_FILE must be set")
    database_path = Path(os.getenv("ARMORY_DATABASE_PATH", "data/guild-armory.sqlite3"))
    if restore_marker_path(database_path).exists():
        _LOGGER.error("database_restore_requires_operator_review")
        raise RuntimeError("database restore was interrupted; operator review required")
    try:
        with exclusive_file_lock(runtime_lock_path(database_path)):
            with exclusive_file_lock(database_file_lock_path(database_path)):
                if restore_marker_path(database_path).exists():
                    _LOGGER.error("database_restore_requires_operator_review")
                    raise RuntimeError(
                        "database restore was interrupted; operator review required"
                    )
                async with create_bot() as bot:
                    _LOGGER.info("discord_connection_starting")
                    await bot.start(token)
    except LockUnavailable as exc:
        _LOGGER.error("another_bot_process_owns_database_lock")
        raise RuntimeError("another bot process already owns this database") from exc


if __name__ == "__main__":
    asyncio.run(run_bot())
