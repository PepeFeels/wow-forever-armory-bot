#!/usr/bin/env python3
"""Read-only checks for a proposed WoW Forever Armory production deployment.

Run with the system Python 3.14 using ``-I -B``. This tool reports findings
only: it never repairs state, changes files, operates services, or writes to a
SQLite database. Database checks use SQLite's immutable read-only mode; if
sidecar files are present, the result is UNKNOWN because they cannot safely be
included without creating SQLite shared-memory files.
"""

from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import platform
import pwd
import re
import sqlite3
import stat
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BRANCH = "release/v2.0.0"
FAILED_V1_RELEASE = "a1a6a2ad95302f7e3c4900f18e91691f43631e18"
APPROVED_PREFLIGHT_SHA256 = (
    "c1c4f067fa2b465726d374ecb06acfae82a1ec234dc0f38c4734a6c422089803"
)
BASE = Path("/opt/wow-forever-armory-bot")
REPO = BASE / "deployment-checkout"
STATE = Path("/var/lib/wow-forever-armory-bot")
AUDIT = Path("/var/lib/armory-deployment-audit")
ETC = Path("/etc/wow-forever-armory-bot")
PIN = ETC / "deployment-pin.json"
SERVICE = "wow-forever-armory-bot.service"
BACKUP_TIMER = "wow-forever-armory-backup.timer"
BACKUP_SERVICE = "wow-forever-armory-backup.service"
UNITS = (SERVICE, BACKUP_TIMER, BACKUP_SERVICE)
REQUIRED_TABLES = {"guilds", "members", "characters", "flows"}
CHILD_ENV = {
    "HOME": "/nonexistent",
    "LANG": "C.UTF-8",
    "LC_ALL": "C",
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "PYTHONDONTWRITEBYTECODE": "1",
}


@dataclass(frozen=True)
class Result:
    status: str
    detail: str


