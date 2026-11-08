"""Read-only source snapshot validation for production installer gates.

The source database and its sidecars are opened only with read-only OS file
descriptors. SQLite is run exclusively against a private temporary copy.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import struct
import tempfile
from pathlib import Path
from typing import Literal

SNAPSHOT_ROOT = Path("/tmp")
REQUIRED_TABLES = {"guilds", "members", "characters", "flows"}


class SnapshotVerificationError(RuntimeError):
    """Raised when the source cannot be safely snapshotted and verified."""


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        info.st_uid,
        info.st_gid,
    )


def _source_signature(path: Path) -> tuple[int, ...] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise SnapshotVerificationError("database or sidecar is not a regular file")
    return _signature(info)


def _open_readonly(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


def _wal_checksum(
    data: bytes,
    first: int,
    second: int,
    byteorder: Literal["little", "big"],
) -> tuple[int, int]:
    if len(data) % 8:
        raise SnapshotVerificationError("WAL checksum data is misaligned")
    for offset in range(0, len(data), 8):
        word_a = int.from_bytes(data[offset : offset + 4], byteorder)
        word_b = int.from_bytes(data[offset + 4 : offset + 8], byteorder)
        first = (first + word_a + second) & 0xFFFFFFFF
        second = (second + word_b + first) & 0xFFFFFFFF
    return first, second


def _database_page_size(path: Path) -> int:
    descriptor = _open_readonly(path)
    try:
        header = os.pread(descriptor, 100, 0)
    finally:
        os.close(descriptor)
    if len(header) != 100 or header[:16] != b"SQLite format 3\x00":
        raise SnapshotVerificationError("SQLite database header is invalid")
    page_size = int.from_bytes(header[16:18], "big")
    if page_size == 1:
        page_size = 65536
    if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
        raise SnapshotVerificationError("SQLite database page size is invalid")
    return page_size


def _validate_wal(path: Path, database_page_size: int) -> None:
    descriptor = _open_readonly(path)
    try:
        info = os.fstat(descriptor)
        if info.st_size == 0:
            return
        if info.st_size < 32:
            raise SnapshotVerificationError("SQLite WAL header is truncated")
        header = os.pread(descriptor, 32, 0)
        magic = int.from_bytes(header[:4], "big")
        if magic not in (0x377F0682, 0x377F0683):
            raise SnapshotVerificationError("SQLite WAL magic is invalid")
        byteorder: Literal["little", "big"] = "little" if magic == 0x377F0682 else "big"
        version, page_size = struct.unpack(">II", header[4:12])
        if version != 3007000 or page_size != database_page_size:
            raise SnapshotVerificationError("SQLite WAL version or page size mismatch")
        checksum = _wal_checksum(header[:24], 0, 0, byteorder)
        if checksum != struct.unpack(">II", header[24:32]):
            raise SnapshotVerificationError("SQLite WAL header checksum is invalid")
        frame_size = 24 + page_size
        if (info.st_size - 32) % frame_size:
            raise SnapshotVerificationError("SQLite WAL has a truncated frame")
        salts = header[16:24]
        offset = 32
        while offset < info.st_size:
            frame = os.pread(descriptor, frame_size, offset)
            if len(frame) != frame_size or frame[8:16] != salts:
                raise SnapshotVerificationError("SQLite WAL frame is inconsistent")
            checksum = _wal_checksum(frame[:8] + frame[24:], *checksum, byteorder)
            if checksum != struct.unpack(">II", frame[16:24]):
                raise SnapshotVerificationError("SQLite WAL frame checksum is invalid")
            offset += frame_size
    finally:
        os.close(descriptor)


def _validate_sources(
    database: Path, *, expected_owner: int, expected_group: int
) -> tuple[dict[Path, tuple[int, ...] | None], bool]:
    try:
        info = database.lstat()
    except OSError as exc:
        raise SnapshotVerificationError("production database is unavailable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != expected_owner
        or info.st_gid != expected_group
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size == 0
    ):
        raise SnapshotVerificationError("production database metadata is unsafe")

    wal = Path(f"{database}-wal")
    shm = Path(f"{database}-shm")
    journal = Path(f"{database}-journal")
    if os.path.lexists(journal):
        raise SnapshotVerificationError(
            "SQLite rollback journal is present; snapshot verification deferred"
        )
    signatures: dict[Path, tuple[int, ...] | None] = {
        database: _signature(info),
        journal: None,
    }
    wal_present = False
    for sidecar in (wal, shm):
        signature = _source_signature(sidecar)
        signatures[sidecar] = signature
        if signature is None:
            continue
        side_info = sidecar.lstat()
        side_mode = stat.S_IMODE(side_info.st_mode)
        if (
            side_info.st_uid not in {expected_owner, 0}
            or side_info.st_gid not in {expected_group, 0}
            or side_mode & 0o022
        ):
            raise SnapshotVerificationError("SQLite sidecar metadata is unsafe")
        if sidecar == wal:
            wal_present = True
        elif side_info.st_size == 0 or side_info.st_size % 32768:
            raise SnapshotVerificationError("SQLite SHM size is inconsistent")

    page_size = _database_page_size(database)
    if wal_present:
        _validate_wal(wal, page_size)
    return signatures, wal_present


def _copy_readonly_source(
    source: Path, destination: Path, expected_signature: tuple[int, ...]
) -> None:
    source_fd = _open_readonly(source)
    target_fd = -1
    try:
        before = os.fstat(source_fd)
        if _signature(before) != expected_signature:
            raise SnapshotVerificationError("source changed before snapshot copy")
        target_fd = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(target_fd, 0o600)
        copied = 0
        while True:
            block = os.read(source_fd, 1024 * 1024)
            if not block:
                break
            view = memoryview(block)
            while view:
                written = os.write(target_fd, view)
                if written <= 0:
                    raise OSError("short write while creating private snapshot")
                view = view[written:]
            copied += len(block)
        after = os.fstat(source_fd)
        if copied != before.st_size or _signature(after) != expected_signature:
            raise SnapshotVerificationError("source changed during snapshot copy")
        os.fsync(target_fd)
    finally:
        os.close(source_fd)
        if target_fd >= 0:
            os.close(target_fd)


def _verify_snapshot(snapshot: Path) -> tuple[int, int, int]:
    # SQLite may recover or rebuild WAL-index files here, but only in SNAPSHOT_ROOT.
    uri = snapshot.resolve(strict=True).as_uri() + "?mode=rw"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        connection.execute("PRAGMA query_only=ON")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            raise SnapshotVerificationError("SQLite snapshot integrity check failed")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not REQUIRED_TABLES.issubset(tables):
            raise SnapshotVerificationError("snapshot schema is incomplete")
        counts = tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("guilds", "members", "characters")
        )
        return counts
    finally:
        connection.close()


def verify_database_snapshot(
    database: str | Path, *, expected_owner: int, expected_group: int
) -> tuple[int, int, int]:
    """Return aggregate counts after verifying a private DB+WAL snapshot.

    Original files are opened only through O_RDONLY|O_NOFOLLOW descriptors.
    The transient SHM index is validated but not copied; SQLite reconstructs
    it in a mode-0700 temporary directory. Only that temporary directory is
    removed on exit.
    """
    source = Path(database)
    signatures, wal_present = _validate_sources(
        source, expected_owner=expected_owner, expected_group=expected_group
    )
    try:
        root_info = SNAPSHOT_ROOT.lstat()
    except OSError as exc:
        raise SnapshotVerificationError("private snapshot root is unavailable") from exc
    root_mode = stat.S_IMODE(root_info.st_mode)
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid not in {0, os.geteuid()}
        or (root_mode & 0o022 and not root_mode & stat.S_ISVTX)
    ):
        raise SnapshotVerificationError("private snapshot root is unsafe")

    try:
        with tempfile.TemporaryDirectory(
            prefix="armory-db-snapshot-", dir=SNAPSHOT_ROOT
        ) as temp_name:
            temp_dir = Path(temp_name)
            os.chmod(temp_dir, 0o700)
            if stat.S_IMODE(temp_dir.lstat().st_mode) != 0o700:
                raise SnapshotVerificationError("snapshot directory is not private")
            snapshot = temp_dir / "database.sqlite3"
            database_signature = signatures[source]
            if database_signature is None:
                raise SnapshotVerificationError("database disappeared before copy")
            _copy_readonly_source(source, snapshot, database_signature)
            wal = Path(f"{source}-wal")
            if wal_present:
                wal_signature = signatures[wal]
                if wal_signature is None:
                    raise SnapshotVerificationError("WAL disappeared before copy")
                _copy_readonly_source(wal, Path(f"{snapshot}-wal"), wal_signature)

            # Confirm database, WAL, and SHM did not change during either copy.
            current = {path: _source_signature(path) for path in signatures}
            if current != signatures:
                raise SnapshotVerificationError(
                    "source changed during snapshot creation"
                )
            return _verify_snapshot(snapshot)
    except (OSError, sqlite3.Error, ValueError, struct.error) as exc:
        raise SnapshotVerificationError(
            "isolated database snapshot verification failed"
        ) from exc


__all__ = ["SnapshotVerificationError", "verify_database_snapshot"]
