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

BASE_CHECKPOINT_SHA = "9f5548d3763b42d38fff0613ef2cfd6c3b560d07"
BRANCH = "feature/v0.2.0-discord"
SERVICE_USER = "wow-armory"
BASE = Path("/opt/wow-forever-armory-bot")
ETC = Path("/etc/wow-forever-armory-bot")
STATE = Path("/var/lib/wow-forever-armory-bot")
UNIT_FILES = (
    "wow-forever-armory-bot.service",
    "wow-forever-armory-backup.service",
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
        raise InstallError("input file is unavailable") from exc
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
            raise InstallError("input file ownership or permissions are unsafe")
        data = os.read(descriptor, maximum_bytes + 1)
        if len(data) > maximum_bytes:
            raise InstallError("input file is larger than permitted")
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
        raise InstallError("configuration file is unavailable") from exc
    settings: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if not separator or key not in CONFIG_KEYS or key in settings:
            raise InstallError(
                "configuration contains unsupported or duplicate settings"
            )
        settings[key] = value
    log_level = settings.get("ARMORY_LOG_LEVEL", "INFO")
    log_format = settings.get("ARMORY_LOG_FORMAT", "text")
    retention = settings.get("ARMORY_BACKUP_RETENTION", "14")
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise InstallError("configuration has an invalid log level")
    if log_format not in {"text", "json"}:
        raise InstallError("configuration has an invalid log format")
    if not retention.isdecimal() or int(retention) < 1:
        raise InstallError("configuration has an invalid backup retention")
    logo_path = settings.get("ARMORY_LOGO_PATH")
    if "ARMORY_LOGO_PATH" in settings and logo_path != (
        "/etc/wow-forever-armory-bot/wow_forever_logo.png"
    ):
        raise InstallError("configuration has an unsupported local logo path")


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
        raise InstallError("credential source must contain one non-empty token line")


def _run(
    command: list[str], *, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            text=True,
            capture_output=capture,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise InstallError(f"installation command failed: {command[0]}") from exc


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
        raise InstallError("could not verify the release Python runtime") from exc
    if version != "3.14:x86_64:linux":
        raise InstallError("release lock supports CPython 3.14 on Linux x86_64")


def _git(repo: Path, *args: str) -> str:
    result = _run(["git", "-C", str(repo), *args], capture=True)
    return result.stdout.strip()


def _check_repository(repo: Path, release_sha: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", release_sha):
        raise InstallError("release SHA must be a full lowercase commit hash")
    if _git(repo, "branch", "--show-current") != BRANCH:
        raise InstallError("repository is not on the approved feature branch")
    if _git(repo, "status", "--porcelain"):
        raise InstallError("repository must be clean before installation")
    if _git(repo, "rev-parse", "HEAD") != release_sha:
        raise InstallError("checkout HEAD does not match the approved release SHA")
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
            "approved release is not based on the reviewed checkpoint"
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
        raise InstallError("existing service account does not match expected isolation")
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


def _verify_release_readable(release: Path, uid: int, gid: int) -> None:
    if release.is_symlink():
        raise InstallError("release path must not be a symbolic link")
    for directory, child_directories, files in os.walk(release, followlinks=False):
        for path in (
            Path(directory),
            *(Path(directory) / name for name in child_directories),
        ):
            if path.is_symlink():
                continue
            info = path.stat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise InstallError("release directory ownership or mode is unsafe")
            if not _permits(info, uid, gid, 0o1):
                raise InstallError("service account cannot traverse release directory")
        for name in files:
            path = Path(directory) / name
            info = path.stat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise InstallError("release file ownership or mode is unsafe")
            if not _permits(info, uid, gid, 0o4):
                raise InstallError("service account cannot read release file")
            if info.st_mode & 0o111 and not _permits(info, uid, gid, 0o1):
                raise InstallError("service account cannot execute release file")


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


def _install_release(
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
                "preserve it for operator review"
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
        raise InstallError("could not read approved source release") from exc
    try:
        with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as bundle:
            bundle.extractall(release, filter="data")
    except (OSError, tarfile.TarError) as exc:
        raise InstallError("could not extract the approved source release") from exc
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
    _run([str(python), "-m", "compileall", "-q", str(release / "src")])
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


def _install_current_link(release: Path) -> None:
    current = BASE / "current"
    if current.is_symlink():
        if current.resolve() != release.resolve():
            raise InstallError(
                "current points to another release; refusing to replace it"
            )
        return
    if current.exists():
        raise InstallError("current exists and is not the approved release symlink")
    temporary = BASE / ".current.installing"
    if temporary.exists() or temporary.is_symlink():
        raise InstallError(
            "temporary current symlink already exists; inspect it manually"
        )
    temporary.symlink_to(release)
    os.replace(temporary, current)
    os.chown(current, 0, 0, follow_symlinks=False)


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
) -> None:
    if os.geteuid() != 0:
        raise InstallError("run this installation script as an administrator")
    _check_repository(repo, release_sha)
    validate_inputs(config_source, credential_source)
    _require_supported_runtime(Path(sys.executable))
    for unit in UNIT_FILES:
        for verb in ("is-active", "is-enabled"):
            result = subprocess.run(
                ["systemctl", verb, unit],
                text=True,
                capture_output=True,
            )
            if result.returncode == 0:
                raise InstallError(
                    "a target service or timer is already active or enabled"
                )

    uid, gid = _account()
    configured_logo = _configured_logo_path(config_source)
    if configured_logo is not None:
        _validate_logo_asset(configured_logo, uid, gid)
    for path, mode, owner, group in (
        (BASE, 0o755, 0, 0),
        (BASE / "releases", 0o755, 0, 0),
        (BASE / "bin", 0o755, 0, 0),
        (ETC, 0o750, 0, gid),
        (STATE, 0o700, uid, gid),
        (STATE / "backups", 0o700, uid, gid),
    ):
        _mkdir_owned(path, mode, owner, group)

    release = BASE / "releases" / release_sha
    _install_release(repo, release, uid, gid, release_sha)
    _install_current_link(release)

    config = ETC / "config"
    config_temp = config.with_name(".config.installing")
    shutil.copyfile(config_source, config_temp)
    os.chown(config_temp, 0, gid)
    os.chmod(config_temp, 0o640)
    os.replace(config_temp, config)
    _copy_secret(credential_source, ETC / "discord-token")

    deploy = repo / "deploy"
    _copy_unit(deploy / "systemd/armory-launch", BASE / "bin/armory-launch", 0o755)
    _copy_unit(
        deploy / "systemd/armory-db-preflight.py",
        BASE / "bin/armory-db-preflight",
        0o755,
    )
    systemd_directory = Path("/etc/systemd/system")
    for name in UNIT_FILES:
        _copy_unit(deploy / "systemd" / name, systemd_directory / name)
    _run(
        [
            "systemd-analyze",
            "verify",
            *[str(systemd_directory / name) for name in UNIT_FILES],
        ]
    )
    _run(["systemctl", "daemon-reload"])
    # Intentionally no enable, start, or restart commands in this installer.


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--config-source", required=True, type=Path)
    parser.add_argument("--credential-source", required=True, type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    try:
        install(
            repo,
            args.config_source,
            args.credential_source,
            args.release_sha,
        )
    except InstallError as exc:
        print(f"Installation stopped safely: {exc}", file=sys.stderr)
        return 1
    print("Release installed; units remain disabled and stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
