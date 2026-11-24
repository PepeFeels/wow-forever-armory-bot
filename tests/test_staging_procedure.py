"""Unprivileged regression tests for the documented bundle staging flow."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

GIT = "/usr/bin/git"
BRANCH = "release/v2.0.0"
ORIGIN = "https://github.com/PepeFeels/wow-forever-armory-bot.git"
ALLOWED_CONFIG_KEYS = {
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
    "branch.release/v2.0.0.remote",
    "branch.release/v2.0.0.merge",
}


@unittest.skipIf(os.geteuid() == 0, "must run in an unprivileged test process")
class StagingProcedureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="armory-staging-test-")
        self.root = Path(self.temporary.name)
        self.home = self.root / "clean-home"
        self.home.mkdir()
        self.git_tmp = self.root / "git-tmp"
        self.git_tmp.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _env(self, *, file_only: bool = False) -> dict[str, str]:
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.home),
            "TMPDIR": str(self.git_tmp),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }
        if file_only:
            env["GIT_ALLOW_PROTOCOL"] = "file"
        return env

    def _run(
        self,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        input_data: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            args,
            env=env if env is not None else self._env(),
            input=input_data,
            capture_output=True,
            check=check,
        )

    def _git(
        self,
        repository: Path,
        *args: str,
        file_only: bool = False,
        input_data: bytes | None = None,
    ) -> bytes:
        return self._run(
            [
                GIT,
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "credential.helper=",
                "-C",
                str(repository),
                *args,
            ],
            env=self._env(file_only=file_only),
            input_data=input_data,
        ).stdout.strip()

    def _source_bundle(
        self, *, symlink: bool = False
    ) -> tuple[Path, str, tuple[Path, Path, Path]]:
        source = self.root / ("source-with-link" if symlink else "source")
        source.mkdir()
        self._run(
            [GIT, "init", "--initial-branch", BRANCH, str(source)],
            env=self._env(),
        )
        self._run([GIT, "-C", str(source), "config", "user.name", "Fixture"])
        self._run(
            [GIT, "-C", str(source), "config", "user.email", "fixture@example.invalid"]
        )
        filter_marker = self.root / "developer-filter-executed"
        hook_marker = self.root / "developer-hook-executed"
        credential_marker = self.root / "developer-credential-helper-executed"
        (source / "deploy").mkdir()
        script = source / "deploy/production_preflight.py"
        script.write_text("print('fixture preflight')\n", encoding="ascii")
        (source / ".gitattributes").write_text(
            "payload.txt filter.fixture\n", encoding="ascii"
        )
        (source / "payload.txt").write_text("ordinary bundle data\n", encoding="ascii")
        self._run(
            [
                GIT,
                "-C",
                str(source),
                "config",
                "filter.fixture.smudge",
                f"/usr/bin/touch {filter_marker}",
            ]
        )
        hook_dir = self.root / "developer-hooks"
        hook_dir.mkdir()
        hook = hook_dir / "post-checkout"
        hook.write_text(f"#!/bin/sh\n/usr/bin/touch {hook_marker}\n", encoding="ascii")
        hook.chmod(0o755)
        self._run([GIT, "-C", str(source), "config", "core.hooksPath", str(hook_dir)])
        self._run(
            [
                GIT,
                "-C",
                str(source),
                "config",
                "credential.helper",
                f"!/usr/bin/touch {credential_marker}",
            ]
        )
        if symlink:
            (source / "external-link").symlink_to(self.root / "outside-target")
        self._run([GIT, "-C", str(source), "add", "."])
        self._run([GIT, "-C", str(source), "commit", "-m", "fixture"])
        commit = (
            self._run([GIT, "-C", str(source), "rev-parse", "HEAD"])
            .stdout.decode()
            .strip()
        )
        bundle = self.root / ("linked.bundle" if symlink else "valid.bundle")
        self._run(
            [
                GIT,
                "-C",
                str(source),
                "bundle",
                "create",
                str(bundle),
                f"refs/heads/{BRANCH}",
            ]
        )
        return bundle, commit, (filter_marker, hook_marker, credential_marker)

    @staticmethod
    def _validate_tree(tree: bytes) -> None:
        for record in tree.split(b"\0"):
            if not record:
                continue
            metadata, path = record.split(b"\t", 1)
            mode, kind, _object_id = metadata.split(b" ", 2)
            parts = path.split(b"/")
            if (
                path.startswith(b"/")
                or any(part in {b"", b".", b".."} for part in parts)
                or any(part.lower() == b".git" for part in parts)
            ):
                raise ValueError("unsafe path in Git tree")
            if kind != b"blob" or mode not in {b"100644", b"100755"}:
                raise ValueError("symlink or special file in Git tree")

    def _import_bundle(
        self,
        stage_root: Path,
        bundle: Path,
        commit: str,
        expected_bundle_sha256: str,
    ) -> tuple[Path, Path]:
        stage_root.mkdir(parents=True, exist_ok=True)
        stage = stage_root / commit
        stage.mkdir(mode=0o700)
        self.git_tmp = stage / "tmp"
        self.git_tmp.mkdir(mode=0o700)
        copied_bundle = stage / "source.bundle"
        shutil.copyfile(bundle, copied_bundle)
        copied_bundle.chmod(0o400)
        actual_bundle_sha256 = hashlib.sha256(copied_bundle.read_bytes()).hexdigest()
        if actual_bundle_sha256 != expected_bundle_sha256:
            raise ValueError("bundle hash mismatch")
        if copied_bundle.stat().st_mode & 0o777 != 0o400:
            raise ValueError("copied bundle has unsafe permissions")

        candidate = stage / "candidate"
        self._run(
            [
                GIT,
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "credential.helper=",
                "init",
                "--initial-branch=staging-import",
                str(candidate),
            ],
            env=self._env(),
        )
        for key, value in (
            ("core.hooksPath", "/dev/null"),
            ("core.fsmonitor", "false"),
            ("core.symlinks", "false"),
            ("credential.helper", ""),
        ):
            self._git(candidate, "config", "--local", key, value)

        self._git(candidate, "bundle", "verify", str(copied_bundle))
        self._git(
            candidate,
            "fetch",
            str(copied_bundle),
            f"refs/heads/{BRANCH}:refs/remotes/staging/{BRANCH}",
            file_only=True,
        )
        self._validate_tree(
            self._git(candidate, "ls-tree", "-rz", "--full-tree", "-r", commit)
        )
        self._git(
            candidate,
            "switch",
            "--create",
            BRANCH,
            f"refs/remotes/staging/{BRANCH}",
        )
        self._git(candidate, "remote", "add", "origin", ORIGIN)

        config_keys = set(
            self._git(candidate, "config", "--local", "--name-only", "--list")
            .decode()
            .splitlines()
        )
        if not config_keys <= ALLOWED_CONFIG_KEYS:
            raise ValueError("unexpected local Git configuration key")
        self.assertEqual(
            self._git(candidate, "config", "--local", "--get", "core.hooksPath"),
            b"/dev/null",
        )
        self.assertEqual(
            self._git(candidate, "config", "--local", "--get", "core.fsmonitor"),
            b"false",
        )
        self.assertEqual(
            self._git(candidate, "config", "--local", "--get", "core.symlinks"),
            b"false",
        )
        self.assertEqual(
            self._git(candidate, "config", "--local", "--get-all", "credential.helper"),
            b"",
        )
        self.assertEqual(self._git(candidate, "rev-parse", "HEAD").decode(), commit)
        self.assertEqual(
            self._git(candidate, "rev-parse", f"refs/heads/{BRANCH}").decode(),
            commit,
        )
        self.assertEqual(
            self._git(candidate, "branch", "--show-current").decode(), BRANCH
        )
        self.assertEqual(
            self._git(candidate, "status", "--porcelain", "--untracked-files=all"),
            b"",
        )
        self.assertEqual(
            self._git(candidate, "remote", "get-url", "origin").decode(), ORIGIN
        )

        checkout_paths = [candidate]
        for directory, children, files in os.walk(candidate, followlinks=False):
            for name in children + files:
                path = Path(directory) / name
                if path.is_symlink():
                    raise ValueError("symlink in staged checkout")
                if path.stat().st_uid != os.getuid():
                    raise ValueError("unexpected staged owner")
                checkout_paths.append(path)
        for path in checkout_paths:
            path.chmod(path.stat().st_mode & ~0o222)
            if path.stat().st_mode & 0o022:
                raise ValueError("staged path is group/world writable")
        if stage.stat().st_mode & 0o777 != 0o700:
            raise ValueError("staging directory has unsafe permissions")
        return stage, candidate

    def test_bundle_import_branch_config_hash_and_repeat_safety(self) -> None:
        bundle, commit, command_markers = self._source_bundle()
        bundle_hash = hashlib.sha256(bundle.read_bytes()).hexdigest()
        stage, candidate = self._import_bundle(
            self.root / "staging", bundle, commit, bundle_hash
        )
        script = candidate / "deploy/production_preflight.py"
        expected_script_hash = hashlib.sha256(script.read_bytes()).hexdigest()
        staged_script = stage / "production_preflight.py"
        shutil.copyfile(script, staged_script)
        staged_script.chmod(0o444)
        self.assertEqual(staged_script.stat().st_mode & 0o777, 0o444)
        self.assertEqual(
            hashlib.sha256(staged_script.read_bytes()).hexdigest(), expected_script_hash
        )
        self.assertFalse(
            any(marker.exists() for marker in command_markers),
            "developer hook, filter, or credential helper executed",
        )

        staged_before = hashlib.sha256(staged_script.read_bytes()).hexdigest()
        with self.assertRaises(FileExistsError):
            (self.root / "staging" / commit).mkdir()
        self.assertEqual(
            hashlib.sha256(staged_script.read_bytes()).hexdigest(), staged_before
        )

    def test_bundle_hash_mismatch_stops_before_git_init(self) -> None:
        bundle, commit, _command_markers = self._source_bundle()
        with self.assertRaisesRegex(ValueError, "bundle hash mismatch"):
            self._import_bundle(self.root / "bad-hash", bundle, commit, "0" * 64)
        self.assertFalse((self.root / "bad-hash" / commit / "candidate").exists())

    def test_symlink_bundle_is_rejected_before_checkout(self) -> None:
        bundle, commit, command_markers = self._source_bundle(symlink=True)
        stage_root = self.root / "symlink-stage"
        stage_root.mkdir()
        stage = stage_root / commit
        stage.mkdir()
        self.git_tmp = stage / "tmp"
        self.git_tmp.mkdir(mode=0o700)
        copied = stage / "source.bundle"
        shutil.copyfile(bundle, copied)
        candidate = stage / "candidate"
        self._run(
            [
                GIT,
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "init",
                "--initial-branch=staging-import",
                str(candidate),
            ],
            env=self._env(),
        )
        self._git(candidate, "config", "--local", "core.symlinks", "false")
        self._git(
            candidate,
            "fetch",
            str(copied),
            f"refs/heads/{BRANCH}:refs/remotes/staging/{BRANCH}",
            file_only=True,
        )
        tree = self._git(candidate, "ls-tree", "-rz", "--full-tree", "-r", commit)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self._validate_tree(tree)
        self.assertFalse((candidate / "external-link").exists())
        self.assertFalse((self.root / "outside-target").exists())
        self.assertFalse(any(marker.exists() for marker in command_markers))

    def test_handcrafted_traversal_tree_is_rejected_before_checkout(self) -> None:
        source = self.root / "malformed-source"
        source.mkdir()
        self._run([GIT, "init", "--initial-branch", BRANCH, str(source)])
        self._run([GIT, "-C", str(source), "config", "user.name", "Fixture"])
        self._run(
            [GIT, "-C", str(source), "config", "user.email", "fixture@example.invalid"]
        )
        blob = (
            self._run(
                [GIT, "-C", str(source), "hash-object", "-w", "--stdin"],
                input_data=b"outside data",
            )
            .stdout.strip()
            .decode()
        )
        raw_tree = b"100644 ../outside\0" + bytes.fromhex(blob)
        tree = (
            self._run(
                [
                    GIT,
                    "-C",
                    str(source),
                    "hash-object",
                    "--literally",
                    "-w",
                    "-t",
                    "tree",
                    "--stdin",
                ],
                input_data=raw_tree,
            )
            .stdout.strip()
            .decode()
        )
        commit_text = (
            f"tree {tree}\n"
            "author Fixture <fixture@example.invalid> 1700000000 +0000\n"
            "committer Fixture <fixture@example.invalid> 1700000000 +0000\n\n"
            "malformed tree\n"
        ).encode()
        commit = (
            self._run(
                [
                    GIT,
                    "-C",
                    str(source),
                    "hash-object",
                    "--literally",
                    "-w",
                    "-t",
                    "commit",
                    "--stdin",
                ],
                input_data=commit_text,
            )
            .stdout.strip()
            .decode()
        )
        self._run(
            [GIT, "-C", str(source), "update-ref", f"refs/heads/{BRANCH}", commit]
        )
        bundle = self.root / "malformed-tree.bundle"
        self._run(
            [
                GIT,
                "-C",
                str(source),
                "bundle",
                "create",
                str(bundle),
                f"refs/heads/{BRANCH}",
            ]
        )
        stage = self.root / "traversal-stage" / commit
        stage.mkdir(parents=True)
        self.git_tmp = stage / "tmp"
        self.git_tmp.mkdir(mode=0o700)
        candidate = stage / "candidate"
        self._run(
            [GIT, "init", "--initial-branch=staging-import", str(candidate)],
            env=self._env(),
        )
        self._git(
            candidate,
            "fetch",
            str(bundle),
            f"refs/heads/{BRANCH}:refs/remotes/staging/{BRANCH}",
            file_only=True,
        )
        tree_listing = self._git(
            candidate, "ls-tree", "-rz", "--full-tree", "-r", commit
        )
        with self.assertRaisesRegex(ValueError, "unsafe path"):
            self._validate_tree(tree_listing)
        self.assertFalse((self.root / "outside").exists())
        self.assertFalse((candidate / "outside").exists())

    def test_git_metadata_path_is_rejected(self) -> None:
        malicious_record = b"100644 blob " + b"0" * 40 + b"\t.git\0"
        with self.assertRaisesRegex(ValueError, "unsafe path"):
            self._validate_tree(malicious_record)


if __name__ == "__main__":
    unittest.main()
