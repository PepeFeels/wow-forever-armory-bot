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
from datetime import UTC, datetime
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
SYSTEM_PYTHON = "/usr/bin/python3.14"
VENV_SYMLINK_TARGETS = {
    ".venv/lib64": "lib",
    ".venv/bin/python": "python3.14",
    ".venv/bin/python3": "python3.14",
    ".venv/bin/python3.14": SYSTEM_PYTHON,
    ".venv/bin/𝜋thon": "python3.14",
}
SQLITE_READONLY_CHECK = r"""
import sqlite3, sys
uri = f"file:/proc/self/fd/{sys.argv[1]}?mode=ro&immutable=1"
try:
    with sqlite3.connect(uri, uri=True, timeout=2) as connection:
        connection.execute("PRAGMA query_only=ON")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    print("PASS" if integrity == ("ok",) and
          {"guilds", "members", "characters", "flows"} <= tables else "FAIL")
except sqlite3.Error:
    print("FAIL")
"""
GROUPS = {
    "preflight_execution_source": "Preflight safety",
    "python_and_architecture": "Preflight safety",
    "candidate_git": "v2.0.0 deployment prerequisites",
    "candidate_checkout": "v2.0.0 deployment prerequisites",
    "production_checkout": "v2.0.0 deployment prerequisites",
    "deployment_pin": "v2.0.0 deployment prerequisites",
    "installed_helper_hashes": "v2.0.0 deployment prerequisites",
    "v2_target_release": "v2.0.0 deployment prerequisites",
    "deployment_recovery_ack": "v2.0.0 deployment prerequisites",
    "failed_v1_1_4_manifest": "v2.0.0 deployment prerequisites",
}
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
    group: str
    status: str
    detail: str


