"""Check package version and the v1.1.2 installer release gate."""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

from deploy.production_install import BASE_CHECKPOINT_SHA, BRANCH


class ReleaseMetadataTests(unittest.TestCase):
    def test_package_version_is_v1_1_2(self) -> None:
        project = tomllib.loads(
            (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        self.assertEqual(project["version"], "1.1.2")

    def test_installer_release_gate_matches_v1_1_2(self) -> None:
        self.assertEqual(BRANCH, "release/v1.1.2")
        self.assertEqual(
            BASE_CHECKPOINT_SHA, "de1ffb89a366ae1e4ba73db2a30804698d218e40"
        )


if __name__ == "__main__":
    unittest.main()
