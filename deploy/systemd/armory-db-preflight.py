#!/usr/bin/python3
"""Read-only startup gate for an initialized Guild Armory database."""

from __future__ import annotations

import os
import pwd
import sqlite3
import sys
from pathlib import Path


def armory_process_present(proc_root: Path = Path("/proc")) -> bool:
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return True
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            arguments = [
                value.decode(errors="replace")
                for value in (entry / "cmdline").read_bytes().split(b"\0")
                if value
            ]
        except FileNotFoundError:
            continue
        except OSError:
            return True
        if (
            len(arguments) >= 3
            and arguments[1:3] == ["-m", "wow_forever_armory.discord_bot"]
        ) or any(Path(argument).name == "discord_bot.py" for argument in arguments):
            return True
    return False


def database_is_ready(path: str | Path, *, expected_owner: int | None = None) -> bool:
    database = Path(path)
    marker = Path(f"{database}.restore-in-progress")
    if marker.exists() or not database.is_file() or database.stat().st_size == 0:
        return False
    try:
        info = database.stat()
        if expected_owner is not None and info.st_uid != expected_owner:
            return False
        if info.st_mode & 0o077:
            return False
        uri = f"{database.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                return False
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if not {"guilds", "members", "characters", "flows"}.issubset(tables):
                return False
            guild_count = connection.execute("SELECT count(*) FROM guilds").fetchone()
            return guild_count is not None
    except OSError, sqlite3.Error:
        return False


def main() -> int:
    path = os.getenv("ARMORY_DATABASE_PATH", "")
    if armory_process_present():
        print("An Armory bot process is already running.", file=sys.stderr)
        return 78
    try:
        service_uid = pwd.getpwnam("wow-armory").pw_uid
    except KeyError:
        print("Armory service account is unavailable.", file=sys.stderr)
        return 78
    if not path or not database_is_ready(path, expected_owner=service_uid):
        print("Armory database is unavailable or not migrated.", file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
