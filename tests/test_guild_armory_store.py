"""Offline persistence tests for per-guild Guild Armory state."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wow_forever_armory.guild_armory_store import (
    CharacterLimitReached,
    CharacterNotFound,
    DuplicateCharacter,
    GuildArmoryStore,
)
from wow_forever_armory.models import Character, Profession


class GuildArmoryStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "armory.sqlite3"
        self.store = GuildArmoryStore(self.database)
        await self.store.initialize()
        await self.store.activate_guild(1, 10, 100)
        await self.store.activate_guild(2, 20, 200)

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def character(name: str) -> Character:
        return Character(
            name=name,
            surname="Woodsong",
            level=60,
            character_class="Druid",
            professions=(Profession("Herbalism", skill=300, max_skill=300),),
        )

    async def test_persists_and_recovers_panel_flows_member_and_snapshot(self) -> None:
        await self.store.activate_guild(1, 10, 101)
        self.assertEqual(
            await self.store.get_guild_by_panel(101), await self.store.get_guild(1)
        )
        self.assertEqual(await self.store.begin_flow(1, 42, 101), "started")
        self.assertEqual(await self.store.begin_flow(1, 42, 101), "already_pending")
        await self.store.set_flow_message(1, 42, 999)
        recovered = GuildArmoryStore(self.database)
        await recovered.initialize()
        self.assertEqual(await recovered.flow_message(1, 42), 999)
        self.assertEqual(await recovered.get_guild(1), await self.store.get_guild(1))
        state = await recovered.finish_flow(
            1,
            42,
            "Éowyn",
            "en-us/forever/eu/armory/character/eowyn",
            self.character("Éowyn"),
        )
        self.assertEqual(state.display_name, "Éowyn")
        self.assertEqual(state.characters[0].character.surname, "Woodsong")
        self.assertEqual(state.characters[0].character.professions[0].skill, 300)
        self.assertEqual(
            state.characters[0].path, "en-us/forever/eu/armory/character/eowyn"
        )
        self.assertIsNone(await recovered.flow_message(1, 42))
        self.assertEqual(await recovered.member(2, 42), None)

    async def test_three_character_limit_duplicates_replace_and_remove(self) -> None:
        paths = [f"en-us/forever/eu/armory/character/hero-{i}" for i in range(4)]
        for path in paths[:3]:
            await self.store.finish_flow(1, 42, "Hero", path, self.character(path[-1]))
        with self.assertRaises(DuplicateCharacter):
            await self.store.finish_flow(
                1, 42, "Hero", paths[0], self.character("same")
            )
        with self.assertRaises(CharacterLimitReached):
            await self.store.finish_flow(
                1, 42, "Hero", paths[3], self.character("fourth")
            )
        replaced = await self.store.finish_flow(
            1,
            42,
            "Hero",
            paths[3],
            self.character("Replacement"),
            replace_path=paths[1],
        )
        self.assertEqual(
            [entry.path for entry in replaced.characters],
            [paths[0], paths[3], paths[2]],
        )
        removed = await self.store.remove_character(1, 42, paths[0])
        self.assertEqual([entry.slot for entry in removed.characters], [0, 1])
        with self.assertRaises(CharacterNotFound):
            await self.store.remove_character(2, 42, paths[3])

    async def test_guilds_are_isolated_and_same_user_can_register_per_guild(
        self,
    ) -> None:
        path = "en-us/forever/eu/armory/character/hero"
        await self.store.finish_flow(1, 42, "Hero One", path, self.character("One"))
        await self.store.finish_flow(2, 42, "Hero Two", path, self.character("Two"))
        self.assertEqual(
            (await self.store.member(1, 42)).characters[0].character.name, "One"
        )
        self.assertEqual(
            (await self.store.member(2, 42)).characters[0].character.name, "Two"
        )
        self.assertEqual(len(await self.store.members(1)), 1)
        self.assertEqual(len(await self.store.members(2)), 1)

    async def test_concurrent_adds_never_exceed_three_slots(self) -> None:
        async def add(index: int) -> str:
            path = f"en-us/forever/eu/armory/character/concurrent-{index}"
            try:
                await self.store.finish_flow(
                    1, 77, "Concurrent", path, self.character(str(index))
                )
            except CharacterLimitReached:
                return "full"
            return "saved"

        outcomes = await asyncio.gather(*(add(index) for index in range(6)))
        self.assertEqual(outcomes.count("saved"), 3)
        state = await self.store.member(1, 77)
        self.assertEqual(len(state.characters), 3)
        self.assertEqual([entry.slot for entry in state.characters], [0, 1, 2])

    async def test_removing_each_slot_compacts_then_allows_new_registration(
        self,
    ) -> None:
        for removed_slot in range(3):
            user_id = 100 + removed_slot
            paths = [
                f"en-us/forever/eu/armory/character/{user_id}-{index}"
                for index in range(3)
            ]
            for path in paths:
                await self.store.finish_flow(
                    1, user_id, "Hero", path, self.character(path.rsplit("/", 1)[-1])
                )
            removed = await self.store.remove_character(1, user_id, paths[removed_slot])
            self.assertEqual(
                [entry.slot for entry in removed.characters],
                list(range(len(removed.characters))),
            )
            added_path = f"en-us/forever/eu/armory/character/{user_id}-new"
            added = await self.store.finish_flow(
                1, user_id, "Hero", added_path, self.character("New")
            )
            self.assertEqual(len(added.characters), 3)
            self.assertEqual([entry.slot for entry in added.characters], [0, 1, 2])

    async def test_concurrent_duplicate_registration_saves_only_one(self) -> None:
        path = "en-us/forever/eu/armory/character/same"

        async def add() -> str:
            try:
                await self.store.finish_flow(
                    1, 500, "Hero", path, self.character("Same")
                )
            except DuplicateCharacter:
                return "duplicate"
            return "saved"

        results = await asyncio.gather(*(add() for _ in range(8)))
        self.assertEqual(results.count("saved"), 1)
        self.assertEqual(results.count("duplicate"), 7)
        state = await self.store.member(1, 500)
        self.assertEqual(len(state.characters), 1)

    async def test_unexpected_transaction_error_rolls_back_changes(self) -> None:
        path = "en-us/forever/eu/armory/character/rollback"
        with patch.object(
            self.store, "_member", side_effect=RuntimeError("forced test error")
        ):
            with self.assertRaisesRegex(RuntimeError, "forced test error"):
                await self.store.finish_flow(
                    1, 501, "Hero", path, self.character("Rollback")
                )
        self.assertIsNone(await self.store.member(1, 501))
        state = await self.store.finish_flow(
            1, 501, "Hero", path, self.character("Rollback")
        )
        self.assertEqual(len(state.characters), 1)
