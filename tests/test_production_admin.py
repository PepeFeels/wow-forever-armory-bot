"""Security tests for pin provisioning and hash-bound recovery administration."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import py_compile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy import production_admin as admin


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

    def _verify_release(self, root: Path, *, owner_uid: int | None = None) -> None:
        admin._verify_release_tree(
            root,
            service_uid=self.owner,
            service_gid=self.group,
            owner_uid=self.owner if owner_uid is None else owner_uid,
        )

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
            "from pathlib import Path; "
            f"Path({str(marker)!r}).write_text('executed')\n"
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
            "/var/lib/wow-forever-armory-bot/backups/"
            "guild-armory-test.sqlite3"
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
        return audit, review_path, {
            "journal_sha256": journal_sha,
            "manifest_sha256": manifest_sha,
        }

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
        self.assertEqual(set(ack), {
            "schema_version",
            "journal_sha256",
            "manifest_sha256",
            "approved_release_sha",
            "previous_release_sha",
            "decision",
            "approved_by",
            "approval_reference",
            "approved_at_utc",
        })

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
