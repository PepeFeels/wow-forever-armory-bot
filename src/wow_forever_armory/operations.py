"""Offline-safe SQLite backup, restore, and integrity operations.

This module is intended for explicit operator invocations. It never contacts
Discord. Restore requires both a stopped bot (the shared runtime lock) and an
explicit confirmation flag.
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .logging_config import configure_logging

_LOGGER = logging.getLogger("wow_forever_armory.operations")
_BACKUP_PREFIX = "guild-armory-"
_BACKUP_SUFFIX = ".sqlite3"


class OperationError(RuntimeError):
    """A safe operational failure suitable for CLI display."""


class LockUnavailable(OperationError):
    """Another process currently owns an exclusive operational lock."""


@contextmanager
def exclusive_file_lock(path: str | Path) -> Iterator[None]:
    """Hold a restrictive, nonblocking advisory lock for the context lifetime."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise OperationError("could not access the operational lock") from exc
    try:
        try:
            os.fchmod(descriptor, 0o600)
        except OSError as exc:
            raise OperationError("could not secure the operational lock") from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockUnavailable("another operation is using this database") from exc
        except OSError as exc:
            raise OperationError("could not acquire the operational lock") from exc
        yield
    finally:
        os.close(descriptor)


def runtime_lock_path(database: str | Path) -> Path:
    """Return the lock path shared by the running bot and restore command."""
    return Path(f"{Path(database).resolve()}.runtime.lock")


def database_file_lock_path(database: str | Path) -> Path:
    """Use the database inode as a second lock to cover hard-link aliases."""
    return Path(database).resolve()


def backup_operation_lock_path(database: str | Path) -> Path:
    """Return the shared backup/restore maintenance lock for a database."""
    return Path(f"{Path(database).resolve()}.backup.lock")


def restore_marker_path(database: str | Path) -> Path:
    """Return a fail-closed marker path for interrupted restore operations."""
    return Path(f"{Path(database).resolve()}.restore-in-progress")


def _readonly_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise OperationError("database or backup file does not exist")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True, timeout=10)
    except sqlite3.Error as exc:
        raise OperationError("could not open SQLite file") from exc


def verify_database(path: str | Path) -> None:
    """Raise OperationError unless SQLite's full integrity check succeeds."""
    database = Path(path)
    connection: sqlite3.Connection | None = None
    try:
        connection = _readonly_connection(database)
        result = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        raise OperationError("SQLite integrity check failed") from exc
    finally:
        if connection is not None:
            connection.close()
    if result is None or result[0] != "ok":
        raise OperationError("SQLite integrity check failed")


