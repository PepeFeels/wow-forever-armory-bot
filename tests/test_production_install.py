"""Offline checks for installation gates and the database startup guard."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import unittest
from contextlib import ExitStack
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from deploy import production_db_snapshot, production_install
from deploy.production_install import (
    InstallError,
    _assert_units_stopped_and_disabled,
    _check_repository,
    _copy_unit,
    _install_current_link,
    _install_release,
    _require_supported_runtime,
    _validate_logo_asset,
    _verify_upgrade_database,
    validate_config,
    validate_inputs,
)


def _load_preflight():
    script = Path(__file__).parents[1] / "deploy/systemd/armory-db-preflight.py"
    loader = SourceFileLoader("armory_db_preflight", str(script))
    spec = importlib.util.spec_from_loader("armory_db_preflight", loader)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not import database preflight")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


preflight = _load_preflight()


def _create_preflight_database(path: Path, *, guild_count: int = 0) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE guilds (guild_id INTEGER PRIMARY KEY);"
            "CREATE TABLE members (guild_id INTEGER, user_id INTEGER);"
            "CREATE TABLE characters (guild_id INTEGER, user_id INTEGER);"
            "CREATE TABLE flows (guild_id INTEGER, user_id INTEGER);"
        )
        connection.executemany(
            "INSERT INTO guilds VALUES (?)",
            ((guild_id,) for guild_id in range(1, guild_count + 1)),
        )
    path.chmod(0o600)


class ProductionInstallTests(unittest.TestCase):
    def test_startup_gate_blocks_unresolved_deployment_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "audit"
            audit.mkdir(mode=0o700)
            audit.chmod(0o700)
            self.assertTrue(
                preflight.deployment_recovery_is_clear(
                    audit, expected_owner=os.getuid(), expected_group=os.getgid()
                )
            )

            for name in (
                preflight.ACTIVE_JOURNAL,
                preflight.JOURNAL_TEMPORARY,
            ):
                with self.subTest(name=name):
                    pending = audit / name
                    pending.write_bytes(b"synthetic pending evidence\n")
                    self.assertFalse(
                        preflight.deployment_recovery_is_clear(
                            audit,
                            expected_owner=os.getuid(),
                            expected_group=os.getgid(),
                        )
                    )
                    pending.unlink()

    def test_startup_gate_blocks_symlinked_journal_and_unsafe_audit_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audit = root / "audit"
            audit.mkdir(mode=0o700)
            audit.chmod(0o700)
            outside = root / "outside"
            outside.write_bytes(b"preserved evidence\n")
            (audit / preflight.ACTIVE_JOURNAL).symlink_to(outside)
            self.assertFalse(
                preflight.deployment_recovery_is_clear(
                    audit, expected_owner=os.getuid(), expected_group=os.getgid()
                )
            )
            self.assertEqual(outside.read_bytes(), b"preserved evidence\n")
            (audit / preflight.ACTIVE_JOURNAL).unlink()
            audit.chmod(0o750)
            self.assertFalse(
                preflight.deployment_recovery_is_clear(
                    audit, expected_owner=os.getuid(), expected_group=os.getgid()
                )
            )
            self.assertFalse(
                preflight.deployment_recovery_is_clear(
                    root / "missing-audit",
                    expected_owner=os.getuid(),
                    expected_group=os.getgid(),
                )
            )

    def test_startup_gate_rejects_audit_symlink_hardlink_and_special_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audit = root / "audit"
            audit.mkdir(mode=0o700)
            audit.chmod(0o700)
            target = root / "outside.json"
            target.write_text("{}\n", encoding="ascii")
            target.chmod(0o600)
            review_name = f"recovery-review-{'a' * 64}.json"
            entry = audit / review_name
            kwargs = {"expected_owner": os.getuid(), "expected_group": os.getgid()}

            entry.symlink_to(target)
            self.assertFalse(preflight.deployment_recovery_is_clear(audit, **kwargs))
            entry.unlink()

            os.link(target, entry)
            self.assertFalse(preflight.deployment_recovery_is_clear(audit, **kwargs))
            entry.unlink()

            os.mkfifo(entry, 0o600)
            self.assertFalse(preflight.deployment_recovery_is_clear(audit, **kwargs))

    def test_startup_gate_requires_complete_hash_bound_incident_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "audit"
            audit.mkdir(mode=0o700)
            audit.chmod(0o700)
            release, previous = "a" * 40, "b" * 40
            manifest = {
                "approved_release_sha": release,
                "previous_release_sha": previous,
                "database_backup": "/backup/test.sqlite3",
                "database_backup_sha256": "c" * 64,
                "shared_preflight_backup": "/audit/preflight.py",
                "shared_preflight_sha256": "d" * 64,
                "created_at_utc": "2026-11-15T12:00:00+00:00",
            }
            manifest_raw = json.dumps(manifest, sort_keys=True).encode("ascii")
            manifest_path = audit / f"deployment-{release}.json"
            manifest_path.write_bytes(manifest_raw)
            manifest_path.chmod(0o600)
            journal = {
                **manifest,
                "phase": "manual_recovery_required",
                "fail_closed_verified": True,
                "fail_closed_error": None,
                "manifest": str(manifest_path),
            }
            journal_raw = json.dumps(journal, sort_keys=True).encode("ascii") + b"\n"
            journal_sha = hashlib.sha256(journal_raw).hexdigest()
            archive = audit / f"deployment-state-{journal_sha}.json"
            archive.write_bytes(journal_raw)
            archive.chmod(0o600)
            ack = {
                "schema_version": 1,
                "journal_sha256": journal_sha,
                "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
                "approved_release_sha": release,
                "previous_release_sha": previous,
                "decision": "manual_recovery_verified",
                "approved_by": "reviewer",
                "approval_reference": "review-record-1",
                "approved_at_utc": "2026-11-15T12:00:00+00:00",
            }
            ack_path = audit / f"deployment-recovery-ack-{journal_sha}.json"
            ack_path.write_text(json.dumps(ack, sort_keys=True), encoding="ascii")
            ack_path.chmod(0o600)
            kwargs = {"expected_owner": os.getuid(), "expected_group": os.getgid()}
            self.assertTrue(preflight.deployment_recovery_is_clear(audit, **kwargs))

            contradictory = dict(journal, recovery_note="different retained record")
            contradictory_raw = (
                json.dumps(contradictory, sort_keys=True).encode("ascii") + b"\n"
            )
            contradictory_sha = hashlib.sha256(contradictory_raw).hexdigest()
            contradictory_archive = audit / f"deployment-state-{contradictory_sha}.json"
            contradictory_archive.write_bytes(contradictory_raw)
            contradictory_archive.chmod(0o600)
            contradictory_ack = dict(
                ack,
                journal_sha256=contradictory_sha,
                manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
            )
            contradictory_ack_path = (
                audit / f"deployment-recovery-ack-{contradictory_sha}.json"
            )
            contradictory_ack_path.write_text(
                json.dumps(contradictory_ack, sort_keys=True), encoding="ascii"
            )
            contradictory_ack_path.chmod(0o600)
            self.assertFalse(preflight.deployment_recovery_is_clear(audit, **kwargs))
            contradictory_archive.unlink()
            contradictory_ack_path.unlink()

            # Correctly completed and acknowledged incidents permit normal start.
            journal["phase"] = "complete"
            ack["decision"] = "deployment_completion_verified"
            archive.unlink()
            ack_path.unlink()
            journal_raw = json.dumps(journal, sort_keys=True).encode("ascii") + b"\n"
            journal_sha = hashlib.sha256(journal_raw).hexdigest()
            archive = audit / f"deployment-state-{journal_sha}.json"
            archive.write_bytes(journal_raw)
            archive.chmod(0o600)
            ack["journal_sha256"] = journal_sha
            ack_path = audit / f"deployment-recovery-ack-{journal_sha}.json"
            ack_path.write_text(json.dumps(ack, sort_keys=True), encoding="ascii")
            ack_path.chmod(0o600)
            self.assertTrue(preflight.deployment_recovery_is_clear(audit, **kwargs))

            # A bad fail-closed marker blocks even with a matching archive/ack.
            journal["phase"] = "manual_recovery_required"
            journal["fail_closed_verified"] = False
            ack["decision"] = "manual_recovery_verified"
            archive.unlink()
            ack_path.unlink()
            journal_raw = json.dumps(journal, sort_keys=True).encode("ascii") + b"\n"
            journal_sha = hashlib.sha256(journal_raw).hexdigest()
            archive = audit / f"deployment-state-{journal_sha}.json"
            archive.write_bytes(journal_raw)
            archive.chmod(0o600)
            ack["journal_sha256"] = journal_sha
            ack_path = audit / f"deployment-recovery-ack-{journal_sha}.json"
            ack_path.write_text(json.dumps(ack, sort_keys=True), encoding="ascii")
            ack_path.chmod(0o600)
            self.assertFalse(preflight.deployment_recovery_is_clear(audit, **kwargs))

            # Restore a valid incident, then reject a bad approval reference.
            journal["fail_closed_verified"] = True
            archive.unlink()
            ack_path.unlink()
            journal_raw = json.dumps(journal, sort_keys=True).encode("ascii") + b"\n"
            journal_sha = hashlib.sha256(journal_raw).hexdigest()
            archive = audit / f"deployment-state-{journal_sha}.json"
            archive.write_bytes(journal_raw)
            archive.chmod(0o600)
            ack.update(journal_sha256=journal_sha, approval_reference="bad ref")
            ack_path = audit / f"deployment-recovery-ack-{journal_sha}.json"
            ack_path.write_text(json.dumps(ack, sort_keys=True), encoding="ascii")
            ack_path.chmod(0o600)
            self.assertFalse(preflight.deployment_recovery_is_clear(audit, **kwargs))

            # Contradictory backup evidence in the manifest is rejected.
            ack["approval_reference"] = "CHANGE-1"
            ack_path.write_text(json.dumps(ack, sort_keys=True), encoding="ascii")
            manifest["database_backup_sha256"] = "e" * 64
            manifest_raw = json.dumps(manifest, sort_keys=True).encode("ascii")
            manifest_path.write_bytes(manifest_raw)
            manifest_path.chmod(0o600)
            ack["manifest_sha256"] = hashlib.sha256(manifest_raw).hexdigest()
            ack_path.write_text(json.dumps(ack, sort_keys=True), encoding="ascii")
            ack_path.chmod(0o600)
            ack["approval_reference"] = "CHANGE-1"
            self.assertFalse(preflight.deployment_recovery_is_clear(audit, **kwargs))

            manifest["database_backup_sha256"] = "c" * 64
            manifest["shared_preflight_sha256"] = "f" * 64
            manifest_raw = json.dumps(manifest, sort_keys=True).encode("ascii")
            manifest_path.write_bytes(manifest_raw)
            manifest_path.chmod(0o600)
            ack["manifest_sha256"] = hashlib.sha256(manifest_raw).hexdigest()
            ack_path.write_text(json.dumps(ack, sort_keys=True), encoding="ascii")
            ack_path.chmod(0o600)
            self.assertFalse(preflight.deployment_recovery_is_clear(audit, **kwargs))

    def test_startup_gate_detects_audit_directory_mutation_during_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "audit"
            audit.mkdir(mode=0o700)
            audit.chmod(0o700)
            original_listdir = os.listdir
            changed = False

            def mutate_after_first_listing(path):
                nonlocal changed
                entries = original_listdir(path)
                if not changed:
                    changed = True
                    (audit / "deployment-unexpected.json").write_text("{}\n")
                    (audit / "deployment-unexpected.json").chmod(0o600)
                return entries

            with patch.object(
                preflight.os, "listdir", side_effect=mutate_after_first_listing
            ):
                self.assertFalse(
                    preflight.deployment_recovery_is_clear(
                        audit, expected_owner=os.getuid(), expected_group=os.getgid()
                    )
                )

    def test_startup_main_stops_before_database_check_when_recovery_is_pending(
        self,
    ) -> None:
        with (
            patch.dict(os.environ, {"ARMORY_DATABASE_PATH": "/unused/database"}),
            patch.object(preflight, "deployment_recovery_is_clear", return_value=False),
            patch.object(preflight, "armory_process_present") as process_check,
            patch.object(preflight, "database_metadata_is_safe") as database_check,
        ):
            self.assertEqual(preflight.main([]), 78)
            process_check.assert_not_called()
            database_check.assert_not_called()

    def _make_venv_release(self, root: Path, system_python: Path) -> Path:
        release = root / "release"
        bin_dir = release / ".venv/bin"
        bin_dir.mkdir(parents=True)
        (release / ".venv/lib").mkdir()
        (release / ".venv/lib64").symlink_to("lib", target_is_directory=True)
        (release / "app.py").write_text("pass\n", encoding="ascii")
        (release / "app.py").chmod(0o644)
        system_python.write_text("#!/bin/sh\n", encoding="ascii")
        system_python.chmod(0o755)
        (bin_dir / "python3.14").symlink_to(system_python)
        for name in ("python", "python3", "𝜋thon"):
            (bin_dir / name).symlink_to("python3.14")
        return release

    def test_release_readability_accepts_exact_python314_venv_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system_python = root / "system-python"
            release = self._make_venv_release(root, system_python)
            with patch.object(
                production_install,
                "_trusted_directory_chain",
                return_value=True,
            ):
                production_install._verify_release_readable(
                    release,
                    os.getuid(),
                    os.getgid(),
                    owner_uid=os.getuid(),
                    system_python=system_python,
                )

    def test_release_readability_rejects_unlisted_internal_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system_python = root / "system-python"
            release = self._make_venv_release(root, system_python)
            (release / "app-alias.py").symlink_to("app.py")
            with patch.object(
                production_install,
                "_trusted_directory_chain",
                return_value=True,
            ):
                with self.assertRaisesRegex(InstallError, "unapproved symbolic link"):
                    production_install._verify_release_readable(
                        release,
                        os.getuid(),
                        os.getgid(),
                        owner_uid=os.getuid(),
                        system_python=system_python,
                    )

    def test_release_readability_rejects_wrong_python314_pi_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system_python = root / "system-python"
            release = self._make_venv_release(root, system_python)
            pi_link = release / ".venv/bin/𝜋thon"
            pi_link.unlink()
            pi_link.symlink_to(system_python)
            with patch.object(
                production_install,
                "_trusted_directory_chain",
                return_value=True,
            ):
                with self.assertRaisesRegex(InstallError, "unapproved symbolic link"):
                    production_install._verify_release_readable(
                        release,
                        os.getuid(),
                        os.getgid(),
                        owner_uid=os.getuid(),
                        system_python=system_python,
                    )

    def test_release_build_uses_readable_modes_and_restores_restrictive_umask(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = io.BytesIO()
            with tarfile.open(fileobj=bundle, mode="w") as archive:
                for name in ("src/", "src/pkg/"):
                    directory = tarfile.TarInfo(name)
                    directory.type = tarfile.DIRTYPE
                    directory.mode = 0o755
                    archive.addfile(directory)
                source = tarfile.TarInfo("src/pkg/module.py")
                source.mode = 0o644
                source_data = b"value = 1\n"
                source.size = len(source_data)
                archive.addfile(source, io.BytesIO(source_data))
            archive_bytes = bundle.getvalue()
            commands = []

            def fake_run(command, **kwargs):
                commands.append(command)
                if command[0] == "git":
                    return subprocess.CompletedProcess(command, 0, stdout=archive_bytes)
                if "-m" in command and "venv" in command:
                    environment = Path(command[-1])
                    binary_dir = environment / "bin"
                    binary_dir.mkdir(parents=True, mode=0o777)
                    interpreter = binary_dir / "python"
                    descriptor = os.open(interpreter, os.O_CREAT | os.O_WRONLY, 0o777)
                    os.close(descriptor)
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            release = root / "release"
            previous_umask = os.umask(0o077)
            try:
                original_stat = Path.stat

                def root_owned_stat(path, *, follow_symlinks=True):
                    result = original_stat(path, follow_symlinks=follow_symlinks)
                    return SimpleNamespace(st_uid=0, st_gid=0, st_mode=result.st_mode)

                with (
                    patch.object(production_install, "_require_supported_runtime"),
                    patch.object(
                        production_install.subprocess, "run", side_effect=fake_run
                    ),
                    patch.object(production_install.os, "chown"),
                    patch.object(Path, "stat", root_owned_stat),
                ):
                    production_install._install_release(
                        root, release, os.getuid(), os.getgid(), "a" * 40
                    )
                observed_umask = os.umask(0o022)
                os.umask(observed_umask)
                self.assertEqual(observed_umask, 0o077)
                self.assertEqual(
                    stat.S_IMODE((release / ".venv").stat().st_mode), 0o755
                )
                self.assertEqual(
                    stat.S_IMODE((release / ".venv/bin/python").stat().st_mode),
                    0o755,
                )
                syntax_check = next(
                    command
                    for command in commands
                    if any("ast.parse" in part for part in command)
                )
                self.assertIn("-B", syntax_check)
                self.assertFalse((release / "src/pkg/__pycache__").exists())
            finally:
                os.umask(previous_umask)

    def test_release_build_restores_umask_after_failure(self) -> None:
        previous_umask = os.umask(0o077)
        try:
            with patch.object(
                production_install,
                "_install_release_impl",
                side_effect=InstallError("synthetic failure"),
            ):
                with self.assertRaises(InstallError):
                    production_install._install_release(
                        Path("/synthetic/repo"),
                        Path("/synthetic/release"),
                        os.getuid(),
                        os.getgid(),
                        "a" * 40,
                    )
            observed_umask = os.umask(0o022)
            os.umask(observed_umask)
            self.assertEqual(observed_umask, 0o077)
        finally:
            os.umask(previous_umask)

    def test_child_failure_reports_safe_executable_and_exit_code(self) -> None:
        failure = subprocess.CalledProcessError(
            1,
            ["/usr/bin/python3.14", "-m", "pip"],
            stderr="secret-token /etc/private/config",
        )
        with patch(
            "deploy.production_install.subprocess.run", side_effect=failure
        ) as run:
            with self.assertRaises(InstallError) as raised:
                production_install._run(["/usr/bin/python3.14", "-m", "pip"])

        self.assertEqual(
            str(raised.exception), "installation command failed: python3.14 (exit 1)"
        )
        self.assertNotIn("secret-token", str(raised.exception))
        self.assertNotIn("/etc/private/config", str(raised.exception))
        self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_installer_disables_source_tree_bytecode_writes(self) -> None:
        self.assertTrue(production_install.sys.dont_write_bytecode)

    def test_upgrade_requires_units_confirmed_stopped_and_disabled(self) -> None:
        stopped = SimpleNamespace(returncode=3, stdout="inactive\n", stderr="")
        disabled = SimpleNamespace(returncode=1, stdout="disabled\n", stderr="")
        with patch(
            "deploy.production_install.subprocess.run",
            side_effect=[stopped, stopped, stopped, disabled, disabled],
        ):
            _assert_units_stopped_and_disabled(upgrade_existing=True)

        active = SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        with patch("deploy.production_install.subprocess.run", return_value=active):
            with self.assertRaisesRegex(InstallError, "stopped"):
                _assert_units_stopped_and_disabled(upgrade_existing=True)

        enabled = SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")
        with patch(
            "deploy.production_install.subprocess.run",
            side_effect=[stopped, stopped, stopped, enabled],
        ):
            with self.assertRaisesRegex(InstallError, "enabled"):
                _assert_units_stopped_and_disabled(upgrade_existing=True)

        unavailable = SimpleNamespace(returncode=1, stdout="", stderr="no bus")
        with patch(
            "deploy.production_install.subprocess.run", return_value=unavailable
        ):
            with self.assertRaisesRegex(InstallError, "stopped"):
                _assert_units_stopped_and_disabled(upgrade_existing=True)

    def test_exact_backup_unit_transition_is_verified_and_dormant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "systemd/wow-forever-armory-backup.service"
            destination.parent.mkdir()
            fixture = Path(__file__).parent / "fixtures/task298-previous-backup.service"
            destination.write_bytes(fixture.read_bytes())
            old_release = root / "previous"
            old_release_unit = old_release / "deploy/systemd/wow-forever-armory-backup.service"
            old_release_unit.parent.mkdir(parents=True)
            old_release_unit.write_bytes(fixture.read_bytes())
            candidate = Path(__file__).parents[1] / "deploy/systemd/wow-forever-armory-backup.service"
            real_lstat = Path.lstat
            root_owned = SimpleNamespace(
                st_mode=stat.S_IFREG | 0o644, st_uid=0, st_gid=0, st_nlink=1
            )

            def lstat(path):
                return root_owned if path == destination else real_lstat(path)

            def atomic_copy(source, target, mode):
                target.write_bytes(Path(source).read_bytes())
                target.chmod(mode)

            with (
                patch.object(Path, "lstat", lstat),
                patch.object(production_install, "_copy_unit", side_effect=atomic_copy),
                patch.object(production_install, "_run") as run,
            ):
                production_install._upgrade_backup_unit(candidate, destination, old_release)

            self.assertEqual(destination.read_bytes(), candidate.read_bytes())
            self.assertEqual(
                [call.args[0][:2] for call in run.call_args_list],
                [["systemd-analyze", "verify"], ["systemctl", "daemon-reload"]],
            )

    def test_backup_unit_transition_rejects_modified_previous_unit_without_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "installed.service"
            fixture = Path(__file__).parent / "fixtures/task298-previous-backup.service"
            destination.write_bytes(fixture.read_bytes() + b"# modified\n")
            old_release = root / "previous"
            old_release_unit = old_release / "deploy/systemd/wow-forever-armory-backup.service"
            old_release_unit.parent.mkdir(parents=True)
            old_release_unit.write_bytes(fixture.read_bytes())
            candidate = Path(__file__).parents[1] / "deploy/systemd/wow-forever-armory-backup.service"
            real_lstat = Path.lstat
            root_owned = SimpleNamespace(
                st_mode=stat.S_IFREG | 0o644, st_uid=0, st_gid=0, st_nlink=1
            )
            with (
                patch.object(Path, "lstat", lambda path: root_owned if path == destination else real_lstat(path)),
                patch.object(production_install, "_copy_unit") as copy_unit,
            ):
                with self.assertRaisesRegex(InstallError, "pinned previous version"):
                    production_install._upgrade_backup_unit(candidate, destination, old_release)
            copy_unit.assert_not_called()
            self.assertEqual(destination.read_bytes(), fixture.read_bytes() + b"# modified\n")

    def test_backup_unit_transition_is_idempotent_when_candidate_is_preinstalled(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "installed.service"
            candidate = Path(__file__).parents[1] / (
                "deploy/systemd/wow-forever-armory-backup.service"
            )
            destination.write_bytes(candidate.read_bytes())
            old_release = root / "previous"
            old_release_unit = (
                old_release / "deploy/systemd/wow-forever-armory-backup.service"
            )
            old_release_unit.parent.mkdir(parents=True)
            old_release_unit.write_bytes(candidate.read_bytes())
            real_lstat = Path.lstat
            root_owned = SimpleNamespace(
                st_mode=stat.S_IFREG | 0o644, st_uid=0, st_gid=0, st_nlink=1
            )
            with (
                patch.object(
                    Path,
                    "lstat",
                    lambda path: root_owned if path == destination else real_lstat(path),
                ),
                patch.object(production_install, "_copy_unit") as copy_unit,
                patch.object(production_install, "_run") as run,
            ):
                production_install._upgrade_backup_unit(
                    candidate, destination, old_release
                )
            copy_unit.assert_not_called()
            run.assert_not_called()
            self.assertEqual(destination.read_bytes(), candidate.read_bytes())

    def test_backup_unit_reload_failure_restores_exact_previous_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "installed.service"
            fixture = Path(__file__).parent / "fixtures/task298-previous-backup.service"
            destination.write_bytes(fixture.read_bytes())
            old_release = root / "previous"
            old_release_unit = old_release / "deploy/systemd/wow-forever-armory-backup.service"
            old_release_unit.parent.mkdir(parents=True)
            old_release_unit.write_bytes(fixture.read_bytes())
            candidate = Path(__file__).parents[1] / "deploy/systemd/wow-forever-armory-backup.service"
            real_lstat = Path.lstat
            root_owned = SimpleNamespace(
                st_mode=stat.S_IFREG | 0o644, st_uid=0, st_gid=0, st_nlink=1
            )

            def atomic_copy(source, target, mode):
                target.write_bytes(Path(source).read_bytes())
                target.chmod(mode)

            with (
                patch.object(Path, "lstat", lambda path: root_owned if path == destination else real_lstat(path)),
                patch.object(production_install, "_copy_unit", side_effect=atomic_copy),
                patch.object(production_install, "_run", side_effect=[None, InstallError("reload failed"), None]) as run,
            ):
                with self.assertRaisesRegex(InstallError, "was restored"):
                    production_install._upgrade_backup_unit(candidate, destination, old_release)
            self.assertEqual(destination.read_bytes(), fixture.read_bytes())
            self.assertEqual(run.call_args_list[-1].args[0], ["systemctl", "daemon-reload"])

    def test_upgrade_path_does_not_invoke_database_migration(self) -> None:
        import inspect

        from deploy.production_install import install

        source = inspect.getsource(install)
        self.assertNotIn("production_migration", source)
        self.assertIn("if not upgrade_existing", source)

    def test_upgrade_uses_snapshot_and_cannot_invoke_installed_db_preflight(
        self,
    ) -> None:
        import ast
        import inspect

        from deploy.production_install import install

        tree = ast.parse(inspect.getsource(install))
        run_calls = [
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_run"
        ]
        self.assertIn("_verify_upgrade_database(", inspect.getsource(install))
        source_calls = {
            name: min(
                node.lineno
                for node in ast.walk(ast.parse(inspect.getsource(install)))
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == name
            )
            for name in (
                "_verify_upgrade_database",
                "_install_release",
                "_install_current_link",
            )
        }
        self.assertLess(
            source_calls["_verify_upgrade_database"],
            source_calls["_install_release"],
        )
        self.assertLess(
            source_calls["_verify_upgrade_database"],
            source_calls["_install_current_link"],
        )
        self.assertFalse(
            any("armory-db-preflight" in call for call in run_calls), run_calls
        )

    def test_upgrade_snapshot_failure_fails_closed(self) -> None:
        with (
            patch("deploy.production_install._assert_no_armory_process") as process,
            patch(
                "deploy.production_install.verify_database_snapshot",
                side_effect=OSError("synthetic snapshot failure"),
            ) as verify,
        ):
            with self.assertRaisesRegex(InstallError, "snapshot verification failed"):
                _verify_upgrade_database(
                    Path("/temporary/database.sqlite3"), uid=10, gid=10
                )
        process.assert_called_once_with()
        verify.assert_called_once_with(
            Path("/temporary/database.sqlite3"), expected_owner=10, expected_group=10
        )

    def test_complete_upgrade_flow_accepts_empty_initialized_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = Path(__file__).parents[1]
            base = root / "opt/wow-forever-armory-bot"
            etc = root / "etc/wow-forever-armory-bot"
            state = root / "var/lib/wow-forever-armory-bot"
            systemd = root / "etc/systemd/system"
            bin_dir = base / "bin"
            releases = base / "releases"
            etc.mkdir(parents=True)
            state.mkdir(parents=True)
            systemd.mkdir(parents=True)
            bin_dir.mkdir(parents=True)
            releases.mkdir()

            old_release = releases / ("a" * 40)
            old_release.mkdir()
            (old_release / ".installation-complete").write_text("a" * 40)
            previous_unit = old_release / "deploy/systemd/wow-forever-armory-backup.service"
            previous_unit.parent.mkdir(parents=True)
            previous_unit.write_bytes(
                (Path(__file__).parent / "fixtures/task298-previous-backup.service").read_bytes()
            )
            (base / "current").symlink_to(old_release)

            for unit in production_install.UNIT_FILES:
                target = systemd / unit
                if unit == "wow-forever-armory-backup.service":
                    target.write_bytes(previous_unit.read_bytes())
                else:
                    target.write_bytes((repo / "deploy/systemd" / unit).read_bytes())

            config = etc / "config"
            credential = etc / "discord-token"
            config.write_text("ARMORY_LOG_LEVEL=INFO\n", encoding="utf-8")
            credential.write_text("synthetic-test-credential\n", encoding="utf-8")

            launcher_source = repo / "deploy/systemd/armory-launch"
            preflight_source = repo / "deploy/systemd/armory-db-preflight.py"
            installed_launcher = bin_dir / "armory-launch"
            installed_launcher.write_bytes(launcher_source.read_bytes())
            installed_preflight = bin_dir / "armory-db-preflight"
            installed_preflight.write_bytes(b"previous preflight")
            installed_preflight.chmod(0o755)

            database = state / "guild-armory.sqlite3"
            _create_preflight_database(database, guild_count=0)
            service_account = SimpleNamespace(
                pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir="/nonexistent"
            )
            service_group = SimpleNamespace(gr_gid=os.getgid())
            release_sha = "b" * 40

            with ExitStack() as stack:
                for target, attribute, value in (
                    (production_install, "BASE", base),
                    (production_install, "ETC", etc),
                    (production_install, "STATE", state),
                    (production_install, "SYSTEMD_DIRECTORY", systemd),
                ):
                    stack.enter_context(patch.object(target, attribute, value))
                for target, attribute, kwargs in (
                    (production_install.os, "geteuid", {"return_value": 0}),
                    (production_install.os, "chown", {}),
                    (production_install, "_check_repository", {}),
                    (production_install, "validate_inputs", {}),
                    (production_install, "_require_supported_runtime", {}),
                    (production_install, "_assert_units_stopped_and_disabled", {}),
                    (production_install.pwd, "getpwnam", {"return_value": service_account}),
                    (production_install.grp, "getgrnam", {"return_value": service_group}),
                    (production_install, "_configured_logo_path", {"return_value": None}),
                    (production_install, "_read_regular_file", {"return_value": b"old"}),
                    (production_install, "_mkdir_owned", {}),
                    (production_install, "_assert_no_armory_process", {}),
                    (production_install, "_install_release", {}),
                ):
                    stack.enter_context(patch.object(target, attribute, **kwargs))
                copy_unit = stack.enter_context(
                    patch.object(
                        production_install,
                        "_copy_unit",
                        wraps=production_install._copy_unit,
                    )
                )
                switch_link = stack.enter_context(
                    patch.object(production_install, "_install_current_link")
                )
                run = stack.enter_context(patch.object(production_install, "_run"))
                verify_unit_metadata = stack.enter_context(
                    patch.object(production_install, "_verify_previous_backup_unit")
                )
                verify_database = stack.enter_context(
                    patch.object(
                        production_install,
                        "_verify_upgrade_database",
                        wraps=production_install._verify_upgrade_database,
                    )
                )
                stack.enter_context(
                    patch.object(
                        production_install,
                        "verify_database_snapshot",
                        return_value=(0, 0, 0),
                    )
                )
                production_install.install(
                    repo,
                    config,
                    credential,
                    release_sha,
                    upgrade_existing=True,
                )

            verify_database.assert_called_once_with(
                state / "guild-armory.sqlite3", uid=os.getuid(), gid=os.getgid()
            )
            copy_unit.assert_any_call(preflight_source, installed_preflight, 0o755)
            copy_unit.assert_any_call(
                repo / "deploy/recovery_audit.py", bin_dir / "recovery_audit.py", 0o644
            )
            self.assertEqual(copy_unit.call_count, 3)
            self.assertEqual(
                (systemd / "wow-forever-armory-backup.service").read_bytes(),
                (repo / "deploy/systemd/wow-forever-armory-backup.service").read_bytes(),
            )
            verify_unit_metadata.assert_any_call(
                systemd / "wow-forever-armory-backup.service", previous_unit
            )
            self.assertEqual(verify_unit_metadata.call_count, 2)
            self.assertEqual(
                [call.args[0] for call in run.call_args_list],
                [
                    ["systemd-analyze", "verify", str(repo / "deploy/systemd/wow-forever-armory-backup.service")],
                    ["systemctl", "daemon-reload"],
                ],
            )
            switch_link.assert_called_once()

    def test_upgrade_process_guard_detects_bot_and_fails_closed_on_proc_error(
        self,
    ) -> None:
        from deploy.production_install import _assert_no_armory_process

        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            bot = proc / "456"
            bot.mkdir()
            (bot / "cmdline").write_bytes(
                b"python\0-m\0wow_forever_armory.discord_bot\0"
            )
            (bot / "environ").write_bytes(b"DISCORD_BOT_TOKEN=synthetic")
            with self.assertRaisesRegex(InstallError, "still running"):
                _assert_no_armory_process(proc)

        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            process = proc / "123"
            process.mkdir()
            with patch.object(Path, "read_bytes", side_effect=PermissionError):
                with self.assertRaisesRegex(InstallError, "verify Armory process"):
                    _assert_no_armory_process(proc)

    def test_upgrade_atomically_switches_managed_release_and_keeps_old_release(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            releases = root / "releases"
            old_sha = "a" * 40
            new_sha = "b" * 40
            old_release = releases / old_sha
            new_release = releases / new_sha
            old_release.mkdir(parents=True)
            new_release.mkdir()
            (old_release / ".installation-complete").write_text(old_sha)
            (new_release / ".installation-complete").write_text(new_sha)
            (root / "current").symlink_to(old_release)
            with (
                patch("deploy.production_install.BASE", root),
                patch("deploy.production_install._run") as run,
            ):
                _install_current_link(
                    new_release, upgrade_existing=True, repo=Path(temporary)
                )
            self.assertEqual((root / "current").resolve(), new_release)
            self.assertEqual(
                os.readlink(root / "current"), f"releases/{new_release.name}"
            )
            self.assertTrue(old_release.is_dir())
            run.assert_called_once()

    def test_upgrade_refuses_unapproved_release_switch_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            releases = root / "releases"
            old_release = releases / ("a" * 40)
            new_release = releases / ("b" * 40)
            old_release.mkdir(parents=True)
            new_release.mkdir()
            (old_release / ".installation-complete").write_text("a" * 40)
            (root / "current").symlink_to(old_release)
            with patch("deploy.production_install.BASE", root):
                with self.assertRaisesRegex(InstallError, "refusing to replace"):
                    _install_current_link(new_release)
            self.assertEqual((root / "current").resolve(), old_release)

    def test_upgrade_rejects_noncanonical_current_symlink_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            releases = root / "releases"
            old_sha = "a" * 40
            new_sha = "b" * 40
            old_release = releases / old_sha
            new_release = releases / new_sha
            old_release.mkdir(parents=True)
            new_release.mkdir()
            (old_release / ".installation-complete").write_text(old_sha)
            current = root / "current"
            current.symlink_to(f"releases//{old_sha}")
            with patch("deploy.production_install.BASE", root):
                with self.assertRaisesRegex(InstallError, "non-canonical"):
                    _install_current_link(
                        new_release, upgrade_existing=True, repo=Path(temporary)
                    )
            self.assertEqual(os.readlink(current), f"releases//{old_sha}")

    def test_installer_requires_exact_clean_release_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            subprocess.run(
                [
                    "git",
                    "init",
                    "--quiet",
                    "--initial-branch=release/v2.0.0",
                    str(repo),
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.name", "Fixture"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "config",
                    "user.email",
                    "fixture@example.invalid",
                ],
                check=True,
            )
            (repo / "fixture.txt").write_text("fixture", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "fixture.txt"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "--quiet", "-m", "fixture"],
                check=True,
            )
            head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            with patch("deploy.production_install.BASE_CHECKPOINT_SHA", head):
                _check_repository(repo, head)
                with self.assertRaisesRegex(InstallError, "does not match"):
                    _check_repository(repo, "0" * 40)
                with self.assertRaisesRegex(InstallError, "full lowercase"):
                    _check_repository(repo, "HEAD")
                subprocess.run(
                    ["git", "-C", str(repo), "branch", "--move", "wrong-branch"],
                    check=True,
                )
                with self.assertRaisesRegex(InstallError, "approved release branch"):
                    _check_repository(repo, head)

    def test_production_config_does_not_pin_a_guild(self) -> None:
        example = (
            Path(__file__).parents[1] / "deploy/systemd/armory-config.example"
        ).read_text(encoding="utf-8")
        self.assertNotIn("DISCORD_TEST_GUILD_ID=", example)
        self.assertFalse(hasattr(preflight, "guild_configuration_is_valid"))

    def test_optional_logo_path_is_allowlisted_for_external_provisioning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config"
            config.write_text(
                "ARMORY_LOGO_PATH=/etc/wow-forever-armory-bot/wow_forever_logo.png\n",
                encoding="utf-8",
            )
            config.chmod(0o600)
            validate_config(config, expected_owner=os.getuid())
            config.write_text("ARMORY_LOGO_PATH=/tmp/unreviewed.png\n")
            config.chmod(0o600)
            with self.assertRaisesRegex(InstallError, "unsupported local logo"):
                validate_config(config, expected_owner=os.getuid())

    def test_logo_asset_requires_root_service_group_and_exact_private_mode(
        self,
    ) -> None:
        asset = Mock()
        asset.is_symlink.return_value = False
        asset.is_file.return_value = True
        asset.stat.return_value = SimpleNamespace(
            st_uid=0, st_gid=321, st_mode=stat.S_IFREG | 0o640
        )
        _validate_logo_asset(asset, service_uid=654, service_gid=321)
        asset.stat.return_value = SimpleNamespace(
            st_uid=0, st_gid=0, st_mode=stat.S_IFREG | 0o644
        )
        with self.assertRaisesRegex(InstallError, "ownership or permissions"):
            _validate_logo_asset(asset, service_uid=654, service_gid=321)

    def test_incomplete_release_is_preserved_for_operator_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            release = root / "release"
            release.mkdir()
            sentinel = release / "operator-review.txt"
            sentinel.write_text("preserve this file", encoding="utf-8")
            with self.assertRaisesRegex(
                InstallError, "preserve it for operator review"
            ):
                _install_release(repo, release, os.getuid(), os.getgid(), "a" * 40)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve this file")

    def _make_completed_release(self, root: Path, release_sha: str) -> Path:
        release = root / "releases" / release_sha
        interpreter = release / ".venv/bin/python"
        interpreter.parent.mkdir(parents=True, mode=0o750)
        interpreter.write_bytes(b"python fixture")
        interpreter.chmod(0o750)
        payload = release / "src/package.py"
        payload.parent.mkdir(parents=True, mode=0o750)
        payload.write_bytes(b"preserved release data")
        payload.chmod(0o640)
        marker = release / ".installation-complete"
        marker.write_text(release_sha, encoding="ascii")
        marker.chmod(0o440)
        for directory in (
            release,
            release / ".venv",
            interpreter.parent,
            payload.parent,
        ):
            directory.chmod(0o750)
        return release

    def _root_owned_release_stat(self, service_gid: int, overrides=None):
        original_stat = Path.stat
        overrides = overrides or {}

        def root_owned_stat(path, *, follow_symlinks=True):
            result = original_stat(path, follow_symlinks=follow_symlinks)
            uid, gid, mode = overrides.get(path, (0, service_gid, result.st_mode))
            return SimpleNamespace(st_uid=uid, st_gid=gid, st_mode=mode)

        return root_owned_stat

    def test_completed_release_is_reused_without_rebuilding_or_changing_data(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release_sha = "a" * 40
            release = self._make_completed_release(root, release_sha)
            before = {
                path.relative_to(release): path.read_bytes()
                for path in release.rglob("*")
                if path.is_file()
            }
            service_uid = os.getuid() + 1000
            service_gid = os.getgid() + 1000
            with (
                patch.object(production_install, "_require_supported_runtime"),
                patch.object(
                    Path,
                    "stat",
                    self._root_owned_release_stat(service_gid),
                ),
                patch.object(production_install.subprocess, "run") as run,
            ):
                production_install._install_release_impl(
                    root, release, service_uid, service_gid, release_sha
                )
            after = {
                path.relative_to(release): path.read_bytes()
                for path in release.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)
            run.assert_not_called()

    def test_completed_release_with_invalid_marker_is_preserved_and_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release_sha = "b" * 40
            release = self._make_completed_release(root, release_sha)
            marker = release / ".installation-complete"
            marker.chmod(0o600)
            marker.write_text("c" * 40, encoding="ascii")
            marker.chmod(0o440)
            payload = release / "src/package.py"
            payload_before = payload.read_bytes()
            service_gid = os.getgid() + 1000
            with (
                patch.object(production_install, "_require_supported_runtime"),
                patch.object(
                    Path,
                    "stat",
                    self._root_owned_release_stat(service_gid),
                ),
            ):
                with self.assertRaisesRegex(InstallError, "mismatched marker"):
                    production_install._install_release_impl(
                        root, release, os.getuid() + 1000, service_gid, release_sha
                    )
            self.assertEqual(payload.read_bytes(), payload_before)
            self.assertEqual(marker.read_text(encoding="ascii"), "c" * 40)

    def test_completed_release_reuse_enforces_owner_read_and_execute_permissions(
        self,
    ) -> None:
        service_uid = os.getuid() + 1000
        service_gid = os.getgid() + 1000
        cases = ("ownership", "readability", "execution")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                release_sha = "d" * 40
                release = self._make_completed_release(root, release_sha)
                payload = release / "src/package.py"
                interpreter = release / ".venv/bin/python"
                overrides = {}
                if case == "ownership":
                    overrides[payload] = (
                        service_uid + 1,
                        service_gid,
                        payload.stat().st_mode,
                    )
                    message = "release file ownership or mode is unsafe"
                elif case == "readability":
                    overrides[payload] = (
                        0,
                        service_gid,
                        stat.S_IFREG | 0o600,
                    )
                    message = "service account cannot read release file"
                else:
                    overrides[interpreter] = (
                        0,
                        service_gid,
                        stat.S_IFREG | 0o640,
                    )
                    message = "cannot read and execute release Python"
                with (
                    patch.object(production_install, "_require_supported_runtime"),
                    patch.object(
                        Path,
                        "stat",
                        self._root_owned_release_stat(service_gid, overrides),
                    ),
                ):
                    with self.assertRaisesRegex(InstallError, message):
                        production_install._install_release_impl(
                            root, release, service_uid, service_gid, release_sha
                        )
                self.assertEqual(payload.read_bytes(), b"preserved release data")

    def test_dependency_locks_pin_every_dependency_and_hash(self) -> None:
        root = Path(__file__).parents[1] / "deploy"
        for name in ("requirements-build.lock", "requirements-runtime.lock"):
            entries = [
                line.strip()
                for line in (root / name).read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            self.assertTrue(entries)
            logical = " ".join(entries).replace("\\", "").split()
            self.assertEqual(len(logical) % 2, 0)
            for index in range(0, len(logical), 2):
                self.assertRegex(logical[index], r"^[A-Za-z0-9_.-]+==[^=]+$")
                self.assertRegex(logical[index + 1], r"^--hash=sha256:[0-9a-f]{64}$")

    def test_release_python_guard_is_explicitly_limited_to_locked_target(self) -> None:
        source = (Path(__file__).parents[1] / "deploy/production_install.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('version != "3.14:x86_64:linux"', source)
        project = (Path(__file__).parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn('requires-python = ">=3.14,<3.15"', project)
        with patch(
            "deploy.production_install.subprocess.run",
            return_value=SimpleNamespace(stdout="3.13:x86_64:linux"),
        ):
            with self.assertRaisesRegex(InstallError, "supports CPython 3.14"):
                _require_supported_runtime(Path("python"))

    def test_config_guild_must_match_independently_supplied_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config"
            credential = root / "token"
            config.write_text(
                "ARMORY_LOG_LEVEL=INFO\n",
                encoding="utf-8",
            )
            config.chmod(0o600)
            credential.write_text("synthetic-fixture\n", encoding="utf-8")
            credential.chmod(0o600)

            validate_inputs(config, credential, expected_owner=os.getuid())

    def test_insecure_or_empty_credential_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config"
            credential = root / "token"
            config.write_text("ARMORY_LOG_LEVEL=INFO\n")
            config.chmod(0o600)
            credential.write_text("synthetic-fixture\n")
            credential.chmod(0o644)
            with self.assertRaisesRegex(InstallError, "permissions are unsafe"):
                validate_inputs(config, credential, expected_owner=os.getuid())
            credential.write_bytes(b"")
            credential.chmod(0o600)
            with self.assertRaisesRegex(InstallError, "token line"):
                validate_inputs(config, credential, expected_owner=os.getuid())

    def test_config_rejects_duplicate_guild_and_token_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config"
            credential = Path(temporary) / "token"
            credential.write_text("synthetic-fixture\n", encoding="ascii")
            credential.chmod(0o600)
            for contents in (
                "ARMORY_LOG_LEVEL=INFO\nARMORY_LOG_LEVEL=ERROR\n",
                "DISCORD_TEST_GUILD_ID=123456789012345678\n",
            ):
                config.write_text(contents, encoding="utf-8")
                config.chmod(0o600)
                with self.assertRaises(InstallError):
                    validate_inputs(config, credential, expected_owner=os.getuid())

    def test_preflight_rejects_missing_and_empty_database_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing.sqlite3"
            self.assertFalse(preflight.database_metadata_is_safe(missing))

            empty = root / "empty.sqlite3"
            empty.touch()
            empty.chmod(0o600)
            self.assertFalse(preflight.database_metadata_is_safe(empty))

    def test_preflight_accepts_valid_zero_and_one_guild_databases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for guild_count in (0, 1):
                with self.subTest(guild_count=guild_count):
                    database = root / f"guilds-{guild_count}.sqlite3"
                    _create_preflight_database(database, guild_count=guild_count)
                    database.chmod(0o600)
                    self.assertTrue(preflight.database_metadata_is_safe(database))

    def test_preflight_does_not_open_or_interpret_database_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corrupt = root / "corrupt.sqlite3"
            corrupt.write_bytes(b"not a SQLite database")
            corrupt.chmod(0o600)
            self.assertTrue(preflight.database_metadata_is_safe(corrupt))

    def test_preflight_rejects_wrong_owner_and_unsafe_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "private.sqlite3"
            _create_preflight_database(database)
            database.chmod(0o600)
            self.assertFalse(
                preflight.database_metadata_is_safe(
                    database, expected_owner=os.getuid() + 1
                )
            )
            database.chmod(0o640)
            self.assertFalse(preflight.database_metadata_is_safe(database))

    def test_preflight_restore_marker_blocks_initialized_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "initialized.sqlite3"
            _create_preflight_database(database)
            Path(f"{database}.restore-in-progress").write_text("review")
            database.chmod(0o600)
            self.assertFalse(preflight.database_metadata_is_safe(database))

    def test_installed_preflight_copy_keeps_metadata_only_database_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = Path(__file__).parents[1] / "deploy/systemd/armory-db-preflight.py"
            destination = root / "bin/armory-db-preflight"
            destination.parent.mkdir()
            temporary_copy = destination.with_name(".armory-db-preflight.installing")
            with (
                patch("deploy.production_install.os.chown"),
                patch("deploy.production_install.os.chmod") as chmod,
            ):
                _copy_unit(source, destination, 0o755)
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertFalse(temporary_copy.exists())
            chmod.assert_called_once_with(temporary_copy, 0o755)

            loader = SourceFileLoader("installed_armory_db_preflight", str(destination))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            self.assertIsNotNone(spec)
            installed = importlib.util.module_from_spec(spec)
            loader.exec_module(installed)
            database = root / "empty-guilds.sqlite3"
            database.write_bytes(b"metadata check never opens SQLite")
            database.chmod(0o600)
            self.assertTrue(installed.database_metadata_is_safe(database))

    def test_startup_preflight_is_guild_agnostic(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "ARMORY_DATABASE_PATH": "/temporary/verified.sqlite3",
                    "DISCORD_TEST_GUILD_ID": "123456789012345678",
                },
            ),
            patch.object(preflight, "deployment_recovery_is_clear", return_value=True),
            patch.object(preflight, "armory_process_present", return_value=False),
            patch.object(preflight, "database_metadata_is_safe", return_value=True),
            patch.object(
                preflight.pwd,
                "getpwnam",
                return_value=SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()),
            ),
        ):
            self.assertEqual(preflight.main([]), 0)

    def test_startup_preflight_requires_database_path(self) -> None:
        with (
            patch.dict(os.environ, {"ARMORY_DATABASE_PATH": ""}),
            patch.object(preflight, "deployment_recovery_is_clear", return_value=True),
            patch.object(preflight, "armory_process_present", return_value=False),
            patch.object(
                preflight.pwd,
                "getpwnam",
                return_value=SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()),
            ),
        ):
            self.assertEqual(preflight.main([]), 78)

    def test_preflight_finds_existing_armory_module_without_reading_env(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            process = proc / "101"
            process.mkdir()
            (process / "cmdline").write_bytes(
                b"python\0-m\0wow_forever_armory.discord_bot\0"
            )
            (process / "environ").write_bytes(
                b"DISCORD_BOT_TOKEN=synthetic-never-print\0"
            )
            self.assertTrue(preflight.armory_process_present(proc))

    def test_existing_armory_process_still_blocks_preflight_database_check(self):
        with (
            patch.dict(os.environ, {"ARMORY_DATABASE_PATH": "/unused/database"}),
            patch.object(preflight, "deployment_recovery_is_clear", return_value=True),
            patch.object(preflight, "armory_process_present", return_value=True),
            patch.object(
                preflight, "database_metadata_is_safe"
            ) as database_metadata_is_safe,
        ):
            self.assertEqual(preflight.main([]), 78)
            database_metadata_is_safe.assert_not_called()

    def test_bot_unit_uses_root_preflight_and_nonstarting_install_files(self) -> None:
        root = Path(__file__).parents[1]
        unit = (root / "deploy/systemd/wow-forever-armory-bot.service").read_text(
            encoding="utf-8"
        )
        installer = (root / "deploy/production_install.py").read_text(encoding="utf-8")
        self.assertIn(
            "ExecStartPre=+"
            "/opt/wow-forever-armory-bot/bin/armory-db-preflight --unit bot",
            unit,
        )
        self.assertIn("LoadCredential=discord-token:", unit)
        self.assertNotIn('"enable"', installer)
        self.assertNotIn('"start"', installer)
        self.assertIn('deploy / "systemd/armory-db-preflight.py"', installer)
        self.assertIn('BASE / "bin/armory-db-preflight"', installer)
        self.assertIn(
            "_copy_unit(preflight_source, preflight_destination, 0o755)", installer
        )
        self.assertTrue(
            (root / "deploy/systemd/armory-db-preflight.py").stat().st_mode & 0o111
        )
        self.assertIn('deploy / "systemd/armory-db-preflight.py"', installer)
        self.assertIn('BASE / "bin/armory-db-preflight"', installer)


if __name__ == "__main__":
    unittest.main()
