#!/usr/bin/python3.14 -I
"""Administrative, fail-closed helpers for release pins and recovery records.

This tool is intended to be copied to a separately reviewed, root-owned path
before use. It never starts, stops, enables, or disables a service and it never
reads or writes a database. Recovery acknowledgment archives the active journal
and writes a hash-bound administrative attestation only after a separate
root-owned review record reports every required check as PASS.
"""

from __future__ import annotations

import argparse
import fcntl
import grp
import hashlib
import json
import os
import pwd
import re
import secrets
import stat
import subprocess
import sys
import types
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PIN_PATH = Path("/etc/wow-forever-armory-bot/deployment-pin.json")
PIN_HISTORY_DIR = PIN_PATH.parent / "deployment-pin-history"
AUDIT_DIR = Path("/var/lib/armory-deployment-audit")
MAX_JSON_BYTES = 65_536
MAX_SOURCE_BYTES = 1_048_576
SYSTEM_PYTHON = Path("/usr/bin/python3.14")
VENV_SYMLINK_TARGETS = {
    ".venv/lib64": "lib",
    ".venv/bin/python": "python3.14",
    ".venv/bin/python3": "python3.14",
    ".venv/bin/python3.14": "/usr/bin/python3.14",
    ".venv/bin/𝜋thon": "python3.14",
}
PIN_KEYS = frozenset({"release_sha", "previous_sha", "installer_sha256"})
RECOVERY_CHECKS = frozenset(
    {
        "release_pointer",
        "installation_marker",
        "release_permissions",
        "database_signature",
        "database_integrity",
        "backup_metadata",
        "backup_sha256",
        "backup_integrity",
        "manifest",
        "saved_preflight_sha256",
        "bot_unit_state",
        "backup_timer_state",
        "backup_service_state",
    }
)
PRE_SHUTDOWN_ACK_FIELDS = frozenset(
    {
        "schema_version",
        "journal_sha256",
        "approved_release_sha",
        "previous_release_sha",
        "decision",
        "approved_by",
        "approval_reference",
        "approved_at_utc",
        "evidence",
    }
)
UNIT_REQUIREMENTS = {
    "wow-forever-armory-bot.service": ("inactive", "disabled"),
    "wow-forever-armory-backup.timer": ("inactive", "disabled"),
}
BACKUP_SERVICE = "wow-forever-armory-backup.service"
COMPLETION_BASE = Path("/opt/wow-forever-armory-bot")
COMPLETION_DATABASE = Path("/var/lib/wow-forever-armory-bot/guild-armory.sqlite3")
COMPLETION_INSTALLER = COMPLETION_BASE / "bin/production_install.py"
COMPLETION_AUDIT_VALIDATOR = COMPLETION_BASE / "bin/recovery_audit.py"
RECOVERY_AUDIT_SHA256 = (
    "02465785fa96cf13fce668e8df59d35dfa0bd2d7eb227153ff74f6b7697de5fc"
)
TASK301_JOURNAL_SHA256 = "b957b1735570ad821ddbc5f4f8bcaa82ca635e1393394a439879bef531a7f4ca"
TASK301_RELEASE_SHA = "fa15f4e3095fad60ff48a059f7ce07414633d07d"
TASK301_PREVIOUS_SHA = "dcceeb85464e09e34b112e41b16a833226c72270"
TASK301_DATABASE_SHA256 = "4b7e10b846c31fd080f21449478475f783cba00964f0b532b62606c356e1a998"
TASK301_BACKUP = Path(
    "/var/lib/wow-forever-armory-bot/backups/active/"
    "guild-armory-20261123T173904.584416Z.sqlite3"
)
TASK301_BACKUP_SHA256 = "697b031ced2bf45818eafeb8b4d2556784da04873b1565ce6e1678007a112b5c"
TASK301_HISTORICAL_PREFLIGHT = AUDIT_DIR / (
    "armory-db-preflight.dcceeb85464e09e34b112e41b16a833226c72270.py"
)
TASK301_HISTORICAL_PREFLIGHT_SHA256 = (
    "c1c4f067fa2b465726d374ecb06acfae82a1ec234dc0f38c4734a6c422089803"
)
COMPLETION_SNAPSHOT_MODULE = COMPLETION_BASE / "bin/production_db_snapshot.py"
SNAPSHOT_MODULE_SHA256 = (
    "564fb40f430c9f96434ab03a3e008a631a7cb7e556f1f63b16f4e9357c2111c8"
)
TASK267_REPORT = Path(
    "/home/nolo-admin/armory-current-db-verification-200f-99da0f81c2e53ff3.json"
)
TASK267_REPORT_SHA256 = "8ffae1176165b46c0e06733ac9d34a937ee033a0d6886a3bbcab7575b7cef96a"
TASK267_JOURNAL_SHA256 = "f16dd8394724b6e15797d9ac7a7f6f39241cb58dadf86b05347e2372b874e6cb"
TASK267_MANIFEST_SHA256 = "9628fb0b363b724d9e88349fef9d100aa75297d16bf663a26263e793ac32d116"
TASK267_INCIDENT_BACKUP_SHA256 = "e15bd8d7434246f71b4580437c63bd2f79b572c6d104b05b6e2c28f3d05345c9"
TASK267_LATEST_BACKUP = Path("/var/lib/wow-forever-armory-bot/backups/guild-armory-20261115T001002.517528Z.sqlite3")
TASK267_LATEST_BACKUP_SHA256 = "bc87d4f26b213fdca1466fffbd6ec5b9c707f6c2fdaa71927574f365b494cbc0"
TASK267_PREFLIGHT_SHA256 = "c1c4f067fa2b465726d374ecb06acfae82a1ec234dc0f38c4734a6c422089803"
TASK267_ATTESTATION_SHA256 = "dc82c79862053f64f460e41cc18bca6a90308545d4a4ac56781c09628dde09c4"
TASK267_REVIEW_SHA256 = "784a8759817045c2c9b59545022e43c4cf23ae7772d26f37f61ebcb69e0c308f"
ARCHIVE_NAME_RE = re.compile(r"deployment-state-([0-9a-f]{64})\.json\Z")
ACTIVATION_EVENT_RE = re.compile(
    r"service-activation-([0-9a-f]{64})-([0-9a-f]{32})-([0-9]{4})\.json\Z"
)


class AdminError(RuntimeError):
    """A safe, non-sensitive administrative validation failure."""


def _signature(info: os.stat_result) -> tuple[int, ...]:
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


def _stable_signature(info: os.stat_result) -> tuple[int, ...]:
    """Identity stable across hard-link creation (link count/ctime changes)."""
    values = _signature(info)
    return values[:5] + values[6:8]


def _open_private_directory(directory: Path, *, owner_uid: int, exact_mode: int) -> int:
    if not hasattr(os, "O_NOATIME"):
        raise AdminError("filesystem cannot guarantee atime-safe reads")
    try:
        if not directory.is_absolute() or directory.resolve(strict=True) != directory:
            raise AdminError("administrative directory path is not canonical")
        entry = directory.lstat()
        if (
            not stat.S_ISDIR(entry.st_mode)
            or entry.st_uid != owner_uid
            or stat.S_IMODE(entry.st_mode) != exact_mode
        ):
            raise AdminError("administrative directory metadata is unsafe")
        fd = os.open(
            directory,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NOATIME", 0),
        )
    except (OSError, RuntimeError) as exc:
        raise AdminError("administrative directory is unavailable") from exc
    opened = os.fstat(fd)
    if _signature(opened)[:2] != _signature(entry)[:2]:
        os.close(fd)
        raise AdminError("administrative directory changed while opening")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise AdminError(
            "another administrative operation holds the directory"
        ) from exc
    return fd


def _verify_directory_binding(
    directory_fd: int,
    directory: Path,
    *,
    owner_uid: int,
    exact_mode: int,
) -> None:
    try:
        if directory.resolve(strict=True) != directory:
            raise AdminError("administrative directory path changed")
        opened = os.fstat(directory_fd)
        entry = directory.lstat()
    except (OSError, RuntimeError) as exc:
        raise AdminError("administrative directory path is unavailable") from exc
    if (
        not stat.S_ISDIR(entry.st_mode)
        or entry.st_uid != owner_uid
        or stat.S_IMODE(entry.st_mode) != exact_mode
        or (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino)
    ):
        raise AdminError("administrative directory path changed during operation")


def _json_object(raw: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=unique)
    except (UnicodeError, ValueError) as exc:
        raise AdminError("administrative JSON is invalid") from exc
    if not isinstance(value, dict):
        raise AdminError("administrative JSON must be an object")
    return value


def _read_at(
    directory_fd: int,
    name: str,
    *,
    owner_uid: int,
    mode: int,
    maximum: int = MAX_JSON_BYTES,
    expected_nlink: int = 1,
) -> tuple[bytes, os.stat_result]:
    if not hasattr(os, "O_NOATIME"):
        raise AdminError("filesystem cannot guarantee atime-safe reads")
    if not name or name in {".", ".."} or "/" in name:
        raise AdminError("administrative filename is invalid")
    try:
        before_entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        fd = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOATIME", 0),
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise AdminError("administrative file is unavailable") from exc
    try:
        before_fd = os.fstat(fd)
        if (
            not stat.S_ISREG(before_fd.st_mode)
            or before_fd.st_uid != owner_uid
            or stat.S_IMODE(before_fd.st_mode) != mode
            or before_fd.st_nlink != expected_nlink
            or before_fd.st_size > maximum
            or _signature(before_fd) != _signature(before_entry)
        ):
            raise AdminError("administrative file metadata is unsafe")
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(fd, min(65_536, maximum + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > maximum:
                raise AdminError("administrative file exceeds its size limit")
        raw = b"".join(chunks)
        after_fd = os.fstat(fd)
        after_entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            _signature(before_fd) != _signature(after_fd)
            or _signature(after_fd) != _signature(after_entry)
            or len(raw) != after_fd.st_size
        ):
            raise AdminError("administrative file changed while reading")
        return raw, after_fd
    except OSError as exc:
        raise AdminError("administrative file could not be read safely") from exc
    finally:
        os.close(fd)


def read_secure_json(
    path: Path,
    *,
    owner_uid: int,
    mode: int = 0o600,
    parent_mode: int = 0o700,
) -> tuple[dict[str, Any], bytes]:
    """Read one private JSON file without following links or trusting a path race."""
    directory_fd = _open_private_directory(
        path.parent, owner_uid=owner_uid, exact_mode=parent_mode
    )
    try:
        raw, _ = _read_at(directory_fd, path.name, owner_uid=owner_uid, mode=mode)
        _verify_directory_binding(
            directory_fd,
            path.parent,
            owner_uid=owner_uid,
            exact_mode=parent_mode,
        )
    finally:
        os.close(directory_fd)
    return _json_object(raw), raw


def _write_new_at(
    directory_fd: int,
    name: str,
    raw: bytes,
    *,
    owner_uid: int,
    owner_gid: int,
    mode: int,
) -> None:
    """Publish a complete file with link-based no-replace semantics."""
    if not name or name in {".", ".."} or "/" in name:
        raise AdminError("administrative filename is invalid")
    _assert_no_stale_temporary(directory_fd, name)
    temporary = f".{name}.tmp.{secrets.token_hex(16)}"
    fd = -1
    created = False
    original_info: os.stat_result | None = None
    try:
        fd = os.open(
            temporary,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            mode,
            dir_fd=directory_fd,
        )
        created = True
        if os.geteuid() == 0:
            os.fchown(fd, owner_uid, owner_gid)
        os.fchmod(fd, mode)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != owner_uid
            or info.st_gid != owner_gid
            or stat.S_IMODE(info.st_mode) != mode
            or info.st_nlink != 1
        ):
            raise AdminError("new administrative file metadata is unsafe")
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise AdminError("administrative file write was incomplete")
            view = view[written:]
        os.fsync(fd)
        if os.pread(fd, len(raw) + 1, 0) != raw:
            raise AdminError("administrative file read-back did not match")
        final_info = os.fstat(fd)
        if (
            final_info.st_size != len(raw)
            or final_info.st_nlink != 1
            or stat.S_IMODE(final_info.st_mode) != mode
        ):
            raise AdminError("administrative file changed before publication")
        temp_entry = os.stat(temporary, dir_fd=directory_fd, follow_symlinks=False)
        if _signature(temp_entry) != _signature(final_info):
            raise AdminError("temporary administrative file path changed")
        original_info = final_info
        # link() is atomic and fails if the destination already exists,
        # including when it is a symlink. Both names are in the same directory.
        os.link(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.fsync(directory_fd)
        linked_temp = os.stat(temporary, dir_fd=directory_fd, follow_symlinks=False)
        if (
            original_info is None
            or _stable_signature(linked_temp) != _stable_signature(original_info)
            or linked_temp.st_nlink != 2
        ):
            raise AdminError("temporary administrative link changed")
        os.unlink(temporary, dir_fd=directory_fd)
        os.fsync(directory_fd)
        final_entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        final_fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        try:
            before_read = os.fstat(final_fd)
            verified = os.read(final_fd, len(raw) + 1)
            after_read = os.fstat(final_fd)
            current_entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                _signature(before_read) != _signature(final_entry)
                or _signature(after_read) != _signature(before_read)
                or _signature(current_entry) != _signature(after_read)
                or after_read.st_uid != owner_uid
                or after_read.st_gid != owner_gid
                or stat.S_IMODE(after_read.st_mode) != mode
                or after_read.st_nlink != 1
                or verified != raw
            ):
                raise AdminError("published administrative file failed verification")
        finally:
            os.close(final_fd)
    except FileExistsError as exc:
        raise AdminError(
            "refusing to overwrite an existing administrative file"
        ) from exc
    except OSError as exc:
        raise AdminError("administrative file could not be published safely") from exc
    finally:
        if created and fd >= 0:
            try:
                temp_entry = os.stat(
                    temporary, dir_fd=directory_fd, follow_symlinks=False
                )
                open_info = os.fstat(fd)
                if (temp_entry.st_dev, temp_entry.st_ino) == (
                    open_info.st_dev,
                    open_info.st_ino,
                ):
                    os.unlink(temporary, dir_fd=directory_fd)
                    try:
                        os.fsync(directory_fd)
                    except OSError:
                        pass
            except FileNotFoundError:
                pass
        if fd >= 0:
            os.close(fd)


def _assert_no_stale_temporary(directory_fd: int, name: str) -> None:
    prefix = f".{name}.tmp."
    try:
        if any(entry.startswith(prefix) for entry in os.listdir(directory_fd)):
            raise AdminError("a stale administrative temporary file needs review")
    except OSError as exc:
        raise AdminError("administrative directory could not be inventoried") from exc


def _clean_ack_temporaries(
    directory_fd: int,
    name: str,
    *,
    owner_uid: int,
    owner_gid: int,
    mode: int,
) -> None:
    """Remove only private interrupted ACK temp files after fresh verification."""
    prefix = f".{name}.tmp."
    removed = False
    try:
        for temporary in os.listdir(directory_fd):
            if not temporary.startswith(prefix):
                continue
            entry = os.stat(temporary, dir_fd=directory_fd, follow_symlinks=False)
            fd = os.open(
                temporary,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_fd,
            )
            try:
                opened = os.fstat(fd)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_uid != owner_uid
                    or opened.st_gid != owner_gid
                    or stat.S_IMODE(opened.st_mode) != mode
                    or _signature(entry) != _signature(opened)
                ):
                    raise AdminError("interrupted acknowledgment file is unsafe")
                try:
                    destination = os.stat(
                        name, dir_fd=directory_fd, follow_symlinks=False
                    )
                except FileNotFoundError:
                    destination = None
                same_destination = destination is not None and (
                    destination.st_dev,
                    destination.st_ino,
                ) == (opened.st_dev, opened.st_ino)
                if opened.st_nlink != (2 if same_destination else 1):
                    raise AdminError("interrupted acknowledgment link count is unsafe")
            finally:
                os.close(fd)
            current = os.stat(temporary, dir_fd=directory_fd, follow_symlinks=False)
            if _signature(current) != _signature(entry):
                raise AdminError("interrupted acknowledgment changed during cleanup")
            os.unlink(temporary, dir_fd=directory_fd)
            removed = True
        if removed:
            os.fsync(directory_fd)
    except OSError as exc:
        raise AdminError(
            "interrupted acknowledgment temp could not be recovered"
        ) from exc


def provision_pin(
    path: Path,
    *,
    release_sha: str,
    previous_sha: str,
    installer_sha256: str,
    owner_uid: int = 0,
    owner_gid: int = 0,
    parent_mode: int = 0o750,
) -> bytes:
    """Create a deployment pin once; never replace an existing file or link."""
    if (
        re.fullmatch(r"[0-9a-f]{40}", release_sha) is None
        or re.fullmatch(r"[0-9a-f]{40}", previous_sha) is None
        or release_sha == previous_sha
        or re.fullmatch(r"[0-9a-f]{64}", installer_sha256) is None
    ):
        raise AdminError("deployment pin values are invalid")
    data = {
        "release_sha": release_sha,
        "previous_sha": previous_sha,
        "installer_sha256": installer_sha256,
    }
    raw = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )
    directory_fd = _open_private_directory(
        path.parent, owner_uid=owner_uid, exact_mode=parent_mode
    )
    try:
        _write_new_at(
            directory_fd,
            path.name,
            raw,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            mode=0o600,
        )
        _verify_directory_binding(
            directory_fd,
            path.parent,
            owner_uid=owner_uid,
            exact_mode=parent_mode,
        )
    finally:
        os.close(directory_fd)
    return raw


