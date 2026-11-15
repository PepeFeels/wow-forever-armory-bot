#!/usr/bin/env python3
"""Install the reviewed Armory release and dormant systemd units.

This program is an administrator-only deployment tool. It never enables or
starts a service. Supply a non-secret EnvironmentFile and a root-owned 0600
credential source file. Production command sync is global; test-guild sync
must not be configured in the production EnvironmentFile.
"""

from __future__ import annotations

import argparse
import grp
import io
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

APPROVED_REPO = Path("/opt/wow-forever-armory-bot/deployment-checkout")
INSTALLED_HELPER = Path("/opt/wow-forever-armory-bot/bin/production_install.py")
SYSTEM_PYTHON = Path("/usr/bin/python3.14")
VENV_SYMLINK_TARGETS = {
    ".venv/lib64": "lib",
    ".venv/bin/python": "python3.14",
    ".venv/bin/python3": "python3.14",
    ".venv/bin/python3.14": "/usr/bin/python3.14",
    ".venv/bin/𝜋thon": "python3.14",
}

# Importing the snapshot helper must not dirty the trusted source checkout.
sys.dont_write_bytecode = True

if not __package__:
    script_path = Path(__file__).resolve()
    import_root = (
        APPROVED_REPO if script_path == INSTALLED_HELPER else script_path.parent.parent
    )
    sys.path.insert(0, str(import_root))
from deploy.production_db_snapshot import verify_database_snapshot  # noqa: E402

BASE_CHECKPOINT_SHA = "a1a6a2ad95302f7e3c4900f18e91691f43631e18"
BRANCH = "release/v2.0.0"
SERVICE_USER = "wow-armory"
BASE = Path("/opt/wow-forever-armory-bot")
ETC = Path("/etc/wow-forever-armory-bot")
STATE = Path("/var/lib/wow-forever-armory-bot")
UNIT_FILES = (
    "wow-forever-armory-bot.service",
    "wow-forever-armory-backup.service",
    "wow-forever-armory-backup.timer",
)
ENABLEABLE_UNITS = (
    "wow-forever-armory-bot.service",
    "wow-forever-armory-backup.timer",
)
CONFIG_KEYS = {
    "ARMORY_LOG_LEVEL",
    "ARMORY_LOG_FORMAT",
    "ARMORY_BACKUP_RETENTION",
    "ARMORY_LOGO_PATH",
}


class InstallError(RuntimeError):
    """Safe installation failure with no credential data in the message."""

    diagnostic_category = "installer_rejected"

    def __init__(self, message: str, *, category: str = "installer_rejected") -> None:
        self.diagnostic_category = category
        super().__init__(message)


def _read_regular_file(
    path: str | Path,
    *,
    expected_owner: int,
    maximum_bytes: int,
    exact_mode: int | None = None,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise InstallError(
            "input file is unavailable", category="input_rejected"
        ) from exc
    try:
        info = os.fstat(descriptor)
        mode = stat.S_IMODE(info.st_mode)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != expected_owner
            or (
                mode != exact_mode
                if exact_mode is not None
                else bool(mode & 0o027) or mode & 0o600 != 0o600
            )
        ):
            raise InstallError(
                "input file ownership or permissions are unsafe",
                category="input_rejected",
            )
        data = os.read(descriptor, maximum_bytes + 1)
        if len(data) > maximum_bytes:
            raise InstallError(
                "input file is larger than permitted", category="input_rejected"
            )
        return data
    finally:
        os.close(descriptor)


