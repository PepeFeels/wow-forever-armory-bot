"""Explicit, reviewable global Discord application-command synchronization.

Normal bot startup never calls this module and never synchronizes commands.
Run this one-shot administrative command first with ``--mode dry-run``. An
apply requires the exact diff hash printed by that run and a separate approval
reference. Any removal additionally requires its own approval reference.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands

from .discord_bot import GuildArmoryCommands

TOKEN_PATH = Path("/etc/wow-forever-armory-bot/discord-token")
SERVER_ONLY_FIELDS = frozenset({"id", "application_id", "guild_id", "version"})
APPROVAL_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")


class CommandSyncError(RuntimeError):
    """The requested review or command-sync operation could not be completed."""


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )


def _command_key(command: dict[str, Any]) -> str:
    return f"{command['type']}:{command['name']}"


def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove server identity fields and normalize permission integers."""
    normalized: dict[str, Any] = {}
    for key, value in payload.items():
        if key in SERVER_ONLY_FIELDS or value is None:
            continue
        if key in {"name_localizations", "description_localizations"} and value == {}:
            # Fetched AppCommand models include empty maps at every depth;
            # local Command serialization omits them.
            continue
        if isinstance(value, dict):
            value = _normalize_payload(value)
        elif isinstance(value, list):
            value = [
                _normalize_payload(item) if isinstance(item, dict) else item
                for item in value
            ]
        normalized[key] = value
    permissions = normalized.get("default_member_permissions")
    if permissions is not None:
        try:
            normalized["default_member_permissions"] = int(permissions)
        except (TypeError, ValueError) as exc:
            raise CommandSyncError("command permission value is malformed") from exc
    return normalized


def local_command_payloads(tree: discord.app_commands.CommandTree) -> list[dict[str, Any]]:
    """Serialize the locally registered global commands without API writes."""
    commands_by_key: dict[str, dict[str, Any]] = {}
    for command in tree.get_commands():
        payload = _normalize_payload(command.to_dict(tree))
        key = _command_key(payload)
        if key in commands_by_key:
            raise CommandSyncError("local command tree contains duplicate commands")
        commands_by_key[key] = payload
    return [commands_by_key[key] for key in sorted(commands_by_key)]


def remote_command_payloads(remote_commands: list[discord.AppCommand]) -> list[dict[str, Any]]:
    """Serialize fetched global commands while excluding Discord-assigned IDs."""
    commands_by_key: dict[str, dict[str, Any]] = {}
    for command in remote_commands:
        payload = command.to_dict()
        payload["default_member_permissions"] = (
            None
            if command.default_member_permissions is None
            else command.default_member_permissions.value
        )
        payload["dm_permission"] = command.dm_permission
        payload["nsfw"] = command.nsfw
        normalized = _normalize_payload(payload)
        key = _command_key(normalized)
        if key in commands_by_key:
            raise CommandSyncError("Discord returned duplicate global commands")
        commands_by_key[key] = normalized
    return [commands_by_key[key] for key in sorted(commands_by_key)]