class Preflight:
    """Collect fixed, non-sensitive diagnostics from production paths."""

    def __init__(self, candidate: Path) -> None:
        self.candidate = candidate
        self.checks: dict[str, Result] = {}
        self.pin: dict[str, str] | None = None
        self.service_uid: int | None = None
        self.service_gid: int | None = None
        self.unit_states: dict[str, tuple[str, str]] = {}
        self.unit_states_checked = False

    def add(self, name: str, status: str, detail: str) -> None:
        self.checks[name] = Result(status, detail)

    @staticmethod
    def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=CHILD_ENV,
            timeout=15,
            check=False,
        )

    @classmethod
    def _git(cls, path: Path, *args: str) -> subprocess.CompletedProcess[str]:
        # Root may inspect the developer-owned candidate only by explicitly
        # trusting this exact path for read-only Git queries. Global config is
        # disabled above and Git's optional index writes are disabled as well.
        return cls._run(
            [
                "/usr/bin/git",
                "-c",
                f"safe.directory={path}",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                str(path),
                *args,
            ]
        )

    @staticmethod
    def _trusted_checkout_tree(path: Path) -> bool:
        try:
            marker = path / ".git"
            marker_info = marker.lstat()
            if not stat.S_ISDIR(marker_info.st_mode):
                return False
            for directory, names, files in os.walk(path, followlinks=False):
                for item in [
                    Path(directory),
                    *(Path(directory) / name for name in names + files),
                ]:
                    info = item.lstat()
                    if (
                        stat.S_ISLNK(info.st_mode)
                        or info.st_uid != 0
                        or info.st_mode & 0o222
                        or not (
                            stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
                        )
                    ):
                        return False
            return True
        except OSError:
            return False

    @staticmethod
    def _secure_json(path: Path, *, owner: int, mode: int) -> dict[str, Any]:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != owner
                or stat.S_IMODE(info.st_mode) != mode
                or info.st_size > 65536
            ):
                raise ValueError("unsafe metadata")
            raw = os.read(fd, 65537)
            if len(raw) != info.st_size:
                raise ValueError("short read")
        finally:
            os.close(fd)
        if len(raw) > 65536:
            raise ValueError("oversized data")

        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result

        value = json.loads(raw.decode("ascii"), object_pairs_hook=unique)
        if not isinstance(value, dict):
            raise ValueError("invalid structure")
        return value

    @staticmethod
    def _metadata(
        path: Path,
        *,
        owner: int,
        group: int | None = None,
        mode: int | None = None,
        directory: bool = False,
    ) -> os.stat_result:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("symbolic link")
        expected_type = (
            stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        )
        if not expected_type or info.st_uid != owner:
            raise ValueError("unexpected type or owner")
        if directory and path.resolve(strict=True) != path:
            raise ValueError("directory path contains a symbolic link")
        if group is not None and info.st_gid != group:
            raise ValueError("unexpected group")
        if mode is not None and stat.S_IMODE(info.st_mode) != mode:
            raise ValueError("unexpected permissions")
        if info.st_mode & 0o022:
            raise ValueError("group/world writable")
        return info

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("not a regular file")
            while block := os.read(fd, 1024 * 1024):
                digest.update(block)
        finally:
            os.close(fd)
        return digest.hexdigest()

    def candidate_git(self) -> None:
        try:
            branch = self._git(self.candidate, "branch", "--show-current")
            head = self._git(self.candidate, "rev-parse", "HEAD")
            status = self._git(
                self.candidate, "status", "--porcelain", "--untracked-files=all"
            )
            if any(item.returncode for item in (branch, head, status)):
                raise ValueError
            if branch.stdout.strip() != BRANCH or status.stdout:
                self.add(
                    "candidate_git", "FAIL", "candidate branch or worktree is not clean"
                )
                return
            candidate_sha = head.stdout.strip()
            if not re.fullmatch(r"[0-9a-f]{40}", candidate_sha):
                raise ValueError
            self.add("candidate_git", "PASS", f"clean {BRANCH} at {candidate_sha}")
        except OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired:
            self.add(
                "candidate_git", "UNKNOWN", "candidate Git state could not be verified"
            )

    def runtime(self) -> None:
        okay = sys.version_info[:2] == (3, 14) and platform.machine() == "x86_64"
        self.add(
            "python_and_architecture",
            "PASS" if okay else "FAIL",
            f"Python {sys.version_info.major}.{sys.version_info.minor}; "
            f"architecture {platform.machine()}",
        )
        try:
            user = pwd.getpwnam("wow-armory")
            group = grp.getgrnam("wow-armory")
            self.service_uid, self.service_gid = user.pw_uid, group.gr_gid
            valid = user.pw_gid == group.gr_gid and user.pw_dir == "/nonexistent"
            self.add(
                "service_account_and_group",
                "PASS" if valid else "FAIL",
                "wow-armory account and primary group match the service layout"
                if valid
                else "service account isolation does not match the expected layout",
            )
        except KeyError:
            self.add(
                "service_account_and_group",
                "FAIL",
                "wow-armory account or group is missing",
            )

    def deployment_pin(self) -> None:
        try:
            self._metadata(PIN, owner=0, mode=0o600)
            pin = self._secure_json(PIN, owner=0, mode=0o600)
            if (
                set(pin) != {"release_sha", "previous_sha", "installer_sha256"}
                or not all(isinstance(item, str) for item in pin.values())
                or not re.fullmatch(r"[0-9a-f]{40}", pin["release_sha"])
                or not re.fullmatch(r"[0-9a-f]{40}", pin["previous_sha"])
                or pin["release_sha"] == pin["previous_sha"]
                or not re.fullmatch(r"[0-9a-f]{64}", pin["installer_sha256"])
            ):
                raise ValueError
            self.pin = pin
            self.add(
                "deployment_pin",
                "PASS",
                "root-owned private pin is valid; credential values were not read",
            )
        except OSError, ValueError, KeyError, TypeError:
            self.add(
                "deployment_pin",
                "FAIL",
                "deployment pin is missing, malformed, or has unsafe permissions",
            )

    def checkout(
        self, path: Path, *, candidate: bool = False
    ) -> tuple[str | None, bool]:
        label = "candidate_checkout" if candidate else "production_checkout"
        try:
            if not candidate and not self._trusted_checkout_tree(path):
                self.add(
                    label,
                    "FAIL",
                    "production checkout is not a root-owned, write-protected "
                    "full Git clone",
                )
                return None, False
            root = self._git(path, "rev-parse", "--show-toplevel")
            branch = self._git(path, "branch", "--show-current")
            head = self._git(path, "rev-parse", "HEAD")
            ref = self._git(path, "rev-parse", f"refs/heads/{BRANCH}")
            status = self._git(path, "status", "--porcelain", "--untracked-files=all")
            if any(item.returncode for item in (root, branch, head, ref, status)):
                raise ValueError
            head_sha = head.stdout.strip()
            clean = not status.stdout
            expected = self.pin["release_sha"] if self.pin else None
            branch_ref_matches = ref.stdout.strip() == head_sha
            okay = (
                Path(root.stdout.strip()) == path
                and branch.stdout.strip() == BRANCH
                and re.fullmatch(r"[0-9a-f]{40}", head_sha) is not None
                and clean
                and branch_ref_matches
                and (expected is None or head_sha == expected)
            )
            if candidate:
                project = tomllib.loads(
                    (path / "pyproject.toml").read_text(encoding="utf-8")
                )
                okay = okay and project["project"]["version"] == "2.0.0"
                cand_head = self._git(self.candidate, "rev-parse", "HEAD")
                okay = (
                    okay
                    and cand_head.returncode == 0
                    and head_sha == cand_head.stdout.strip()
                )
            self.add(
                label,
                "PASS" if okay else "FAIL",
                f"{BRANCH} at {head_sha}; worktree "
                f"{'clean' if clean else 'has changes'}",
            )
            return head_sha, okay
        except OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired:
            self.add(
                label,
                "UNKNOWN",
                "checkout branch, commit, or worktree could not be verified",
            )
            return None, False

    def systemd(self) -> None:
        states: dict[str, tuple[str, str]] = {}
        try:
            for unit in UNITS:
                active = self._run(["/usr/bin/systemctl", "is-active", unit])
                enabled = self._run(["/usr/bin/systemctl", "is-enabled", unit])
                if active.returncode not in {0, 1, 3, 4} or enabled.returncode not in {
                    0,
                    1,
                    3,
                    4,
                }:
                    raise ValueError
                states[unit] = (active.stdout.strip(), enabled.stdout.strip())
            self.unit_states = states
            self.unit_states_checked = True
            expected = {
                SERVICE: ("active", "enabled"),
                BACKUP_TIMER: ("active", "enabled"),
                BACKUP_SERVICE: ("inactive", "static"),
            }
            okay = states == expected or (
                states.get(BACKUP_SERVICE) == ("inactive", "disabled")
                and states.get(SERVICE) == expected[SERVICE]
                and states.get(BACKUP_TIMER) == expected[BACKUP_TIMER]
            )
            summary = "; ".join(f"{name}: {a}/{e}" for name, (a, e) in states.items())
            self.add("systemd_states", "PASS" if okay else "FAIL", summary)
        except OSError, ValueError, subprocess.TimeoutExpired:
            self.add(
                "systemd_states", "UNKNOWN", "read-only systemd state queries failed"
            )

    def release_pointer(self) -> None:
        if not self.pin:
            self.add(
                "current_release",
                "UNKNOWN",
                "cannot compare current release without a valid pin",
            )
            return
        previous = self.pin["previous_sha"]
        current = BASE / "current"
        try:
            link = current.lstat()
            if not stat.S_ISLNK(link.st_mode):
                raise ValueError
            target = current.resolve(strict=True)
            expected = BASE / "releases" / previous
            marker = target / ".installation-complete"
            self._metadata(marker, owner=0, mode=0o444)
            value = marker.read_text(encoding="ascii").strip()
            okay = target == expected and value == previous
            self.add(
                "current_release",
                "PASS" if okay else "FAIL",
                "current points to the pinned previous release"
                if okay
                else "current or its completion marker differs from the "
                "pinned previous release",
            )
            if self.service_uid is None or self.service_gid is None:
                raise ValueError
            safe_tree = True
            for directory, children, files in os.walk(target, followlinks=False):
                for item in [
                    Path(directory),
                    *(Path(directory) / name for name in children),
                ]:
                    if item.is_symlink():
                        continue
                    info = item.stat()
                    if (
                        info.st_uid != 0
                        or info.st_mode & 0o022
                        or not self._permits(
                            info, self.service_uid, self.service_gid, 0o1
                        )
                    ):
                        safe_tree = False
                for name in files:
                    item = Path(directory) / name
                    if item.is_symlink():
                        continue
                    info = item.stat()
                    if (
                        info.st_uid != 0
                        or info.st_mode & 0o022
                        or not self._permits(
                            info, self.service_uid, self.service_gid, 0o4
                        )
                        or (
                            info.st_mode & 0o111
                            and not self._permits(
                                info, self.service_uid, self.service_gid, 0o1
                            )
                        )
                    ):
                        safe_tree = False
            interpreter = target / ".venv/bin/python"
            info = interpreter.stat()
            safe_python = (
                stat.S_ISREG(info.st_mode)
                and info.st_uid == 0
                and not info.st_mode & 0o022
                and self._permits(info, self.service_uid, self.service_gid, 0o4)
                and self._permits(info, self.service_uid, self.service_gid, 0o1)
            )
            valid_permissions = safe_tree and safe_python
            self.add(
                "installed_release_permissions",
                "PASS" if valid_permissions else "FAIL",
                "release tree is root-owned and service-readable/traversable; "
                "interpreter is readable and executable"
                if valid_permissions
                else "release tree or interpreter permissions are unsafe",
            )
        except OSError, ValueError, UnicodeError:
            self.add(
                "current_release",
                "FAIL",
                "current link or pinned release marker is missing or unsafe",
            )
            self.add(
                "installed_release_permissions",
                "UNKNOWN",
                "release permissions could not be checked",
            )

    @staticmethod
    def _permits(info: os.stat_result, uid: int, gid: int, bit: int) -> bool:
        mode = stat.S_IMODE(info.st_mode)
        if uid == info.st_uid:
            return bool(mode & (bit << 6))
        if gid == info.st_gid:
            return bool(mode & (bit << 3))
        return bool(mode & bit)

    def files_and_hashes(self) -> None:
        if self.service_uid is None or self.service_gid is None:
            self.add(
                "protected_files", "UNKNOWN", "service account metadata is unavailable"
            )
            self.add(
                "installed_helper_hashes",
                "UNKNOWN",
                "service account metadata is unavailable",
            )
            return
        checks = (
            (BASE, True, 0, 0, 0o755),
            (BASE / "releases", True, 0, 0, 0o755),
            (BASE / "bin", True, 0, 0, 0o755),
            (ETC, True, 0, self.service_gid, 0o750),
            (ETC / "config", False, 0, self.service_gid, 0o640),
            (ETC / "discord-token", False, 0, 0, 0o600),
            (Path("/etc/systemd/system"), True, 0, None, None),
            (STATE, True, self.service_uid, self.service_gid, 0o700),
            (STATE / "backups", True, self.service_uid, self.service_gid, 0o700),
            (AUDIT, True, 0, 0, 0o700),
        )
        failures = []
        for path, is_dir, owner, group, mode in checks:
            try:
                self._metadata(
                    path, owner=owner, group=group, mode=mode, directory=is_dir
                )
            except OSError, ValueError:
                failures.append(path.name)
        self.add(
            "protected_files",
            "PASS" if not failures else "FAIL",
            "configuration, credential, state and audit metadata are private"
            if not failures
            else "unsafe or missing metadata: " + ", ".join(failures),
        )

        if not self.pin:
            self.add(
                "installed_helper_hashes",
                "UNKNOWN",
                "installer hash cannot be checked without a valid pin",
            )
            return
        installed = BASE / "bin/production_install.py"
        wrapper = Path("/usr/local/sbin/armory-deploy-v2")
        preflight = BASE / "bin/armory-db-preflight"
        try:
            self._metadata(installed, owner=0, mode=0o755)
            installer_ok = self._sha256(installed) == self.pin["installer_sha256"]
            source_ok = (
                self._sha256(REPO / "deploy/production_install.py")
                == self.pin["installer_sha256"]
            )
            self._metadata(wrapper, owner=0, mode=0o755)
            wrapper_ok = self._sha256(wrapper) == self._sha256(
                REPO / "deploy/production_wrapper.py"
            )
            self._metadata(preflight, owner=0, mode=0o755)
            preflight_hash = self._sha256(preflight)
            preflight_ok = (
                preflight_hash == APPROVED_PREFLIGHT_SHA256
                and preflight_hash
                == self._sha256(REPO / "deploy/systemd/armory-db-preflight.py")
            )
            units_ok = True
            for unit in UNITS:
                installed_unit = Path("/etc/systemd/system") / unit
                source_unit = REPO / "deploy/systemd" / unit
                self._metadata(installed_unit, owner=0, mode=0o644)
                units_ok = units_ok and self._sha256(installed_unit) == self._sha256(
                    source_unit
                )
            launcher = BASE / "bin/armory-launch"
            self._metadata(launcher, owner=0, mode=0o755)
            launcher_ok = self._sha256(launcher) == self._sha256(
                REPO / "deploy/systemd/armory-launch"
            )
            all_ok = (
                installer_ok
                and source_ok
                and wrapper_ok
                and preflight_ok
                and units_ok
                and launcher_ok
            )
            self.add(
                "installed_helper_hashes",
                "PASS" if all_ok else "FAIL",
                "installed wrapper, installer, preflight, units and launcher "
                "match their approved source hashes"
                if all_ok
                else "one or more installed helper, unit or launcher hashes differ",
            )
        except OSError, ValueError:
            self.add(
                "installed_helper_hashes",
                "FAIL",
                "an installed helper is unavailable or has unsafe metadata",
            )

    def installed_artifacts(self) -> None:
        if not self.pin:
            self.add(
                "v2_target_release",
                "UNKNOWN",
                "cannot inspect target release without a valid pin",
            )
            return
        target = BASE / "releases" / self.pin["release_sha"]
        try:
            info = target.lstat()
        except FileNotFoundError:
            self.add(
                "v2_target_release", "PASS", "pinned v2.0.0 release directory is absent"
            )
            return
        except OSError:
            self.add(
                "v2_target_release",
                "UNKNOWN",
                "target release metadata could not be read",
            )
            return
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            self.add(
                "v2_target_release",
                "FAIL",
                "target release path exists but is not a normal directory",
            )
            return
        marker = target / ".installation-complete"
        try:
            self._metadata(marker, owner=0, mode=0o444)
            complete = (
                marker.read_text(encoding="ascii").strip() == self.pin["release_sha"]
            )
        except OSError, ValueError, UnicodeError:
            complete = False
        self.add(
            "v2_target_release",
            "FAIL",
            "target release directory already exists; it is complete"
            if complete
            else "target release directory exists without a valid completion "
            "marker; possible partial installation",
        )

    def database(self, path: Path, check_name: str) -> tuple[str, str]:
        if self.service_uid is None or self.service_gid is None:
            return "UNKNOWN", "service account metadata is unavailable"
        if not self.unit_states_checked:
            return "UNKNOWN", "service writer state could not be verified safely"
        active_unit = SERVICE if check_name == "production_database" else BACKUP_SERVICE
        if self.unit_states.get(active_unit, ("unknown", ""))[0] != "inactive":
            return (
                "UNKNOWN",
                "database has an active writer; preflight will not inspect a "
                "changing SQLite file",
            )
        try:
            info = self._metadata(
                path, owner=self.service_uid, group=self.service_gid, mode=0o600
            )
            sidecars = [
                Path(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal")
            ]
            if any(sidecar.exists() or sidecar.is_symlink() for sidecar in sidecars):
                return (
                    "UNKNOWN",
                    "SQLite sidecars exist; immutable read-only validation cannot "
                    "safely include them",
                )
            uri = path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
            with sqlite3.connect(uri, uri=True, timeout=2) as connection:
                connection.execute("PRAGMA query_only=ON")
                integrity = connection.execute("PRAGMA integrity_check").fetchone()
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            after = path.stat()
            signature_before = (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
            signature_after = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if signature_before != signature_after:
                return "UNKNOWN", "database changed while it was being inspected"
            okay = integrity == ("ok",) and REQUIRED_TABLES <= tables
            return (
                (
                    "PASS",
                    "SQLite integrity and required Armory tables verified read-only",
                )
                if okay
                else ("FAIL", "SQLite integrity or required Armory schema check failed")
            )
        except OSError, ValueError, sqlite3.Error:
            return (
                "FAIL",
                "database is missing, has unsafe permissions, or failed a "
                "read-only integrity check",
            )

    def database_checks(self) -> None:
        status, detail = self.database(
            STATE / "guild-armory.sqlite3", "production_database"
        )
        self.add("production_database", status, detail)
        if self.service_uid is None or self.service_gid is None:
            self.add(
                "latest_backup", "UNKNOWN", "service account metadata is unavailable"
            )
            return
        try:
            backup_dir = STATE / "backups"
            self._metadata(
                backup_dir,
                owner=self.service_uid,
                group=self.service_gid,
                mode=0o700,
                directory=True,
            )
            candidates = [
                item
                for item in backup_dir.glob("guild-armory-*.sqlite3")
                if not item.is_symlink() and item.is_file()
            ]
            if not candidates:
                self.add(
                    "latest_backup", "FAIL", "no regular database backup was found"
                )
                return
            latest = max(candidates, key=lambda item: item.stat().st_mtime_ns)
            status, detail = self.database(latest)
            if status == "PASS":
                journal = AUDIT / "deployment-state.json"
                if journal.exists():
                    try:
                        record = self._secure_json(journal, owner=0, mode=0o600)
                        if record.get("database_backup") == str(latest):
                            expected = record.get("database_backup_sha256")
                            if (
                                not isinstance(expected, str)
                                or self._sha256(latest) != expected
                            ):
                                status, detail = (
                                    "FAIL",
                                    "latest backup does not match its journal checksum",
                                )
                    except OSError, ValueError:
                        status, detail = (
                            "UNKNOWN",
                            "backup hash could not be compared with the "
                            "deployment journal",
                        )
            self.add(
                "latest_backup",
                status,
                f"{detail}; {len(candidates)} regular backup(s) present",
            )
        except OSError, ValueError:
            self.add(
                "latest_backup",
                "FAIL",
                "backup directory or newest backup has unsafe metadata",
            )

    def incident_journal(self) -> None:
        active = AUDIT / "deployment-state.json"
        temporary = AUDIT / ".deployment-state.json.tmp"
        incident_manifest = AUDIT / f"deployment-{FAILED_V1_RELEASE}.json"
        try:
            self._metadata(AUDIT, owner=0, mode=0o700, directory=True)
            if (
                active.exists()
                or active.is_symlink()
                or temporary.exists()
                or temporary.is_symlink()
            ):
                try:
                    record = self._secure_json(active, owner=0, mode=0o600)
                    phase = record.get("phase")
                    interrupted = phase == "manual_recovery_required"
                    detail = (
                        "deployment journal remains and requires administrator "
                        "acknowledgment"
                        if interrupted
                        else "deployment journal remains; wrapper treats it as a "
                        "recovery block until administrator acknowledgment"
                    )
                except OSError, ValueError:
                    detail = (
                        "deployment journal exists but could not be safely "
                        "validated; administrator review is required"
                    )
                self.add("deployment_recovery_ack", "FAIL", detail)
            else:
                self.add(
                    "deployment_recovery_ack",
                    "PASS",
                    "no active deployment journal blocks a future wrapper run",
                )
            if incident_manifest.exists() or incident_manifest.is_symlink():
                self._metadata(incident_manifest, owner=0, mode=0o600)
                incident = self._secure_json(incident_manifest, owner=0, mode=0o600)
                is_incident = incident.get("approved_release_sha") == FAILED_V1_RELEASE
                self.add(
                    "failed_v1_1_4_manifest",
                    "PASS" if is_incident else "FAIL",
                    "preserved v1.1.4 incident manifest is valid and private"
                    if is_incident
                    else "manifest at the v1.1.4 path has an unexpected "
                    "release identity",
                )
            else:
                self.add(
                    "failed_v1_1_4_manifest",
                    "UNKNOWN",
                    "preserved v1.1.4 manifest was not found at its expected "
                    "audit path",
                )
        except OSError, ValueError:
            self.add(
                "deployment_recovery_ack",
                "UNKNOWN",
                "audit directory metadata could not be verified",
            )
            self.add(
                "failed_v1_1_4_manifest",
                "UNKNOWN",
                "audit evidence could not be safely inspected",
            )

    def run(self) -> dict[str, Result]:
        self.candidate_git()
        self.runtime()
        self.deployment_pin()
        self.checkout(REPO)
        self.checkout(self.candidate, candidate=True)
        self.systemd()
        self.release_pointer()
        self.files_and_hashes()
        self.installed_artifacts()
        self.database_checks()
        self.incident_journal()
        return self.checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidate",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="candidate checkout containing this script (default: script's repository)",
    )
    args = parser.parse_args()
    checks = Preflight(args.candidate.resolve()).run()
    for name, result in checks.items():
        print(f"{result.status:7} {name}: {result.detail}")
    counts = {
        status: sum(result.status == status for result in checks.values())
        for status in ("PASS", "FAIL", "UNKNOWN")
    }
    print("Summary: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    return 1 if counts["FAIL"] else 2 if counts["UNKNOWN"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