def validate_config(config: str | Path, *, expected_owner: int = 0) -> None:
    """Validate a production config, which must not pin command sync to a guild."""
    try:
        content = _read_regular_file(
            config, expected_owner=expected_owner, maximum_bytes=65536
        ).decode("utf-8")
    except UnicodeError as exc:
        raise InstallError(
            "configuration file is unavailable", category="input_rejected"
        ) from exc
    settings: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if not separator or key not in CONFIG_KEYS or key in settings:
            raise InstallError(
                "configuration contains unsupported or duplicate settings",
                category="input_rejected",
            )
        settings[key] = value
    log_level = settings.get("ARMORY_LOG_LEVEL", "INFO")
    log_format = settings.get("ARMORY_LOG_FORMAT", "text")
    retention = settings.get("ARMORY_BACKUP_RETENTION", "14")
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise InstallError(
            "configuration has an invalid log level", category="input_rejected"
        )
    if log_format not in {"text", "json"}:
        raise InstallError(
            "configuration has an invalid log format", category="input_rejected"
        )
    if not retention.isdecimal() or int(retention) < 1:
        raise InstallError(
            "configuration has an invalid backup retention", category="input_rejected"
        )
    logo_path = settings.get("ARMORY_LOGO_PATH")
    if "ARMORY_LOGO_PATH" in settings and logo_path != (
        "/etc/wow-forever-armory-bot/wow_forever_logo.png"
    ):
        raise InstallError(
            "configuration has an unsupported local logo path",
            category="input_rejected",
        )


def validate_inputs(
    config_source: str | Path,
    credential_source: str | Path,
    *,
    expected_owner: int = 0,
) -> None:
    validate_config(config_source, expected_owner=expected_owner)
    token = _read_regular_file(
        credential_source,
        expected_owner=expected_owner,
        maximum_bytes=4096,
        exact_mode=0o600,
    )
    token = token.removesuffix(b"\n")
    if (
        not token
        or b"\n" in token
        or b"\r" in token
        or b"\0" in token
        or not token.isascii()
        or any(byte < 0x21 or byte > 0x7E for byte in token)
    ):
        raise InstallError(
            "credential source must contain one non-empty token line",
            category="input_rejected",
        )


