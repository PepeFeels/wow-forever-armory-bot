"""Persistent Discord components and private character management modals."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import discord

from .guild_armory_store import MemberState

_OFFICIAL_ARMORY_URL = "https://worldofwarcraft.blizzard.com/en-gb/forever/eu/armory"

if TYPE_CHECKING:
    from .discord_bot import ArmoryBot


class CharacterUrlModal(discord.ui.Modal):
    def __init__(
        self,
        bot: ArmoryBot,
        guild_id: int,
        *,
        replace_path: str | None = None,
    ) -> None:
        super().__init__(title="Registrera WoW-karaktär")
        self.bot = bot
        self.guild_id = guild_id
        self.replace_path = replace_path
        self.profile_url: discord.ui.TextInput[discord.ui.Modal] = discord.ui.TextInput(
            label="Full Armory-länk",
            placeholder="https://worldofwarcraft.blizzard.com/en-us/forever/eu/...",
            max_length=1024,
            required=True,
        )
        self.add_item(self.profile_url)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.bot.submit_character_url(
            interaction,
            self.guild_id,
            str(self.profile_url.value),
            replace_path=self.replace_path,
        )


class _RegisterButton(discord.ui.Button[discord.ui.View]):
    def __init__(self, guild_id: int) -> None:
        super().__init__(
            label="Registrera privat",
            style=discord.ButtonStyle.primary,
            custom_id=f"guild-armory:register:{guild_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = cast(GuildPanelView | None, self.view)
        if view is None:
            return
        await interaction.response.send_modal(
            CharacterUrlModal(view.bot, view.guild_id)
        )


class _ManageButton(discord.ui.Button[discord.ui.View]):
    def __init__(self, guild_id: int) -> None:
        super().__init__(
            label="Mina karaktärer",
            style=discord.ButtonStyle.secondary,
            custom_id=f"guild-armory:manage:{guild_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = cast(GuildPanelView | None, self.view)
        if view is not None:
            await view.bot.open_management(interaction, view.guild_id)


class GuildPanelView(discord.ui.View):
    """Persistent panel controls; their guild ID removes DM ambiguity."""

    def __init__(self, bot: ArmoryBot, guild_id: int) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.guild_id = guild_id
        self.add_item(_RegisterButton(guild_id))
        self.add_item(_ManageButton(guild_id))
        self.add_item(
            discord.ui.Button(
                label="WoW Forever Armory (EU)",
                style=discord.ButtonStyle.link,
                url=_OFFICIAL_ARMORY_URL,
            )
        )


class _CharacterSelect(discord.ui.Select[discord.ui.View]):
    def __init__(self, state: MemberState) -> None:
        options = [
            discord.SelectOption(
                label=(entry.character.name or "Okänd karaktär")[:100],
                description=(
                    f"{(entry.character.character_class or 'Okänd klass')[:50]} · "
                    f"{entry.path[-40:]}"
                ),
                value=entry.path,
            )
            for entry in state.characters
        ]
        super().__init__(
            placeholder="Välj en karaktär att hantera",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = cast(CharacterSelectView, self.view)
        if interaction.user.id != view.owner_id:
            await interaction.response.send_message(
                "Den här menyn tillhör en annan medlem.", ephemeral=True
            )
            return
        await interaction.response.edit_message(
            content="Välj vad du vill göra med karaktären:",
            view=CharacterActionView(
                view.bot, view.guild_id, view.owner_id, self.values[0]
            ),
        )


class _AddCharacterButton(discord.ui.Button[discord.ui.View]):
    def __init__(self, guild_id: int) -> None:
        super().__init__(
            label="Lägg till karaktär",
            style=discord.ButtonStyle.primary,
            custom_id=f"guild-armory:add:{guild_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = cast(CharacterSelectView | None, self.view)
        if view is None:
            return
        if interaction.user.id != view.owner_id:
            await interaction.response.send_message(
                "Den här menyn tillhör en annan medlem.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            CharacterUrlModal(view.bot, view.guild_id)
        )


class CharacterSelectView(discord.ui.View):
    def __init__(
        self, bot: ArmoryBot, guild_id: int, owner_id: int, state: MemberState
    ) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.guild_id = guild_id
        self.owner_id = owner_id
        if state.characters:
            self.add_item(_CharacterSelect(state))
        self.add_item(_AddCharacterButton(guild_id))


class _RemoveButton(discord.ui.Button[discord.ui.View]):
    def __init__(self) -> None:
        super().__init__(label="Ta bort", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = cast(CharacterActionView | None, self.view)
        if view is None:
            return
        if interaction.user.id != view.owner_id:
            await interaction.response.send_message(
                "Den här menyn tillhör en annan medlem.", ephemeral=True
            )
            return
        await view.bot.remove_character(interaction, view.guild_id, view.path)


class _ReplaceButton(discord.ui.Button[discord.ui.View]):
    def __init__(self) -> None:
        super().__init__(label="Ersätt", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = cast(CharacterActionView | None, self.view)
        if view is None:
            return
        if interaction.user.id != view.owner_id:
            await interaction.response.send_message(
                "Den här menyn tillhör en annan medlem.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            CharacterUrlModal(view.bot, view.guild_id, replace_path=view.path)
        )


class CharacterActionView(discord.ui.View):
    def __init__(self, bot: ArmoryBot, guild_id: int, owner_id: int, path: str) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.path = path
        self.add_item(_RemoveButton())
        self.add_item(_ReplaceButton())