def rotate_pin(
    path: Path,
    *,
    history_dir: Path,
    expected_current_pin_sha256: str,
    expected_current_release_sha: str,
    expected_previous_sha: str,
    release_sha: str,
    previous_sha: str,
    installer_sha256: str,
    approved_by: str,
    approval_reference: str,
    owner_uid: int = 0,
    owner_gid: int = 0,
    parent_mode: int = 0o750,
) -> tuple[str, str]:
    """Hash-bind a pin rotation after preserving the exact old pin bytes."""
    for value in (
        expected_current_pin_sha256,
        installer_sha256,
    ):
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise AdminError("deployment pin hash is invalid")
    for value in (
        expected_current_release_sha,
        expected_previous_sha,
        release_sha,
        previous_sha,
    ):
        if re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise AdminError("deployment pin release identity is invalid")
    if (
        history_dir.parent != path.parent
        or history_dir.name in {"", ".", ".."}
        or "/" in history_dir.name
        or
        release_sha == previous_sha
        or release_sha == expected_current_release_sha
        or expected_current_release_sha == expected_previous_sha
        or previous_sha != expected_previous_sha
        or not isinstance(approved_by, str)
        or not approved_by.strip()
        or len(approved_by) > 128
        or not isinstance(approval_reference, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", approval_reference)
        is None
    ):
        raise AdminError("deployment pin rotation request is invalid")
    new_raw = (
        json.dumps(
            {
                "release_sha": release_sha,
                "previous_sha": previous_sha,
                "installer_sha256": installer_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")
    new_hash = hashlib.sha256(new_raw).hexdigest()
    parent_fd = _open_private_directory(
        path.parent, owner_uid=owner_uid, exact_mode=parent_mode
    )
    history_fd = -1
    try:
        old_raw, old_info = _read_at(
            parent_fd, path.name, owner_uid=owner_uid, mode=0o600
        )
        old_hash = hashlib.sha256(old_raw).hexdigest()
        old_pin = _json_object(old_raw)

        def verify_or_write_rotation_record() -> None:
            rotation_name = f"deployment-pin-rotation-{expected_current_pin_sha256}-{new_hash}.json"
            rotation_expected = {
                "schema_version": 1,
                "previous_pin_sha256": expected_current_pin_sha256,
                "current_pin_sha256": new_hash,
                "previous_release_sha": expected_current_release_sha,
                "current_release_sha": release_sha,
                "approved_by": approved_by.strip(),
                "approval_reference": approval_reference,
            }
            try:
                os.stat(rotation_name, dir_fd=history_fd, follow_symlinks=False)
            except FileNotFoundError:
                record = {
                    **rotation_expected,
                    "approved_at_utc": datetime.now(UTC).isoformat(),
                }
                _write_new_at(
                    history_fd,
                    rotation_name,
                    (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii"),
                    owner_uid=owner_uid,
                    owner_gid=owner_gid,
                    mode=0o400,
                )
                return
            record_raw, _ = _read_at(
                history_fd, rotation_name, owner_uid=owner_uid, mode=0o400
            )
            record = _json_object(record_raw)
            if (
                set(record) != set(rotation_expected) | {"approved_at_utc"}
                or any(record.get(key) != value for key, value in rotation_expected.items())
            ):
                raise AdminError("pin rotation approval record is contradictory")
            _parse_utc(record.get("approved_at_utc"))

        if old_hash == new_hash and old_pin == {
            "release_sha": release_sha,
            "previous_sha": previous_sha,
            "installer_sha256": installer_sha256,
        }:
            history_fd = _open_private_directory(
                history_dir, owner_uid=owner_uid, exact_mode=0o700
            )
            archive_name = f"deployment-pin-{expected_current_pin_sha256}.json"
            archived_raw, _ = _read_at(
                history_fd, archive_name, owner_uid=owner_uid, mode=0o400
            )
            if hashlib.sha256(archived_raw).hexdigest() != expected_current_pin_sha256:
                raise AdminError("pin rotation archive does not match its expected hash")
            verify_or_write_rotation_record()
            return expected_current_pin_sha256, new_hash
        if (
            old_hash != expected_current_pin_sha256
            or set(old_pin) != {"release_sha", "previous_sha", "installer_sha256"}
            or old_pin.get("release_sha") != expected_current_release_sha
            or old_pin.get("previous_sha") != expected_previous_sha
            or old_pin.get("installer_sha256") != installer_sha256
        ):
            raise AdminError("current deployment pin differs from approved rotation")

        try:
            os.mkdir(history_dir.name, 0o700, dir_fd=parent_fd)
            if os.geteuid() == 0:
                os.chown(history_dir.name, owner_uid, owner_gid, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileExistsError:
            pass
        history_fd = _open_private_directory(
            history_dir, owner_uid=owner_uid, exact_mode=0o700
        )

        archive_name = f"deployment-pin-{old_hash}.json"
        try:
            _write_new_at(
                history_fd,
                archive_name,
                old_raw,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
                mode=0o400,
            )
        except AdminError as exc:
            if "refusing to overwrite" not in str(exc):
                raise
            archived_raw, _ = _read_at(
                history_fd, archive_name, owner_uid=owner_uid, mode=0o400
            )
            if archived_raw != old_raw:
                raise AdminError("existing pin history differs from current pin") from exc
        verify_or_write_rotation_record()

        temporary_name = f".deployment-pin-rotation-{new_hash}.json"
        try:
            os.stat(temporary_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            staged_exists = False
        else:
            staged_exists = True
        if staged_exists:
            staged_raw, _ = _read_at(
                parent_fd, temporary_name, owner_uid=owner_uid, mode=0o600
            )
            if staged_raw != new_raw:
                raise AdminError("interrupted pin rotation file is contradictory")
        else:
            _write_new_at(
                parent_fd,
                temporary_name,
                new_raw,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
                mode=0o600,
            )
        current_raw, current_info = _read_at(
            parent_fd, path.name, owner_uid=owner_uid, mode=0o600
        )
        if (
            current_raw != old_raw
            or _signature(current_info) != _signature(old_info)
        ):
            raise AdminError("deployment pin changed before atomic rotation")
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
        rotated_raw, rotated_info = _read_at(
            parent_fd, path.name, owner_uid=owner_uid, mode=0o600
        )
        if rotated_raw != new_raw or rotated_info.st_nlink != 1:
            raise AdminError("rotated deployment pin failed read-back validation")
        _verify_directory_binding(
            parent_fd, path.parent, owner_uid=owner_uid, exact_mode=parent_mode
        )
        return old_hash, new_hash
    except OSError as exc:
        raise AdminError("deployment pin rotation stopped on a filesystem error") from exc
    finally:
        if history_fd >= 0:
            os.close(history_fd)
        os.close(parent_fd)


def load_reviewed_python_source(
    path: Path, *, expected_sha256: str, owner_uid: int = 0
) -> types.ModuleType:
    """Compile verified source bytes directly, never importing adjacent .pyc."""
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise AdminError("reviewed source hash is invalid")
    try:
        parent_info = path.parent.lstat()
    except OSError as exc:
        raise AdminError("reviewed source directory is unavailable") from exc
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != owner_uid
        or parent_info.st_mode & 0o022
    ):
        raise AdminError("reviewed source directory is not protected")
    directory_fd = _open_private_directory(
        path.parent,
        owner_uid=owner_uid,
        exact_mode=stat.S_IMODE(parent_info.st_mode),
    )
    try:
        raw, info = _read_at(
            directory_fd,
            path.name,
            owner_uid=owner_uid,
            mode=stat.S_IMODE(path.lstat().st_mode),
            maximum=MAX_SOURCE_BYTES,
        )
        parent_info = os.fstat(directory_fd)
        _verify_directory_binding(
            directory_fd,
            path.parent,
            owner_uid=owner_uid,
            exact_mode=stat.S_IMODE(parent_info.st_mode),
        )
    finally:
        os.close(directory_fd)
    if info.st_mode & 0o022:
        raise AdminError("reviewed source is writable")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise AdminError("reviewed source hash does not match")
    try:
        source = raw.decode("utf-8")
        code = compile(source, str(path), "exec", dont_inherit=True)
    except (UnicodeError, SyntaxError) as exc:
        raise AdminError("reviewed source could not be compiled") from exc
    module = types.ModuleType(f"_reviewed_{path.stem}")
    module.__file__ = str(path)
    exec(code, module.__dict__)
    return module


def _validate_task267_addendum_with_module(
    validator: types.ModuleType, *args: Any, **kwargs: Any
) -> Any:
    """Call the hash-reviewed addendum validator exposed by its module."""
    validate = getattr(validator, "_validate_task267_addendum", None)
    if not callable(validate):
        raise AdminError("reviewed recovery validator lacks its addendum check")
    return validate(*args, **kwargs)


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise AdminError("review record time is missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AdminError("review record time is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise AdminError("review record time must be UTC")
    return parsed


def _validate_incident(journal: Mapping[str, Any], audit_dir: Path) -> tuple[str, str]:
    release_sha = journal.get("approved_release_sha")
    previous_sha = journal.get("previous_release_sha")
    failure = journal.get("installer_failure")
    if (
        journal.get("phase") != "manual_recovery_required"
        or journal.get("interrupted_phase") != "installer_started"
        or journal.get("failure_class") != "InstallerExecutionFailure"
        or not isinstance(failure, dict)
        or set(failure) != {"category", "exit_code", "outcome"}
        or failure.get("category") != "installer_rejected"
        or failure.get("exit_code") != 1
        or failure.get("outcome") != "installer_rejected"
        or journal.get("fail_closed_verified") is not True
        or journal.get("fail_closed_error") is not None
        or not isinstance(release_sha, str)
        or re.fullmatch(r"[0-9a-f]{40}", release_sha) is None
        or not isinstance(previous_sha, str)
        or re.fullmatch(r"[0-9a-f]{40}", previous_sha) is None
        or release_sha == previous_sha
        or journal.get("manifest") != str(audit_dir / f"deployment-{release_sha}.json")
    ):
        raise AdminError("active journal is not the reviewed fail-closed incident")
    return release_sha, previous_sha


def _validate_manifest(
    journal: Mapping[str, Any],
    manifest: Mapping[str, Any],
    release_sha: str,
    previous_sha: str,
    audit_dir: Path,
) -> None:
    fields = (
        "database_backup",
        "database_backup_sha256",
        "shared_preflight_backup",
        "shared_preflight_sha256",
    )
    if (
        manifest.get("approved_release_sha") != release_sha
        or manifest.get("previous_release_sha") != previous_sha
        or any(manifest.get(field) != journal.get(field) for field in fields)
    ):
        raise AdminError("incident manifest does not match the active journal")
    backup = Path(str(journal.get("database_backup", "")))
    saved_preflight = Path(str(journal.get("shared_preflight_backup", "")))
    preflight_match = re.fullmatch(
        rf"armory-db-preflight\.{re.escape(previous_sha)}(?:\.([0-9a-f]{{64}}))?\.py",
        saved_preflight.name,
    )
    if (
        backup.parent != Path("/var/lib/wow-forever-armory-bot/backups")
        or not backup.name.startswith("guild-armory-")
        or backup.suffix != ".sqlite3"
        or saved_preflight.parent != audit_dir
        or preflight_match is None
        or (
            preflight_match.group(1) is not None
            and preflight_match.group(1) != journal.get("shared_preflight_sha256")
        )
    ):
        raise AdminError("incident artifact paths are invalid")
    for field in ("database_backup_sha256", "shared_preflight_sha256"):
        if (
            not isinstance(journal.get(field), str)
            or re.fullmatch(r"[0-9a-f]{64}", journal[field]) is None
        ):
            raise AdminError("incident journal contains an invalid artifact hash")


def _validate_review_record(
    record: Mapping[str, Any],
    *,
    journal_sha256: str,
    manifest_sha256: str,
    release_sha: str,
    previous_sha: str,
    journal: Mapping[str, Any],
    approved_by: str,
    approval_reference: str,
    allow_timestamp_signature_delta: bool = False,
) -> None:
    required = {
        "schema_version",
        "journal_sha256",
        "manifest_sha256",
        "approved_release_sha",
        "previous_release_sha",
        "checks",
        "observations",
        "completed_at_utc",
        "approved_by",
        "approval_reference",
        "approved_at_utc",
    }
    if (
        set(record) != required
        or type(record.get("schema_version")) is not int
        or record.get("schema_version") != 1
    ):
        raise AdminError("recovery review record schema is invalid")
    if (
        record.get("journal_sha256") != journal_sha256
        or record.get("manifest_sha256") != manifest_sha256
        or record.get("approved_release_sha") != release_sha
        or record.get("previous_release_sha") != previous_sha
        or record.get("approved_by") != approved_by
        or record.get("approval_reference") != approval_reference
    ):
        raise AdminError("recovery review record identity does not match")
    checks = record.get("checks")
    if (
        not isinstance(checks, dict)
        or set(checks) != RECOVERY_CHECKS
        or any(result != "PASS" for result in checks.values())
    ):
        raise AdminError("every required recovery check must be PASS")
    observations = record.get("observations")
    if not isinstance(observations, dict) or set(observations) != {
        "release_target",
        "installation_marker",
        "database_signature",
        "database_integrity",
        "backup_path",
        "backup_sha256",
        "backup_integrity",
        "saved_preflight_path",
        "saved_preflight_sha256",
        "systemd_states",
    }:
        raise AdminError("recovery review observations are incomplete")
    release_target = observations.get("release_target")
    if (
        not isinstance(release_target, str)
        or release_target
        not in {
            f"/opt/wow-forever-armory-bot/releases/{release_sha}",
            f"/opt/wow-forever-armory-bot/releases/{previous_sha}",
        }
        or observations.get("installation_marker") != Path(release_target).name
    ):
        raise AdminError("recovery review release observation is invalid")
    signature = observations.get("database_signature")
    if (
        not isinstance(signature, list)
        or len(signature) != 8
        or any(type(value) is not int or value < 0 for value in signature)
        or observations.get("database_integrity") != "ok"
        or observations.get("backup_integrity") != "ok"
    ):
        raise AdminError("database or backup review observations are invalid")
    if (
        not isinstance(observations.get("backup_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", observations["backup_sha256"]) is None
        or not isinstance(observations.get("saved_preflight_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", observations["saved_preflight_sha256"]) is None
    ):
        raise AdminError("backup or preflight review hash is invalid")
    observed_matches = (
        (
            allow_timestamp_signature_delta
            or observations.get("database_signature") == journal.get("database_signature")
        )
        and observations.get("backup_path") == journal.get("database_backup")
        and observations.get("backup_sha256") == journal.get("database_backup_sha256")
        and observations.get("saved_preflight_path")
        == journal.get("shared_preflight_backup")
        and observations.get("saved_preflight_sha256")
        == journal.get("shared_preflight_sha256")
    )
    if not observed_matches:
        raise AdminError("recovery observations do not match the journal")
    states = observations.get("systemd_states")
    if not isinstance(states, dict) or set(states) != {
        *UNIT_REQUIREMENTS,
        BACKUP_SERVICE,
    }:
        raise AdminError("recovery review record has no systemd results")
    if any(
        states.get(unit) != list(expected)
        for unit, expected in UNIT_REQUIREMENTS.items()
    ):
        raise AdminError("bot and backup timer must be inactive and disabled")
    backup_state = states.get(BACKUP_SERVICE)
    if (
        not isinstance(backup_state, list)
        or len(backup_state) != 2
        or backup_state[0] != "inactive"
        or backup_state[1] not in {"static", "disabled"}
    ):
        raise AdminError("backup service must be inactive")
    _parse_utc(record.get("approved_at_utc"))
    _parse_utc(record.get("completed_at_utc"))


def _systemd_states() -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for unit in (*UNIT_REQUIREMENTS, BACKUP_SERVICE):
        values: list[str] = []
        for action in ("is-active", "is-enabled"):
            try:
                completed = subprocess.run(
                    ["/usr/bin/systemctl", action, unit],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="ascii",
                    errors="strict",
                    env={"PATH": "/usr/bin:/bin", "LANG": "C"},
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise AdminError("systemd state could not be queried") from exc
            value = completed.stdout.strip()
            if not value or "\n" in value:
                raise AdminError("systemd returned an invalid state")
            values.append(value)
        result[unit] = values
    return result


def _hash_regular_file(
    path: Path,
    *,
    owner_uid: int,
    owner_gid: int,
    mode: int,
    parent_mode: int,
    locked_directory_fd: int | None = None,
) -> str:
    if not hasattr(os, "O_NOATIME"):
        raise AdminError("filesystem cannot guarantee atime-safe hashing")
    if locked_directory_fd is None:
        directory_fd = _open_private_directory(
            path.parent, owner_uid=owner_uid, exact_mode=parent_mode
        )
    else:
        # Reuse the caller's exclusive directory lock. A second open()+flock()
        # of the same directory conflicts with Linux flock semantics even in
        # the same process. Validate that the supplied fd is the exact parent,
        # reaffirm its lock non-blockingly, and duplicate it for local cleanup.
        _verify_directory_binding(
            locked_directory_fd,
            path.parent,
            owner_uid=owner_uid,
            exact_mode=parent_mode,
        )
        try:
            fcntl.flock(locked_directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            directory_fd = os.dup(locked_directory_fd)
        except OSError as exc:
            raise AdminError(
                "another administrative operation holds the directory"
            ) from exc
    try:
        try:
            entry_before = os.stat(
                path.name, dir_fd=directory_fd, follow_symlinks=False
            )
            fd = os.open(
                path.name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOATIME", 0),
                dir_fd=directory_fd,
            )
        except OSError as exc:
            raise AdminError("recovery evidence file is unavailable") from exc
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != owner_uid
                or before.st_gid != owner_gid
                or stat.S_IMODE(before.st_mode) != mode
                or before.st_nlink != 1
                or _signature(before) != _signature(entry_before)
            ):
                raise AdminError("recovery evidence file metadata is unsafe")
            digest = hashlib.sha256()
            while True:
                block = os.read(fd, 1_048_576)
                if not block:
                    break
                digest.update(block)
            after = os.fstat(fd)
            entry_after = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            if _signature(before) != _signature(after) or _signature(
                after
            ) != _signature(entry_after):
                raise AdminError("recovery evidence file changed while hashing")
            _verify_directory_binding(
                directory_fd,
                path.parent,
                owner_uid=owner_uid,
                exact_mode=parent_mode,
            )
            return digest.hexdigest()
        except OSError as exc:
            raise AdminError(
                "recovery evidence file could not be hashed safely"
            ) from exc
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)


def _service_has(
    mode: int,
    owner_uid: int,
    owner_gid: int,
    service_uid: int,
    service_gid: int,
    bit: int,
) -> bool:
    if owner_uid == service_uid:
        return bool(mode & (bit << 6))
    if owner_gid == service_gid:
        return bool(mode & (bit << 3))
    return bool(mode & bit)


def _trusted_directory_chain(path: Path, *, owner_uid: int) -> bool:
    try:
        for directory in reversed((path, *path.parents)):
            info = directory.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != owner_uid
                or info.st_mode & 0o022
            ):
                return False
        return True
    except OSError:
        return False


def _trusted_system_python(
    path: Path, *, owner_uid: int, service_uid: int, service_gid: int
) -> Path:
    try:
        resolved = path.resolve(strict=True)
        info = resolved.lstat()
    except OSError as exc:
        raise AdminError("trusted system Python is unavailable") from exc
    if (
        not path.is_absolute()
        or not _trusted_directory_chain(resolved.parent, owner_uid=owner_uid)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != owner_uid
        or info.st_mode & 0o022
        or not _service_has(
            info.st_mode, info.st_uid, info.st_gid, service_uid, service_gid, 0o4
        )
        or not _service_has(
            info.st_mode, info.st_uid, info.st_gid, service_uid, service_gid, 0o1
        )
    ):
        raise AdminError("trusted system Python permissions are unsafe")
    return resolved


def _verify_release_tree(
    path: Path,
    *,
    service_uid: int,
    service_gid: int,
    owner_uid: int = 0,
    system_python: Path = SYSTEM_PYTHON,
) -> None:
    try:
        root_info = path.lstat()
    except OSError as exc:
        raise AdminError("recovery release directory is unavailable") from exc
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != owner_uid
        or root_info.st_mode & 0o022
    ):
        raise AdminError("recovery release directory metadata is unsafe")

    root_identity = (root_info.st_dev, root_info.st_ino)
    snapshots: dict[Path, tuple[int, ...]] = {path: _signature(root_info)}
    symlink_snapshots: dict[Path, tuple[tuple[int, ...], str, Path]] = {}
    resolved_system_python: Path | None = None

    def walk_error(_error: OSError) -> None:
        raise AdminError("recovery release tree could not be fully inspected")

    try:
        for _directory, directories, files, dir_fd in os.fwalk(
            path, follow_symlinks=False, onerror=walk_error
        ):
            directory_info = os.fstat(dir_fd)
            directory_path = Path(_directory)
            if snapshots.get(directory_path) != _signature(directory_info):
                raise AdminError("recovery release directory changed during inspection")
            if (
                not stat.S_ISDIR(directory_info.st_mode)
                or directory_info.st_uid != owner_uid
                or directory_info.st_mode & 0o022
                or not _service_has(
                    directory_info.st_mode,
                    directory_info.st_uid,
                    directory_info.st_gid,
                    service_uid,
                    service_gid,
                    0o1,
                )
            ):
                raise AdminError("recovery release directory permissions are unsafe")
            for name in (*directories, *files):
                entry = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                if stat.S_ISLNK(entry.st_mode):
                    item = directory_path / name
                    relative_name = item.relative_to(path).as_posix()
                    expected_target = VENV_SYMLINK_TARGETS.get(relative_name)
                    if relative_name == ".venv/bin/python3.14":
                        expected_target = str(system_python)
                    try:
                        target_text = os.readlink(name, dir_fd=dir_fd)
                    except OSError as exc:
                        raise AdminError(
                            "recovery release symbolic link could not be read"
                        ) from exc
                    if expected_target is None or target_text != expected_target:
                        raise AdminError(
                            "recovery release tree contains an unapproved symbolic link"
                        )
                    try:
                        resolved_target = item.resolve(strict=True)
                    except (OSError, RuntimeError, ValueError) as exc:
                        raise AdminError(
                            "recovery release symbolic link target is unavailable"
                        ) from exc
                    if relative_name == ".venv/lib64":
                        expected_resolved = path / ".venv/lib"
                        target_info = resolved_target.stat()
                        if (
                            resolved_target != expected_resolved
                            or not stat.S_ISDIR(target_info.st_mode)
                            or target_info.st_uid != owner_uid
                            or target_info.st_mode & 0o022
                            or not _service_has(
                                target_info.st_mode,
                                target_info.st_uid,
                                target_info.st_gid,
                                service_uid,
                                service_gid,
                                0o1,
                            )
                        ):
                            raise AdminError(
                                "venv lib64 link does not target its verified "
                                "lib directory"
                            )
                    else:
                        if resolved_system_python is None:
                            resolved_system_python = _trusted_system_python(
                                system_python,
                                owner_uid=owner_uid,
                                service_uid=service_uid,
                                service_gid=service_gid,
                            )
                        if resolved_target != resolved_system_python:
                            raise AdminError(
                                "venv interpreter link does not target trusted "
                                "system Python"
                            )
                        target_info = resolved_target.stat()
                        if (
                            not stat.S_ISREG(target_info.st_mode)
                            or target_info.st_uid != owner_uid
                            or target_info.st_mode & 0o022
                            or not _service_has(
                                target_info.st_mode,
                                target_info.st_uid,
                                target_info.st_gid,
                                service_uid,
                                service_gid,
                                0o4,
                            )
                            or not _service_has(
                                target_info.st_mode,
                                target_info.st_uid,
                                target_info.st_gid,
                                service_uid,
                                service_gid,
                                0o1,
                            )
                        ):
                            raise AdminError(
                                "venv system interpreter permissions are unsafe"
                            )
                    symlink_snapshots[item] = (
                        _signature(entry),
                        target_text,
                        resolved_target,
                    )
                    continue
                if name in directories:
                    if (
                        not stat.S_ISDIR(entry.st_mode)
                        or entry.st_uid != owner_uid
                        or entry.st_mode & 0o022
                        or not _service_has(
                            entry.st_mode,
                            entry.st_uid,
                            entry.st_gid,
                            service_uid,
                            service_gid,
                            0o1,
                        )
                    ):
                        raise AdminError(
                            "recovery release directory permissions are unsafe"
                        )
                    snapshots[Path(_directory) / name] = _signature(entry)
                    continue
                if (
                    not stat.S_ISREG(entry.st_mode)
                    or entry.st_uid != owner_uid
                    or entry.st_nlink != 1
                    or entry.st_mode & 0o022
                    or not _service_has(
                        entry.st_mode,
                        entry.st_uid,
                        entry.st_gid,
                        service_uid,
                        service_gid,
                        0o4,
                    )
                    or entry.st_mode & 0o111
                    and not _service_has(
                        entry.st_mode,
                        entry.st_uid,
                        entry.st_gid,
                        service_uid,
                        service_gid,
                        0o1,
                    )
                ):
                    raise AdminError("recovery release file permissions are unsafe")
                file_fd = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=dir_fd,
                )
                try:
                    opened = os.fstat(file_fd)
                    current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or _signature(opened) != _signature(entry)
                        or _signature(current) != _signature(opened)
                    ):
                        raise AdminError(
                            "recovery release file changed during inspection"
                        )
                finally:
                    os.close(file_fd)
                snapshots[Path(_directory) / name] = _signature(entry)
        for item, expected in snapshots.items():
            current = item.lstat()
            if stat.S_ISLNK(current.st_mode) or _signature(current) != expected:
                raise AdminError("recovery release tree changed during inspection")
        for item, (expected, target_text, resolved_target) in symlink_snapshots.items():
            current = item.lstat()
            if (
                not stat.S_ISLNK(current.st_mode)
                or _signature(current) != expected
                or os.readlink(item) != target_text
                or item.resolve(strict=True) != resolved_target
            ):
                raise AdminError(
                    "recovery release symbolic link changed during inspection"
                )
        final_info = path.lstat()
        if (final_info.st_dev, final_info.st_ino) != root_identity:
            raise AdminError("recovery release directory changed during inspection")
    except AdminError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise AdminError("recovery release tree changed during inspection") from exc
    interpreter = path / ".venv/bin/python"
    try:
        interpreter_info = interpreter.stat()
    except OSError as exc:
        raise AdminError("recovery release interpreter is unavailable") from exc
    if (
        not stat.S_ISREG(interpreter_info.st_mode)
        or interpreter_info.st_uid != owner_uid
        or interpreter_info.st_mode & 0o022
        or not _service_has(
            interpreter_info.st_mode,
            interpreter_info.st_uid,
            interpreter_info.st_gid,
            service_uid,
            service_gid,
            0o4,
        )
        or not _service_has(
            interpreter_info.st_mode,
            interpreter_info.st_uid,
            interpreter_info.st_gid,
            service_uid,
            service_gid,
            0o1,
        )
    ):
        raise AdminError("recovery release interpreter permissions are unsafe")


def _verify_recovery_evidence(
    journal: Mapping[str, Any],
    release_sha: str,
    previous_sha: str,
    audit_dir: Path,
    review: Mapping[str, Any],
    *,
    base: Path = Path("/opt/wow-forever-armory-bot"),
    database: Path = Path("/var/lib/wow-forever-armory-bot/guild-armory.sqlite3"),
    service_uid: int | None = None,
    service_gid: int | None = None,
    owner_uid: int = 0,
    locked_directory_fd: int | None = None,
) -> None:
    current = base / "current"
    try:
        current_info = current.lstat()
        link_text = os.readlink(current)
    except OSError as exc:
        raise AdminError("current release pointer is unavailable") from exc
    match = re.fullmatch(
        r"(?:releases/|/opt/wow-forever-armory-bot/releases/)([0-9a-f]{40})",
        link_text,
    )
    if (
        not stat.S_ISLNK(current_info.st_mode)
        or current_info.st_uid != owner_uid
        or match is None
    ):
        raise AdminError("current release pointer is not canonical")
    target_sha = match.group(1)
    if target_sha not in {release_sha, previous_sha}:
        raise AdminError("current release pointer is not pinned by the incident")
    target = base / "releases" / target_sha
    try:
        if current.resolve(strict=True) != target:
            raise AdminError("current release pointer resolves unexpectedly")
        current_after = current.lstat()
        if (
            _signature(current_info) != _signature(current_after)
            or os.readlink(current) != link_text
            or current.resolve(strict=True) != target
        ):
            raise AdminError("current release pointer changed during verification")
    except OSError as exc:
        raise AdminError("current release pointer target is unavailable") from exc
    observations = review["observations"]
    if observations["release_target"] != str(target):
        raise AdminError("review record release target is stale")

    if service_uid is None or service_gid is None:
        try:
            service_uid = pwd.getpwnam("wow-armory").pw_uid
            service_gid = grp.getgrnam("wow-armory").gr_gid
        except KeyError as exc:
            raise AdminError("Armory service account is unavailable") from exc
    for sha in {release_sha, previous_sha}:
        _verify_release_tree(
            base / "releases" / sha,
            service_uid=service_uid,
            service_gid=service_gid,
            owner_uid=owner_uid,
        )
    release_dir_fd = _open_private_directory(
        target,
        owner_uid=owner_uid,
        exact_mode=stat.S_IMODE(target.stat().st_mode),
    )
    try:
        marker, _ = _read_at(
            release_dir_fd,
            ".installation-complete",
            owner_uid=owner_uid,
            mode=0o444,
            maximum=128,
        )
    finally:
        os.close(release_dir_fd)
    try:
        marker_text = marker.decode("ascii").strip()
    except UnicodeError as exc:
        raise AdminError("installation marker is not ASCII") from exc
    if marker_text != target_sha:
        raise AdminError("installation marker does not match current release")
    if observations["installation_marker"] != target_sha:
        raise AdminError("review record installation marker is stale")

    try:
        db_info = database.lstat()
    except OSError as exc:
        raise AdminError("production database metadata is unavailable") from exc
    db_signature = [
        db_info.st_dev,
        db_info.st_ino,
        db_info.st_size,
        db_info.st_mtime_ns,
        db_info.st_ctime_ns,
        db_info.st_uid,
        db_info.st_gid,
        db_info.st_mode,
    ]
    if (
        not stat.S_ISREG(db_info.st_mode)
        or db_info.st_uid != service_uid
        or db_info.st_gid != service_gid
        or stat.S_IMODE(db_info.st_mode) != 0o600
        or db_signature != journal.get("database_signature")
        or observations["database_signature"] != db_signature
    ):
        raise AdminError("production database signature differs from the journal")

    backup = Path(journal["database_backup"])
    backup_hash = _hash_regular_file(
        backup,
        owner_uid=service_uid,
        owner_gid=service_gid,
        mode=0o600,
        parent_mode=0o700,
    )
    if (
        backup_hash != journal["database_backup_sha256"]
        or observations["backup_sha256"] != backup_hash
    ):
        raise AdminError("production backup hash differs from the journal")
    preflight = Path(journal["shared_preflight_backup"])
    preflight_hash = _hash_regular_file(
        preflight,
        owner_uid=owner_uid,
        owner_gid=0 if owner_uid == 0 else service_gid,
        mode=0o700,
        parent_mode=0o700,
        locked_directory_fd=locked_directory_fd,
    )
    if (
        preflight_hash != journal["shared_preflight_sha256"]
        or observations["saved_preflight_sha256"] != preflight_hash
    ):
        raise AdminError("saved preflight hash differs from the journal")
    try:
        current_info_after = current.lstat()
        db_info_after = database.lstat()
    except OSError as exc:
        raise AdminError("recovery evidence disappeared during verification") from exc
    if (
        _signature(current_info_after) != _signature(current_info)
        or os.readlink(current) != link_text
        or current.resolve(strict=True) != target
        or _signature(db_info_after) != _signature(db_info)
    ):
        raise AdminError("release pointer or database changed during verification")


_DEFAULT_RECOVERY_EVIDENCE_VERIFIER = _verify_recovery_evidence


def _read_task267_report(expected_uid: int) -> tuple[bytes, str]:
    """Read the single pinned report without following links or changing atime."""
    if not hasattr(os, "O_NOATIME"):
        raise AdminError("filesystem cannot guarantee atime-safe report reads")
    parent = TASK267_REPORT.parent
    try:
        if parent.resolve(strict=True) != parent:
            raise AdminError("verification report directory is not canonical")
        parent_info = parent.lstat()
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != expected_uid
            or parent_info.st_mode & 0o022
        ):
            raise AdminError("verification report directory metadata is unsafe")
        directory_fd = os.open(
            parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOATIME", 0),
        )
        try:
            opened_dir = os.fstat(directory_fd)
            if (opened_dir.st_dev, opened_dir.st_ino) != (
                parent_info.st_dev,
                parent_info.st_ino,
            ):
                raise AdminError("verification report directory changed")
            before = os.stat(TASK267_REPORT.name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != expected_uid
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_nlink != 1
                or before.st_size > 2_000_000
            ):
                raise AdminError("verification report metadata is unsafe")
            fd = os.open(
                TASK267_REPORT.name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NOATIME", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_fd,
            )
            try:
                opened = os.fstat(fd)
                if _signature(opened) != _signature(before):
                    raise AdminError("verification report changed while opening")
                chunks = []
                total = 0
                while True:
                    block = os.read(fd, 65536)
                    if not block:
                        break
                    chunks.append(block)
                    total += len(block)
                    if total > 2_000_000:
                        raise AdminError("verification report exceeds its size limit")
                raw = b"".join(chunks)
                after = os.fstat(fd)
                current = os.stat(TASK267_REPORT.name, dir_fd=directory_fd, follow_symlinks=False)
                current_parent = parent.lstat()
                if (
                    _signature(opened) != _signature(after)
                    or _signature(after) != _signature(current)
                    or (opened_dir.st_dev, opened_dir.st_ino)
                    != (current_parent.st_dev, current_parent.st_ino)
                    or len(raw) != after.st_size
                ):
                    raise AdminError("verification report changed while reading")
                return raw, hashlib.sha256(raw).hexdigest()
            finally:
                os.close(fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise AdminError("verification report cannot be read safely") from exc


def _validate_task267_report(
    report: Mapping[str, Any],
    journal: Mapping[str, Any],
    manifest: Mapping[str, Any],
    addendum: Mapping[str, Any],
    *,
    journal_sha256: str,
    manifest_sha256: str,
) -> None:
    """Check the pinned verifier's semantic claims before applying addendum."""
    db = report.get("database")
    refs = report.get("incident_references")
    if not isinstance(db, Mapping) or not isinstance(refs, Mapping):
        raise AdminError("fresh verifier report has incomplete evidence")
    if (
        type(report.get("schema_version")) is not int
        or report.get("schema_version") != 1
        or report.get("status") != "PASS"
        or report.get("hostname") != "nololinux-server"
        or report.get("warnings")
        != ["Incident journal signature differs from the current database."]
        or journal_sha256 != TASK267_JOURNAL_SHA256
        or manifest_sha256 != TASK267_MANIFEST_SHA256
        or refs.get("phase") != "manual_recovery_required"
        or refs.get("journal_sha256") != TASK267_JOURNAL_SHA256
        or refs.get("manifest_sha256") != TASK267_MANIFEST_SHA256
        or refs.get("backup_sha256") != TASK267_INCIDENT_BACKUP_SHA256
        or refs.get("latest_backup_sha256") != TASK267_LATEST_BACKUP_SHA256
        or db.get("integrity_check") != "ok"
        or db.get("schema") != "PASS"
        or db.get("source_sha256") != db.get("snapshot_sha256")
        or db.get("source_sha256") != addendum.get("database_sha256")
        or db.get("incident_signature_match") is not False
        or db.get("process_open_count_before_and_after") != 0
        or db.get("counts") != {"guilds": 1, "members": 4, "characters": 4}
        or db.get("wal") != {
            "present": False,
            "snapshot_sha256": None,
            "source_sha256": None,
        }
        or db.get("shm") != {"present": False, "sha256": None}
        or report.get("systemd_before") != {
            "wow-forever-armory-bot.service": {"active": "inactive", "enabled": "disabled"},
            "wow-forever-armory-backup.timer": {"active": "inactive", "enabled": "disabled"},
            "wow-forever-armory-backup.service": {"active": "inactive"},
        }
        or report.get("systemd_after") != report.get("systemd_before")
        or not isinstance(db.get("backup_comparison"), Mapping)
        or not isinstance(db["backup_comparison"].get("incident"), Mapping)
        or db["backup_comparison"]["incident"].get("status") != "MATCH"
        or not isinstance(db["backup_comparison"].get("latest_2026_11_15"), Mapping)
        or db["backup_comparison"]["latest_2026_11_15"].get("status") != "MATCH"
    ):
        raise AdminError("fresh verifier report failed addendum requirements")
    signature = db.get("source_signature")
    if not isinstance(signature, list) or len(signature) != 9 or signature[3] != 1:
        raise AdminError("fresh verifier database signature is invalid")
    converted = [signature[0], signature[1], signature[2], signature[4], signature[5], signature[7], signature[8], signature[6]]
    if converted != addendum.get("verified_database_signature"):
        raise AdminError("report signature differs from the approved addendum")
    historical = journal.get("database_signature")
    if (
        addendum.get("historical_database_signature") != historical
        or not isinstance(historical, list)
        or len(historical) != 8
        or any(historical[index] != converted[index] for index in (0, 1, 2, 5, 6, 7))
        or historical[3:5] == converted[3:5]
    ):
        raise AdminError("database signature drift is not limited to mtime/ctime")


def _database_open_handle_count(database: Path) -> int:
    """Passively scan procfs; inability to complete the scan is a hard stop."""
    if not hasattr(os, "O_NOATIME"):
        raise AdminError("process scan cannot guarantee atime-safe access")
    try:
        target = database.stat()
        proc_fd = os.open(
            "/proc",
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOATIME", 0),
        )
        try:
            pids = os.listdir(proc_fd)
        finally:
            os.close(proc_fd)
    except OSError as exc:
        raise AdminError("process handle scan is unavailable") from exc
    count = 0
    for pid in pids:
        if not pid.isdecimal():
            continue
        fd_directory = Path("/proc") / pid / "fd"
        try:
            fd_dir = os.open(
                fd_directory,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOATIME", 0),
            )
            try:
                entries = os.listdir(fd_dir)
                identities = []
                for descriptor in entries:
                    try:
                        info = os.stat(descriptor, dir_fd=fd_dir, follow_symlinks=True)
                    except FileNotFoundError:
                        continue
                    identities.append((info.st_dev, info.st_ino))
            finally:
                os.close(fd_dir)
        except FileNotFoundError:
            continue  # Process exited during the passive scan.
        except OSError as exc:
            raise AdminError("process handle scan is incomplete") from exc
        count += sum(identity == (target.st_dev, target.st_ino) for identity in identities)
    return count


def _require_recovery_unit_states(
    states: Mapping[str, list[str] | tuple[str, str]],
) -> None:
    for unit, expected in UNIT_REQUIREMENTS.items():
        if states.get(unit) != list(expected):
            raise AdminError(
                "live systemd states do not permit recovery acknowledgment"
            )
    backup_state = states.get(BACKUP_SERVICE)
    if (
        not isinstance(backup_state, (tuple, list))
        or len(backup_state) != 2
        or backup_state[0] != "inactive"
        or backup_state[1] not in {"static", "disabled"}
    ):
        raise AdminError("live backup service state does not permit acknowledgment")


def _read_activation_attempts(
    directory_fd: int,
    journal_sha256: str,
    *,
    owner_uid: int,
) -> list[tuple[int, str, list[dict[str, Any]]]]:
    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for name in os.listdir(directory_fd):
        match = ACTIVATION_EVENT_RE.fullmatch(name)
        if match is None or match.group(1) != journal_sha256:
            continue
        raw, _ = _read_at(directory_fd, name, owner_uid=owner_uid, mode=0o600)
        event = _json_object(raw)
        grouped.setdefault(match.group(2), []).append((int(match.group(3)), event))
    attempts = []
    for activation_id, records in grouped.items():
        records.sort(key=lambda item: item[0])
        attempts.append(
            (records[0][1]["attempt_number"], activation_id, [r[1] for r in records])
        )
    return sorted(attempts, key=lambda item: item[0])


def _append_activation_event(
    directory_fd: int,
    *,
    journal_sha256: str,
    activation_id: str,
    attempt_number: int,
    sequence: int,
    event_type: str,
    deployment_id: str,
    release_sha: str,
    completion_ack_sha256: str,
    manifest_sha256: str,
    installation_result_sha256: str,
    deployment_pin_sha256: str,
    approved_by: str,
    approval_reference: str,
    intended_states: Mapping[str, list[str]],
    action: Mapping[str, Any] | None = None,
    observed_states: Mapping[str, list[str]] | None = None,
    failure_class: str | None = None,
    lifecycle_transition: Mapping[str, Any] | None = None,
    owner_uid: int,
    owner_gid: int,
) -> dict[str, Any]:
    if event_type not in {
        "approved",
        "action_planned",
        "action_verified",
        "success",
        "failure",
    }:
        raise AdminError("activation event type is invalid")
    if lifecycle_transition is not None and event_type != "success":
        raise AdminError("lifecycle transition is valid only on activation success")
    event = {
        "schema_version": 2 if lifecycle_transition is not None else 1,
        "journal_sha256": journal_sha256,
        "activation_id": activation_id,
        "attempt_number": attempt_number,
        "sequence": sequence,
        "event": event_type,
        "deployment_id": deployment_id,
        "approved_release_sha": release_sha,
        "completion_ack_sha256": completion_ack_sha256,
        "manifest_sha256": manifest_sha256,
        "installation_result_sha256": installation_result_sha256,
        "deployment_pin_sha256": deployment_pin_sha256,
        "approved_by": approved_by,
        "approval_reference": approval_reference,
        "intended_states": dict(intended_states),
        "action": dict(action) if action is not None else None,
        "observed_states": {
            key: list(value) for key, value in (observed_states or {}).items()
        },
        "failure_class": failure_class,
        "recorded_at_utc": datetime.now(UTC).isoformat(),
    }
    if lifecycle_transition is not None:
        event["lifecycle_transition"] = dict(lifecycle_transition)
    name = f"service-activation-{journal_sha256}-{activation_id}-{sequence:04d}.json"
    raw = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )
    _write_new_at(
        directory_fd,
        name,
        raw,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        mode=0o600,
    )
    return event


def _completion_process_present(proc_root: Path = Path("/proc")) -> bool:
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return True
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            argv = [
                part.decode(errors="replace")
                for part in (entry / "cmdline").read_bytes().split(b"\0")
                if part
            ]
        except FileNotFoundError:
            continue
        except OSError:
            return True
        if (
            len(argv) >= 3 and argv[1:3] == ["-m", "wow_forever_armory.discord_bot"]
        ) or any(Path(arg).name == "discord_bot.py" for arg in argv):
            return True
    return False


def _verify_completion_evidence(
    journal: Mapping[str, Any],
    report: Mapping[str, Any],
    manifest: Mapping[str, Any],
    audit_dir: Path,
    manifest_raw: bytes,
    *,
    owner_uid: int = 0,
    locked_directory_fd: int | None = None,
) -> None:
    """Recheck installation evidence and stopped-state conditions before closure."""
    release = journal.get("approved_release_sha")
    previous = journal.get("previous_release_sha")
    if (
        journal.get("phase") not in {"installation_verified", "complete"}
        or journal.get("installer_exit_code") != 0
        or not isinstance(release, str)
        or re.fullmatch(r"[0-9a-f]{40}", release) is None
        or not isinstance(previous, str)
        or re.fullmatch(r"[0-9a-f]{40}", previous) is None
        or release == previous
    ):
        raise AdminError("deployment journal is not eligible for completion")
    legacy_report_fields = {
        "schema_version",
        "deployment_id",
        "approved_release_sha",
        "previous_release_sha",
        "manifest",
        "manifest_sha256",
        "database_backup",
        "database_backup_sha256",
        "shared_preflight_backup",
        "shared_preflight_sha256",
        "installer_sha256",
        "deployment_pin_sha256",
        "database_signature",
        "intended_service_state",
        "intended_backup_timer_state",
        "observed_service_states",
        "verified_at_utc",
    }
    report_schema = report.get("schema_version")
    expected_report_fields = (
        legacy_report_fields | {"initial_unit_states"}
        if report_schema == 2
        else legacy_report_fields
    )
    if (
        set(report) != expected_report_fields
        or report_schema not in {1, 2}
        or report.get("deployment_id") != journal.get("deployment_id")
        or report.get("approved_release_sha") != release
        or report.get("previous_release_sha") != previous
        or report.get("manifest") != journal.get("manifest")
        or report.get("manifest_sha256") != hashlib.sha256(manifest_raw).hexdigest()
        or report.get("database_backup") != journal.get("database_backup")
        or report.get("database_backup_sha256") != journal.get("database_backup_sha256")
        or report.get("shared_preflight_backup")
        != journal.get("shared_preflight_backup")
        or report.get("shared_preflight_sha256")
        != journal.get("shared_preflight_sha256")
        or report.get("database_signature") != journal.get("database_signature")
        or journal.get("database_signature") != report.get("database_signature")
        or report.get("installer_sha256")
        != journal.get("deployment_pin_installer_sha256")
        or report.get("deployment_pin_sha256") != journal.get("deployment_pin_sha256")
        or (
            report_schema == 2
            and report.get("initial_unit_states")
            != journal.get("initial_unit_states")
        )
    ):
        raise AdminError("installation result contradicts deployment journal")
    if report_schema == 2:
        initial_states = report.get("initial_unit_states")
        if not isinstance(initial_states, Mapping) or set(initial_states) != {
            SERVICE_NAME,
            TIMER_NAME,
            BACKUP_SERVICE,
        }:
            raise AdminError("installation report initial unit states are incomplete")
        for unit in (SERVICE_NAME, TIMER_NAME):
            if initial_states[unit] not in (
                ["active", "enabled"],
                ["inactive", "disabled"],
            ):
                raise AdminError("installation report initial unit state is invalid")
        backup_initial = initial_states[BACKUP_SERVICE]
        if (
            not isinstance(backup_initial, list)
            or len(backup_initial) != 2
            or backup_initial[0] != "inactive"
            or backup_initial[1] not in {"static", "disabled"}
        ):
            raise AdminError("installation report initial backup state is invalid")
    if (
        manifest.get("approved_release_sha") != release
        or manifest.get("previous_release_sha") != previous
        or manifest.get("database_backup") != journal.get("database_backup")
        or manifest.get("database_backup_sha256")
        != journal.get("database_backup_sha256")
        or manifest.get("shared_preflight_backup")
        != journal.get("shared_preflight_backup")
        or manifest.get("shared_preflight_sha256")
        != journal.get("shared_preflight_sha256")
    ):
        raise AdminError("deployment manifest contradicts journal")
    if set(manifest) != {
        "approved_release_sha",
        "previous_release_sha",
        "database_backup",
        "database_backup_sha256",
        "shared_preflight_backup",
        "shared_preflight_sha256",
        "created_at_utc",
    }:
        raise AdminError("deployment manifest fields are invalid")
    _parse_utc(manifest.get("created_at_utc"))
    _parse_utc(report.get("verified_at_utc"))
    _validate_manifest(journal, manifest, release, previous, audit_dir)
    pin, pin_raw = read_secure_json(
        PIN_PATH, owner_uid=owner_uid, mode=0o600, parent_mode=0o750
    )
    if (
        set(pin) != PIN_KEYS
        or pin.get("release_sha") != release
        or pin.get("previous_sha") != previous
        or pin.get("installer_sha256") != report.get("installer_sha256")
        or hashlib.sha256(pin_raw).hexdigest() != report.get("deployment_pin_sha256")
        or _hash_regular_file(
            COMPLETION_INSTALLER,
            owner_uid=owner_uid,
            owner_gid=0,
            mode=0o755,
            parent_mode=0o755,
        )
        != pin.get("installer_sha256")
    ):
        raise AdminError("deployment pin or installed installer is inconsistent")

    current = COMPLETION_BASE / "current"
    try:
        current_info = current.lstat()
        link_text = os.readlink(current)
    except OSError as exc:
        raise AdminError("current release pointer is unavailable") from exc
    if (
        not stat.S_ISLNK(current_info.st_mode)
        or current_info.st_uid != owner_uid
        or link_text != f"releases/{release}"
        or current.resolve(strict=True) != COMPLETION_BASE / "releases" / release
    ):
        raise AdminError("installed release pointer is not the pinned relative link")
    target = COMPLETION_BASE / "releases" / release
    try:
        service_uid = pwd.getpwnam("wow-armory").pw_uid
        service_gid = grp.getgrnam("wow-armory").gr_gid
    except KeyError as exc:
        raise AdminError("Armory service account is unavailable") from exc
    _verify_release_tree(
        target,
        service_uid=service_uid,
        service_gid=service_gid,
        owner_uid=owner_uid,
    )
    release_fd = _open_private_directory(
        target, owner_uid=owner_uid, exact_mode=stat.S_IMODE(target.stat().st_mode)
    )
    try:
        marker_raw, _ = _read_at(
            release_fd,
            ".installation-complete",
            owner_uid=owner_uid,
            mode=0o444,
            maximum=128,
        )
    finally:
        os.close(release_fd)
    if marker_raw.decode("ascii").strip() != release:
        raise AdminError("installation marker does not match pinned release")

    db_info = COMPLETION_DATABASE.lstat()
    db_signature = [
        db_info.st_dev,
        db_info.st_ino,
        db_info.st_size,
        db_info.st_mtime_ns,
        db_info.st_ctime_ns,
        db_info.st_uid,
        db_info.st_gid,
        db_info.st_mode,
    ]
    if (
        not stat.S_ISREG(db_info.st_mode)
        or db_info.st_nlink != 1
        or db_info.st_uid != service_uid
        or db_info.st_gid != service_gid
        or stat.S_IMODE(db_info.st_mode) != 0o600
        or db_signature != report.get("database_signature")
    ):
        raise AdminError("production database changed after installation verification")
    backup = Path(str(journal.get("database_backup", "")))
    backup_hash = _hash_regular_file(
        backup,
        owner_uid=service_uid,
        owner_gid=service_gid,
        mode=0o600,
        parent_mode=0o700,
    )
    saved_preflight = Path(str(journal.get("shared_preflight_backup", "")))
    preflight_hash = _hash_regular_file(
        saved_preflight,
        owner_uid=owner_uid,
        owner_gid=owner_uid,
        mode=0o700,
        parent_mode=0o700,
        locked_directory_fd=locked_directory_fd,
    )
    if backup_hash != journal.get(
        "database_backup_sha256"
    ) or preflight_hash != journal.get("shared_preflight_sha256"):
        raise AdminError("backup or saved preflight hash changed")
    snapshot_module = load_reviewed_python_source(
        COMPLETION_SNAPSHOT_MODULE,
        expected_sha256=SNAPSHOT_MODULE_SHA256,
        owner_uid=owner_uid,
    )
    try:
        snapshot_module.verify_database_snapshot(
            COMPLETION_DATABASE,
            expected_owner=service_uid,
            expected_group=service_gid,
        )
        snapshot_module.verify_database_snapshot(
            backup,
            expected_owner=service_uid,
            expected_group=service_gid,
        )
    except Exception as exc:
        raise AdminError("database or backup integrity verification failed") from exc
    try:
        current_after = current.lstat()
        db_after = COMPLETION_DATABASE.lstat()
    except OSError as exc:
        raise AdminError("completion evidence changed during verification") from exc
    if (
        _signature(current_after) != _signature(current_info)
        or os.readlink(current) != link_text
        or current.resolve(strict=True) != target
        or _signature(db_after) != _signature(db_info)
    ):
        raise AdminError("release pointer or database changed during verification")
    _require_recovery_unit_states(_systemd_states())
    if _completion_process_present():
        raise AdminError("an Armory bot process remains outside systemd")


def _publish_completed_journal(
    directory_fd: int,
    active_raw: bytes,
    journal: Mapping[str, Any],
    *,
    owner_uid: int,
    owner_gid: int,
    approved_by: str,
    approval_reference: str,
) -> bytes:
    updated = dict(journal)
    if journal.get("phase") == "installation_verified":
        updated.update(
            phase="complete",
            finalization_source_journal_sha256=hashlib.sha256(active_raw).hexdigest(),
            completion_approved_by=approved_by,
            completion_approval_reference=approval_reference,
            completed_at_utc=datetime.now(UTC).isoformat(),
        )
    elif journal.get("phase") == "complete":
        if (
            journal.get("completion_approved_by") != approved_by
            or journal.get("completion_approval_reference") != approval_reference
        ):
            raise AdminError("retry approval differs from saved completion approval")
    else:
        raise AdminError("active journal is not ready for administrative completion")
    raw = (json.dumps(updated, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )
    temporary = ".deployment-state.json.tmp"
    try:
        existing, _ = _read_at(directory_fd, temporary, owner_uid=owner_uid, mode=0o600)
    except AdminError as exc:
        if "unavailable" not in str(exc):
            raise
        existing = None
    if existing is not None:
        candidate = _json_object(existing)
        if (
            candidate.get("phase") != "complete"
            or candidate.get("finalization_source_journal_sha256")
            != hashlib.sha256(active_raw).hexdigest()
            or candidate.get("completion_approved_by") != approved_by
            or candidate.get("completion_approval_reference") != approval_reference
        ):
            raise AdminError("interrupted journal temporary conflicts with approval")
        raw = existing
    else:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        try:
            os.fchown(fd, owner_uid, owner_gid) if os.geteuid() == 0 else None
            os.fchmod(fd, 0o600)
            view = memoryview(raw)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise AdminError("completion journal write was incomplete")
                view = view[count:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(directory_fd)
    current_raw, _ = _read_at(
        directory_fd, "deployment-state.json", owner_uid=owner_uid, mode=0o600
    )
    if current_raw != active_raw:
        raise AdminError("active journal changed before completion update")
    temporary_raw, temporary_info = _read_at(
        directory_fd, temporary, owner_uid=owner_uid, mode=0o600
    )
    if temporary_raw != raw or temporary_info.st_nlink != 1:
        raise AdminError("completion journal temporary failed verification")
    active_info = os.stat(
        "deployment-state.json", dir_fd=directory_fd, follow_symlinks=False
    )
    if active_info.st_nlink != 1 or not stat.S_ISREG(active_info.st_mode):
        raise AdminError("active journal metadata changed before replacement")
    os.replace(
        temporary,
        "deployment-state.json",
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
    )
    os.fsync(directory_fd)
    return raw


def finalize_deployment(
    audit_dir: Path,
    *,
    expected_journal_sha256: str,
    approved_by: str,
    approval_reference: str,
    owner_uid: int = 0,
    owner_gid: int = 0,
    state_reader: Callable[[], Mapping[str, list[str]]] = _systemd_states,
    evidence_verifier: Callable[
        [Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], Path, bytes], None
    ]
    | None = None,
    history_verifier: Callable[[Path, int, int], None] | None = None,
    checkpoint: Callable[[str], None] | None = None,
) -> tuple[str, str]:
    """Administratively finalize a verified installation; never starts services."""
    if re.fullmatch(r"[0-9a-f]{64}", expected_journal_sha256) is None:
        raise AdminError("expected journal hash is invalid")
    if not approved_by.strip() or len(approved_by) > 128:
        raise AdminError("administrator identity is invalid")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", approval_reference) is None:
        raise AdminError("completion approval reference is invalid")
    directory_fd = _open_private_directory(
        audit_dir, owner_uid=owner_uid, exact_mode=0o700
    )
    try:
        _verify_directory_binding(
            directory_fd, audit_dir, owner_uid=owner_uid, exact_mode=0o700
        )
        active_present = True
        try:
            active_info = os.stat(
                "deployment-state.json", dir_fd=directory_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            active_present = False
        if active_present:
            if active_info.st_nlink not in {1, 2}:
                raise AdminError("active journal has an unexpected link count")
            journal_raw, _ = _read_at(
                directory_fd,
                "deployment-state.json",
                owner_uid=owner_uid,
                mode=0o600,
                expected_nlink=active_info.st_nlink,
            )
        if not active_present:
            matching = []
            for candidate in os.listdir(directory_fd):
                if not ARCHIVE_NAME_RE.fullmatch(candidate):
                    continue
                candidate_raw, _ = _read_at(
                    directory_fd, candidate, owner_uid=owner_uid, mode=0o600
                )
                if (
                    _json_object(candidate_raw).get(
                        "finalization_source_journal_sha256"
                    )
                    == expected_journal_sha256
                ):
                    matching.append((candidate, candidate_raw))
            if len(matching) != 1:
                raise AdminError(
                    "interrupted completion archive is missing or ambiguous"
                )
            archive_name, journal_raw = matching[0]
        journal = _json_object(journal_raw)
        if active_present:
            source_sha = hashlib.sha256(journal_raw).hexdigest()
            if journal.get("phase") == "installation_verified":
                if source_sha != expected_journal_sha256:
                    raise AdminError("active journal hash differs from reviewed hash")
            elif (
                journal.get("phase") != "complete"
                or journal.get("finalization_source_journal_sha256")
                != expected_journal_sha256
            ):
                raise AdminError(
                    "active completion journal does not match reviewed source"
                )
        elif (
            journal.get("phase") != "complete"
            or journal.get("finalization_source_journal_sha256")
            != expected_journal_sha256
        ):
            raise AdminError(
                "archived completion journal does not match reviewed source"
            )
        if journal.get("completion_approved_by") not in (
            None,
            approved_by,
        ) or journal.get("completion_approval_reference") not in (
            None,
            approval_reference,
        ):
            raise AdminError("saved completion approval conflicts with this request")
        release = journal.get("approved_release_sha")
        manifest_name = f"deployment-{release}.json"
        manifest_raw, _ = _read_at(
            directory_fd, manifest_name, owner_uid=owner_uid, mode=0o600
        )
        manifest = _json_object(manifest_raw)
        report_name = journal.get("installation_result")
        report_sha = journal.get("installation_result_sha256")
        if not isinstance(report_name, str) or not isinstance(report_sha, str):
            raise AdminError("verified installation result is missing")
        report_raw, _ = _read_at(
            directory_fd, report_name, owner_uid=owner_uid, mode=0o600
        )
        if hashlib.sha256(report_raw).hexdigest() != report_sha:
            raise AdminError("installation result hash differs from journal")
        report = _json_object(report_raw)
        if report.get("manifest_sha256") != hashlib.sha256(manifest_raw).hexdigest():
            raise AdminError("installation result manifest hash is invalid")
        verifier = evidence_verifier or (
            lambda j, r, m, d, raw: _verify_completion_evidence(
                j, r, m, d, raw, owner_uid=owner_uid,
                locked_directory_fd=directory_fd,
            )
        )
        verifier(journal, report, manifest, audit_dir, manifest_raw)
        observed_states = state_reader()
        if any(
            observed_states.get(unit) != list(expected)
            for unit, expected in UNIT_REQUIREMENTS.items()
        ) or observed_states.get(BACKUP_SERVICE) not in (
            ["inactive", "static"],
            ["inactive", "disabled"],
        ):
            raise AdminError("services changed before deployment completion")

        if active_present and journal.get("phase") == "installation_verified":
            journal_raw = _publish_completed_journal(
                directory_fd,
                journal_raw,
                journal,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
                approved_by=approved_by,
                approval_reference=approval_reference,
            )
            journal = _json_object(journal_raw)
        journal_sha = hashlib.sha256(journal_raw).hexdigest()
        archive_name = f"deployment-state-{journal_sha}.json"
        ack_name = f"deployment-recovery-ack-{journal_sha}.json"
        ack = {
            "schema_version": 1,
            "journal_sha256": journal_sha,
            "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "approved_release_sha": journal["approved_release_sha"],
            "previous_release_sha": journal["previous_release_sha"],
            "decision": "deployment_completion_verified",
            "approved_by": approved_by,
            "approval_reference": approval_reference,
            "approved_at_utc": journal["completed_at_utc"],
        }
        ack_raw = (
            json.dumps(ack, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("ascii")
        if (
            not active_present
            and archive_name != f"deployment-state-{journal_sha}.json"
        ):
            raise AdminError("completion archive name does not match its contents")

        try:
            archive_raw, _ = _read_at(
                directory_fd,
                archive_name,
                owner_uid=owner_uid,
                mode=0o600,
                expected_nlink=2 if active_present else 1,
            )
            if archive_raw != journal_raw:
                raise AdminError("existing deployment archive conflicts")
        except AdminError as exc:
            if "unavailable" not in str(exc):
                raise
            if not active_present:
                raise AdminError("active journal and archive are both missing") from exc
            os.link(
                "deployment-state.json",
                archive_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            os.fsync(directory_fd)
            archive_raw, _ = _read_at(
                directory_fd,
                archive_name,
                owner_uid=owner_uid,
                mode=0o600,
                expected_nlink=2,
            )
            if archive_raw != journal_raw:
                raise AdminError(
                    "deployment archive failed byte verification"
                ) from None
        if active_present:
            if checkpoint:
                checkpoint("archive_verified")
            current_raw, _ = _read_at(
                directory_fd,
                "deployment-state.json",
                owner_uid=owner_uid,
                mode=0o600,
                expected_nlink=2,
            )
            if current_raw != journal_raw:
                raise AdminError("active journal changed before archival removal")
            try:
                os.stat(ack_name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise AdminError(
                    "active completion conflicts with existing acknowledgment"
                )
            os.unlink("deployment-state.json", dir_fd=directory_fd)
            os.fsync(directory_fd)
            active_present = False
            if checkpoint:
                checkpoint("active_removed")
        _require_recovery_unit_states(state_reader())
        _clean_ack_temporaries(
            directory_fd,
            ack_name,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            mode=0o600,
        )
        try:
            existing_ack, _ = _read_at(
                directory_fd, ack_name, owner_uid=owner_uid, mode=0o600
            )
        except AdminError as exc:
            if "unavailable" not in str(exc):
                raise
            existing_ack = None
        if existing_ack is not None:
            if existing_ack != ack_raw:
                raise AdminError("existing completion acknowledgment conflicts")
        else:
            if checkpoint:
                checkpoint("before_ack_publication")
            _write_new_at(
                directory_fd,
                ack_name,
                ack_raw,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
                mode=0o600,
            )
            if checkpoint:
                checkpoint("ack_published")
        _verify_directory_binding(
            directory_fd, audit_dir, owner_uid=owner_uid, exact_mode=0o700
        )
        if history_verifier:
            history_verifier(audit_dir, owner_uid, owner_gid)
        else:
            validator = load_reviewed_python_source(
                COMPLETION_AUDIT_VALIDATOR,
                expected_sha256=RECOVERY_AUDIT_SHA256,
                owner_uid=owner_uid,
            )
            validator.verify_audit_directory(
                audit_dir,
                owner=owner_uid,
                group=owner_gid,
                acquire_lock=False,
                preserved_historical_directories=("reviewed-v2.0.0", "archive"),
            )
        return journal_sha, hashlib.sha256(manifest_raw).hexdigest()
    except OSError as exc:
        raise AdminError(
            "deployment finalization stopped on a filesystem error"
        ) from exc
    finally:
        os.close(directory_fd)


def activate_services(
    audit_dir: Path,
    *,
    expected_journal_sha256: str,
    approved_by: str,
    approval_reference: str,
    owner_uid: int = 0,
    owner_gid: int = 0,
    validator: Callable[[Path, int, int], None] | None = None,
    state_reader: Callable[[], Mapping[str, list[str]]] = _systemd_states,
    service_action: Callable[[str, str], None] | None = None,
    process_check: Callable[[str], bool] | None = None,
    database_check: Callable[[], None] | None = None,
    activation_lock: Path = Path("/run/lock/armory-service-activation.lock"),
    report: Callable[[str], None] | None = None,
) -> None:
    """Activate only the hash-bound deployment with explicit approval.

    The immutable approved event authorizes this release's future bot restarts.
    The backup service requires a final success event. An unfinished bot start
    remains resumable but is never recorded as a successful activation.
    """
    if re.fullmatch(r"[0-9a-f]{64}", expected_journal_sha256) is None:
        raise AdminError("completion journal hash is invalid")
    if not approved_by.strip() or len(approved_by) > 128:
        raise AdminError("service activation administrator is invalid")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", approval_reference) is None:
        raise AdminError("service activation approval reference is invalid")
    directory_fd = _open_private_directory(
        audit_dir, owner_uid=owner_uid, exact_mode=0o700
    )
    activation_fd = -1
    success_record: str | None = None
    try:
        # Hold a shared audit lock throughout the unit operations. Deployment
        # and recovery writers require the exclusive lock; the start gate uses
        # a shared lock and may validate the explicit activation event.
        fcntl.flock(directory_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        try:
            lock_parent = activation_lock.parent.lstat()
            if (
                not stat.S_ISDIR(lock_parent.st_mode)
                or lock_parent.st_uid != owner_uid
                or not lock_parent.st_mode & stat.S_ISVTX
            ):
                raise AdminError("service activation lock directory is unsafe")
            activation_fd = os.open(
                activation_lock,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            activation_info = os.fstat(activation_fd)
            if (
                not stat.S_ISREG(activation_info.st_mode)
                or activation_info.st_uid != owner_uid
                or stat.S_IMODE(activation_info.st_mode) != 0o600
                or activation_info.st_nlink != 1
            ):
                raise AdminError("service activation lock metadata is unsafe")
            fcntl.flock(activation_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AdminError("another service activation is in progress") from exc
        loaded = None
        if validator is None:
            loaded = load_reviewed_python_source(
                COMPLETION_AUDIT_VALIDATOR,
                expected_sha256=RECOVERY_AUDIT_SHA256,
                owner_uid=owner_uid,
            )

        def verify_history() -> None:
            if validator is not None:
                validator(audit_dir, owner_uid, owner_gid)
            else:
                assert loaded is not None
                loaded.verify_audit_directory(
                    audit_dir,
                    owner=owner_uid,
                    group=owner_gid,
                    acquire_lock=False,
                    preserved_historical_directories=("reviewed-v2.0.0", "archive"),
                )

        verify_history()
        archive_name = f"deployment-state-{expected_journal_sha256}.json"
        archive_raw, _ = _read_at(
            directory_fd, archive_name, owner_uid=owner_uid, mode=0o600
        )
        journal = _json_object(archive_raw)
        if hashlib.sha256(archive_raw).hexdigest() != expected_journal_sha256:
            raise AdminError("completion archive hash is invalid")
        phase = journal.get("phase")
        manual_recovery = phase == "manual_recovery_required"
        if phase not in {"complete", "manual_recovery_required"}:
            raise AdminError("journal is not eligible for controlled activation")
        if manual_recovery and (
            validator is not None
            or owner_uid != 0
            or owner_gid != 0
            or state_reader is not _systemd_states
            or service_action is not None
            or process_check is not None
            or database_check is not None
            or activation_lock != Path("/run/lock/armory-service-activation.lock")
            or report is not None
        ):
            raise AdminError(
                "recovery activation requires fixed privileged evidence and controls"
            )
        if manual_recovery:
            release, previous = _validate_incident(journal, audit_dir)
            manifest_name = f"deployment-{release}.json"
            manifest_raw, _ = _read_at(
                directory_fd, manifest_name, owner_uid=owner_uid, mode=0o600
            )
            manifest = _json_object(manifest_raw)
            _validate_manifest(journal, manifest, release, previous, audit_dir)
            manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
            deployment_pin_sha256 = journal.get("deployment_pin_sha256")
            result_sha = journal.get("installation_result_sha256")
        else:
            release = journal.get("approved_release_sha")
            previous = journal.get("previous_release_sha")
            if not isinstance(release, str) or not isinstance(previous, str):
                raise AdminError("completed deployment release identity is invalid")
            manifest_name = f"deployment-{release}.json"
            manifest_raw, _ = _read_at(
                directory_fd, manifest_name, owner_uid=owner_uid, mode=0o600
            )
            manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
        ack_name = f"deployment-recovery-ack-{expected_journal_sha256}.json"
        ack_raw, _ = _read_at(directory_fd, ack_name, owner_uid=owner_uid, mode=0o600)
        ack = _json_object(ack_raw)
        if manual_recovery:
            if (
                ack.get("schema_version") != 2
                or ack.get("decision") != "manual_recovery_verified_with_addendum"
                or ack.get("journal_sha256") != expected_journal_sha256
                or ack.get("manifest_sha256") != manifest_sha
                or ack.get("approved_release_sha") != release
                or ack.get("previous_release_sha") != previous
            ):
                raise AdminError("recovery acknowledgment is invalid")
        elif (
            ack.get("schema_version") != 1
            or ack.get("decision") != "deployment_completion_verified"
            or ack.get("journal_sha256") != expected_journal_sha256
            or ack.get("approved_release_sha") != release
            or ack.get("approved_by") != journal.get("completion_approved_by")
            or ack.get("approval_reference")
            != journal.get("completion_approval_reference")
        ):
            raise AdminError("completion acknowledgment is invalid")
        recovery_lifecycle_transition = None
        activation_release = release
        if manual_recovery:
            addendum_name = f"recovery-addendum-{expected_journal_sha256}.json"
            addendum_raw, _ = _read_at(
                directory_fd, addendum_name, owner_uid=owner_uid, mode=0o600
            )
            addendum = _json_object(addendum_raw)
            addendum_sha256 = hashlib.sha256(addendum_raw).hexdigest()
            if (
                addendum_sha256 != ack.get("recovery_addendum_sha256")
                or addendum.get("journal_sha256") != expected_journal_sha256
                or not isinstance(addendum.get("database_sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", addendum["database_sha256"])
                is None
                or not isinstance(
                    addendum.get("verification_report_sha256"), str
                )
                or re.fullmatch(
                    r"[0-9a-f]{64}", addendum["verification_report_sha256"]
                )
                is None
            ):
                raise AdminError("recovery lifecycle evidence is inconsistent")
            audit_validator = loaded
            assert audit_validator is not None
            _validate_task267_addendum_with_module(
                audit_validator,
                addendum_raw,
                addendum,
                journal_sha256=expected_journal_sha256,
                manifest_sha256=manifest_sha,
                approved_release_sha=release,
                previous_release_sha=previous,
                journal_database_signature=journal.get("database_signature"),
            )
            review_sha = addendum.get("recovery_review_sha256")
            if not isinstance(review_sha, str) or re.fullmatch(
                r"[0-9a-f]{64}", review_sha
            ) is None:
                raise AdminError("recovery review hash is invalid")
            bound_review_name = (
                f"recovery-review-{expected_journal_sha256}-{review_sha}.json"
            )
            try:
                os.stat(bound_review_name, dir_fd=directory_fd, follow_symlinks=False)
                recovery_review_name = bound_review_name
            except FileNotFoundError:
                recovery_review_name = f"recovery-review-{expected_journal_sha256}.json"
            recovery_review_raw, recovery_review = _read_at(
                directory_fd,
                recovery_review_name,
                owner_uid=owner_uid,
                mode=0o600,
            )
            recovery_review = _json_object(recovery_review_raw)
            _validate_review_record(
                recovery_review,
                journal_sha256=expected_journal_sha256,
                manifest_sha256=manifest_sha,
                release_sha=release,
                previous_sha=previous,
                journal=journal,
                approved_by=ack.get("approved_by"),
                approval_reference=ack.get("approval_reference"),
                allow_timestamp_signature_delta=True,
            )
            if (
                hashlib.sha256(recovery_review_raw).hexdigest()
                != addendum.get("recovery_review_sha256")
                or addendum.get("recovery_release_sha")
                != Path(
                    recovery_review["observations"]["release_target"]
                ).name
            ):
                raise AdminError("recovery review is not bound to the addendum")
            activation_release = addendum["recovery_release_sha"]
            if activation_release not in {release, previous}:
                raise AdminError("recovery release is not incident-pinned")
            recovery_lifecycle_transition = {
                "schema_version": 1,
                "from_state": "RECOVERY_APPROVED",
                "to_state": "NORMAL_OPERATION",
                "journal_sha256": expected_journal_sha256,
                "acknowledgment_sha256": hashlib.sha256(ack_raw).hexdigest(),
                "addendum_sha256": addendum_sha256,
                "verification_report_sha256": addendum.get(
                    "verification_report_sha256"
                ),
                "database_sha256": addendum.get("database_sha256"),
                "recovery_approved_by": ack.get("approved_by"),
                "recovery_approval_reference": ack.get("approval_reference"),
                "activation_approved_by": approved_by,
                "activation_approval_reference": approval_reference,
                "release_sha": activation_release,
            }
        else:
            result_name = journal.get("installation_result")
            result_sha = journal.get("installation_result_sha256")
            if not isinstance(result_name, str) or not isinstance(result_sha, str):
                raise AdminError("completed installation result is unavailable")
            result_raw, _ = _read_at(
                directory_fd, result_name, owner_uid=owner_uid, mode=0o600
            )
            if (
                hashlib.sha256(result_raw).hexdigest() != result_sha
                or _json_object(result_raw).get("deployment_pin_sha256")
                != journal.get("deployment_pin_sha256")
                or _json_object(result_raw).get("manifest_sha256") != manifest_sha
            ):
                raise AdminError("installation report does not match completed deployment")
            deployment_pin_sha256 = journal.get("deployment_pin_sha256")
            if (
                not isinstance(deployment_pin_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", deployment_pin_sha256) is None
                or _json_object(result_raw).get("deployment_pin_sha256")
                != deployment_pin_sha256
            ):
                raise AdminError("deployment pin identity is invalid")
        intended = {
            SERVICE_NAME: journal.get("service_state"),
            TIMER_NAME: journal.get("backup_timer_state"),
        }
        if any(
            not isinstance(value, list)
            or len(value) != 2
            or value[0] not in {"active", "inactive"}
            or value[1] not in {"enabled", "disabled"}
            for value in intended.values()
        ):
            raise AdminError("saved service states are invalid")
        if manual_recovery and intended[SERVICE_NAME] != ["active", "enabled"]:
            raise AdminError("recovery activation must explicitly enable the bot")
        initial_states = state_reader()
        attempts = _read_activation_attempts(
            directory_fd, expected_journal_sha256, owner_uid=owner_uid
        )
        activation_id: str
        attempt_number: int
        events: list[dict[str, Any]]
        if attempts:
            attempt_number, activation_id, events = attempts[-1]
            lifecycle_already_established = any(
                event.get("schema_version") == 2 for _, _, prior in attempts
                for event in prior
            )
            last_event = events[-1]
            if last_event["event"] == "success":
                if (
                    last_event["approved_by"] != approved_by
                    or last_event["approval_reference"] != approval_reference
                    or any(
                        initial_states.get(unit) != target
                        for unit, target in intended.items()
                    )
                    or (
                        intended[SERVICE_NAME][0] == "active"
                        and not _running_process_matches_release(activation_release)
                    )
                ):
                    raise AdminError(
                        "completed activation no longer matches live state"
                    )
                return
            if last_event["event"] == "failure":
                if approval_reference == last_event["approval_reference"]:
                    raise AdminError("a failed activation retry needs new approval")
                attempt_number += 1
                activation_id = secrets.token_hex(16)
                events = []
            elif (
                last_event["approved_by"] != approved_by
                or last_event["approval_reference"] != approval_reference
            ):
                raise AdminError("activation resumption approval does not match")
        else:
            attempt_number = 1
            activation_id = secrets.token_hex(16)
            events = []
            lifecycle_already_established = False
        if lifecycle_already_established:
            recovery_lifecycle_transition = None
        elif recovery_lifecycle_transition is not None:
            recovery_lifecycle_transition["activation_id"] = activation_id
            recovery_lifecycle_transition["attempt_number"] = attempt_number
        if not events:
            if initial_states.get(SERVICE_NAME) != [
                "inactive",
                "disabled",
            ] or initial_states.get(TIMER_NAME) != ["inactive", "disabled"]:
                raise AdminError(
                    "services must be stopped and disabled before activation"
                )
            if _completion_process_present():
                raise AdminError("an Armory bot process is already running")
        elif _completion_process_present() and not _running_process_matches_release(
            activation_release
        ):
            raise AdminError("an unexpected Armory bot process is already running")
        release_dir = _verify_current_release_pointer(
            COMPLETION_BASE, activation_release, owner_uid=owner_uid
        )
        if manual_recovery:
            service_uid = pwd.getpwnam("wow-armory").pw_uid
            service_gid = grp.getgrnam("wow-armory").gr_gid
            _verify_release_tree(
                release_dir,
                service_uid=service_uid,
                service_gid=service_gid,
                owner_uid=owner_uid,
            )
        release_fd = _open_private_directory(
            release_dir,
            owner_uid=owner_uid,
            exact_mode=stat.S_IMODE(release_dir.stat().st_mode),
        )
        try:
            marker, _ = _read_at(
                release_fd,
                ".installation-complete",
                owner_uid=owner_uid,
                mode=0o444,
                maximum=128,
            )
        finally:
            os.close(release_fd)
        if marker.decode("ascii").strip() != activation_release:
            raise AdminError("installation marker differs from approved release")
        if database_check is None:
            preflight = subprocess.run(
                ["/opt/wow-forever-armory-bot/bin/armory-db-preflight"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin", "LANG": "C"},
                timeout=60,
                check=False,
            )
            if preflight.returncode != 0:
                raise AdminError(
                    "database/startup preflight refused service activation"
                )
        else:
            database_check()
        action = service_action or _activation_systemctl
        check_process = process_check or _running_process_matches_release

        event_fields = {
            "journal_sha256": expected_journal_sha256,
            "activation_id": activation_id,
            "attempt_number": attempt_number,
            "deployment_id": journal.get("deployment_id"),
            "release_sha": release,
            "completion_ack_sha256": hashlib.sha256(ack_raw).hexdigest(),
            "manifest_sha256": manifest_sha,
            "installation_result_sha256": result_sha,
            "deployment_pin_sha256": deployment_pin_sha256,
            "approved_by": approved_by,
            "approval_reference": approval_reference,
            "intended_states": intended,
            "owner_uid": owner_uid,
            "owner_gid": owner_gid,
        }

        def append_event(event_type: str, **details: Any) -> dict[str, Any]:
            sequence = len(events)
            event = _append_activation_event(
                directory_fd,
                sequence=sequence,
                event_type=event_type,
                observed_states=details.pop("observed_states", state_reader()),
                **event_fields,
                **details,
                lifecycle_transition=(
                    recovery_lifecycle_transition
                    if event_type == "success"
                    else None
                ),
            )
            events.append(event)
            return event

        try:
            if not events:
                append_event("approved")
            actions = []
            for unit, target in (
                (SERVICE_NAME, intended[SERVICE_NAME]),
                (TIMER_NAME, intended[TIMER_NAME]),
            ):
                actions.extend(
                    (
                        {
                            "unit": unit,
                            "verb": "enable" if target[1] == "enabled" else "disable",
                            "target": target,
                        },
                        {
                            "unit": unit,
                            "verb": "start" if target[0] == "active" else "stop",
                            "target": target,
                        },
                    )
                )
            verified = [
                event["action"]
                for event in events
                if event["event"] == "action_verified"
            ]
            pending = (
                events[-1]["action"]
                if events and events[-1]["event"] == "action_planned"
                else None
            )
            index = len(verified)
            if pending is not None and (
                index >= len(actions) or pending != actions[index]
            ):
                raise AdminError("interrupted activation action conflicts with plan")
            while index < len(actions):
                planned = actions[index]
                if pending is None:
                    append_event("action_planned", action=planned)
                else:
                    planned = pending
                action(planned["verb"], planned["unit"])
                observed = state_reader()
                unit_state = observed.get(planned["unit"])
                dimension = 1 if planned["verb"] in {"enable", "disable"} else 0
                if (
                    unit_state is None
                    or unit_state[dimension] != planned["target"][dimension]
                ):
                    raise AdminError("systemd action did not reach its approved state")
                if planned["unit"] == SERVICE_NAME and planned["verb"] == "start":
                    if not check_process(activation_release):
                        raise AdminError(
                            "running bot process is not from approved release"
                        )
                if planned["unit"] == SERVICE_NAME and planned["verb"] == "stop":
                    if _completion_process_present():
                        raise AdminError("bot process remains after approved stop")
                append_event(
                    "action_verified", action=planned, observed_states=observed
                )
                index += 1
                pending = None
            final_states = state_reader()
            if any(
                final_states.get(unit) != target for unit, target in intended.items()
            ):
                raise AdminError("services differ from approved activation states")
            append_event("success", observed_states=final_states)
            success_record = json.dumps(
                {
                    "result": "success",
                    "journal_sha256": expected_journal_sha256,
                    "activation_id": activation_id,
                    "approved_by": approved_by,
                    "approval_reference": approval_reference,
                },
                sort_keys=True,
            )
        except BaseException as exc:
            fail_event_error = None
            if not events or events[-1]["event"] not in {"failure", "success"}:
                try:
                    append_event(
                        "failure",
                        failure_class=(
                            type(exc).__name__
                            if type(exc).__name__ in {"AdminError", "OSError"}
                            else "AdminError"
                        ),
                        observed_states=state_reader(),
                    )
                except BaseException as audit_exc:
                    fail_event_error = type(audit_exc).__name__
            cleanup_errors = []
            for unit in (TIMER_NAME, SERVICE_NAME):
                for verb in ("stop", "disable"):
                    try:
                        action(verb, unit)
                    except BaseException:
                        cleanup_errors.append(type(exc).__name__)
            try:
                final = state_reader()
                if any(
                    final.get(unit) != ["inactive", "disabled"]
                    for unit in (TIMER_NAME, SERVICE_NAME)
                ):
                    cleanup_errors.append("UnverifiedFailClosed")
            except BaseException:
                cleanup_errors.append("StateObservationFailure")
            failure_record = json.dumps(
                {
                    "result": "failed",
                    "journal_sha256": expected_journal_sha256,
                    "approved_by": approved_by,
                    "approval_reference": approval_reference,
                    "activation_id": activation_id,
                    "failure_class": type(exc).__name__,
                    "audit_event_error": fail_event_error,
                    "fail_closed_errors": cleanup_errors,
                    "recorded_at_utc": datetime.now(UTC).isoformat(),
                },
                sort_keys=True,
            )
            if report:
                report(failure_record)
            else:
                _emit_activation_report(failure_record)
            raise AdminError(
                "service activation failed; managed units were stopped"
            ) from exc
    finally:
        if activation_fd >= 0:
            os.close(activation_fd)
        os.close(directory_fd)
    if success_record is not None:
        try:
            if report:
                report(success_record)
            else:
                _emit_activation_report(success_record)
        except OSError:
            # A durable success event is authoritative; reporting failure must
            # not stop services or append a contradictory activation failure.
            return


SERVICE_NAME = "wow-forever-armory-bot.service"
TIMER_NAME = "wow-forever-armory-backup.timer"


def _activation_systemctl(action: str, unit: str) -> None:
    if action not in {"enable", "disable", "start", "stop"} or unit not in {
        SERVICE_NAME,
        TIMER_NAME,
    }:
        raise AdminError("service activation target is not allowed")
    completed = subprocess.run(
        ["/usr/bin/systemctl", action, unit],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise AdminError("systemd rejected a service activation action")


def _emit_activation_report(payload: str) -> None:
    """Write a sanitized activation result to stderr and the system journal."""
    print(payload, file=sys.stderr)
    try:
        subprocess.run(
            ["/usr/bin/logger", "-t", "armory-deploy-admin", "--", payload],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            timeout=10,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        # The command output remains the report if journald is unavailable.
        return


def _running_process_matches_release(release_sha: str) -> bool:
    proc_root = Path("/proc")
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return False
    found = False
    expected = COMPLETION_BASE / "releases" / release_sha
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            argv = [
                part.decode(errors="replace")
                for part in (entry / "cmdline").read_bytes().split(b"\0")
                if part
            ]
            cwd = Path(os.readlink(entry / "cwd")).resolve(strict=True)
        except FileNotFoundError:
            continue
        except OSError:
            return False
        is_bot = (
            len(argv) >= 3 and argv[1:3] == ["-m", "wow_forever_armory.discord_bot"]
        ) or any(Path(value).name == "discord_bot.py" for value in argv)
        if is_bot:
            if cwd != expected:
                return False
            found = True
    return found


def _verify_current_release_pointer(
    base: Path, release_sha: str, *, owner_uid: int
) -> Path:
    """Accept only the canonical relative or exact absolute release symlink."""
    current = base / "current"
    target = base / "releases" / release_sha
    try:
        before = current.lstat()
        link_text = os.readlink(current)
        if (
            not stat.S_ISLNK(before.st_mode)
            or before.st_uid != owner_uid
            or link_text not in {f"releases/{release_sha}", str(target)}
            or current.resolve(strict=True) != target
        ):
            raise AdminError("current release pointer differs from approved release")
        after = current.lstat()
        if (
            _signature(before) != _signature(after)
            or os.readlink(current) != link_text
            or current.resolve(strict=True) != target
        ):
            raise AdminError("current release pointer changed during verification")
    except OSError as exc:
        raise AdminError("current release pointer cannot be verified") from exc
    return target


def acknowledge_incident(
    audit_dir: Path,
    review_record_path: Path,
    *,
    expected_journal_sha256: str,
    approved_by: str,
    approval_reference: str,
    owner_uid: int = 0,
    owner_gid: int = 0,
    state_reader: Callable[[], Mapping[str, list[str]]] = _systemd_states,
    evidence_verifier: Callable[
        [Mapping[str, Any], str, str, Path, Mapping[str, Any]], None
    ] = _verify_recovery_evidence,
    checkpoint: Callable[[str], None] | None = None,
    recovery_addendum_sha256: str | None = None,
) -> tuple[str, str]:
    """Verify and idempotently finish an approved incident acknowledgment."""
    if re.fullmatch(r"[0-9a-f]{64}", expected_journal_sha256) is None:
        raise AdminError("expected journal hash is invalid")
    if not approved_by.strip() or len(approved_by) > 128:
        raise AdminError("administrator identity is invalid")
    if not approval_reference.strip() or len(approval_reference) > 256:
        raise AdminError("approval reference is invalid")
    if review_record_path.parent != audit_dir:
        raise AdminError("recovery review record must be in the audit directory")
    if recovery_addendum_sha256 is not None and (
        re.fullmatch(r"[0-9a-f]{64}", recovery_addendum_sha256) is None
        or expected_journal_sha256 != TASK267_JOURNAL_SHA256
        or evidence_verifier is not _DEFAULT_RECOVERY_EVIDENCE_VERIFIER
        or state_reader is not _systemd_states
        or checkpoint is not None
        or owner_uid != 0
        or owner_gid != 0
        or audit_dir != AUDIT_DIR
        or not re.fullmatch(
            rf"recovery-review-{expected_journal_sha256}(?:-[0-9a-f]{{64}})?\.json",
            review_record_path.name,
        )
    ):
        raise AdminError("addendum recovery parameters are not approved")

    directory_fd = _open_private_directory(
        audit_dir, owner_uid=owner_uid, exact_mode=0o700
    )
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _verify_directory_binding(
            directory_fd, audit_dir, owner_uid=owner_uid, exact_mode=0o700
        )
        archive_name = f"deployment-state-{expected_journal_sha256}.json"
        ack_name = f"deployment-recovery-ack-{expected_journal_sha256}.json"
        active_present = True
        try:
            os.stat("deployment-state.json", dir_fd=directory_fd, follow_symlinks=False)
            active_present = True
        except FileNotFoundError:
            active_present = False
        try:
            os.stat(archive_name, dir_fd=directory_fd, follow_symlinks=False)
            archive_present = True
        except FileNotFoundError:
            archive_present = False
        if active_present:
            try:
                os.stat(ack_name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise AdminError(
                    "active journal conflicts with an existing acknowledgment"
                )
        if active_present:
            journal_raw, journal_info = _read_at(
                directory_fd,
                "deployment-state.json",
                owner_uid=owner_uid,
                mode=0o600,
                expected_nlink=2 if archive_present else 1,
            )
        else:
            journal_raw, journal_info = _read_at(
                directory_fd, archive_name, owner_uid=owner_uid, mode=0o600
            )
        journal_sha256 = hashlib.sha256(journal_raw).hexdigest()
        if journal_sha256 != expected_journal_sha256:
            raise AdminError("journal or archive hash differs from reviewed hash")
        journal = _json_object(journal_raw)
        release_sha, previous_sha = _validate_incident(journal, audit_dir)
        manifest_name = f"deployment-{release_sha}.json"
        manifest_raw, _ = _read_at(
            directory_fd, manifest_name, owner_uid=owner_uid, mode=0o600
        )
        manifest = _json_object(manifest_raw)
        _validate_manifest(journal, manifest, release_sha, previous_sha, audit_dir)
        manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
        addendum_raw = None
        addendum = None
        historical_attestation_raw = None
        historical_review_raw = None
        staged_report_raw = None
        if recovery_addendum_sha256 is not None:
            if manifest_sha256 != TASK267_MANIFEST_SHA256:
                raise AdminError("addendum manifest hash differs from its pin")
            addendum_name = f"recovery-addendum-{journal_sha256}.json"
            addendum_raw, _ = _read_at(
                directory_fd, addendum_name, owner_uid=owner_uid, mode=0o600
            )
            if hashlib.sha256(addendum_raw).hexdigest() != recovery_addendum_sha256:
                raise AdminError("recovery addendum hash differs from approval")
            historical_attestation_raw, _ = _read_at(
                directory_fd,
                f"task262-attestation-{TASK267_ATTESTATION_SHA256}.json",
                owner_uid=owner_uid,
                mode=0o600,
                maximum=2_000_000,
            )
            historical_review_raw, _ = _read_at(
                directory_fd,
                f"task262-review-{TASK267_REVIEW_SHA256}.json",
                owner_uid=owner_uid,
                mode=0o600,
                maximum=2_000_000,
            )
            if (
                hashlib.sha256(historical_attestation_raw).hexdigest()
                != TASK267_ATTESTATION_SHA256
                or hashlib.sha256(historical_review_raw).hexdigest()
                != TASK267_REVIEW_SHA256
            ):
                raise AdminError("historical recovery evidence hash differs from pin")
            addendum = _json_object(addendum_raw)
            validator = load_reviewed_python_source(
                COMPLETION_AUDIT_VALIDATOR,
                expected_sha256=RECOVERY_AUDIT_SHA256,
                owner_uid=owner_uid,
            )
            _validate_task267_addendum_with_module(
                validator,
                addendum_raw,
                addendum,
                journal_sha256=journal_sha256,
                manifest_sha256=manifest_sha256,
                approved_release_sha=release_sha,
                previous_release_sha=previous_sha,
                journal_database_signature=journal.get("database_signature"),
            )
            if (
                addendum.get("approved_by") != approved_by
                or addendum.get("approval_reference") != approval_reference
            ):
                raise AdminError("CLI approval does not match addendum approval")
        review_name = review_record_path.name
        if addendum is None:
            expected_review_name = f"recovery-review-{journal_sha256}.json"
        else:
            expected_review_name = (
                f"recovery-review-{journal_sha256}-"
                f"{addendum.get('recovery_review_sha256')}.json"
            )
        legacy_review_name = f"recovery-review-{journal_sha256}.json"
        if review_name not in {expected_review_name, legacy_review_name}:
            raise AdminError("recovery review record filename is invalid")
        review_raw, _ = _read_at(
            directory_fd, review_name, owner_uid=owner_uid, mode=0o600
        )
        review = _json_object(review_raw)
        _validate_review_record(
            review,
            journal_sha256=journal_sha256,
            manifest_sha256=manifest_sha256,
            release_sha=release_sha,
            previous_sha=previous_sha,
            journal=journal,
            approved_by=approved_by,
            approval_reference=approval_reference,
            allow_timestamp_signature_delta=addendum is not None,
        )
        if addendum is not None and (
            addendum.get("approved_at_utc") != review.get("approved_at_utc")
            or addendum.get("recovery_review_sha256")
            != hashlib.sha256(review_raw).hexdigest()
            or review_name
            != (
                f"recovery-review-{journal_sha256}-"
                f"{addendum.get('recovery_review_sha256')}.json"
            )
            and review_name != legacy_review_name
            or addendum.get("recovery_release_sha")
            != Path(review["observations"]["release_target"]).name
        ):
            raise AdminError("addendum and recovery review bindings differ")
        if addendum is None:
            if evidence_verifier is _DEFAULT_RECOVERY_EVIDENCE_VERIFIER:
                _verify_recovery_evidence(
                    journal,
                    release_sha,
                    previous_sha,
                    audit_dir,
                    review,
                    owner_uid=owner_uid,
                    locked_directory_fd=directory_fd,
                )
            else:
                evidence_verifier(journal, release_sha, previous_sha, audit_dir, review)
        else:
            expected_live_signature = addendum["verified_database_signature"]
            observations = review["observations"]
            if observations["database_signature"] != expected_live_signature:
                raise AdminError("review signature differs from approved addendum")
            report_uid = pwd.getpwnam("nolo-admin").pw_uid
            report_raw, report_hash = _read_task267_report(report_uid)
            if report_hash != TASK267_REPORT_SHA256:
                raise AdminError("fresh verification report hash differs from pin")
            staged_report_raw, _ = _read_at(
                directory_fd,
                f"task267-verification-{TASK267_REPORT_SHA256}.json",
                owner_uid=owner_uid,
                mode=0o600,
                maximum=2_000_000,
            )
            if staged_report_raw != report_raw:
                raise AdminError("staged verification report differs from its source")
            report = _json_object(report_raw)
            _validate_task267_report(
                report,
                journal,
                manifest,
                addendum,
                journal_sha256=journal_sha256,
                manifest_sha256=manifest_sha256,
            )
            live_db_info = COMPLETION_DATABASE.lstat()
            live_signature = [
                live_db_info.st_dev,
                live_db_info.st_ino,
                live_db_info.st_size,
                live_db_info.st_mtime_ns,
                live_db_info.st_ctime_ns,
                live_db_info.st_uid,
                live_db_info.st_gid,
                live_db_info.st_mode,
            ]
            live_hash = _hash_regular_file(
                COMPLETION_DATABASE,
                owner_uid=live_db_info.st_uid,
                owner_gid=live_db_info.st_gid,
                mode=0o600,
                parent_mode=0o700,
            )
            if live_signature != expected_live_signature or live_hash != addendum["database_sha256"]:
                raise AdminError("live database differs from approved addendum")
            for suffix in ("-wal", "-shm", "-journal", ".restore-in-progress"):
                try:
                    Path(f"{COMPLETION_DATABASE}{suffix}").lstat()
                except FileNotFoundError:
                    continue
                raise AdminError("database sidecar or restore marker is present")
            try:
                wow_uid = pwd.getpwnam("wow-armory").pw_uid
                wow_gid = grp.getgrnam("wow-armory").gr_gid
            except KeyError as exc:
                raise AdminError("Armory service identity is unavailable") from exc
            latest_backup_hash = _hash_regular_file(
                TASK267_LATEST_BACKUP,
                owner_uid=wow_uid,
                owner_gid=wow_gid,
                mode=0o600,
                parent_mode=0o700,
            )
            preflight_hash = _hash_regular_file(
                Path(journal["shared_preflight_backup"]),
                owner_uid=owner_uid,
                owner_gid=owner_gid,
                mode=0o700,
                parent_mode=0o700,
                locked_directory_fd=directory_fd,
            )
            if (
                latest_backup_hash != TASK267_LATEST_BACKUP_SHA256
                or preflight_hash != TASK267_PREFLIGHT_SHA256
                or addendum.get("backup_sha256") != TASK267_INCIDENT_BACKUP_SHA256
                or addendum.get("latest_backup_sha256") != latest_backup_hash
                or addendum.get("saved_preflight_sha256") != preflight_hash
            ):
                raise AdminError("backup or saved preflight evidence changed")
            _require_recovery_unit_states(state_reader())
            if _database_open_handle_count(COMPLETION_DATABASE) != 0:
                raise AdminError("a process still has the production database open")
            proof_journal = dict(journal)
            proof_journal["database_signature"] = expected_live_signature
            proof_review = dict(review)
            proof_observations = dict(observations)
            proof_review["observations"] = proof_observations
            _verify_recovery_evidence(
                proof_journal,
                release_sha,
                previous_sha,
                audit_dir,
                proof_review,
                base=COMPLETION_BASE,
                database=COMPLETION_DATABASE,
                service_uid=wow_uid,
                service_gid=wow_gid,
                owner_uid=owner_uid,
                locked_directory_fd=directory_fd,
            )

        _verify_directory_binding(
            directory_fd, audit_dir, owner_uid=owner_uid, exact_mode=0o700
        )
        if active_present:
            final_journal, _ = _read_at(
                directory_fd,
                "deployment-state.json",
                owner_uid=owner_uid,
                mode=0o600,
                expected_nlink=2 if archive_present else 1,
            )
        else:
            final_journal, _ = _read_at(
                directory_fd, archive_name, owner_uid=owner_uid, mode=0o600
            )
        final_manifest, _ = _read_at(
            directory_fd, manifest_name, owner_uid=owner_uid, mode=0o600
        )
        final_review, _ = _read_at(
            directory_fd, review_name, owner_uid=owner_uid, mode=0o600
        )
        if (
            final_journal != journal_raw
            or final_manifest != manifest_raw
            or final_review != review_raw
        ):
            raise AdminError("recovery evidence changed before archival")
        if addendum is not None:
            final_addendum, _ = _read_at(
                directory_fd,
                f"recovery-addendum-{journal_sha256}.json",
                owner_uid=owner_uid,
                mode=0o600,
            )
            final_historical_attestation, _ = _read_at(
                directory_fd,
                f"task262-attestation-{TASK267_ATTESTATION_SHA256}.json",
                owner_uid=owner_uid,
                mode=0o600,
                maximum=2_000_000,
            )
            final_historical_review, _ = _read_at(
                directory_fd,
                f"task262-review-{TASK267_REVIEW_SHA256}.json",
                owner_uid=owner_uid,
                mode=0o600,
                maximum=2_000_000,
            )
            final_staged_report, _ = _read_at(
                directory_fd,
                f"task267-verification-{TASK267_REPORT_SHA256}.json",
                owner_uid=owner_uid,
                mode=0o600,
                maximum=2_000_000,
            )
            final_report_raw, final_report_hash = _read_task267_report(
                pwd.getpwnam("nolo-admin").pw_uid
            )
            final_db_info = COMPLETION_DATABASE.lstat()
            final_db_hash = _hash_regular_file(
                COMPLETION_DATABASE,
                owner_uid=final_db_info.st_uid,
                owner_gid=final_db_info.st_gid,
                mode=0o600,
                parent_mode=0o700,
            )
            final_latest_backup = _hash_regular_file(
                TASK267_LATEST_BACKUP,
                owner_uid=wow_uid,
                owner_gid=wow_gid,
                mode=0o600,
                parent_mode=0o700,
            )
            final_incident_backup = _hash_regular_file(
                Path(journal["database_backup"]),
                owner_uid=wow_uid,
                owner_gid=wow_gid,
                mode=0o600,
                parent_mode=0o700,
            )
            final_preflight_hash = _hash_regular_file(
                Path(journal["shared_preflight_backup"]),
                owner_uid=owner_uid,
                owner_gid=owner_gid,
                mode=0o700,
                parent_mode=0o700,
                locked_directory_fd=directory_fd,
            )
            if (
                final_addendum != addendum_raw
                or final_historical_attestation != historical_attestation_raw
                or final_historical_review != historical_review_raw
                or final_staged_report != staged_report_raw
                or final_report_hash != TASK267_REPORT_SHA256
                or final_report_raw != report_raw
                or final_db_hash != addendum.get("database_sha256")
                or final_latest_backup != TASK267_LATEST_BACKUP_SHA256
                or final_incident_backup != TASK267_INCIDENT_BACKUP_SHA256
                or final_preflight_hash != TASK267_PREFLIGHT_SHA256
                or [
                    final_db_info.st_dev,
                    final_db_info.st_ino,
                    final_db_info.st_size,
                    final_db_info.st_mtime_ns,
                    final_db_info.st_ctime_ns,
                    final_db_info.st_uid,
                    final_db_info.st_gid,
                    final_db_info.st_mode,
                ] != addendum.get("verified_database_signature")
                or _database_open_handle_count(COMPLETION_DATABASE) != 0
            ):
                raise AdminError("addendum evidence changed before archival")
            for suffix in ("-wal", "-shm", "-journal", ".restore-in-progress"):
                try:
                    Path(f"{COMPLETION_DATABASE}{suffix}").lstat()
                except FileNotFoundError:
                    continue
                raise AdminError("database sidecar appeared before archival")
        _require_recovery_unit_states(state_reader())

        ack = {
            "schema_version": 2 if addendum is not None else 1,
            "journal_sha256": journal_sha256,
            "manifest_sha256": manifest_sha256,
            "approved_release_sha": release_sha,
            "previous_release_sha": previous_sha,
            "decision": (
                "manual_recovery_verified_with_addendum"
                if addendum is not None
                else "manual_recovery_verified"
            ),
            "approved_by": approved_by,
            "approval_reference": approval_reference,
            "approved_at_utc": review["approved_at_utc"],
        }
        if addendum is not None:
            ack["recovery_addendum_sha256"] = recovery_addendum_sha256
        ack_raw = (
            json.dumps(ack, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("ascii")

        def checkpoint_at(name: str) -> None:
            if checkpoint is not None:
                checkpoint(name)

        def recheck_addendum_quiescence() -> None:
            if addendum is None:
                return
            _require_recovery_unit_states(state_reader())
            info = COMPLETION_DATABASE.lstat()
            signature = [
                info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns, info.st_uid, info.st_gid, info.st_mode,
            ]
            digest = _hash_regular_file(
                COMPLETION_DATABASE,
                owner_uid=info.st_uid,
                owner_gid=info.st_gid,
                mode=0o600,
                parent_mode=0o700,
            )
            if (
                signature != addendum.get("verified_database_signature")
                or digest != addendum.get("database_sha256")
                or _database_open_handle_count(COMPLETION_DATABASE) != 0
            ):
                raise AdminError("database changed or became active before publication")
            for suffix in ("-wal", "-shm", "-journal", ".restore-in-progress"):
                try:
                    Path(f"{COMPLETION_DATABASE}{suffix}").lstat()
                except FileNotFoundError:
                    continue
                raise AdminError("database sidecar appeared before publication")

        try:
            os.stat(archive_name, dir_fd=directory_fd, follow_symlinks=False)
            archive_exists = True
        except FileNotFoundError:
            archive_exists = False
        if archive_exists:
            archive_raw, archive_info = _read_at(
                directory_fd,
                archive_name,
                owner_uid=owner_uid,
                mode=0o600,
                expected_nlink=2 if active_present else 1,
            )
        else:
            archive_raw = b""
            archive_info = None

        if archive_exists:
            if archive_raw != journal_raw:
                raise AdminError("existing journal archive has different bytes")
            if active_present:
                active_info = os.stat(
                    "deployment-state.json",
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if _stable_signature(active_info) != _stable_signature(archive_info):
                    raise AdminError("active journal and archive are not the same file")
        elif not active_present:
            raise AdminError("active journal and its archive are both missing")
        else:
            recheck_addendum_quiescence()
            checkpoint_at("before_archive")
            try:
                os.link(
                    "deployment-state.json",
                    archive_name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise AdminError("journal archive could not be created safely") from exc
            os.fsync(directory_fd)
            checkpoint_at("archive_created")
            archive_raw, archive_info = _read_at(
                directory_fd,
                archive_name,
                owner_uid=owner_uid,
                mode=0o600,
                expected_nlink=2,
            )
            if archive_raw != journal_raw or archive_info.st_nlink != 2:
                raise AdminError("new journal archive failed exact verification")
            active_info = os.stat(
                "deployment-state.json", dir_fd=directory_fd, follow_symlinks=False
            )
            if _stable_signature(active_info) != _stable_signature(archive_info):
                raise AdminError("active journal changed while archiving")
            checkpoint_at("archive_verified")

        if active_present:
            if addendum is None:
                _require_recovery_unit_states(state_reader())
            else:
                recheck_addendum_quiescence()
            active_raw, active_info = _read_at(
                directory_fd,
                "deployment-state.json",
                owner_uid=owner_uid,
                mode=0o600,
                expected_nlink=2 if archive_exists or archive_info is not None else 1,
            )
            if (
                active_raw != journal_raw
                or archive_info is None
                or _stable_signature(active_info) != _stable_signature(archive_info)
            ):
                raise AdminError("active journal changed before removal")
            try:
                os.stat(ack_name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise AdminError(
                    "active journal conflicts with an existing acknowledgment"
                )
            os.unlink("deployment-state.json", dir_fd=directory_fd)
            os.fsync(directory_fd)
            checkpoint_at("active_removed")

        archived_raw, archived_info = _read_at(
            directory_fd, archive_name, owner_uid=owner_uid, mode=0o600
        )
        if (
            archived_raw != journal_raw
            or hashlib.sha256(archived_raw).hexdigest() != journal_sha256
            or archived_info.st_nlink != 1
        ):
            raise AdminError("archived journal failed byte-for-byte verification")
        if addendum is None:
            _require_recovery_unit_states(state_reader())
        else:
            recheck_addendum_quiescence()

        _clean_ack_temporaries(
            directory_fd,
            ack_name,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            mode=0o600,
        )
        try:
            os.stat(ack_name, dir_fd=directory_fd, follow_symlinks=False)
            ack_exists = True
        except FileNotFoundError:
            ack_exists = False
        if ack_exists:
            existing_ack, _ = _read_at(
                directory_fd, ack_name, owner_uid=owner_uid, mode=0o600
            )
        else:
            existing_ack = None
        if existing_ack is not None:
            if existing_ack != ack_raw:
                raise AdminError("existing acknowledgment conflicts with this review")
            return journal_sha256, manifest_sha256

        checkpoint_at("before_ack_publication")
        recheck_addendum_quiescence()
        _write_new_at(
            directory_fd,
            ack_name,
            ack_raw,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            mode=0o600,
        )
        checkpoint_at("ack_published")
        _verify_directory_binding(
            directory_fd, audit_dir, owner_uid=owner_uid, exact_mode=0o700
        )
        return journal_sha256, manifest_sha256
    except OSError as exc:
        raise AdminError(
            "recovery acknowledgment stopped on a filesystem error"
        ) from exc
    finally:
        os.close(directory_fd)


def _task301_database_signature(path: Path) -> list[int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise AdminError("production database metadata is unsafe")
    return [
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_uid,
        info.st_gid,
        info.st_mode,
    ]


def _task301_sqlite_sidecars(path: Path, *, owner_uid: int, owner_gid: int) -> dict[str, Any]:
    """Capture stable, read-only identities for SQLite sidecar files."""
    result: dict[str, Any] = {}
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{path}{suffix}")
        try:
            info = sidecar.lstat()
        except FileNotFoundError:
            result[suffix] = None
            continue
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != owner_uid
            or info.st_gid != owner_gid
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise AdminError("SQLite sidecar metadata is unsafe")
        result[suffix] = [
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            info.st_uid,
            info.st_gid,
            info.st_mode,
        ]
    return result


def _task301_pre_shutdown_evidence(
    journal: Mapping[str, Any],
    *,
    base: Path,
    database: Path,
    audit_dir: Path,
    backup: Path,
    historical_preflight: Path,
    owner_uid: int,
    owner_gid: int,
    service_uid: int,
    service_gid: int,
    state_reader: Callable[[], Mapping[str, list[str]]],
    process_reader: Callable[[], int],
    snapshot_verifier: Callable[..., tuple[int, int, int]],
    locked_directory_fd: int,
) -> dict[str, Any]:
    """Freshly verify the one approved backup_verified incident, read-only."""
    current = base / "current"
    try:
        pointer_info = current.lstat()
        pointer = os.readlink(current)
        target = current.resolve(strict=True)
    except OSError as exc:
        raise AdminError("current release pointer is unavailable") from exc
    expected_target = base / "releases" / TASK301_PREVIOUS_SHA
    if (
        not stat.S_ISLNK(pointer_info.st_mode)
        or pointer_info.st_uid != owner_uid
        or pointer not in {
            f"releases/{TASK301_PREVIOUS_SHA}",
            str(expected_target),
        }
        or target != expected_target
    ):
        raise AdminError("current release is not the approved previous release")
    release_fd = _open_private_directory(
        expected_target,
        owner_uid=owner_uid,
        exact_mode=stat.S_IMODE(expected_target.stat().st_mode),
    )
    try:
        marker, _ = _read_at(
            release_fd, ".installation-complete", owner_uid=owner_uid, mode=0o444
        )
    finally:
        os.close(release_fd)
    if marker.decode("ascii").strip() != TASK301_PREVIOUS_SHA:
        raise AdminError("previous release marker is invalid")

    if database != COMPLETION_DATABASE or backup != TASK301_BACKUP:
        # Test seams may use temporary paths; production CLI pins the real ones.
        if owner_uid == 0:
            raise AdminError("production database or backup path differs from pin")
    db_info = database.lstat()
    db_signature = _task301_database_signature(database)
    if (
        db_info.st_uid != service_uid
        or db_info.st_gid != service_gid
        or stat.S_IMODE(db_info.st_mode) != 0o600
    ):
        raise AdminError("production database ownership or mode is unsafe")
    db_sidecars = _task301_sqlite_sidecars(
        database, owner_uid=service_uid, owner_gid=service_gid
    )
    db_hash = _hash_regular_file(
        database,
        owner_uid=service_uid,
        owner_gid=service_gid,
        mode=0o600,
        parent_mode=0o700,
    )
    if db_hash != TASK301_DATABASE_SHA256:
        raise AdminError("production database bytes differ from reviewed evidence")
    db_counts = snapshot_verifier(
        database,
        expected_owner=service_uid,
        expected_group=service_gid,
        expected_sha256=TASK301_DATABASE_SHA256,
    )
    if tuple(db_counts) != (1, 4, 4):
        raise AdminError("production database contents differ from reviewed evidence")
    if (
        _task301_database_signature(database) != db_signature
        or _task301_sqlite_sidecars(
            database, owner_uid=service_uid, owner_gid=service_gid
        )
        != db_sidecars
    ):
        raise AdminError("production database or sidecars changed during snapshot")

    if (
        journal.get("database_backup") != str(backup)
        or journal.get("database_backup_sha256") != TASK301_BACKUP_SHA256
        or journal.get("database_signature") is not None
        or journal.get("shared_preflight_backup") is not None
        or journal.get("shared_preflight_sha256") is not None
    ):
        raise AdminError("journal does not match the interrupted backup_verified schema")
    backup_hash = _hash_regular_file(
        backup,
        owner_uid=service_uid,
        owner_gid=service_gid,
        mode=0o600,
        parent_mode=0o700,
    )
    if backup_hash != TASK301_BACKUP_SHA256:
        raise AdminError("registered backup hash differs from reviewed evidence")
    backup_sidecars = _task301_sqlite_sidecars(
        backup, owner_uid=service_uid, owner_gid=service_gid
    )
    backup_counts = snapshot_verifier(
        backup,
        expected_owner=service_uid,
        expected_group=service_gid,
        expected_sha256=TASK301_BACKUP_SHA256,
    )
    if tuple(backup_counts) != (1, 4, 4):
        raise AdminError("registered backup contents differ from reviewed evidence")
    if _task301_sqlite_sidecars(
        backup, owner_uid=service_uid, owner_gid=service_gid
    ) != backup_sidecars:
        raise AdminError("registered backup sidecars changed during snapshot")

    historical_hash = _hash_regular_file(
        historical_preflight,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        mode=0o444,
        parent_mode=0o700,
        locked_directory_fd=locked_directory_fd,
    )
    if historical_hash != TASK301_HISTORICAL_PREFLIGHT_SHA256:
        raise AdminError("historical DB-preflight evidence differs from its pin")
    states = dict(state_reader())
    expected_states = {
        SERVICE_NAME: ["inactive", "disabled"],
        TIMER_NAME: ["inactive", "disabled"],
        BACKUP_SERVICE: ["inactive", "static"],
    }
    if states != expected_states:
        raise AdminError("Armory services are not in the required stopped state")
    if process_reader() != 0:
        raise AdminError("an Armory process is still running")
    if _task301_database_signature(database) != db_signature:
        raise AdminError("production database changed during recovery verification")
    if os.readlink(current) != pointer or current.resolve(strict=True) != expected_target:
        raise AdminError("current release pointer changed during recovery verification")
    return {
        "release_target": str(expected_target),
        "installation_marker": TASK301_PREVIOUS_SHA,
        "database_signature": db_signature,
        "database_sha256": db_hash,
        "database_integrity": "ok",
        "database_counts": list(db_counts),
        "database_sidecars": db_sidecars,
        "backup_path": str(backup),
        "backup_sha256": backup_hash,
        "backup_integrity": "ok",
        "backup_counts": list(backup_counts),
        "backup_sidecars": backup_sidecars,
        "historical_preflight_path": str(historical_preflight),
        "historical_preflight_sha256": historical_hash,
        "systemd_states": states,
        "armory_process_count": 0,
    }


def acknowledge_task301_pre_shutdown(
    audit_dir: Path,
    *,
    expected_journal_sha256: str,
    approved_by: str,
    approval_reference: str,
    owner_uid: int = 0,
    owner_gid: int = 0,
    base: Path = COMPLETION_BASE,
    database: Path = COMPLETION_DATABASE,
    backup: Path = TASK301_BACKUP,
    historical_preflight: Path = TASK301_HISTORICAL_PREFLIGHT,
    service_uid: int | None = None,
    service_gid: int | None = None,
    state_reader: Callable[[], Mapping[str, list[str]]] = _systemd_states,
    process_reader: Callable[[], int] | None = None,
    snapshot_verifier: Callable[..., tuple[int, int, int]] | None = None,
    history_verifier: Callable[[Path, int, int], Any] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[str, str]:
    """Archive and acknowledge the pinned pre-shutdown incident without DB writes."""
    if (
        expected_journal_sha256 != TASK301_JOURNAL_SHA256
        or owner_uid != os.geteuid()
        or (owner_uid == 0 and (audit_dir != AUDIT_DIR or owner_gid != 0))
        or not approved_by.strip()
        or len(approved_by) > 128
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", approval_reference)
        is None
    ):
        raise AdminError("pre-shutdown recovery request is not the approved incident")
    if service_uid is None or service_gid is None:
        try:
            service_uid = pwd.getpwnam("wow-armory").pw_uid
            service_gid = grp.getgrnam("wow-armory").gr_gid
        except KeyError as exc:
            raise AdminError("Armory service account is unavailable") from exc
    if process_reader is None:
        def process_reader() -> int:
            try:
                result = subprocess.run(
                    ["/usr/bin/pgrep", "-af", "wow_forever_armory"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C"},
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise AdminError("Armory process state could not be verified") from exc
            if result.returncode not in {0, 1}:
                raise AdminError("Armory process state could not be verified")
            return sum("pgrep" not in line for line in result.stdout.splitlines())
    if snapshot_verifier is None:
        snapshot_module = load_reviewed_python_source(
            COMPLETION_SNAPSHOT_MODULE,
            expected_sha256=SNAPSHOT_MODULE_SHA256,
            owner_uid=owner_uid,
        )
        snapshot_verifier = snapshot_module.verify_database_snapshot

    if history_verifier is None:
        def history_verifier(path: Path, uid: int, gid: int) -> None:
            verifier = load_reviewed_python_source(
                COMPLETION_AUDIT_VALIDATOR,
                expected_sha256=RECOVERY_AUDIT_SHA256,
                owner_uid=uid,
            )
            verifier.verify_audit_directory(
                path,
                owner=uid,
                group=gid,
                acquire_lock=False,
                preserved_historical_directories=("reviewed-v2.0.0", "archive"),
            )

    directory_fd = _open_private_directory(
        audit_dir, owner_uid=owner_uid, exact_mode=0o700
    )
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _verify_directory_binding(
            directory_fd, audit_dir, owner_uid=owner_uid, exact_mode=0o700
        )
        archive_name = f"deployment-state-{expected_journal_sha256}.json"
        ack_name = f"deployment-recovery-ack-{expected_journal_sha256}.json"
        active_present = True
        try:
            active_info = os.stat(
                "deployment-state.json", dir_fd=directory_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            active_present = False
            active_info = None
        if not active_present:
            journal_raw, _ = _read_at(
                directory_fd,
                archive_name,
                owner_uid=owner_uid,
                mode=0o600,
            )
        else:
            archive_present = False
            try:
                archive_info = os.stat(
                    archive_name, dir_fd=directory_fd, follow_symlinks=False
                )
                archive_present = True
            except FileNotFoundError:
                archive_info = None
            expected_links = 2 if archive_present else 1
            if active_info.st_nlink != expected_links or (
                archive_info is not None and archive_info.st_nlink != 2
            ):
                raise AdminError("active journal archive link state is unsafe")
            journal_raw, _ = _read_at(
                directory_fd,
                "deployment-state.json",
                owner_uid=owner_uid,
                mode=0o600,
                expected_nlink=expected_links,
            )
            if archive_present:
                archive_raw, _ = _read_at(
                    directory_fd,
                    archive_name,
                    owner_uid=owner_uid,
                    mode=0o600,
                    expected_nlink=2,
                )
                if archive_raw != journal_raw:
                    raise AdminError("active and archived journal bytes differ")
        journal_sha = hashlib.sha256(journal_raw).hexdigest()
        if journal_sha != expected_journal_sha256:
            raise AdminError("journal bytes differ from the approved incident hash")
        journal = _json_object(journal_raw)
        if (
            journal.get("phase") != "manual_recovery_required"
            or journal.get("interrupted_phase") != "backup_verified"
            or journal.get("approved_release_sha") != TASK301_RELEASE_SHA
            or journal.get("previous_release_sha") != TASK301_PREVIOUS_SHA
            or journal.get("database_signature") is not None
            or journal.get("shared_preflight_backup") is not None
            or journal.get("shared_preflight_sha256") is not None
        ):
            raise AdminError("journal does not match the approved pre-shutdown incident")
        if not active_present:
            history_verifier(audit_dir, owner_uid, owner_gid)
            ack_raw, _ = _read_at(
                directory_fd, ack_name, owner_uid=owner_uid, mode=0o600
            )
            ack = _json_object(ack_raw)
            if (
                set(ack) != PRE_SHUTDOWN_ACK_FIELDS
                or ack.get("schema_version") != 3
                or ack.get("journal_sha256") != journal_sha
                or ack.get("approved_release_sha") != TASK301_RELEASE_SHA
                or ack.get("previous_release_sha") != TASK301_PREVIOUS_SHA
                or ack.get("approved_by") != approved_by
                or ack.get("approval_reference") != approval_reference
                or ack.get("decision") != "pre_shutdown_recovery_verified"
            ):
                raise AdminError("existing recovery acknowledgment is inconsistent")
            _parse_utc(ack.get("approved_at_utc"))
            return journal_sha, hashlib.sha256(ack_raw).hexdigest()

        observed = _task301_pre_shutdown_evidence(
            journal,
            base=base,
            database=database,
            audit_dir=audit_dir,
            backup=backup,
            historical_preflight=historical_preflight,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            service_uid=service_uid,
            service_gid=service_gid,
            state_reader=state_reader,
            process_reader=process_reader,
            snapshot_verifier=snapshot_verifier,
            locked_directory_fd=directory_fd,
        )
        try:
            os.stat(ack_name, dir_fd=directory_fd, follow_symlinks=False)
            ack_exists = True
        except FileNotFoundError:
            ack_exists = False
        if ack_exists:
            ack_raw, _ = _read_at(directory_fd, ack_name, owner_uid=owner_uid, mode=0o600)
            ack = _json_object(ack_raw)
            if (
                set(ack) != PRE_SHUTDOWN_ACK_FIELDS
                or ack.get("schema_version") != 3
                or ack.get("journal_sha256") != journal_sha
                or ack.get("approved_release_sha") != TASK301_RELEASE_SHA
                or ack.get("previous_release_sha") != TASK301_PREVIOUS_SHA
                or ack.get("approved_by") != approved_by
                or ack.get("approval_reference") != approval_reference
                or ack.get("decision") != "pre_shutdown_recovery_verified"
                or ack.get("evidence") != observed
            ):
                raise AdminError("existing recovery acknowledgment differs from fresh evidence")
            _parse_utc(ack.get("approved_at_utc"))
        else:
            approved_at = clock().astimezone(UTC).isoformat()
            ack = {
                "schema_version": 3,
                "journal_sha256": journal_sha,
                "approved_release_sha": TASK301_RELEASE_SHA,
                "previous_release_sha": TASK301_PREVIOUS_SHA,
                "decision": "pre_shutdown_recovery_verified",
                "approved_by": approved_by,
                "approval_reference": approval_reference,
                "approved_at_utc": approved_at,
                "evidence": observed,
            }
            ack_raw = (json.dumps(ack, sort_keys=True, separators=(",", ":")) + "\n").encode()
        try:
            os.stat(archive_name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            os.link(
                "deployment-state.json",
                archive_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            os.fsync(directory_fd)
        archived_raw, _ = _read_at(
            directory_fd,
            archive_name,
            owner_uid=owner_uid,
            mode=0o600,
            expected_nlink=2 if active_present else 1,
        )
        if archived_raw != journal_raw:
            raise AdminError("archived journal bytes do not match original")
        fresh_observed = _task301_pre_shutdown_evidence(
            journal,
            base=base,
            database=database,
            audit_dir=audit_dir,
            backup=backup,
            historical_preflight=historical_preflight,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            service_uid=service_uid,
            service_gid=service_gid,
            state_reader=state_reader,
            process_reader=process_reader,
            snapshot_verifier=snapshot_verifier,
            locked_directory_fd=directory_fd,
        )
        if fresh_observed != observed:
            raise AdminError("recovery evidence changed before acknowledgment")
        if not ack_exists:
            _write_new_at(
                directory_fd,
                ack_name,
                ack_raw,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
                mode=0o600,
            )
        final_observed = _task301_pre_shutdown_evidence(
            journal,
            base=base,
            database=database,
            audit_dir=audit_dir,
            backup=backup,
            historical_preflight=historical_preflight,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            service_uid=service_uid,
            service_gid=service_gid,
            state_reader=state_reader,
            process_reader=process_reader,
            snapshot_verifier=snapshot_verifier,
            locked_directory_fd=directory_fd,
        )
        if final_observed != observed:
            raise AdminError("recovery evidence changed before journal archival")
        if active_present:
            os.unlink("deployment-state.json", dir_fd=directory_fd)
        os.fsync(directory_fd)
        history_verifier(audit_dir, owner_uid, owner_gid)
        return journal_sha, hashlib.sha256(ack_raw).hexdigest()
    finally:
        os.close(directory_fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pin = commands.add_parser("provision-pin")
    pin.add_argument("--release-sha", required=True)
    pin.add_argument("--previous-sha", required=True)
    pin.add_argument("--installer-sha256", required=True)
    rotate_pin_parser = commands.add_parser("rotate-pin")
    rotate_pin_parser.add_argument("--expected-current-pin-sha256", required=True)
    rotate_pin_parser.add_argument("--expected-current-release-sha", required=True)
    rotate_pin_parser.add_argument("--expected-previous-sha", required=True)
    rotate_pin_parser.add_argument("--release-sha", required=True)
    rotate_pin_parser.add_argument("--previous-sha", required=True)
    rotate_pin_parser.add_argument("--installer-sha256", required=True)
    rotate_pin_parser.add_argument("--approved-by", required=True)
    rotate_pin_parser.add_argument("--approval-reference", required=True)
    ack = commands.add_parser("acknowledge-recovery")
    ack.add_argument("--journal-sha256", required=True)
    ack.add_argument("--review-record", type=Path, required=True)
    ack.add_argument("--approved-by", required=True)
    ack.add_argument("--approval-reference", required=True)
    ack_pre_shutdown = commands.add_parser("acknowledge-pre-shutdown-recovery")
    ack_pre_shutdown.add_argument("--journal-sha256", required=True)
    ack_pre_shutdown.add_argument("--approved-by", required=True)
    ack_pre_shutdown.add_argument("--approval-reference", required=True)
    ack_addendum = commands.add_parser("acknowledge-recovery-addendum")
    ack_addendum.add_argument("--journal-sha256", required=True)
    ack_addendum.add_argument("--review-record", type=Path, required=True)
    ack_addendum.add_argument("--addendum-sha256", required=True)
    ack_addendum.add_argument("--approved-by", required=True)
    ack_addendum.add_argument("--approval-reference", required=True)
    finalize = commands.add_parser("finalize-deployment")
    finalize.add_argument("--journal-sha256", required=True)
    finalize.add_argument("--approved-by", required=True)
    finalize.add_argument("--approval-reference", required=True)
    activate = commands.add_parser("activate-services")
    activate.add_argument("--journal-sha256", required=True)
    activate.add_argument("--approved-by", required=True)
    activate.add_argument("--approval-reference", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    if os.geteuid() != 0:
        print("Armory administrative operation requires root.", file=sys.stderr)
        return 1
    args = _parser().parse_args(argv)
    os.umask(0o077)
    try:
        if args.command == "provision-pin":
            provision_pin(
                PIN_PATH,
                release_sha=args.release_sha,
                previous_sha=args.previous_sha,
                installer_sha256=args.installer_sha256,
                owner_uid=0,
                owner_gid=0,
                parent_mode=0o750,
            )
            print("Deployment pin created and verified.")
            return 0
        if args.command == "rotate-pin":
            old_hash, new_hash = rotate_pin(
                PIN_PATH,
                history_dir=PIN_HISTORY_DIR,
                expected_current_pin_sha256=args.expected_current_pin_sha256,
                expected_current_release_sha=args.expected_current_release_sha,
                expected_previous_sha=args.expected_previous_sha,
                release_sha=args.release_sha,
                previous_sha=args.previous_sha,
                installer_sha256=args.installer_sha256,
                approved_by=args.approved_by,
                approval_reference=args.approval_reference,
                owner_uid=0,
                owner_gid=0,
                parent_mode=0o750,
            )
            print(
                "Deployment pin rotated with immutable hash-bound history; "
                f"previous_pin_sha256={old_hash}; current_pin_sha256={new_hash}"
            )
            return 0
        if args.command == "acknowledge-recovery":
            journal_sha, manifest_sha = acknowledge_incident(
                AUDIT_DIR,
                args.review_record,
                expected_journal_sha256=args.journal_sha256,
                approved_by=args.approved_by,
                approval_reference=args.approval_reference,
                owner_uid=0,
                owner_gid=0,
            )
            print(
                "Incident archived and acknowledgment written; "
                f"journal_sha256={journal_sha}; manifest_sha256={manifest_sha}"
            )
            return 0
        if args.command == "acknowledge-pre-shutdown-recovery":
            journal_sha, ack_sha = acknowledge_task301_pre_shutdown(
                AUDIT_DIR,
                expected_journal_sha256=args.journal_sha256,
                approved_by=args.approved_by,
                approval_reference=args.approval_reference,
                owner_uid=0,
                owner_gid=0,
            )
            print(
                "Pre-shutdown incident archived and acknowledged; "
                f"journal_sha256={journal_sha}; acknowledgment_sha256={ack_sha}"
            )
            return 0
        if args.command == "acknowledge-recovery-addendum":
            journal_sha, manifest_sha = acknowledge_incident(
                AUDIT_DIR,
                args.review_record,
                expected_journal_sha256=args.journal_sha256,
                approved_by=args.approved_by,
                approval_reference=args.approval_reference,
                recovery_addendum_sha256=args.addendum_sha256,
                owner_uid=0,
                owner_gid=0,
            )
            print(
                "Incident archived with verified recovery addendum; "
                f"journal_sha256={journal_sha}; manifest_sha256={manifest_sha}"
            )
            return 0
        if args.command == "finalize-deployment":
            journal_sha, manifest_sha = finalize_deployment(
                AUDIT_DIR,
                expected_journal_sha256=args.journal_sha256,
                approved_by=args.approved_by,
                approval_reference=args.approval_reference,
                owner_uid=0,
                owner_gid=0,
            )
            print(
                "Deployment administratively finalized; "
                f"journal_sha256={journal_sha}; manifest_sha256={manifest_sha}"
            )
            return 0
        if args.command == "activate-services":
            activate_services(
                AUDIT_DIR,
                expected_journal_sha256=args.journal_sha256,
                approved_by=args.approved_by,
                approval_reference=args.approval_reference,
                owner_uid=0,
                owner_gid=0,
            )
            print("Approved service states restored and verified.")
            return 0
    except AdminError as exc:
        print(f"Armory administrative operation refused: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
