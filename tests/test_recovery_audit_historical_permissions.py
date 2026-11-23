from __future__ import annotations

import hashlib
import json
import os
import grp
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from deploy import recovery_audit


HISTORICAL = ("reviewed-v2.0.0", "archive")


def make_active_chain(audit: Path) -> None:
    audit.mkdir(mode=0o700)
    audit.chmod(0o700)
    approved, previous = "a" * 40, "b" * 40
    manifest_name = f"deployment-{approved}.json"
    manifest = {
        "approved_release_sha": approved,
        "previous_release_sha": previous,
        "database_backup": "/synthetic/backup.sqlite3",
        "database_backup_sha256": "c" * 64,
        "shared_preflight_backup": "/synthetic/preflight.py",
        "shared_preflight_sha256": "d" * 64,
        "created_at_utc": "2026-11-23T00:00:00+00:00",
    }
    manifest_raw = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (audit / manifest_name).write_bytes(manifest_raw)
    journal = {
        "phase": "manual_recovery_required",
        "fail_closed_verified": True,
        "fail_closed_error": None,
        "approved_release_sha": approved,
        "previous_release_sha": previous,
        "manifest": str(audit / manifest_name),
        "database_backup": manifest["database_backup"],
        "database_backup_sha256": manifest["database_backup_sha256"],
        "shared_preflight_backup": manifest["shared_preflight_backup"],
        "shared_preflight_sha256": manifest["shared_preflight_sha256"],
    }
    journal_raw = (json.dumps(journal, sort_keys=True, separators=(",", ":")) + "\n").encode()
    journal_hash = hashlib.sha256(journal_raw).hexdigest()
    (audit / f"deployment-state-{journal_hash}.json").write_bytes(journal_raw)
    ack = {
        "schema_version": 1,
        "journal_sha256": journal_hash,
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "approved_release_sha": approved,
        "previous_release_sha": previous,
        "decision": "manual_recovery_verified",
        "approved_by": "synthetic-test",
        "approval_reference": "TASK-298-STAGING",
        "approved_at_utc": "2026-11-23T00:00:00+00:00",
    }
    (audit / f"deployment-recovery-ack-{journal_hash}.json").write_text(
        json.dumps(ack, sort_keys=True, separators=(",", ":")) + "\n", encoding="ascii"
    )
    for path in audit.iterdir():
        path.chmod(0o600)


def add_observed_safe_history(audit: Path) -> tuple[Path, ...]:
    reviewed = audit / "reviewed-v2.0.0"
    reviewed.mkdir(mode=0o700)
    reviewed.chmod(0o700)
    reviewed_files = []
    for name, content in (
        ("production_db_snapshot.py", b"untrusted snapshot source bytes"),
        ("production_admin.py", b"untrusted admin source bytes"),
    ):
        path = reviewed / name
        path.write_bytes(content)
        path.chmod(0o400)
        reviewed_files.append(path)
    reviewed.chmod(0o500)
    archive = audit / "archive"
    archive.mkdir(mode=0o700)
    archive.chmod(0o700)
    nested = archive / "older"
    nested.mkdir(mode=0o700)
    nested.chmod(0o700)
    archived_json = nested / "historical.json"
    archived_json.write_bytes(b"opaque historical JSON; never parsed")
    archived_json.chmod(0o600)
    return (*reviewed_files, archived_json)


def snapshot(root: Path) -> dict[str, tuple[bytes | None, int, int, int, int, int, int]]:
    result = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        data = path.read_bytes() if path.is_file() and not path.is_symlink() else None
        result[path.relative_to(root).as_posix()] = (
            data,
            info.st_mode,
            info.st_uid,
            info.st_gid,
            info.st_mtime_ns,
            info.st_ctime_ns,
            info.st_ino,
        )
    return result


def as_root_owned_metadata(audit: Path, callback):
    """Simulate root:root stat identity while retaining real local file bytes/modes."""
    paths = [audit, *audit.rglob("*")]
    identities = {
        (info.st_dev, info.st_ino)
        for path in paths
        for info in (path.lstat(),)
    }
    real_stat, real_lstat, real_fstat = os.stat, os.lstat, os.fstat

    def rootify(info):
        if (info.st_dev, info.st_ino) not in identities:
            return info
        fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        fields["st_uid"] = fields["st_gid"] = 0
        return SimpleNamespace(**fields)

    def fake_stat(path, *args, **kwargs):
        return rootify(real_stat(path, *args, **kwargs))

    def fake_lstat(path, *args, **kwargs):
        return rootify(real_lstat(path, *args, **kwargs))

    def fake_fstat(fd):
        return rootify(real_fstat(fd))

    with ExitStack() as stack:
        stack.enter_context(patch.object(os, "stat", fake_stat))
        stack.enter_context(patch.object(os, "lstat", fake_lstat))
        stack.enter_context(patch.object(os, "fstat", fake_fstat))
        return callback()