def _run(
    command: list[str], *, capture: bool = False, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            text=True,
            # Always capture child output so an unexpected error cannot print
            # config-derived secrets or host paths to the install journal.
            capture_output=True,
            env=env,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        executable = Path(command[0]).name or "command"
        exit_code = (
            f" (exit {exc.returncode})"
            if isinstance(exc, subprocess.CalledProcessError)
            else " (could not start)"
        )
        # Child stderr can contain local paths or data copied from config files.
        # Keep the safe, actionable facts and omit raw output entirely.
        raise InstallError(
            f"installation command failed: {executable}{exit_code}",
            category="command_failed",
        ) from exc


def _assert_units_stopped_and_disabled(*, upgrade_existing: bool) -> None:
    """Fail closed unless the bot and backup jobs are dormant."""
    try:
        for unit in UNIT_FILES:
            result = subprocess.run(
                ["systemctl", "is-active", unit],
                text=True,
                capture_output=True,
            )
            if result.stdout.strip() not in {"inactive", "failed", "unknown"}:
                raise InstallError(
                    "could not verify that target units are stopped",
                    category="unit_state_rejected",
                )
        for unit in ENABLEABLE_UNITS:
            result = subprocess.run(
                ["systemctl", "is-enabled", unit],
                text=True,
                capture_output=True,
            )
            accepted = {"disabled"} if upgrade_existing else {"disabled", "not-found"}
            if result.stdout.strip() not in accepted:
                raise InstallError(
                    "target service or backup timer is enabled or unknown",
                    category="unit_state_rejected",
                )
    except OSError as exc:
        raise InstallError(
            "could not verify systemd unit state", category="unit_state_rejected"
        ) from exc


def _assert_no_armory_process(proc_root: Path = Path("/proc")) -> None:
    """Keep the old preflight's process guard without opening its database."""
    try:
        entries = tuple(proc_root.iterdir())
    except OSError as exc:
        raise InstallError("could not verify Armory process state") from exc
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            arguments = [
                item.decode(errors="replace")
                for item in (entry / "cmdline").read_bytes().split(b"\0")
                if item
            ]
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise InstallError("could not verify Armory process state") from exc
        if (
            len(arguments) >= 3
            and arguments[1:3] == ["-m", "wow_forever_armory.discord_bot"]
        ) or any(Path(argument).name == "discord_bot.py" for argument in arguments):
            raise InstallError(
                "an Armory bot process is still running",
                category="process_state_rejected",
            )


def _verify_upgrade_database(database: Path, *, uid: int, gid: int) -> None:
    _assert_no_armory_process()
    try:
        verify_database_snapshot(database, expected_owner=uid, expected_group=gid)
    except Exception as exc:
        # Surface no source paths, SQL details, or data values in installer output.
        raise InstallError(
            "isolated production database snapshot verification failed",
            category="database_snapshot_rejected",
        ) from exc


def _require_supported_runtime(interpreter: Path) -> None:
    try:
        version = subprocess.run(
            [
                str(interpreter),
                "-c",
                (
                    "import platform,sys; "
                    "print(f'{sys.version_info.major}.{sys.version_info.minor}:"
                    "{platform.machine()}:{sys.platform}')"
                ),
            ],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise InstallError(
            "could not verify the release Python runtime",
            category="python_runtime_rejected",
        ) from exc
    if version != "3.14:x86_64:linux":
        raise InstallError(
            "release lock supports CPython 3.14 on Linux x86_64",
            category="python_runtime_rejected",
        )


def _git(repo: Path, *args: str) -> str:
    result = _run(["git", "-C", str(repo), *args], capture=True)
    return result.stdout.strip()


def _check_repository(repo: Path, release_sha: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", release_sha):
        raise InstallError("release SHA must be a full lowercase commit hash")
    if _git(repo, "branch", "--show-current") != BRANCH:
        raise InstallError(
            "repository is not on the approved release branch",
            category="repository_rejected",
        )
    if _git(repo, "status", "--porcelain"):
        raise InstallError(
            "repository must be clean before installation",
            category="repository_rejected",
        )
    if _git(repo, "rev-parse", "HEAD") != release_sha:
        raise InstallError(
            "checkout HEAD does not match the approved release SHA",
            category="repository_rejected",
        )
    try:
        _run(
            [
                "git",
                "-C",
                str(repo),
                "merge-base",
                "--is-ancestor",
                BASE_CHECKPOINT_SHA,
                release_sha,
            ]
        )
    except InstallError as exc:
        raise InstallError(
            "approved release is not based on the reviewed checkpoint",
            category="repository_rejected",
        ) from exc


def _account() -> tuple[int, int]:
    try:
        group = grp.getgrnam(SERVICE_USER)
    except KeyError:
        _run(["groupadd", "--system", SERVICE_USER])
        group = grp.getgrnam(SERVICE_USER)
    try:
        user = pwd.getpwnam(SERVICE_USER)
    except KeyError:
        _run(
            [
                "useradd",
                "--system",
                "--no-create-home",
                "--home-dir",
                "/nonexistent",
                "--shell",
                "/usr/sbin/nologin",
                "--gid",
                SERVICE_USER,
                SERVICE_USER,
            ]
        )
        user = pwd.getpwnam(SERVICE_USER)
    if user.pw_gid != group.gr_gid or user.pw_dir != "/nonexistent":
        raise InstallError(
            "existing service account does not match expected isolation",
            category="service_account_rejected",
        )
    return user.pw_uid, group.gr_gid


def _mkdir_owned(path: Path, mode: int, uid: int, gid: int) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    if path.is_symlink() or not path.is_dir():
        raise InstallError("installation directory is not a real directory")
    os.chmod(path, mode)
    os.chown(path, uid, gid)


def _permits(info: os.stat_result, uid: int, gid: int, bit: int) -> bool:
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid == uid:
        return bool(mode & (bit << 6))
    if info.st_gid == gid:
        return bool(mode & (bit << 3))
    return bool(mode & bit)


def _trusted_directory_chain(path: Path, *, owner_uid: int) -> bool:
    try:
        for directory in reversed((path, *path.parents)):
            info = directory.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != owner_uid
                or info.st_mode & 0o022
            ):
                return False
        return True
    except OSError:
        return False


def _verify_release_symlink(
    release: Path,
    path: Path,
    uid: int,
    gid: int,
    *,
    owner_uid: int = 0,
    system_python: Path = SYSTEM_PYTHON,
) -> None:
    relative_name = path.relative_to(release).as_posix()
    expected_target = VENV_SYMLINK_TARGETS.get(relative_name)
    if relative_name == ".venv/bin/python3.14":
        expected_target = str(system_python)
    try:
        info = path.lstat()
        target_text = os.readlink(path)
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallError(
            "release symbolic link is unavailable",
            category="release_symlink_rejected",
        ) from exc
    if (
        not stat.S_ISLNK(info.st_mode)
        or info.st_uid != owner_uid
        or expected_target is None
        or target_text != expected_target
    ):
        raise InstallError(
            "release contains an unapproved symbolic link",
            category="release_symlink_rejected",
        )

    if relative_name == ".venv/lib64":
        expected_resolved = release / ".venv/lib"
        target_info = resolved.stat()
        valid_target = stat.S_ISDIR(target_info.st_mode)
    else:
        try:
            expected_resolved = system_python.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise InstallError(
                "trusted system Python is unavailable",
                category="release_symlink_rejected",
            ) from exc
        target_info = resolved.stat()
        valid_target = (
            _trusted_directory_chain(
                expected_resolved.parent, owner_uid=owner_uid
            )
            and stat.S_ISREG(target_info.st_mode)
        )

    required_bit = 0o1 if stat.S_ISDIR(target_info.st_mode) else 0o4
    if (
        resolved != expected_resolved
        or not valid_target
        or target_info.st_uid != owner_uid
        or target_info.st_mode & 0o022
        or not _permits(target_info, uid, gid, required_bit)
        or stat.S_ISREG(target_info.st_mode)
        and not _permits(target_info, uid, gid, 0o1)
    ):
        raise InstallError(
            "release symbolic link target is not a verified venv target",
            category="release_symlink_rejected",
        )


def _verify_release_readable(
    release: Path,
    uid: int,
    gid: int,
    *,
    owner_uid: int = 0,
    system_python: Path = SYSTEM_PYTHON,
) -> None:
    if release.is_symlink():
        raise InstallError(
            "release path must not be a symbolic link",
            category="release_marker_rejected",
        )
    def walk_error(_error: OSError) -> None:
        raise InstallError(
            "release tree could not be fully inspected",
            category="release_permissions_rejected",
        )

    for directory, child_directories, files in os.walk(
        release, followlinks=False, onerror=walk_error
    ):
        for path in (
            Path(directory),
            *(Path(directory) / name for name in child_directories),
        ):
            if path.is_symlink():
                _verify_release_symlink(
                    release,
                    path,
                    uid,
                    gid,
                    owner_uid=owner_uid,
                    system_python=system_python,
                )
                continue
            info = path.stat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != owner_uid
                or info.st_mode & 0o022
            ):
                raise InstallError(
                    "release directory ownership or mode is unsafe",
                    category="release_permissions_rejected",
                )
            if not _permits(info, uid, gid, 0o1):
                raise InstallError(
                    "service account cannot traverse release directory",
                    category="release_permissions_rejected",
                )
        for name in files:
            path = Path(directory) / name
            if path.is_symlink():
                _verify_release_symlink(
                    release,
                    path,
                    uid,
                    gid,
                    owner_uid=owner_uid,
                    system_python=system_python,
                )
                continue
            info = path.stat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != owner_uid
                or info.st_mode & 0o022
            ):
                raise InstallError(
                    "release file ownership or mode is unsafe",
                    category="release_permissions_rejected",
                )
            if not _permits(info, uid, gid, 0o4):
                raise InstallError(
                    "service account cannot read release file",
                    category="release_permissions_rejected",
                )
            if info.st_mode & 0o111 and not _permits(info, uid, gid, 0o1):
                raise InstallError(
                    "service account cannot execute release file",
                    category="release_permissions_rejected",
                )

    # The generic walk only requires execute permission when a file already
    # carries an execute bit. Explicitly validate the venv interpreter so a
    # completed marker cannot make a non-executable Python pass reuse checks.
    interpreter = release / ".venv/bin/python"
    try:
        info = interpreter.stat()
    except OSError as exc:
        raise InstallError(
            "release Python interpreter is unavailable",
            category="release_permissions_rejected",
        ) from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != owner_uid
        or info.st_mode & 0o022
        or not _permits(info, uid, gid, 0o4)
        or not _permits(info, uid, gid, 0o1)
    ):
        raise InstallError(
            "service account cannot read and execute release Python",
            category="release_permissions_rejected",
        )