def _backup_to(source_path: Path, destination_path: Path) -> None:
    if not source_path.is_file():
        raise OperationError("database or backup file does not exist")
    guard: sqlite3.Connection | None = None
    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    try:
        # Pin a stable source snapshot. SQLite's online backup API restarts if
        # another writer commits mid-copy; a short reserved lock prevents
        # unbounded restarts while still allowing existing readers.
        guard = sqlite3.connect(source_path, timeout=30, isolation_level=None)
        guard.execute("BEGIN IMMEDIATE")
        source = _readonly_connection(source_path)
        destination = sqlite3.connect(destination_path, timeout=10)
        source.backup(destination, pages=256, sleep=0.05)
        result = destination.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise OperationError("new SQLite copy failed integrity check")
    except sqlite3.Error as exc:
        raise OperationError("SQLite backup operation failed") from exc
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
        if guard is not None:
            if guard.in_transaction:
                guard.rollback()
            guard.close()


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_verified_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        _backup_to(source, temporary)
        os.chmod(temporary, 0o600)
        _fsync_file(temporary)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _write_raw_copy(source: Path, destination: Path) -> None:
    """Preserve exact bytes when the source cannot form a valid SQLite copy."""
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".raw.tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with source.open("rb") as source_file, temporary.open("wb") as target_file:
            shutil.copyfileobj(source_file, target_file)
            target_file.flush()
            os.fsync(target_file.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _remove_unpublished_temporary_files(
    directory: Path, patterns: tuple[str, ...]
) -> None:
    """Remove only this tool's unpublished temp naming patterns under a lock."""
    for pattern in patterns:
        for temporary in directory.glob(pattern):
            if not (temporary.is_file() or temporary.is_symlink()):
                continue
            try:
                temporary.unlink()
            except OSError as exc:
                raise OperationError(
                    "could not remove an incomplete operational temporary file"
                ) from exc
            _LOGGER.warning("incomplete_temporary_file_removed")


def _prune_backups(directory: Path, keep: int, current: Path) -> None:
    candidates = sorted(
        (
            path
            for path in directory.glob(f"{_BACKUP_PREFIX}*{_BACKUP_SUFFIX}")
            if path != current
        ),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    for old in candidates[max(keep - 1, 0) :]:
        try:
            old.unlink()
        except OSError:
            _LOGGER.warning("backup_retention_remove_failed")


def create_backup(
    database: str | Path,
    backup_directory: str | Path,
    *,
    retention: int = 14,
) -> Path:
    """Create and verify an online SQLite backup, then apply retention."""
    if retention < 1:
        raise OperationError("retention must be at least one backup")
    source = Path(database)
    directory = Path(backup_directory)
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink():
            raise OperationError("backup directory must not be a symbolic link")
        os.chmod(directory, 0o700)
        with exclusive_file_lock(backup_operation_lock_path(source)):
            with exclusive_file_lock(directory / ".backup.lock"):
                _remove_unpublished_temporary_files(
                    directory, (f".{_BACKUP_PREFIX}*{_BACKUP_SUFFIX}.*.tmp",)
                )
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
                destination = directory / f"{_BACKUP_PREFIX}{stamp}{_BACKUP_SUFFIX}"
                try:
                    _write_verified_copy(source, destination)
                except OSError as exc:
                    _LOGGER.error("backup_failed", extra={"reason": "filesystem"})
                    raise OperationError(
                        "backup could not be safely published"
                    ) from exc
                except OperationError:
                    _LOGGER.error(
                        "backup_failed", extra={"reason": "integrity_or_sqlite"}
                    )
                    raise
                _prune_backups(directory, retention, destination)
    except OSError as exc:
        _LOGGER.error("backup_failed", extra={"reason": "filesystem_or_permissions"})
        raise OperationError("backup could not access its configured paths") from exc
    _LOGGER.info("backup_completed")
    return destination


def restore_backup(
    database: str | Path,
    backup: str | Path,
    *,
    confirmed: bool = False,
) -> Path | None:
    """Restore a verified backup while stopped, preserving the old DB for rollback."""
    target = Path(database).resolve()
    source = Path(backup)
    if not confirmed:
        raise OperationError("restore requires explicit replacement confirmation")
    if source.resolve() == target.resolve():
        raise OperationError("backup and destination must be different files")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OperationError("restore could not access its destination") from exc

    with exclusive_file_lock(backup_operation_lock_path(target)):
        with exclusive_file_lock(source.parent / ".backup.lock"):
            verify_database(source)
            with exclusive_file_lock(runtime_lock_path(target)):
                database_existed = target.exists()
                with exclusive_file_lock(database_file_lock_path(target)):
                    marker = restore_marker_path(target)
                    if marker.exists():
                        raise OperationError(
                            "an interrupted restore needs operator recovery "
                            "before continuing"
                        )
                    _remove_unpublished_temporary_files(
                        target.parent,
                        (
                            f".{target.name}.*.restore.tmp",
                            f".{target.name}.pre-restore-*.sqlite3.*.tmp",
                            f".{target.name}.pre-restore-corrupt-*.sqlite3.*.raw.tmp",
                        ),
                    )
                    rollback: Path | None = None
                    if database_existed:
                        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
                        try:
                            verify_database(target)
                        except OperationError:
                            rollback = target.parent / (
                                f"{target.name}.pre-restore-corrupt-{stamp}.sqlite3"
                            )
                            try:
                                _write_raw_copy(target, rollback)
                                for suffix in ("-wal", "-shm"):
                                    sidecar = Path(f"{target}{suffix}")
                                    if sidecar.exists():
                                        _write_raw_copy(
                                            sidecar, Path(f"{rollback}{suffix}")
                                        )
                            except OSError as exc:
                                raise OperationError(
                                    "could not preserve the corrupt database files; "
                                    "restore was not applied"
                                ) from exc
                        else:
                            rollback = target.parent / (
                                f"{target.name}.pre-restore-{stamp}.sqlite3"
                            )
                            try:
                                _write_verified_copy(target, rollback)
                            except (OSError, OperationError) as exc:
                                raise OperationError(
                                    "could not create a verified rollback copy; "
                                    "restore was not applied"
                                ) from exc

                    descriptor, temporary_name = tempfile.mkstemp(
                        prefix=f".{target.name}.",
                        suffix=".restore.tmp",
                        dir=target.parent,
                    )
                    os.close(descriptor)
                    temporary = Path(temporary_name)
                    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
                    sidecars: list[tuple[Path, Path]] = []
                    replaced = False
                    marker_created = False
                    try:
                        os.chmod(temporary, 0o600)
                        _backup_to(source, temporary)
                        _fsync_file(temporary)
                        marker_descriptor = os.open(
                            marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                        )
                        try:
                            os.write(
                                marker_descriptor,
                                b"restore requires operator review\n",
                            )
                            os.fsync(marker_descriptor)
                        finally:
                            os.close(marker_descriptor)
                        marker_created = True
                        _fsync_directory(target.parent)
                        for suffix in ("-wal", "-shm"):
                            sidecar = Path(f"{target}{suffix}")
                            if sidecar.exists():
                                preserved = Path(
                                    f"{target}.pre-restore-{stamp}{suffix}"
                                )
                                os.replace(sidecar, preserved)
                                sidecars.append((sidecar, preserved))
                        os.replace(temporary, target)
                        replaced = True
                        _fsync_directory(target.parent)
                        verify_database(target)
                        marker.unlink()
                        marker_created = False
                        _fsync_directory(target.parent)
                    except BaseException as exc:
                        temporary.unlink(missing_ok=True)
                        if not replaced:
                            restored = True
                            for original, preserved in reversed(sidecars):
                                if preserved.exists() and not original.exists():
                                    try:
                                        os.replace(preserved, original)
                                    except OSError:
                                        restored = False
                            if marker_created and restored:
                                marker.unlink(missing_ok=True)
                        if isinstance(exc, OperationError):
                            raise
                        raise OperationError(
                            "restore could not be safely completed"
                        ) from exc

    _LOGGER.info("restore_completed")
    return rollback


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WoW Forever Armory database tools")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("backup", help="create a verified SQLite backup")
    backup.add_argument("--database", default=os.getenv("ARMORY_DATABASE_PATH"))
    backup.add_argument("--directory", default=os.getenv("ARMORY_BACKUP_DIR"))

    try:
        retention_default = int(os.getenv("ARMORY_BACKUP_RETENTION", "14"))
    except ValueError:
        retention_default = 0
    backup.add_argument("--retention", type=int, default=retention_default)
    restore = subparsers.add_parser("restore", help="restore a verified SQLite backup")
    restore.add_argument("--database", default=os.getenv("ARMORY_DATABASE_PATH"))
    restore.add_argument("--backup", required=True)
    restore.add_argument("--confirm-replace", action="store_true")
    check = subparsers.add_parser("check", help="run SQLite integrity_check")
    check.add_argument("--database", default=os.getenv("ARMORY_DATABASE_PATH"))
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if not args.database:
            raise OperationError("database path must be configured")
        if args.command == "backup":
            if not args.directory:
                raise OperationError("backup directory must be configured")
            create_backup(args.database, args.directory, retention=args.retention)
        elif args.command == "restore":
            restore_backup(args.database, args.backup, confirmed=args.confirm_replace)
        else:
            verify_database(args.database)
            _LOGGER.info("database_integrity_ok")
    except OperationError as exc:
        _LOGGER.error("operation_failed: %s", str(exc))
        return 1
    return 0


if __name__ == "__main__":
    configure_logging()
    raise SystemExit(main())