class HistoricalAuditPermissionTests(unittest.TestCase):
    def test_observed_unit_states_accept_systemd_activating_transition(self):
        self.assertTrue(
            recovery_audit._valid_observed_unit_states(
                {
                    "wow-forever-armory-backup.service": ["activating", "static"],
                    "wow-forever-armory-backup.timer": ["active", "enabled"],
                }
            )
        )
        self.assertFalse(
            recovery_audit._valid_observed_unit_states(
                {"wow-forever-armory-backup.service": ["unknown", "static"]}
            )
        )

    def test_pinned_preserved_preflight_is_verified_as_read_only_evidence(self) -> None:
        service_uid = 4242
        service_gid = 4343
        expected_by_path = {
            recovery_audit.TASK267_REPORT_PATH: recovery_audit.TASK267_REPORT_SHA256,
            recovery_audit.TASK267_PREFLIGHT_PATH: recovery_audit.TASK267_PREFLIGHT_SHA256,
            recovery_audit.TASK267_ATTESTATION_PATH: recovery_audit.TASK267_ATTESTATION_SHA256,
            recovery_audit.TASK267_REVIEW_PATH: recovery_audit.TASK267_REVIEW_SHA256,
            recovery_audit.TASK267_INCIDENT_BACKUP_PATH: recovery_audit.TASK267_INCIDENT_BACKUP_SHA256,
            recovery_audit.TASK267_LATEST_BACKUP_PATH: recovery_audit.TASK267_LATEST_BACKUP_SHA256,
        }
        observed_modes = {}

        def fake_hash(path, **kwargs):
            observed_modes[path] = kwargs["mode"]
            return expected_by_path[path], None

        with (
            patch.object(recovery_audit.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=service_uid)),
            patch.object(grp, "getgrnam", return_value=SimpleNamespace(gr_gid=service_gid)),
            patch.object(recovery_audit, "_hash_pinned_path", side_effect=fake_hash),
            patch.object(recovery_audit, "_task267_database_sidecar_snapshot", return_value={}),
            patch.object(recovery_audit, "_verify_task267_normal_database_metadata"),
        ):
            recovery_audit._verify_task267_live_files({}, normal_operation=True)

        self.assertEqual(observed_modes[recovery_audit.TASK267_PREFLIGHT_PATH], 0o444)
        self.assertEqual(observed_modes[recovery_audit.TASK267_ATTESTATION_PATH], 0o600)

    def _write_attestation_pair(self, audit: Path, canonical: bytes, duplicate: bytes) -> tuple[Path, Path]:
        canonical_path = audit / recovery_audit.TASK267_ATTESTATION_PATH.name
        duplicate_path = audit / recovery_audit.RECOVERY_ATTESTATION_DUPLICATE_NAME
        canonical_path.write_bytes(canonical)
        duplicate_path.write_bytes(duplicate)
        canonical_path.chmod(0o600)
        duplicate_path.chmod(0o600)
        return canonical_path, duplicate_path

    def test_exact_recovery_attestation_duplicate_is_accepted_only_as_same_pinned_bytes(self):
        self.assertEqual(
            recovery_audit.TASK267_ATTESTATION_SHA256,
            "dc82c79862053f64f460e41cc18bca6a90308545d4a4ac56781c09628dde09c4",
        )
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "audit"
            make_active_chain(audit)
            content = b'{"synthetic":"TASK 262 attestation fixture"}\n'
            content_sha = hashlib.sha256(content).hexdigest()
            canonical, duplicate = self._write_attestation_pair(audit, content, content)
            before = snapshot(audit)
            with patch.object(recovery_audit, "TASK267_ATTESTATION_SHA256", content_sha):
                count = as_root_owned_metadata(
                    audit,
                    lambda: recovery_audit.verify_audit_directory(
                        audit, owner=0, group=0
                    ),
                )
            self.assertEqual(count, 1)
            self.assertEqual(canonical.read_bytes(), duplicate.read_bytes())
            self.assertEqual(snapshot(audit), before)

    def test_recovery_attestation_duplicate_rejects_content_metadata_and_missing_pair(self):
        cases = ("different-bytes", "no-canonical", "wrong-mode", "hardlink")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                audit = Path(temporary) / "audit"
                make_active_chain(audit)
                content = b'{"synthetic":"TASK 262 attestation fixture"}\n'
                content_sha = hashlib.sha256(content).hexdigest()
                if case == "no-canonical":
                    duplicate = audit / recovery_audit.RECOVERY_ATTESTATION_DUPLICATE_NAME
                    duplicate.write_bytes(content)
                    duplicate.chmod(0o600)
                else:
                    canonical, duplicate = self._write_attestation_pair(audit, content, content)
                    if case == "different-bytes":
                        duplicate.write_bytes(content + b" ")
                        duplicate.chmod(0o600)
                    elif case == "wrong-mode":
                        duplicate.chmod(0o640)
                    elif case == "hardlink":
                        os.link(duplicate, duplicate.with_name("second-link"))
                with patch.object(recovery_audit, "TASK267_ATTESTATION_SHA256", content_sha):
                    with self.assertRaises(recovery_audit.AuditError):
                        as_root_owned_metadata(
                            audit,
                            lambda: recovery_audit.verify_audit_directory(
                                audit, owner=0, group=0
                            ),
                        )

    def test_other_recovery_attestation_names_remain_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "audit"
            make_active_chain(audit)
            content = b'{"synthetic":"TASK 262 attestation fixture"}\n'
            content_sha = hashlib.sha256(content).hexdigest()
            self._write_attestation_pair(audit, content, content)
            other = audit / f"recovery-attestation-{'0' * 64}.json"
            other.write_bytes(content)
            other.chmod(0o600)
            with patch.object(recovery_audit, "TASK267_ATTESTATION_SHA256", content_sha):
                with self.assertRaisesRegex(
                    recovery_audit.AuditError,
                    "audit directory contains an unexpected file",
                ):
                    as_root_owned_metadata(
                        audit,
                        lambda: recovery_audit.verify_audit_directory(
                            audit, owner=0, group=0
                        ),
                    )

    def observed_executable(self, audit: Path, *, payload: bytes = b"x" * 43_813) -> Path:
        target = audit / "archive" / "pre-wrapper-fix-20261114T184045Z" / "armory-deploy-a1a6a2a"
        target.parent.mkdir(mode=0o700)
        target.parent.chmod(0o700)
        target.write_bytes(payload)
        target.chmod(0o750)
        os.utime(target, ns=(1_794_676_808_459_758_687, 1_794_676_808_459_758_687))
        return target

    def test_exact_task280_file_with_wrong_content_hash_is_rejected_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "audit"
            make_active_chain(audit)
            add_observed_safe_history(audit)
            target = self.observed_executable(audit)
            before = snapshot(audit)
            with self.assertRaisesRegex(
                recovery_audit.AuditError,
                "TASK 280 historical observation hash does not match",
            ):
                as_root_owned_metadata(
                    audit,
                    lambda: recovery_audit.verify_audit_directory(
                        audit, owner=0, group=0, preserved_historical_directories=HISTORICAL
                    ),
                )
            self.assertEqual(snapshot(audit), before)
            self.assertEqual(target.stat().st_mode & 0o777, 0o750)

    def test_exact_task280_file_rejects_metadata_and_hardlink_deviations(self):
        for deviation in ("mode", "size", "mtime", "hardlink"):
            with self.subTest(deviation=deviation), tempfile.TemporaryDirectory() as temporary:
                audit = Path(temporary) / "audit"
                make_active_chain(audit)
                add_observed_safe_history(audit)
                target = self.observed_executable(audit)
                if deviation == "mode":
                    target.chmod(0o740)
                elif deviation == "size":
                    target.write_bytes(b"x" * 43_812)
                    target.chmod(0o750)
                    os.utime(target, ns=(1_794_676_808_459_758_687,) * 2)
                elif deviation == "mtime":
                    os.utime(target, ns=(1_794_676_808_459_758_686,) * 2)
                else:
                    os.link(target, target.parent / "second-link")
                with self.assertRaises(recovery_audit.AuditError):
                    as_root_owned_metadata(
                        audit,
                        lambda: recovery_audit.inventory_historical_audit_directories(
                            audit, owner=0, group=0, names=HISTORICAL
                        ),
                    )

    def test_other_executable_and_symlink_at_history_path_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "audit"
            make_active_chain(audit)
            add_observed_safe_history(audit)
            other = audit / "archive" / "other-executable.json"
            other.write_bytes(b"not the approved object")
            other.chmod(0o750)
            with self.assertRaisesRegex(
                recovery_audit.AuditError, "historical audit executable file is not approved"
            ):
                recovery_audit.verify_audit_directory(
                    audit, owner=os.getuid(), group=os.getgid(),
                    preserved_historical_directories=HISTORICAL,
                )
            other.unlink()
            target = audit / "archive" / "pre-wrapper-fix-20261114T184045Z"
            target.mkdir(mode=0o700)
            target.chmod(0o700)
            (target / "armory-deploy-a1a6a2a").symlink_to("/dev/null")
            with self.assertRaises(recovery_audit.AuditError):
                recovery_audit.verify_audit_directory(
                    audit, owner=os.getuid(), group=os.getgid(),
                    preserved_historical_directories=HISTORICAL,
                )

    def test_exact_observed_structure_rejects_group_executable_0750_file_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "audit"
            make_active_chain(audit)
            historical_files = add_observed_safe_history(audit)
            unsafe = audit / "archive" / "armory-deploy-a1a6a2a"
            unsafe.write_bytes(b"untrusted executable historical payload")
            unsafe.chmod(0o750)
            before = snapshot(audit)

            with self.assertRaisesRegex(
                recovery_audit.AuditError, "historical audit executable file is not approved"
            ):
                as_root_owned_metadata(
                    audit,
                    lambda: recovery_audit.inventory_historical_audit_directories(
                        audit, owner=0, group=0, names=HISTORICAL
                    ),
                )
            with self.assertRaisesRegex(
                recovery_audit.AuditError, "historical audit executable file is not approved"
            ):
                as_root_owned_metadata(
                    audit,
                    lambda: recovery_audit.verify_audit_directory(
                        audit,
                        owner=0,
                        group=0,
                        preserved_historical_directories=HISTORICAL,
                    ),
                )

            self.assertEqual(snapshot(audit), before)
            self.assertEqual(unsafe.stat().st_mode & 0o777, 0o750)
            self.assertTrue(all(path.exists() for path in historical_files))
            (audit / "reviewed-v2.0.0").chmod(0o700)

    def test_observed_read_only_modes_inventory_as_untrusted_and_validate_active_chain(self):
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "audit"
            make_active_chain(audit)
            historical_files = add_observed_safe_history(audit)
            before = snapshot(audit)

            def run_controls():
                inventory = recovery_audit.inventory_historical_audit_directories(
                    audit, owner=0, group=0, names=HISTORICAL
                )
                count = recovery_audit.verify_audit_directory(
                    audit,
                    owner=0,
                    group=0,
                    preserved_historical_directories=HISTORICAL,
                )
                return inventory, count

            inventory, count = as_root_owned_metadata(audit, run_controls)
            self.assertEqual(count, 1)
            self.assertEqual(len([item for item in inventory if item[1] == "untrusted-file"]), 3)
            self.assertEqual(
                {item[0] for item in inventory if item[1] == "untrusted-file"},
                {
                    "reviewed-v2.0.0/production_admin.py",
                    "reviewed-v2.0.0/production_db_snapshot.py",
                    "archive/older/historical.json",
                },
            )
            self.assertEqual(snapshot(audit), before)
            self.assertTrue(all(path.exists() for path in historical_files))
            (audit / "reviewed-v2.0.0").chmod(0o700)

    def test_unsafe_historical_file_classes_fail_closed(self):
        cases = (
            ("unknown.py", b"unknown", 0o400, "historical archive accepts only read-only JSON files"),
            ("group-write.json", b"group writable", 0o660, "historical audit file is group- or world-writable"),
            ("executable.json", b"executable", 0o700, "historical audit executable file is not approved"),
        )
        for filename, payload, mode, error in cases:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temporary:
                audit = Path(temporary) / "audit"
                make_active_chain(audit)
                add_observed_safe_history(audit)
                unsafe = audit / "archive" / filename
                unsafe.write_bytes(payload)
                unsafe.chmod(mode)
                with self.assertRaisesRegex(recovery_audit.AuditError, error):
                    recovery_audit.verify_audit_directory(
                        audit,
                        owner=os.getuid(),
                        group=os.getgid(),
                        preserved_historical_directories=HISTORICAL,
                    )

    def test_reviewed_directory_rejects_unknown_file_and_unapproved_modes(self):
        for filename, mode in (("extra.json", 0o400), ("production_admin.py", 0o440)):
            with self.subTest(filename=filename, mode=oct(mode)), tempfile.TemporaryDirectory() as temporary:
                audit = Path(temporary) / "audit"
                make_active_chain(audit)
                reviewed = audit / "reviewed-v2.0.0"
                reviewed.mkdir(mode=0o700)
                reviewed.chmod(0o700)
                for name in ("production_db_snapshot.py", "production_admin.py"):
                    if name != filename:
                        item = reviewed / name
                        item.write_bytes(b"opaque")
                        item.chmod(0o400)
                extra = reviewed / filename
                extra.write_bytes(b"opaque")
                extra.chmod(mode)
                (audit / "archive").mkdir(mode=0o700)
                (audit / "archive").chmod(0o700)
                with self.assertRaises(recovery_audit.AuditError):
                    recovery_audit.verify_audit_directory(
                        audit,
                        owner=os.getuid(),
                        group=os.getgid(),
                        preserved_historical_directories=HISTORICAL,
                    )
                reviewed.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
