#!/usr/bin/env python3
"""Shared fail-closed validation for retained deployment incident evidence."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
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
REVIEW = re.compile(r"recovery-review-([0-9a-f]{64})\.json\Z")
INSTALLATION_RESULT = re.compile(r"deployment-installation-([0-9a-f]{32})\.json\Z")
SERVICE_ACTIVATION = re.compile(
    r"service-activation-([0-9a-f]{64})-([0-9a-f]{32})-([0-9]{4})\.json\Z"
)
LEGACY_SERVICE_ACTIVATION = re.compile(
    r"service-activation-([0-9a-f]{64})-([0-9a-f]{32})\.json\Z"
)
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
        | getattr(os, "O_NONBLOCK", 0),
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


def verify_audit_directory(
    directory: str | Path,
    *,
    owner: int = 0,
    group: int = 0,
    acquire_lock: bool = True,
    activation_release: str | None = None,
    activation_role: str = "bot",
    activation_in_progress: bool = False,
) -> int:
    """Return acknowledged record count; raise AuditError for every unsafe state.

    The shared directory lock coordinates with administrative recovery and
    deployment writers that take an exclusive flock on the same directory.
    Before/after snapshots detect ordinary concurrent mutations by other
    writers. Writers that ignore this lock are not made atomic by this check;
    protected directory ownership and the final snapshot are additional,
    not absolute, safeguards.
    """
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
            | getattr(os, "O_CLOEXEC", 0),
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
        if "deployment-state.json" in names or ".deployment-state.json.tmp" in names:
            raise AuditError("an active or incomplete deployment journal exists")
        for name, identity in before_snapshot[1]:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            allowed_name = any(
                pattern.fullmatch(name)
                for pattern in (
                    ARCHIVE,
                    ACK,
                    MANIFEST,
                    PREFLIGHT_COPY,
                    REVIEW,
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
        manifests: dict[str, str] = {}
        installation_results: dict[str, str] = {}
        activation_events: dict[str, list[tuple[str, int, str]]] = {}
        legacy_activation_events: dict[str, list[tuple[str, str]]] = {}
        for name in names:
            match = ARCHIVE.fullmatch(name)
            if match:
                archives[match.group(1)] = name
                continue
            match = ACK.fullmatch(name)
            if match:
                acknowledgments[match.group(1)] = name
                continue
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
            _, ack = _read_json(
                fd, acknowledgments[journal_sha], owner=owner, group=group
            )
            expected_decision = (
                "manual_recovery_verified"
                if phase == "manual_recovery_required"
                else "deployment_completion_verified"
            )
            reference = ack.get("approval_reference")
            if (
                set(ack) != ACK_FIELDS
                or type(ack.get("schema_version")) is not int
                or ack.get("schema_version") != 1
                or ack.get("journal_sha256") != journal_sha
                or ack.get("manifest_sha256")
                != hashlib.sha256(manifest_raw).hexdigest()
                or ack.get("approved_release_sha") != approved
                or ack.get("previous_release_sha") != previous
                or ack.get("decision") != expected_decision
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
            bound.add(approved)
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
            for attempt_id, sequence, name in events:
                _, event = _read_json(fd, name, owner=owner, group=group)
                if (
                    set(event)
                    != {
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
                    or event.get("schema_version") != 1
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
                    != hashlib.sha256(
                        _read_json(
                            fd,
                            acknowledgments[journal_sha],
                            owner=owner,
                            group=group,
                        )[0]
                    ).hexdigest()
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
        if legacy_activation_events:
            _verify_legacy_activation_events(
                fd,
                legacy_activation_events,
                archives,
                owner=owner,
                group=group,
            )
        if activation_release is not None:
            if re.fullmatch(
                r"[0-9a-f]{40}", activation_release
            ) is None or activation_role not in {"bot", "backup"}:
                raise AuditError("activation start-check parameters are invalid")
            matching = [
                journal_sha
                for journal_sha, archive_name in archives.items()
                if _read_json(fd, archive_name, owner=owner, group=group)[1].get(
                    "approved_release_sha"
                )
                == activation_release
            ]
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
        if _audit_snapshot(fd) != before_snapshot:
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
