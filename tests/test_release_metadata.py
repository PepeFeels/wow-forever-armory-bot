"""Keep package metadata and installer release branch aligned."""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

from deploy.production_install import BASE_CHECKPOINT_SHA, BRANCH


class ReleaseMetadataTests(unittest.TestCase):
    def test_v1_1_package_version_and_release_branch(self) -> None:
        project = tomllib.loads(
            (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        self.assertEqual(project["version"], "1.1.0")
        self.assertEqual(BRANCH, "feature/v1.1.0")
        self.assertEqual(
            BASE_CHECKPOINT_SHA, "4ac05712259e26560d1818746f5cee90e6793f78"
        )


if __name__ == "__main__":
    unittest.main()
