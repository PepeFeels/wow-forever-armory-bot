#!/usr/bin/python3.14 -I
"""Root-owned Armory deployment wrapper using a protected release pin.

Do not install or run this source until the release, installer hash, sudo rule,
and maintenance procedure have each been reviewed and approved.
"""

from __future__ import annotations

import fcntl
import grp
import hashlib
import json
import os
import pwd
import re
import secrets
import signal
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HOSTNAME = "nololinux-server"
CALLER = "codex-dev"
WRAPPER_PATH = "/usr/local/sbin/armory-deploy-v2"
BRANCH = "release/v2.0.0"
BASE_CHECKPOINT_SHA = "a1a6a2ad95302f7e3c4900f18e91691f43631e18"
DEPLOYMENT_PIN = Path("/etc/wow-forever-armory-bot/deployment-pin.json")
RELEASE_SHA = "0" * 40
PREVIOUS_SHA = "0" * 40
BASE = Path("/opt/wow-forever-armory-bot")
REPO = BASE / "deployment-checkout"
CURRENT = BASE / "current"
RELEASES = BASE / "releases"
DB = Path("/var/lib/wow-forever-armory-bot/guild-armory.sqlite3")
BACKUPS = Path("/var/lib/wow-forever-armory-bot/backups/active")
AUDIT_DIR = Path("/var/lib/armory-deployment-audit")
JOURNAL_PATH = AUDIT_DIR / "deployment-state.json"
JOURNAL_TEMP_PATH = AUDIT_DIR / ".deployment-state.json.tmp"
MANIFEST_PATH = AUDIT_DIR / f"deployment-{RELEASE_SHA}.json"
CONFIG = Path("/etc/wow-forever-armory-bot/config")
CREDENTIAL = Path("/etc/wow-forever-armory-bot/discord-token")
PREFLIGHT = BASE / "bin/armory-db-preflight"
INSTALLER = BASE / "bin/production_install.py"
INSTALLER_SHA256 = "0" * 64
DEPLOYMENT_PIN_SHA256 = "0" * 64
RECOVERY_AUDIT_SHA256 = (
    "ef05cc4bda55136ac51e9d2b548f61e863aa2f54d9da7d049d4baee3e5fbb927"
)
PYTHON = Path("/usr/bin/python3.14")
GIT = Path("/usr/bin/git")
SYSTEMCTL = Path("/usr/bin/systemctl")
SERVICE = "wow-forever-armory-bot.service"
BACKUP_TIMER = "wow-forever-armory-backup.timer"
BACKUP_SERVICE = "wow-forever-armory-backup.service"
PREVIOUS_BACKUP_UNIT_SHA256 = "e5c4497e36a31c93e4418472e390db9e45e3590a726df6f10b7ef41a3eb72b70"
RELEASE_BACKUP_UNIT_SHA256 = "4d2358635efbc02394ed99f7cbaf84b8a1620188399aee0b3185bcb23bfe97b0"
UNITS = (SERVICE, BACKUP_TIMER, BACKUP_SERVICE)
MANAGED_UNITS = (SERVICE, BACKUP_TIMER)
SECURE_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
CHILD_ENV = {
    "HOME": "/root",
    "LANG": "C.UTF-8",
    "PATH": SECURE_PATH,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_TERMINAL_PROMPT": "0",
    # The installer creates a venv as root. Keep pip's cache/config out of
    # root's home and prevent user/system pip config from changing behavior.
    "PIP_CONFIG_FILE": "/dev/null",
    "PIP_NO_CACHE_DIR": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "TMPDIR": "/tmp",
}
ALLOWED_LOCAL_GIT_CONFIG_KEYS = frozenset(
    {
        "core.repositoryformatversion",
        "core.filemode",
        "core.bare",
        "core.logallrefupdates",
        "core.hookspath",
        "core.fsmonitor",
        "core.symlinks",
        "credential.helper",
        "remote.origin.url",
        "remote.origin.fetch",
        f"branch.{BRANCH}.remote",
        f"branch.{BRANCH}.merge",
    }
)
DANGEROUS_ENV_PREFIXES = ("PYTHON", "LD_", "DYLD_")
DANGEROUS_ENV_NAMES = {"BASH_ENV", "ENV", "PERL5OPT", "RUBYOPT", "NODE_OPTIONS"}
# Review inventory of fixed application paths the approved installer/wrapper
# can mutate. The existing installer also uses private, random /tmp workdirs.
AUTHORIZED_WRITE_PATHS = (
    BASE,
    RELEASES,
    BASE / "bin",
    RELEASES / RELEASE_SHA,
    CURRENT,
    BASE / ".current.installing",
    BASE / "bin/.armory-db-preflight.installing",
    PREFLIGHT,
    BACKUPS,
    AUDIT_DIR,
    Path("/etc/systemd/system/wow-forever-armory-backup.service"),
)


def read_deployment_pin(
    path: Path = DEPLOYMENT_PIN, *, expected_owner: int = 0
) -> dict[str, str]:
    """Read release pins from a root-owned, private, non-symlink policy file."""
    return _read_deployment_pin_with_hash(path, expected_owner=expected_owner)[0]


def _read_deployment_pin_with_hash(
    path: Path = DEPLOYMENT_PIN, *, expected_owner: int = 0
) -> tuple[dict[str, str], str]:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise Refusal("the deployment release pin is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != expected_owner
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 4096
        ):
            raise Refusal("the deployment release pin has unsafe metadata")
        data = os.read(descriptor, 4097)
        if len(data) > 4096:
            raise Refusal("the deployment release pin is too large")
    finally:
        os.close(descriptor)

    def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        pin = json.loads(data.decode("ascii"), object_pairs_hook=unique_keys)
    except (UnicodeError, ValueError) as exc:
        raise Refusal("the deployment release pin is invalid") from exc
    if (
        not isinstance(pin, dict)
        or set(pin) != {"release_sha", "previous_sha", "installer_sha256"}
        or not all(isinstance(value, str) for value in pin.values())
        or not re.fullmatch(r"[0-9a-f]{40}", pin["release_sha"])
        or not re.fullmatch(r"[0-9a-f]{40}", pin["previous_sha"])
        or pin["release_sha"] == pin["previous_sha"]
        or not re.fullmatch(r"[0-9a-f]{64}", pin["installer_sha256"])
    ):
        raise Refusal("the deployment release pin is invalid")
    return pin, hashlib.sha256(data).hexdigest()


def apply_deployment_pin(
    pin: Mapping[str, str], *, pin_sha256: str | None = None
) -> None:
    """Set runtime pins only after the protected policy file is validated."""
    global RELEASE_SHA, PREVIOUS_SHA, INSTALLER_SHA256, MANIFEST_PATH
    global DEPLOYMENT_PIN_SHA256
    global AUTHORIZED_WRITE_PATHS
    RELEASE_SHA = pin["release_sha"]
    PREVIOUS_SHA = pin["previous_sha"]
    INSTALLER_SHA256 = pin["installer_sha256"]
    if pin_sha256 is not None:
        DEPLOYMENT_PIN_SHA256 = pin_sha256
    MANIFEST_PATH = AUDIT_DIR / f"deployment-{RELEASE_SHA}.json"
    AUTHORIZED_WRITE_PATHS = (
        BASE,
        RELEASES,
        BASE / "bin",
        RELEASES / RELEASE_SHA,
        CURRENT,
        BASE / ".current.installing",
        BASE / "bin/.armory-db-preflight.installing",
        PREFLIGHT,
        BACKUPS,
        AUDIT_DIR,
        Path("/etc/systemd/system/wow-forever-armory-backup.service"),
    )


class Refusal(RuntimeError):
    """A fixed, non-sensitive reason to stop before an unsafe action."""


