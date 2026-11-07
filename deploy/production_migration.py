"""Fail-closed, offline migration of the legacy SQLite database."""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path

from wow_forever_armory.operations import (
    OperationError,
    create_backup,
    exclusive_file_lock,
    restore_marker_path,
    runtime_lock_path,
    verify_database,
)

_BOT_MODULE = "wow_forever_armory.discord_bot"
_TABLES = {"guilds", "members", "characters", "flows"}


def armory_process_pids(proc_root: Path = Path("/proc")) -> list[int]:
    """Return exact module-runner PIDs without exposing command arguments."""
    pids: list[int] = []
    for entry in proc_root.iterdir():
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
        except OSError as exc:
            raise RuntimeError("could not inspect all process command lines") from exc
        if (len(arguments) >= 3 and arguments[1:3] == ["-m", _BOT_MODULE]) or any(
            Path(argument).name == "discord_bot.py" for argument in arguments
        ):
            pids.append(int(entry.name))
    return sorted(pids)


def legacy_process_details(
    pid: int,
    proc_root: Path = Path("/proc"),
    expected_guild_id: str | None = None,
) -> dict[str, object]:
    """Return safe process identity and DB path metadata, never secret values."""
    process = proc_root / str(pid)
    arguments = [
        value.decode(errors="replace")
        for value in (process / "cmdline").read_bytes().split(b"\0")
        if value
    ]
    if not (
        (len(arguments) >= 3 and arguments[1:3] == ["-m", _BOT_MODULE])
        or any(Path(argument).name == "discord_bot.py" for argument in arguments)
    ):
        raise RuntimeError("PID is not the Armory bot module")
    cwd = Path(os.readlink(process / "cwd"))
    database_setting: str | None = None
    token_present = False
    token_file_present = False
    guild_setting_present = False
    guild_setting: str | None = None
    for item in (process / "environ").read_bytes().split(b"\0"):
        if item.startswith(b"ARMORY_DATABASE_PATH="):
            database_setting = item.partition(b"=")[2].decode(errors="replace")
        elif item.startswith(b"DISCORD_TEST_GUILD_ID="):
            guild_setting_present = True
            guild_setting = item.partition(b"=")[2].decode(errors="replace")
        elif item.startswith(b"DISCORD_BOT_TOKEN="):
            token_present = True
        elif item.startswith(b"DISCORD_BOT_TOKEN_FILE="):
            token_file_present = True
    database = (
        Path(database_setting)
        if database_setting
        else Path("data/guild-armory.sqlite3")
    )
    if not database.is_absolute():
        database = cwd / database
    return {
        "pid": pid,
        "cwd": str(cwd),
        "database_path": str(database.resolve()),
        "database_setting_was_explicit": database_setting is not None,
        "token_variable_present": token_present or token_file_present,
        "guild_setting_present": guild_setting_present,
        "guild_matches_expected": (
            guild_setting == expected_guild_id
            if expected_guild_id is not None
            else None
        ),
    }


def _counts(database: Path) -> tuple[int, int, int]:
    uri = f"{database.resolve().as_uri()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=10) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if not _TABLES.issubset(tables):
                raise RuntimeError("source is not a Guild Armory database")
            result = connection.execute(
                "SELECT (SELECT count(*) FROM guilds), "
                "(SELECT count(*) FROM members), "
                "(SELECT count(*) FROM characters)"
            ).fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError("could not inspect Guild Armory database") from exc
    if result is None or result[0] < 1:
        raise RuntimeError("Guild Armory database is empty")
    return int(result[0]), int(result[1]), int(result[2])


