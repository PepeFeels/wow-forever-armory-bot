"""Check package version and the v1.1.4 installer release gate."""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

from deploy.production_install import BASE_CHECKPOINT_SHA, BRANCH


class ReleaseMetadataTests(unittest.TestCase):
    def test_package_version_is_v1_1_4(self) -> None:
        project = tomllib.loads(
            (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        self.assertEqual(project["version"], "1.1.4")

    def test_installer_release_gate_matches_v1_1_4(self) -> None:
        self.assertEqual(BRANCH, "release/v1.1.4")
        self.assertEqual(
            BASE_CHECKPOINT_SHA, "c99e21f9afc6b9e4d5df0a8668a31f9ab9031dce"
        )

    def test_empty_database_rollback_keeps_compatible_preflight(self) -> None:
        operations = (
            Path(__file__).parents[1] / "OPERATIONS.md"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "Application rollback and preflight rollback are separate", operations
        )
        self.assertIn("never restore\nthe saved v1.1.2 preflight", operations)
        self.assertIn(
            "c1c4f067fa2b465726d374ecb06acfae82a1ec234dc0f38c4734a6c422089803",
            operations,
        )
        self.assertIn("keep\nthe service stopped", operations)


if __name__ == "__main__":
    unittest.main()