def command_diff(
    local: list[dict[str, Any]], remote: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build a deterministic diff and SHA-256 approval identity."""
    local_by_key = {_command_key(item): item for item in local}
    remote_by_key = {_command_key(item): item for item in remote}
    if len(local_by_key) != len(local) or len(remote_by_key) != len(remote):
        raise CommandSyncError("command diff contains duplicate identities")

    unchanged: list[str] = []
    added: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for key in sorted(set(local_by_key) | set(remote_by_key)):
        current = remote_by_key.get(key)
        desired = local_by_key.get(key)
        if current is None:
            added.append(desired)
        elif desired is None:
            removed.append(current)
        elif current == desired:
            unchanged.append(key)
        else:
            changed.append({"identity": key, "before": current, "after": desired})

    body = {
        "schema_version": 1,
        "scope": "global",
        "unchanged": unchanged,
        "added": added,
        "changed": changed,
        "removed": removed,
    }
    return {**body, "diff_sha256": hashlib.sha256(_canonical(body)).hexdigest()}


def validate_apply_approval(
    diff: dict[str, Any],
    *,
    approved_diff_sha256: str | None,
    approval_reference: str | None,
    approved_deletions_sha256: str | None,
    deletion_approval_reference: str | None,
) -> None:
    """Fail closed unless the exact diff and any removals have separate approval."""
    digest = diff["diff_sha256"]
    has_changes = bool(diff["added"] or diff["changed"] or diff["removed"])
    if not has_changes:
        return
    if approved_diff_sha256 != digest:
        raise CommandSyncError("apply approval does not match the current diff hash")
    if not isinstance(approval_reference, str) or not APPROVAL_REFERENCE.fullmatch(
        approval_reference
    ):
        raise CommandSyncError("a valid command-sync approval reference is required")

    if diff["removed"]:
        if approved_deletions_sha256 != digest:
            raise CommandSyncError("deletions require a separate approval of this diff")
        if (
            not isinstance(deletion_approval_reference, str)
            or not APPROVAL_REFERENCE.fullmatch(deletion_approval_reference)
            or deletion_approval_reference == approval_reference
        ):
            raise CommandSyncError("a distinct deletion approval reference is required")
    elif approved_deletions_sha256 is not None or deletion_approval_reference is not None:
        raise CommandSyncError("deletion approval was supplied when no deletion exists")


def read_production_token(path: Path = TOKEN_PATH) -> str:
    """Read the fixed root-only credential without following links or logging it."""
    if os.geteuid() != 0:
        raise CommandSyncError("command synchronization must run as root")
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise CommandSyncError("Discord credential path is unsafe")
    directory_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            if component in {"", ".", ".."}:
                raise CommandSyncError("Discord credential path is unsafe")
            child_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            child_info = os.fstat(child_fd)
            if (
                not stat.S_ISDIR(child_info.st_mode)
                or child_info.st_uid != 0
                or child_info.st_mode & 0o022
            ):
                os.close(child_fd)
                raise CommandSyncError("Discord credential directory is unsafe")
            os.close(directory_fd)
            directory_fd = child_fd
        before = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        os.close(directory_fd)
        raise CommandSyncError("Discord credential path cannot be verified") from exc
    except Exception:
        os.close(directory_fd)
        raise
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != 0
        or before.st_gid != 0
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_nlink != 1
        or before.st_size < 1
        or before.st_size > 4096
    ):
        os.close(directory_fd)
        raise CommandSyncError("Discord credential file metadata is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOATIME", 0)
    try:
        fd = os.open(path.name, flags, dir_fd=directory_fd)
    except OSError as exc:
        os.close(directory_fd)
        raise CommandSyncError("Discord credential cannot be opened safely") from exc
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise CommandSyncError("Discord credential changed while opening")
        chunks: list[bytes] = []
        remaining = 4097
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        entry = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            len(raw) != before.st_size
            or len(raw) > 4096
            or (after.st_dev, after.st_ino, after.st_size)
            != (opened.st_dev, opened.st_ino, opened.st_size)
            or (entry.st_dev, entry.st_ino, entry.st_size)
            != (opened.st_dev, opened.st_ino, opened.st_size)
        ):
            raise CommandSyncError("Discord credential changed while reading")
    finally:
        os.close(fd)
        os.close(directory_fd)
    try:
        token = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise CommandSyncError("Discord credential is malformed") from exc
    if not token or "\n" in token or "\r" in token or "\0" in token:
        raise CommandSyncError("Discord credential is malformed")
    return token


async def review_or_apply(
    client: commands.Bot,
    *,
    mode: str,
    approved_diff_sha256: str | None = None,
    approval_reference: str | None = None,
    approved_deletions_sha256: str | None = None,
    deletion_approval_reference: str | None = None,
) -> dict[str, Any]:
    """Fetch, compare, optionally apply one explicitly approved global diff."""
    remote = remote_command_payloads(await client.tree.fetch_commands())
    local = local_command_payloads(client.tree)
    diff = command_diff(local, remote)
    if mode == "dry-run":
        return {"status": "DRY_RUN", **diff}
    if mode != "apply":
        raise CommandSyncError("unsupported command-sync mode")
    validate_apply_approval(
        diff,
        approved_diff_sha256=approved_diff_sha256,
        approval_reference=approval_reference,
        approved_deletions_sha256=approved_deletions_sha256,
        deletion_approval_reference=deletion_approval_reference,
    )
    if not (diff["added"] or diff["changed"] or diff["removed"]):
        return {"status": "NO_CHANGES", **diff}

    # Fetch again immediately before the sole write and reject a stale review.
    latest_remote = remote_command_payloads(await client.tree.fetch_commands())
    latest_diff = command_diff(local, latest_remote)
    if latest_diff["diff_sha256"] != diff["diff_sha256"]:
        raise CommandSyncError("global command set changed after review")

    await client.tree.sync()
    after_sync = remote_command_payloads(await client.tree.fetch_commands())
    if after_sync != local:
        return {
            "status": "APPLY_UNVERIFIED",
            **diff,
            "approval_reference": approval_reference,
            "deletion_approval_reference": deletion_approval_reference,
        }
    return {
        "status": "APPLIED_AND_VERIFIED",
        **diff,
        "approval_reference": approval_reference,
        "deletion_approval_reference": deletion_approval_reference,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("dry-run", "apply"), required=True)
    parser.add_argument("--approve-diff-sha256")
    parser.add_argument("--approval-reference")
    parser.add_argument("--approve-deletions-sha256")
    parser.add_argument("--deletion-approval-reference")
    return parser


async def _run(args: argparse.Namespace) -> int:
    token = read_production_token()
    client = commands.Bot(
        command_prefix=commands.when_mentioned,
        intents=discord.Intents.none(),
    )
    try:
        await client.add_cog(GuildArmoryCommands(client))
        # login() performs REST authentication only; it does not connect to the
        # gateway, initialize SQLite, start tasks, or sync application commands.
        await client.login(token)
        report = await review_or_apply(
            client,
            mode=args.mode,
            approved_diff_sha256=args.approve_diff_sha256,
            approval_reference=args.approval_reference,
            approved_deletions_sha256=args.approve_deletions_sha256,
            deletion_approval_reference=args.deletion_approval_reference,
        )
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 0 if report["status"] in {"DRY_RUN", "NO_CHANGES", "APPLIED_AND_VERIFIED"} else 2
    finally:
        await client.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    approvals = (
        args.approve_diff_sha256,
        args.approval_reference,
        args.approve_deletions_sha256,
        args.deletion_approval_reference,
    )
    if args.mode == "dry-run" and any(value is not None for value in approvals):
        print(json.dumps({"status": "NO_GO", "error": "dry-run accepts no approvals"}))
        return 2
    try:
        return asyncio.run(_run(args))
    except Exception as exc:
        status = getattr(exc, "status", None)
        print(
            json.dumps(
                {
                    "status": "NO_GO",
                    "error_type": type(exc).__name__,
                    "http_status": status,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
