#!/usr/bin/env python3
"""Shared fail-closed validation for retained deployment incident evidence."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pwd
import re
import stat
from datetime import UTC, datetime
from pathlib import Path


class AuditError(ValueError):
    """Audit evidence is absent, inconsistent, unsafe, or changed while read."""


ARCHIVE = re.compile(r"deployment-state-([0-9a-f]{64})\.json\Z")
ACK = re.compile(r"deployment-recovery-ack-([0-9a-f]{64})\.json\Z")
MANIFEST = re.compile(r"deployment-([0-9a-f]{40})\.json\Z")
PREFLIGHT_COPY = re.compile(r"armory-db-preflight\.([0-9a-f]{40})\.py\Z")
REVIEW = re.compile(
    r"recovery-review-([0-9a-f]{64})(?:-([0-9a-f]{64}))?\.json\Z"
)
INSTALLATION_RESULT = re.compile(r"deployment-installation-([0-9a-f]{32})\.json\Z")
SERVICE_ACTIVATION = re.compile(
    r"service-activation-([0-9a-f]{64})-([0-9a-f]{32})-([0-9]{4})\.json\Z"
)
LEGACY_SERVICE_ACTIVATION = re.compile(
    r"service-activation-([0-9a-f]{64})-([0-9a-f]{32})\.json\Z"
)
PRESERVED_HISTORICAL_AUDIT_DIRECTORIES = frozenset(
    {"reviewed-v2.0.0", "archive"}
)
REVIEWED_HISTORY_FILE_MODES = {
    "production_db_snapshot.py": 0o400,
    "production_admin.py": 0o400,
}


def _historical_root_mode(name: str) -> int:
    if name == "reviewed-v2.0.0":
        return 0o500
    if name == "archive":
        return 0o700
    raise AuditError("historical audit directory name is not approved")


def _historical_file_mode(directory_name: str, relative: str, mode: int) -> None:
    """Accept only the observed read-only historical file classes."""
    if mode & 0o111:
        raise AuditError("historical audit executable file is not approved")
    if mode & 0o022:
        raise AuditError("historical audit file is group- or world-writable")
    if directory_name == "reviewed-v2.0.0":
        filename = relative.removeprefix("reviewed-v2.0.0/")
        expected = REVIEWED_HISTORY_FILE_MODES.get(filename)
        if expected is None or relative.count("/") != 1 or mode != expected:
            raise AuditError("reviewed history file name or read-only mode is not approved")
        return
    if not relative.endswith(".json") or mode != 0o600:
        raise AuditError("historical archive accepts only read-only JSON files")
RECOVERY_ADDENDUM = re.compile(r"recovery-addendum-([0-9a-f]{64})\.json\Z")
TASK267_JOURNAL_SHA256 = "f16dd8394724b6e15797d9ac7a7f6f39241cb58dadf86b05347e2372b874e6cb"
TASK267_MANIFEST_SHA256 = "9628fb0b363b724d9e88349fef9d100aa75297d16bf663a26263e793ac32d116"
TASK267_REPORT_SHA256 = "8ffae1176165b46c0e06733ac9d34a937ee033a0d6886a3bbcab7575b7cef96a"
TASK267_ATTESTATION_SHA256 = "dc82c79862053f64f460e41cc18bca6a90308545d4a4ac56781c09628dde09c4"
TASK267_REVIEW_SHA256 = "784a8759817045c2c9b59545022e43c4cf23ae7772d26f37f61ebcb69e0c308f"
TASK267_INCIDENT_BACKUP_SHA256 = "e15bd8d7434246f71b4580437c63bd2f79b572c6d104b05b6e2c28f3d05345c9"
TASK267_LATEST_BACKUP_SHA256 = "bc87d4f26b213fdca1466fffbd6ec5b9c707f6c2fdaa71927574f365b494cbc0"
TASK267_PREFLIGHT_SHA256 = "c1c4f067fa2b465726d374ecb06acfae82a1ec234dc0f38c4734a6c422089803"
TASK267_ADMIN_UID = 0
TASK267_ADMIN_GID = 0
TASK267_REPORT_PATH = Path("/var/lib/armory-deployment-audit/task267-verification-8ffae1176165b46c0e06733ac9d34a937ee033a0d6886a3bbcab7575b7cef96a.json")
TASK267_DATABASE_PATH = Path("/var/lib/wow-forever-armory-bot/guild-armory.sqlite3")
TASK267_INCIDENT_BACKUP_PATH = Path("/var/lib/wow-forever-armory-bot/backups/guild-armory-20261114T203738.735054Z.sqlite3")
TASK267_LATEST_BACKUP_PATH = Path("/var/lib/wow-forever-armory-bot/backups/guild-armory-20261115T001002.517528Z.sqlite3")
TASK267_PREFLIGHT_PATH = Path("/var/lib/armory-deployment-audit/armory-db-preflight.dcceeb85464e09e34b112e41b16a833226c72270.py")
TASK267_ATTESTATION_PATH = Path(f"/var/lib/armory-deployment-audit/task262-attestation-{TASK267_ATTESTATION_SHA256}.json")
TASK267_REVIEW_PATH = Path(f"/var/lib/armory-deployment-audit/task262-review-{TASK267_REVIEW_SHA256}.json")
TASK267_ATTESTATION_FILE = re.compile(r"task262-attestation-[0-9a-f]{64}\.json\Z")
TASK267_REVIEW_FILE = re.compile(r"task262-review-[0-9a-f]{64}\.json\Z")
TASK267_REPORT_FILE = re.compile(r"task267-verification-[0-9a-f]{64}\.json\Z")
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
ACK_ADDENDUM_FIELDS = ACK_FIELDS | {"recovery_addendum_sha256"}
ADDENDUM_FIELDS = {
    "schema_version", "decision", "journal_sha256", "manifest_sha256",
    "verification_report_sha256", "historical_attestation_sha256",
    "historical_review_sha256", "historical_database_signature",
    "verified_database_signature", "database_sha256", "backup_sha256",
    "latest_backup_sha256", "saved_preflight_sha256", "historical_eagain",
    "recovery_release_sha", "recovery_review_sha256", "approved_by",
    "approval_reference", "approved_at_utc",
}
MANIFEST_FIELDS = {
    "approved_release_sha",
    "previous_release_sha",
    "database_backup",
    "database_backup_sha256",
    "shared_preflight_backup",
    "shared_preflight_sha256",
    "created_at_utc",
}


def _identity(info: os.stat_result) -> tuple[int, ...]:
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


def _hash_pinned_path(
    path: Path,
    *,
    owner: int,
    group: int,
    mode: int,
    parent_owner: int,
    parent_mode: int | None,
    maximum: int = 1 << 40,
) -> tuple[str, os.stat_result]:
    """Hash one regular file without following links or changing atime."""
    if not hasattr(os, "O_NOATIME"):
        raise AuditError("filesystem cannot guarantee atime-safe hashing")
    try:
        parent = path.parent
        if parent.resolve(strict=True) != parent:
            raise AuditError("pinned evidence directory path is not canonical")
        parent_info = parent.lstat()
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != parent_owner
            or parent_info.st_mode & 0o022
            or (parent_mode is not None and stat.S_IMODE(parent_info.st_mode) != parent_mode)
        ):
            raise AuditError("pinned evidence directory metadata is unsafe")
        directory_fd = os.open(
            parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NOATIME", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            opened_parent = os.fstat(directory_fd)
            if (opened_parent.st_dev, opened_parent.st_ino) != (
                parent_info.st_dev,
                parent_info.st_ino,
            ):
                raise AuditError("pinned evidence directory changed")
            entry = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(entry.st_mode)
                or entry.st_uid != owner
                or entry.st_gid != group
                or stat.S_IMODE(entry.st_mode) != mode
                or entry.st_nlink != 1
                or entry.st_size > maximum
            ):
                raise AuditError("pinned evidence file metadata is unsafe")
            fd = os.open(
                path.name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NOATIME", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_fd,
            )
            try:
                before = os.fstat(fd)
                if _identity(before) != _identity(entry):
                    raise AuditError("pinned evidence changed while opening")
                digest = hashlib.sha256()
                total = 0
                while True:
                    block = os.read(fd, 1_048_576)
                    if not block:
                        break
                    total += len(block)
                    if total > maximum:
                        raise AuditError("pinned evidence exceeds its size limit")
                    digest.update(block)
                after = os.fstat(fd)
                after_entry = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
                after_parent = parent.lstat()
                if (
                    _identity(before) != _identity(after)
                    or _identity(after) != _identity(after_entry)
                    or (opened_parent.st_dev, opened_parent.st_ino)
                    != (after_parent.st_dev, after_parent.st_ino)
                    or total != after.st_size
                ):
                    raise AuditError("pinned evidence changed while hashing")
                return digest.hexdigest(), after
            finally:
                os.close(fd)
        finally:
            os.close(directory_fd)
    except AuditError:
        raise
    except OSError as exc:
        raise AuditError("pinned evidence cannot be read safely") from exc


def _verify_task267_live_files(
    addendum: dict, *, normal_operation: bool = False
) -> None:
    """Revalidate pinned evidence and enforce its current lifecycle state."""
    try:
        service_uid = pwd.getpwnam("wow-armory").pw_uid
        import grp

        service_gid = grp.getgrnam("wow-armory").gr_gid
    except KeyError as exc:
        raise AuditError("pinned evidence account is unavailable") from exc
    report_hash, _ = _hash_pinned_path(
        TASK267_REPORT_PATH,
        owner=TASK267_ADMIN_UID,
        group=TASK267_ADMIN_GID,
        mode=0o600,
        parent_owner=TASK267_ADMIN_UID,
        parent_mode=0o700,
        maximum=2_000_000,
    )
    if report_hash != TASK267_REPORT_SHA256:
        raise AuditError("fresh verification report is missing or changed")
    static_files = (
        (TASK267_PREFLIGHT_PATH, TASK267_PREFLIGHT_SHA256, True),
        (TASK267_ATTESTATION_PATH, TASK267_ATTESTATION_SHA256, False),
        (TASK267_REVIEW_PATH, TASK267_REVIEW_SHA256, False),
    )
    for path, expected, executable in static_files:
        actual, _ = _hash_pinned_path(
            path,
            owner=TASK267_ADMIN_UID,
            group=TASK267_ADMIN_GID,
            mode=0o700 if executable else 0o600,
            parent_owner=TASK267_ADMIN_UID,
            parent_mode=0o700,
            maximum=2_000_000,
        )
        if actual != expected:
            raise AuditError("pinned historical recovery evidence is missing or changed")
    if normal_operation:
        _verify_task267_normal_database_metadata(
            service_uid=service_uid,
            service_gid=service_gid,
            approved_signature=addendum.get("verified_database_signature"),
        )
    else:
        database_hash, database_info = _hash_pinned_path(
            TASK267_DATABASE_PATH,
            owner=service_uid,
            group=service_gid,
            mode=0o600,
            parent_owner=service_uid,
            parent_mode=0o700,
        )
        database_signature = [
            database_info.st_dev,
            database_info.st_ino,
            database_info.st_size,
            database_info.st_mtime_ns,
            database_info.st_ctime_ns,
            database_info.st_uid,
            database_info.st_gid,
            database_info.st_mode,
        ]
        if (
            database_hash != addendum.get("database_sha256")
            or database_signature != addendum.get("verified_database_signature")
        ):
            raise AuditError("current database is missing or changed")
        if _task267_database_handle_count(TASK267_DATABASE_PATH) != 0:
            raise AuditError("a process has the recovery database open")
        for suffix in ("-wal", "-shm", "-journal", ".restore-in-progress"):
            try:
                Path(f"{TASK267_DATABASE_PATH}{suffix}").lstat()
            except FileNotFoundError:
                continue
            raise AuditError("database sidecar or restore marker is present")
    for path, expected in (
        (TASK267_INCIDENT_BACKUP_PATH, TASK267_INCIDENT_BACKUP_SHA256),
        (TASK267_LATEST_BACKUP_PATH, TASK267_LATEST_BACKUP_SHA256),
    ):
        actual, _ = _hash_pinned_path(
            path,
            owner=service_uid,
            group=service_gid,
            mode=0o600,
            parent_owner=service_uid,
            parent_mode=0o700,
            maximum=1 << 40,
        )
        if actual != expected:
            raise AuditError("pinned recovery backup is missing or changed")


def _verify_task267_normal_database_metadata(
    *, service_uid: int, service_gid: int, approved_signature: object
) -> None:
    """Allow ordinary SQLite writes after a verified transition, never restore."""
    database = TASK267_DATABASE_PATH
    if database.parent.resolve(strict=True) != database.parent:
        raise AuditError("normal-operation database path is not canonical")
    parent = database.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != service_uid
        or parent.st_gid != service_gid
        or stat.S_IMODE(parent.st_mode) != 0o700
    ):
        raise AuditError("normal-operation database directory metadata is unsafe")
    before = database.lstat()
    if (
        not isinstance(approved_signature, list)
        or len(approved_signature) != 8
        or any(type(value) is not int or value < 0 for value in approved_signature)
    ):
        raise AuditError("approved normal-operation database identity is invalid")
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != service_uid
        or before.st_gid != service_gid
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_nlink != 1
        or before.st_size <= 0
        or (before.st_dev, before.st_ino) != tuple(approved_signature[:2])
        or (before.st_uid, before.st_gid, before.st_mode)
        != tuple(approved_signature[5:8])
    ):
        raise AuditError("normal-operation database metadata is unsafe")
    def sidecar_snapshot() -> tuple[tuple[str, tuple[int, ...]], ...]:
        present = []
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{database}{suffix}")
            try:
                side = sidecar.lstat()
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(side.st_mode)
                or side.st_uid != service_uid
                or side.st_gid != service_gid
                or stat.S_IMODE(side.st_mode) != 0o600
                or side.st_nlink != 1
            ):
                raise AuditError("normal-operation database sidecar is unsafe")
            present.append(
                (
                    suffix,
                    (
                        side.st_dev,
                        side.st_ino,
                        side.st_uid,
                        side.st_gid,
                        side.st_mode,
                        side.st_nlink,
                    ),
                )
            )
        return tuple(present)

    def require_no_restore_artifacts() -> None:
        for suffix in ("-journal", ".restore-in-progress"):
            try:
                Path(f"{database}{suffix}").lstat()
            except FileNotFoundError:
                continue
            raise AuditError("database rollback or restore state blocks startup")

    sidecars_before = sidecar_snapshot()
    require_no_restore_artifacts()
    after = database.lstat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_uid,
        before.st_gid,
        before.st_mode,
        before.st_nlink,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_uid,
        after.st_gid,
        after.st_mode,
        after.st_nlink,
    ):
        raise AuditError("normal-operation database identity changed during check")
    require_no_restore_artifacts()
    if sidecar_snapshot() != sidecars_before:
        raise AuditError("normal-operation database sidecars changed during check")


def _validate_lifecycle_transition(
    event: dict,
    *,
    journal_sha256: str,
    acknowledgment_raw: bytes,
    acknowledgment: dict,
    addendum: dict | None,
) -> None:
    """Validate the explicit, hash-bound RECOVERY_APPROVED -> NORMAL event."""
    transition = event.get("lifecycle_transition")
    expected = {
        "schema_version": 1,
        "from_state": "RECOVERY_APPROVED",
        "to_state": "NORMAL_OPERATION",
        "journal_sha256": journal_sha256,
        "acknowledgment_sha256": hashlib.sha256(acknowledgment_raw).hexdigest(),
        "addendum_sha256": acknowledgment.get("recovery_addendum_sha256"),
        "verification_report_sha256": (
            addendum.get("verification_report_sha256") if addendum else None
        ),
        "database_sha256": addendum.get("database_sha256") if addendum else None,
        "recovery_approved_by": acknowledgment.get("approved_by"),
        "recovery_approval_reference": acknowledgment.get("approval_reference"),
        "activation_approved_by": event.get("approved_by"),
        "activation_approval_reference": event.get("approval_reference"),
        "activation_id": event.get("activation_id"),
        "attempt_number": event.get("attempt_number"),
        "release_sha": addendum.get("recovery_release_sha") if addendum else None,
    }
    if (
        type(event.get("schema_version")) is not int
        or event.get("schema_version") != 2
        or event.get("event") != "success"
        or acknowledgment.get("schema_version") != 2
        or addendum is None
        or not isinstance(transition, dict)
        or set(transition) != set(expected)
        or transition != expected
    ):
        raise AuditError("normal-operation lifecycle transition is invalid")


def _task267_database_handle_count(database: Path) -> int:
    """Count matching procfs descriptors; incomplete visibility is a NO-GO."""
    if not hasattr(os, "O_NOATIME"):
        raise AuditError("procfs access cannot guarantee atime-safe reads")
    try:
        target = database.stat()
        proc_fd = os.open(
            "/proc",
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NOATIME", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            pids = os.listdir(proc_fd)
        finally:
            os.close(proc_fd)
    except OSError as exc:
        raise AuditError("procfs process inventory is unavailable") from exc
    count = 0
    for pid in pids:
        if not pid.isdecimal():
            continue
        try:
            process_fd = os.open(
                f"/proc/{pid}/fd",
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NOATIME", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise AuditError("procfs descriptor inventory is incomplete") from exc
        try:
            for descriptor in os.listdir(process_fd):
                try:
                    info = os.stat(descriptor, dir_fd=process_fd, follow_symlinks=True)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise AuditError("procfs descriptor identity is unreadable") from exc
                if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
                    count += 1
        finally:
            os.close(process_fd)
    return count


def _valid_activation_action(action: object) -> bool:
    return (
        isinstance(action, dict)
        and set(action) == {"unit", "verb", "target"}
        and action.get("unit")
        in {"wow-forever-armory-bot.service", "wow-forever-armory-backup.timer"}
        and action.get("verb") in {"enable", "disable", "start", "stop"}
        and isinstance(action.get("target"), list)
        and len(action["target"]) == 2
        and action["target"][0] in {"active", "inactive"}
        and action["target"][1] in {"enabled", "disabled"}
    )


def _expected_activation_actions(intended: dict) -> list[dict]:
    result = []
    for unit in ("wow-forever-armory-bot.service", "wow-forever-armory-backup.timer"):
        target = intended.get(unit)
        if not isinstance(target, list) or len(target) != 2:
            raise AuditError("saved activation states are invalid")
        result.extend(
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
    return result


def _valid_utc(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        return False
    return timestamp.tzinfo is not None and timestamp.utcoffset() == UTC.utcoffset(
        timestamp
    )


def _validate_task267_addendum(
    raw: bytes,
    record: dict,
    *,
    journal_sha256: str,
    manifest_sha256: str,
    approved_release_sha: str,
    previous_release_sha: str,
    journal_database_signature: object,
) -> None:
    """Validate the narrowly pinned timestamp-only incident addendum."""
    if (
        set(record) != ADDENDUM_FIELDS
        or type(record.get("schema_version")) is not int
        or record.get("schema_version") != 2
    ):
        raise AuditError("recovery addendum schema is invalid")
    digests = {
        "journal_sha256": TASK267_JOURNAL_SHA256,
        "manifest_sha256": TASK267_MANIFEST_SHA256,
        "verification_report_sha256": TASK267_REPORT_SHA256,
        "historical_attestation_sha256": TASK267_ATTESTATION_SHA256,
        "historical_review_sha256": TASK267_REVIEW_SHA256,
        "backup_sha256": TASK267_INCIDENT_BACKUP_SHA256,
        "latest_backup_sha256": TASK267_LATEST_BACKUP_SHA256,
        "saved_preflight_sha256": TASK267_PREFLIGHT_SHA256,
    }
    if (
        journal_sha256 != TASK267_JOURNAL_SHA256
        or manifest_sha256 != TASK267_MANIFEST_SHA256
        or record.get("decision") != "preserve_current_database_after_fresh_integrity"
        or any(record.get(key) != value for key, value in digests.items())
        or record.get("historical_eagain") != "unresolved_no_retry"
        or not isinstance(record.get("approved_by"), str)
        or not record["approved_by"].strip()
        or len(record["approved_by"]) > 128
        or not isinstance(record.get("approval_reference"), str)
        or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}",
            record["approval_reference"],
        ) is None
        or not _valid_utc(record.get("approved_at_utc"))
    ):
        raise AuditError("recovery addendum bindings or approval are invalid")
    historical = record.get("historical_database_signature")
    verified = record.get("verified_database_signature")
    if (
        not isinstance(historical, list)
        or len(historical) != 8
        or any(type(value) is not int or value < 0 for value in historical)
        or not isinstance(verified, list)
        or len(verified) != 8
        or any(type(value) is not int or value < 0 for value in verified)
        or any(historical[index] != verified[index] for index in (0, 1, 2, 5, 6, 7))
        or historical != journal_database_signature
        or historical[3:5] == verified[3:5]
        or not isinstance(record.get("database_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", record["database_sha256"]) is None
    ):
        raise AuditError("recovery addendum does not prove timestamp-only drift")
    # These incident hashes map to one approved release pair. The archive
    # validator still verifies the actual journal and manifest bytes below.
    if (
        not re.fullmatch(r"[0-9a-f]{40}", approved_release_sha)
        or not re.fullmatch(r"[0-9a-f]{40}", previous_release_sha)
        or not isinstance(record.get("recovery_release_sha"), str)
        or record.get("recovery_release_sha")
        not in {approved_release_sha, previous_release_sha}
        or not isinstance(record.get("recovery_review_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", record["recovery_review_sha256"])
        is None
    ):
        raise AuditError("recovery addendum release identity is invalid")


def _verify_legacy_activation_events(
    directory_fd: int,
    events_by_journal: dict[str, list[tuple[str, str]]],
    archives: dict[str, str],
    *,
    owner: int,
    group: int,
) -> None:
    """Continue to validate historical terminal activation reports."""
    for journal_sha, events in events_by_journal.items():
        if journal_sha not in archives:
            raise AuditError("legacy activation report has no archived deployment")
        archive_raw, archived_journal = _read_json(
            directory_fd, archives[journal_sha], owner=owner, group=group
        )
        if hashlib.sha256(archive_raw).hexdigest() != journal_sha:
            raise AuditError("legacy activation journal hash is invalid")
        expected_states = {
            "wow-forever-armory-bot.service": archived_journal.get("service_state"),
            "wow-forever-armory-backup.timer": archived_journal.get(
                "backup_timer_state"
            ),
        }
        for attempt_id, name in events:
            _, event = _read_json(directory_fd, name, owner=owner, group=group)
            if (
                set(event)
                != {
                    "schema_version",
                    "journal_sha256",
                    "attempt_id",
                    "status",
                    "approved_by",
                    "approval_reference",
                    "intended_states",
                    "observed_states",
                    "failure_class",
                    "recorded_at_utc",
                }
                or event.get("schema_version") != 1
                or event.get("journal_sha256") != journal_sha
                or event.get("attempt_id") != attempt_id
                or event.get("status") not in {"success", "failed"}
                or event.get("intended_states") != expected_states
                or not isinstance(event.get("approved_by"), str)
                or not event["approved_by"].strip()
                or not isinstance(event.get("approval_reference"), str)
                or re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}",
                    event["approval_reference"],
                )
                is None
                or not isinstance(event.get("observed_states"), dict)
                or not isinstance(event.get("recorded_at_utc"), str)
            ):
                raise AuditError("legacy activation report is invalid")


def _read_json(directory_fd: int, name: str, *, owner: int, group: int):
    if not hasattr(os, "O_NOATIME"):
        raise AuditError("filesystem cannot guarantee atime-safe audit reads")
    before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != owner
        or before.st_gid != group
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_nlink != 1
        or before.st_size > 65536
    ):
        raise AuditError("unsafe audit file metadata")
    fd = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOATIME", 0),
        dir_fd=directory_fd,
    )
    try:
        opened = os.fstat(fd)
        if _identity(before) != _identity(opened):
            raise AuditError("audit file changed while opening")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(8192, 65537 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > 65536:
                raise AuditError("audit file exceeds size limit")
        raw = b"".join(chunks)
        after_fd = os.fstat(fd)
        after_name = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            _identity(opened) != _identity(after_fd)
            or _identity(after_fd) != _identity(after_name)
            or len(raw) != after_fd.st_size
        ):
            raise AuditError("audit file changed while reading")
    finally:
        os.close(fd)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise AuditError("duplicate audit JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AuditError("audit JSON is malformed") from exc
    if not isinstance(value, dict):
        raise AuditError("audit JSON is not an object")
    return raw, value


def _audit_snapshot(
    fd: int,
) -> tuple[tuple[str, ...], tuple[tuple[str, tuple[int, ...]], ...]]:
    names = tuple(sorted(os.listdir(fd)))
    metadata = []
    for name in names:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        metadata.append((name, _identity(info)))
    return names, tuple(metadata)


def _historical_directory_snapshot(
    parent_fd: int,
    directory_name: str,
    *,
    owner: int,
    group: int,
    root_device: int,
) -> tuple[tuple[str, str, str, tuple[int, ...]], ...]:
    """Hash a named historical subtree as untrusted data, never as evidence."""
    if directory_name not in PRESERVED_HISTORICAL_AUDIT_DIRECTORIES:
        raise AuditError("historical audit directory name is not approved")
    root_before = os.stat(directory_name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISDIR(root_before.st_mode)
        or root_before.st_uid != owner
        or root_before.st_gid != group
        or stat.S_IMODE(root_before.st_mode) != _historical_root_mode(directory_name)
        or root_before.st_dev != root_device
    ):
        raise AuditError("historical audit directory metadata is unsafe")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOATIME", 0)
    )
    try:
        root_fd = os.open(directory_name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise AuditError("historical audit directory cannot be opened safely") from exc

    records: list[tuple[str, str, str, tuple[int, ...]]] = []
    total_bytes = 0
    try:
        if _identity(root_before) != _identity(os.fstat(root_fd)):
            raise AuditError("historical audit directory changed while opening")

        def walk(directory_fd: int, prefix: str, depth: int) -> None:
            nonlocal total_bytes
            if depth > 32 or len(records) > 100_000:
                raise AuditError("historical audit tree exceeds inventory limits")
            before_dir = os.fstat(directory_fd)
            names_before = tuple(sorted(os.listdir(directory_fd)))
            for name in names_before:
                if len(records) >= 100_000:
                    raise AuditError("historical audit tree exceeds inventory limits")
                if name in {".", ".."} or "/" in name or "\x00" in name:
                    raise AuditError("historical audit filename is invalid")
                relative = f"{prefix}/{name}"
                before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISDIR(before.st_mode):
                    if (
                        directory_name == "reviewed-v2.0.0"
                        or
                        before.st_uid != owner
                        or before.st_gid != group
                        or stat.S_IMODE(before.st_mode) != 0o700
                        or before.st_dev != root_device
                    ):
                        raise AuditError("historical audit subdirectory metadata is unsafe")
                    child_fd = os.open(name, flags, dir_fd=directory_fd)
                    try:
                        opened = os.fstat(child_fd)
                        if _identity(before) != _identity(opened):
                            raise AuditError("historical audit subdirectory changed while opening")
                        records.append((relative, "untrusted-directory", "", _identity(opened)))
                        walk(child_fd, relative, depth + 1)
                        after = os.fstat(child_fd)
                        entry_after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                        if _identity(opened) != _identity(after) or _identity(after) != _identity(entry_after):
                            raise AuditError("historical audit subdirectory changed while reading")
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(before.st_mode):
                    _historical_file_mode(
                        directory_name, relative, stat.S_IMODE(before.st_mode)
                    )
                    if (
                        before.st_uid != owner
                        or before.st_gid != group
                        or before.st_nlink != 1
                        or before.st_dev != root_device
                    ):
                        raise AuditError("historical audit file metadata is unsafe")
                    file_flags = (
                        os.O_RDONLY
                        | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NONBLOCK", 0)
                        | getattr(os, "O_NOATIME", 0)
                    )
                    file_fd = os.open(name, file_flags, dir_fd=directory_fd)
                    try:
                        opened = os.fstat(file_fd)
                        if _identity(before) != _identity(opened):
                            raise AuditError("historical audit file changed while opening")
                        digest = hashlib.sha256()
                        size = 0
                        while block := os.read(file_fd, 1024 * 1024):
                            digest.update(block)
                            size += len(block)
                            total_bytes += len(block)
                            if total_bytes > 2 * 1024 * 1024 * 1024:
                                raise AuditError("historical audit data exceeds inventory limit")
                        after = os.fstat(file_fd)
                        entry_after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                        if (
                            _identity(opened) != _identity(after)
                            or _identity(after) != _identity(entry_after)
                            or size != after.st_size
                        ):
                            raise AuditError("historical audit file changed while hashing")
                        records.append((relative, "untrusted-file", digest.hexdigest(), _identity(after)))
                    finally:
                        os.close(file_fd)
                else:
                    raise AuditError("historical audit tree contains a link or special file")
            names_after = tuple(sorted(os.listdir(directory_fd)))
            after_dir = os.fstat(directory_fd)
            if names_before != names_after or _identity(before_dir) != _identity(after_dir):
                raise AuditError("historical audit directory changed during inventory")

        records.append((directory_name, "untrusted-directory", "", _identity(root_before)))
        walk(root_fd, directory_name, 1)
        root_after = os.stat(directory_name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(root_before) != _identity(root_after):
            raise AuditError("historical audit directory changed during inventory")
        return tuple(records)
    finally:
        os.close(root_fd)


def inventory_historical_audit_directories(
    directory: str | Path,
    *,
    owner: int = 0,
    group: int = 0,
    names: tuple[str, ...] = tuple(sorted(PRESERVED_HISTORICAL_AUDIT_DIRECTORIES)),
) -> list[tuple[str, str, str]]:
    """Return hashes for explicitly preserved historical subtrees as untrusted."""
    if not set(names) <= PRESERVED_HISTORICAL_AUDIT_DIRECTORIES or len(set(names)) != len(names):
        raise AuditError("historical audit directory allowlist is invalid")
    path = Path(directory)
    try:
        before = path.lstat()
        if (
            not stat.S_ISDIR(before.st_mode)
            or before.st_uid != owner
            or before.st_gid != group
            or stat.S_IMODE(before.st_mode) != 0o700
        ):
            raise AuditError("audit directory metadata is unsafe")
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOATIME", 0),
        )
    except OSError as exc:
        raise AuditError("audit directory cannot be opened safely") from exc
    try:
        opened = os.fstat(fd)
        if _identity(before) != _identity(opened):
            raise AuditError("audit directory changed while opening")
        if not hasattr(os, "O_NOATIME"):
            raise AuditError("filesystem cannot guarantee atime-safe audit reads")
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        names_before = tuple(sorted(os.listdir(fd)))
        result = []
        for name in names:
            if name in names_before:
                result.extend(
                    (relative, kind, digest)
                    for relative, kind, digest, _identity_value in _historical_directory_snapshot(
                        fd, name, owner=owner, group=group, root_device=opened.st_dev
                    )
                )
        if names_before != tuple(sorted(os.listdir(fd))) or _identity(opened) != _identity(os.fstat(fd)):
            raise AuditError("audit directory changed during historical inventory")
        return result
    except OSError as exc:
        raise AuditError("historical audit data could not be read safely") from exc
    finally:
        os.close(fd)


def verify_audit_directory(
    directory: str | Path,
    *,
    owner: int = 0,
    group: int = 0,
    acquire_lock: bool = True,
    activation_release: str | None = None,
    activation_role: str = "bot",
    activation_in_progress: bool = False,
    preserved_historical_directories: tuple[str, ...] = (),
) -> int:
    """Return acknowledged record count; raise AuditError for every unsafe state.

    The shared directory lock coordinates with administrative recovery and
    deployment writers that take an exclusive flock on the same directory.
    Before/after snapshots detect ordinary concurrent mutations by other
    writers. Writers that ignore this lock are not made atomic by this check;
    protected directory ownership and the final snapshot are additional,
    not absolute, safeguards.
    """
    if not hasattr(os, "O_NOATIME"):
        raise AuditError("filesystem cannot guarantee atime-safe audit reads")
    if (
        not set(preserved_historical_directories) <= PRESERVED_HISTORICAL_AUDIT_DIRECTORIES
        or len(set(preserved_historical_directories)) != len(preserved_historical_directories)
    ):
        raise AuditError("historical audit directory allowlist is invalid")
    path = Path(directory)
    try:
        before_path = path.lstat()
        if (
            not stat.S_ISDIR(before_path.st_mode)
            or before_path.st_uid != owner
            or before_path.st_gid != group
            or stat.S_IMODE(before_path.st_mode) != 0o700
        ):
            raise AuditError("audit directory metadata is unsafe")
        fd = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOATIME", 0),
        )
    except OSError as exc:
        raise AuditError("audit directory cannot be opened safely") from exc
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != owner
            or opened.st_gid != group
            or stat.S_IMODE(opened.st_mode) != 0o700
            or (opened.st_dev, opened.st_ino)
            != (before_path.st_dev, before_path.st_ino)
        ):
            raise AuditError("audit directory changed while opening")
        if acquire_lock:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError as exc:
                raise AuditError("audit directory is being modified") from exc
        before_snapshot = _audit_snapshot(fd)
        names = set(before_snapshot[0])
        historical_snapshot = tuple(
            item
            for name in sorted(preserved_historical_directories)
            if name in names
            for item in _historical_directory_snapshot(
                fd, name, owner=owner, group=group, root_device=opened.st_dev
            )
        )
        if "deployment-state.json" in names or ".deployment-state.json.tmp" in names:
            raise AuditError("an active or incomplete deployment journal exists")
        for name, identity in before_snapshot[1]:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if name in preserved_historical_directories:
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != owner
                    or info.st_gid != group
                    or stat.S_IMODE(info.st_mode) != _historical_root_mode(name)
                    or _identity(info) != identity
                ):
                    raise AuditError("historical audit directory metadata is unsafe")
                continue
            if (
                TASK267_ATTESTATION_FILE.fullmatch(name)
                and name != TASK267_ATTESTATION_PATH.name
            ) or (
                TASK267_REVIEW_FILE.fullmatch(name)
                and name != TASK267_REVIEW_PATH.name
            ) or (
                TASK267_REPORT_FILE.fullmatch(name)
                and name != TASK267_REPORT_PATH.name
            ):
                raise AuditError("unexpected historical recovery evidence filename")
            allowed_name = any(
                pattern.fullmatch(name)
                for pattern in (
                    ARCHIVE,
                    ACK,
                    MANIFEST,
                    PREFLIGHT_COPY,
                    REVIEW,
                    RECOVERY_ADDENDUM,
                    TASK267_ATTESTATION_FILE,
                    TASK267_REVIEW_FILE,
                    TASK267_REPORT_FILE,
                    INSTALLATION_RESULT,
                    SERVICE_ACTIVATION,
                    LEGACY_SERVICE_ACTIVATION,
                )
            )
            if (
                not allowed_name
                or ".tmp." in name
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != owner
                or info.st_gid != group
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) not in {0o600, 0o700}
                or _identity(info) != identity
            ):
                raise AuditError("audit directory contains an unexpected file")

        archives: dict[str, str] = {}
        acknowledgments: dict[str, str] = {}
        addenda: dict[str, str] = {}
        manifests: dict[str, str] = {}
        installation_results: dict[str, str] = {}
        activation_events: dict[str, list[tuple[str, int, str]]] = {}
        legacy_activation_events: dict[str, list[tuple[str, str]]] = {}
        task267_addenda: dict[str, dict] = {}
        hash_bound_reviews: set[str] = set()
        expected_hash_bound_reviews: set[str] = set()
        for name in names:
            match = ARCHIVE.fullmatch(name)
            if match:
                archives[match.group(1)] = name
                continue
            match = ACK.fullmatch(name)
            if match:
                acknowledgments[match.group(1)] = name
                continue
            match = RECOVERY_ADDENDUM.fullmatch(name)
            if match:
                addenda[match.group(1)] = name
                continue
            match = REVIEW.fullmatch(name)
            if match and match.group(2) is not None:
                hash_bound_reviews.add(name)
            match = MANIFEST.fullmatch(name)
            if match:
                manifests[match.group(1)] = name
                continue
            match = INSTALLATION_RESULT.fullmatch(name)
            if match:
                installation_results[match.group(1)] = name
                continue
            match = SERVICE_ACTIVATION.fullmatch(name)
            if match:
                activation_events.setdefault(match.group(1), []).append(
                    (match.group(2), int(match.group(3)), name)
                )
            elif match := LEGACY_SERVICE_ACTIVATION.fullmatch(name):
                legacy_activation_events.setdefault(match.group(1), []).append(
                    (match.group(2), name)
                )
            elif name.startswith(
                (
                    "deployment-state-",
                    "deployment-recovery-ack-",
                    "deployment-installation-",
                    "deployment-",
                )
            ):
                raise AuditError("an incident evidence filename is invalid")

        if set(archives) != set(acknowledgments):
            raise AuditError("an archive is missing its matching acknowledgment")
        bound: set[str] = set()
        for journal_sha, archive_name in archives.items():
            journal_raw, journal = _read_json(
                fd, archive_name, owner=owner, group=group
            )
            if hashlib.sha256(journal_raw).hexdigest() != journal_sha:
                raise AuditError("archived journal hash mismatch")
            phase = journal.get("phase")
            if phase == "manual_recovery_required":
                if (
                    journal.get("fail_closed_verified") is not True
                    or journal.get("fail_closed_error") is not None
                ):
                    raise AuditError("incident was not verified fail-closed")
            elif phase != "complete":
                raise AuditError("journal phase is unresolved or invalid")
            approved, previous = (
                journal.get("approved_release_sha"),
                journal.get("previous_release_sha"),
            )
            if (
                not isinstance(approved, str)
                or re.fullmatch(r"[0-9a-f]{40}", approved) is None
                or not isinstance(previous, str)
                or re.fullmatch(r"[0-9a-f]{40}", previous) is None
                or approved in bound
            ):
                raise AuditError("journal release identity is invalid or duplicated")
            manifest_name = f"deployment-{approved}.json"
            if (
                journal.get("manifest") != str(path / manifest_name)
                or manifests.get(approved) != manifest_name
            ):
                raise AuditError(
                    "journal manifest reference is missing or inconsistent"
                )
            manifest_raw, manifest = _read_json(
                fd, manifest_name, owner=owner, group=group
            )
            if (
                set(manifest) != MANIFEST_FIELDS
                or manifest.get("approved_release_sha") != approved
                or manifest.get("previous_release_sha") != previous
                or journal.get("database_backup") != manifest.get("database_backup")
                or journal.get("database_backup_sha256")
                != manifest.get("database_backup_sha256")
                or journal.get("shared_preflight_backup")
                != manifest.get("shared_preflight_backup")
                or journal.get("shared_preflight_sha256")
                != manifest.get("shared_preflight_sha256")
            ):
                raise AuditError("manifest contradicts its journal")
            deployment_id = journal.get("deployment_id")
            result_name = journal.get("installation_result")
            result_sha = journal.get("installation_result_sha256")
            if result_name is not None or result_sha is not None:
                if (
                    not isinstance(deployment_id, str)
                    or re.fullmatch(r"[0-9a-f]{32}", deployment_id) is None
                    or result_name != f"deployment-installation-{deployment_id}.json"
                    or installation_results.get(deployment_id) != result_name
                    or not isinstance(result_sha, str)
                    or re.fullmatch(r"[0-9a-f]{64}", result_sha) is None
                ):
                    raise AuditError("installation result reference is invalid")
                result_raw, result = _read_json(
                    fd, result_name, owner=owner, group=group
                )
                expected_result_fields = {
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
                if (
                    hashlib.sha256(result_raw).hexdigest() != result_sha
                    or set(result) != expected_result_fields
                    or result.get("schema_version") != 1
                    or result.get("deployment_id") != deployment_id
                    or result.get("approved_release_sha") != approved
                    or result.get("previous_release_sha") != previous
                    or result.get("installer_sha256")
                    != journal.get("deployment_pin_installer_sha256")
                    or result.get("deployment_pin_sha256")
                    != journal.get("deployment_pin_sha256")
                    or result.get("manifest") != journal.get("manifest")
                    or result.get("manifest_sha256")
                    != hashlib.sha256(manifest_raw).hexdigest()
                    or result.get("database_backup") != manifest.get("database_backup")
                    or result.get("database_backup_sha256")
                    != manifest.get("database_backup_sha256")
                    or result.get("shared_preflight_backup")
                    != manifest.get("shared_preflight_backup")
                    or result.get("shared_preflight_sha256")
                    != manifest.get("shared_preflight_sha256")
                    or not isinstance(result.get("installer_sha256"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", result["installer_sha256"]) is None
                    or not isinstance(result.get("deployment_pin_sha256"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", result["deployment_pin_sha256"])
                    is None
                    or result.get("observed_service_states", {}).get(
                        "wow-forever-armory-bot.service"
                    )
                    != ["inactive", "disabled"]
                    or result.get("observed_service_states", {}).get(
                        "wow-forever-armory-backup.timer"
                    )
                    != ["inactive", "disabled"]
                    or not isinstance(
                        result.get("observed_service_states", {}).get(
                            "wow-forever-armory-backup.service"
                        ),
                        list,
                    )
                    or len(
                        result["observed_service_states"][
                            "wow-forever-armory-backup.service"
                        ]
                    )
                    != 2
                    or result["observed_service_states"][
                        "wow-forever-armory-backup.service"
                    ][0]
                    != "inactive"
                    or not isinstance(result.get("database_signature"), list)
                    or len(result["database_signature"]) != 8
                    or any(
                        type(value) is not int for value in result["database_signature"]
                    )
                    or result.get("database_signature")
                    != journal.get("database_signature")
                    or result.get("intended_service_state")
                    != journal.get("service_state")
                    or result.get("intended_backup_timer_state")
                    != journal.get("backup_timer_state")
                    or not isinstance(result.get("intended_service_state"), list)
                    or len(result["intended_service_state"]) != 2
                    or result["intended_service_state"][0] not in {"active", "inactive"}
                    or result["intended_service_state"][1]
                    not in {"enabled", "disabled"}
                    or not isinstance(result.get("intended_backup_timer_state"), list)
                    or len(result["intended_backup_timer_state"]) != 2
                    or result["intended_backup_timer_state"][0]
                    not in {"active", "inactive"}
                    or result["intended_backup_timer_state"][1]
                    not in {"enabled", "disabled"}
                    or not isinstance(result.get("verified_at_utc"), str)
                    or journal.get("installer_exit_code") != 0
                ):
                    raise AuditError("installation result contradicts its journal")
                try:
                    verified_at = datetime.fromisoformat(result["verified_at_utc"])
                except ValueError as exc:
                    raise AuditError("installation result time is invalid") from exc
                if (
                    verified_at.tzinfo is None
                    or verified_at.utcoffset() != UTC.utcoffset(verified_at)
                ):
                    raise AuditError("installation result time is not UTC")
            elif phase == "complete" and journal.get("completion_approval_reference"):
                raise AuditError(
                    "completed deployment is missing its installation result"
                )
            ack_raw, ack = _read_json(
                fd, acknowledgments[journal_sha], owner=owner, group=group
            )
            expected_decision = (
                "manual_recovery_verified"
                if phase == "manual_recovery_required"
                else "deployment_completion_verified"
            )
            reference = ack.get("approval_reference")
            ack_version = ack.get("schema_version")
            if type(ack_version) is not int or ack_version not in {1, 2}:
                raise AuditError("acknowledgment schema version is invalid")
            expected_ack_fields = ACK_ADDENDUM_FIELDS if ack_version == 2 else ACK_FIELDS
            if (
                set(ack) != expected_ack_fields
                or ack.get("schema_version") != ack_version
                or ack.get("journal_sha256") != journal_sha
                or ack.get("manifest_sha256")
                != hashlib.sha256(manifest_raw).hexdigest()
                or ack.get("approved_release_sha") != approved
                or ack.get("previous_release_sha") != previous
                or ack.get("decision")
                != (
                    "manual_recovery_verified_with_addendum"
                    if ack_version == 2 and phase == "manual_recovery_required"
                    else expected_decision
                )
                or (
                    phase == "complete"
                    and journal.get("completion_approval_reference")
                    and (
                        ack.get("approved_by") != journal.get("completion_approved_by")
                        or ack.get("approval_reference")
                        != journal.get("completion_approval_reference")
                    )
                )
                or (
                    phase == "complete"
                    and journal.get("completion_approval_reference")
                    and (
                        not isinstance(journal.get("completion_approved_by"), str)
                        or not journal["completion_approved_by"].strip()
                        or re.fullmatch(
                            r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}",
                            journal["completion_approval_reference"],
                        )
                        is None
                    )
                )
                or not isinstance(ack.get("approved_by"), str)
                or not ack["approved_by"].strip()
                or len(ack["approved_by"]) > 128
                or not isinstance(reference, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", reference) is None
                or not isinstance(ack.get("approved_at_utc"), str)
            ):
                raise AuditError("acknowledgment contents are invalid")
            try:
                timestamp = datetime.fromisoformat(ack["approved_at_utc"])
            except ValueError as exc:
                raise AuditError("acknowledgment time is invalid") from exc
            if timestamp.tzinfo is None or timestamp.utcoffset() != UTC.utcoffset(
                timestamp
            ):
                raise AuditError("acknowledgment time is not UTC")
            if ack_version == 2:
                addendum_name = addenda.get(journal_sha)
                addendum_hash = ack.get("recovery_addendum_sha256")
                if (
                    phase != "manual_recovery_required"
                    or addendum_name != f"recovery-addendum-{journal_sha}.json"
                    or not isinstance(addendum_hash, str)
                    or re.fullmatch(r"[0-9a-f]{64}", addendum_hash) is None
                ):
                    raise AuditError("addendum acknowledgment reference is invalid")
                addendum_raw, addendum = _read_json(
                    fd, addendum_name, owner=owner, group=group
                )
                if hashlib.sha256(addendum_raw).hexdigest() != addendum_hash:
                    raise AuditError("recovery addendum hash mismatch")
                _validate_task267_addendum(
                    addendum_raw,
                    addendum,
                    journal_sha256=journal_sha,
                    manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
                    approved_release_sha=approved,
                    previous_release_sha=previous,
                    journal_database_signature=journal.get("database_signature"),
                )
                review_sha = addendum.get("recovery_review_sha256")
                if not isinstance(review_sha, str) or re.fullmatch(
                    r"[0-9a-f]{64}", review_sha
                ) is None:
                    raise AuditError("recovery review hash is invalid")
                bound_review_name = f"recovery-review-{journal_sha}-{review_sha}.json"
                legacy_review_name = f"recovery-review-{journal_sha}.json"
                try:
                    os.stat(bound_review_name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    review_name = legacy_review_name
                else:
                    review_name = bound_review_name
                    expected_hash_bound_reviews.add(bound_review_name)
                recovery_review_raw, recovery_review = _read_json(
                    fd, review_name, owner=owner, group=group
                )
                if (
                    hashlib.sha256(recovery_review_raw).hexdigest()
                    != review_sha
                    or not isinstance(recovery_review.get("observations"), dict)
                    or Path(
                        recovery_review["observations"].get("release_target", "")
                    ).name
                    != addendum.get("recovery_release_sha")
                ):
                    raise AuditError("recovery review is not bound to addendum")
                # Preserve the historical TASK 262 filename without allowing it
                # to stand in for the newly hash-bound review.
                if journal_sha == TASK267_JOURNAL_SHA256 and legacy_review_name in names and review_name != legacy_review_name:
                    legacy_raw, _ = _read_json(
                        fd, legacy_review_name, owner=owner, group=group
                    )
                    if hashlib.sha256(legacy_raw).hexdigest() != TASK267_REVIEW_SHA256:
                        raise AuditError("historical recovery review was changed")
                task267_addenda[journal_sha] = addendum
                if (
                    ack.get("approved_by") != addendum.get("approved_by")
                    or ack.get("approval_reference") != addendum.get("approval_reference")
                    or ack.get("approved_at_utc") != addendum.get("approved_at_utc")
                ):
                    raise AuditError("acknowledgment does not match addendum approval")
            bound.add(approved)
        if set(addenda) != {
            journal_sha
            for journal_sha, ack_name in acknowledgments.items()
            if _read_json(fd, ack_name, owner=owner, group=group)[1].get("schema_version") == 2
        }:
            raise AuditError("an orphan or missing recovery addendum exists")
        if hash_bound_reviews != expected_hash_bound_reviews:
            raise AuditError("an orphan or mismatched hash-bound recovery review exists")
        if set(manifests) != bound:
            raise AuditError("an orphan or contradictory manifest exists")
        referenced_results = {
            _read_json(fd, archive_name, owner=owner, group=group)[1].get(
                "deployment_id"
            )
            for archive_name in archives.values()
        }
        if set(installation_results) != {
            value for value in referenced_results if isinstance(value, str)
        }:
            raise AuditError("an orphan or contradictory installation result exists")
        activation_attempts: dict[str, list[tuple[int, str, list[dict]]]] = {}
        normal_operation_journals: set[str] = set()
        for journal_sha, events in activation_events.items():
            if journal_sha not in archives:
                raise AuditError("service activation event has no archived deployment")
            archive_raw, archived_journal = _read_json(
                fd, archives[journal_sha], owner=owner, group=group
            )
            if hashlib.sha256(archive_raw).hexdigest() != journal_sha:
                raise AuditError("service activation report journal hash is invalid")
            expected_states = {
                "wow-forever-armory-bot.service": archived_journal.get("service_state"),
                "wow-forever-armory-backup.timer": archived_journal.get(
                    "backup_timer_state"
                ),
            }
            grouped: dict[str, list[tuple[int, str, dict]]] = {}
            event_fields = {
                "schema_version",
                "journal_sha256",
                "activation_id",
                "attempt_number",
                "sequence",
                "event",
                "deployment_id",
                "approved_release_sha",
                "completion_ack_sha256",
                "manifest_sha256",
                "installation_result_sha256",
                "deployment_pin_sha256",
                "approved_by",
                "approval_reference",
                "intended_states",
                "action",
                "observed_states",
                "failure_class",
                "recorded_at_utc",
            }
            for attempt_id, sequence, name in events:
                _, event = _read_json(fd, name, owner=owner, group=group)
                acknowledgment_raw, acknowledgment = _read_json(
                    fd,
                    acknowledgments[journal_sha],
                    owner=owner,
                    group=group,
                )
                event_version = event.get("schema_version")
                if (
                    type(event_version) is not int
                    or event_version not in {1, 2}
                    or set(event)
                    != (
                        event_fields
                        if event_version == 1
                        else event_fields | {"lifecycle_transition"}
                    )
                    or event.get("journal_sha256") != journal_sha
                    or event.get("activation_id") != attempt_id
                    or event.get("deployment_id")
                    != archived_journal.get("deployment_id")
                    or event.get("approved_release_sha")
                    != archived_journal.get("approved_release_sha")
                    or event.get("deployment_pin_sha256")
                    != archived_journal.get("deployment_pin_sha256")
                    or event.get("intended_states") != expected_states
                    or type(event.get("attempt_number")) is not int
                    or event.get("attempt_number", 0) < 1
                    or type(event.get("sequence")) is not int
                    or event.get("sequence") != sequence
                    or event.get("event")
                    not in {
                        "approved",
                        "action_planned",
                        "action_verified",
                        "success",
                        "failure",
                    }
                    or not isinstance(event.get("approved_by"), str)
                    or not event["approved_by"].strip()
                    or len(event["approved_by"]) > 128
                    or not isinstance(event.get("approval_reference"), str)
                    or re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}",
                        event["approval_reference"],
                    )
                    is None
                    or not isinstance(event.get("observed_states"), dict)
                    or any(
                        not isinstance(state, list)
                        or len(state) != 2
                        or state[0] not in {"active", "inactive", "failed"}
                        or state[1] not in {"enabled", "disabled", "static", "indirect"}
                        for state in event.get("observed_states", {}).values()
                    )
                    or not _valid_utc(event.get("recorded_at_utc"))
                    or event.get("completion_ack_sha256")
                    != hashlib.sha256(acknowledgment_raw).hexdigest()
                    or event.get("manifest_sha256")
                    != hashlib.sha256(manifest_raw).hexdigest()
                    or event.get("installation_result_sha256")
                    != archived_journal.get("installation_result_sha256")
                    or (
                        event.get("action") is not None
                        and not isinstance(event.get("action"), dict)
                    )
                ):
                    raise AuditError("service activation event is invalid")
                if event_version == 2:
                    addendum = task267_addenda.get(journal_sha)
                    _validate_lifecycle_transition(
                        event,
                        journal_sha256=journal_sha,
                        acknowledgment_raw=acknowledgment_raw,
                        acknowledgment=acknowledgment,
                        addendum=addendum,
                    )
                grouped.setdefault(attempt_id, []).append((sequence, name, event))

            attempt_numbers = set()
            for attempt_id, attempt_events in grouped.items():
                attempt_events.sort(key=lambda item: item[0])
                records = [item[2] for item in attempt_events]
                number = records[0]["attempt_number"]
                if number in attempt_numbers or [
                    r["sequence"] for r in records
                ] != list(range(len(records))):
                    raise AuditError("activation event sequence is invalid")
                attempt_numbers.add(number)
                if sorted({r["attempt_number"] for r in records}) != [number]:
                    raise AuditError("activation attempt number is inconsistent")
                if any(
                    (r["approved_by"], r["approval_reference"])
                    != (records[0]["approved_by"], records[0]["approval_reference"])
                    for r in records
                ):
                    raise AuditError("activation approval changed within an attempt")
                lifecycle_events = [
                    record for record in records if record.get("schema_version") == 2
                ]
                if lifecycle_events:
                    if (
                        len(lifecycle_events) != 1
                        or lifecycle_events[0] is not records[-1]
                        or records[-1].get("event") != "success"
                        or records[0]["intended_states"].get(
                            "wow-forever-armory-bot.service"
                        )
                        != ["active", "enabled"]
                        or not any(
                            record["event"] == "action_verified"
                            and record["action"]
                            == {
                                "unit": "wow-forever-armory-bot.service",
                                "verb": "start",
                                "target": ["active", "enabled"],
                            }
                            for record in records
                        )
                    ):
                        raise AuditError("lifecycle transition is not a completed bot activation")
                if records[0]["event"] != "approved" or records[0]["sequence"] != 0:
                    raise AuditError("activation attempt lacks its approval record")
                if (
                    records[0]["action"] is not None
                    or records[0]["failure_class"] is not None
                ):
                    raise AuditError("activation approval event contains an action")
                pending_action = None
                terminal = False
                verified_actions = []
                expected_actions = _expected_activation_actions(expected_states)
                for record in records[1:]:
                    event_type = record["event"]
                    if terminal:
                        raise AuditError("activation events follow a terminal result")
                    if event_type == "action_planned":
                        if (
                            pending_action is not None
                            or not _valid_activation_action(record["action"])
                            or record["failure_class"] is not None
                            or len(verified_actions) >= len(expected_actions)
                            or record["action"]
                            != expected_actions[len(verified_actions)]
                        ):
                            raise AuditError("activation action order is invalid")
                        pending_action = record["action"]
                    elif event_type == "action_verified":
                        if (
                            pending_action is None
                            or record["action"] != pending_action
                            or record["failure_class"] is not None
                            or record["observed_states"].get(pending_action["unit"])
                            is None
                            or record["observed_states"][pending_action["unit"]][
                                1
                                if pending_action["verb"] in {"enable", "disable"}
                                else 0
                            ]
                            != pending_action["target"][
                                1
                                if pending_action["verb"] in {"enable", "disable"}
                                else 0
                            ]
                        ):
                            raise AuditError("verified activation action has no plan")
                        verified_actions.append(pending_action)
                        pending_action = None
                    elif event_type == "success":
                        if (
                            pending_action is not None
                            or record["failure_class"] is not None
                            or verified_actions
                            != _expected_activation_actions(expected_states)
                        ):
                            raise AuditError("activation success is incomplete")
                        terminal = True
                    elif event_type == "failure":
                        if record["failure_class"] not in {"AdminError", "OSError"}:
                            raise AuditError("activation failure class is invalid")
                        terminal = True
                    else:
                        raise AuditError("activation approval is duplicated")
                activation_attempts.setdefault(journal_sha, []).append(
                    (number, attempt_id, records)
                )
                _, ack_record = _read_json(
                    fd, acknowledgments[journal_sha], owner=owner, group=group
                )
                if ack_record.get("schema_version") == 2:
                    if records[-1].get("event") == "success":
                        if lifecycle_events:
                            normal_operation_journals.add(journal_sha)
                        elif journal_sha not in normal_operation_journals:
                            raise AuditError(
                                "successful addendum activation lacks lifecycle transition"
                            )
                    elif lifecycle_events:
                        raise AuditError(
                            "incomplete activation cannot establish normal operation"
                        )
        if legacy_activation_events:
            _verify_legacy_activation_events(
                fd,
                legacy_activation_events,
                archives,
                owner=owner,
                group=group,
            )
        for journal_sha, addendum in task267_addenda.items():
            _verify_task267_live_files(
                addendum, normal_operation=journal_sha in normal_operation_journals
            )
        if activation_release is not None:
            if re.fullmatch(
                r"[0-9a-f]{40}", activation_release
            ) is None or activation_role not in {"bot", "backup"}:
                raise AuditError("activation start-check parameters are invalid")
            matching = []
            for journal_sha, archive_name in archives.items():
                archived = _read_json(
                    fd, archive_name, owner=owner, group=group
                )[1]
                ack_name = acknowledgments.get(journal_sha)
                addendum = task267_addenda.get(journal_sha)
                if ack_name is None:
                    continue
                _, ack = _read_json(fd, ack_name, owner=owner, group=group)
                if ack.get("schema_version") == 2:
                    if (
                        addendum is not None
                        and addendum.get("recovery_release_sha") == activation_release
                    ):
                        matching.append(journal_sha)
                elif archived.get("approved_release_sha") == activation_release:
                    matching.append(journal_sha)
            if len(matching) != 1 or matching[0] not in activation_attempts:
                raise AuditError("current release has no activation authorization")
            attempts = sorted(
                activation_attempts[matching[0]], key=lambda item: item[0]
            )
            if [item[0] for item in attempts] != list(range(1, len(attempts) + 1)):
                raise AuditError("activation attempts are ambiguous")
            latest_records = attempts[-1][2]
            latest = latest_records[-1]["event"]
            intended = latest_records[0]["intended_states"]
            bot_start_verified = any(
                record["event"] == "action_verified"
                and record["action"]
                == {
                    "unit": "wow-forever-armory-bot.service",
                    "verb": "start",
                    "target": intended.get("wow-forever-armory-bot.service"),
                }
                for record in latest_records
            )
            bot_start_pending = latest == "action_planned" and latest_records[-1][
                "action"
            ] == {
                "unit": "wow-forever-armory-bot.service",
                "verb": "start",
                "target": intended.get("wow-forever-armory-bot.service"),
            }
            permitted = (
                activation_role == "bot"
                and intended["wow-forever-armory-bot.service"][0] == "active"
                and (
                    latest == "success"
                    and bot_start_verified
                    or activation_in_progress
                    and bot_start_pending
                )
                or activation_role == "backup"
                and intended["wow-forever-armory-backup.timer"][0] == "active"
                and latest == "success"
            )
            if not permitted:
                raise AuditError("activation state does not authorize this unit")
        historical_after = tuple(
            item
            for name in sorted(preserved_historical_directories)
            if name in names
            for item in _historical_directory_snapshot(
                fd, name, owner=owner, group=group, root_device=opened.st_dev
            )
        )
        if _audit_snapshot(fd) != before_snapshot or historical_after != historical_snapshot:
            raise AuditError("audit directory changed during validation")
        after_path = path.lstat()
        after_fd = os.fstat(fd)
        if (after_path.st_dev, after_path.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ) or _identity(opened) != _identity(after_fd):
            raise AuditError("audit directory metadata changed during validation")
        return len(archives)
    except OSError as exc:
        raise AuditError("audit evidence could not be read safely") from exc
    finally:
        os.close(fd)