def _validate_logo_asset(path: Path, service_uid: int, service_gid: int) -> None:
    if path.is_symlink() or not path.is_file():
        raise InstallError("configured logo must be a regular non-symlink file")
    info = path.stat()
    if (
        info.st_uid != 0
        or info.st_gid != service_gid
        or stat.S_IMODE(info.st_mode) != 0o640
    ):
        raise InstallError("configured logo ownership or permissions are unsafe")
    if not _permits(info, service_uid, service_gid, 0o4):
        raise InstallError("service account cannot read configured logo")


def _configured_logo_path(config: Path) -> Path | None:
    content = _read_regular_file(config, expected_owner=0, maximum_bytes=65536)
    for line in content.decode("utf-8").splitlines():
        key, separator, value = line.strip().partition("=")
        if separator and key == "ARMORY_LOGO_PATH":
            return Path(value)
    return None


def _copy_secret(source: Path, destination: Path) -> None:
    data = _read_regular_file(
        source, expected_owner=0, maximum_bytes=4096, exact_mode=0o600
    )
    temporary = destination.with_name(f".{destination.name}.installing")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chown(temporary, 0, 0)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _install_release_impl(
    repo: Path, release: Path, uid: int, gid: int, release_sha: str
) -> None:
    _require_supported_runtime(Path(sys.executable))
    marker = release / ".installation-complete"
    if release.is_symlink():
        raise InstallError("release directory must not be a symbolic link")
    if release.exists():
        if (
            marker.is_symlink()
            or not marker.is_file()
            or marker.stat().st_uid != 0
            or marker.read_text() != release_sha
        ):
            raise InstallError(
                "release path is incomplete or has a mismatched marker; "
                "preserve it for operator review",
                category="release_marker_rejected",
            )
        else:
            _verify_release_readable(release, uid, gid)
            return
    else:
        release.mkdir(parents=True, mode=0o755)
    try:
        archive = subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar", release_sha],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise InstallError(
            "could not read approved source release",
            category="release_build_failed",
        ) from exc
    try:
        with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as bundle:
            bundle.extractall(release, filter="data")
    except (OSError, tarfile.TarError) as exc:
        raise InstallError(
            "could not extract the approved source release",
            category="release_build_failed",
        ) from exc
    interpreter = Path(sys.executable)
    _run([str(interpreter), "-m", "venv", str(release / ".venv")])
    python = release / ".venv" / "bin" / "python"
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-input",
            "--require-hashes",
            "-r",
            str(release / "deploy/requirements-build.lock"),
        ]
    )
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-input",
            "--require-hashes",
            "-r",
            str(release / "deploy/requirements-runtime.lock"),
        ]
    )
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-input",
            "--no-build-isolation",
            "--no-deps",
            str(release),
        ]
    )
    _run([str(python), "-m", "pip", "check"])
    # Parse each source file without creating __pycache__ in the immutable release.
    _run(
        [
            str(python),
            "-I",
            "-B",
            "-c",
            "import ast, pathlib, sys; root = pathlib.Path(sys.argv[1]); "
            "[ast.parse(path.read_text(encoding='utf-8'), filename=str(path)) "
            "for path in root.rglob('*.py')]",
            str(release / "src"),
        ]
    )
    for directory, subdirectories, files in os.walk(release, followlinks=False):
        os.chown(directory, 0, 0)
        for name in subdirectories:
            path = Path(directory) / name
            if not path.is_symlink():
                os.chown(path, 0, 0)
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink():
                os.chown(path, 0, 0)
    marker.write_text(release_sha, encoding="ascii")
    os.chown(marker, 0, 0)
    os.chmod(marker, 0o444)
    os.chown(release, 0, 0)
    os.chmod(release, 0o755)
    _verify_release_readable(release, uid, gid)


