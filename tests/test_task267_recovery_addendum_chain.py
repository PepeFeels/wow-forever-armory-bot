from __future__ import annotations

import hashlib
import errno
import json
import tempfile
import unittest
import os
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from deploy import production_admin as admin
from deploy import recovery_audit


class Task267RecoveryAddendumChainTests(unittest.TestCase):
    def setUp(self):
        self.historical = [11, 22, 33000, 100, 200, 1001, 1001, 0o100600]
        self.verified = [11, 22, 33000, 300, 400, 1001, 1001, 0o100600]
        self.addendum = {
            "schema_version": 2,
            "decision": "preserve_current_database_after_fresh_integrity",
            "journal_sha256": recovery_audit.TASK267_JOURNAL_SHA256,
            "manifest_sha256": recovery_audit.TASK267_MANIFEST_SHA256,
            "verification_report_sha256": recovery_audit.TASK267_REPORT_SHA256,
            "historical_attestation_sha256": recovery_audit.TASK267_ATTESTATION_SHA256,
            "historical_review_sha256": recovery_audit.TASK267_REVIEW_SHA256,
            "historical_database_signature": self.historical,
            "verified_database_signature": self.verified,
            "database_sha256": "a" * 64,
            "backup_sha256": recovery_audit.TASK267_INCIDENT_BACKUP_SHA256,
            "latest_backup_sha256": recovery_audit.TASK267_LATEST_BACKUP_SHA256,
            "saved_preflight_sha256": recovery_audit.TASK267_PREFLIGHT_SHA256,
            "historical_eagain": "unresolved_no_retry",
            "recovery_release_sha": "a" * 40,
            "recovery_review_sha256": "b" * 64,
            "approved_by": "independent-reviewer",
            "approval_reference": "PO-TASK267-REVIEW",
            "approved_at_utc": "2026-11-22T10:00:00+00:00",
        }

    def _audit_validate(self, record=None, historical=None):
        value = self.addendum if record is None else record
        raw = json.dumps(value, sort_keys=True).encode("ascii")
        recovery_audit._validate_task267_addendum(
            raw,
            value,
            journal_sha256=recovery_audit.TASK267_JOURNAL_SHA256,
            manifest_sha256=recovery_audit.TASK267_MANIFEST_SHA256,
            approved_release_sha="a" * 40,
            previous_release_sha="b" * 40,
            journal_database_signature=self.historical if historical is None else historical,
        )

    def test_audit_validator_accepts_only_pinned_timestamp_drift(self):
        self._audit_validate()

    def test_audit_validator_rejects_changed_report_or_manifest_pin(self):
        for key in (
            "verification_report_sha256",
            "manifest_sha256",
            "backup_sha256",
            "journal_sha256",
        ):
            changed = dict(self.addendum)
            changed[key] = "0" * 64
            with self.subTest(key=key), self.assertRaises(recovery_audit.AuditError):
                self._audit_validate(changed)

    def test_audit_validator_rejects_changed_historical_signature(self):
        with self.assertRaises(recovery_audit.AuditError):
            self._audit_validate(historical=[*self.historical[:-1], 1002])

    def test_audit_validator_rejects_non_timestamp_drift(self):
        changed = dict(self.addendum)
        changed["verified_database_signature"] = [99, *self.verified[1:]]
        with self.assertRaisesRegex(recovery_audit.AuditError, "timestamp-only"):
            self._audit_validate(changed)

    def test_audit_validator_requires_a_real_timestamp_delta(self):
        changed = dict(self.addendum)
        changed["verified_database_signature"] = self.historical[:]
        with self.assertRaisesRegex(recovery_audit.AuditError, "timestamp-only"):
            self._audit_validate(changed)

    def test_audit_validator_keeps_historical_eagain_unresolved(self):
        changed = dict(self.addendum)
        changed["historical_eagain"] = "resolved"
        with self.assertRaises(recovery_audit.AuditError):
            self._audit_validate(changed)

    def test_audit_validator_rejects_boolean_schema_version(self):
        changed = dict(self.addendum)
        changed["schema_version"] = True
        with self.assertRaises(recovery_audit.AuditError):
            self._audit_validate(changed)

    def test_audit_validator_rejects_non_string_recovery_release(self):
        changed = dict(self.addendum)
        changed["recovery_release_sha"] = []
        with self.assertRaises(recovery_audit.AuditError):
            self._audit_validate(changed)

    def _report(self):
        signature9 = [11, 22, 33000, 1, 300, 400, 0o100600, 1001, 1001]
        stopped = {
            "wow-forever-armory-bot.service": {"active": "inactive", "enabled": "disabled"},
            "wow-forever-armory-backup.timer": {"active": "inactive", "enabled": "disabled"},
            "wow-forever-armory-backup.service": {"active": "inactive"},
        }
        return {
            "schema_version": 1,
            "status": "PASS",
            "hostname": "nololinux-server",
            "warnings": [
                "Incident journal signature differs from the current database."
            ],
            "incident_references": {
                "phase": "manual_recovery_required",
                "journal_sha256": recovery_audit.TASK267_JOURNAL_SHA256,
                "manifest_sha256": recovery_audit.TASK267_MANIFEST_SHA256,
                "backup_sha256": recovery_audit.TASK267_INCIDENT_BACKUP_SHA256,
                "latest_backup_sha256": recovery_audit.TASK267_LATEST_BACKUP_SHA256,
            },
            "systemd_before": stopped,
            "systemd_after": stopped,
            "database": {
                "integrity_check": "ok",
                "schema": "PASS",
                "counts": {"guilds": 1, "members": 4, "characters": 4},
                "source_sha256": "a" * 64,
                "snapshot_sha256": "a" * 64,
                "source_signature": signature9,
                "incident_signature_match": False,
                "wal": {
                    "present": False,
                    "snapshot_sha256": None,
                    "source_sha256": None,
                },
                "shm": {"present": False, "sha256": None},
                "process_open_count_before_and_after": 0,
                "backup_comparison": {
                    "incident": {"status": "MATCH"},
                    "latest_2026_11_15": {"status": "MATCH"},
                },
            },
        }

    def test_admin_report_validator_accepts_exact_expected_report_claims(self):
        journal = {"database_signature": self.historical}
        manifest = {}
        admin._validate_task267_report(
            self._report(), journal, manifest, self.addendum,
            journal_sha256=recovery_audit.TASK267_JOURNAL_SHA256,
            manifest_sha256=recovery_audit.TASK267_MANIFEST_SHA256,
        )

    def test_admin_report_validator_rejects_missing_fresh_integrity(self):
        report = self._report()
        report["database"]["integrity_check"] = None
        with self.assertRaises(admin.AdminError):
            admin._validate_task267_report(
                report, {"database_signature": self.historical}, {}, self.addendum,
                journal_sha256=recovery_audit.TASK267_JOURNAL_SHA256,
                manifest_sha256=recovery_audit.TASK267_MANIFEST_SHA256,
            )
        report = self._report()
        report["database"]["wal"] = {"present": False}
        with self.assertRaises(admin.AdminError):
            admin._validate_task267_report(
                report, {"database_signature": self.historical}, {}, self.addendum,
                journal_sha256=recovery_audit.TASK267_JOURNAL_SHA256,
                manifest_sha256=recovery_audit.TASK267_MANIFEST_SHA256,
            )

    def test_admin_report_validator_requires_v1_schema_and_exact_sidecar_shape(self):
        report = self._report()
        report["schema_version"] = 2
        with self.assertRaises(admin.AdminError):
            admin._validate_task267_report(
                report, {"database_signature": self.historical}, {}, self.addendum,
                journal_sha256=recovery_audit.TASK267_JOURNAL_SHA256,
                manifest_sha256=recovery_audit.TASK267_MANIFEST_SHA256,
            )

    def test_admin_report_validator_allows_only_documented_warning(self):
        report = self._report()
        report["warnings"].append("unexpected warning")
        with self.assertRaises(admin.AdminError):
            admin._validate_task267_report(
                report, {"database_signature": self.historical}, {}, self.addendum,
                journal_sha256=recovery_audit.TASK267_JOURNAL_SHA256,
                manifest_sha256=recovery_audit.TASK267_MANIFEST_SHA256,
            )
    def test_admin_report_validator_rejects_active_service(self):
        report = self._report()
        report["systemd_after"]["wow-forever-armory-bot.service"]["active"] = "active"
        with self.assertRaises(admin.AdminError):
            admin._validate_task267_report(
                report, {"database_signature": self.historical}, {}, self.addendum,
                journal_sha256=recovery_audit.TASK267_JOURNAL_SHA256,
                manifest_sha256=recovery_audit.TASK267_MANIFEST_SHA256,
            )

    def test_process_scan_fails_closed_when_proc_access_is_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "db"
            path.write_bytes(b"synthetic")
            with patch.object(admin.os, "open", side_effect=PermissionError(errno.EPERM, "denied")):
                with self.assertRaisesRegex(admin.AdminError, "process handle scan"):
                    admin._database_open_handle_count(path)

    def test_passive_proc_scan_counts_database_inode_without_content_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "db"
            path.write_bytes(b"synthetic")
            real_stat = os.stat

            def fake_stat(path_or_fd, *args, **kwargs):
                if path_or_fd == "3" and kwargs.get("dir_fd") == 102:
                    return path.stat()
                return real_stat(path_or_fd, *args, **kwargs)

            with patch.object(admin.os, "open", side_effect=lambda p, *a, **k: 101 if str(p) == "/proc" else 102), patch.object(
                admin.os, "listdir", side_effect=lambda fd: ["4242"] if fd == 101 else ["3"]
            ), patch.object(admin.os, "stat", side_effect=fake_stat), patch.object(
                admin.os, "close", return_value=None
            ):
                self.assertEqual(admin._database_open_handle_count(path), 1)

    def test_report_reader_preserves_access_time_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "nolo-admin"
            parent.mkdir(mode=0o700)
            report = parent / "report.json"
            report.write_bytes(b'{"status":"PASS"}')
            report.chmod(0o600)
            os.utime(report, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
            before = report.stat().st_atime_ns
            with patch.object(admin, "TASK267_REPORT", report):
                raw, _ = admin._read_task267_report(os.geteuid())
            self.assertEqual(raw, b'{"status":"PASS"}')
            self.assertEqual(report.stat().st_atime_ns, before)
            alias = parent / "alias.json"
            alias.symlink_to(report)
            with patch.object(admin, "TASK267_REPORT", alias):
                with self.assertRaises(admin.AdminError):
                    admin._read_task267_report(os.geteuid())

    def test_noatime_denial_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "protected"
            parent.mkdir(mode=0o700)
            data = parent / "evidence"
            data.write_bytes(b"pinned")
            data.chmod(0o600)
            with patch.object(recovery_audit.os, "open", side_effect=PermissionError(errno.EPERM, "denied")):
                with self.assertRaises(recovery_audit.AuditError):
                    recovery_audit._hash_pinned_path(
                        data, owner=os.geteuid(), group=os.getegid(), mode=0o600,
                        parent_owner=os.geteuid(), parent_mode=0o700,
                    )
            report = parent / "report.json"
            report.write_text("{}")
            report.chmod(0o600)
            with patch.object(admin, "TASK267_REPORT", report), patch.object(
                admin.os, "open", side_effect=PermissionError(errno.EPERM, "denied")
            ):
                with self.assertRaises(admin.AdminError):
                    admin._read_task267_report(os.geteuid())

    def test_addendum_cli_rejects_unpinned_journal_and_injected_verifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "audit"
            audit.mkdir(mode=0o700)
            review = audit / ("recovery-review-" + "0" * 64 + ".json")
            with self.assertRaises(admin.AdminError):
                admin.acknowledge_incident(
                    audit,
                    review,
                    expected_journal_sha256="0" * 64,
                    approved_by="admin",
                    approval_reference="PO-267",
                    recovery_addendum_sha256="1" * 64,
                    evidence_verifier=lambda *args: None,
                )

    def test_addendum_cli_rejects_malformed_expected_hash_before_io(self):
        with self.assertRaisesRegex(admin.AdminError, "addendum recovery parameters"):
            admin.acknowledge_incident(
                Path("/nonexistent/audit"),
                Path("/nonexistent/audit/recovery-review-" + "f" * 64 + ".json"),
                expected_journal_sha256=admin.TASK267_JOURNAL_SHA256,
                approved_by="admin",
                approval_reference="PO-267",
                recovery_addendum_sha256="not-a-digest",
            )

    def _temporary_live_evidence(self, root):
        state = root / "state"
        backups = state / "backups"
        report_dir = root / "nolo-admin"
        backups.mkdir(parents=True, mode=0o700)
        state.chmod(0o700)
        report_dir.mkdir(mode=0o700)
        db = state / "guild-armory.sqlite3"
        db.write_bytes(b"current-db-bytes")
        db.chmod(0o600)
        incident = backups / "incident.sqlite3"
        incident.write_bytes(b"incident-backup")
        incident.chmod(0o600)
        latest = backups / "latest.sqlite3"
        latest.write_bytes(b"latest-backup")
        latest.chmod(0o600)
        report = report_dir / "report.json"
        report.write_bytes(b'{"status":"PASS"}')
        report.chmod(0o600)
        info = db.stat()
        audit_dir = root / "audit"
        audit_dir.mkdir(mode=0o700)
        preflight = audit_dir / "preflight.py"
        preflight.write_bytes(b"synthetic preflight")
        preflight.chmod(0o444)
        attestation = audit_dir / "attestation.json"
        attestation.write_bytes(b"synthetic attestation")
        attestation.chmod(0o600)
        historical_review = audit_dir / "review.json"
        historical_review.write_bytes(b"synthetic historical review")
        historical_review.chmod(0o600)
        addendum = {
            "database_sha256": hashlib.sha256(db.read_bytes()).hexdigest(),
            "verified_database_signature": [
                info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns, info.st_uid, info.st_gid, info.st_mode,
            ],
        }
        return db, incident, latest, report, addendum, preflight, attestation, historical_review

    @contextmanager
    def _patch_live_evidence_paths(self, db, incident, latest, report):
        uid, gid = os.geteuid(), os.getegid()
        with ExitStack() as stack:
            preflight = db.parent / "backups" / ".." / ".." / "audit" / "preflight.py"
            preflight = preflight.resolve()
            historical_attestation = preflight.parent / "attestation.json"
            historical_review = preflight.parent / "review.json"
            values = {
                "TASK267_REPORT_PATH": report,
                "TASK267_DATABASE_PATH": db,
                "TASK267_INCIDENT_BACKUP_PATH": incident,
                "TASK267_LATEST_BACKUP_PATH": latest,
                "TASK267_PREFLIGHT_PATH": preflight,
                "TASK267_ATTESTATION_PATH": historical_attestation,
                "TASK267_REVIEW_PATH": historical_review,
                "TASK267_REPORT_SHA256": hashlib.sha256(report.read_bytes()).hexdigest(),
                "TASK267_INCIDENT_BACKUP_SHA256": hashlib.sha256(incident.read_bytes()).hexdigest(),
                "TASK267_LATEST_BACKUP_SHA256": hashlib.sha256(latest.read_bytes()).hexdigest(),
                "TASK267_PREFLIGHT_SHA256": hashlib.sha256(preflight.read_bytes()).hexdigest(),
                "TASK267_ATTESTATION_SHA256": hashlib.sha256(historical_attestation.read_bytes()).hexdigest(),
                "TASK267_REVIEW_SHA256": hashlib.sha256(historical_review.read_bytes()).hexdigest(),
                "TASK267_ADMIN_UID": uid,
                "TASK267_ADMIN_GID": gid,
            }
            for name, value in values.items():
                stack.enter_context(patch.object(recovery_audit, name, value))
            stack.enter_context(patch.object(recovery_audit.pwd, "getpwnam", side_effect=lambda _name: SimpleNamespace(pw_uid=uid, pw_gid=gid)))
            stack.enter_context(patch("grp.getgrnam", side_effect=lambda _name: SimpleNamespace(gr_gid=gid)))
            stack.enter_context(patch.object(recovery_audit, "_task267_database_handle_count", return_value=0))
            yield

    def test_startup_live_file_check_accepts_exact_database_and_pins(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, incident, latest, report, addendum, *_ = self._temporary_live_evidence(Path(tmp))
            with self._patch_live_evidence_paths(db, incident, latest, report):
                recovery_audit._verify_task267_live_files(addendum)

    def test_startup_live_file_check_rejects_open_database_handle(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, incident, latest, report, addendum, *_ = self._temporary_live_evidence(Path(tmp))
            with self._patch_live_evidence_paths(db, incident, latest, report):
                with db.open("rb"):
                    with patch.object(recovery_audit, "_task267_database_handle_count", return_value=1):
                        with self.assertRaisesRegex(recovery_audit.AuditError, "process has"):
                            recovery_audit._verify_task267_live_files(addendum)

    def test_startup_live_file_check_rejects_missing_report_and_changed_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, incident, latest, report, addendum, *_ = self._temporary_live_evidence(Path(tmp))
            with self._patch_live_evidence_paths(db, incident, latest, report):
                db.write_bytes(b"changed-db-bytes")
                with self.assertRaises(recovery_audit.AuditError):
                    recovery_audit._verify_task267_live_files(addendum)

    def test_normal_operation_accepts_database_writes_only_after_trusted_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, incident, latest, report, addendum, *_ = self._temporary_live_evidence(Path(tmp))
            with self._patch_live_evidence_paths(db, incident, latest, report):
                db.write_bytes(b"normal-runtime-update")
                with self.assertRaisesRegex(recovery_audit.AuditError, "database is missing or changed"):
                    recovery_audit._verify_task267_live_files(addendum)
                recovery_audit._verify_task267_live_files(
                    addendum, normal_operation=True
                )
                report.write_bytes(b"changed-report")
                with self.assertRaises(recovery_audit.AuditError):
                    recovery_audit._verify_task267_live_files(
                        addendum, normal_operation=True
                    )

    def test_normal_operation_rejects_unsafe_database_metadata_and_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, incident, latest, report, addendum, *_ = self._temporary_live_evidence(Path(tmp))
            with self._patch_live_evidence_paths(db, incident, latest, report):
                wal = Path(f"{db}-wal")
                wal.write_bytes(b"wal")
                wal.chmod(0o600)
                recovery_audit._verify_task267_live_files(
                    addendum, normal_operation=True
                )
                wal.chmod(0o644)
                with self.assertRaisesRegex(
                    recovery_audit.AuditError, "sidecar is unsafe"
                ):
                    recovery_audit._verify_task267_live_files(
                        addendum, normal_operation=True
                    )

    def test_normal_operation_rejects_database_inode_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, incident, latest, report, addendum, *_ = self._temporary_live_evidence(Path(tmp))
            with self._patch_live_evidence_paths(db, incident, latest, report):
                replacement = db.with_name("replacement.sqlite3")
                replacement.write_bytes(b"replacement database")
                replacement.chmod(0o600)
                db.unlink()
                replacement.rename(db)
                with self.assertRaisesRegex(
                    recovery_audit.AuditError, "metadata is unsafe"
                ):
                    recovery_audit._verify_task267_live_files(
                        addendum, normal_operation=True
                    )

    def test_live_file_check_rejects_missing_historical_attestation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, incident, latest, report, addendum, _preflight, attestation, _review = self._temporary_live_evidence(Path(tmp))
            with self._patch_live_evidence_paths(db, incident, latest, report):
                attestation.unlink()
                with self.assertRaises(recovery_audit.AuditError):
                    recovery_audit._verify_task267_live_files(addendum)
                db.write_bytes(b"current-db-bytes")
                info = db.stat()
                addendum["database_sha256"] = hashlib.sha256(db.read_bytes()).hexdigest()
                addendum["verified_database_signature"] = [
                    info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                    info.st_ctime_ns, info.st_uid, info.st_gid, info.st_mode,
                ]
                report.unlink()
                with self.assertRaises(recovery_audit.AuditError):
                    recovery_audit._verify_task267_live_files(addendum)

    def test_schema_one_acknowledgment_fields_remain_separate(self):
        self.assertNotIn("recovery_addendum_sha256", recovery_audit.ACK_FIELDS)
        self.assertIn("recovery_addendum_sha256", recovery_audit.ACK_ADDENDUM_FIELDS)

    def test_end_to_end_audit_chain_accepts_v2_only_with_matching_addendum(self):
        owner = __import__("os").geteuid()
        group = __import__("os").getegid()
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "audit"
            audit.mkdir(mode=0o700)
            release, previous = "a" * 40, "b" * 40
            backup_path = str(recovery_audit.TASK267_INCIDENT_BACKUP_PATH)
            preflight_path = str(audit / f"armory-db-preflight.{previous}.py")
            preflight_copy = Path(preflight_path)
            preflight_copy.write_bytes(b"synthetic preflight copy")
            preflight_copy.chmod(0o700)
            preflight_hash = hashlib.sha256(preflight_copy.read_bytes()).hexdigest()
            historical_attestation = audit / (
                "task262-attestation-" + recovery_audit.TASK267_ATTESTATION_SHA256 + ".json"
            )
            historical_attestation.write_bytes(b"synthetic attestation")
            historical_attestation.chmod(0o600)
            attestation_hash = hashlib.sha256(historical_attestation.read_bytes()).hexdigest()
            historical_review = audit / (
                "task262-review-" + recovery_audit.TASK267_REVIEW_SHA256 + ".json"
            )
            historical_review.write_bytes(b'{"historical":true}\n')
            historical_review.chmod(0o600)
            historical_review_hash = hashlib.sha256(historical_review.read_bytes()).hexdigest()
            recovery_review_raw = json.dumps(
                {"observations": {"release_target": f"/opt/wow-forever-armory-bot/releases/{previous}"}},
                sort_keys=True,
            ).encode()
            report_bytes = b"synthetic pinned verification report"
            report_hash = hashlib.sha256(report_bytes).hexdigest()
            report_copy = audit / f"task267-verification-{report_hash}.json"
            report_copy.write_bytes(report_bytes)
            report_copy.chmod(0o600)
            manifest = {
                "approved_release_sha": release,
                "previous_release_sha": previous,
                "database_backup": backup_path,
                "database_backup_sha256": "c" * 64,
                "shared_preflight_backup": preflight_path,
                "shared_preflight_sha256": preflight_hash,
                "created_at_utc": "2026-11-14T20:37:39+00:00",
            }
            manifest_raw = (json.dumps(manifest, sort_keys=True) + "\n").encode()
            (audit / f"deployment-{release}.json").write_bytes(manifest_raw)
            (audit / f"deployment-{release}.json").chmod(0o600)
            journal = {
                "deployment_pin_sha256": "d" * 64,
                "approved_release_sha": release,
                "previous_release_sha": previous,
                "phase": "manual_recovery_required",
                "fail_closed_verified": True,
                "fail_closed_error": None,
                "database_signature": self.historical,
                "database_backup": backup_path,
                "database_backup_sha256": "c" * 64,
                "shared_preflight_backup": preflight_path,
                "shared_preflight_sha256": preflight_hash,
                "manifest": str(audit / f"deployment-{release}.json"),
                "service_state": ["active", "enabled"],
                "backup_timer_state": ["inactive", "disabled"],
            }
            journal_raw = (json.dumps(journal, sort_keys=True) + "\n").encode()
            journal_hash = hashlib.sha256(journal_raw).hexdigest()
            recovery_review_path = audit / (
                f"recovery-review-{journal_hash}-"
                f"{hashlib.sha256(recovery_review_raw).hexdigest()}.json"
            )
            historical_collision_path = audit / f"recovery-review-{journal_hash}.json"
            (audit / f"deployment-state-{journal_hash}.json").write_bytes(journal_raw)
            (audit / f"deployment-state-{journal_hash}.json").chmod(0o600)
            manifest_hash = hashlib.sha256(manifest_raw).hexdigest()
            addendum = dict(self.addendum)
            addendum["journal_sha256"] = journal_hash
            addendum["manifest_sha256"] = manifest_hash
            addendum["historical_database_signature"] = self.historical
            addendum["verified_database_signature"] = self.verified
            addendum["backup_sha256"] = "c" * 64
            addendum["saved_preflight_sha256"] = preflight_hash
            addendum["latest_backup_sha256"] = "e" * 64
            addendum["verification_report_sha256"] = report_hash
            addendum["historical_attestation_sha256"] = attestation_hash
            addendum["historical_review_sha256"] = historical_review_hash
            addendum["recovery_release_sha"] = previous
            addendum["recovery_review_sha256"] = hashlib.sha256(recovery_review_raw).hexdigest()
            addendum["approved_by"] = "independent-reviewer"
            addendum["approval_reference"] = "PO-TASK267-REVIEW"
            addendum["approved_at_utc"] = "2026-11-22T10:00:00+00:00"
            addendum_raw = (json.dumps(addendum, sort_keys=True) + "\n").encode()
            addendum_hash = hashlib.sha256(addendum_raw).hexdigest()
            (audit / f"recovery-addendum-{journal_hash}.json").write_bytes(addendum_raw)
            (audit / f"recovery-addendum-{journal_hash}.json").chmod(0o600)
            historical_collision_path.write_bytes(b'{"historical":true}\n')
            historical_collision_path.chmod(0o600)
            recovery_review_path.write_bytes(recovery_review_raw)
            recovery_review_path.chmod(0o600)
            ack = {
                "schema_version": 2,
                "journal_sha256": journal_hash,
                "manifest_sha256": manifest_hash,
                "approved_release_sha": release,
                "previous_release_sha": previous,
                "decision": "manual_recovery_verified_with_addendum",
                "approved_by": addendum["approved_by"],
                "approval_reference": addendum["approval_reference"],
                "approved_at_utc": addendum["approved_at_utc"],
                "recovery_addendum_sha256": addendum_hash,
            }
            ack_path = audit / f"deployment-recovery-ack-{journal_hash}.json"
            ack_path.write_text(json.dumps(ack, sort_keys=True) + "\n")
            ack_path.chmod(0o600)
            constants = {
                "TASK267_JOURNAL_SHA256": journal_hash,
                "TASK267_MANIFEST_SHA256": manifest_hash,
                "TASK267_INCIDENT_BACKUP_SHA256": "c" * 64,
                "TASK267_LATEST_BACKUP_SHA256": "e" * 64,
                "TASK267_PREFLIGHT_SHA256": preflight_hash,
                "TASK267_ATTESTATION_SHA256": attestation_hash,
                "TASK267_REVIEW_SHA256": historical_review_hash,
                "TASK267_REPORT_SHA256": report_hash,
                "TASK267_REPORT_PATH": report_copy,
            }
            with patch.multiple(recovery_audit, **constants), patch.object(
                recovery_audit, "_verify_task267_live_files", return_value=None
            ):
                recovery_audit.verify_audit_directory(
                    audit, owner=owner, group=group, acquire_lock=False
                )
                self.assertEqual(
                    hashlib.sha256(historical_collision_path.read_bytes()).hexdigest(),
                    historical_review_hash,
                )
                wrong_bound_path = audit / (
                    f"recovery-review-{journal_hash}-{'0' * 64}.json"
                )
                recovery_review_path.rename(wrong_bound_path)
                with self.assertRaisesRegex(
                    recovery_audit.AuditError, "safely|review"
                ):
                    recovery_audit.verify_audit_directory(
                        audit, owner=owner, group=group, acquire_lock=False
                    )
                wrong_bound_path.rename(recovery_review_path)
                activation_id = "f" * 32
                intended = {
                    "wow-forever-armory-bot.service": ["active", "enabled"],
                    "wow-forever-armory-backup.timer": ["inactive", "disabled"],
                }
                actions = recovery_audit._expected_activation_actions(intended)
                activation_approved_by = "activation-admin"
                activation_reference = "PO-TASK268-ACTIVATE"
                event_base = {
                    "journal_sha256": journal_hash,
                    "activation_id": activation_id,
                    "attempt_number": 1,
                    "deployment_id": journal.get("deployment_id"),
                    "release_sha": release,
                    "completion_ack_sha256": hashlib.sha256(ack_path.read_bytes()).hexdigest(),
                    "manifest_sha256": manifest_hash,
                    "installation_result_sha256": None,
                    "deployment_pin_sha256": journal["deployment_pin_sha256"],
                    "approved_by": activation_approved_by,
                    "approval_reference": activation_reference,
                    "intended_states": intended,
                    "owner_uid": owner,
                    "owner_gid": group,
                }
                audit_fd = os.open(audit, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    admin._append_activation_event(
                        audit_fd,
                        sequence=0,
                        event_type="approved",
                        observed_states=intended,
                        **event_base,
                    )
                    for index, action in enumerate(actions):
                        sequence = 1 + index * 2
                        admin._append_activation_event(
                            audit_fd,
                            sequence=sequence,
                            event_type="action_planned",
                            action=action,
                            observed_states=intended,
                            **event_base,
                        )
                        observed = dict(intended)
                        admin._append_activation_event(
                            audit_fd,
                            sequence=sequence + 1,
                            event_type="action_verified",
                            action=action,
                            observed_states=observed,
                            **event_base,
                        )
                    transition = {
                        "schema_version": 1,
                        "from_state": "RECOVERY_APPROVED",
                        "to_state": "NORMAL_OPERATION",
                        "journal_sha256": journal_hash,
                        "acknowledgment_sha256": hashlib.sha256(ack_path.read_bytes()).hexdigest(),
                        "addendum_sha256": addendum_hash,
                        "verification_report_sha256": report_hash,
                        "database_sha256": addendum["database_sha256"],
                        "recovery_approved_by": addendum["approved_by"],
                        "recovery_approval_reference": addendum["approval_reference"],
                        "activation_approved_by": activation_approved_by,
                        "activation_approval_reference": activation_reference,
                        "activation_id": activation_id,
                        "attempt_number": 1,
                        "release_sha": previous,
                    }
                    admin._append_activation_event(
                        audit_fd,
                        sequence=len(actions) * 2 + 1,
                        event_type="success",
                        observed_states=intended,
                        lifecycle_transition=transition,
                        **event_base,
                    )
                finally:
                    os.close(audit_fd)
                transition_flags = []
                def capture_lifecycle(_addendum, *, normal_operation=False):
                    transition_flags.append(normal_operation)
                with patch.object(
                    recovery_audit,
                    "_verify_task267_live_files",
                    side_effect=capture_lifecycle,
                ):
                    recovery_audit.verify_audit_directory(
                        audit,
                        owner=owner,
                        group=group,
                        acquire_lock=False,
                        activation_release=previous,
                    )
                self.assertEqual(transition_flags, [True])
                with patch.object(
                    recovery_audit,
                    "_verify_task267_live_files",
                    return_value=None,
                ), self.assertRaisesRegex(
                    recovery_audit.AuditError, "no activation authorization"
                ):
                    recovery_audit.verify_audit_directory(
                        audit,
                        owner=owner,
                        group=group,
                        acquire_lock=False,
                        activation_release=release,
                    )
                success_path = audit / (
                    f"service-activation-{journal_hash}-{activation_id}-0009.json"
                )
                success_raw = success_path.read_bytes()
                success_event = json.loads(success_raw)
                success_event["lifecycle_transition"]["to_state"] = "INCIDENT"
                success_path.write_text(
                    json.dumps(success_event, sort_keys=True, separators=(",", ":"))
                    + "\n"
                )
                success_path.chmod(0o600)
                with patch.object(
                    recovery_audit,
                    "_verify_task267_live_files",
                    return_value=None,
                ), self.assertRaisesRegex(
                    recovery_audit.AuditError, "lifecycle transition"
                ):
                    recovery_audit.verify_audit_directory(
                        audit, owner=owner, group=group, acquire_lock=False
                    )
                success_path.write_bytes(success_raw)
                success_path.chmod(0o600)

                verified_action_path = audit / (
                    f"service-activation-{journal_hash}-{activation_id}-0002.json"
                )
                verified_action_raw = verified_action_path.read_bytes()
                verified_action_path.unlink()
                with patch.object(
                    recovery_audit,
                    "_verify_task267_live_files",
                    return_value=None,
                ), self.assertRaisesRegex(
                    recovery_audit.AuditError, "activation event sequence"
                ):
                    recovery_audit.verify_audit_directory(
                        audit, owner=owner, group=group, acquire_lock=False
                    )
                verified_action_path.write_bytes(verified_action_raw)
                verified_action_path.chmod(0o600)
                ack_bytes = ack_path.read_bytes()
                ack_path.unlink()
                with self.assertRaisesRegex(recovery_audit.AuditError, "acknowledgment"):
                    recovery_audit.verify_audit_directory(
                        audit, owner=owner, group=group, acquire_lock=False
                    )
                ack_path.write_bytes(ack_bytes)
                ack_path.chmod(0o600)
                addendum_path = audit / f"recovery-addendum-{journal_hash}.json"
                addendum_bytes = addendum_path.read_bytes()
                addendum_path.unlink()
                with self.assertRaisesRegex(recovery_audit.AuditError, "addendum"):
                    recovery_audit.verify_audit_directory(
                        audit, owner=owner, group=group, acquire_lock=False
                    )
                addendum_path.write_bytes(addendum_bytes)
                addendum_path.chmod(0o600)
                addendum_path.write_bytes(addendum_path.read_bytes() + b" ")
                with self.assertRaisesRegex(recovery_audit.AuditError, "addendum hash"):
                    recovery_audit.verify_audit_directory(
                        audit, owner=owner, group=group, acquire_lock=False
                    )

    def test_end_to_end_audit_chain_rejects_addendum_tampering(self):
        altered = dict(self.addendum)
        altered["historical_eagain"] = "resolved"
        with self.assertRaises(recovery_audit.AuditError):
            self._audit_validate(altered)

    def test_addendum_json_hash_is_content_sensitive(self):
        raw = json.dumps(self.addendum, sort_keys=True).encode("ascii")
        changed = raw + b" "
        self.assertNotEqual(hashlib.sha256(raw).digest(), hashlib.sha256(changed).digest())


if __name__ == "__main__":
    unittest.main()
