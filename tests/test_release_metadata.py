"""Check package version and the v1.1.3 installer release gate."""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

from deploy.production_install import BASE_CHECKPOINT_SHA, BRANCH


class ReleaseMetadataTests(unittest.TestCase):
    def test_package_version_is_v1_1_3(self) -> None:
        project = tomllib.loads(
            (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        self.assertEqual(project["version"], "1.1.3")

    def test_installer_release_gate_matches_v1_1_3(self) -> None:
        self.assertEqual(BRANCH, "release/v1.1.3")
        self.assertEqual(
            BASE_CHECKPOINT_SHA, "0cb6377c1cb89e2594974e1c547c7d886ba7b8d4"
        )


if __name__ == "__main__":
    unittest.main()
