#!/usr/bin/python3
"""Read-only, secret-free evidence collection for TASK-301 on nololinux-server."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import types
from pathlib import Path

BASE = Path("/opt/wow-forever-armory-bot")
AUDIT = Path("/var/lib/armory-deployment-audit")
DB = Path("/var/lib/wow-forever-armory-bot/guild-armory.sqlite3")
BACKUPS = Path("/var/lib/wow-forever-armory-bot/backups")
SNAPSHOT_SHA256 = (
    "564fb40f430c9f96434ab03a3e008a631a7cb7e556f1f63b16f4e9357c2111c8"
)
UNITS = (
    "wow-forever-armory-bot.service",
    "wow-forever-armory-backup.timer",
    "wow-forever-armory-backup.service",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOATIME", 0)
    fd = os.open(path, flags)
    try:
        while block := os.read(fd, 1024 * 1024):
            digest.update(block)
    finally:
        os.close(fd)
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOATIME", 0)
    fd = os.open(path, flags)
    try:
        chunks = []
        while block := os.read(fd, 65536):
            chunks.append(block)
        value = json.loads(b"".join(chunks))
    finally:
        os.close(fd)
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value


def unit_state(unit: str) -> dict[str, str]:
    result = {}
    for prop in ("ActiveState", "UnitFileState"):
        proc = subprocess.run(
            ["/usr/bin/systemctl", "show", unit, f"--property={prop}", "--value"],
            check=False,
            capture_output=True,
            text=True,
        )
        result[prop] = proc.stdout.strip() if proc.returncode == 0 else "unavailable"
    return result


def db_report(path: Path, expected_sha256: str | None = None) -> dict:
    if not path.is_file():
        return {"exists": False}
    info = path.stat(follow_symlinks=False)
    module_path = Path(__file__).with_name("production_db_snapshot.py")
    source_fd = os.open(
        module_path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOATIME", 0),
    )
    try:
        source_info = os.fstat(source_fd)
        source_raw = b""
        while block := os.read(source_fd, 65536):
            source_raw += block
        if (
            hashlib.sha256(source_raw).hexdigest() != SNAPSHOT_SHA256
            or os.fstat(source_fd).st_ino != source_info.st_ino
            or os.fstat(source_fd).st_size != source_info.st_size
        ):
            raise RuntimeError("snapshot verifier hash mismatch")
    finally:
        os.close(source_fd)
    module = types.ModuleType("armory_db_snapshot")
    exec(compile(source_raw, str(module_path), "exec", dont_inherit=True), module.__dict__)
    counts = module.verify_database_snapshot(
        path,
        expected_owner=info.st_uid,
        expected_group=info.st_gid,
        expected_sha256=expected_sha256,
    )
    return {
        "exists": True,
        "sha256": sha256(path),
        "integrity": "ok",
        "required_table_counts_guilds_members_characters": list(counts),
        "owner_uid": info.st_uid,
        "group_gid": info.st_gid,
    }


def main() -> int:
    report: dict = {"host": subprocess.run(
        ["/usr/bin/hostname"], capture_output=True, text=True, check=False
    ).stdout.strip()}
    journal_path = AUDIT / "deployment-state.json"
    try:
        journal = read_json(journal_path)
        report["journal"] = {
            key: journal.get(key)
            for key in (
                "phase",
                "interrupted_phase",
                "approved_release_sha",
                "previous_release_sha",
                "database_backup",
                "database_backup_sha256",
                "database_signature",
                "shared_preflight_backup",
                "shared_preflight_sha256",
                "fail_closed_verified",
                "fail_closed_error",
            )
        }
        report["journal"]["sha256"] = sha256(journal_path)
        backup_value = journal.get("database_backup")
        expected_backup_hash = journal.get("database_backup_sha256")
        if isinstance(backup_value, str):
            backup = Path(backup_value)
            try:
                actual_backup_hash = sha256(backup)
                report["registered_backup"] = {
                    "exists": True,
                    "path": str(backup),
                    "sha256": actual_backup_hash,
                    "matches_journal": actual_backup_hash == expected_backup_hash,
                    "database": db_report(backup, expected_backup_hash),
                }
            except OSError:
                report["registered_backup"] = {"exists": False}
    except Exception as exc:  # report type only; never include raw contents
        report["journal_error"] = type(exc).__name__

    try:
        current = BASE / "current"
        report["release"] = {
            "current_target": os.readlink(current),
            "installation_marker": (current / ".installation-complete").read_text(
                encoding="ascii"
            ).strip(),
        }
    except Exception as exc:
        report["release_error"] = type(exc).__name__
    report["production_database"] = db_report(DB)
    try:
        preflights = []
        for path in sorted(AUDIT.glob("armory-db-preflight.*.py")):
            if path.is_file() and not path.is_symlink():
                preflights.append({"name": path.name, "sha256": sha256(path)})
        report["preserved_preflights"] = preflights
        backup_files = []
        for path in sorted(BACKUPS.glob("guild-armory-*.sqlite3")):
            if path.is_file() and not path.is_symlink():
                backup_files.append({"name": path.name, "sha256": sha256(path)})
        report["backup_inventory"] = backup_files
    except Exception as exc:
        report["inventory_error"] = type(exc).__name__
    report["systemd"] = {unit: unit_state(unit) for unit in UNITS}
    processes = subprocess.run(
        ["/usr/bin/pgrep", "-af", "wow_forever_armory"],
        capture_output=True,
        text=True,
        check=False,
    )
    report["bot_process_match_count"] = sum(
        1 for line in processes.stdout.splitlines() if "pgrep" not in line
    )
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