def _install_release(
    repo: Path, release: Path, uid: int, gid: int, release_sha: str
) -> None:
    """Build readable release files, then restore the caller's restrictive mask."""
    previous_umask = os.umask(0o022)
    try:
        _install_release_impl(repo, release, uid, gid, release_sha)
    finally:
        os.umask(previous_umask)


def _install_current_link(
    release: Path,
    *,
    upgrade_existing: bool = False,
    repo: Path | None = None,
) -> None:
    current = BASE / "current"
    if current.is_symlink():
        raw_target = os.readlink(current)
        relative = re.fullmatch(r"releases/([0-9a-f]{40})", raw_target)
        absolute = re.fullmatch(
            re.escape(str(BASE / "releases")) + r"/([0-9a-f]{40})", raw_target
        )
        match = relative or absolute
        if match is None:
            raise InstallError(
                "current has a non-canonical release link",
                category="current_pointer_rejected",
            )
        previous = BASE / "releases" / match.group(1)
        if current.resolve() != previous:
            raise InstallError(
                "current release link does not resolve to its declared target",
                category="current_pointer_rejected",
            )
        if previous == release.resolve():
            return
        if not upgrade_existing or repo is None:
            raise InstallError(
                "current points to another release; refusing to replace it",
                category="current_pointer_rejected",
            )
        releases_root = (BASE / "releases").resolve()
        try:
            previous.relative_to(releases_root)
        except ValueError as exc:
            raise InstallError(
                "current points outside the managed releases",
                category="current_pointer_rejected",
            ) from exc
        previous_sha = previous.name
        marker = previous / ".installation-complete"
        if (
            not re.fullmatch(r"[0-9a-f]{40}", previous_sha)
            or previous.is_symlink()
            or not marker.is_file()
            or marker.is_symlink()
            or marker.read_text(encoding="ascii") != previous_sha
        ):
            raise InstallError(
                "current release is incomplete; refusing upgrade",
                category="current_pointer_rejected",
            )
        try:
            _run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "merge-base",
                    "--is-ancestor",
                    previous_sha,
                    release.name,
                ]
            )
        except InstallError as exc:
            raise InstallError(
                "upgrade must descend from the installed release",
                category="current_pointer_rejected",
            ) from exc
    elif current.exists():
        raise InstallError("current exists and is not the approved release symlink")
    elif upgrade_existing:
        raise InstallError("upgrade requested but no current release is installed")
    temporary = BASE / ".current.installing"
    if temporary.exists() or temporary.is_symlink():
        raise InstallError(
            "temporary current symlink already exists; inspect it manually"
        )
    temporary.symlink_to(Path("releases") / release.name)
    os.replace(temporary, current)


