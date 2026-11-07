"""Offline tests for the systemd credential bridge launcher."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

LAUNCHER = Path(__file__).parents[1] / "deploy/systemd/armory-launch"


class SystemdLauncherTests(unittest.TestCase):
    def test_forwards_systemd_credential_to_legacy_environment_interface(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary_path = Path(temp_dir)
            credential_dir = temporary_path / "credentials"
            credential_dir.mkdir()
            credential = credential_dir / "discord-token"
            credential.write_text("fixture-token\n", encoding="utf-8")
            credential.chmod(0o600)

            fake_python = temporary_path / "python"
            fake_python.write_text(
                "#!/bin/sh\n"
                'test "$DISCORD_BOT_TOKEN" = "fixture-token" || exit 41\n'
                'test "$1" = "-m" || exit 42\n'
                'test "$2" = "wow_forever_armory.discord_bot" || exit 43\n'
                'printf "%s\\n" "credential_forwarded=yes"\n',
                encoding="utf-8",
            )
            fake_python.chmod(0o700)

            environment = os.environ.copy()
            environment.pop("DISCORD_BOT_TOKEN", None)
            environment["CREDENTIALS_DIRECTORY"] = str(credential_dir)
            environment["ARMORY_PYTHON"] = str(fake_python)
            result = subprocess.run(
                [str(LAUNCHER)],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "credential_forwarded=yes")
            self.assertNotIn("fixture-token", result.stdout + result.stderr)

    def test_missing_or_empty_credential_fails_without_running_python(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary_path = Path(temp_dir)
            fake_python = temporary_path / "python"
            marker = temporary_path / "interpreter-ran"
            fake_python.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
            fake_python.chmod(0o700)
            credential_dir = temporary_path / "credentials"
            credential_dir.mkdir()

            for credential_contents in (None, ""):
                credential = credential_dir / "discord-token"
                if credential_contents is None:
                    credential.unlink(missing_ok=True)
                else:
                    credential.write_text(credential_contents, encoding="utf-8")
                    credential.chmod(0o600)
                environment = os.environ.copy()
                environment.pop("DISCORD_BOT_TOKEN", None)
                environment["CREDENTIALS_DIRECTORY"] = str(credential_dir)
                environment["ARMORY_PYTHON"] = str(fake_python)
                result = subprocess.run(
                    [str(LAUNCHER)],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=environment,
                )
                self.assertEqual(result.returncode, 78)
                self.assertFalse(marker.exists())
                self.assertNotIn("fixture-token", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
