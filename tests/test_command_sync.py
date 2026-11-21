"""Offline tests for the separately approved global command sync tool."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from discord.ext import commands

from wow_forever_armory.command_sync import (
    CommandSyncError,
    command_diff,
    local_command_payloads,
    remote_command_payloads,
    review_or_apply,
    validate_apply_approval,
)
from wow_forever_armory.discord_bot import GuildArmoryCommands


def payload(name: str, description: str) -> dict[str, object]:
    return {
        "type": 1,
        "name": name,
        "description": description,
        "options": [],
        "dm_permission": True,
        "nsfw": False,
    }


def _remote(payload_data: dict[str, object]) -> SimpleNamespace:
    data = {
        **payload_data,
        "id": "123",
        "application_id": "456",
        "version": "789",
    }
    return SimpleNamespace(
        to_dict=lambda: data,
        default_member_permissions=None,
        dm_permission=True,
        nsfw=False,
    )


class CommandSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = commands.Bot(
            command_prefix=commands.when_mentioned, intents=discord.Intents.none()
        )
        await self.client.add_cog(GuildArmoryCommands(self.client))

    async def asyncTearDown(self) -> None:
        await self.client.close()

    def test_local_tree_contains_expected_armory_group_without_duplicates(self) -> None:
        local = local_command_payloads(self.client.tree)
        self.assertEqual([item["name"] for item in local], ["armory"])
        self.assertEqual(
            [item["name"] for item in local[0]["options"]],
            ["activate", "manage"],
        )

    def test_real_discord_model_round_trips_to_same_local_payload(self) -> None:
        local = local_command_payloads(self.client.tree)
        remote_models = []
        for index, payload_data in enumerate(local, start=1):
            remote_data = {
                **payload_data,
                "id": str(index),
                "application_id": "999",
                "version": "1000",
                "name_localizations": {},
                "description_localizations": {},
            }
            remote_models.append(
                discord.app_commands.AppCommand(
                    data=remote_data, state=self.client._connection
                )
            )
        self.assertEqual(remote_command_payloads(remote_models), local)

    def test_diff_is_stable_and_reports_all_four_categories(self) -> None:
        same = payload("same", "Same")
        added = payload("added", "Added")
        old = payload("changed", "Old")
        new = payload("changed", "New")
        removed = payload("removed", "Removed")
        first = command_diff([same, added, new], [same, old, removed])
        second = command_diff([same, added, new], [same, old, removed])
        self.assertEqual(first, second)
        self.assertEqual(first["unchanged"], ["1:same"])
        self.assertEqual(first["added"], [added])
        self.assertEqual(
            first["changed"], [{"identity": "1:changed", "before": old, "after": new}]
        )
        self.assertEqual(first["removed"], [removed])
        self.assertEqual(len(first["diff_sha256"]), 64)

    def test_apply_requires_exact_diff_approval_and_separate_deletion_approval(
        self,
    ) -> None:
        removed = command_diff([], [payload("old", "Old")])
        with self.assertRaises(CommandSyncError):
            validate_apply_approval(
                removed,
                approved_diff_sha256=removed["diff_sha256"],
                approval_reference="PO-SYNC",
                approved_deletions_sha256=None,
                deletion_approval_reference=None,
            )
        with self.assertRaises(CommandSyncError):
            validate_apply_approval(
                removed,
                approved_diff_sha256=removed["diff_sha256"],
                approval_reference="PO-SYNC",
                approved_deletions_sha256=removed["diff_sha256"],
                deletion_approval_reference="PO-SYNC",
            )
        validate_apply_approval(
            removed,
            approved_diff_sha256=removed["diff_sha256"],
            approval_reference="PO-SYNC",
            approved_deletions_sha256=removed["diff_sha256"],
            deletion_approval_reference="PO-DELETE",
        )

    async def test_dry_run_fetches_but_never_syncs(self) -> None:
        self.client.tree.fetch_commands = AsyncMock(return_value=[])
        self.client.tree.sync = AsyncMock(side_effect=AssertionError("write forbidden"))
        result = await review_or_apply(self.client, mode="dry-run")
        self.assertEqual(result["status"], "DRY_RUN")
        self.assertTrue(result["added"])
        self.client.tree.fetch_commands.assert_awaited_once_with()
        self.client.tree.sync.assert_not_awaited()

    async def test_apply_rejects_missing_approval_without_sync(self) -> None:
        self.client.tree.fetch_commands = AsyncMock(return_value=[])
        self.client.tree.sync = AsyncMock()
        with self.assertRaisesRegex(CommandSyncError, "approval"):
            await review_or_apply(
                self.client,
                mode="apply",
                approved_diff_sha256="0" * 64,
                approval_reference="PO-SYNC",
            )
        self.client.tree.sync.assert_not_awaited()

    async def test_apply_rejects_stale_remote_state_without_sync(self) -> None:
        local = local_command_payloads(self.client.tree)
        remote_initial = []
        remote_changed = [_remote(payload("unexpected", "Unexpected"))]
        self.client.tree.fetch_commands = AsyncMock(
            side_effect=[remote_initial, remote_changed]
        )
        self.client.tree.sync = AsyncMock()
        diff = command_diff(local, [])
        with self.assertRaisesRegex(CommandSyncError, "changed after review"):
            await review_or_apply(
                self.client,
                mode="apply",
                approved_diff_sha256=diff["diff_sha256"],
                approval_reference="PO-SYNC-1",
            )
        self.client.tree.sync.assert_not_awaited()

    async def test_apply_syncs_once_and_verifies_remote_state(self) -> None:
        local = local_command_payloads(self.client.tree)
        remote_initial = []
        remote_final = [_remote(item) for item in local]
        self.client.tree.fetch_commands = AsyncMock(
            side_effect=[remote_initial, remote_initial, remote_final]
        )
        self.client.tree.sync = AsyncMock()
        diff = command_diff(local, [])
        result = await review_or_apply(
            self.client,
            mode="apply",
            approved_diff_sha256=diff["diff_sha256"],
            approval_reference="PO-SYNC-1",
        )
        self.assertEqual(result["status"], "APPLIED_AND_VERIFIED")
        self.client.tree.sync.assert_awaited_once_with()
        self.assertEqual(self.client.tree.fetch_commands.await_count, 3)


if __name__ == "__main__":
    unittest.main()