def _copy_unit(source: Path, destination: Path, mode: int = 0o644) -> None:
    temporary = destination.with_name(f".{destination.name}.installing")
    shutil.copyfile(source, temporary)
    os.chown(temporary, 0, 0)
    os.chmod(temporary, mode)
    os.replace(temporary, destination)


def install(
    repo: Path,
    config_source: Path,
    credential_source: Path,
    release_sha: str,
    *,
    upgrade_existing: bool = False,
) -> None:
    if os.geteuid() != 0:
        raise InstallError(
            "run this installation script as an administrator",
            category="service_account_rejected",
        )
    _check_repository(repo, release_sha)
    if upgrade_existing and (
        config_source != ETC / "config"
        or credential_source != ETC / "discord-token"
        or not (BASE / "current").is_symlink()
    ):
        raise InstallError(
            "upgrade must use the installed config, credential, and current release"
        )
    validate_inputs(config_source, credential_source)
    _require_supported_runtime(Path(sys.executable))
    _assert_units_stopped_and_disabled(upgrade_existing=upgrade_existing)

    if upgrade_existing:
        try:
            service_user = pwd.getpwnam(SERVICE_USER)
            service_group = grp.getgrnam(SERVICE_USER)
        except KeyError as exc:
            raise InstallError("installed service account is unavailable") from exc
        if (
            service_user.pw_gid != service_group.gr_gid
            or service_user.pw_dir != "/nonexistent"
        ):
            raise InstallError(
                "existing service account does not match expected isolation"
            )
        uid, gid = service_user.pw_uid, service_group.gr_gid
    else:
        uid, gid = _account()
    configured_logo = _configured_logo_path(config_source)
    if configured_logo is not None:
        _validate_logo_asset(configured_logo, uid, gid)
    deploy = repo / "deploy"
    systemd_directory = Path("/etc/systemd/system")
    runtime_files = (
        (deploy / "systemd/armory-launch", BASE / "bin/armory-launch"),
        (
            deploy / "systemd/armory-db-preflight.py",
            BASE / "bin/armory-db-preflight",
        ),
        *((deploy / "systemd" / name, systemd_directory / name) for name in UNIT_FILES),
    )
    if upgrade_existing:
        for source, destination in runtime_files:
            if source.name == "armory-db-preflight.py":
                _read_regular_file(
                    destination,
                    expected_owner=0,
                    maximum_bytes=262144,
                    exact_mode=0o755,
                )
                continue
            if (
                not destination.is_file()
                or source.read_bytes() != destination.read_bytes()
            ):
                raise InstallError(
                    "upgrade changes runtime configuration; use a separately "
                    "reviewed install"
                )
        _verify_upgrade_database(STATE / "guild-armory.sqlite3", uid=uid, gid=gid)
    directories = (
        (BASE, 0o755, 0, 0),
        (BASE / "releases", 0o755, 0, 0),
        (BASE / "bin", 0o755, 0, 0),
        (ETC, 0o750, 0, gid),
        (STATE, 0o700, uid, gid),
        (STATE / "backups", 0o700, uid, gid),
    )
    for path, mode, owner, group in (
        directories[:3] if upgrade_existing else directories
    ):
        _mkdir_owned(path, mode, owner, group)

    release = BASE / "releases" / release_sha
    _install_release(repo, release, uid, gid, release_sha)
    if upgrade_existing:
        preflight_source = deploy / "systemd/armory-db-preflight.py"
        preflight_destination = BASE / "bin/armory-db-preflight"
        _copy_unit(preflight_source, preflight_destination, 0o755)
    _install_current_link(
        release,
        upgrade_existing=upgrade_existing,
        repo=repo if upgrade_existing else None,
    )

    config = ETC / "config"
    if not upgrade_existing:
        config_temp = config.with_name(".config.installing")
        shutil.copyfile(config_source, config_temp)
        os.chown(config_temp, 0, gid)
        os.chmod(config_temp, 0o640)
        os.replace(config_temp, config)
        _copy_secret(credential_source, ETC / "discord-token")

    if not upgrade_existing:
        for source, destination in runtime_files:
            _copy_unit(
                source,
                destination,
                0o755
                if source.name == "armory-launch"
                or source.name == "armory-db-preflight.py"
                else 0o644,
            )
        _run(
            [
                "systemd-analyze",
                "verify",
                *[str(systemd_directory / name) for name in UNIT_FILES],
            ]
        )
        _run(["systemctl", "daemon-reload"])
    # Intentionally no enable, start, or restart commands in this installer.


