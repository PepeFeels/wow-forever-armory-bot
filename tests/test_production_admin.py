"""Security tests for pin provisioning and hash-bound recovery administration."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import py_compile
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

from deploy import production_admin as admin
from deploy import recovery_audit


def _load_start_preflight():
    source = Path(__file__).parents[1] / "deploy/systemd/armory-db-preflight.py"
    loader = SourceFileLoader("armory_start_preflight", str(source))
    spec = importlib.util.spec_from_loader("armory_start_preflight", loader)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load startup preflight")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


start_preflight = _load_start_preflight()


class ProductionAdminTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.owner = os.getuid()
        self.group = os.getgid()

    def _provision_pin(self, path: Path) -> bytes:
        return admin.provision_pin(
            path,
            release_sha="a" * 40,
            previous_sha="b" * 40,
            installer_sha256="c" * 64,
            owner_uid=self.owner,
            owner_gid=self.group,
            parent_mode=0o700,
        )

    def _make_release(self, root: Path) -> Path:
        (root / ".venv/bin").mkdir(parents=True, mode=0o755)
        (root / ".installation-complete").write_text(root.name + "\n")
        (root / ".installation-complete").chmod(0o444)
        interpreter = root / ".venv/bin/python"
        interpreter.write_bytes(b"interpreter")
        interpreter.chmod(0o555)
        return root

    def _verify_release(
        self,
        root: Path,
        *,
        owner_uid: int | None = None,
        system_python: Path = admin.SYSTEM_PYTHON,
    ) -> None:
        admin._verify_release_tree(
            root,
            service_uid=self.owner,
            service_gid=self.group,
            owner_uid=self.owner if owner_uid is None else owner_uid,
            system_python=system_python,
        )

    def _completion_fixture(self, audit: Path) -> tuple[str, dict[str, object]]:
        release, previous, deployment_id = "a" * 40, "b" * 40, "d" * 32
        manifest = {
            "approved_release_sha": release,
            "previous_release_sha": previous,
            "database_backup": "/backup/database.sqlite3",
            "database_backup_sha256": "c" * 64,
            "shared_preflight_backup": str(audit / "preflight.py"),
            "shared_preflight_sha256": "e" * 64,
            "created_at_utc": "2026-11-15T12:00:00+00:00",
        }
        manifest_raw = json.dumps(manifest, sort_keys=True).encode() + b"\n"
        manifest_path = audit / f"deployment-{release}.json"
        manifest_path.write_bytes(manifest_raw)
        manifest_path.chmod(0o600)
        result = {
            "schema_version": 1,
            "deployment_id": deployment_id,
            "approved_release_sha": release,
            "previous_release_sha": previous,
            "manifest": str(manifest_path),
            "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "database_backup": manifest["database_backup"],
            "database_backup_sha256": manifest["database_backup_sha256"],
            "shared_preflight_backup": manifest["shared_preflight_backup"],
            "shared_preflight_sha256": manifest["shared_preflight_sha256"],
            "installer_sha256": "f" * 64,
            "deployment_pin_sha256": "9" * 64,
            "database_signature": [1, 2, 3, 4, 5, 6, 7, 8],
            "intended_service_state": ["active", "enabled"],
            "intended_backup_timer_state": ["active", "enabled"],
            "observed_service_states": {
                "wow-forever-armory-bot.service": ["inactive", "disabled"],
                "wow-forever-armory-backup.timer": ["inactive", "disabled"],
                "wow-forever-armory-backup.service": ["inactive", "static"],
            },
            "verified_at_utc": "2026-11-15T12:10:00+00:00",
        }
        result_raw = json.dumps(result, sort_keys=True).encode() + b"\n"
        result_path = audit / f"deployment-installation-{deployment_id}.json"
        result_path.write_bytes(result_raw)
        result_path.chmod(0o600)
        journal = {
            "deployment_id": deployment_id,
            "deployment_pin_installer_sha256": "f" * 64,
            "deployment_pin_sha256": "9" * 64,
            "approved_release_sha": release,
            "previous_release_sha": previous,
            "phase": "installation_verified",
            "installer_exit_code": 0,
            "service_state": ["active", "enabled"],
            "backup_timer_state": ["active", "enabled"],
            "database_signature": result["database_signature"],
            "database_backup": manifest["database_backup"],
            "database_backup_sha256": manifest["database_backup_sha256"],
            "shared_preflight_backup": manifest["shared_preflight_backup"],
            "shared_preflight_sha256": manifest["shared_preflight_sha256"],
            "manifest": str(manifest_path),
            "installation_result": result_path.name,
            "installation_result_sha256": hashlib.sha256(result_raw).hexdigest(),
        }
        journal_raw = json.dumps(journal, sort_keys=True).encode() + b"\n"
        active = audit / "deployment-state.json"
        active.write_bytes(journal_raw)
        active.chmod(0o600)
        return hashlib.sha256(journal_raw).hexdigest(), journal

    def test_finalize_deployment_archives_then_acknowledges_and_is_idempotent(self):
        audit = self.root / "audit"
        audit.mkdir(mode=0o700)
        source_hash, journal = self._completion_fixture(audit)
        states = {
            "wow-forever-armory-bot.service": ["inactive", "disabled"],
            "wow-forever-armory-backup.timer": ["inactive", "disabled"],
            "wow-forever-armory-backup.service": ["inactive", "static"],
        }
        verified = []
        result = admin.finalize_deployment(
            audit,
            expected_journal_sha256=source_hash,
            approved_by="release-admin",
            approval_reference="CHANGE-199D",
            owner_uid=self.owner,
            owner_gid=self.group,
            state_reader=lambda: states,
            evidence_verifier=lambda *args: None,
            history_verifier=lambda path, uid, gid: (
                recovery_audit.verify_audit_directory(
                    path, owner=uid, group=gid, acquire_lock=False
                ),
                verified.append(True),
            ),
        )
        journal_sha, manifest_sha = result
        self.assertEqual(len(journal_sha), 64)
        self.assertEqual(
            manifest_sha,
            hashlib.sha256(
                (audit / f"deployment-{'a' * 40}.json").read_bytes()
            ).hexdigest(),
        )
        self.assertFalse((audit / "deployment-state.json").exists())
        archive = audit / f"deployment-state-{journal_sha}.json"
        ack_path = audit / f"deployment-recovery-ack-{journal_sha}.json"
        archived = json.loads(archive.read_text())
        acknowledgment = json.loads(ack_path.read_text())
        self.assertEqual(archived["phase"], "complete")
        self.assertEqual(archived["completion_approval_reference"], "CHANGE-199D")
        self.assertEqual(acknowledgment["decision"], "deployment_completion_verified")
        self.assertEqual(acknowledgment["journal_sha256"], journal_sha)
        self.assertEqual(verified, [True])

    def test_finalize_deployment_resumes_after_archive_before_active_removal(self):
        audit = self.root / "audit"
        audit.mkdir(mode=0o700)
        source_hash, _ = self._completion_fixture(audit)
        states = {
            "wow-forever-armory-bot.service": ["inactive", "disabled"],
            "wow-forever-armory-backup.timer": ["inactive", "disabled"],
            "wow-forever-armory-backup.service": ["inactive", "static"],
        }

        def interrupt(point):
            if point == "archive_verified":
                raise RuntimeError("simulated interruption")

        with self.assertRaisesRegex(RuntimeError, "simulated"):
            admin.finalize_deployment(
                audit,
                expected_journal_sha256=source_hash,
                approved_by="release-admin",
                approval_reference="CHANGE-199D",
                owner_uid=self.owner,
                owner_gid=self.group,
                state_reader=lambda: states,
                evidence_verifier=lambda *args: None,
                history_verifier=lambda *args: None,
                checkpoint=interrupt,
            )
        self.assertTrue((audit / "deployment-state.json").exists())
        archives = list(audit.glob("deployment-state-*.json"))
        self.assertEqual(len(archives), 1)
        self.assertFalse(list(audit.glob("deployment-recovery-ack-*.json")))
        completed = admin.finalize_deployment(
            audit,
            expected_journal_sha256=source_hash,
            approved_by="release-admin",
            approval_reference="CHANGE-199D",
            owner_uid=self.owner,
            owner_gid=self.group,
            state_reader=lambda: states,
            evidence_verifier=lambda *args: None,
            history_verifier=lambda *args: None,
        )
        self.assertFalse((audit / "deployment-state.json").exists())
        self.assertTrue(
            (audit / f"deployment-recovery-ack-{completed[0]}.json").is_file()
        )

    def test_finalize_deployment_does_not_acknowledge_historical_incident(self):
        audit = self.root / "audit"
        audit.mkdir(mode=0o700)
        journal = {
            "approved_release_sha": "a" * 40,
            "previous_release_sha": "b" * 40,
            "phase": "manual_recovery_required",
        }
        raw = json.dumps(journal).encode() + b"\n"
        active = audit / "deployment-state.json"
        active.write_bytes(raw)
        active.chmod(0o600)
        with self.assertRaises(admin.AdminError):
            admin.finalize_deployment(
                audit,
                expected_journal_sha256=hashlib.sha256(raw).hexdigest(),
                approved_by="release-admin",
                approval_reference="CHANGE-199D",
                owner_uid=self.owner,
                owner_gid=self.group,
                evidence_verifier=lambda *args: None,
                history_verifier=lambda *args: None,
            )
        self.assertEqual(active.read_bytes(), raw)
        self.assertFalse(list(audit.glob("deployment-recovery-ack-*.json")))

    def test_completion_evidence_rechecks_pin_release_database_and_artifact_hashes(
        self,
    ):
        release, previous, deployment_id = "a" * 40, "b" * 40, "d" * 32
        audit = self.root / "audit"
        audit.mkdir(mode=0o700)
        base = self.root / "install"
        target = base / "releases" / release
        target.mkdir(parents=True)
        marker = target / ".installation-complete"
        marker.write_text(release + "\n")
        marker.chmod(0o444)
        (base / "current").symlink_to(f"releases/{release}")
        database = self.root / "database.sqlite3"
        database.write_bytes(b"database")
        database.chmod(0o600)
        info = database.lstat()
        signature = [
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            info.st_uid,
            info.st_gid,
            info.st_mode,
        ]
        backup = "/var/lib/wow-forever-armory-bot/backups/guild-armory-test.sqlite3"
        saved_preflight = str(audit / f"armory-db-preflight.{previous}.py")
        manifest = {
            "approved_release_sha": release,
            "previous_release_sha": previous,
            "database_backup": backup,
            "database_backup_sha256": "c" * 64,
            "shared_preflight_backup": saved_preflight,
            "shared_preflight_sha256": "e" * 64,
            "created_at_utc": "2026-11-15T12:00:00+00:00",
        }
        manifest_raw = json.dumps(manifest, sort_keys=True).encode() + b"\n"
        manifest_path = audit / f"deployment-{release}.json"
        manifest_path.write_bytes(manifest_raw)
        manifest_path.chmod(0o600)
        journal = {
            "deployment_id": deployment_id,
            "phase": "installation_verified",
            "installer_exit_code": 0,
            "approved_release_sha": release,
            "previous_release_sha": previous,
            "service_state": ["active", "enabled"],
            "backup_timer_state": ["active", "enabled"],
            "deployment_pin_installer_sha256": "f" * 64,
            "deployment_pin_sha256": hashlib.sha256(
                json.dumps(
                    {
                        "release_sha": release,
                        "previous_sha": previous,
                        "installer_sha256": "f" * 64,
                    }
                ).encode("ascii")
            ).hexdigest(),
            "database_signature": signature,
            "database_backup": backup,
            "database_backup_sha256": "c" * 64,
            "shared_preflight_backup": saved_preflight,
            "shared_preflight_sha256": "e" * 64,
            "manifest": str(manifest_path),
        }
        report = {
            "schema_version": 1,
            "deployment_id": deployment_id,
            "approved_release_sha": release,
            "previous_release_sha": previous,
            "manifest": journal["manifest"],
            "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "database_backup": backup,
            "database_backup_sha256": "c" * 64,
            "shared_preflight_backup": saved_preflight,
            "shared_preflight_sha256": "e" * 64,
            "installer_sha256": "f" * 64,
            "deployment_pin_sha256": journal["deployment_pin_sha256"],
            "database_signature": signature,
            "intended_service_state": ["active", "enabled"],
            "intended_backup_timer_state": ["active", "enabled"],
            "observed_service_states": {
                "wow-forever-armory-bot.service": ["inactive", "disabled"],
                "wow-forever-armory-backup.timer": ["inactive", "disabled"],
                "wow-forever-armory-backup.service": ["inactive", "static"],
            },
            "verified_at_utc": "2026-11-15T12:10:00+00:00",
        }
        pin_dir = self.root / "etc"
        pin_dir.mkdir(mode=0o750)
        pin_dir.chmod(0o750)
        pin_path = pin_dir / "deployment-pin.json"
        pin_path.write_bytes(
            json.dumps(
                {
                    "release_sha": release,
                    "previous_sha": previous,
                    "installer_sha256": "f" * 64,
                }
            ).encode("ascii")
        )
        pin_path.chmod(0o600)
        installer = self.root / "production_install.py"
        installer.write_text("source\n", encoding="ascii")
        installer.chmod(0o755)
        snapshot_helper = type(
            "SnapshotHelper",
            (),
            {
                "verify_database_snapshot": staticmethod(
                    lambda *args, **kwargs: (1, 1, 1)
                )
            },
        )()
        fake_user = type("User", (), {"pw_uid": self.owner})()
        fake_group = type("Group", (), {"gr_gid": self.group})()
        hashes = {
            backup: "c" * 64,
            saved_preflight: "e" * 64,
            str(installer): "f" * 64,
        }
        with (
            patch.object(admin, "COMPLETION_BASE", base),
            patch.object(admin, "COMPLETION_DATABASE", database),
            patch.object(admin, "COMPLETION_INSTALLER", installer),
            patch.object(admin, "PIN_PATH", pin_path),
            patch.object(admin, "_verify_release_tree"),
            patch.object(admin, "_validate_manifest"),
            patch.object(
                admin,
                "_hash_regular_file",
                side_effect=lambda path, **kwargs: hashes[str(path)],
            ),
            patch.object(admin.pwd, "getpwnam", return_value=fake_user),
            patch.object(admin.grp, "getgrnam", return_value=fake_group),
            patch.object(
                admin,
                "_systemd_states",
                return_value={
                    "wow-forever-armory-bot.service": ["inactive", "disabled"],
                    "wow-forever-armory-backup.timer": ["inactive", "disabled"],
                    "wow-forever-armory-backup.service": ["inactive", "static"],
                },
            ),
            patch.object(
                admin,
                "load_reviewed_python_source",
                return_value=snapshot_helper,
            ),
        ):
            admin._verify_completion_evidence(
                journal, report, manifest, audit, manifest_raw, owner_uid=self.owner
            )
            tampered = dict(report, installer_sha256="0" * 64)
            with self.assertRaisesRegex(admin.AdminError, "contradicts"):
                admin._verify_completion_evidence(
                    journal,
                    tampered,
                    manifest,
                    audit,
                    manifest_raw,
                    owner_uid=self.owner,
                )
            tampered_pin = dict(report, deployment_pin_sha256="0" * 64)
            with self.assertRaisesRegex(admin.AdminError, "contradicts"):
                admin._verify_completion_evidence(
                    journal,
                    tampered_pin,
                    manifest,
                    audit,
                    manifest_raw,
                    owner_uid=self.owner,
                )
            original_pin = pin_path.read_bytes()
            pin_path.write_bytes(original_pin + b" ")
            with self.assertRaisesRegex(admin.AdminError, "pin or installed installer"):
                admin._verify_completion_evidence(
                    journal,
                    report,
                    manifest,
                    audit,
                    manifest_raw,
                    owner_uid=self.owner,
                )
            pin_path.write_bytes(original_pin)
            database.write_bytes(b"changed database")
            with self.assertRaisesRegex(admin.AdminError, "database changed"):
                admin._verify_completion_evidence(
                    journal, report, manifest, audit, manifest_raw, owner_uid=self.owner
                )

    def test_manifest_accepts_active_backup_directory_but_rejects_other_nesting(
        self,
    ):
        release, previous = "a" * 40, "b" * 40
        audit = self.root / "audit"
        audit.mkdir(mode=0o700)
        backup = (
            "/var/lib/wow-forever-armory-bot/backups/active/"
            "guild-armory-test.sqlite3"
        )
        preflight = str(audit / f"armory-db-preflight.{previous}.py")
        journal = {
            "database_backup": backup,
            "database_backup_sha256": "c" * 64,
            "shared_preflight_backup": preflight,
            "shared_preflight_sha256": "d" * 64,
        }
        manifest = {
            "approved_release_sha": release,
            "previous_release_sha": previous,
            **journal,
        }
        admin._validate_manifest(journal, manifest, release, previous, audit)
        unexpected = (
            "/var/lib/wow-forever-armory-bot/backups/unexpected/"
            "guild-armory-test.sqlite3"
        )
        journal["database_backup"] = unexpected
        manifest["database_backup"] = unexpected
        with self.assertRaisesRegex(admin.AdminError, "artifact paths"):
            admin._validate_manifest(journal, manifest, release, previous, audit)

    def test_finalize_deployment_resumes_after_active_name_removal(self):
        audit = self.root / "audit"
        audit.mkdir(mode=0o700)
        source_hash, _ = self._completion_fixture(audit)
        states = {
            "wow-forever-armory-bot.service": ["inactive", "disabled"],
            "wow-forever-armory-backup.timer": ["inactive", "disabled"],
            "wow-forever-armory-backup.service": ["inactive", "static"],
        }

        def interrupt(point):
            if point == "active_removed":
                raise RuntimeError("simulated interruption")

        with self.assertRaisesRegex(RuntimeError, "simulated"):
            admin.finalize_deployment(
                audit,
                expected_journal_sha256=source_hash,
                approved_by="release-admin",
                approval_reference="CHANGE-199D",
                owner_uid=self.owner,
                owner_gid=self.group,
                state_reader=lambda: states,
                evidence_verifier=lambda *args: None,
                history_verifier=lambda *args: None,
                checkpoint=interrupt,
            )
        self.assertFalse((audit / "deployment-state.json").exists())
        self.assertEqual(len(list(audit.glob("deployment-state-*.json"))), 1)
        self.assertFalse(list(audit.glob("deployment-recovery-ack-*.json")))
        journal_sha, _ = admin.finalize_deployment(
            audit,
            expected_journal_sha256=source_hash,
            approved_by="release-admin",
            approval_reference="CHANGE-199D",
            owner_uid=self.owner,
            owner_gid=self.group,
            state_reader=lambda: states,
            evidence_verifier=lambda *args: None,
            history_verifier=lambda path, uid, gid: (
                recovery_audit.verify_audit_directory(
                    path, owner=uid, group=gid, acquire_lock=False
                )
            ),
        )
        self.assertTrue(
            (audit / f"deployment-recovery-ack-{journal_sha}.json").is_file()
        )

    def test_finalize_refuses_conflicting_ack_after_archive_removal(self):
        audit = self.root / "audit"
        audit.mkdir(mode=0o700)
        source_hash, _ = self._completion_fixture(audit)
        states = {
            "wow-forever-armory-bot.service": ["inactive", "disabled"],
            "wow-forever-armory-backup.timer": ["inactive", "disabled"],
            "wow-forever-armory-backup.service": ["inactive", "static"],
        }

        def interrupt(point):
            if point == "active_removed":
                raise RuntimeError("simulated interruption")

        with self.assertRaises(RuntimeError):
            admin.finalize_deployment(
                audit,
                expected_journal_sha256=source_hash,
                approved_by="release-admin",
                approval_reference="CHANGE-199D",
                owner_uid=self.owner,
                owner_gid=self.group,
                state_reader=lambda: states,
                evidence_verifier=lambda *args: None,
                history_verifier=lambda *args: None,
                checkpoint=interrupt,
            )
        archive = next(audit.glob("deployment-state-*.json"))
        final_sha = archive.stem.removeprefix("deployment-state-")
        ack = audit / f"deployment-recovery-ack-{final_sha}.json"
        ack.write_text("{}\n")
        ack.chmod(0o600)
        before = ack.read_bytes()
        with self.assertRaisesRegex(admin.AdminError, "conflicts"):
            admin.finalize_deployment(
                audit,
                expected_journal_sha256=source_hash,
                approved_by="release-admin",
                approval_reference="CHANGE-199D",
                owner_uid=self.owner,
                owner_gid=self.group,
                state_reader=lambda: states,
                evidence_verifier=lambda *args: None,
                history_verifier=lambda *args: None,
            )
        self.assertEqual(ack.read_bytes(), before)

    def test_finalize_resumes_after_atomic_journal_replace_failure(self):
        audit = self.root / "audit"
        audit.mkdir(mode=0o700)
        source_hash, _ = self._completion_fixture(audit)
        states = {
            "wow-forever-armory-bot.service": ["inactive", "disabled"],
            "wow-forever-armory-backup.timer": ["inactive", "disabled"],
            "wow-forever-armory-backup.service": ["inactive", "static"],
        }
        real_replace = os.replace

        def fail_replace_once(*args, **kwargs):
            if not getattr(fail_replace_once, "failed", False):
                fail_replace_once.failed = True
                raise OSError("simulated atomic rename failure")
            return real_replace(*args, **kwargs)

        with patch.object(admin.os, "replace", side_effect=fail_replace_once):
            with self.assertRaises(admin.AdminError):
                admin.finalize_deployment(
                    audit,
                    expected_journal_sha256=source_hash,
                    approved_by="release-admin",
                    approval_reference="CHANGE-199D",
                    owner_uid=self.owner,
                    owner_gid=self.group,
                    state_reader=lambda: states,
                    evidence_verifier=lambda *args: None,
                    history_verifier=lambda *args: None,
                )
        self.assertEqual(
            json.loads((audit / "deployment-state.json").read_text())["phase"],
            "installation_verified",
        )
        self.assertTrue((audit / ".deployment-state.json.tmp").is_file())
        result = admin.finalize_deployment(
            audit,
            expected_journal_sha256=source_hash,
            approved_by="release-admin",
            approval_reference="CHANGE-199D",
            owner_uid=self.owner,
            owner_gid=self.group,
            state_reader=lambda: states,
            evidence_verifier=lambda *args: None,
            history_verifier=lambda path, uid, gid: (
                recovery_audit.verify_audit_directory(
                    path, owner=uid, group=gid, acquire_lock=False
                )
            ),
        )
        self.assertFalse((audit / ".deployment-state.json.tmp").exists())
        self.assertTrue((audit / f"deployment-recovery-ack-{result[0]}.json").is_file())

    def test_finalize_resumes_after_ack_publication(self):
        audit = self.root / "audit"
        audit.mkdir(mode=0o700)
        source_hash, _ = self._completion_fixture(audit)
        states = {
            "wow-forever-armory-bot.service": ["inactive", "disabled"],
            "wow-forever-armory-backup.timer": ["inactive", "disabled"],
            "wow-forever-armory-backup.service": ["inactive", "static"],
        }

        def interrupt(point):
            if point == "ack_published":
                raise RuntimeError("simulated interruption")

        with self.assertRaisesRegex(RuntimeError, "simulated"):
            admin.finalize_deployment(
                audit,
                expected_journal_sha256=source_hash,
                approved_by="release-admin",
                approval_reference="CHANGE-199D",
                owner_uid=self.owner,
                owner_gid=self.group,
                state_reader=lambda: states,
                evidence_verifier=lambda *args: None,
                history_verifier=lambda *args: None,
                checkpoint=interrupt,
            )
        self.assertFalse((audit / "deployment-state.json").exists())
        ack = next(audit.glob("deployment-recovery-ack-*.json"))
        before = ack.read_bytes()
        journal_sha, _ = admin.finalize_deployment(
            audit,
            expected_journal_sha256=source_hash,
            approved_by="release-admin",
            approval_reference="CHANGE-199D",
            owner_uid=self.owner,
            owner_gid=self.group,
            state_reader=lambda: states,
            evidence_verifier=lambda *args: None,
            history_verifier=lambda path, uid, gid: (
                recovery_audit.verify_audit_directory(
                    path, owner=uid, group=gid, acquire_lock=False
                )
            ),
        )
        self.assertEqual(ack.name, f"deployment-recovery-ack-{journal_sha}.json")
        self.assertEqual(ack.read_bytes(), before)

    def _finalized_completion_fixture(self) -> tuple[Path, str, Path]:
        audit = self.root / "audit"
        audit.mkdir(mode=0o700)
        source_hash, _ = self._completion_fixture(audit)
        states = {
            "wow-forever-armory-bot.service": ["inactive", "disabled"],
            "wow-forever-armory-backup.timer": ["inactive", "disabled"],
            "wow-forever-armory-backup.service": ["inactive", "static"],
        }
        journal_sha, _ = admin.finalize_deployment(
            audit,
            expected_journal_sha256=source_hash,
            approved_by="release-admin",
            approval_reference="CHANGE-199D",
            owner_uid=self.owner,
            owner_gid=self.group,
            state_reader=lambda: states,
            evidence_verifier=lambda *args: None,
            history_verifier=lambda path, uid, gid: (
                recovery_audit.verify_audit_directory(
                    path, owner=uid, group=gid, acquire_lock=False
                )
            ),
        )
        base = self.root / "installation"
        release = base / "releases" / ("a" * 40)
        release.mkdir(parents=True)
        marker = release / ".installation-complete"
        marker.write_text("a" * 40 + "\n")
        marker.chmod(0o444)
        (base / "current").symlink_to(f"releases/{'a' * 40}")
        lock_dir = self.root / "lock"
        lock_dir.mkdir()
        lock_dir.chmod(0o1777)
        return audit, journal_sha, base

    def _append_test_activation_event(
        self,
        audit: Path,
        journal_sha: str,
        *,
        sequence: int,
        event_type: str,
        action: dict[str, object] | None = None,
        observed: dict[str, list[str]] | None = None,
    ) -> None:
        journal = json.loads(
            (audit / f"deployment-state-{journal_sha}.json").read_text()
        )
        ack_raw = (audit / f"deployment-recovery-ack-{journal_sha}.json").read_bytes()
        release = journal["approved_release_sha"]
        manifest_raw = (audit / f"deployment-{release}.json").read_bytes()
        fd = os.open(audit, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            admin._append_activation_event(
                fd,
                journal_sha256=journal_sha,
                activation_id="e" * 32,
                attempt_number=1,
                sequence=sequence,
                event_type=event_type,
                deployment_id=journal["deployment_id"],
                release_sha=release,
                completion_ack_sha256=hashlib.sha256(ack_raw).hexdigest(),
                manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
                installation_result_sha256=journal["installation_result_sha256"],
                deployment_pin_sha256=journal["deployment_pin_sha256"],
                approved_by="resume-admin",
                approval_reference="RESUME-199F-1",
                intended_states={
                    admin.SERVICE_NAME: journal["service_state"],
                    admin.TIMER_NAME: journal["backup_timer_state"],
                },
                action=action,
                observed_states=observed or {},
                owner_uid=self.owner,
                owner_gid=self.group,
            )
        finally:
            os.close(fd)

    def test_activate_services_resumes_after_bot_start_before_verification(self):
        audit, journal_sha, base = self._finalized_completion_fixture()
        states = {
            admin.SERVICE_NAME: ["active", "enabled"],
            admin.TIMER_NAME: ["inactive", "disabled"],
            admin.BACKUP_SERVICE: ["inactive", "static"],
        }
        bot_enable = {
            "unit": admin.SERVICE_NAME,
            "verb": "enable",
            "target": ["active", "enabled"],
        }
        bot_start = {
            "unit": admin.SERVICE_NAME,
            "verb": "start",
            "target": ["active", "enabled"],
        }
        self._append_test_activation_event(
            audit,
            journal_sha,
            sequence=0,
            event_type="approved",
        )
        self._append_test_activation_event(
            audit,
            journal_sha,
            sequence=1,
            event_type="action_planned",
            action=bot_enable,
        )
        self._append_test_activation_event(
            audit,
            journal_sha,
            sequence=2,
            event_type="action_verified",
            action=bot_enable,
            observed={
                admin.SERVICE_NAME: ["inactive", "enabled"],
                admin.TIMER_NAME: ["inactive", "disabled"],
                admin.BACKUP_SERVICE: ["inactive", "static"],
            },
        )
        self._append_test_activation_event(
            audit,
            journal_sha,
            sequence=3,
            event_type="action_planned",
            action=bot_start,
            observed=states,
        )
        actions = []

        def apply(verb, unit):
            actions.append((verb, unit))
            if verb in {"enable", "disable"}:
                states[unit][1] = "enabled" if verb == "enable" else "disabled"
            else:
                states[unit][0] = "active" if verb == "start" else "inactive"

        with patch.object(admin, "COMPLETION_BASE", base):
            admin.activate_services(
                audit,
                expected_journal_sha256=journal_sha,
                approved_by="resume-admin",
                approval_reference="RESUME-199F-1",
                owner_uid=self.owner,
                owner_gid=self.group,
                validator=lambda *args, **kwargs: None,
                state_reader=lambda: states,
                service_action=apply,
                process_check=lambda _: True,
                database_check=lambda: None,
                activation_lock=self.root / "lock" / "activation.lock",
            )
        self.assertEqual(states[admin.SERVICE_NAME], ["active", "enabled"])
        self.assertEqual(states[admin.TIMER_NAME], ["active", "enabled"])
        self.assertEqual(actions[0], ("start", admin.SERVICE_NAME))
        recovery_audit.verify_audit_directory(
            audit,
            owner=self.owner,
            group=self.group,
            activation_release="a" * 40,
            activation_role="backup",
        )

    def test_start_gate_requires_activation_authorization_and_verified_bot_start(self):
        audit, journal_sha, _ = self._finalized_completion_fixture()
        archive = json.loads(
            (audit / f"deployment-state-{journal_sha}.json").read_text()
        )
        ack_raw = (audit / f"deployment-recovery-ack-{journal_sha}.json").read_bytes()
        manifest_raw = (audit / f"deployment-{'a' * 40}.json").read_bytes()
        intended = {
            admin.SERVICE_NAME: archive["service_state"],
            admin.TIMER_NAME: archive["backup_timer_state"],
        }
        activation_id = "c" * 32
        fields = {
            "journal_sha256": journal_sha,
            "activation_id": activation_id,
            "attempt_number": 1,
            "deployment_id": archive["deployment_id"],
            "release_sha": archive["approved_release_sha"],
            "completion_ack_sha256": hashlib.sha256(ack_raw).hexdigest(),
            "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "installation_result_sha256": archive["installation_result_sha256"],
            "deployment_pin_sha256": archive["deployment_pin_sha256"],
            "approved_by": "release-admin",
            "approval_reference": "ACTIVATE-TEST-1",
            "intended_states": intended,
            "owner_uid": self.owner,
            "owner_gid": self.group,
        }
        states = {
            admin.SERVICE_NAME: ["inactive", "disabled"],
            admin.TIMER_NAME: ["inactive", "disabled"],
            admin.BACKUP_SERVICE: ["inactive", "static"],
        }

        def append(sequence, event, action=None, observed=None):
            fd = os.open(audit, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                admin._append_activation_event(
                    fd,
                    sequence=sequence,
                    event_type=event,
                    action=action,
                    observed_states=observed or {},
                    **fields,
                )
            finally:
                os.close(fd)

        def check(role="bot"):
            return start_preflight.deployment_recovery_is_clear(
                audit,
                expected_owner=self.owner,
                expected_group=self.group,
                activation_release=archive["approved_release_sha"],
                activation_role=role,
                activation_in_progress=lock_held[0],
            )

        lock_held = [False]
        self.assertFalse(check())
        append(0, "approved")
        self.assertFalse(check())
        bot_enable = {
            "unit": admin.SERVICE_NAME,
            "verb": "enable",
            "target": intended[admin.SERVICE_NAME],
        }
        append(1, "action_planned", bot_enable, states)
        states[admin.SERVICE_NAME] = ["inactive", "enabled"]
        append(2, "action_verified", bot_enable, states)
        bot_start = {
            "unit": admin.SERVICE_NAME,
            "verb": "start",
            "target": intended[admin.SERVICE_NAME],
        }
        append(3, "action_planned", bot_start, states)
        self.assertFalse(check())
        lock_held[0] = True
        self.assertTrue(check())
        self.assertFalse(check("backup"))
        states[admin.SERVICE_NAME] = ["active", "enabled"]
        append(4, "action_verified", bot_start, states)
        lock_held[0] = False
        self.assertFalse(check())
        lock_held[0] = True
        self.assertFalse(check())
        self.assertFalse(check("backup"))
        for seq, unit, verb, target in (
            (5, admin.TIMER_NAME, "enable", intended[admin.TIMER_NAME]),
            (7, admin.TIMER_NAME, "start", intended[admin.TIMER_NAME]),
        ):
            action = {"unit": unit, "verb": verb, "target": target}
            append(seq, "action_planned", action, states)
            states[unit] = ["inactive", "enabled"] if verb == "enable" else target
            append(seq + 1, "action_verified", action, states)
        append(9, "success", observed=states)
        lock_held[0] = False
        self.assertTrue(check())
        self.assertTrue(check("backup"))

        (audit / "deployment-state.json").write_text("{}\n", encoding="ascii")
        (audit / "deployment-state.json").chmod(0o600)
        self.assertFalse(check())

    def test_start_gate_only_accepts_incomplete_activation_while_lock_is_held(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "lock-dir"
            parent.mkdir()
            parent.chmod(0o1777)
            lock = parent / "activation.lock"
            lock.touch(mode=0o600)
            lock.chmod(0o600)
            self.assertFalse(
                start_preflight._activation_lock_is_held(
                    lock, expected_owner=os.getuid()
                )
            )
            fd = os.open(lock, os.O_RDONLY)
            try:
                start_preflight.fcntl.flock(fd, start_preflight.fcntl.LOCK_EX)
                self.assertTrue(
                    start_preflight._activation_lock_is_held(
                        lock, expected_owner=os.getuid()
                    )
                )
            finally:
                start_preflight.fcntl.flock(fd, start_preflight.fcntl.LOCK_UN)
                os.close(fd)
            symlink = parent / "activation-link"
            symlink.symlink_to(lock)
            self.assertFalse(
                start_preflight._activation_lock_is_held(
                    symlink, expected_owner=os.getuid()
                )
            )

    def test_database_start_gate_checks_metadata_without_opening_sqlite(self):
        state = self.root / "state"
        state.mkdir(mode=0o700)
        state.chmod(0o700)
        database = state / "guild-armory.sqlite3"
        wal = Path(f"{database}-wal")
        shm = Path(f"{database}-shm")
        database.write_bytes(b"not opened by the start gate")
        wal.write_bytes(b"preserve wal bytes")
        shm.write_bytes(b"preserve shm bytes")
        for path in (database, wal, shm):
            path.chmod(0o600)
        before = {
            path: (
                path.read_bytes(),
                path.stat().st_dev,
                path.stat().st_ino,
                path.stat().st_size,
                path.stat().st_mtime_ns,
                path.stat().st_ctime_ns,
                path.stat().st_mode,
            )
            for path in (database, wal, shm)
        }

        self.assertTrue(
            start_preflight.database_metadata_is_safe(
                database,
                expected_owner=self.owner,
                expected_group=self.group,
            )
        )

        after = {
            path: (
                path.read_bytes(),
                path.stat().st_dev,
                path.stat().st_ino,
                path.stat().st_size,
                path.stat().st_mtime_ns,
                path.stat().st_ctime_ns,
                path.stat().st_mode,
            )
            for path in (database, wal, shm)
        }
        self.assertEqual(after, before)

    def test_database_start_gate_rejects_restore_marker_symlink_and_hardlinks(self):
        state = self.root / "state"
        state.mkdir(mode=0o700)
        state.chmod(0o700)
        database = state / "guild-armory.sqlite3"
        database.write_bytes(b"database")
        database.chmod(0o600)
        marker = Path(f"{database}.restore-in-progress")
        marker.symlink_to(state / "missing-marker-target")
        self.assertFalse(
            start_preflight.database_metadata_is_safe(
                database,
                expected_owner=self.owner,
                expected_group=self.group,
            )
        )
        marker.unlink()

        alias = state / "alias"
        os.link(database, alias)
        self.assertFalse(
            start_preflight.database_metadata_is_safe(
                database,
                expected_owner=self.owner,
                expected_group=self.group,
            )
        )
        alias.unlink()

        symlink = state / "database-link"
        symlink.symlink_to(database)
        self.assertFalse(
            start_preflight.database_metadata_is_safe(
                symlink,
                expected_owner=self.owner,
                expected_group=self.group,
            )
        )

    def test_activate_services_requires_completion_and_restores_saved_states(self):
        audit, journal_sha, base = self._finalized_completion_fixture()
        states = {
            admin.SERVICE_NAME: ["inactive", "disabled"],
            admin.TIMER_NAME: ["inactive", "disabled"],
            admin.BACKUP_SERVICE: ["inactive", "static"],
        }
        actions = []

        def apply(action, unit):
            actions.append((action, unit))
            if action in {"start", "stop"}:
                states[unit][0] = "active" if action == "start" else "inactive"
            elif action in {"enable", "disable"}:
                states[unit][1] = "enabled" if action == "enable" else "disabled"

        reports = []
        with patch.object(admin, "COMPLETION_BASE", base):
            admin.activate_services(
                audit,
                expected_journal_sha256=journal_sha,
                approved_by="release-admin",
                approval_reference="ACTIVATE-199D",
                owner_uid=self.owner,
                owner_gid=self.group,
                validator=lambda *args, **kwargs: None,
                state_reader=lambda: states,
                service_action=apply,
                process_check=lambda _: True,
                database_check=lambda: None,
                activation_lock=self.root / "lock" / "activation.lock",
                report=reports.append,
            )
        self.assertEqual(states[admin.SERVICE_NAME], ["active", "enabled"])
        self.assertEqual(states[admin.TIMER_NAME], ["active", "enabled"])
        self.assertEqual(reports, [reports[0]])
        self.assertIn('"result": "success"', reports[0])
        self.assertEqual(len(actions), 4)
        recovery_audit.verify_audit_directory(
            audit,
            owner=self.owner,
            group=self.group,
            activation_release="a" * 40,
            activation_role="bot",
        )
        recovery_audit.verify_audit_directory(
            audit,
            owner=self.owner,
            group=self.group,
            activation_release="a" * 40,
            activation_role="backup",
        )

    def test_reporting_failure_after_success_does_not_stop_services(self):
        audit, journal_sha, base = self._finalized_completion_fixture()
        states = {
            admin.SERVICE_NAME: ["inactive", "disabled"],
            admin.TIMER_NAME: ["inactive", "disabled"],
            admin.BACKUP_SERVICE: ["inactive", "static"],
        }
        actions = []

        def apply(action, unit):
            actions.append((action, unit))
            if action in {"start", "stop"}:
                states[unit][0] = "active" if action == "start" else "inactive"
            elif action in {"enable", "disable"}:
                states[unit][1] = "enabled" if action == "enable" else "disabled"

        def broken_report(_payload):
            raise BrokenPipeError("simulated closed report pipe")

        with patch.object(admin, "COMPLETION_BASE", base):
            admin.activate_services(
                audit,
                expected_journal_sha256=journal_sha,
                approved_by="release-admin",
                approval_reference="ACTIVATE-REPORT-FAILURE",
                owner_uid=self.owner,
                owner_gid=self.group,
                validator=lambda *args, **kwargs: None,
                state_reader=lambda: states,
                service_action=apply,
                process_check=lambda _: True,
                database_check=lambda: None,
                activation_lock=self.root / "lock" / "activation.lock",
                report=broken_report,
            )

        self.assertEqual(states[admin.SERVICE_NAME], ["active", "enabled"])
        self.assertEqual(states[admin.TIMER_NAME], ["active", "enabled"])
        self.assertEqual(len(actions), 4)
        recovery_audit.verify_audit_directory(
            audit,
            owner=self.owner,
            group=self.group,
            activation_release="a" * 40,
            activation_role="bot",
        )
        recovery_audit.verify_audit_directory(
            audit,
            owner=self.owner,
            group=self.group,
            activation_release="a" * 40,
            activation_role="backup",
        )

    def test_completed_deployment_without_activation_authorization_blocks_start(self):
        audit, journal_sha, _ = self._finalized_completion_fixture()
        journal = json.loads(
            (audit / f"deployment-state-{journal_sha}.json").read_text()
        )
        with self.assertRaisesRegex(
            recovery_audit.AuditError, "activation authorization"
        ):
            recovery_audit.verify_audit_directory(
                audit,
                owner=self.owner,
                group=self.group,
                activation_release=journal["approved_release_sha"],
                activation_role="bot",
            )
        with self.assertRaisesRegex(
            recovery_audit.AuditError, "activation authorization"
        ):
            recovery_audit.verify_audit_directory(
                audit,
                owner=self.owner,
                group=self.group,
                activation_release="f" * 40,
                activation_role="bot",
            )

    def test_activate_services_fails_closed_when_start_fails(self):
        audit, journal_sha, base = self._finalized_completion_fixture()
        states = {
            admin.SERVICE_NAME: ["inactive", "disabled"],
            admin.TIMER_NAME: ["inactive", "disabled"],
            admin.BACKUP_SERVICE: ["inactive", "static"],
        }
        reports = []

        def apply(action, unit):
            if unit == admin.SERVICE_NAME and action == "start":
                raise admin.AdminError("synthetic start failure")
            state = states[unit]
            state[0] = "active" if action == "start" else "inactive"
            state[1] = "enabled" if action == "enable" else "disabled"

        with patch.object(admin, "COMPLETION_BASE", base):
            with self.assertRaisesRegex(admin.AdminError, "activation failed"):
                admin.activate_services(
                    audit,
                    expected_journal_sha256=journal_sha,
                    approved_by="release-admin",
                    approval_reference="ACTIVATE-FAIL",
                    owner_uid=self.owner,
                    owner_gid=self.group,
                    validator=lambda *args, **kwargs: None,
                    state_reader=lambda: states,
                    service_action=apply,
                    process_check=lambda _: False,
                    database_check=lambda: None,
                    activation_lock=self.root / "lock" / "activation.lock",
                    report=reports.append,
                )
        self.assertEqual(states[admin.SERVICE_NAME], ["inactive", "disabled"])
        self.assertEqual(states[admin.TIMER_NAME], ["inactive", "disabled"])
        self.assertIn('"result": "failed"', reports[-1])

    def test_activate_services_stops_if_unmanaged_bot_process_already_exists(self):
        audit, journal_sha, base = self._finalized_completion_fixture()
        states = {
            admin.SERVICE_NAME: ["inactive", "disabled"],
            admin.TIMER_NAME: ["inactive", "disabled"],
            admin.BACKUP_SERVICE: ["inactive", "static"],
        }
        actions = []
        with (
            patch.object(admin, "COMPLETION_BASE", base),
            patch.object(admin, "_completion_process_present", return_value=True),
        ):
            with self.assertRaisesRegex(admin.AdminError, "already running"):
                admin.activate_services(
                    audit,
                    expected_journal_sha256=journal_sha,
                    approved_by="release-admin",
                    approval_reference="ACTIVATE-199D",
                    owner_uid=self.owner,
                    owner_gid=self.group,
                    validator=lambda *args, **kwargs: None,
                    state_reader=lambda: states,
                    service_action=lambda *args: actions.append(args),
                    database_check=lambda: None,
                    activation_lock=self.root / "lock" / "activation.lock",
                )
        self.assertEqual(actions, [])

    def test_activate_services_fails_closed_when_timer_start_fails(self):
        audit, journal_sha, base = self._finalized_completion_fixture()
        states = {
            admin.SERVICE_NAME: ["inactive", "disabled"],
            admin.TIMER_NAME: ["inactive", "disabled"],
            admin.BACKUP_SERVICE: ["inactive", "static"],
        }
        reports = []

        def apply(action, unit):
            if unit == admin.TIMER_NAME and action == "start":
                raise admin.AdminError("synthetic timer failure")
            if action in {"start", "stop"}:
                states[unit][0] = "active" if action == "start" else "inactive"
            elif action in {"enable", "disable"}:
                states[unit][1] = "enabled" if action == "enable" else "disabled"

        with patch.object(admin, "COMPLETION_BASE", base):
            with self.assertRaisesRegex(admin.AdminError, "activation failed"):
                admin.activate_services(
                    audit,
                    expected_journal_sha256=journal_sha,
                    approved_by="release-admin",
                    approval_reference="ACTIVATE-TIMER-FAIL",
                    owner_uid=self.owner,
                    owner_gid=self.group,
                    validator=lambda *args, **kwargs: None,
                    state_reader=lambda: states,
                    service_action=apply,
                    process_check=lambda _: True,
                    database_check=lambda: None,
                    activation_lock=self.root / "lock" / "activation.lock",
                    report=reports.append,
                )
        self.assertEqual(states[admin.SERVICE_NAME], ["inactive", "disabled"])
        self.assertEqual(states[admin.TIMER_NAME], ["inactive", "disabled"])
        self.assertIn('"result": "failed"', reports[-1])

    def test_admin_tool_has_no_local_runtime_imports(self) -> None:
        source = Path(admin.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                if node.module:
                    imported.append(node.module.split(".", 1)[0])
        self.assertNotIn("deploy", imported)
        self.assertNotIn("wow_forever_armory", imported)

    def test_release_tree_rejects_symlinked_file(self) -> None:
        root = self._make_release(self.root / "release")
        external = self.root / "outside"
        external.write_text("outside")
        (root / "module.py").symlink_to(external)
        with self.assertRaises(admin.AdminError):
            self._verify_release(root)

    def test_release_tree_rejects_symlinked_directory_even_outside_root(self) -> None:
        root = self._make_release(self.root / "release")
        external = self.root / "outside-dir"
        external.mkdir()
        (external / "module.py").write_text("outside")
        (root / "linked-dir").symlink_to(external, target_is_directory=True)
        with self.assertRaises(admin.AdminError):
            self._verify_release(root)

    def test_release_tree_rejects_hardlinked_files(self) -> None:
        root = self._make_release(self.root / "release")
        original = root / "module.py"
        original.write_text("code")
        original.chmod(0o444)
        os.link(original, root / "module-copy.py")
        with self.assertRaises(admin.AdminError):
            self._verify_release(root)

    def test_release_tree_rejects_special_files(self) -> None:
        root = self._make_release(self.root / "release")
        os.mkfifo(root / "named-pipe")
        with self.assertRaises(admin.AdminError):
            self._verify_release(root)

    def test_release_tree_rejects_unsafe_permissions_and_owner(self) -> None:
        root = self._make_release(self.root / "release")
        writable = root / "module.py"
        writable.write_text("code")
        writable.chmod(0o666)
        with self.assertRaises(admin.AdminError):
            self._verify_release(root)
        writable.chmod(0o444)
        with self.assertRaises(admin.AdminError):
            self._verify_release(root, owner_uid=self.owner + 1)

    def test_release_tree_accepts_only_regular_root_owned_layout(self) -> None:
        root = self._make_release(self.root / "release")
        (root / "module.py").write_text("code")
        (root / "module.py").chmod(0o444)
        self._verify_release(root)

    def test_release_tree_accepts_exact_python_314_venv_links(self) -> None:
        root = self._make_release(self.root / "release")
        interpreter = root / ".venv/bin/python"
        interpreter.unlink()
        lib = root / ".venv/lib"
        lib.mkdir()
        (root / ".venv/lib64").symlink_to("lib", target_is_directory=True)

        system_python = self.root / "system-python"
        system_python.write_bytes(b"trusted python fixture")
        system_python.chmod(0o755)
        bin_dir = root / ".venv/bin"
        (bin_dir / "python3.14").symlink_to(system_python)
        for name in ("python", "python3", "𝜋thon"):
            (bin_dir / name).symlink_to("python3.14")

        with patch.object(admin, "_trusted_directory_chain", return_value=True):
            self._verify_release(root, system_python=system_python)

    def test_release_tree_rejects_unlisted_internal_symlink(self) -> None:
        root = self._make_release(self.root / "release")
        payload = root / "module.py"
        payload.write_text("code", encoding="ascii")
        payload.chmod(0o444)
        (root / "module-alias.py").symlink_to("module.py")
        with self.assertRaisesRegex(admin.AdminError, "unapproved symbolic link"):
            self._verify_release(root)

    def test_release_tree_rejects_wrong_python_314_alias_target(self) -> None:
        root = self._make_release(self.root / "release")
        (root / ".venv/bin/python").unlink()
        system_python = self.root / "system-python"
        system_python.write_bytes(b"trusted python fixture")
        system_python.chmod(0o755)
        bin_dir = root / ".venv/bin"
        (bin_dir / "python3.14").symlink_to(system_python)
        (bin_dir / "𝜋thon").symlink_to(system_python)
        with patch.object(admin, "_trusted_directory_chain", return_value=True):
            with self.assertRaisesRegex(admin.AdminError, "unapproved symbolic link"):
                self._verify_release(root, system_python=system_python)

    def test_pin_is_created_once_with_exact_private_metadata(self) -> None:
        path = self.root / "deployment-pin.json"
        raw = self._provision_pin(path)
        self.assertEqual(
            json.loads(raw),
            {
                "installer_sha256": "c" * 64,
                "previous_sha": "b" * 40,
                "release_sha": "a" * 40,
            },
        )
        info = path.stat()
        self.assertEqual(info.st_uid, self.owner)
        self.assertEqual(info.st_gid, self.group)
        self.assertEqual(info.st_nlink, 1)
        self.assertEqual(info.st_mode & 0o777, 0o600)
        self.assertEqual(path.read_bytes(), raw)

    def test_pin_refuses_existing_file_without_overwriting(self) -> None:
        path = self.root / "deployment-pin.json"
        path.write_bytes(b"original\n")
        path.chmod(0o600)
        with self.assertRaises(admin.AdminError):
            self._provision_pin(path)
        self.assertEqual(path.read_bytes(), b"original\n")

    def test_pin_rotation_archives_old_bytes_and_is_idempotent(self) -> None:
        path = self.root / "deployment-pin.json"
        history = self.root / "deployment-pin-history"
        old_raw = self._provision_pin(path)
        old_hash = hashlib.sha256(old_raw).hexdigest()
        arguments = {
            "history_dir": history,
            "expected_current_pin_sha256": old_hash,
            "expected_current_release_sha": "a" * 40,
            "expected_previous_sha": "b" * 40,
            "release_sha": "d" * 40,
            "previous_sha": "b" * 40,
            "installer_sha256": "c" * 64,
            "approved_by": "release-admin",
            "approval_reference": "TASK-302-DEPLOY",
            "owner_uid": self.owner,
            "owner_gid": self.group,
            "parent_mode": 0o700,
        }
        old_result_hash, new_hash = admin.rotate_pin(path, **arguments)
        archive = history / f"deployment-pin-{old_hash}.json"
        self.assertEqual(old_result_hash, old_hash)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), new_hash)
        self.assertEqual(archive.read_bytes(), old_raw)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o400)
        self.assertEqual(json.loads(path.read_bytes())["release_sha"], "d" * 40)
        approval = history / f"deployment-pin-rotation-{old_hash}-{new_hash}.json"
        approval_record = json.loads(approval.read_bytes())
        self.assertEqual(approval_record["previous_pin_sha256"], old_hash)
        self.assertEqual(approval_record["current_pin_sha256"], new_hash)
        self.assertEqual(approval_record["approval_reference"], "TASK-302-DEPLOY")
        self.assertEqual(approval.stat().st_mode & 0o777, 0o400)
        self.assertEqual(admin.rotate_pin(path, **arguments), (old_hash, new_hash))
        self.assertEqual(archive.read_bytes(), old_raw)

    def test_pin_rotation_refuses_contradictory_existing_history(self) -> None:
        path = self.root / "deployment-pin.json"
        history = self.root / "deployment-pin-history"
        old_raw = self._provision_pin(path)
        old_hash = hashlib.sha256(old_raw).hexdigest()
        history.mkdir(mode=0o700)
        archive = history / f"deployment-pin-{old_hash}.json"
        archive.write_bytes(b"contradictory history\n")
        archive.chmod(0o400)
        with self.assertRaisesRegex(admin.AdminError, "existing pin history differs"):
            admin.rotate_pin(
                path,
                history_dir=history,
                expected_current_pin_sha256=old_hash,
                expected_current_release_sha="a" * 40,
                expected_previous_sha="b" * 40,
                release_sha="d" * 40,
                previous_sha="b" * 40,
                installer_sha256="c" * 64,
                approved_by="release-admin",
                approval_reference="TASK-302-DEPLOY",
                owner_uid=self.owner,
                owner_gid=self.group,
                parent_mode=0o700,
            )
        self.assertEqual(path.read_bytes(), old_raw)
        self.assertEqual(archive.read_bytes(), b"contradictory history\n")

    def test_pin_rotation_rejects_unexpected_current_pin_without_replacement(self) -> None:
        path = self.root / "deployment-pin.json"
        history = self.root / "deployment-pin-history"
        old_raw = self._provision_pin(path)
        arguments = {
            "history_dir": history,
            "expected_current_pin_sha256": "0" * 64,
            "expected_current_release_sha": "a" * 40,
            "expected_previous_sha": "b" * 40,
            "release_sha": "d" * 40,
            "previous_sha": "b" * 40,
            "installer_sha256": "c" * 64,
            "approved_by": "release-admin",
            "approval_reference": "TASK-302-DEPLOY",
            "owner_uid": self.owner,
            "owner_gid": self.group,
            "parent_mode": 0o700,
        }
        with self.assertRaisesRegex(admin.AdminError, "differs from approved rotation"):
            admin.rotate_pin(path, **arguments)
        self.assertEqual(path.read_bytes(), old_raw)
        self.assertFalse(history.exists())

    def test_pin_refuses_symlink_without_touching_target(self) -> None:
        target = self.root / "protected-target"
        target.write_bytes(b"do not change\n")
        target.chmod(0o600)
        path = self.root / "deployment-pin.json"
        path.symlink_to(target)
        with self.assertRaises(admin.AdminError):
            self._provision_pin(path)
        self.assertTrue(path.is_symlink())
        self.assertEqual(target.read_bytes(), b"do not change\n")

    def test_pin_refuses_destination_created_in_publication_race(self) -> None:
        path = self.root / "deployment-pin.json"
        target = self.root / "raced-target"
        target.write_bytes(b"protected\n")
        original_link = os.link

        def create_target_then_link(source, destination, **kwargs):
            if destination == path.name:
                os.symlink(target, destination, dir_fd=kwargs["dst_dir_fd"])
            return original_link(source, destination, **kwargs)

        with patch.object(admin.os, "link", side_effect=create_target_then_link):
            with self.assertRaises(admin.AdminError):
                self._provision_pin(path)
        self.assertTrue(path.is_symlink())
        self.assertEqual(target.read_bytes(), b"protected\n")
        self.assertFalse(any(self.root.glob(".deployment-pin.json.tmp.*")))

    def test_pin_refuses_stale_temporary_file(self) -> None:
        (self.root / ".deployment-pin.json.tmp.stale").write_text("stale")
        with self.assertRaises(admin.AdminError):
            self._provision_pin(self.root / "deployment-pin.json")
        self.assertFalse((self.root / "deployment-pin.json").exists())

    def test_pin_rejects_malformed_sha256_values(self) -> None:
        with self.assertRaises(admin.AdminError):
            admin.provision_pin(
                self.root / "deployment-pin.json",
                release_sha="A" * 40,
                previous_sha="b" * 40,
                installer_sha256="not-a-hash",
                owner_uid=self.owner,
                owner_gid=self.group,
                parent_mode=0o700,
            )

    def test_pin_refuses_concurrent_directory_operation(self) -> None:
        directory_fd = admin._open_private_directory(
            self.root, owner_uid=self.owner, exact_mode=0o700
        )
        try:
            with self.assertRaises(admin.AdminError):
                self._provision_pin(self.root / "deployment-pin.json")
        finally:
            os.close(directory_fd)

    def test_pin_refuses_symlinked_parent(self) -> None:
        real = self.root / "real"
        real.mkdir(mode=0o700)
        alias = self.root / "alias"
        alias.symlink_to(real, target_is_directory=True)
        with self.assertRaises(admin.AdminError):
            self._provision_pin(alias / "deployment-pin.json")

    def test_reviewed_source_loader_ignores_adjacent_bytecode(self) -> None:
        source_dir = self.root / "source"
        source_dir.mkdir()
        source = source_dir / "reviewed_module.py"
        marker = self.root / "bytecode-ran"
        source.write_text("source_value = 'reviewed source'\n", encoding="utf-8")
        cache = self.root / "__pycache__" / "reviewed_module.cpython-314.pyc"
        cache.parent.mkdir()
        evil = (
            f"from pathlib import Path; Path({str(marker)!r}).write_text('executed')\n"
        )
        cache_source = self.root / "evil.py"
        cache_source.write_text(evil, encoding="utf-8")
        py_compile.compile(
            str(cache_source),
            cfile=str(cache),
            doraise=True,
        )
        source.chmod(0o444)
        source_dir.chmod(0o555)
        expected = hashlib.sha256(source.read_bytes()).hexdigest()
        loaded = admin.load_reviewed_python_source(
            source, expected_sha256=expected, owner_uid=self.owner
        )
        self.assertEqual(loaded.source_value, "reviewed source")
        self.assertFalse(marker.exists())

    def test_reviewed_source_loader_accepts_root_owned_style_source_and_directory(
        self,
    ) -> None:
        source_dir = self.root / "root-owned-style"
        source_dir.mkdir()
        source = source_dir / "reviewed_module.py"
        source.write_text("source_value = 'reviewed source'\n", encoding="utf-8")
        source.chmod(0o644)
        source_dir.chmod(0o755)
        expected = hashlib.sha256(source.read_bytes()).hexdigest()

        loaded = admin.load_reviewed_python_source(
            source, expected_sha256=expected, owner_uid=self.owner
        )

        self.assertEqual(loaded.source_value, "reviewed source")

    def test_reviewed_source_loader_still_rejects_group_writable_directory(
        self,
    ) -> None:
        source_dir = self.root / "group-writable"
        source_dir.mkdir()
        source = source_dir / "reviewed_module.py"
        source.write_text("safe = True\n", encoding="utf-8")
        source.chmod(0o444)
        source_dir.chmod(0o775)
        expected = hashlib.sha256(source.read_bytes()).hexdigest()

        with self.assertRaises(admin.AdminError):
            admin.load_reviewed_python_source(
                source, expected_sha256=expected, owner_uid=self.owner
            )

    def test_reviewed_source_loader_still_rejects_group_writable_source(self) -> None:
        source_dir = self.root / "trusted-parent"
        source_dir.mkdir()
        source = source_dir / "reviewed_module.py"
        source.write_text("safe = True\n", encoding="utf-8")
        source.chmod(0o664)
        source_dir.chmod(0o755)
        expected = hashlib.sha256(source.read_bytes()).hexdigest()

        with self.assertRaises(admin.AdminError):
            admin.load_reviewed_python_source(
                source, expected_sha256=expected, owner_uid=self.owner
            )

    def test_reviewed_source_loader_still_rejects_other_writable_source(self) -> None:
        source_dir = self.root / "trusted-parent-other-write"
        source_dir.mkdir()
        source = source_dir / "reviewed_module.py"
        source.write_text("safe = True\n", encoding="utf-8")
        source.chmod(0o646)
        source_dir.chmod(0o755)
        expected = hashlib.sha256(source.read_bytes()).hexdigest()

        with self.assertRaises(admin.AdminError):
            admin.load_reviewed_python_source(
                source, expected_sha256=expected, owner_uid=self.owner
            )

    def test_reviewed_source_loader_rejects_wrong_hash(self) -> None:
        source_dir = self.root / "source"
        source_dir.mkdir()
        source = source_dir / "reviewed_module.py"
        source.write_text("safe = True\n", encoding="utf-8")
        source.chmod(0o444)
        source_dir.chmod(0o555)
        with self.assertRaises(admin.AdminError):
            admin.load_reviewed_python_source(
                source, expected_sha256="f" * 64, owner_uid=self.owner
            )

    def test_reviewed_source_loader_rejects_symlink_and_hardlink(self) -> None:
        source_dir = self.root / "source"
        source_dir.mkdir()
        source = source_dir / "reviewed_module.py"
        source.write_text("safe = True\n", encoding="utf-8")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        alias = source_dir / "alias.py"
        alias.symlink_to(source)
        with self.assertRaises(admin.AdminError):
            admin.load_reviewed_python_source(
                alias, expected_sha256=digest, owner_uid=self.owner
            )
        os.link(source, source_dir / "extra-link.py")
        source.chmod(0o444)
        (source_dir / "extra-link.py").chmod(0o444)
        source_dir.chmod(0o555)
        with self.assertRaises(admin.AdminError):
            admin.load_reviewed_python_source(
                source, expected_sha256=digest, owner_uid=self.owner
            )

    def test_reviewed_source_loader_rejects_writable_source(self) -> None:
        source_dir = self.root / "source"
        source_dir.mkdir()
        source = source_dir / "reviewed_module.py"
        source.write_text("safe = True\n", encoding="utf-8")
        source_dir.chmod(0o555)
        source.chmod(0o666)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        with self.assertRaises(admin.AdminError):
            admin.load_reviewed_python_source(
                source, expected_sha256=digest, owner_uid=self.owner
            )

    def _incident_fixture(self) -> tuple[Path, Path, dict[str, str]]:
        audit = Path(tempfile.mkdtemp(prefix="audit-", dir=self.root))
        audit.chmod(0o700)
        release_sha = "a" * 40
        previous_sha = "b" * 40
        backup_path = (
            "/var/lib/wow-forever-armory-bot/backups/guild-armory-test.sqlite3"
        )
        preflight_path = f"{audit}/armory-db-preflight.{previous_sha}.py"
        manifest = {
            "approved_release_sha": release_sha,
            "previous_release_sha": previous_sha,
            "database_backup": backup_path,
            "database_backup_sha256": "c" * 64,
            "shared_preflight_backup": preflight_path,
            "shared_preflight_sha256": "d" * 64,
            "created_at_utc": "2026-11-15T12:00:00+00:00",
        }
        manifest_path = audit / f"deployment-{release_sha}.json"
        manifest_raw = (json.dumps(manifest, sort_keys=True) + "\n").encode()
        manifest_path.write_bytes(manifest_raw)
        manifest_path.chmod(0o600)
        journal = {
            "approved_release_sha": release_sha,
            "previous_release_sha": previous_sha,
            "phase": "manual_recovery_required",
            "interrupted_phase": "installer_started",
            "failure_class": "InstallerExecutionFailure",
            "installer_failure": {
                "category": "installer_rejected",
                "exit_code": 1,
                "outcome": "installer_rejected",
            },
            "fail_closed_verified": True,
            "fail_closed_error": None,
            "database_signature": [1, 2, 3, 4, 5, 6, 7, 8],
            "database_backup": backup_path,
            "database_backup_sha256": "c" * 64,
            "shared_preflight_backup": preflight_path,
            "shared_preflight_sha256": "d" * 64,
            "manifest": str(manifest_path),
        }
        journal_path = audit / "deployment-state.json"
        journal_path.write_text(json.dumps(journal, sort_keys=True) + "\n")
        journal_path.chmod(0o600)
        journal_sha = hashlib.sha256(journal_path.read_bytes()).hexdigest()
        manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
        review = {
            "schema_version": 1,
            "journal_sha256": journal_sha,
            "manifest_sha256": manifest_sha,
            "approved_release_sha": release_sha,
            "previous_release_sha": previous_sha,
            "checks": {name: "PASS" for name in admin.RECOVERY_CHECKS},
            "observations": {
                "release_target": (
                    f"/opt/wow-forever-armory-bot/releases/{previous_sha}"
                ),
                "installation_marker": previous_sha,
                "database_signature": [1, 2, 3, 4, 5, 6, 7, 8],
                "database_integrity": "ok",
                "backup_path": backup_path,
                "backup_sha256": "c" * 64,
                "backup_integrity": "ok",
                "saved_preflight_path": preflight_path,
                "saved_preflight_sha256": "d" * 64,
                "systemd_states": {
                    "wow-forever-armory-bot.service": ["inactive", "disabled"],
                    "wow-forever-armory-backup.timer": ["inactive", "disabled"],
                    "wow-forever-armory-backup.service": ["inactive", "static"],
                },
            },
            "completed_at_utc": "2026-11-15T12:30:00+00:00",
            "approved_by": "admin-example",
            "approval_reference": "CHANGE-191",
            "approved_at_utc": "2026-11-15T12:31:00+00:00",
        }
        review_path = audit / f"recovery-review-{journal_sha}.json"
        review_path.write_text(json.dumps(review, sort_keys=True) + "\n")
        review_path.chmod(0o600)
        return (
            audit,
            review_path,
            {
                "journal_sha256": journal_sha,
                "manifest_sha256": manifest_sha,
            },
        )

    def _states(self) -> dict[str, list[str]]:
        return {
            "wow-forever-armory-bot.service": ["inactive", "disabled"],
            "wow-forever-armory-backup.timer": ["inactive", "disabled"],
            "wow-forever-armory-backup.service": ["inactive", "static"],
        }

    def _recovery_evidence_fixture(self) -> tuple[dict, dict, dict]:
        audit = self.root / "audit-real"
        audit.mkdir(mode=0o700)
        audit.chmod(0o700)
        release_sha, previous_sha = "a" * 40, "b" * 40
        base = self.root / "deployment"
        releases = base / "releases"
        releases.mkdir(parents=True)
        self._make_release(releases / release_sha)
        self._make_release(releases / previous_sha)
        current = base / "current"
        current.symlink_to(f"releases/{previous_sha}")
        database = self.root / "guild-armory.sqlite3"
        database.write_bytes(b"database contents")
        database.chmod(0o600)
        db_info = database.stat()
        signature = [
            db_info.st_dev,
            db_info.st_ino,
            db_info.st_size,
            db_info.st_mtime_ns,
            db_info.st_ctime_ns,
            db_info.st_uid,
            db_info.st_gid,
            db_info.st_mode,
        ]
        backup_dir = self.root / "backups"
        backup_dir.mkdir(mode=0o700)
        backup_dir.chmod(0o700)
        backup = backup_dir / "guild-armory-test.sqlite3"
        backup.write_bytes(b"backup contents")
        backup.chmod(0o600)
        preflight = audit / f"armory-db-preflight.{previous_sha}.py"
        preflight.write_bytes(b"# saved preflight\n")
        preflight.chmod(0o700)
        journal = {
            "database_signature": signature,
            "database_backup": str(backup),
            "database_backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
            "shared_preflight_backup": str(preflight),
            "shared_preflight_sha256": hashlib.sha256(
                preflight.read_bytes()
            ).hexdigest(),
        }
        review = {
            "observations": {
                "release_target": str(releases / previous_sha),
                "installation_marker": previous_sha,
                "database_signature": signature,
                "backup_sha256": journal["database_backup_sha256"],
                "saved_preflight_sha256": journal["shared_preflight_sha256"],
            }
        }
        options = {
            "base": base,
            "database": database,
            "service_uid": self.owner,
            "service_gid": self.group,
            "owner_uid": self.owner,
        }
        return journal, review, options

    def test_real_recovery_evidence_verifier_accepts_consistent_fixture(self) -> None:
        journal, review, options = self._recovery_evidence_fixture()
        admin._verify_recovery_evidence(
            journal,
            "a" * 40,
            "b" * 40,
            self.root / "audit-real",
            review,
            **options,
        )

    def test_real_recovery_evidence_verifier_rejects_release_symlink(self) -> None:
        journal, review, options = self._recovery_evidence_fixture()
        release = options["base"] / "releases" / ("a" * 40)
        (release / "escape.py").symlink_to(self.root / "outside")
        with self.assertRaises(admin.AdminError):
            admin._verify_recovery_evidence(
                journal,
                "a" * 40,
                "b" * 40,
                self.root / "audit-real",
                review,
                **options,
            )

    def test_real_recovery_evidence_verifier_rejects_database_signature_change(
        self,
    ) -> None:
        journal, review, options = self._recovery_evidence_fixture()
        journal["database_signature"] = [0] * 8
        with self.assertRaises(admin.AdminError):
            admin._verify_recovery_evidence(
                journal,
                "a" * 40,
                "b" * 40,
                self.root / "audit-real",
                review,
                **options,
            )

    @staticmethod
    def _evidence_ok(*_args) -> None:
        return None

    def test_acknowledgment_archives_exact_journal_and_binds_manifest(self) -> None:
        audit, review, hashes = self._incident_fixture()
        active = audit / "deployment-state.json"
        original = active.read_bytes()
        journal_sha, manifest_sha = admin.acknowledge_incident(
            audit,
            review,
            expected_journal_sha256=hashes["journal_sha256"],
            approved_by="admin-example",
            approval_reference="CHANGE-191",
            owner_uid=self.owner,
            owner_gid=self.group,
            state_reader=self._states,
            evidence_verifier=self._evidence_ok,
        )
        archive = audit / f"deployment-state-{journal_sha}.json"
        ack_path = audit / f"deployment-recovery-ack-{journal_sha}.json"
        self.assertFalse(active.exists())
        self.assertEqual(archive.read_bytes(), original)
        self.assertEqual(archive.stat().st_nlink, 1)
        ack = json.loads(ack_path.read_text(encoding="ascii"))
        self.assertEqual(ack["journal_sha256"], journal_sha)
        self.assertEqual(ack["manifest_sha256"], manifest_sha)
        self.assertEqual(ack["decision"], "manual_recovery_verified")
        self.assertEqual(ack["approval_reference"], "CHANGE-191")
        self.assertEqual(
            set(ack),
            {
                "schema_version",
                "journal_sha256",
                "manifest_sha256",
                "approved_release_sha",
                "previous_release_sha",
                "decision",
                "approved_by",
                "approval_reference",
                "approved_at_utc",
            },
        )

    def test_acknowledgment_refuses_restored_active_journal_without_changes(
        self,
    ) -> None:
        audit, review, hashes = self._incident_fixture()
        active = audit / "deployment-state.json"
        archive = audit / f"deployment-state-{hashes['journal_sha256']}.json"
        acknowledgment = audit / (
            f"deployment-recovery-ack-{hashes['journal_sha256']}.json"
        )
        os.link(active, archive)
        ack = {
            "schema_version": 1,
            "journal_sha256": hashes["journal_sha256"],
            "manifest_sha256": hashes["manifest_sha256"],
            "approved_release_sha": "a" * 40,
            "previous_release_sha": "b" * 40,
            "decision": "manual_recovery_verified",
            "approved_by": "admin-example",
            "approval_reference": "CHANGE-191",
            "approved_at_utc": "2026-11-15T12:31:00+00:00",
        }
        acknowledgment.write_text(
            json.dumps(ack, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="ascii",
        )
        acknowledgment.chmod(0o600)

        protected = [
            active,
            archive,
            acknowledgment,
            audit / f"deployment-{'a' * 40}.json",
            review,
        ]
        before = {
            path: (
                path.read_bytes(),
                path.stat().st_ino,
                path.stat().st_nlink,
                path.stat().st_mode,
                path.stat().st_uid,
                path.stat().st_gid,
                path.stat().st_mtime_ns,
                path.stat().st_ctime_ns,
            )
            for path in protected
        }

        with self.assertRaisesRegex(
            admin.AdminError, "active journal conflicts with an existing acknowledgment"
        ):
            admin.acknowledge_incident(
                audit,
                review,
                expected_journal_sha256=hashes["journal_sha256"],
                approved_by="admin-example",
                approval_reference="CHANGE-191",
                owner_uid=self.owner,
                owner_gid=self.group,
                state_reader=self._states,
                evidence_verifier=self._evidence_ok,
            )

        after = {
            path: (
                path.read_bytes(),
                path.stat().st_ino,
                path.stat().st_nlink,
                path.stat().st_mode,
                path.stat().st_uid,
                path.stat().st_gid,
                path.stat().st_mtime_ns,
                path.stat().st_ctime_ns,
            )
            for path in protected
        }
        self.assertEqual(after, before)

    def test_acknowledgment_refuses_active_unit_states_without_archiving(self) -> None:
        audit, review, hashes = self._incident_fixture()
        with self.assertRaises(admin.AdminError):
            admin.acknowledge_incident(
                audit,
                review,
                expected_journal_sha256=hashes["journal_sha256"],
                approved_by="admin-example",
                approval_reference="CHANGE-191",
                owner_uid=self.owner,
                owner_gid=self.group,
                state_reader=lambda: {
                    **self._states(),
                    "wow-forever-armory-bot.service": ["active", "enabled"],
                },
                evidence_verifier=self._evidence_ok,
            )
        self.assertTrue((audit / "deployment-state.json").exists())
        self.assertFalse(any(audit.glob("deployment-state-*.json")))

    def test_acknowledgment_refuses_unknown_review_result(self) -> None:
        audit, review, hashes = self._incident_fixture()
        record = json.loads(review.read_text(encoding="ascii"))
        record["checks"]["database_integrity"] = "UNKNOWN"
        review.write_text(json.dumps(record, sort_keys=True) + "\n")
        review.chmod(0o600)
        with self.assertRaises(admin.AdminError):
            admin.acknowledge_incident(
                audit,
                review,
                expected_journal_sha256=hashes["journal_sha256"],
                approved_by="admin-example",
                approval_reference="CHANGE-191",
                owner_uid=self.owner,
                owner_gid=self.group,
                state_reader=self._states,
                evidence_verifier=self._evidence_ok,
            )
        self.assertTrue((audit / "deployment-state.json").exists())

    def test_acknowledgment_refuses_journal_symlink(self) -> None:
        audit, review, hashes = self._incident_fixture()
        active = audit / "deployment-state.json"
        target = audit / "journal-target"
        active.rename(target)
        active.symlink_to(target)
        with self.assertRaises(admin.AdminError):
            admin.acknowledge_incident(
                audit,
                review,
                expected_journal_sha256=hashes["journal_sha256"],
                approved_by="admin-example",
                approval_reference="CHANGE-191",
                owner_uid=self.owner,
                owner_gid=self.group,
                state_reader=self._states,
                evidence_verifier=self._evidence_ok,
            )
        self.assertTrue(active.is_symlink())
        self.assertTrue(target.exists())

    def test_acknowledgment_rejects_corrupt_json_and_wrong_hash(self) -> None:
        audit, review, hashes = self._incident_fixture()
        active = audit / "deployment-state.json"
        active.write_bytes(b"{not json\n")
        active.chmod(0o600)
        with self.assertRaises(admin.AdminError):
            admin.acknowledge_incident(
                audit,
                review,
                expected_journal_sha256=hashes["journal_sha256"],
                approved_by="admin-example",
                approval_reference="CHANGE-191",
                owner_uid=self.owner,
                owner_gid=self.group,
                state_reader=self._states,
                evidence_verifier=self._evidence_ok,
            )
        self.assertTrue(active.exists())

    def test_acknowledgment_rejects_insecure_journal_mode_and_hardlink(self) -> None:
        for mode, make_hardlink in ((0o644, False), (0o600, True)):
            with self.subTest(mode=mode, make_hardlink=make_hardlink):
                audit, review, hashes = self._incident_fixture()
                active = audit / "deployment-state.json"
                active.chmod(mode)
                if make_hardlink:
                    os.link(active, audit / "journal-extra-link")
                with self.assertRaises(admin.AdminError):
                    admin.acknowledge_incident(
                        audit,
                        review,
                        expected_journal_sha256=hashes["journal_sha256"],
                        approved_by="admin-example",
                        approval_reference="CHANGE-191",
                        owner_uid=self.owner,
                        owner_gid=self.group,
                        state_reader=self._states,
                        evidence_verifier=self._evidence_ok,
                    )
                self.assertTrue(active.exists())

    def test_acknowledgment_refuses_concurrent_directory_operation(self) -> None:
        audit, review, hashes = self._incident_fixture()
        directory_fd = admin._open_private_directory(
            audit, owner_uid=self.owner, exact_mode=0o700
        )
        try:
            with self.assertRaises(admin.AdminError):
                admin.acknowledge_incident(
                    audit,
                    review,
                    expected_journal_sha256=hashes["journal_sha256"],
                    approved_by="admin-example",
                    approval_reference="CHANGE-191",
                    owner_uid=self.owner,
                    owner_gid=self.group,
                    state_reader=self._states,
                    evidence_verifier=self._evidence_ok,
                )
        finally:
            os.close(directory_fd)
        self.assertTrue((audit / "deployment-state.json").exists())

    def test_acknowledgment_resumes_after_each_durable_checkpoint(self) -> None:
        checkpoints = (
            "before_archive",
            "archive_created",
            "archive_verified",
            "active_removed",
            "before_ack_publication",
            "ack_published",
        )
        for stop_at in checkpoints:
            with self.subTest(stop_at=stop_at):
                audit, review, hashes = self._incident_fixture()

                def interrupt(checkpoint: str, expected: str = stop_at) -> None:
                    if checkpoint == expected:
                        raise RuntimeError("simulated process interruption")

                with self.assertRaises(RuntimeError):
                    admin.acknowledge_incident(
                        audit,
                        review,
                        expected_journal_sha256=hashes["journal_sha256"],
                        approved_by="admin-example",
                        approval_reference="CHANGE-191",
                        owner_uid=self.owner,
                        owner_gid=self.group,
                        state_reader=self._states,
                        evidence_verifier=self._evidence_ok,
                        checkpoint=interrupt,
                    )
                result = admin.acknowledge_incident(
                    audit,
                    review,
                    expected_journal_sha256=hashes["journal_sha256"],
                    approved_by="admin-example",
                    approval_reference="CHANGE-191",
                    owner_uid=self.owner,
                    owner_gid=self.group,
                    state_reader=self._states,
                    evidence_verifier=self._evidence_ok,
                )
                self.assertEqual(
                    result,
                    (hashes["journal_sha256"], hashes["manifest_sha256"]),
                )

    def test_acknowledgment_resumes_after_archive_fsync_failure(self) -> None:
        audit, review, hashes = self._incident_fixture()
        original_fsync = os.fsync
        failed = False

        def fail_once(fd: int) -> None:
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("simulated directory fsync failure")
            original_fsync(fd)

        with patch.object(admin.os, "fsync", side_effect=fail_once):
            with self.assertRaises(admin.AdminError):
                admin.acknowledge_incident(
                    audit,
                    review,
                    expected_journal_sha256=hashes["journal_sha256"],
                    approved_by="admin-example",
                    approval_reference="CHANGE-191",
                    owner_uid=self.owner,
                    owner_gid=self.group,
                    state_reader=self._states,
                    evidence_verifier=self._evidence_ok,
                )
        self.assertTrue((audit / "deployment-state.json").exists())
        admin.acknowledge_incident(
            audit,
            review,
            expected_journal_sha256=hashes["journal_sha256"],
            approved_by="admin-example",
            approval_reference="CHANGE-191",
            owner_uid=self.owner,
            owner_gid=self.group,
            state_reader=self._states,
            evidence_verifier=self._evidence_ok,
        )

    def test_acknowledgment_resumes_after_active_removal_fsync_failure(self) -> None:
        audit, review, hashes = self._incident_fixture()
        original_fsync = os.fsync
        count = 0

        def fail_after_unlink(fd: int) -> None:
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("simulated journal-removal fsync failure")
            original_fsync(fd)

        with patch.object(admin.os, "fsync", side_effect=fail_after_unlink):
            with self.assertRaises(admin.AdminError):
                admin.acknowledge_incident(
                    audit,
                    review,
                    expected_journal_sha256=hashes["journal_sha256"],
                    approved_by="admin-example",
                    approval_reference="CHANGE-191",
                    owner_uid=self.owner,
                    owner_gid=self.group,
                    state_reader=self._states,
                    evidence_verifier=self._evidence_ok,
                )
        self.assertFalse((audit / "deployment-state.json").exists())
        admin.acknowledge_incident(
            audit,
            review,
            expected_journal_sha256=hashes["journal_sha256"],
            approved_by="admin-example",
            approval_reference="CHANGE-191",
            owner_uid=self.owner,
            owner_gid=self.group,
            state_reader=self._states,
            evidence_verifier=self._evidence_ok,
        )

    def test_acknowledgment_resumes_after_ack_file_fsync_failure(self) -> None:
        audit, review, hashes = self._incident_fixture()
        original_fsync = os.fsync
        count = 0

        def fail_ack_file_flush(fd: int) -> None:
            nonlocal count
            count += 1
            if count == 3:
                raise OSError("simulated ACK file fsync failure")
            original_fsync(fd)

        with patch.object(admin.os, "fsync", side_effect=fail_ack_file_flush):
            with self.assertRaises(admin.AdminError):
                admin.acknowledge_incident(
                    audit,
                    review,
                    expected_journal_sha256=hashes["journal_sha256"],
                    approved_by="admin-example",
                    approval_reference="CHANGE-191",
                    owner_uid=self.owner,
                    owner_gid=self.group,
                    state_reader=self._states,
                    evidence_verifier=self._evidence_ok,
                )
        self.assertFalse((audit / "deployment-state.json").exists())
        admin.acknowledge_incident(
            audit,
            review,
            expected_journal_sha256=hashes["journal_sha256"],
            approved_by="admin-example",
            approval_reference="CHANGE-191",
            owner_uid=self.owner,
            owner_gid=self.group,
            state_reader=self._states,
            evidence_verifier=self._evidence_ok,
        )

    def test_acknowledgment_resumes_after_ack_publication_error(self) -> None:
        audit, review, hashes = self._incident_fixture()
        ack_name = f"deployment-recovery-ack-{hashes['journal_sha256']}.json"
        original_link = os.link

        def fail_ack_link(source, destination, **kwargs):
            if destination == ack_name:
                raise OSError("simulated acknowledgment publication failure")
            return original_link(source, destination, **kwargs)

        with patch.object(admin.os, "link", side_effect=fail_ack_link):
            with self.assertRaises(admin.AdminError):
                admin.acknowledge_incident(
                    audit,
                    review,
                    expected_journal_sha256=hashes["journal_sha256"],
                    approved_by="admin-example",
                    approval_reference="CHANGE-191",
                    owner_uid=self.owner,
                    owner_gid=self.group,
                    state_reader=self._states,
                    evidence_verifier=self._evidence_ok,
                )
        self.assertFalse((audit / "deployment-state.json").exists())
        self.assertFalse((audit / ack_name).exists())
        admin.acknowledge_incident(
            audit,
            review,
            expected_journal_sha256=hashes["journal_sha256"],
            approved_by="admin-example",
            approval_reference="CHANGE-191",
            owner_uid=self.owner,
            owner_gid=self.group,
            state_reader=self._states,
            evidence_verifier=self._evidence_ok,
        )

    def test_acknowledgment_resumes_after_ack_write_failure(self) -> None:
        audit, review, hashes = self._incident_fixture()

        def fail_write(fd: int, data: bytes) -> int:
            raise OSError("simulated acknowledgment write failure")

        with patch.object(admin.os, "write", side_effect=fail_write):
            with self.assertRaises(admin.AdminError):
                admin.acknowledge_incident(
                    audit,
                    review,
                    expected_journal_sha256=hashes["journal_sha256"],
                    approved_by="admin-example",
                    approval_reference="CHANGE-191",
                    owner_uid=self.owner,
                    owner_gid=self.group,
                    state_reader=self._states,
                    evidence_verifier=self._evidence_ok,
                )
        self.assertFalse((audit / "deployment-state.json").exists())
        archive = audit / f"deployment-state-{hashes['journal_sha256']}.json"
        ack = audit / f"deployment-recovery-ack-{hashes['journal_sha256']}.json"
        self.assertTrue(archive.exists())
        self.assertFalse(ack.exists())
        admin.acknowledge_incident(
            audit,
            review,
            expected_journal_sha256=hashes["journal_sha256"],
            approved_by="admin-example",
            approval_reference="CHANGE-191",
            owner_uid=self.owner,
            owner_gid=self.group,
            state_reader=self._states,
            evidence_verifier=self._evidence_ok,
        )

    def test_acknowledgment_resumes_after_ack_directory_fsync_failure(self) -> None:
        audit, review, hashes = self._incident_fixture()
        original_fsync = os.fsync
        count = 0

        def fail_after_ack_link(fd: int) -> None:
            nonlocal count
            count += 1
            if count == 4:
                raise OSError("simulated ACK directory fsync failure")
            original_fsync(fd)

        with patch.object(admin.os, "fsync", side_effect=fail_after_ack_link):
            with self.assertRaises(admin.AdminError):
                admin.acknowledge_incident(
                    audit,
                    review,
                    expected_journal_sha256=hashes["journal_sha256"],
                    approved_by="admin-example",
                    approval_reference="CHANGE-191",
                    owner_uid=self.owner,
                    owner_gid=self.group,
                    state_reader=self._states,
                    evidence_verifier=self._evidence_ok,
                )
        admin.acknowledge_incident(
            audit,
            review,
            expected_journal_sha256=hashes["journal_sha256"],
            approved_by="admin-example",
            approval_reference="CHANGE-191",
            owner_uid=self.owner,
            owner_gid=self.group,
            state_reader=self._states,
            evidence_verifier=self._evidence_ok,
        )

    def test_acknowledgment_cleans_only_private_stale_temp_after_reverification(
        self,
    ) -> None:
        audit, review, hashes = self._incident_fixture()
        ack_name = f"deployment-recovery-ack-{hashes['journal_sha256']}.json"
        stale = audit / f".{ack_name}.tmp.interrupted"
        stale.write_bytes(b"partial unpublished content")
        stale.chmod(0o600)
        admin.acknowledge_incident(
            audit,
            review,
            expected_journal_sha256=hashes["journal_sha256"],
            approved_by="admin-example",
            approval_reference="CHANGE-191",
            owner_uid=self.owner,
            owner_gid=self.group,
            state_reader=self._states,
            evidence_verifier=self._evidence_ok,
        )
        self.assertFalse(stale.exists())
        self.assertTrue((audit / ack_name).is_file())

    def test_acknowledgment_refuses_existing_archive_without_overwrite(self) -> None:
        audit, review, hashes = self._incident_fixture()
        archive = audit / f"deployment-state-{hashes['journal_sha256']}.json"
        archive.write_bytes(b"preserve existing evidence\n")
        archive.chmod(0o600)
        with self.assertRaises(admin.AdminError):
            admin.acknowledge_incident(
                audit,
                review,
                expected_journal_sha256=hashes["journal_sha256"],
                approved_by="admin-example",
                approval_reference="CHANGE-191",
                owner_uid=self.owner,
                owner_gid=self.group,
                state_reader=self._states,
                evidence_verifier=self._evidence_ok,
            )
        self.assertEqual(archive.read_bytes(), b"preserve existing evidence\n")
        self.assertTrue((audit / "deployment-state.json").exists())

    def test_acknowledgment_refuses_archive_destination_race(self) -> None:
        audit, review, hashes = self._incident_fixture()
        target = audit / f"deployment-state-{hashes['journal_sha256']}.json"
        original_link = os.link

        def race_archive(source, destination, **kwargs):
            if destination == target.name:
                target.write_bytes(b"other evidence\n")
                target.chmod(0o600)
            return original_link(source, destination, **kwargs)

        with patch.object(admin.os, "link", side_effect=race_archive):
            with self.assertRaises(admin.AdminError):
                admin.acknowledge_incident(
                    audit,
                    review,
                    expected_journal_sha256=hashes["journal_sha256"],
                    approved_by="admin-example",
                    approval_reference="CHANGE-191",
                    owner_uid=self.owner,
                    owner_gid=self.group,
                    state_reader=self._states,
                    evidence_verifier=self._evidence_ok,
                )
        self.assertEqual(target.read_bytes(), b"other evidence\n")
        self.assertTrue((audit / "deployment-state.json").exists())

    def test_ack_write_race_leaves_archive_and_fails_closed(self) -> None:
        audit, review, hashes = self._incident_fixture()
        journal_sha = hashes["journal_sha256"]
        archive = audit / f"deployment-state-{journal_sha}.json"
        ack = audit / f"deployment-recovery-ack-{journal_sha}.json"
        original_link = os.link

        def race_ack(source, destination, **kwargs):
            if destination == ack.name:
                ack.write_bytes(b"preserve collision\n")
                ack.chmod(0o600)
            return original_link(source, destination, **kwargs)

        with patch.object(admin.os, "link", side_effect=race_ack):
            with self.assertRaises(admin.AdminError):
                admin.acknowledge_incident(
                    audit,
                    review,
                    expected_journal_sha256=journal_sha,
                    approved_by="admin-example",
                    approval_reference="CHANGE-191",
                    owner_uid=self.owner,
                    owner_gid=self.group,
                    state_reader=self._states,
                    evidence_verifier=self._evidence_ok,
                )
        self.assertFalse((audit / "deployment-state.json").exists())
        self.assertTrue(archive.is_file())
        self.assertEqual(ack.read_bytes(), b"preserve collision\n")

    def test_acknowledgment_refuses_changed_live_evidence(self) -> None:
        audit, review, hashes = self._incident_fixture()

        def changed_evidence(*_args) -> None:
            raise admin.AdminError("evidence changed")

        with self.assertRaises(admin.AdminError):
            admin.acknowledge_incident(
                audit,
                review,
                expected_journal_sha256=hashes["journal_sha256"],
                approved_by="admin-example",
                approval_reference="CHANGE-191",
                owner_uid=self.owner,
                owner_gid=self.group,
                state_reader=self._states,
                evidence_verifier=changed_evidence,
            )
        self.assertTrue((audit / "deployment-state.json").exists())
        self.assertFalse(any(audit.glob("deployment-state-*.json")))


if __name__ == "__main__":
    unittest.main()
