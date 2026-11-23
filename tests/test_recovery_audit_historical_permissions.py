from __future__ import annotations

import hashlib
import json
import os
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