class DatabaseCheckFailure(Refusal):
    """Allowlisted diagnostic for one failed operations check."""

    def __init__(self, check_id: str, exit_code: int | None, error_class: str):
        self.check_id = check_id
        self.exit_code = exit_code
        self.error_class = error_class
        super().__init__("the Armory database operation failed")


INSTALLER_DIAGNOSTIC_CATEGORIES = frozenset(
    {
        "installer_rejected",
        "input_rejected",
        "command_failed",
        "unit_state_rejected",
        "process_state_rejected",
        "database_snapshot_rejected",
        "python_runtime_rejected",
        "repository_rejected",
        "service_account_rejected",
        "release_permissions_rejected",
        "release_marker_rejected",
        "release_build_failed",
        "current_pointer_rejected",
    }
)
INSTALLER_OUTCOMES = frozenset(
    {
        "timeout",
        "startup_failure",
        "installer_rejected",
        "process_failure",
        "protocol_failure",
    }
)


class InstallerExecutionFailure(Refusal):
    """Fixed installer outcome metadata; never retains subprocess output."""

    def __init__(
        self, outcome: str, exit_code: int | None = None, category: str | None = None
    ):
        self.outcome = outcome if outcome in INSTALLER_OUTCOMES else "process_failure"
        # subprocess uses negative return codes when a child dies from a signal.
        self.exit_code = (
            exit_code if type(exit_code) is int and -255 <= exit_code <= 255 else None
        )
        self.category = (
            category if category in INSTALLER_DIAGNOSTIC_CATEGORIES else None
        )
        super().__init__("the approved installer did not complete successfully")


def _run_installer(command: Sequence[str], *, timeout: int = 3600) -> int:
    """Run installer and retain only fixed category, outcome, and exit status."""
    try:
        diagnostic_read_fd, diagnostic_write_fd = os.pipe()
    except OSError as exc:
        raise InstallerExecutionFailure("protocol_failure") from exc
    child_env = {**CHILD_ENV, "ARMORY_INSTALL_DIAGNOSTIC_FD": str(diagnostic_write_fd)}
    try:
        result = subprocess.run(
            list(command),
            shell=False,
            check=False,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
            timeout=timeout,
            pass_fds=(diagnostic_write_fd,),
        )
    except subprocess.TimeoutExpired as exc:
        os.close(diagnostic_read_fd)
        raise InstallerExecutionFailure("timeout") from exc
    except OSError as exc:
        os.close(diagnostic_read_fd)
        raise InstallerExecutionFailure("startup_failure") from exc
    finally:
        os.close(diagnostic_write_fd)
    try:
        diagnostic = os.read(diagnostic_read_fd, 128)
    except OSError as exc:
        raise InstallerExecutionFailure("protocol_failure", result.returncode) from exc
    finally:
        os.close(diagnostic_read_fd)
    category = next(
        (
            allowed
            for allowed in INSTALLER_DIAGNOSTIC_CATEGORIES
            if diagnostic == f"{allowed}\n".encode("ascii")
        ),
        None,
    )
    if result.returncode != 0:
        outcome = "installer_rejected" if category else "process_failure"
        raise InstallerExecutionFailure(outcome, result.returncode, category)
    return result.returncode


CHECK_IDS = frozenset(
    {
        "backup_source_before_copy",
        "published_backup_after_copy",
        "production_database_after_lock_handoff",
        "backup_after_lock_handoff",
        "preflight_before_timer_pause",
        "installed_release_database",
    }
)


def _safe_database_error(stderr: bytes) -> str:
    """Map known fixed CLI messages to labels; never return subprocess text."""
    safe = stderr.decode("utf-8", errors="replace")
    if "database or backup file does not exist" in safe:
        return "database_file_missing"
    if "could not open SQLite file" in safe:
        return "sqlite_open_failed"
    if (
        "SQLite integrity check failed" in safe
        or "new SQLite copy failed integrity check" in safe
    ):
        return "integrity_check_failed"
    if "database path must be configured" in safe:
        return "database_path_unconfigured"
    return "unclassified_operation_error"


def validate_arguments(argv: Sequence[str]) -> None:
    if len(argv) != 1:
        raise Refusal("this deployment command accepts no arguments")


def validate_environment(environ: Mapping[str, str]) -> None:
    """Check sudo identity and reject interpreter/loader injection variables.

    Ubuntu sudoers normally provides HOME, MAIL, SHELL, LOGNAME, USER, SUDO_*
    and TERM under env_reset; secure_path may replace PATH. PAM and local
    env_keep settings can add harmless values, so the wrapper does not require
    one exact environment dictionary. Python runs with -I, subprocesses receive
    CHILD_ENV, and known code-loading variables are refused.
    """
    if {"SUDO_USER", "SUDO_UID", "SUDO_GID"} - set(environ):
        raise Refusal("required sudo identity variables are missing")
    if environ["SUDO_USER"] != CALLER:
        raise Refusal("only the approved deployment caller may use this command")
    if any(
        key.startswith(DANGEROUS_ENV_PREFIXES) or key in DANGEROUS_ENV_NAMES
        for key in environ
    ):
        raise Refusal("a code-loading environment variable is present")
    try:
        caller = pwd.getpwnam(CALLER)
        caller_uid = int(environ["SUDO_UID"])
        caller_gid = int(environ["SUDO_GID"])
    except (KeyError, ValueError) as exc:
        raise Refusal("the invoking account could not be verified") from exc
    if caller_uid != caller.pw_uid or caller_gid != caller.pw_gid:
        raise Refusal("the invoking account could not be verified")


def validate_host(hostname: str) -> None:
    if hostname != HOSTNAME:
        raise Refusal("this command is restricted to nololinux-server")


def validate_source_identity(branch: str, head: str, status: str) -> None:
    if branch != BRANCH or head != RELEASE_SHA or status:
        raise Refusal("the protected release checkout is not the approved clean source")


def validate_current_release(target: Path, marker_text: str) -> None:
    expected = RELEASES / PREVIOUS_SHA
    if target != expected or marker_text != PREVIOUS_SHA:
        raise Refusal("the installed release is not the approved previous release")


def _managed_current_target(link: Path, *, owner_uid: int = 0) -> Path:
    info = link.lstat()
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != owner_uid:
        raise Refusal("the current release pointer has unsafe metadata")
    raw_target = os.readlink(link)
    relative = re.fullmatch(r"releases/([0-9a-f]{40})", raw_target)
    absolute = re.fullmatch(
        re.escape(str(BASE / "releases")) + r"/([0-9a-f]{40})", raw_target
    )
    match = relative or absolute
    if match is None:
        raise Refusal("the current release pointer is not canonical")
    expected = BASE / "releases" / match.group(1)
    if link.resolve(strict=True) != expected:
        raise Refusal("the current release pointer resolves unexpectedly")
    return expected


def backup_retention(existing_count: int) -> int:
    if existing_count < 0:
        raise Refusal("backup inventory is invalid")
    # operations.backup keeps at most this count. This value prevents this run
    # from pruning any existing verified backup, including when history > 14.
    return max(14, existing_count + 1)


def installer_command() -> list[str]:
    return [
        str(PYTHON),
        str(INSTALLER),
        "--release-sha",
        RELEASE_SHA,
        "--upgrade-existing",
    ]


