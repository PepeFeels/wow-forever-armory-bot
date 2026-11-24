"""Isolated tests for the wrapper/installer diagnostic protocol."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from deploy import production_install


def _load_wrapper():
    path = Path(__file__).parents[1] / "deploy/production_wrapper.py"
    spec = importlib.util.spec_from_file_location("deployment_wrapper", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wrapper = _load_wrapper()


class MemoryJournal:
    def __init__(self):
        self.records = []

    def start(self, states):
        pass

    def update(self, phase, **details):
        self.records.append((phase, details))


class InstallerDiagnosticTests(unittest.TestCase):
    def test_wrapper_accepts_only_hash_pinned_previous_backup_unit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = root / "previous.service"
            candidate = root / "candidate.service"
            fixture = Path(__file__).parent / "fixtures/task298-previous-backup.service"
            old.write_bytes(fixture.read_bytes())
            candidate.write_bytes(
                (Path(__file__).parents[1] / "deploy/systemd/wow-forever-armory-backup.service").read_bytes()
            )
            metadata = SimpleNamespace(st_gid=0, st_nlink=1)
            with patch.object(wrapper, "_assert_managed_file", return_value=metadata):
                wrapper._verify_previous_backup_unit(old, candidate)
                candidate.write_bytes(candidate.read_bytes() + b"# unexpected\n")
                with self.assertRaisesRegex(wrapper.Refusal, "exact approved previous"):
                    wrapper._verify_previous_backup_unit(old, candidate)

    def test_wrapper_accepts_exact_candidate_backup_unit_as_idempotent_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed = root / "installed.service"
            candidate = Path(__file__).parents[1] / (
                "deploy/systemd/wow-forever-armory-backup.service"
            )
            installed.write_bytes(candidate.read_bytes())
            metadata = SimpleNamespace(st_gid=0, st_nlink=1)
            with patch.object(wrapper, "_assert_managed_file", return_value=metadata):
                wrapper._verify_previous_backup_unit(installed, candidate)

    def test_active_backup_directory_is_created_beside_preserved_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            history = state / "backups"
            history.mkdir(mode=0o700, parents=True)
            history.chmod(0o700)
            old = history / "armory.sqlite3"
            old.write_bytes(b"opaque preserved history")
            old.chmod(0o600)
            before = (old.read_bytes(), old.stat().st_mode, old.stat().st_mtime_ns)
            active = history / "active"
            with patch.object(wrapper, "BACKUPS", active):
                wrapper._ensure_backup_directory(os.getuid(), os.getgid())
            info = active.lstat()
            self.assertTrue(active.is_dir())
            self.assertEqual(info.st_uid, os.getuid())
            self.assertEqual(info.st_gid, os.getgid())
            self.assertEqual(info.st_mode & 0o777, 0o700)
            self.assertEqual((old.read_bytes(), old.stat().st_mode, old.stat().st_mtime_ns), before)
            self.assertEqual(sorted(path.name for path in history.iterdir()),
                             ["active", "armory.sqlite3"])

    def test_deployment_accepts_stopped_disabled_baseline_without_starting_units(self):
        stopped = {
            wrapper.SERVICE: ("inactive", "disabled"),
            wrapper.BACKUP_TIMER: ("inactive", "disabled"),
            wrapper.BACKUP_SERVICE: ("inactive", "static"),
        }
        wrapper.validate_unit_states(stopped)
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "audit"
            audit.mkdir(mode=0o700)
            journal = wrapper.DeploymentJournal(audit, owner_uid=os.getuid())
            journal.start(stopped)
            record = json.loads((audit / "deployment-state.json").read_text())
            self.assertEqual(record["service_state"], ["active", "enabled"])
            self.assertEqual(record["backup_timer_state"], ["active", "enabled"])
            self.assertEqual(
                record["initial_unit_states"],
                {unit: list(state) for unit, state in stopped.items()},
            )
            self.assertEqual(stopped[wrapper.SERVICE], ("inactive", "disabled"))

    def test_preflight_preservation_versions_new_copy_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir = root / "repo/deploy/systemd"
            source_dir.mkdir(parents=True)
            source = source_dir / "armory-db-preflight.py"
            source.write_bytes(b"approved new preflight\n")
            audit = root / "audit"
            audit.mkdir(mode=0o700)
            previous = "a" * 40
            legacy = audit / f"armory-db-preflight.{previous}.py"
            legacy.write_bytes(b"immutable historical preflight\n")
            legacy.chmod(0o700)
            legacy_before = (legacy.read_bytes(), legacy.stat().st_mtime_ns)
            expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()

            with (
                patch.object(wrapper, "REPO", root / "repo"),
                patch.object(wrapper, "PREFLIGHT", source),
                patch.object(wrapper, "AUDIT_DIR", audit),
                patch.object(wrapper, "PREVIOUS_SHA", previous),
                patch.object(wrapper, "_assert_managed_file"),
                patch.object(wrapper.os, "chown"),
            ):
                preserved, digest = wrapper._preserve_preflight()
                self.assertEqual(digest, expected_hash)
                self.assertEqual(
                    preserved.name,
                    f"armory-db-preflight.{previous}.{expected_hash}.py",
                )
                self.assertEqual(preserved.read_bytes(), source.read_bytes())
                self.assertEqual(
                    wrapper._preserve_preflight(), (preserved, expected_hash)
                )

            self.assertEqual(
                (legacy.read_bytes(), legacy.stat().st_mtime_ns), legacy_before
            )

    def test_preflight_preservation_rejects_wrong_bytes_at_versioned_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir = root / "repo/deploy/systemd"
            source_dir.mkdir(parents=True)
            source = source_dir / "armory-db-preflight.py"
            source.write_bytes(b"approved preflight\n")
            audit = root / "audit"
            audit.mkdir(mode=0o700)
            previous = "b" * 40
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            conflict = audit / f"armory-db-preflight.{previous}.{source_hash}.py"
            conflict.write_bytes(b"different bytes\n")
            conflict.chmod(0o700)
            with (
                patch.object(wrapper, "REPO", root / "repo"),
                patch.object(wrapper, "PREFLIGHT", source),
                patch.object(wrapper, "AUDIT_DIR", audit),
                patch.object(wrapper, "PREVIOUS_SHA", previous),
                patch.object(wrapper, "_assert_managed_file"),
            ):
                with self.assertRaisesRegex(wrapper.Refusal, "different checksum"):
                    wrapper._preserve_preflight()

    def test_deployment_rejects_mixed_bot_and_timer_baseline(self):
        with self.assertRaisesRegex(wrapper.Refusal, "states do not match"):
            wrapper.validate_unit_states(
                {
                    wrapper.SERVICE: ("inactive", "disabled"),
                    wrapper.BACKUP_TIMER: ("active", "enabled"),
                    wrapper.BACKUP_SERVICE: ("inactive", "static"),
                }
            )

    def test_audit_json_hash_matches_the_same_validated_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            record = Path(temporary) / "record.json"
            raw = b'{"decision":"verified"}\n'
            record.write_bytes(raw)
            record.chmod(0o600)
            value, digest = wrapper._read_audit_json(record, owner_uid=os.getuid())
            self.assertEqual(value, {"decision": "verified"})
            self.assertEqual(digest, hashlib.sha256(raw).hexdigest())

    def test_audit_json_rejects_symlink_hardlink_and_unsafe_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.json"
            target.write_text("{}\n", encoding="ascii")
            target.chmod(0o600)
            alias = root / "record.json"
            alias.symlink_to(target)
            with self.assertRaises(wrapper.Refusal):
                wrapper._read_audit_json(alias, owner_uid=os.getuid())
            alias.unlink()
            os.link(target, alias)
            with self.assertRaises(wrapper.Refusal):
                wrapper._read_audit_json(target, owner_uid=os.getuid())
            alias.unlink()
            target.chmod(0o640)
            with self.assertRaises(wrapper.Refusal):
                wrapper._read_audit_json(target, owner_uid=os.getuid())
            target.chmod(0o600)
            with self.assertRaises(wrapper.Refusal):
                wrapper._read_audit_json(target, owner_uid=os.getuid() + 1)

    def test_audit_json_rejects_path_swap_during_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            record = Path(temporary) / "record.json"
            replacement = Path(temporary) / "replacement.json"
            record.write_text('{"value":"old"}\n', encoding="ascii")
            record.chmod(0o600)
            replacement.write_text('{"value":"new"}\n', encoding="ascii")
            replacement.chmod(0o600)
            real_read = os.read
            swapped = False

            def swap_after_read(fd, size):
                nonlocal swapped
                block = real_read(fd, size)
                if not swapped:
                    swapped = True
                    os.replace(replacement, record)
                return block

            with patch.object(wrapper.os, "read", side_effect=swap_after_read):
                with self.assertRaises(wrapper.Refusal):
                    wrapper._read_audit_json(record, owner_uid=os.getuid())

    def test_audit_json_rejects_in_place_change_during_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            record = Path(temporary) / "record.json"
            record.write_text('{"value":"old"}\n', encoding="ascii")
            record.chmod(0o600)
            real_read = os.read
            changed = False

            def change_after_read(fd, size):
                nonlocal changed
                block = real_read(fd, size)
                if not changed:
                    changed = True
                    record.write_text('{"value":"new"}\n', encoding="ascii")
                    record.chmod(0o600)
                return block

            with patch.object(wrapper.os, "read", side_effect=change_after_read):
                with self.assertRaises(wrapper.Refusal):
                    wrapper._read_audit_json(record, owner_uid=os.getuid())

    def test_audit_json_rejects_read_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            record = Path(temporary) / "record.json"
            record.write_text("{}\n", encoding="ascii")
            record.chmod(0o600)
            with patch.object(os, "read", side_effect=PermissionError):
                with self.assertRaises(wrapper.Refusal):
                    wrapper._read_audit_json(record, owner_uid=os.getuid())

    @staticmethod
    def _archive_fixture(directory: Path, *, ack_changes=None, fail_closed=True):
        release_sha = "a" * 40
        previous_sha = "b" * 40
        manifest_path = directory / f"deployment-{release_sha}.json"
        manifest = {
            "approved_release_sha": release_sha,
            "previous_release_sha": previous_sha,
            "database_backup": "/backup/test.sqlite3",
            "database_backup_sha256": "c" * 64,
            "shared_preflight_backup": "/audit/preflight.py",
            "shared_preflight_sha256": "d" * 64,
            "created_at_utc": "2026-11-15T12:00:00+00:00",
        }
        manifest_bytes = json.dumps(manifest, sort_keys=True).encode() + b"\n"
        manifest_path.write_bytes(manifest_bytes)
        manifest_path.chmod(0o600)
        journal = {
            "approved_release_sha": release_sha,
            "previous_release_sha": previous_sha,
            "phase": "manual_recovery_required",
            "fail_closed_verified": fail_closed,
            "fail_closed_error": None,
            "database_backup": manifest["database_backup"],
            "database_backup_sha256": manifest["database_backup_sha256"],
            "shared_preflight_backup": manifest["shared_preflight_backup"],
            "shared_preflight_sha256": manifest["shared_preflight_sha256"],
            "manifest": str(manifest_path),
        }
        journal_bytes = json.dumps(journal, sort_keys=True).encode() + b"\n"
        journal_sha = hashlib.sha256(journal_bytes).hexdigest()
        archive = directory / f"deployment-state-{journal_sha}.json"
        archive.write_bytes(journal_bytes)
        archive.chmod(0o600)
        ack = {
            "schema_version": 1,
            "journal_sha256": journal_sha,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "approved_release_sha": release_sha,
            "previous_release_sha": previous_sha,
            "decision": "manual_recovery_verified",
            "approved_by": "admin-example",
            "approval_reference": "CHANGE-185",
            "approved_at_utc": "2026-11-15T12:30:00+00:00",
        }
        ack.update(ack_changes or {})
        ack_path = directory / f"deployment-recovery-ack-{journal_sha}.json"
        ack_path.write_text(json.dumps(ack, sort_keys=True) + "\n", encoding="ascii")
        ack_path.chmod(0o600)
        return archive, manifest_path, ack_path

    def test_deployment_journal_allows_empty_first_install_history_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "audit"
            audit.mkdir(mode=0o700)
            with patch.object(wrapper, "REPO", Path(__file__).parents[1]):
                wrapper.DeploymentJournal(
                    audit, owner_uid=os.getuid()
                ).assert_no_pending()
            with self.assertRaises(wrapper.Refusal):
                wrapper.DeploymentJournal(
                    audit / "missing", owner_uid=os.getuid()
                ).assert_no_pending()

    def test_deployment_lock_is_held_until_wrapper_exit_after_journal_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "audit"
            audit = Path(temporary) / "audit"
            audit.mkdir(mode=0o700)
            audit.chmod(0o700)
            journal = wrapper.DeploymentJournal(audit, owner_uid=os.getuid())
            journal.acquire_exclusive_lock()
            reader = os.open(audit, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                with self.assertRaises(BlockingIOError):
                    wrapper.fcntl.flock(
                        reader, wrapper.fcntl.LOCK_SH | wrapper.fcntl.LOCK_NB
                    )
            finally:
                os.close(reader)
            journal.start(
                {
                    wrapper.SERVICE: ("active", "enabled"),
                    wrapper.BACKUP_TIMER: ("active", "enabled"),
                }
            )
            self.assertIsNotNone(journal.lock_fd)
            self.assertTrue((audit / "deployment-state.json").is_file())
            reader = os.open(audit, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                with self.assertRaises(BlockingIOError):
                    wrapper.fcntl.flock(
                        reader, wrapper.fcntl.LOCK_SH | wrapper.fcntl.LOCK_NB
                    )
            finally:
                os.close(reader)
            journal.release_lock()

    def test_journal_transition_table_allows_only_ordered_installation_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "audit"
            audit.mkdir(mode=0o700)
            journal = wrapper.DeploymentJournal(audit, owner_uid=os.getuid())
            journal.start(
                {
                    wrapper.SERVICE: ("active", "enabled"),
                    wrapper.BACKUP_TIMER: ("active", "enabled"),
                }
            )
            for phase in (
                "timer_pause_started",
                "timer_paused",
                "backup_verified",
                "backup_and_preflight_verified",
                "shutdown_started",
                "installer_started",
                "installer_returned",
                "release_verified",
                "installation_verified",
            ):
                journal.update(phase)
            self.assertIn(
                "complete", wrapper.DEPLOYMENT_TRANSITIONS["installation_verified"]
            )
            with self.assertRaisesRegex(wrapper.Refusal, "transition"):
                journal.update("complete")
            journal.release_lock()

    def test_deployment_journal_rejects_archive_without_hash_bound_ack(self):
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary)
            archive, _, ack = self._archive_fixture(audit)
            ack.unlink()
            with self.assertRaises(wrapper.Refusal):
                wrapper.DeploymentJournal(
                    audit, owner_uid=os.getuid()
                ).assert_no_pending()
            self.assertTrue(archive.is_file())

    def test_deployment_journal_rejects_manifest_after_active_journal_disappears(self):
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary)
            orphan = audit / f"deployment-{'a' * 40}.json"
            orphan.write_text("{}\n", encoding="ascii")
            orphan.chmod(0o600)
            with self.assertRaises(wrapper.Refusal):
                wrapper.DeploymentJournal(
                    audit, owner_uid=os.getuid()
                ).assert_no_pending()

    def test_deployment_journal_accepts_hash_bound_admin_ack(self):
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary)
            self._archive_fixture(audit)
            with patch.object(wrapper, "REPO", Path(__file__).parents[1]):
                wrapper.DeploymentJournal(
                    audit, owner_uid=os.getuid()
                ).assert_no_pending()

    def test_deployment_journal_rejects_tampered_hashes_and_missing_approval(self):
        for changes in (
            {"journal_sha256": "0" * 64},
            {"manifest_sha256": "0" * 64},
            {"approved_by": ""},
            {"approval_reference": ""},
        ):
            with (
                self.subTest(changes=changes),
                tempfile.TemporaryDirectory() as temporary,
            ):
                audit = Path(temporary)
                self._archive_fixture(audit, ack_changes=changes)
                with self.assertRaises(wrapper.Refusal):
                    wrapper.DeploymentJournal(
                        audit, owner_uid=os.getuid()
                    ).assert_no_pending()

    def test_deployment_journal_rejects_changed_archived_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary)
            archive, _, _ = self._archive_fixture(audit)
            archive.write_text("tampered\n", encoding="ascii")
            archive.chmod(0o600)
            with self.assertRaises(wrapper.Refusal):
                wrapper.DeploymentJournal(
                    audit, owner_uid=os.getuid()
                ).assert_no_pending()

    def test_deployment_journal_rejects_archive_without_verified_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary)
            self._archive_fixture(audit, fail_closed=False)
            with self.assertRaises(wrapper.Refusal):
                wrapper.DeploymentJournal(
                    audit, owner_uid=os.getuid()
                ).assert_no_pending()

    def test_wrapper_current_pointer_requires_exact_canonical_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            release_sha = "a" * 40
            release = base / "releases" / release_sha
            release.mkdir(parents=True)
            current = base / "current"
            with patch.object(wrapper, "BASE", base):
                for target in (str(release), f"releases/{release_sha}"):
                    current.symlink_to(target)
                    self.assertEqual(
                        wrapper._managed_current_target(current, owner_uid=os.getuid()),
                        release,
                    )
                    current.unlink()
                for target in (
                    f"releases//{release_sha}",
                    f"releases/./{release_sha}",
                    f"releases/../releases/{release_sha}",
                    f"{base}/releases//{release_sha}",
                    str(base / "outside" / release_sha),
                ):
                    with self.subTest(target=target):
                        current.symlink_to(target)
                        with self.assertRaises(wrapper.Refusal):
                            wrapper._managed_current_target(
                                current, owner_uid=os.getuid()
                            )
                        current.unlink()

    def test_protected_checkout_requires_read_only_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary) / "checkout"
            (checkout / ".git").mkdir(parents=True)
            source = checkout / "README.md"
            source.write_text("fixture\n", encoding="ascii")
            source.chmod(0o444)
            (checkout / ".git").chmod(0o555)
            checkout.chmod(0o555)
            try:
                wrapper._assert_root_owned_tree(
                    checkout, owner_uid=os.getuid(), check_parents=False
                )
                source.chmod(0o644)
                with self.assertRaises(wrapper.Refusal):
                    wrapper._assert_root_owned_tree(
                        checkout, owner_uid=os.getuid(), check_parents=False
                    )
            finally:
                checkout.chmod(0o755)

    def test_protected_checkout_accepts_root_only_directories_and_validator_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary) / "checkout"
            git_dir = checkout / ".git"
            deploy_dir = checkout / "deploy"
            git_dir.mkdir(parents=True)
            deploy_dir.mkdir()
            validator = deploy_dir / "recovery_audit.py"
            validator.write_text("def verify_audit_directory(*args, **kwargs): pass\n")
            validator.chmod(0o644)
            ordinary = checkout / "README.md"
            ordinary.write_text("fixture\n", encoding="ascii")
            ordinary.chmod(0o444)
            for directory in (checkout, git_dir, deploy_dir):
                directory.chmod(0o700)

            wrapper._assert_root_owned_tree(
                checkout, owner_uid=os.getuid(), check_parents=False
            )
            validator.chmod(0o444)
            wrapper._assert_root_owned_tree(
                checkout, owner_uid=os.getuid(), check_parents=False
            )

            validator.chmod(0o664)
            with self.assertRaises(wrapper.Refusal):
                wrapper._assert_root_owned_tree(
                    checkout, owner_uid=os.getuid(), check_parents=False
                )
            validator.chmod(0o444)
            ordinary.chmod(0o644)
            with self.assertRaises(wrapper.Refusal):
                wrapper._assert_root_owned_tree(
                    checkout, owner_uid=os.getuid(), check_parents=False
                )

    def test_protected_checkout_still_rejects_wrong_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary) / "checkout"
            (checkout / ".git").mkdir(parents=True)
            with self.assertRaises(wrapper.Refusal):
                wrapper._assert_root_owned_tree(
                    checkout, owner_uid=os.getuid() + 1, check_parents=False
                )

    def test_protected_checkout_rejects_writable_parent_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "parent"
            checkout = parent / "checkout"
            (checkout / ".git").mkdir(parents=True)
            parent.chmod(0o770)
            try:
                with self.assertRaises(wrapper.Refusal):
                    wrapper._assert_root_owned_tree(
                        checkout, owner_uid=os.getuid()
                    )
            finally:
                parent.chmod(0o700)

    def test_protected_checkout_rejects_hardlinked_regular_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary) / "checkout"
            (checkout / ".git").mkdir(parents=True)
            target = checkout / "payload"
            target.write_bytes(b"release bytes")
            target.chmod(0o444)
            os.link(target, checkout / "payload-alias")
            with self.assertRaises(wrapper.Refusal):
                wrapper._assert_root_owned_tree(
                    checkout, owner_uid=os.getuid(), check_parents=False
                )

    def test_local_git_configuration_is_allowlisted_before_checkout_queries(self):
        safe_keys = "\n".join(sorted(wrapper.ALLOWED_LOCAL_GIT_CONFIG_KEYS))
        with patch.object(wrapper, "_git", return_value=safe_keys):
            wrapper._check_git_config()
        for unexpected in (
            "filter.payload.clean",
            "include.path",
            "core.sshCommand",
            "alias.status",
        ):
            with self.subTest(unexpected=unexpected):
                with patch.object(
                    wrapper, "_git", return_value=safe_keys + "\n" + unexpected
                ):
                    with self.assertRaises(wrapper.Refusal):
                        wrapper._check_git_config()

    def test_git_queries_disable_replace_objects_and_external_helpers(self):
        self.assertEqual(wrapper.CHILD_ENV["GIT_NO_REPLACE_OBJECTS"], "1")
        with patch.object(wrapper, "_run", return_value="") as run:
            wrapper._git("status", "--porcelain")
        command = run.call_args.args[0]
        self.assertIn("core.fsmonitor=false", command)
        self.assertIn("core.hooksPath=/dev/null", command)
        self.assertIn("credential.helper=", command)
        self.assertIn("core.symlinks=false", command)
        self.assertIn("core.bare=false", command)

    def test_git_metadata_accepts_clean_repository_and_rejects_redirectors(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary) / "checkout"
            git_dir = checkout / ".git"
            (git_dir / "objects/info").mkdir(parents=True)
            (git_dir / "info").mkdir()
            (git_dir / "refs").mkdir()
            (git_dir / "packed-refs").write_text("# pack-refs with: peeled\n")
            self.assertTrue(wrapper._validate_git_metadata(checkout, owner_uid=os.getuid()))
            for relative in (
                "objects/info/alternates",
                "info/grafts",
                "shallow",
            ):
                target = git_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("redirect\n")
                self.assertRaises(wrapper.Refusal, wrapper._validate_git_metadata, checkout)
                target.unlink()
            (git_dir / "refs/replace").mkdir()
            self.assertRaises(wrapper.Refusal, wrapper._validate_git_metadata, checkout)
            (git_dir / "refs/replace").rmdir()
            (git_dir / "packed-refs").write_text("a" * 40 + " refs/replace/" + "b" * 40 + "\n")
            self.assertRaises(wrapper.Refusal, wrapper._validate_git_metadata, checkout)

    def test_git_metadata_rejects_symlinked_metadata_and_git_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "checkout"
            git_dir = checkout / ".git"
            (git_dir / "objects/info").mkdir(parents=True)
            (git_dir / "info").mkdir()
            (git_dir / "refs").mkdir()
            outside = root / "outside"
            outside.mkdir()
            (git_dir / "objects/info/alternates").symlink_to(outside)
            with self.assertRaises(wrapper.Refusal):
                wrapper._validate_git_metadata(checkout, owner_uid=os.getuid())
        for relative in (
            "objects/info/alternates",
            "objects/info/http-alternates",
            "info/grafts",
            "shallow",
            "refs/replace",
            "packed-refs",
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                checkout = root / "checkout"
                git_dir = checkout / ".git"
                (git_dir / "objects/info").mkdir(parents=True)
                (git_dir / "info").mkdir()
                (git_dir / "refs").mkdir()
                outside = root / "outside"
                outside.mkdir()
                target = git_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(outside, target_is_directory=True)
                with self.assertRaises(wrapper.Refusal):
                    wrapper._validate_git_metadata(checkout, owner_uid=os.getuid())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "real"
            real.mkdir()
            checkout = root / "checkout"
            checkout.mkdir()
            (checkout / ".git").symlink_to(real, target_is_directory=True)
            with self.assertRaises(wrapper.Refusal):
                wrapper._validate_git_metadata(checkout, owner_uid=os.getuid())

    def test_protected_checkout_git_identity_and_ancestry_remain_pinned(self):
        calls = []
        outputs = iter(
            (
                str(wrapper.REPO),
                wrapper.BRANCH,
                wrapper.RELEASE_SHA,
                wrapper.RELEASE_SHA,
                "",
                "",
                "",
            )
        )

        def fake_run(command, **kwargs):
            calls.append(command)
            return next(outputs)

        real_lstat = Path.lstat

        def fake_lstat(path):
            if path == wrapper.REPO / ".git":
                return SimpleNamespace(st_dev=1, st_ino=2)
            return real_lstat(path)

        with (
            patch.object(wrapper, "_assert_root_owned_tree"),
            patch.object(
                wrapper,
                "_validate_git_metadata",
                return_value=SimpleNamespace(st_dev=1, st_ino=2),
            ),
            patch.object(wrapper, "_check_git_config"),
            patch.object(wrapper, "_run", side_effect=fake_run),
            patch.object(Path, "lstat", fake_lstat),
        ):
            wrapper._check_checkout()

        self.assertEqual(
            [call[-3:] for call in calls[-2:]],
            [
                ("--is-ancestor", wrapper.PREVIOUS_SHA, wrapper.RELEASE_SHA),
                ("--is-ancestor", wrapper.BASE_CHECKPOINT_SHA, wrapper.RELEASE_SHA),
            ],
        )

        bad_ref = iter(
            (str(wrapper.REPO), wrapper.BRANCH, wrapper.RELEASE_SHA, "f" * 40, "")
        )
        with (
            patch.object(wrapper, "_assert_root_owned_tree"),
            patch.object(
                wrapper,
                "_validate_git_metadata",
                return_value=SimpleNamespace(st_dev=1, st_ino=2),
            ),
            patch.object(wrapper, "_check_git_config"),
            patch.object(wrapper, "_run", side_effect=lambda *_a, **_k: next(bad_ref)),
            self.assertRaises(wrapper.Refusal),
        ):
            wrapper._check_checkout()

    def test_invalid_git_metadata_is_rejected_before_any_git_query(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary) / "checkout"
            (checkout / ".git/info").mkdir(parents=True)
            (checkout / ".git/shallow").write_text("shallow commit\n")
            with (
                patch.object(wrapper, "REPO", checkout),
                patch.object(wrapper, "_assert_root_owned_tree"),
                patch.object(wrapper, "_run") as run,
                self.assertRaises(wrapper.Refusal),
            ):
                wrapper._check_checkout()
            run.assert_not_called()

    def test_recovery_validator_accepts_read_only_modes_and_checks_hash_before_exec(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "checkout"
            deploy = checkout / "deploy"
            deploy.mkdir(parents=True)
            audit = root / "audit"
            audit.mkdir(mode=0o700)
            validator = deploy / "recovery_audit.py"
            source = (
                "def verify_audit_directory(directory, **kwargs):\n"
                "    assert kwargs['preserved_historical_directories'] == "
                "('reviewed-v2.0.0', 'archive')\n"
            ).encode("ascii")
            validator.write_bytes(source)

            with (
                patch.object(wrapper, "REPO", checkout),
                patch.object(
                    wrapper, "RECOVERY_AUDIT_SHA256", hashlib.sha256(source).hexdigest()
                ),
            ):
                for mode in (0o644, 0o444):
                    validator.chmod(mode)
                    wrapper._verify_archived_deployments(
                        audit, owner_uid=os.getuid()
                    )

                alias = deploy / "validator-alias.py"
                os.link(validator, alias)
                with self.assertRaisesRegex(wrapper.Refusal, "metadata is unsafe"):
                    wrapper._verify_archived_deployments(
                        audit, owner_uid=os.getuid()
                    )
                alias.unlink()
                with self.assertRaisesRegex(wrapper.Refusal, "metadata is unsafe"):
                    wrapper._verify_archived_deployments(
                        audit, owner_uid=os.getuid() + 1
                    )

                validator.chmod(0o644)
                validator.write_bytes(source + b"# changed\n")
                validator.chmod(0o444)
                with self.assertRaisesRegex(wrapper.Refusal, "hash mismatch"):
                    wrapper._verify_archived_deployments(
                        audit, owner_uid=os.getuid()
                    )

    def test_protected_checkout_rejects_python_bytecode_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary)
            cache = checkout / "deploy/__pycache__"
            cache.mkdir(parents=True)
            (cache / "production_install.cpython-314.pyc").write_bytes(b"cache")
            self.assertTrue(wrapper._has_python_bytecode_cache(checkout))

    def test_protected_checkout_cache_scan_rejects_unreadable_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary)
            hidden = checkout / "unreadable"
            hidden.mkdir()
            hidden.chmod(0)
            try:
                with self.assertRaises(wrapper.Refusal):
                    wrapper._has_python_bytecode_cache(checkout)
            finally:
                hidden.chmod(0o700)

    def test_protected_checkout_rejects_symlinked_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "checkout"
            (checkout / ".git").mkdir(parents=True)
            outside = root / "outside"
            outside.mkdir()
            (outside / "module.pyc").write_bytes(b"cache")
            (checkout / "linked").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(wrapper.Refusal):
                wrapper._assert_root_owned_tree(
                    checkout, owner_uid=os.getuid(), check_parents=False
                )

    def test_deployment_pin_is_private_and_contains_only_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            pin = Path(temporary) / "deployment-pin.json"
            pin.write_text(
                '{"release_sha":"'
                + "a" * 40
                + '","previous_sha":"'
                + "b" * 40
                + '","installer_sha256":"'
                + "c" * 64
                + '"}\n'
            )
            pin.chmod(0o600)
            value = wrapper.read_deployment_pin(pin, expected_owner=os.getuid())
            self.assertEqual(value["release_sha"], "a" * 40)
            self.assertEqual(value["installer_sha256"], "c" * 64)
            pin.chmod(0o640)
            with self.assertRaises(wrapper.Refusal):
                wrapper.read_deployment_pin(pin, expected_owner=os.getuid())
            pin.write_text(
                '{"release_sha":"'
                + "a" * 40
                + '","release_sha":"'
                + "a" * 40
                + '","previous_sha":"'
                + "b" * 40
                + '","installer_sha256":"'
                + "c" * 64
                + '"}\n'
            )
            pin.chmod(0o600)
            with self.assertRaises(wrapper.Refusal):
                wrapper.read_deployment_pin(pin, expected_owner=os.getuid())

    def test_installer_command_targets_separate_protected_location(self):
        self.assertEqual(wrapper.installer_command()[1], str(wrapper.INSTALLER))
        self.assertEqual(wrapper.INSTALLER, wrapper.BASE / "bin/production_install.py")

    def test_checkout_identity_rejects_wrong_release_or_local_modification(self):
        with self.assertRaises(wrapper.Refusal):
            wrapper.validate_source_identity("release/v1.1.4", wrapper.RELEASE_SHA, "")
        with self.assertRaises(wrapper.Refusal):
            wrapper.validate_source_identity(
                "release/v2.0.0", wrapper.RELEASE_SHA, " M deploy/production_install.py"
            )

    def test_installer_helper_uses_protected_source_checkout(self):
        self.assertEqual(
            production_install.APPROVED_REPO,
            Path("/opt/wow-forever-armory-bot/deployment-checkout"),
        )

    def test_installed_helper_resolves_to_protected_checkout(self):
        self.assertEqual(
            production_install._repository_for_installer(
                production_install.INSTALLED_HELPER
            ),
            production_install.APPROVED_REPO,
        )

    def test_pinned_installer_rejects_hash_owner_and_permission_changes(self):
        with (
            patch.object(
                wrapper, "INSTALLER", Path("/synthetic/production_install.py")
            ),
            patch.object(wrapper, "INSTALLER_SHA256", "f" * 64),
            patch.object(wrapper, "_assert_managed_file") as managed,
            patch.object(wrapper, "_sha256", return_value="0" * 64),
        ):
            with self.assertRaises(wrapper.Refusal):
                wrapper._assert_installer()
            managed.assert_called_once_with(wrapper.INSTALLER, owner=0, mode=0o755)

    def test_managed_file_check_rejects_wrong_owner_and_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            installer = Path(temporary) / "installer.py"
            installer.write_text("pass\n")
            installer.chmod(0o755)
            wrapper._assert_managed_file(installer, owner=os.getuid(), mode=0o755)
            with self.assertRaises(wrapper.Refusal):
                wrapper._assert_managed_file(
                    installer, owner=os.getuid() + 1, mode=0o755
                )
            installer.chmod(0o775)
            with self.assertRaises(wrapper.Refusal):
                wrapper._assert_managed_file(installer, owner=os.getuid(), mode=0o755)
        with (
            patch.object(
                wrapper, "INSTALLER", Path("/synthetic/production_install.py")
            ),
            patch.object(
                wrapper,
                "_assert_managed_file",
                side_effect=wrapper.Refusal("unsafe owner or mode"),
            ),
        ):
            with self.assertRaises(wrapper.Refusal):
                wrapper._assert_installer()

    def test_real_helper_process_success_and_missing_or_invalid_category(self):
        cases = (
            (
                "import os; os.write(int(os.environ['ARMORY_INSTALL_DIAGNOSTIC_FD']), "
                "b'invalid_category\\n'); raise SystemExit(7)",
                "process_failure",
                None,
            ),
            ("raise SystemExit(8)", "process_failure", None),
            (
                "import os; os.write(int(os.environ['ARMORY_INSTALL_DIAGNOSTIC_FD']), "
                "b'installer_rejected\\n'); raise SystemExit(9)",
                "installer_rejected",
                "installer_rejected",
            ),
        )
        for source, outcome, category in cases:
            with (
                self.subTest(outcome=outcome, category=category),
                self.assertRaises(wrapper.InstallerExecutionFailure) as caught,
            ):
                wrapper._run_installer([sys.executable, "-c", source], timeout=5)
            self.assertEqual(caught.exception.outcome, outcome)
            self.assertEqual(caught.exception.category, category)

        self.assertEqual(
            wrapper._run_installer([sys.executable, "-c", "pass"], timeout=5), 0
        )

    def test_real_helper_process_timeout_and_unexpected_signal(self):
        with self.assertRaises(wrapper.InstallerExecutionFailure) as caught:
            wrapper._run_installer(
                [sys.executable, "-c", "import time; time.sleep(2)"], timeout=0.1
            )
        self.assertEqual(caught.exception.outcome, "timeout")
        with self.assertRaises(wrapper.InstallerExecutionFailure) as caught:
            wrapper._run_installer(
                [
                    sys.executable,
                    "-c",
                    "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
                ]
            )
        self.assertEqual(caught.exception.outcome, "process_failure")
        self.assertEqual(caught.exception.exit_code, -signal.SIGTERM)

    def _ops_for_installer_failure(self, root, failure, journal, fail_closed_calls):
        states = {
            wrapper.SERVICE: ("active", "enabled"),
            wrapper.BACKUP_TIMER: ("active", "enabled"),
            wrapper.BACKUP_SERVICE: ("inactive", "static"),
        }
        return {
            "states": states,
            "check_database": lambda: None,
            "pause_timer": lambda: None,
            "wait_backup_idle": lambda: None,
            "backup": lambda: (root / "backup", "hash", {}),
            "acquire_backup_locks": lambda: None,
            "recheck_backup_exclusive": lambda *args: None,
            "preserve_preflight": lambda: (root / "preflight", "hash"),
            "write_manifest": lambda *args: root / "manifest",
            "shutdown": lambda: None,
            "database_signature": lambda: (1,),
            "install": lambda: (_ for _ in ()).throw(failure),
            "fail_closed": lambda: (
                fail_closed_calls.append(True)
                or {
                    wrapper.SERVICE: ("inactive", "disabled"),
                    wrapper.BACKUP_TIMER: ("inactive", "disabled"),
                    wrapper.BACKUP_SERVICE: ("inactive", "static"),
                }
            ),
            "observe_unit_states": lambda: {},
            "verify_release": lambda *args: None,
            "write_installation_report": lambda *args: (
                "deployment-installation-test.json",
                "a" * 64,
            ),
            "restore_states": lambda original: original,
            "release_backup_locks": lambda _: None,
        }

    def test_installer_success_records_exit_code_zero(self):
        result = SimpleNamespace(returncode=0, stdout=b"ok", stderr=b"")
        with patch.object(wrapper.subprocess, "run", return_value=result) as run:
            self.assertEqual(wrapper._run_installer(["/fake/installer"]), 0)
        self.assertIs(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(run.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_successful_install_is_verified_without_restoring_services(self):
        with tempfile.TemporaryDirectory() as temporary:
            journal = MemoryJournal()
            fail_closed_calls = []
            ops = self._ops_for_installer_failure(
                Path(temporary),
                RuntimeError("unused"),
                journal,
                fail_closed_calls,
            )
            ops["install"] = lambda: 0
            restored = []
            ops["restore_states"] = lambda original: restored.append(original)
            wrapper.perform_deployment(ops, journal)
            returned = [
                details
                for phase, details in journal.records
                if phase == "installer_returned"
            ]
        self.assertEqual(returned, [{"installer_exit_code": 0}])
        self.assertEqual(restored, [])
        verified = [
            details
            for phase, details in journal.records
            if phase == "installation_verified"
        ]
        self.assertEqual(
            verified,
            [
                {
                    "installation_result": "deployment-installation-test.json",
                    "installation_result_sha256": "a" * 64,
                    "verified_at_utc": verified[0]["verified_at_utc"],
                }
            ],
        )

    def test_journal_transition_table_rejects_undefined_transitions(self):
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "audit"
            audit.mkdir(mode=0o700)
            journal = wrapper.DeploymentJournal(audit, owner_uid=os.getuid())
            journal.start(
                {
                    wrapper.SERVICE: ("active", "enabled"),
                    wrapper.BACKUP_TIMER: ("active", "enabled"),
                }
            )
            with self.assertRaisesRegex(wrapper.Refusal, "transition"):
                journal.update("complete")

    def test_journal_transition_table_allows_installation_verified_only_after_release(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "audit"
            audit.mkdir(mode=0o700)
            journal = wrapper.DeploymentJournal(audit, owner_uid=os.getuid())
            journal.start(
                {
                    wrapper.SERVICE: ("active", "enabled"),
                    wrapper.BACKUP_TIMER: ("active", "enabled"),
                }
            )
            with self.assertRaisesRegex(wrapper.Refusal, "transition"):
                journal.update("installation_verified")

    def test_nonzero_exit_records_code_and_fixed_category(self):
        result = SimpleNamespace(
            returncode=1,
            stdout=b"secret stdout",
            stderr=(b"arbitrary stderr content\nprivate detail / token=TOPSECRET"),
        )

        def run_and_write_category(*args, **kwargs):
            os.write(kwargs["pass_fds"][0], b"installer_rejected\n")
            return result

        with patch.object(
            wrapper.subprocess, "run", side_effect=run_and_write_category
        ):
            with self.assertRaises(wrapper.InstallerExecutionFailure) as caught:
                wrapper._run_installer(["/fake/installer"])
        self.assertEqual(caught.exception.outcome, "installer_rejected")
        self.assertEqual(caught.exception.exit_code, 1)
        self.assertEqual(caught.exception.category, "installer_rejected")
        self.assertNotIn("TOPSECRET", str(caught.exception))

    def test_unmarked_nonzero_exit_is_process_failure(self):
        result = SimpleNamespace(
            returncode=2,
            stdout=b"secret",
            stderr=b"ARMORY_INSTALL_DIAGNOSTIC:installer_rejected\n",
        )
        with patch.object(wrapper.subprocess, "run", return_value=result):
            with self.assertRaises(wrapper.InstallerExecutionFailure) as caught:
                wrapper._run_installer(["/fake/installer"])
        self.assertEqual(caught.exception.outcome, "process_failure")
        self.assertIsNone(caught.exception.category)

    def test_signal_termination_preserves_negative_return_code(self):
        result = SimpleNamespace(returncode=-9, stdout=b"", stderr=b"")
        with patch.object(wrapper.subprocess, "run", return_value=result):
            with self.assertRaises(wrapper.InstallerExecutionFailure) as caught:
                wrapper._run_installer(["/fake/installer"])
        self.assertEqual(caught.exception.exit_code, -9)
        self.assertEqual(caught.exception.outcome, "process_failure")

    def test_malformed_dedicated_diagnostic_fails_safely(self):
        result = SimpleNamespace(returncode=4, stdout=b"", stderr=b"")

        def write_malformed(*args, **kwargs):
            os.write(kwargs["pass_fds"][0], b"installer_rejected\nSECRET\n")
            return result

        with patch.object(wrapper.subprocess, "run", side_effect=write_malformed):
            with self.assertRaises(wrapper.InstallerExecutionFailure) as caught:
                wrapper._run_installer(["/fake/installer"])
        self.assertEqual(caught.exception.outcome, "process_failure")
        self.assertIsNone(caught.exception.category)

    def test_timeout_and_startup_metadata_reach_journal_without_raw_output(self):
        for failure in (
            wrapper.InstallerExecutionFailure("timeout"),
            wrapper.InstallerExecutionFailure("startup_failure"),
        ):
            with (
                self.subTest(outcome=failure.outcome),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                journal = MemoryJournal()
                fail_closed_calls = []
                ops = self._ops_for_installer_failure(
                    root, failure, journal, fail_closed_calls
                )
                with self.assertRaises(wrapper.Refusal):
                    wrapper.perform_deployment(ops, journal)
                record = [
                    details
                    for phase, details in journal.records
                    if phase == "manual_recovery_required"
                ][0]
                self.assertEqual(
                    record["installer_failure"],
                    {
                        "outcome": failure.outcome,
                        "exit_code": None,
                        "category": None,
                    },
                )
                self.assertEqual(fail_closed_calls, [True])

    def test_interruption_after_shutdown_is_journaled_for_manual_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = MemoryJournal()
            fail_closed_calls = []
            failure = RuntimeError("sensitive synthetic exception")
            ops = self._ops_for_installer_failure(
                root, failure, journal, fail_closed_calls
            )
            ops["shutdown"] = lambda: (_ for _ in ()).throw(failure)
            with self.assertRaises(wrapper.Refusal):
                wrapper.perform_deployment(ops, journal)
            record = [
                details
                for phase, details in journal.records
                if phase == "manual_recovery_required"
            ][0]
            self.assertEqual(record["interrupted_phase"], "shutdown_started")
            self.assertNotIn("sensitive synthetic exception", repr(record))
            self.assertIsNone(record["installer_failure"])
            self.assertEqual(fail_closed_calls, [True])

    def test_failure_during_timer_pause_attempts_timer_restoration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = MemoryJournal()
            fail_closed_calls = []
            ops = self._ops_for_installer_failure(
                root,
                RuntimeError("synthetic wait failure"),
                journal,
                fail_closed_calls,
            )
            restored = []
            states = ops["states"]
            ops["observe_unit_states"] = lambda: states

            def restore_timer(state):
                restored.append(state)
                states[wrapper.BACKUP_TIMER] = tuple(state)

            ops["wait_backup_idle"] = lambda: (_ for _ in ()).throw(
                RuntimeError("private detail")
            )
            ops["restore_timer"] = restore_timer
            with self.assertRaises(wrapper.Refusal):
                wrapper.perform_deployment(ops, journal)
            self.assertEqual(restored, [ops["states"][wrapper.BACKUP_TIMER]])
            record = [
                details
                for phase, details in journal.records
                if phase == "aborted_before_service_changes"
            ][0]
            self.assertNotIn("private detail", repr(record))
            self.assertIsNone(record["installer_failure"])

    def test_timer_restore_error_is_recorded_as_manual_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            journal = MemoryJournal()
            ops = self._ops_for_installer_failure(
                Path(temporary), RuntimeError("unused"), journal, []
            )
            ops["wait_backup_idle"] = lambda: (_ for _ in ()).throw(
                RuntimeError("synthetic wait failure")
            )
            ops["restore_timer"] = lambda _state: (_ for _ in ()).throw(
                RuntimeError("synthetic timer restoration failure")
            )
            with self.assertRaises(wrapper.Refusal):
                wrapper.perform_deployment(ops, journal)
            phases = [phase for phase, _details in journal.records]
            self.assertIn("manual_recovery_required", phases)
            self.assertNotIn("aborted_before_service_changes", phases)
            manual = next(
                details
                for phase, details in journal.records
                if phase == "manual_recovery_required"
            )
            self.assertEqual(manual["timer_restore_error"], "RuntimeError")

    def test_backup_verified_preflight_failure_aborts_after_exact_state_restore(self):
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "audit"
            audit.mkdir(mode=0o700)
            journal = wrapper.DeploymentJournal(audit, owner_uid=os.getuid())
            states = {
                wrapper.SERVICE: ("inactive", "disabled"),
                wrapper.BACKUP_TIMER: ("inactive", "disabled"),
                wrapper.BACKUP_SERVICE: ("inactive", "static"),
            }
            restored = []
            ops = {
                "states": states,
                "check_database": lambda: None,
                "pause_timer": lambda: None,
                "wait_backup_idle": lambda: None,
                "backup": lambda: (audit / "backup.sqlite3", "a" * 64, {}),
                "acquire_backup_locks": lambda: None,
                "recheck_backup_exclusive": lambda *args: None,
                "preserve_preflight": lambda: (_ for _ in ()).throw(
                    wrapper.Refusal("preflight path is occupied")
                ),
                "write_manifest": lambda *args: audit / "manifest.json",
                "shutdown": lambda: self.fail("bot shutdown must not start"),
                "fail_closed": lambda: self.fail("fail-closed stop is not needed"),
                "restore_timer": lambda state: restored.append(state),
                "observe_unit_states": lambda: states,
                "release_backup_locks": lambda _: None,
            }

            with self.assertRaises(wrapper.Refusal):
                wrapper.perform_deployment(ops, journal)

            record = json.loads((audit / "deployment-state.json").read_text())
            self.assertEqual(record["phase"], "aborted_before_service_changes")
            self.assertEqual(record["interrupted_phase"], "backup_verified")
            self.assertEqual(restored, [states[wrapper.BACKUP_TIMER]])
            self.assertEqual(
                record["final_unit_states"],
                {unit: list(state) for unit, state in states.items()},
            )
            self.assertEqual(
                record["initial_unit_states"],
                {unit: list(state) for unit, state in states.items()},
            )

    def test_pre_shutdown_abort_refuses_if_any_initial_state_was_not_restored(self):
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "audit"
            audit.mkdir(mode=0o700)
            journal = wrapper.DeploymentJournal(audit, owner_uid=os.getuid())
            states = {
                wrapper.SERVICE: ("active", "enabled"),
                wrapper.BACKUP_TIMER: ("active", "enabled"),
                wrapper.BACKUP_SERVICE: ("inactive", "static"),
            }
            observed = dict(states)
            observed[wrapper.BACKUP_TIMER] = ("inactive", "disabled")
            ops = {
                "states": states,
                "check_database": lambda: None,
                "pause_timer": lambda: None,
                "wait_backup_idle": lambda: None,
                "backup": lambda: (audit / "backup.sqlite3", "b" * 64, {}),
                "acquire_backup_locks": lambda: None,
                "recheck_backup_exclusive": lambda *args: None,
                "preserve_preflight": lambda: (_ for _ in ()).throw(
                    wrapper.Refusal("preflight path is occupied")
                ),
                "write_manifest": lambda *args: audit / "manifest.json",
                "shutdown": lambda: self.fail("bot shutdown must not start"),
                "fail_closed": lambda: self.fail("fail-closed stop is not needed"),
                "restore_timer": lambda state: None,
                "observe_unit_states": lambda: observed,
                "release_backup_locks": lambda _: None,
            }

            with self.assertRaises(wrapper.Refusal):
                wrapper.perform_deployment(ops, journal)

            record = json.loads((audit / "deployment-state.json").read_text())
            self.assertEqual(record["phase"], "manual_recovery_required")
            self.assertEqual(record["interrupted_phase"], "backup_verified")

    def test_real_journal_accepts_safe_abort_before_service_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "audit"
            audit.mkdir(mode=0o700)
            journal = wrapper.DeploymentJournal(audit, owner_uid=os.getuid())
            journal.acquire_exclusive_lock()
            states = {
                wrapper.SERVICE: ("active", "enabled"),
                wrapper.BACKUP_TIMER: ("active", "enabled"),
                wrapper.BACKUP_SERVICE: ("inactive", "static"),
            }
            ops = {
                "states": states,
                "check_database": lambda: None,
                "pause_timer": lambda: None,
                "wait_backup_idle": lambda: (_ for _ in ()).throw(
                    RuntimeError("synthetic wait failure")
                ),
                "restore_timer": lambda target: None,
                "observe_unit_states": lambda: states,
                "release_backup_locks": lambda _: None,
            }
            try:
                with self.assertRaises(wrapper.Refusal):
                    wrapper.perform_deployment(ops, journal)
                record = json.loads((audit / "deployment-state.json").read_text())
                self.assertEqual(record["phase"], "aborted_before_service_changes")
                self.assertEqual(
                    record["final_unit_states"][wrapper.BACKUP_TIMER],
                    list(states[wrapper.BACKUP_TIMER]),
                )
            finally:
                journal.release_lock()

    def test_fail_closed_cleanup_error_is_recorded_without_exception_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = MemoryJournal()
            fail_closed_calls = []
            failure = wrapper.InstallerExecutionFailure("timeout")
            ops = self._ops_for_installer_failure(
                root, failure, journal, fail_closed_calls
            )
            ops["fail_closed"] = lambda: (_ for _ in ()).throw(
                RuntimeError("private cleanup detail")
            )
            with self.assertRaises(wrapper.Refusal):
                wrapper.perform_deployment(ops, journal)
            record = [
                details
                for phase, details in journal.records
                if phase == "manual_recovery_required"
            ][0]
            self.assertFalse(record["fail_closed_verified"])
            self.assertEqual(record["fail_closed_error"], "RuntimeError")
            self.assertNotIn("private cleanup detail", repr(record))

    def test_fail_closed_is_not_verified_if_unmanaged_bot_process_remains(self):
        observed = {
            wrapper.SERVICE: ("inactive", "disabled"),
            wrapper.BACKUP_TIMER: ("inactive", "disabled"),
            wrapper.BACKUP_SERVICE: ("inactive", "static"),
        }
        with (
            patch.object(wrapper, "_service_action"),
            patch.object(wrapper, "_observe_unit_states", return_value=observed),
            patch.object(wrapper, "_armory_process_present", return_value=True),
        ):
            with self.assertRaisesRegex(wrapper.Refusal, "process remains"):
                wrapper._fail_closed()

    def test_process_inventory_finds_manual_bot_process_without_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            process = proc / "4321"
            process.mkdir()
            (process / "cmdline").write_bytes(
                b"/usr/bin/python3\0-m\0wow_forever_armory.discord_bot\0"
            )
            (process / "environ").write_bytes(
                b"DISCORD_BOT_TOKEN=synthetic-never-print\0"
            )
            self.assertTrue(wrapper._armory_process_present(proc))

    def test_startup_failure_is_distinct(self):
        with patch.object(
            wrapper.subprocess, "run", side_effect=FileNotFoundError("sensitive path")
        ):
            with self.assertRaises(wrapper.InstallerExecutionFailure) as caught:
                wrapper._run_installer(["/missing/installer"])
        self.assertEqual(caught.exception.outcome, "startup_failure")
        self.assertIsNone(caught.exception.exit_code)

    def test_timeout_is_distinct(self):
        with patch.object(
            wrapper.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("installer", 3, output=b"SECRET"),
        ):
            with self.assertRaises(wrapper.InstallerExecutionFailure) as caught:
                wrapper._run_installer(["/fake/installer"], timeout=3)
        self.assertEqual(caught.exception.outcome, "timeout")
        self.assertIsNone(caught.exception.exit_code)
        self.assertNotIn("SECRET", str(caught.exception))

    def test_diagnostic_handling_does_not_touch_database_or_release_pointer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "db.sqlite3"
            database.write_bytes(b"synthetic database bytes")
            db_before = hashlib.sha256(database.read_bytes()).hexdigest()
            old_release = root / "release-old"
            pointer = root / "current"
            old_release.mkdir()
            pointer.symlink_to(old_release)
            journal = MemoryJournal()
            failure = wrapper.InstallerExecutionFailure(
                "installer_rejected", 9, "installer_rejected"
            )
            states = {
                wrapper.SERVICE: ("active", "enabled"),
                wrapper.BACKUP_TIMER: ("active", "enabled"),
                wrapper.BACKUP_SERVICE: ("inactive", "static"),
            }
            fail_closed_calls = []
            ops = {
                "states": states,
                "check_database": lambda: None,
                "pause_timer": lambda: None,
                "wait_backup_idle": lambda: None,
                "backup": lambda: (root / "backup", "hash", {}),
                "acquire_backup_locks": lambda: None,
                "recheck_backup_exclusive": lambda *args: None,
                "preserve_preflight": lambda: (root / "preflight", "hash"),
                "write_manifest": lambda *args: root / "manifest",
                "shutdown": lambda: None,
                "database_signature": lambda: (1,),
                "install": lambda: (_ for _ in ()).throw(failure),
                "fail_closed": lambda: (
                    fail_closed_calls.append(True)
                    or {
                        wrapper.SERVICE: ("inactive", "disabled"),
                        wrapper.BACKUP_TIMER: ("inactive", "disabled"),
                        wrapper.BACKUP_SERVICE: ("inactive", "static"),
                    }
                ),
                "observe_unit_states": lambda: {},
                "release_backup_locks": lambda _: None,
            }
            with self.assertRaises(wrapper.Refusal):
                wrapper.perform_deployment(ops, journal)
            self.assertEqual(pointer.resolve(), old_release)
            self.assertEqual(fail_closed_calls, [True])
            self.assertEqual(
                hashlib.sha256(database.read_bytes()).hexdigest(), db_before
            )
            failure_records = [
                data
                for phase, data in journal.records
                if phase == "manual_recovery_required"
            ]
            self.assertEqual(
                failure_records[0]["installer_failure"],
                {
                    "outcome": "installer_rejected",
                    "exit_code": 9,
                    "category": "installer_rejected",
                },
            )
            self.assertTrue(failure_records[0]["fail_closed_verified"])

    def test_installer_and_wrapper_category_contract_matches(self):
        self.assertIn("installer_rejected", wrapper.INSTALLER_DIAGNOSTIC_CATEGORIES)
        self.assertEqual(
            production_install.InstallError.diagnostic_category, "installer_rejected"
        )
        source = (
            Path(__file__).parents[1] / "deploy/production_install.py"
        ).read_text()
        self.assertIn("ARMORY_INSTALL_DIAGNOSTIC_FD", source)

    def test_installer_emits_category_only_to_dedicated_descriptor(self):
        read_fd, write_fd = os.pipe()
        try:
            with (
                patch.dict(os.environ, {"ARMORY_INSTALL_DIAGNOSTIC_FD": str(write_fd)}),
                patch.object(
                    production_install.sys,
                    "argv",
                    [
                        "installer",
                        "--release-sha",
                        "a" * 40,
                        "--upgrade-existing",
                    ],
                ),
                patch.object(
                    production_install,
                    "install",
                    side_effect=production_install.InstallError("safe fixed error"),
                ),
                contextlib.redirect_stderr(io.StringIO()) as human_output,
            ):
                code = production_install.main()
            os.close(write_fd)
            write_fd = -1
            category = os.read(read_fd, 128)
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)
        self.assertEqual(code, 1)
        self.assertEqual(category, b"installer_rejected\n")
        self.assertNotIn("ARMORY_INSTALL_DIAGNOSTIC", human_output.getvalue())

    def test_installer_emits_specific_allowlisted_category(self):
        read_fd, write_fd = os.pipe()
        try:
            with (
                patch.dict(os.environ, {"ARMORY_INSTALL_DIAGNOSTIC_FD": str(write_fd)}),
                patch.object(
                    production_install.sys,
                    "argv",
                    [
                        "installer",
                        "--release-sha",
                        "a" * 40,
                        "--upgrade-existing",
                    ],
                ),
                patch.object(
                    production_install,
                    "install",
                    side_effect=production_install.InstallError(
                        "safe fixed error", category="release_permissions_rejected"
                    ),
                ),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(production_install.main(), 1)
            os.close(write_fd)
            write_fd = -1
            category = os.read(read_fd, 128)
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)
        self.assertEqual(category, b"release_permissions_rejected\n")
        self.assertIn(
            "release_permissions_rejected",
            wrapper.INSTALLER_DIAGNOSTIC_CATEGORIES,
        )


if __name__ == "__main__":
    unittest.main()