class Preflight:
    """Collect fixed, non-sensitive diagnostics from production paths."""

    def __init__(self, candidate: Path, expected_candidate_sha: str) -> None:
        self.candidate = candidate
        self.expected_candidate_sha = expected_candidate_sha
        self.checks: dict[str, Result] = {}
        self.pin: dict[str, str] | None = None
        self.service_uid: int | None = None
        self.service_gid: int | None = None
        self.unit_states: dict[str, tuple[str, str]] = {}
        self.unit_states_checked = False

    def add(
        self,
        name: str,
        status: str,
        detail: str,
        *,
        group: str = "Current production health",
    ) -> None:
        self.checks[name] = Result(GROUPS.get(name, group), status, detail)

    @staticmethod
    def _trusted_execution_source() -> bool:
        """Reject root execution from a developer-writable source path."""
        if os.geteuid() != 0:
            return False
        source = Path(__file__).absolute()
        try:
            info = source.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or info.st_mode & 0o022
            ):
                return False
            parent = source.parent
            while True:
                parent_info = parent.lstat()
                if (
                    stat.S_ISLNK(parent_info.st_mode)
                    or not stat.S_ISDIR(parent_info.st_mode)
                    or parent_info.st_uid != 0
                    or parent_info.st_mode & 0o022
                ):
                    return False
                if parent == parent.parent:
                    break
                parent = parent.parent
            return True
        except OSError:
            return False

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
    def _walk_error(error: OSError) -> None:
        raise error

    @staticmethod
    def _has_python_bytecode_cache(path: Path) -> bool:
        for directory, names, files in os.walk(
            path, followlinks=False, onerror=Preflight._walk_error
        ):
            if Path(directory).name == "__pycache__":
                return True
            if any(name == "__pycache__" for name in names) or any(
                Path(name).suffix in {".pyc", ".pyo"} for name in files
            ):
                return True
        return False

    @staticmethod
    def _trusted_checkout_tree(path: Path) -> bool:
        try:
            if not path.is_absolute() or path.resolve(strict=True) != path:
                return False
            if Preflight._has_python_bytecode_cache(path):
                return False
            for parent in path.parents:
                parent_info = parent.lstat()
                if (
                    stat.S_ISLNK(parent_info.st_mode)
                    or not stat.S_ISDIR(parent_info.st_mode)
                    or parent_info.st_uid != 0
                    or parent_info.st_mode & 0o022
                ):
                    return False
            marker = path / ".git"
            marker_info = marker.lstat()
            if not stat.S_ISDIR(marker_info.st_mode):
                return False
            for directory, names, files in os.walk(
                path, followlinks=False, onerror=Preflight._walk_error
            ):
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

    def _git_config_is_safe(self, path: Path) -> bool:
        result = self._git(
            path, "config", "--local", "--no-includes", "--name-only", "--list"
        )
        if result.returncode != 0:
            return False
        unsafe_prefixes = ("filter.", "include.", "includeif.")
        for raw_key in result.stdout.splitlines():
            key = raw_key.strip().lower()
            if (
                key.startswith(unsafe_prefixes)
                or key
                in {
                    "core.attributesfile",
                    "core.excludesfile",
                    "extensions.worktreeconfig",
                }
                or key == "diff.external"
                or (key.startswith("diff.") and key.endswith(".textconv"))
            ):
                return False
        return True

    @staticmethod
    def _secure_json_with_hash(
        path: Path, *, owner: int, mode: int
    ) -> tuple[dict[str, Any], str]:
        fd = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != owner
                or stat.S_IMODE(before.st_mode) != mode
                or before.st_nlink != 1
                or before.st_size > 65536
            ):
                raise ValueError("unsafe metadata")
            content = bytearray()
            while len(content) <= 65536:
                block = os.read(fd, min(65537 - len(content), 8192))
                if not block:
                    break
                content.extend(block)
            after = os.fstat(fd)
            before_signature = (
                before.st_dev,
                before.st_ino,
                before.st_mode,
                before.st_uid,
                before.st_gid,
                before.st_nlink,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            after_signature = (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_uid,
                after.st_gid,
                after.st_nlink,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            entry = os.stat(path, follow_symlinks=False)
            entry_signature = (
                entry.st_dev,
                entry.st_ino,
                entry.st_mode,
                entry.st_uid,
                entry.st_gid,
                entry.st_nlink,
                entry.st_size,
                entry.st_mtime_ns,
                entry.st_ctime_ns,
            )
            if (
                len(content) != before.st_size
                or before_signature != after_signature
                or entry_signature != after_signature
                or not stat.S_ISREG(entry.st_mode)
            ):
                raise ValueError("file changed or short read")
            raw = bytes(content)
        finally:
            os.close(fd)

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
        return value, hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _secure_json(path: Path, *, owner: int, mode: int) -> dict[str, Any]:
        return Preflight._secure_json_with_hash(path, owner=owner, mode=mode)[0]

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
            if not re.fullmatch(r"[0-9a-f]{40}", self.expected_candidate_sha):
                self.add(
                    "candidate_git",
                    "FAIL",
                    "an explicit full expected candidate SHA is required",
                )
                return
            if not self._trusted_checkout_tree(self.candidate):
                self.add(
                    "candidate_git",
                    "FAIL",
                    "candidate checkout is not root-owned and write-protected",
                )
                return
            branch = self._git(self.candidate, "branch", "--show-current")
            head = self._git(self.candidate, "rev-parse", "HEAD")
            ref = self._git(self.candidate, "rev-parse", f"refs/heads/{BRANCH}")
            if any(item.returncode for item in (branch, head, ref)):
                raise ValueError
            candidate_sha = head.stdout.strip()
            if (
                branch.stdout.strip() != BRANCH
                or ref.stdout.strip() != self.expected_candidate_sha
                or candidate_sha != self.expected_candidate_sha
            ):
                self.add(
                    "candidate_git",
                    "FAIL",
                    "candidate branch or commit differs from the explicit expected SHA",
                )
                return
            if not self._git_config_is_safe(self.candidate):
                self.add(
                    "candidate_git",
                    "FAIL",
                    "candidate Git config contains an external filter or include",
                )
                return
            status = self._git(
                self.candidate, "status", "--porcelain", "--untracked-files=all"
            )
            if status.returncode != 0:
                raise ValueError
            if status.stdout:
                self.add(
                    "candidate_git",
                    "FAIL",
                    "candidate worktree is not clean",
                )
                return
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
        except FileNotFoundError:
            self.add(
                "deployment_pin",
                "UNKNOWN",
                "v2.0.0 deployment pin has not been provisioned",
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
            try:
                path.lstat()
            except FileNotFoundError:
                status = "FAIL" if candidate else "UNKNOWN"
                detail = (
                    "reviewed candidate checkout is missing"
                    if candidate
                    else "v2.0.0 production checkout has not been provisioned"
                )
                self.add(label, status, detail)
                return None, False
            if not self._trusted_checkout_tree(path):
                self.add(
                    label,
                    "FAIL",
                    "checkout is not a root-owned, write-protected full Git clone",
                )
                return None, False
            root = self._git(path, "rev-parse", "--show-toplevel")
            branch = self._git(path, "branch", "--show-current")
            head = self._git(path, "rev-parse", "HEAD")
            if not candidate and self.pin is None:
                if any(item.returncode for item in (root, branch, head)):
                    raise ValueError
                branch_name = branch.stdout.strip()
                head_sha = head.stdout.strip()
                if (
                    Path(root.stdout.strip()) != path
                    or not branch_name
                    or not re.fullmatch(r"[0-9a-f]{40}", head_sha)
                ):
                    self.add(
                        label,
                        "UNKNOWN",
                        "production checkout branch or commit is not available",
                    )
                    return None, False
                current_ref = self._git(path, "rev-parse", f"refs/heads/{branch_name}")
                if (
                    current_ref.returncode != 0
                    or current_ref.stdout.strip() != head_sha
                ):
                    self.add(
                        label,
                        "UNKNOWN",
                        "production checkout branch identity is not verifiable",
                    )
                    return head_sha, False
                if not self._git_config_is_safe(path):
                    self.add(
                        label,
                        "FAIL",
                        "checkout Git config contains an external filter or include",
                    )
                    return head_sha, False
                worktree_status = self._git(
                    path, "status", "--porcelain", "--untracked-files=all"
                )
                if worktree_status.returncode != 0:
                    raise ValueError
                if worktree_status.stdout:
                    self.add(
                        label,
                        "FAIL",
                        "production checkout has uncommitted changes",
                    )
                    return head_sha, False
                self.add(
                    label,
                    "UNKNOWN",
                    f"current production checkout is clean at {head_sha}; "
                    "v2.0.0 pin is not provisioned",
                )
                return head_sha, False
            ref = self._git(path, "rev-parse", f"refs/heads/{BRANCH}")
            if any(item.returncode for item in (root, branch, head, ref)):
                raise ValueError
            head_sha = head.stdout.strip()
            identity_matches = (
                Path(root.stdout.strip()) == path
                and branch.stdout.strip() == BRANCH
                and re.fullmatch(r"[0-9a-f]{40}", head_sha) is not None
                and ref.stdout.strip() == head_sha
            )
            expected = (
                self.expected_candidate_sha
                if candidate
                else self.pin["release_sha"]
                if self.pin
                else None
            )
            if candidate:
                identity_matches = identity_matches and head_sha == expected
            elif expected is not None:
                identity_matches = identity_matches and head_sha == expected
            if not identity_matches:
                self.add(
                    label,
                    "FAIL",
                    "checkout branch or commit does not match its approved identity",
                )
                return head_sha, False
            if not self._git_config_is_safe(path):
                self.add(
                    label,
                    "FAIL",
                    "checkout Git config contains an external filter or include",
                )
                return head_sha, False
            if candidate:
                project = tomllib.loads(
                    (path / "pyproject.toml").read_text(encoding="utf-8")
                )
                if project["project"]["version"] != "2.0.0":
                    self.add(
                        label,
                        "FAIL",
                        "candidate package metadata is not version 2.0.0",
                    )
                    return head_sha, False
            worktree_status = self._git(
                path, "status", "--porcelain", "--untracked-files=all"
            )
            if worktree_status.returncode != 0:
                raise ValueError
            clean = not worktree_status.stdout
            okay = clean
            if not candidate and self.pin is None:
                self.add(
                    label,
                    "FAIL" if not clean else "UNKNOWN",
                    "production checkout has uncommitted changes"
                    if not clean
                    else "production v2.0.0 checkout/pin is not provisioned yet",
                )
                return head_sha, False
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

    @staticmethod
    def _managed_release_target(current: Path) -> Path:
        raw_target = os.readlink(current)
        relative = re.fullmatch(r"releases/([0-9a-f]{40})", raw_target)
        absolute = re.fullmatch(
            re.escape(str(BASE / "releases")) + r"/([0-9a-f]{40})", raw_target
        )
        match = relative or absolute
        if match is None:
            raise ValueError("unsafe current symlink release identity")
        return BASE / "releases" / match.group(1)

    def release_pointer(self) -> None:
        previous = self.pin["previous_sha"] if self.pin else None
        current = BASE / "current"
        try:
            if not self._trusted_directory_chain(BASE / "releases"):
                raise ValueError("untrusted release path")
            link = current.lstat()
            if not stat.S_ISLNK(link.st_mode) or link.st_uid != 0:
                raise ValueError
            release_path = self._managed_release_target(current)
            release_info = release_path.lstat()
            if not stat.S_ISDIR(release_info.st_mode) or release_info.st_uid != 0:
                raise ValueError("release target is not a root-owned directory")
            target = release_path.resolve(strict=True)
            if target != release_path:
                raise ValueError("release target contains a symbolic link")
            expected = BASE / "releases" / previous if previous else None
            marker = target / ".installation-complete"
            self._metadata(marker, owner=0, mode=0o444)
            value = marker.read_text(encoding="ascii").strip()
            if previous is None:
                okay = (
                    target.parent == BASE / "releases"
                    and target.name == value
                    and re.fullmatch(r"[0-9a-f]{40}", value) is not None
                )
                detail = (
                    "current release and marker are internally consistent; "
                    "v2 previous SHA is not provisioned"
                )
            else:
                okay = target == expected and value == previous
                detail = (
                    "current points to the pinned previous release"
                    if okay
                    else "current or its completion marker differs from the "
                    "pinned previous release"
                )
            self.add(
                "current_release",
                "PASS" if okay else "FAIL",
                detail,
            )
            if self.service_uid is None or self.service_gid is None:
                raise ValueError
            safe_tree = self._release_tree_is_safe(
                target, self.service_uid, self.service_gid
            )
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

    @staticmethod
    def _trusted_directory_chain(path: Path, *, owner: int = 0) -> bool:
        try:
            for item in reversed((path, *path.parents)):
                info = item.lstat()
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or stat.S_ISLNK(info.st_mode)
                    or info.st_uid != owner
                    or info.st_mode & 0o022
                ):
                    return False
            return True
        except OSError:
            return False

    @classmethod
    def _release_tree_is_safe(
        cls, release: Path, uid: int, gid: int, *, owner: int = 0
    ) -> bool:
        """Check release paths without following untrusted symbolic links.

        A venv interpreter may resolve to the configured system Python. All
        other links must resolve within this release directory.
        """
        try:
            root = release.resolve(strict=True)
            if root != release:
                return False
            root_info = root.lstat()
            if (
                not stat.S_ISDIR(root_info.st_mode)
                or root_info.st_uid != owner
                or root_info.st_mode & 0o022
                or not cls._permits(root_info, uid, gid, 0o1)
            ):
                return False
            system_python = Path(SYSTEM_PYTHON).resolve(strict=True)
            if not cls._trusted_directory_chain(system_python.parent, owner=owner):
                return False
            symlink_snapshots: dict[Path, tuple[tuple[int, ...], str, Path]] = {}
            for directory, children, files in os.walk(
                root, followlinks=False, onerror=cls._walk_error
            ):
                parent = Path(directory)
                for name in children + files:
                    item = parent / name
                    entry = item.lstat()
                    if entry.st_uid != owner:
                        return False
                    if stat.S_ISLNK(entry.st_mode):
                        relative_name = item.relative_to(root).as_posix()
                        expected_target = VENV_SYMLINK_TARGETS.get(relative_name)
                        if relative_name == ".venv/bin/python3.14":
                            expected_target = SYSTEM_PYTHON
                        target_text = os.readlink(item)
                        if (
                            expected_target is None
                            or target_text != expected_target
                        ):
                            return False
                        resolved = item.resolve(strict=True)
                        expected_resolved = (
                            root / ".venv/lib"
                            if relative_name == ".venv/lib64"
                            else system_python
                        )
                        if resolved != expected_resolved:
                            return False
                        info = resolved.stat()
                        symlink_snapshots[item] = (
                            (
                                entry.st_dev,
                                entry.st_ino,
                                entry.st_mode,
                                entry.st_uid,
                                entry.st_gid,
                                entry.st_nlink,
                                entry.st_size,
                                entry.st_mtime_ns,
                                entry.st_ctime_ns,
                            ),
                            target_text,
                            resolved,
                        )
                    else:
                        info = entry
                    if info.st_uid != owner or info.st_mode & 0o022:
                        return False
                    if stat.S_ISDIR(info.st_mode):
                        if not cls._permits(info, uid, gid, 0o1):
                            return False
                    elif stat.S_ISREG(info.st_mode):
                        if not cls._permits(info, uid, gid, 0o4):
                            return False
                        if info.st_mode & 0o111 and not cls._permits(
                            info, uid, gid, 0o1
                        ):
                            return False
                    else:
                        return False
            for item, (expected, target_text, resolved) in symlink_snapshots.items():
                current = item.lstat()
                current_signature = (
                    current.st_dev,
                    current.st_ino,
                    current.st_mode,
                    current.st_uid,
                    current.st_gid,
                    current.st_nlink,
                    current.st_size,
                    current.st_mtime_ns,
                    current.st_ctime_ns,
                )
                if (
                    not stat.S_ISLNK(current.st_mode)
                    or current_signature != expected
                    or os.readlink(item) != target_text
                    or item.resolve(strict=True) != resolved
                ):
                    return False
            return True
        except OSError, RuntimeError, ValueError:
            return False

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
        if check_name not in {"production_database", "latest_backup"}:
            return "UNKNOWN", "database check identifier is not recognized"
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
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != self.service_uid
                or before.st_gid != self.service_gid
                or stat.S_IMODE(before.st_mode) != 0o600
            ):
                return "FAIL", "database has unsafe type, owner, group, or permissions"
            if not self._database_path_matches(path, before):
                return "UNKNOWN", "database path changed while it was being opened"
            if self._sqlite_sidecars_exist(path):
                return (
                    "UNKNOWN",
                    "SQLite sidecars exist; immutable read-only validation cannot "
                    "safely include them",
                )
            if os.geteuid() == 0:
                result = subprocess.run(
                    [
                        SYSTEM_PYTHON,
                        "-I",
                        "-B",
                        "-c",
                        SQLITE_READONLY_CHECK,
                        str(descriptor),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="ascii",
                    errors="ignore",
                    env=CHILD_ENV,
                    cwd="/",
                    timeout=30,
                    check=False,
                    pass_fds=(descriptor,),
                    user=self.service_uid,
                    group=self.service_gid,
                    extra_groups=(),
                )
            elif os.geteuid() == self.service_uid and os.getegid() == self.service_gid:
                uri = f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1"
                with sqlite3.connect(uri, uri=True, timeout=2) as connection:
                    connection.execute("PRAGMA query_only=ON")
                    integrity = connection.execute("PRAGMA integrity_check").fetchone()
                    tables = {
                        row[0]
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                result = subprocess.CompletedProcess(
                    ["sqlite-readonly-check"],
                    0,
                    "PASS"
                    if integrity == ("ok",) and REQUIRED_TABLES <= tables
                    else "FAIL",
                    "",
                )
            else:
                return (
                    "UNKNOWN",
                    "SQLite check cannot safely drop to the service account",
                )
            after = os.fstat(descriptor)
            if self._file_signature(before) != self._file_signature(after):
                return "UNKNOWN", "database changed while it was being inspected"
            if not self._database_path_matches(path, after):
                return "UNKNOWN", "database path changed while it was being inspected"
            if os.geteuid() == 0:
                writer_state = self._run(
                    ["/usr/bin/systemctl", "is-active", active_unit]
                )
                if (
                    writer_state.returncode != 3
                    or writer_state.stdout.strip() != "inactive"
                ):
                    return (
                        "UNKNOWN",
                        "writer state changed or could not be rechecked during "
                        "SQLite inspection",
                    )
            if self._sqlite_sidecars_exist(path):
                return "UNKNOWN", "SQLite sidecars appeared during the read-only check"
            if result.returncode != 0 or result.stdout.strip() not in {"PASS", "FAIL"}:
                return "UNKNOWN", "read-only SQLite checker could not complete safely"
            okay = result.stdout.strip() == "PASS"
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
        except subprocess.TimeoutExpired:
            return "UNKNOWN", "read-only SQLite check timed out"
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _file_signature(info: os.stat_result) -> tuple[int, ...]:
        return (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    @staticmethod
    def _database_path_matches(path: Path, expected: os.stat_result) -> bool:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            current = os.fstat(descriptor)
            return (
                stat.S_ISREG(current.st_mode)
                and current.st_dev == expected.st_dev
                and current.st_ino == expected.st_ino
            )
        except OSError:
            return False
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _sqlite_sidecars_exist(database: Path) -> bool:
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(database) + suffix)
            try:
                sidecar.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                return True
            return True
        return False

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
            status, detail = self.database(latest, "latest_backup")
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

    @classmethod
    def _verify_archived_deployments(cls, audit: Path, *, owner: int) -> int:
        try:
            entries = tuple(audit.iterdir())
        except OSError as exc:
            raise ValueError("audit history cannot be listed") from exc

        archives: dict[str, Path] = {}
        acknowledgments: dict[str, Path] = {}
        manifests: dict[str, Path] = {}
        for entry in entries:
            if entry.name.startswith("deployment-state-"):
                match = re.fullmatch(
                    r"deployment-state-([0-9a-f]{64})\.json", entry.name
                )
                if match is None or match.group(1) in archives:
                    raise ValueError("archived journal name is invalid")
                archives[match.group(1)] = entry
            elif entry.name.startswith("deployment-recovery-ack-"):
                match = re.fullmatch(
                    r"deployment-recovery-ack-([0-9a-f]{64})\.json", entry.name
                )
                if match is None or match.group(1) in acknowledgments:
                    raise ValueError("acknowledgment name is invalid")
                acknowledgments[match.group(1)] = entry
            else:
                match = re.fullmatch(r"deployment-([0-9a-f]{40})\.json", entry.name)
                if match is not None:
                    manifests[match.group(1)] = entry
                elif entry.name.startswith("deployment-") and entry.name.endswith(
                    ".json"
                ):
                    raise ValueError("deployment manifest name is invalid")

        if set(archives) != set(acknowledgments):
            raise ValueError("an archived journal lacks its acknowledgment")

        bound_manifests: set[str] = set()
        for journal_sha, journal_path in archives.items():
            journal, actual_journal_sha = cls._secure_json_with_hash(
                journal_path, owner=owner, mode=0o600
            )
            if actual_journal_sha != journal_sha:
                raise ValueError("archived journal hash mismatch")
            phase = journal.get("phase")
            if phase == "manual_recovery_required":
                if (
                    journal.get("fail_closed_verified") is not True
                    or journal.get("fail_closed_error") is not None
                ):
                    raise ValueError("archived incident was not verified fail-closed")
            elif phase != "complete":
                raise ValueError("archived deployment is unresolved")

            release_sha = journal.get("approved_release_sha")
            previous_sha = journal.get("previous_release_sha")
            if (
                not isinstance(release_sha, str)
                or re.fullmatch(r"[0-9a-f]{40}", release_sha) is None
                or not isinstance(previous_sha, str)
                or re.fullmatch(r"[0-9a-f]{40}", previous_sha) is None
                or release_sha in bound_manifests
            ):
                raise ValueError("archived deployment identity is invalid")
            manifest_path = audit / f"deployment-{release_sha}.json"
            if (
                journal.get("manifest") != str(manifest_path)
                or manifests.get(release_sha) != manifest_path
            ):
                raise ValueError("deployment manifest is missing or mismatched")
            manifest, manifest_sha = cls._secure_json_with_hash(
                manifest_path, owner=owner, mode=0o600
            )
            manifest_keys = {
                "approved_release_sha",
                "previous_release_sha",
                "database_backup",
                "database_backup_sha256",
                "shared_preflight_backup",
                "shared_preflight_sha256",
                "created_at_utc",
            }
            if (
                set(manifest) != manifest_keys
                or manifest.get("approved_release_sha") != release_sha
                or manifest.get("previous_release_sha") != previous_sha
                or journal.get("database_backup") != manifest.get("database_backup")
                or journal.get("database_backup_sha256")
                != manifest.get("database_backup_sha256")
                or journal.get("shared_preflight_backup")
                != manifest.get("shared_preflight_backup")
                or journal.get("shared_preflight_sha256")
                != manifest.get("shared_preflight_sha256")
            ):
                raise ValueError("deployment manifest contents do not match journal")

            ack, _ = cls._secure_json_with_hash(
                acknowledgments[journal_sha], owner=owner, mode=0o600
            )
            ack_keys = {
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
            expected_decision = {
                "manual_recovery_required": "manual_recovery_verified",
                "complete": "deployment_completion_verified",
            }[phase]
            if (
                set(ack) != ack_keys
                or type(ack.get("schema_version")) is not int
                or ack.get("schema_version") != 1
                or ack.get("journal_sha256") != journal_sha
                or ack.get("manifest_sha256") != manifest_sha
                or ack.get("approved_release_sha") != release_sha
                or ack.get("previous_release_sha") != previous_sha
                or ack.get("decision") != expected_decision
                or not isinstance(ack.get("approved_by"), str)
                or not ack["approved_by"].strip()
                or len(ack["approved_by"]) > 128
                or not isinstance(ack.get("approval_reference"), str)
                or re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", ack["approval_reference"]
                )
                is None
                or not isinstance(ack.get("approved_at_utc"), str)
            ):
                raise ValueError("deployment acknowledgment is invalid")
            try:
                approved_at = datetime.fromisoformat(ack["approved_at_utc"])
            except ValueError as exc:
                raise ValueError("acknowledgment time is invalid") from exc
            if approved_at.tzinfo is None or approved_at.utcoffset() != UTC.utcoffset(
                approved_at
            ):
                raise ValueError("acknowledgment time is not UTC")
            bound_manifests.add(release_sha)

        if set(manifests) != bound_manifests:
            raise ValueError("a deployment manifest has no acknowledged journal")
        return len(archives)

    def incident_journal(self) -> None:
        active = AUDIT / "deployment-state.json"
        temporary = AUDIT / ".deployment-state.json.tmp"
        incident_manifest = AUDIT / f"deployment-{FAILED_V1_RELEASE}.json"
        try:
            try:
                AUDIT.lstat()
            except FileNotFoundError:
                self.add(
                    "deployment_recovery_ack",
                    "FAIL",
                    "audit directory is missing; prior incident history "
                    "cannot be verified",
                )
                self.add(
                    "failed_v1_1_4_manifest",
                    "UNKNOWN",
                    "audit directory is missing; prior manifest cannot be checked",
                )
                return
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
                archived_count = self._verify_archived_deployments(AUDIT, owner=0)
                self.add(
                    "deployment_recovery_ack",
                    "PASS",
                    "no previous deployment evidence exists"
                    if archived_count == 0
                    else f"{archived_count} archived deployment(s) have "
                    "valid hash-bound approvals",
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
        if not self._trusted_execution_source():
            status = "FAIL" if os.geteuid() == 0 else "UNKNOWN"
            self.add(
                "preflight_execution_source",
                status,
                "run only from a reviewed root-owned, non-group/world-writable "
                "copy; developer-owned paths are refused",
            )
            for name in (
                "candidate_git",
                "python_and_architecture",
                "service_account_and_group",
                "deployment_pin",
                "production_checkout",
                "candidate_checkout",
                "systemd_states",
                "current_release",
                "installed_release_permissions",
                "protected_files",
                "installed_helper_hashes",
                "v2_target_release",
                "production_database",
                "latest_backup",
                "deployment_recovery_ack",
                "failed_v1_1_4_manifest",
            ):
                self.add(
                    name,
                    "UNKNOWN",
                    "not inspected because execution source is not trusted",
                )
            return self.checks

        self.add(
            "preflight_execution_source",
            "PASS",
            "executing from a reviewed root-owned, non-group/world-writable copy",
        )
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
        required=True,
        help="root-owned, read-only candidate checkout to inspect",
    )
    parser.add_argument(
        "--expected-candidate-sha",
        required=True,
        help="reviewed full 40-character SHA expected for the v2.0.0 candidate",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.expected_candidate_sha):
        parser.error("--expected-candidate-sha must be a full lowercase SHA-1")
    checks = Preflight(args.candidate.absolute(), args.expected_candidate_sha).run()
    grouped: dict[str, list[tuple[str, Result]]] = {}
    for name, result in checks.items():
        grouped.setdefault(result.group, []).append((name, result))
    for group, results in grouped.items():
        print(f"\n{group}")
        for name, result in results:
            print(f"{result.status:7} {name}: {result.detail}")
    counts = {
        status: sum(result.status == status for result in checks.values())
        for status in ("PASS", "FAIL", "UNKNOWN")
    }
    print("\nSummary: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    return 1 if counts["FAIL"] else 2 if counts["UNKNOWN"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