def _run(
    argv: Sequence[str],
    *,
    capture: bool = False,
    user: int | None = None,
    group: int | None = None,
    allowed_codes: tuple[int, ...] = (0,),
    timeout: int = 60,
) -> str:
    if not argv or not Path(argv[0]).is_absolute():
        raise Refusal("an internal command path was not absolute")
    try:
        result = subprocess.run(
            list(argv),
            shell=False,
            check=False,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=CHILD_ENV,
            user=user,
            group=group,
            extra_groups=() if user is not None else None,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal(
            "a fixed deployment check or operation could not complete"
        ) from exc
    if result.returncode not in allowed_codes:
        raise Refusal("a fixed deployment check or operation failed")
    return result.stdout.decode("utf-8", errors="strict").strip() if capture else ""


def _git(*args: str, capture: bool = False) -> str:
    """Run only fixed Git queries with external helpers and replacements disabled."""
    return _run(
        (
            str(GIT),
            "-c",
            f"safe.directory={REPO}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "credential.helper=",
            "-c",
            "core.symlinks=false",
            "-c",
            "core.bare=false",
            "-C",
            str(REPO),
            *args,
        ),
        capture=capture,
    )


def _check_git_config() -> None:
    """Reject unreviewed local Git settings before worktree queries run."""
    raw_keys = _git(
        "config",
        "--local",
        "--no-includes",
        "--name-only",
        "--list",
        capture=True,
    )
    keys = {line.strip().lower() for line in raw_keys.splitlines() if line.strip()}
    if not keys.issubset(ALLOWED_LOCAL_GIT_CONFIG_KEYS):
        raise Refusal("the protected checkout has unapproved local Git settings")


def _systemctl_state(action: str, unit: str) -> str:
    if unit not in UNITS or action not in {"is-active", "is-enabled"}:
        raise Refusal("an unapproved systemd unit or query was requested")
    output = _run(
        (str(SYSTEMCTL), action, unit),
        capture=True,
        allowed_codes=(0, 1, 3, 4),
    )
    if output not in {
        "active",
        "inactive",
        "failed",
        "enabled",
        "disabled",
        "static",
        "masked",
        "not-found",
    }:
        raise Refusal("systemd returned an unexpected unit state")
    return output


def validate_unit_states(states: Mapping[str, tuple[str, str]]) -> None:
    if set(states) != set(UNITS):
        raise Refusal("the systemd unit inventory is incomplete or unexpected")
    safe_initial_states = {("active", "enabled"), ("inactive", "disabled")}
    if states[SERVICE] not in safe_initial_states:
        raise Refusal("bot service is not in an approved initial state")
    if states[BACKUP_TIMER] not in safe_initial_states:
        raise Refusal("backup timer is not in an approved initial state")
    if states[SERVICE] != states[BACKUP_TIMER]:
        raise Refusal("bot service and backup timer initial states do not match")
    if states[BACKUP_SERVICE][0] != "inactive":
        raise Refusal("the scheduled backup job is currently running")
    if states[BACKUP_SERVICE][1] not in {"static", "disabled"}:
        raise Refusal("the backup job unit has an unexpected enablement state")


def _checked_unit_state() -> dict[str, tuple[str, str]]:
    states = {
        unit: (
            _systemctl_state("is-active", unit),
            _systemctl_state("is-enabled", unit),
        )
        for unit in UNITS
    }
    validate_unit_states(states)
    return states


def _has_python_bytecode_cache(path: Path) -> bool:
    def onerror(error: OSError) -> None:
        raise error

    try:
        for directory, names, files in os.walk(
            path, followlinks=False, onerror=onerror
        ):
            if Path(directory).name == "__pycache__":
                return True
            if any(name == "__pycache__" for name in names) or any(
                Path(name).suffix in {".pyc", ".pyo"} for name in files
            ):
                return True
        return False
    except OSError as exc:
        raise Refusal("the trusted checkout could not be fully inspected") from exc


def _read_audit_json(path: Path, *, owner_uid: int) -> tuple[dict[str, Any], str]:
    try:
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise Refusal("an audit record is unavailable") from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != owner_uid
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_size > 65536
        ):
            raise Refusal("an audit record has unsafe metadata")
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
            raise Refusal("an audit record changed while it was being read")
        raw = bytes(content)
    except OSError as exc:
        raise Refusal("an audit record could not be read safely") from exc
    finally:
        os.close(fd)

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=unique)
    except (UnicodeError, ValueError) as exc:
        raise Refusal("an audit record is malformed") from exc
    if not isinstance(value, dict):
        raise Refusal("an audit record is malformed")
    return value, hashlib.sha256(raw).hexdigest()