def migrate_database(
    source_database: str | Path,
    expected_source_database: str | Path,
    target_database: str | Path,
    backup_directory: str | Path,
    *,
    service_owner: tuple[int, int] | None = None,
    proc_root: Path = Path("/proc"),
) -> tuple[Path, tuple[int, int, int]]:
    """Back up and atomically import a stopped legacy database.

    The old database is read only. The target must not already exist. A shared
    runtime lock and restore marker prevent updated bot processes from starting
    during the import and leave startup fail-closed after an interrupted run.
    """
    source_input = Path(source_database)
    target_input = Path(target_database)
    if source_input.is_symlink() or target_input.is_symlink():
        raise RuntimeError("source and target paths must not be symbolic links")
    source = source_input.resolve(strict=True)
    expected = Path(expected_source_database).resolve(strict=True)
    target = target_input.resolve()
    backups = Path(backup_directory).resolve()
    if source != expected:
        raise RuntimeError("source path does not match the pre-stop confirmation")
    if source == target:
        raise RuntimeError("source and StateDirectory database must differ")
    if not source.is_file() or source.is_symlink():
        raise RuntimeError("legacy source must be a regular database file")
    if target.exists() or target.is_symlink():
        raise RuntimeError("target database already exists; refusing replacement")
    if armory_process_pids(proc_root):
        raise RuntimeError("an Armory process is still running")
    try:
        backups.relative_to(target.parent)
    except ValueError as exc:
        raise RuntimeError(
            "migration backups must remain inside StateDirectory"
        ) from exc

    verify_database(source)
    source_counts = _counts(source)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_directories = [backups, *backups.parents]
    for directory in private_directories:
        if directory == target.parent.parent:
            break
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError("StateDirectory paths must be real directories")
        os.chmod(directory, 0o700)
        if service_owner is not None:
            os.chown(directory, *service_owner)
    marker = restore_marker_path(target)
    marker_created = False
    temporary: Path | None = None
    with exclusive_file_lock(runtime_lock_path(target)):
        if armory_process_pids(proc_root):
            raise RuntimeError("an Armory process started during migration")
        if target.exists() or marker.exists():
            raise RuntimeError("migration target is already present or gated")
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, b"database migration requires review\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        marker_created = True
        try:
            backup = create_backup(source, backups)
            verify_database(backup)
            if _counts(backup) != source_counts:
                raise RuntimeError("backup aggregate counts do not match source")
            if armory_process_pids(proc_root):
                raise RuntimeError("an Armory process appeared during migration")

            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".migration.tmp", dir=target.parent
            )
            temporary = Path(temporary_name)
            os.fchmod(file_descriptor, 0o600)
            with os.fdopen(file_descriptor, "wb") as destination:
                with backup.open("rb") as source_file:
                    shutil.copyfileobj(source_file, destination)
                destination.flush()
                os.fsync(destination.fileno())
            verify_database(temporary)
            if _counts(temporary) != source_counts:
                raise RuntimeError("staged database aggregate counts do not match")
            os.replace(temporary, target)
            temporary = None
            if service_owner is not None:
                os.chown(target, *service_owner)
                os.chown(runtime_lock_path(target), *service_owner)
                os.chown(backups, *service_owner)
                os.chown(backup, *service_owner)
                os.chown(backups / ".backup.lock", *service_owner)
            os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
            verify_database(target)
            if _counts(target) != source_counts:
                raise RuntimeError("imported database aggregate counts do not match")
            if armory_process_pids(proc_root):
                raise RuntimeError("an Armory process appeared before migration ended")
            marker.unlink()
            marker_created = False
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            # Keep the marker on any failed or interrupted import. The bot's
            # startup path refuses to run while this marker exists.
            if not marker_created:
                marker.unlink(missing_ok=True)
    return backup, source_counts


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect-legacy", action="store_true")
    parser.add_argument(
        "--expected-guild-id",
        help="optional observational comparison; never gates migration or startup",
    )
    parser.add_argument("--source-database")
    parser.add_argument(
        "--expected-source-database",
        help="path recorded from the legacy process before it was stopped",
    )
    parser.add_argument(
        "--target-database",
        default="/var/lib/wow-forever-armory-bot/guild-armory.sqlite3",
    )
    parser.add_argument(
        "--backup-directory",
        default="/var/lib/wow-forever-armory-bot/backups/migration",
    )
    parser.add_argument("--service-user", default="wow-armory")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.inspect_legacy:
        try:
            pids = armory_process_pids()
            if not pids:
                print("No Armory module process is running.")
                return 0
            for pid in pids:
                details = legacy_process_details(
                    pid, expected_guild_id=args.expected_guild_id
                )
                print(f"Legacy process: pid={details['pid']} cwd={details['cwd']}")
                print(
                    f"database={details['database_path']} "
                    f"explicit={details['database_setting_was_explicit']} "
                    f"guild_setting_present={details['guild_setting_present']} "
                    f"guild_matches_expected={details['guild_matches_expected']} "
                    f"credential_present={details['token_variable_present']}"
                )
            return 0
        except (OSError, RuntimeError) as exc:
            print(f"Process inspection failed safely: {exc}", file=sys.stderr)
            return 1
    if os.geteuid() != 0:
        print("Run this migration only as an administrator.", file=sys.stderr)
        return 77
    if not args.source_database or not args.expected_source_database:
        print(
            "Migration requires source and pre-stop confirmed database paths.",
            file=sys.stderr,
        )
        return 2
    try:
        import pwd

        account = pwd.getpwnam(args.service_user)
        backup, counts = migrate_database(
            args.source_database,
            args.expected_source_database,
            args.target_database,
            args.backup_directory,
            service_owner=(account.pw_uid, account.pw_gid),
        )
    except (KeyError, OSError, RuntimeError, OperationError) as exc:
        print(f"Migration stopped safely: {exc}", file=sys.stderr)
        return 1
    print(
        "Migration verified; backup retained at "
        f"{backup} (guilds={counts[0]}, members={counts[1]}, characters={counts[2]})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
