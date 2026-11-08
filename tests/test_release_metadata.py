"""Keep package metadata and installer release branch aligned."""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

from deploy.production_install import BRANCH


class ReleaseMetadataTests(unittest.TestCase):
    def test_v1_package_version_and_release_branch(self) -> None:
        project = tomllib.loads(
            (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        self.assertEqual(project["version"], "1.0.0")
        self.assertEqual(BRANCH, "release/v1.0.0")


if __name__ == "__main__":
    unittest.main()
