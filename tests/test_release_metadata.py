"""Check package version and the v2.0.0 installer release gate."""

from __future__ import annotations

import ast
import re
import tomllib
import unittest
from pathlib import Path

from deploy.production_install import BASE_CHECKPOINT_SHA, BRANCH


class ReleaseMetadataTests(unittest.TestCase):
    def test_package_version_is_v2_0_0(self) -> None:
        project = tomllib.loads(
            (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        self.assertEqual(project["version"], "2.0.0")

    def test_installer_release_gate_matches_v2_0_0(self) -> None:
        self.assertEqual(BRANCH, "release/v2.0.0")
        self.assertEqual(
            BASE_CHECKPOINT_SHA, "a1a6a2ad95302f7e3c4900f18e91691f43631e18"
        )

    def test_build_backend_version_matches_hash_locked_build_dependency(self) -> None:
        root = Path(__file__).parents[1]
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["build-system"]["requires"], ["setuptools==84.1.0"])
        lock = (root / "deploy/requirements-build.lock").read_text(encoding="utf-8")
        self.assertIn("setuptools==84.1.0", lock)
        self.assertIn(
            "--hash=sha256:5231689c6ac8156cd155cfb98c3fd6fb252c3e7e9bd3d78b64d3c55f132624c9",
            lock,
        )

    def test_empty_database_rollback_keeps_compatible_preflight(self) -> None:
        operations = (Path(__file__).parents[1] / "OPERATIONS.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "Application rollback and preflight rollback are separate", operations
        )
        self.assertIn("never restore\nthe saved v1.1.2 preflight", operations)
        self.assertIn(
            "c1c4f067fa2b465726d374ecb06acfae82a1ec234dc0f38c4734a6c422089803",
            operations,
        )
        self.assertIn("keep\nthe service stopped", operations)

    def test_manual_recovery_preflight_is_read_only_and_covers_switched_pointer(
        self,
    ) -> None:
        operations = (Path(__file__).parents[1] / "OPERATIONS.md").read_text(
            encoding="utf-8"
        )
        match = re.search(
            r"sudo /usr/bin/python3\.14 -I -B - <<'PY'\n(.*?)\nPY\n```",
            operations,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        source = match.group(1)
        ast.parse(source)
        for required in (
            "deployment-pin.json",
            "deployment-state.json",
            "database_signature",
            "database_backup_sha256",
            "shared_preflight_sha256",
            "verify_database_snapshot",
            "target in {previous_path, approved_path}",
        ):
            self.assertIn(required, source)
        for forbidden in ("os.replace(", ".unlink(", ".write_text(", "restore_backup("):
            self.assertNotIn(forbidden, source)
        self.assertIn("same-filesystem rename", operations)
        self.assertIn("sudo mv -T", operations)
        self.assertIn("only query state", operations)
        self.assertIn("keep the system stopped", operations)


if __name__ == "__main__":
    unittest.main()
