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
AUDIT_DIR = Path("/var/lib/armory-deployment-audit")
MAX_JSON_BYTES = 65_536
MAX_SOURCE_BYTES = 1_048_576
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
UNIT_REQUIREMENTS = {
    "wow-forever-armory-bot.service": ("inactive", "disabled"),
    "wow-forever-armory-backup.timer": ("inactive", "disabled"),
}
BACKUP_SERVICE = "wow-forever-armory-backup.service"


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


def _open_private_directory(
    directory: Path, *, owner_uid: int, exact_mode: int
) -> int:
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
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
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
    if not name or name in {".", ".."} or "/" in name:
        raise AdminError("administrative filename is invalid")
    try:
        before_entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        fd = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0),
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
        raw, _ = _read_at(
            directory_fd, path.name, owner_uid=owner_uid, mode=mode
        )
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
        temp_entry = os.stat(
            temporary, dir_fd=directory_fd, follow_symlinks=False
        )
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
        linked_temp = os.stat(
            temporary, dir_fd=directory_fd, follow_symlinks=False
        )
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
            current_entry = os.stat(
                name, dir_fd=directory_fd, follow_symlinks=False
            )
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
        or parent_info.st_mode & 0o222
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
    if info.st_mode & 0o222:
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
    if (
        backup.parent != Path("/var/lib/wow-forever-armory-bot/backups")
        or not backup.name.startswith("guild-armory-")
        or backup.suffix != ".sqlite3"
        or saved_preflight != audit_dir / f"armory-db-preflight.{previous_sha}.py"
    ):
        raise AdminError("incident artifact paths are invalid")
    for field in ("database_backup_sha256", "shared_preflight_sha256"):
        if not isinstance(journal.get(field), str) or re.fullmatch(
            r"[0-9a-f]{64}", journal[field]
        ) is None:
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
    if not isinstance(checks, dict) or set(checks) != RECOVERY_CHECKS or any(
        result != "PASS" for result in checks.values()
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
    if not isinstance(release_target, str) or release_target not in {
        f"/opt/wow-forever-armory-bot/releases/{release_sha}",
        f"/opt/wow-forever-armory-bot/releases/{previous_sha}",
    } or observations.get("installation_marker") != Path(release_target).name:
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
        or re.fullmatch(
            r"[0-9a-f]{64}", observations["saved_preflight_sha256"]
        )
        is None
    ):
        raise AdminError("backup or preflight review hash is invalid")
    observed_matches = (
        observations.get("database_signature") == journal.get("database_signature")
        and observations.get("backup_path") == journal.get("database_backup")
        and observations.get("backup_sha256")
        == journal.get("database_backup_sha256")
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
) -> str:
    directory_fd = _open_private_directory(
        path.parent, owner_uid=owner_uid, exact_mode=parent_mode
    )
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
                | getattr(os, "O_CLOEXEC", 0),
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
            entry_after = os.stat(
                path.name, dir_fd=directory_fd, follow_symlinks=False
            )
            if (
                _signature(before) != _signature(after)
                or _signature(after) != _signature(entry_after)
            ):
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


def _verify_release_tree(
    path: Path,
    *,
    service_uid: int,
    service_gid: int,
    owner_uid: int = 0,
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
                    raise AdminError("recovery release tree contains a symbolic link")
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
        final_info = path.lstat()
        if (final_info.st_dev, final_info.st_ino) != root_identity:
            raise AdminError("recovery release directory changed during inspection")
    except OSError as exc:
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
    database: Path = Path(
        "/var/lib/wow-forever-armory-bot/guild-armory.sqlite3"
    ),
    service_uid: int | None = None,
    service_gid: int | None = None,
    owner_uid: int = 0,
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
        review_name = review_record_path.name
        if review_name != f"recovery-review-{journal_sha256}.json":
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
        )
        evidence_verifier(journal, release_sha, previous_sha, audit_dir, review)

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
        _require_recovery_unit_states(state_reader())

        ack = {
            "schema_version": 1,
            "journal_sha256": journal_sha256,
            "manifest_sha256": manifest_sha256,
            "approved_release_sha": release_sha,
            "previous_release_sha": previous_sha,
            "decision": "manual_recovery_verified",
            "approved_by": approved_by,
            "approval_reference": approval_reference,
            "approved_at_utc": review["approved_at_utc"],
        }
        ack_raw = (
            json.dumps(ack, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("ascii")

        def checkpoint_at(name: str) -> None:
            if checkpoint is not None:
                checkpoint(name)

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
            _require_recovery_unit_states(state_reader())
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
        _require_recovery_unit_states(state_reader())

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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pin = commands.add_parser("provision-pin")
    pin.add_argument("--release-sha", required=True)
    pin.add_argument("--previous-sha", required=True)
    pin.add_argument("--installer-sha256", required=True)
    ack = commands.add_parser("acknowledge-recovery")
    ack.add_argument("--journal-sha256", required=True)
    ack.add_argument("--review-record", type=Path, required=True)
    ack.add_argument("--approved-by", required=True)
    ack.add_argument("--approval-reference", required=True)
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
    except AdminError as exc:
        print(f"Armory administrative operation refused: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