def _verify_archived_deployments(
    directory: Path, *, owner_uid: int, lock_held: bool = False
) -> None:
    """Delegate incident validation to the shared reviewed source module."""
    source = REPO / "deploy/recovery_audit.py"
    if not source.exists():
        source = Path(__file__).with_name("recovery_audit.py")
    try:
        fd = os.open(
            source,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise Refusal("shared recovery validator is unavailable") from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != owner_uid
            # Staging strips write bits, while a checkout copied without that
            # step preserves Git's 0644 mode. Both are safe here: root owns the
            # single-link file and its bytes are pinned before compilation.
            or stat.S_IMODE(info.st_mode) not in {0o444, 0o644}
            or info.st_nlink != 1
        ):
            raise Refusal("shared recovery validator metadata is unsafe")
        chunks = []
        while True:
            block = os.read(fd, 65536)
            if not block:
                break
            chunks.append(block)
        data = b"".join(chunks)
        if hashlib.sha256(data).hexdigest() != RECOVERY_AUDIT_SHA256:
            raise Refusal("shared recovery validator hash mismatch")
        after = os.fstat(fd)
        if (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise Refusal("shared recovery validator changed while reading")
    finally:
        os.close(fd)
    namespace = {"__file__": str(source), "__name__": "_armory_recovery_audit"}
    exec(compile(data, str(source), "exec", dont_inherit=True), namespace)
    try:
        namespace["verify_audit_directory"](
            directory,
            owner=owner_uid,
            group=os.lstat(directory).st_gid,
            acquire_lock=not lock_held,
            preserved_historical_directories=("reviewed-v2.0.0", "archive"),
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise Refusal("deployment audit history is unsafe or unresolved") from exc


def _assert_root_owned_tree(
    path: Path, *, owner_uid: int = 0, check_parents: bool = True
) -> None:
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise Refusal("the trusted checkout path is not canonical")
    if _has_python_bytecode_cache(path):
        raise Refusal("the trusted checkout contains Python bytecode caches")
    for parent in path.parents if check_parents else ():
        info = parent.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or info.st_uid != owner_uid
            or info.st_mode & 0o022
        ):
            raise Refusal(
                "a trusted checkout parent is writable by an untrusted account"
            )

    def onerror(error: OSError) -> None:
        raise error

    try:
        for current, directories, files in os.walk(
            path, followlinks=False, onerror=onerror
        ):
            current_path = Path(current)
            for item in (
                current_path,
                *(current_path / name for name in directories),
                *(current_path / name for name in files),
            ):
                info = item.lstat()
                if item.is_symlink():
                    raise Refusal("the trusted checkout contains a symbolic link")
                relative_path = item.relative_to(path)
                is_directory = stat.S_ISDIR(info.st_mode)
                is_recovery_validator = (
                    relative_path == Path("deploy/recovery_audit.py")
                )
                # Root is the trusted administrator. Root-only directory write
                # permission (for example 0700) is acceptable, but group/other
                # writes are not. Regular files stay immutable except for the
                # exact hash-pinned recovery validator checked before use.
                unsafe_write_bits = (
                    bool(info.st_mode & 0o022)
                    if is_directory
                    else (
                        stat.S_IMODE(info.st_mode) not in {0o444, 0o644}
                        if is_recovery_validator
                        else bool(info.st_mode & 0o222)
                    )
                )
                if (
                    stat.S_ISLNK(info.st_mode)
                    or info.st_uid != owner_uid
                    or unsafe_write_bits
                    or (
                        stat.S_ISREG(info.st_mode)
                        and info.st_nlink != 1
                    )
                ):
                    raise Refusal(
                        "the trusted checkout has unsafe ownership or untrusted write permissions"
                    )
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise Refusal("the trusted checkout contains a special file")
    except OSError as exc:
        raise Refusal("the trusted checkout could not be fully inspected") from exc
    if not (path / ".git").is_dir() or (path / ".git").is_symlink():
        raise Refusal("the trusted checkout must be a full Git clone")


def _check_checkout() -> None:
    _assert_root_owned_tree(REPO)
    _check_git_config()
    top = Path(
        _git("rev-parse", "--show-toplevel", capture=True)
    )
    branch = _git("branch", "--show-current", capture=True)
    head = _git("rev-parse", "HEAD", capture=True)
    ref = _git("rev-parse", f"refs/heads/{BRANCH}", capture=True)
    status = _git("status", "--porcelain", "--untracked-files=all", capture=True)
    if top != REPO or ref != RELEASE_SHA:
        raise Refusal("the protected checkout path or release branch ref is unexpected")
    validate_source_identity(branch, head, status)
    _git("merge-base", "--is-ancestor", PREVIOUS_SHA, RELEASE_SHA)
    _git("merge-base", "--is-ancestor", BASE_CHECKPOINT_SHA, RELEASE_SHA)


def _assert_managed_file(
    path: Path, *, owner: int, group: int | None = None, mode: int | None = None
) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise Refusal("a required managed file is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or path.is_symlink():
        raise Refusal("a required managed file has an unexpected type or owner")
    if group is not None and info.st_gid != group:
        raise Refusal("a required managed file has an unexpected group")
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise Refusal("a required managed file has unexpected permissions")
    if info.st_mode & 0o022:
        raise Refusal("a required managed file is writable by an untrusted account")
    return info


def _assert_directory(
    path: Path, *, owner: int, group: int | None = None, mode: int | None = None
) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise Refusal("a required managed directory is unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != owner
        or path.resolve(strict=True) != path
    ):
        raise Refusal("a required managed directory has an unexpected path or owner")
    if group is not None and info.st_gid != group:
        raise Refusal("a required managed directory has an unexpected group")
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise Refusal("a required managed directory has unexpected permissions")
    if info.st_mode & 0o022:
        raise Refusal(
            "a required managed directory is writable by an untrusted account"
        )
    return info


def _verify_previous_backup_unit(installed: Path, candidate: Path) -> None:
    info = _assert_managed_file(installed, owner=0, mode=0o644)
    if (
        info.st_gid != 0
        or info.st_nlink != 1
        or _sha256(installed) != PREVIOUS_BACKUP_UNIT_SHA256
        or _sha256(candidate) != RELEASE_BACKUP_UNIT_SHA256
    ):
        raise Refusal("installed backup unit is not the exact approved previous version")


def _assert_installer() -> None:
    """Require the pinned, root-owned installer before any deployment work."""
    _assert_managed_file(INSTALLER, owner=0, mode=0o755)
    if _sha256(INSTALLER) != INSTALLER_SHA256:
        raise Refusal("the approved installer checksum is unexpected")


def _assert_application_layout(
    config_path: Path = CONFIG,
    *,
    root_uid: int = 0,
    service_gid: int,
) -> os.stat_result:
    """Require the app config to be root-owned and readable by its service group."""
    return _assert_managed_file(
        config_path,
        owner=root_uid,
        group=service_gid,
        mode=0o640,
    )


def _check_paths_and_current() -> tuple[int, int]:
    try:
        service_user = pwd.getpwnam("wow-armory")
        service_group = grp.getgrnam("wow-armory")
    except KeyError as exc:
        raise Refusal("the Armory service account is unavailable") from exc
    if (
        service_user.pw_gid != service_group.gr_gid
        or service_user.pw_dir != "/nonexistent"
    ):
        raise Refusal(
            "the Armory service account does not match the installed isolation"
        )
    _assert_directory(BASE, owner=0, mode=0o755)
    _assert_directory(RELEASES, owner=0, mode=0o755)
    _assert_directory(BASE / "bin", owner=0, mode=0o755)
    _assert_directory(CONFIG.parent, owner=0, group=service_group.gr_gid, mode=0o750)
    _assert_directory(Path("/etc/systemd/system"), owner=0)
    current_target = _managed_current_target(CURRENT)
    marker = current_target / ".installation-complete"
    _assert_managed_file(marker, owner=0)
    validate_current_release(current_target, marker.read_text(encoding="ascii"))
    _assert_application_layout(service_gid=service_group.gr_gid)
    _assert_managed_file(CREDENTIAL, owner=0, mode=0o600)
    _assert_managed_file(PREFLIGHT, owner=0, mode=0o755)
    _assert_installer()
    _assert_managed_file(
        DB, owner=service_user.pw_uid, group=service_group.gr_gid, mode=0o600
    )
    _assert_directory(
        DB.parent, owner=service_user.pw_uid, group=service_group.gr_gid, mode=0o700
    )
    _assert_directory(
        BACKUPS, owner=service_user.pw_uid, group=service_group.gr_gid, mode=0o700
    )
    for unit in (SERVICE, BACKUP_SERVICE, BACKUP_TIMER):
        source = REPO / "deploy/systemd" / unit
        installed = Path("/etc/systemd/system") / unit
        if unit == BACKUP_SERVICE:
            if not source.is_file():
                raise Refusal("trusted release backup unit is unavailable")
            _verify_previous_backup_unit(installed, source)
            continue
        _assert_managed_file(installed, owner=0, mode=0o644)
        if not source.is_file() or source.read_bytes() != installed.read_bytes():
            raise Refusal("installed systemd files differ from the trusted release")
    installed_launcher = BASE / "bin/armory-launch"
    _assert_managed_file(installed_launcher, owner=0, mode=0o755)
    if (
        REPO / "deploy/systemd/armory-launch"
    ).read_bytes() != installed_launcher.read_bytes():
        raise Refusal("the installed Armory launcher differs from the trusted release")
    return service_user.pw_uid, service_group.gr_gid


def _ensure_audit_dir() -> None:
    parent = AUDIT_DIR.parent
    parent_info = parent.lstat()
    if (
        stat.S_ISLNK(parent_info.st_mode)
        or not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != 0
        or parent_info.st_mode & 0o022
    ):
        raise Refusal("the root-owned deployment audit parent is unsafe")
    try:
        AUDIT_DIR.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = AUDIT_DIR.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise Refusal("the root-owned deployment audit directory is unsafe")
    _fsync_directory(parent)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _wait_backup_job_idle(timeout: int = 120) -> None:
    deadline = time.monotonic() + timeout
    while True:
        state = _systemctl_state("is-active", BACKUP_SERVICE)
        if state == "inactive":
            return
        if state != "active" or time.monotonic() >= deadline:
            raise Refusal("the existing backup job did not finish safely")
        time.sleep(1)


def _db_signature() -> tuple[int, ...]:
    info = _assert_managed_file(
        DB,
        owner=pwd.getpwnam("wow-armory").pw_uid,
        group=grp.getgrnam("wow-armory").gr_gid,
        mode=0o600,
    )
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_uid,
        info.st_gid,
        info.st_mode,
    )


def _run_as_armory(
    uid: int,
    gid: int,
    python: Path,
    *args: str,
    timeout: int = 1800,
    check_id: str | None = None,
    cwd: Path | None = None,
) -> None:
    env = {
        **CHILD_ENV,
        "ARMORY_DATABASE_PATH": str(DB),
        "ARMORY_BACKUP_DIR": str(BACKUPS),
        "ARMORY_BACKUP_RETENTION": "14",
    }
    command = (str(python), "-m", "wow_forever_armory.operations", *args)
    try:
        result = subprocess.run(
            command,
            shell=False,
            check=False,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=env,
            user=uid,
            group=gid,
            extra_groups=(),
            timeout=timeout,
            cwd=cwd if cwd is not None else CURRENT.resolve(strict=True),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if args and args[0] == "check" and check_id in CHECK_IDS:
            raise DatabaseCheckFailure(check_id, None, type(exc).__name__) from exc
        raise Refusal("the Armory database operation could not complete") from exc
    if result.returncode != 0:
        if args and args[0] == "check" and check_id in CHECK_IDS:
            raise DatabaseCheckFailure(
                check_id,
                result.returncode,
                _safe_database_error(result.stderr),
            )
        raise Refusal("the Armory database operation failed")


def _backup_files(uid: int, gid: int) -> list[Path]:
    result: list[Path] = []
    for entry in BACKUPS.iterdir():
        if (
            entry.name.startswith(".guild-armory-")
            and ".sqlite3." in entry.name
            and entry.name.endswith(".tmp")
        ):
            raise Refusal(
                "an unpublished backup artifact requires administrator review"
            )
    for path in BACKUPS.glob("guild-armory-*.sqlite3"):
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or path.is_symlink()
            or info.st_uid != uid
            or info.st_gid != gid
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise Refusal("an existing backup has unexpected ownership or permissions")
        result.append(path)
    return result


def _backup_inventory(uid: int, gid: int) -> dict[str, tuple[int, ...]]:
    """Return stable metadata for each published backup, without reading data."""
    inventory: dict[str, tuple[int, ...]] = {}
    for path in _backup_files(uid, gid):
        try:
            info = path.lstat()
        except OSError as exc:
            raise Refusal("the backup inventory changed during inspection") from exc
        inventory[path.name] = (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            info.st_uid,
            info.st_gid,
            info.st_mode,
        )
    return inventory


def _ensure_backup_directory(uid: int, gid: int) -> None:
    """Create the new active area without touching the preserved legacy tree."""
    parent = BACKUPS.parent
    parent_info = parent.lstat()
    if (
        stat.S_ISLNK(parent_info.st_mode)
        or not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != uid
        or parent_info.st_gid != gid
        or stat.S_IMODE(parent_info.st_mode) != 0o700
    ):
        raise Refusal("the preserved backup parent has unsafe metadata")
    try:
        BACKUPS.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = BACKUPS.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != uid
        or info.st_gid != gid
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise Refusal("the active backup directory has unsafe metadata")
    _fsync_directory(parent)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise Refusal("a backup verification file could not be opened safely") from exc
    try:
        with os.fdopen(fd, "rb", closefd=True) as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise Refusal("a backup verification file could not be read safely") from exc
    return digest.hexdigest()


def _preserve_preflight() -> tuple[Path, str]:
    source = REPO / "deploy/systemd/armory-db-preflight.py"
    source_hash = _sha256(source)
    if _sha256(PREFLIGHT) != source_hash:
        raise Refusal("the current and approved shared database preflight differ")
    destination = AUDIT_DIR / f"armory-db-preflight.{PREVIOUS_SHA}.py"
    payload = source.read_bytes()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(destination, flags, 0o700)
    except FileExistsError:
        _assert_managed_file(destination, owner=0, mode=0o700)
        if _sha256(destination) != source_hash:
            raise Refusal(
                "the existing preflight preservation copy has a different checksum"
            ) from None
    except OSError as exc:
        raise Refusal("the shared database preflight could not be preserved") from exc
    else:
        with os.fdopen(fd, "wb", closefd=True) as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.chown(destination, 0, 0)
        os.chmod(destination, 0o700)
        _fsync_directory(AUDIT_DIR)
    return destination, source_hash


def _backup_database(
    uid: int, gid: int
) -> tuple[Path, str, dict[str, tuple[int, ...]]]:
    _ensure_backup_directory(uid, gid)
    before = _backup_inventory(uid, gid)
    _run_as_armory(
        uid,
        gid,
        CURRENT / ".venv/bin/python",
        "check",
        timeout=300,
        check_id="backup_source_before_copy",
    )
    retention = backup_retention(len(before))
    env = {
        **CHILD_ENV,
        "ARMORY_DATABASE_PATH": str(DB),
        "ARMORY_BACKUP_DIR": str(BACKUPS),
        "ARMORY_BACKUP_RETENTION": str(retention),
    }
    try:
        result = subprocess.run(
            (
                str(CURRENT / ".venv/bin/python"),
                "-m",
                "wow_forever_armory.operations",
                "backup",
                "--retention",
                str(retention),
            ),
            shell=False,
            check=False,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=env,
            user=uid,
            group=gid,
            extra_groups=(),
            timeout=1800,
            cwd=CURRENT.resolve(strict=True),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal("the verified online backup could not complete") from exc
    if result.returncode != 0:
        raise Refusal("the verified online backup failed")
    after = _backup_inventory(uid, gid)
    # The app's own command holds both nonblocking locks while copying and
    # pruning. Any deletion/change to an older entry is therefore unexpected.
    if any(after.get(name) != fingerprint for name, fingerprint in before.items()):
        raise Refusal("an existing backup changed during online backup")
    created = sorted(set(after) - set(before))
    if len(created) != 1:
        raise Refusal("the backup inventory changed unexpectedly")
    backup = BACKUPS / created[0]
    if not backup.is_absolute():
        raise Refusal("the published backup path is not absolute")
    _run_as_armory(
        uid,
        gid,
        CURRENT / ".venv/bin/python",
        "check",
        "--database",
        str(backup),
        timeout=300,
        check_id="published_backup_after_copy",
    )
    return backup, _sha256(backup), after


def _recheck_backup_exclusive(
    uid: int,
    gid: int,
    backup: Path,
    backup_hash: str,
    expected_inventory: Mapping[str, tuple[int, ...]],
) -> None:
    """After lock handoff, reject any activity in the handoff window."""
    if _systemctl_state("is-active", BACKUP_SERVICE) != "inactive":
        raise Refusal("the backup service became active during lock handoff")
    current_inventory = _backup_inventory(uid, gid)
    if current_inventory != dict(expected_inventory):
        raise Refusal("backup inventory changed before exclusive deployment access")
    _assert_managed_file(backup, owner=uid, group=gid, mode=0o600)
    if _sha256(backup) != backup_hash:
        raise Refusal("the verified backup changed during lock handoff")
    _run_as_armory(
        uid,
        gid,
        CURRENT / ".venv/bin/python",
        "check",
        timeout=300,
        check_id="production_database_after_lock_handoff",
    )
    _run_as_armory(
        uid,
        gid,
        CURRENT / ".venv/bin/python",
        "check",
        "--database",
        str(backup),
        timeout=300,
        check_id="backup_after_lock_handoff",
    )


def _acquire_backup_locks(
    uid: int,
    gid: int,
    *,
    lock_paths: tuple[Path, Path] | None = None,
) -> tuple[int, int]:
    """Hold the application's two nonblocking backup locks through install.

    The normal backup command creates these service-owned files. Refuse if
    either is missing or replaced; never create a root-owned lock the service
    could not later reopen. The app uses the same database-then-directory order.
    """
    paths = (
        lock_paths
        if lock_paths is not None
        else (Path(f"{DB.resolve()}.backup.lock"), BACKUPS / ".backup.lock")
    )
    descriptors: list[int] = []
    try:
        for path in paths:
            info = _assert_managed_file(path, owner=uid, group=gid, mode=0o600)
            flags = (
                os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            )
            fd = os.open(path, flags)
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                info.st_dev,
                info.st_ino,
            ):
                os.close(fd)
                raise Refusal("a backup lock changed while being opened")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as exc:
                os.close(fd)
                raise Refusal(
                    "another database backup or restore is in progress"
                ) from exc
            descriptors.append(fd)
        return (descriptors[0], descriptors[1])
    except BaseException:
        for fd in descriptors:
            os.close(fd)
        raise


def _release_backup_locks(descriptors: tuple[int, int] | None) -> None:
    if descriptors is None:
        return
    for fd in reversed(descriptors):
        os.close(fd)


def _service_action(action: str, unit: str) -> None:
    if unit not in MANAGED_UNITS or action not in {
        "stop",
        "start",
        "disable",
        "enable",
    }:
        raise Refusal("an unapproved systemd action was requested")
    _run((str(SYSTEMCTL), action, unit), timeout=120)


def _set_unit_state(unit: str, state: tuple[str, str]) -> None:
    active, enabled = state
    _service_action("enable" if enabled == "enabled" else "disable", unit)
    _service_action("start" if active == "active" else "stop", unit)
    if (
        _systemctl_state("is-active", unit) != active
        or _systemctl_state("is-enabled", unit) != enabled
    ):
        raise Refusal("the original Armory unit state could not be restored")


def _stop_and_disable() -> None:
    for unit in (BACKUP_TIMER, SERVICE):
        _service_action("disable", unit)
        _service_action("stop", unit)
    if (
        _systemctl_state("is-active", SERVICE) != "inactive"
        or _systemctl_state("is-enabled", SERVICE) != "disabled"
    ):
        raise Refusal("the bot service did not reach the installer-required state")
    if (
        _systemctl_state("is-active", BACKUP_TIMER) != "inactive"
        or _systemctl_state("is-enabled", BACKUP_TIMER) != "disabled"
    ):
        raise Refusal("the backup timer did not reach the installer-required state")
    if _systemctl_state("is-active", BACKUP_SERVICE) != "inactive":
        raise Refusal("the backup job is still active")


def _observe_unit_states() -> dict[str, tuple[str, str]]:
    return {
        unit: (
            _systemctl_state("is-active", unit),
            _systemctl_state("is-enabled", unit),
        )
        for unit in UNITS
    }


def _armory_process_present(proc_root: Path = Path("/proc")) -> bool:
    """Fail closed if a bot process remains outside the managed systemd units."""
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return True
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            arguments = [
                value.decode(errors="replace")
                for value in (entry / "cmdline").read_bytes().split(b"\0")
                if value
            ]
        except FileNotFoundError:
            continue
        except OSError:
            return True
        if (
            len(arguments) >= 3
            and arguments[1:3] == ["-m", "wow_forever_armory.discord_bot"]
        ) or any(Path(argument).name == "discord_bot.py" for argument in arguments):
            return True
    return False


def _restore_states(states: dict[str, tuple[str, str]]) -> dict[str, tuple[str, str]]:
    for unit in (SERVICE, BACKUP_TIMER):
        _set_unit_state(unit, states[unit])
    observed = _observe_unit_states()
    if observed != states:
        raise Refusal("the complete original Armory unit state was not restored")
    return observed


def _fail_closed() -> dict[str, tuple[str, str]]:
    # Only the two exact Armory units are touched. Do not restart automatically
    # after a partial install: an administrator must inspect the release pointer.
    for unit in (BACKUP_TIMER, SERVICE):
        for action in ("stop", "disable"):
            try:
                _service_action(action, unit)
            except Refusal:
                pass
    observed = _observe_unit_states()
    if any(observed[unit] != ("inactive", "disabled") for unit in MANAGED_UNITS):
        raise Refusal(
            "one or more managed units could not be left stopped and disabled"
        )
    if observed[BACKUP_SERVICE][0] != "inactive":
        raise Refusal("the backup service is active during fail-closed cleanup")
    if _armory_process_present():
        raise Refusal("an Armory bot process remains after fail-closed cleanup")
    return observed


class DeploymentJournal:
    """Root-owned durable phase record; any remaining journal requires review."""

    def __init__(self, directory: Path = AUDIT_DIR, *, owner_uid: int = 0):
        self.directory = directory
        self.path = directory / "deployment-state.json"
        self.temporary = directory / ".deployment-state.json.tmp"
        self.owner_uid = owner_uid
        self.lock_fd: int | None = None

    def acquire_exclusive_lock(self) -> None:
        """Coordinate deployment journal changes with startup and recovery checks."""
        try:
            before = self.directory.lstat()
            if (
                not stat.S_ISDIR(before.st_mode)
                or before.st_uid != self.owner_uid
                or stat.S_IMODE(before.st_mode) != 0o700
            ):
                raise Refusal("deployment audit directory metadata is unsafe")
            fd = os.open(
                self.directory,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError as exc:
            raise Refusal("deployment audit directory cannot be locked") from exc
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or opened.st_uid != self.owner_uid
                or stat.S_IMODE(opened.st_mode) != 0o700
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise Refusal("deployment audit directory changed while locking")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise Refusal(
                    "another recovery or deployment operation is active"
                ) from exc
            self.lock_fd = fd
        except BaseException:
            os.close(fd)
            raise

    def release_lock(self) -> None:
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    def assert_no_pending(self) -> None:
        try:
            self.directory.lstat()
        except FileNotFoundError as exc:
            raise Refusal("deployment audit directory is missing") from exc
        except OSError as exc:
            raise Refusal("deployment audit history is unavailable") from exc
        _assert_directory(self.directory, owner=self.owner_uid, mode=0o700)
        if (
            self.path.exists()
            or self.path.is_symlink()
            or self.temporary.exists()
            or self.temporary.is_symlink()
        ):
            raise Refusal(
                "an interrupted deployment journal requires administrator recovery"
            )
        _verify_archived_deployments(
            self.directory,
            owner_uid=self.owner_uid,
            lock_held=self.lock_fd is not None,
        )

    def _write_fd(self, fd: int, record: Mapping[str, Any]) -> None:
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as output:
            json.dump(record, output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        _fsync_directory(self.directory)

    def start(self, states: Mapping[str, tuple[str, str]]) -> None:
        record = {
            "deployment_id": secrets.token_hex(16),
            "approved_release_sha": RELEASE_SHA,
            "previous_release_sha": PREVIOUS_SHA,
            "deployment_pin_installer_sha256": INSTALLER_SHA256,
            "deployment_pin_sha256": DEPLOYMENT_PIN_SHA256,
            "phase": "prepared",
            # Desired post-approval operating state is fixed by the approved
            # release policy. Observed initial state is only used for safe
            # cleanup before shutdown; it never triggers a service start.
            "service_state": ["active", "enabled"],
            "backup_timer_state": ["active", "enabled"],
            "started_at_utc": datetime.now(UTC).isoformat(),
        }
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except FileExistsError as exc:
            raise Refusal(
                "an interrupted deployment journal requires administrator recovery"
            ) from exc
        self._write_fd(fd, record)
        # Keep the exclusive audit lock for the entire deployment. The active
        # journal also blocks starts after this process exits.

    def update(self, phase: str, **details: Any) -> None:
        _assert_managed_file(self.path, owner=self.owner_uid, mode=0o600)
        try:
            with self.path.open("r", encoding="utf-8") as source:
                record = json.load(source)
        except (OSError, ValueError) as exc:
            raise Refusal("the deployment journal cannot be safely updated") from exc
        if (
            record.get("approved_release_sha") != RELEASE_SHA
            or record.get("previous_release_sha") != PREVIOUS_SHA
        ):
            raise Refusal("the deployment journal release identity is unexpected")
        allowed = DEPLOYMENT_TRANSITIONS.get(record.get("phase"), frozenset())
        if phase == "complete" or phase not in allowed:
            raise Refusal("deployment journal transition is not allowed")
        record["phase"] = phase
        record["updated_at_utc"] = datetime.now(UTC).isoformat()
        record.update(details)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.temporary, flags, 0o600)
        except FileExistsError as exc:
            raise Refusal(
                "a stale journal temporary file requires administrator recovery"
            ) from exc
        self._write_fd(fd, record)
        os.replace(self.temporary, self.path)
        _fsync_directory(self.directory)


DEPLOYMENT_TRANSITIONS = {
    "prepared": {
        "timer_pause_started",
        "aborted_before_service_changes",
        "manual_recovery_required",
    },
    "timer_pause_started": {
        "timer_paused",
        "aborted_before_service_changes",
        "manual_recovery_required",
    },
    "timer_paused": {
        "backup_verified",
        "aborted_before_service_changes",
        "manual_recovery_required",
    },
    "backup_verified": {
        "backup_and_preflight_verified",
        "manual_recovery_required",
    },
    "backup_and_preflight_verified": {"shutdown_started", "manual_recovery_required"},
    "shutdown_started": {"installer_started", "manual_recovery_required"},
    "installer_started": {"installer_returned", "manual_recovery_required"},
    "installer_returned": {"release_verified", "manual_recovery_required"},
    "release_verified": {"installation_verified", "manual_recovery_required"},
    # This is the logical transition table. DeploymentJournal.update rejects
    # complete; only production_admin.finalize-deployment can perform it.
    "installation_verified": {"complete"},
    "complete": set(),
    "aborted_before_service_changes": set(),
    "manual_recovery_required": set(),
}


def perform_deployment(ops: Mapping[str, Any], journal: Any) -> None:
    """Run the controlled state machine; ops/journal are injectable for tests."""
    states = ops["states"]
    validate_unit_states(states)
    ops["check_database"]()
    journal.start(states)
    timer_pause_started = False
    shutdown_started = False
    phase = "prepared"
    backup_locks: Any = None
    try:
        journal.update("timer_pause_started")
        timer_pause_started = True
        phase = "timer_pause_started"
        ops["pause_timer"]()
        journal.update("timer_paused")
        phase = "timer_paused"
        ops["wait_backup_idle"]()

        backup, backup_hash, backup_inventory = ops["backup"]()
        backup_locks = ops["acquire_backup_locks"]()
        ops["recheck_backup_exclusive"](backup, backup_hash, backup_inventory)
        journal.update(
            "backup_verified",
            database_backup=str(backup),
            database_backup_sha256=backup_hash,
            backup_inventory=backup_inventory,
        )
        phase = "backup_verified"
        preflight_copy, preflight_hash = ops["preserve_preflight"]()
        manifest = ops["write_manifest"](
            backup, backup_hash, preflight_copy, preflight_hash
        )
        journal.update(
            "backup_and_preflight_verified",
            shared_preflight_backup=str(preflight_copy),
            shared_preflight_sha256=preflight_hash,
            manifest=str(manifest),
        )
        phase = "backup_and_preflight_verified"

        # Persist intent before changing either managed service.
        journal.update("shutdown_started")
        phase = "shutdown_started"
        shutdown_started = True
        ops["shutdown"]()
        db_signature = ops["database_signature"]()
        journal.update("installer_started", database_signature=list(db_signature))
        phase = "installer_started"
        installer_exit_code = ops["install"]()
        journal.update("installer_returned", installer_exit_code=installer_exit_code)
        phase = "installer_returned"
        ops["verify_release"](backup, backup_hash, preflight_hash, db_signature)
        journal.update("release_verified")
        phase = "release_verified"

        ops["release_backup_locks"](backup_locks)
        backup_locks = None

        result_name, result_sha256 = ops["write_installation_report"](
            states,
            backup,
            backup_hash,
            preflight_copy,
            preflight_hash,
            manifest,
            db_signature,
        )
        journal.update(
            "installation_verified",
            installation_result=result_name,
            installation_result_sha256=result_sha256,
            verified_at_utc=datetime.now(UTC).isoformat(),
        )
        phase = "installation_verified"
    except BaseException as exc:
        failure = type(exc).__name__
        diagnostic = None
        installer_diagnostic = None
        if isinstance(exc, InstallerExecutionFailure):
            installer_diagnostic = {
                "outcome": exc.outcome,
                "exit_code": exc.exit_code,
                "category": exc.category,
            }
        if isinstance(exc, DatabaseCheckFailure):
            diagnostic = {
                "check_id": exc.check_id,
                "exit_code": exc.exit_code,
                "error_class": exc.error_class,
            }
            print(
                "Armory database check failed "
                f"(check={exc.check_id}, exit={exc.exit_code}, "
                f"class={exc.error_class}).",
                file=sys.stderr,
            )
        final_states: dict[str, tuple[str, str]] | None = None
        fail_closed_error: str | None = None
        if shutdown_started:
            try:
                final_states = ops["fail_closed"]()
            except BaseException as cleanup_exc:
                fail_closed_error = type(cleanup_exc).__name__
                try:
                    final_states = ops["observe_unit_states"]()
                except BaseException:
                    final_states = None
            try:
                journal.update(
                    "manual_recovery_required",
                    interrupted_phase=phase,
                    failure_class=failure,
                    final_unit_states=final_states,
                    fail_closed_verified=fail_closed_error is None,
                    fail_closed_error=fail_closed_error,
                    database_check_failure=diagnostic,
                    installer_failure=installer_diagnostic,
                )
            except BaseException:
                pass
            print(
                "Armory deployment stopped safely; keep both managed units stopped "
                "and disabled pending administrator recovery.",
                file=sys.stderr,
            )
        elif timer_pause_started:
            final_states = None
            try:
                ops["restore_timer"](states[BACKUP_TIMER])
                final_states = ops["observe_unit_states"]()
                journal.update(
                    "aborted_before_service_changes",
                    interrupted_phase=phase,
                    failure_class=failure,
                    final_unit_states=final_states,
                    database_check_failure=diagnostic,
                    installer_failure=installer_diagnostic,
                )
            except BaseException:
                try:
                    final_states = ops["observe_unit_states"]()
                except BaseException:
                    pass
                try:
                    journal.update(
                        "manual_recovery_required",
                        interrupted_phase=phase,
                        failure_class=failure,
                        final_unit_states=final_states,
                        database_check_failure=diagnostic,
                        installer_failure=installer_diagnostic,
                    )
                except BaseException:
                    pass
                print(
                    "Deployment stopped before bot shutdown, but timer restoration "
                    "could not be verified; administrator recovery is required.",
                    file=sys.stderr,
                )
        else:
            try:
                journal.update(
                    "aborted_before_service_changes",
                    interrupted_phase=phase,
                    failure_class=failure,
                    final_unit_states=ops["observe_unit_states"](),
                    database_check_failure=diagnostic,
                    installer_failure=installer_diagnostic,
                )
            except BaseException:
                pass
        if isinstance(exc, Refusal):
            raise
        raise Refusal(
            "deployment interrupted or failed; follow the journal recovery procedure"
        ) from exc
    finally:
        try:
            ops["release_backup_locks"](backup_locks)
        except BaseException as exc:
            # A lock close failure is not a reason to alter service/database state.
            print(
                "Warning: could not release a backup lock cleanly "
                f"({type(exc).__name__}).",
                file=sys.stderr,
            )


def deploy(journal: DeploymentJournal | None = None) -> None:
    validate_host(socket.gethostname())
    if os.geteuid() != 0:
        raise Refusal("run only through the approved root-owned sudo entry point")
    _check_checkout()
    uid, gid = _check_paths_and_current()
    states = _checked_unit_state()
    journal = journal or DeploymentJournal()
    journal.assert_no_pending()
    _ensure_audit_dir()

    # The database check occurs before the timer or bot is interrupted.
    def check_database() -> None:
        _run_as_armory(
            uid,
            gid,
            CURRENT / ".venv/bin/python",
            "check",
            timeout=300,
            check_id="preflight_before_timer_pause",
        )

    def backup() -> tuple[Path, str, dict[str, tuple[int, ...]]]:
        return _backup_database(uid, gid)

    def acquire_backup_locks() -> tuple[int, int]:
        return _acquire_backup_locks(uid, gid)

    def recheck_backup_exclusive(
        backup_path: Path,
        backup_hash: str,
        inventory: Mapping[str, tuple[int, ...]],
    ) -> None:
        _recheck_backup_exclusive(uid, gid, backup_path, backup_hash, inventory)

    def preserve_preflight() -> tuple[Path, str]:
        return _preserve_preflight()

    def write_manifest(
        backup_path: Path, backup_hash: str, preflight_copy: Path, preflight_hash: str
    ) -> Path:
        payload = {
            "approved_release_sha": RELEASE_SHA,
            "previous_release_sha": PREVIOUS_SHA,
            "database_backup": str(backup_path),
            "database_backup_sha256": backup_hash,
            "shared_preflight_backup": str(preflight_copy),
            "shared_preflight_sha256": preflight_hash,
            "created_at_utc": datetime.now(UTC).isoformat(),
        }
        _write_manifest_file(MANIFEST_PATH, payload)
        return MANIFEST_PATH

    def verify_release(
        backup_path: Path,
        backup_hash: str,
        preflight_hash: str,
        stopped_db_signature: tuple[int, ...],
    ) -> None:
        current_target = _managed_current_target(CURRENT)
        marker = current_target / ".installation-complete"
        _assert_managed_file(marker, owner=0)
        if (
            current_target != RELEASES / RELEASE_SHA
            or marker.read_text(encoding="ascii") != RELEASE_SHA
        ):
            raise Refusal("the installed release pointer or marker is unexpected")
        if _sha256(PREFLIGHT) != preflight_hash:
            raise Refusal(
                "the installed shared database preflight checksum is unexpected"
            )
        installed_backup_unit = Path("/etc/systemd/system") / BACKUP_SERVICE
        info = _assert_managed_file(installed_backup_unit, owner=0, mode=0o644)
        if (
            info.st_gid != 0
            or info.st_nlink != 1
            or _sha256(installed_backup_unit) != RELEASE_BACKUP_UNIT_SHA256
        ):
            raise Refusal("installed backup unit does not match the approved release")
        if _db_signature() != stopped_db_signature:
            raise Refusal(
                "the production database changed during code-only installation"
            )
        _run_as_armory(
            uid,
            gid,
            current_target / ".venv/bin/python",
            "check",
            timeout=300,
            check_id="installed_release_database",
            cwd=current_target,
        )
        _assert_managed_file(backup_path, owner=uid, group=gid, mode=0o600)
        if _sha256(backup_path) != backup_hash:
            raise Refusal("the verified database backup checksum changed")

    def write_installation_report(
        original_states: Mapping[str, tuple[str, str]],
        backup_path: Path,
        backup_hash: str,
        preflight_copy: Path,
        preflight_hash: str,
        manifest: Path,
        stopped_db_signature: tuple[int, ...],
    ) -> tuple[str, str]:
        observed = _observe_unit_states()
        expected_stopped = {
            SERVICE: ("inactive", "disabled"),
            BACKUP_TIMER: ("inactive", "disabled"),
        }
        if any(observed.get(unit) != state for unit, state in expected_stopped.items()):
            raise Refusal("managed services are not stopped and disabled after install")
        if observed.get(BACKUP_SERVICE, (None, None))[0] != "inactive":
            raise Refusal("backup service remains active after installation")
        if _armory_process_present():
            raise Refusal("an Armory bot process remains after installation")
        active, _ = _read_audit_json(JOURNAL_PATH, owner_uid=0)
        deployment_id = active.get("deployment_id")
        if (
            not isinstance(deployment_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", deployment_id) is None
        ):
            raise Refusal("deployment identity is missing or invalid")
        if (
            active.get("phase") != "release_verified"
            or active.get("installer_exit_code") != 0
            or active.get("approved_release_sha") != RELEASE_SHA
            or active.get("previous_release_sha") != PREVIOUS_SHA
            or active.get("manifest") != str(manifest)
            or active.get("database_backup_sha256") != backup_hash
            or active.get("shared_preflight_backup") != str(preflight_copy)
            or active.get("shared_preflight_sha256") != preflight_hash
        ):
            raise Refusal("installation result does not match the active journal")
        if _db_signature() != stopped_db_signature:
            raise Refusal("the production database changed during installation")
        result = {
            "schema_version": 1,
            "deployment_id": deployment_id,
            "approved_release_sha": RELEASE_SHA,
            "previous_release_sha": PREVIOUS_SHA,
            "manifest": str(manifest),
            "manifest_sha256": _sha256(manifest),
            "database_backup": str(backup_path),
            "database_backup_sha256": backup_hash,
            "shared_preflight_backup": str(preflight_copy),
            "shared_preflight_sha256": preflight_hash,
            "installer_sha256": INSTALLER_SHA256,
            "deployment_pin_sha256": DEPLOYMENT_PIN_SHA256,
            "database_signature": list(stopped_db_signature),
            "intended_service_state": ["active", "enabled"],
            "intended_backup_timer_state": ["active", "enabled"],
            "observed_service_states": {
                key: list(value) for key, value in observed.items()
            },
            "verified_at_utc": datetime.now(UTC).isoformat(),
        }
        name = f"deployment-installation-{deployment_id}.json"
        path = AUDIT_DIR / name
        _write_manifest_file(path, result)
        return name, _sha256(path)

    ops: dict[str, Any] = {
        "states": states,
        "check_database": check_database,
        "pause_timer": lambda: _service_action("stop", BACKUP_TIMER),
        "wait_backup_idle": _wait_backup_job_idle,
        "backup": backup,
        "acquire_backup_locks": acquire_backup_locks,
        "recheck_backup_exclusive": recheck_backup_exclusive,
        "release_backup_locks": _release_backup_locks,
        "preserve_preflight": preserve_preflight,
        "write_manifest": write_manifest,
        "shutdown": _stop_and_disable,
        "database_signature": _db_signature,
        "install": lambda: _run_installer(installer_command(), timeout=3600),
        "verify_release": verify_release,
        "write_installation_report": write_installation_report,
        "restore_states": _restore_states,
        "restore_timer": lambda state: _set_unit_state(BACKUP_TIMER, state),
        "fail_closed": _fail_closed,
        "observe_unit_states": _observe_unit_states,
    }
    perform_deployment(ops, journal)


def _write_manifest_file(path: Path, payload: Mapping[str, Any]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise Refusal(
            "the deployment manifest already exists or cannot be created"
        ) from exc
    with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as output:
        json.dump(payload, output, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    _fsync_directory(path.parent)


def main(
    argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None
) -> int:
    argv = sys.argv if argv is None else argv
    environ = os.environ if environ is None else environ
    try:
        validate_arguments(argv)
        validate_environment(environ)
        validate_host(socket.gethostname())
        os.umask(0o077)
        pin, pin_sha256 = _read_deployment_pin_with_hash()
        apply_deployment_pin(pin, pin_sha256=pin_sha256)
        journal = DeploymentJournal()
        journal.acquire_exclusive_lock()
        try:
            # A pending journal is never automatically resumed or cleared.
            journal.assert_no_pending()
            old_handlers: dict[signal.Signals, Any] = {}

            def interrupted(signum: int, frame: Any) -> None:
                raise Refusal(f"deployment interrupted by signal {signum}")

            for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                old_handlers[signum] = signal.signal(signum, interrupted)
            try:
                deploy(journal)
            finally:
                for signum, handler in old_handlers.items():
                    signal.signal(signum, handler)
        finally:
            journal.release_lock()
        return 0
    except Refusal as exc:
        print(f"Armory deployment refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
