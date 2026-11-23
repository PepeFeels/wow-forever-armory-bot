#!/usr/bin/python3
"""Read-only startup gate for an initialized Guild Armory database."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pwd
import re
import stat
import sys
from pathlib import Path

AUDIT_DIRECTORY = Path("/var/lib/armory-deployment-audit")
INSTALL_BASE = Path("/opt/wow-forever-armory-bot")
ACTIVATION_LOCK = Path("/run/lock/armory-service-activation.lock")
ACTIVE_JOURNAL = "deployment-state.json"
JOURNAL_TEMPORARY = ".deployment-state.json.tmp"
ARCHIVE_PATTERN = re.compile(r"deployment-state-([0-9a-f]{64})\.json\Z")
ACK_PATTERN = re.compile(r"deployment-recovery-ack-([0-9a-f]{64})\.json\Z")
MANIFEST_PATTERN = re.compile(r"deployment-([0-9a-f]{40})\.json\Z")
ACK_FIELDS = {
    "schema_version",
    "journal_sha256",
    "manifest_sha256",
    "approved_release_sha",
    "previous_release_sha",
    "decision",
    "approved_by",
    "approval_reference",
    "approved_at_utc",
}


def _read_audit_json(
    directory_fd: int, name: str, *, expected_owner: int, expected_group: int
):
    """Read a private audit JSON file once, without following or racing links."""
    before_entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(before_entry.st_mode)
        or before_entry.st_uid != expected_owner
        or before_entry.st_gid != expected_group
        or stat.S_IMODE(before_entry.st_mode) != 0o600
        or before_entry.st_nlink != 1
        or before_entry.st_size > 65536
    ):
        raise OSError("audit JSON metadata is unsafe")
    fd = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0),
        dir_fd=directory_fd,
    )
    try:
        before_fd = os.fstat(fd)

        def stable(info):
            return (
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_uid,
                info.st_gid,
                info.st_nlink,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )

        if stable(before_entry) != stable(before_fd):
            raise OSError("audit JSON changed while opening")
        chunks = []
        total = 0
        while True:
            block = os.read(fd, min(65536, 65537 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > 65536:
                raise OSError("audit JSON exceeds size limit")
        raw = b"".join(chunks)
        after_fd = os.fstat(fd)
        after_entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            stable(before_fd) != stable(after_fd)
            or stable(after_fd) != stable(after_entry)
            or len(raw) != after_fd.st_size
        ):
            raise OSError("audit JSON changed while reading")
    finally:
        os.close(fd)

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate audit JSON key")
            result[key] = value
        return result

    value = json.loads(raw.decode("ascii"), object_pairs_hook=unique_pairs)
    if not isinstance(value, dict):
        raise ValueError("audit JSON must be an object")
    return raw, value


RECOVERY_AUDIT_SHA256 = (
    "02465785fa96cf13fce668e8df59d35dfa0bd2d7eb227153ff74f6b7697de5fc"
)


def _load_recovery_audit(expected_owner: int, expected_group: int):
    source = Path(__file__).with_name("recovery_audit.py")
    if not source.exists():
        source = Path(__file__).parents[1] / "recovery_audit.py"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(source, flags)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != expected_owner
            or info.st_gid != expected_group
            or stat.S_IMODE(info.st_mode) != 0o644
            or info.st_nlink != 1
        ):
            raise OSError("shared recovery validator metadata is unsafe")
        chunks = []
        while True:
            block = os.read(fd, 65536)
            if not block:
                break
            chunks.append(block)
        data = b"".join(chunks)
        if hashlib.sha256(data).hexdigest() != RECOVERY_AUDIT_SHA256:
            raise OSError("shared recovery validator hash mismatch")
        if os.fstat(fd).st_ino != info.st_ino or os.fstat(fd).st_size != info.st_size:
            raise OSError("shared recovery validator changed while reading")
    finally:
        os.close(fd)
    namespace = {"__file__": str(source), "__name__": "_armory_recovery_audit"}
    exec(compile(data, str(source), "exec", dont_inherit=True), namespace)
    return namespace


def deployment_recovery_is_clear(
    audit_directory: str | Path = AUDIT_DIRECTORY,
    *,
    expected_owner: int = 0,
    expected_group: int = 0,
    activation_release: str | None = None,
    activation_role: str = "bot",
    activation_in_progress: bool = False,
) -> bool:
    """Fail closed unless the shared, hash-verified validator accepts history."""
    try:
        validator = _load_recovery_audit(expected_owner, expected_group)
        validator["verify_audit_directory"](
            audit_directory,
            owner=expected_owner,
            group=expected_group,
            activation_release=activation_release,
            activation_role=activation_role,
            activation_in_progress=activation_in_progress,
            preserved_historical_directories=("reviewed-v2.0.0", "archive"),
        )
        return True
    except OSError, ValueError, TypeError, KeyError:
        return False


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


def _current_release_sha() -> str | None:
    current = INSTALL_BASE / "current"
    try:
        info = current.lstat()
        raw = os.readlink(current)
        match = re.fullmatch(r"releases/([0-9a-f]{40})", raw)
        if not stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or match is None:
            return None
        release = match.group(1)
        target = INSTALL_BASE / raw
        if current.resolve(strict=True) != target:
            return None
        release_fd = os.open(
            target,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            release_info = os.fstat(release_fd)
            if (
                not stat.S_ISDIR(release_info.st_mode)
                or release_info.st_uid != 0
                or release_info.st_mode & 0o022
            ):
                return None
            marker_fd = os.open(
                ".installation-complete",
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=release_fd,
            )
            try:
                marker_info = os.fstat(marker_fd)
                marker = os.read(marker_fd, 129)
                if (
                    not stat.S_ISREG(marker_info.st_mode)
                    or marker_info.st_uid != 0
                    or stat.S_IMODE(marker_info.st_mode) != 0o444
                    or marker_info.st_nlink != 1
                    or marker != release.encode("ascii")
                    or os.fstat(marker_fd).st_size != len(marker)
                ):
                    return None
            finally:
                os.close(marker_fd)
        finally:
            os.close(release_fd)
        after = current.lstat()
        if (after.st_dev, after.st_ino, after.st_uid, after.st_ctime_ns) != (
            info.st_dev,
            info.st_ino,
            info.st_uid,
            info.st_ctime_ns,
        ) or os.readlink(current) != raw:
            return None
        return release
    except OSError:
        return None


def _activation_lock_is_held(
    path: Path = ACTIVATION_LOCK, *, expected_owner: int = 0
) -> bool:
    """Return true only while a protected activation operation holds its lock."""
    try:
        parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != expected_owner
            or not parent.st_mode & stat.S_ISVTX
        ):
            return False
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != expected_owner
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                return False
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)
    except OSError:
        return False


def database_metadata_is_safe(
    path: str | Path,
    *,
    expected_owner: int | None = None,
    expected_group: int | None = None,
) -> bool:
    """Check database path metadata without opening SQLite or touching sidecars."""
    database = Path(path)
    marker = Path(f"{database}.restore-in-progress")
    try:
        try:
            marker.lstat()
            return False
        except FileNotFoundError:
            pass

        parent = database.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or expected_owner is not None and parent.st_uid != expected_owner
            or expected_group is not None and parent.st_gid != expected_group
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            return False

        info = database.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or expected_owner is not None and info.st_uid != expected_owner
            or expected_group is not None and info.st_gid != expected_group
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size == 0
        ):
            return False

        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{database}{suffix}")
            try:
                side_info = sidecar.lstat()
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(side_info.st_mode)
                or expected_owner is not None and side_info.st_uid != expected_owner
                or expected_group is not None and side_info.st_gid != expected_group
                or stat.S_IMODE(side_info.st_mode) != 0o600
                or side_info.st_nlink != 1
            ):
                return False
            if suffix == "-journal":
                return False
        return True
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit", choices={"bot", "backup"})
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    path = os.getenv("ARMORY_DATABASE_PATH", "")
    release = _current_release_sha() if args.unit else None
    if args.unit and release is None:
        print("The installed release pointer or marker is invalid.", file=sys.stderr)
        return 78
    if not deployment_recovery_is_clear(
        activation_release=release,
        activation_role=args.unit or "bot",
        activation_in_progress=_activation_lock_is_held(),
    ):
        print(
            "An unresolved deployment recovery record blocks bot startup.",
            file=sys.stderr,
        )
        return 78
    if args.unit != "backup" and armory_process_present():
        print("An Armory bot process is already running.", file=sys.stderr)
        return 78
    try:
        service_account = pwd.getpwnam("wow-armory")
    except KeyError:
        print("Armory service account is unavailable.", file=sys.stderr)
        return 78
    if not path or not database_metadata_is_safe(
        path,
        expected_owner=service_account.pw_uid,
        expected_group=service_account.pw_gid,
    ):
        print(
            "Armory database metadata is unsafe or restore is in progress.",
            file=sys.stderr,
        )
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
