"""Release-pointer safety tests for the systemd database preflight."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).parents[1] / "deploy/systemd/armory-db-preflight.py"
SPEC = importlib.util.spec_from_file_location("armory_db_preflight_test", SOURCE)
assert SPEC is not None and SPEC.loader is not None
PREFLIGHT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREFLIGHT)


class SystemdDatabasePreflightTests(unittest.TestCase):
    def _release(self, base: Path, release_sha: str) -> Path:
        release = base / "releases" / release_sha
        release.mkdir(parents=True)
        marker = release / ".installation-complete"
        marker.write_bytes(release_sha.encode("ascii"))
        marker.chmod(0o444)
        return release

    def test_current_release_accepts_relative_and_canonical_absolute_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            release_sha = "a" * 40
            release = self._release(base, release_sha)
            current = base / "current"
            with patch.object(PREFLIGHT, "INSTALL_BASE", base):
                current.symlink_to(f"releases/{release_sha}")
                self.assertEqual(
                    PREFLIGHT._current_release_sha(expected_owner=os.getuid()),
                    release_sha,
                )
                current.unlink()
                current.symlink_to(release)
                self.assertEqual(
                    PREFLIGHT._current_release_sha(expected_owner=os.getuid()),
                    release_sha,
                )

    def test_current_release_rejects_noncanonical_link_and_bad_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            release_sha = "b" * 40
            release = self._release(base, release_sha)
            current = base / "current"
            with patch.object(PREFLIGHT, "INSTALL_BASE", base):
                for raw in (
                    f"releases//{release_sha}",
                    f"releases/../releases/{release_sha}",
                    str(base / "outside" / release_sha),
                ):
                    current.symlink_to(raw)
                    self.assertIsNone(
                        PREFLIGHT._current_release_sha(expected_owner=os.getuid())
                    )
                    current.unlink()
                current.symlink_to(release)
                marker = release / ".installation-complete"
                marker.chmod(0o644)
                marker.write_text("wrong\n")
                marker.chmod(0o444)
                self.assertIsNone(
                    PREFLIGHT._current_release_sha(expected_owner=os.getuid())
                )


if __name__ == "__main__":
    unittest.main()