def _repository_for_installer(script_path: Path) -> Path:
    """Use the protected source checkout when running the installed helper."""
    if script_path.resolve() == INSTALLED_HELPER:
        return APPROVED_REPO
    return script_path.resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--config-source", type=Path)
    parser.add_argument("--credential-source", type=Path)
    parser.add_argument(
        "--upgrade-existing",
        action="store_true",
        help=(
            "install code only, preserving existing config, credential, units, "
            "and state"
        ),
    )
    args = parser.parse_args()
    repo = _repository_for_installer(Path(__file__).resolve())
    if args.upgrade_existing:
        config_source = args.config_source or ETC / "config"
        credential_source = args.credential_source or ETC / "discord-token"
    else:
        if args.config_source is None or args.credential_source is None:
            parser.error("initial installation requires config and credential sources")
        config_source = args.config_source
        credential_source = args.credential_source
    try:
        install(
            repo,
            config_source,
            credential_source,
            args.release_sha,
            upgrade_existing=args.upgrade_existing,
        )
    except InstallError as exc:
        diagnostic_fd = os.environ.get("ARMORY_INSTALL_DIAGNOSTIC_FD")
        if diagnostic_fd is not None:
            try:
                os.write(
                    int(diagnostic_fd),
                    f"{exc.diagnostic_category}\n".encode("ascii"),
                )
            except OSError, ValueError:
                pass
        print(f"Installation stopped safely: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Avoid a traceback that could expose local paths or child data.
        print(
            "Installation failed unexpectedly "
            f"({type(exc).__name__}); preserve the install journal for review.",
            file=sys.stderr,
        )
        return 1
    print("Release installed; units remain disabled and stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
