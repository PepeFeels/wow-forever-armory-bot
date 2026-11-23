"""Check package version and the v2.0.0 installer release gate."""

from __future__ import annotations

import ast
import hashlib
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
            "deployment-state.json",
            "database_signature",
            "database_backup_sha256",
            "shared_preflight_sha256",
            "ef1bba2bc30f0748cad943040109a1bc0838df07b6430ec5e5a6988577655c51",
            "57a25089cd62cee224a6c5260fba53874969409132b8ea758716f64b03c233dc",
            "load_reviewed_python_source",
            "verify_database_snapshot",
            "target in {previous_path, approved_path}",
        ):
            self.assertIn(required, source)
        self.assertNotIn(
            "from deploy.production_db_snapshot import verify_database_snapshot",
            source,
        )
        self.assertNotIn('/opt/wow-forever-armory-bot/deployment-checkout")', source)
        for forbidden in ("os.replace(", ".unlink(", ".write_text(", "restore_backup("):
            self.assertNotIn(forbidden, source)
        self.assertIn("same-filesystem rename", operations)
        self.assertIn("sudo mv -T", operations)
        self.assertIn("only query state", operations)
        self.assertIn("keep the system stopped", operations)

    def test_preflight_documented_hash_matches_committed_source(self) -> None:
        root = Path(__file__).parents[1]
        operations = (root / "OPERATIONS.md").read_text(encoding="utf-8")
        expected = "e4029e9982d07cac54bac0fc011391de70fc36c0fcac617a747ff3260849a9fa"
        actual = hashlib.sha256(
            (root / "deploy/production_preflight.py").read_bytes()
        ).hexdigest()
        self.assertEqual(actual, expected)
        self.assertIn(expected, operations)
        self.assertNotIn(
            "17c7537cf30c2208ff0242978a43d92910992c136cb92840f79180296d738883",
            operations,
        )

    def test_reviewed_admin_source_hash_matches_root_loader_pin(self) -> None:
        root = Path(__file__).parents[1]
        operations = (root / "OPERATIONS.md").read_text(encoding="utf-8")
        match = re.search(r'ADMIN_SHA256 = "([0-9a-f]{64})"', operations)
        self.assertIsNotNone(match)
        actual = hashlib.sha256(
            (root / "deploy/production_admin.py").read_bytes()
        ).hexdigest()
        self.assertEqual(match.group(1), actual)

    def test_operations_documents_cache_free_bundle_checkout_and_admin_tool(
        self,
    ) -> None:
        root = Path(__file__).parents[1]
        operations = (root / "OPERATIONS.md").read_text(encoding="utf-8")
        for required in (
            "clone --no-local",
            "deployment-checkout-v1.1.4-a1a6a2ad95302f7e3c4900f18e91691f43631e18",
            "production_admin.py",
            "ADMIN_SHA256",
            "def load_source(path, expected_sha256)",
            "does not import any candidate module",
            "provision-pin",
            "acknowledge-recovery",
            "idempotent success",
            "archive alone after a prior interruption",
            "atomic no-replace",
            "temporary acknowledgment file",
            "never replace",
        ):
            self.assertIn(required, operations)
        self.assertNotIn("from deploy.production_admin import", operations)

    def test_operations_documents_exact_python_314_venv_symlink_allowlist(
        self,
    ) -> None:
        root = Path(__file__).parents[1]
        operations = (root / "OPERATIONS.md").read_text(encoding="utf-8")
        for required in (
            "`.venv/lib64`",
            "`.venv/bin/python`",
            "`.venv/bin/python3`",
            "`.venv/bin/python3.14`",
            "`.venv/bin/𝜋thon`",
            "All other symlinks, including links",
            "standard `venv` module",
        ):
            self.assertIn(required, operations)


if __name__ == "__main__":
    unittest.main()
