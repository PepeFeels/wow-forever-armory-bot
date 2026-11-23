from __future__ import annotations

import fcntl
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from deploy import production_admin as admin


class Task287NestedAdministrativeLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "audit"
        self.directory.mkdir(mode=0o700)
        self.directory.chmod(0o700)
        self.preflight = self.directory / "armory-db-preflight.test.py"
        self.raw = b"protected preflight fixture\n"
        self.preflight.write_bytes(self.raw)
        self.preflight.chmod(0o700)
        self.owner = os.getuid()
        self.group = os.getgid()

    def _hash(self, *, locked_fd: int | None = None) -> str:
        return admin._hash_regular_file(
            self.preflight,
            owner_uid=self.owner,
            owner_gid=self.group,
            mode=0o700,
            parent_mode=0o700,
            locked_directory_fd=locked_fd,
        )

    def test_reproduces_prod_nested_lock_error_and_reuses_held_lock(self) -> None:
        directory_fd = admin._open_private_directory(
            self.directory, owner_uid=self.owner, exact_mode=0o700
        )
        try:
            # Exact pre-fix code path: _hash_regular_file opens the same parent
            # directory again and its independent flock description conflicts.
            with self.assertRaisesRegex(
                admin.AdminError, "another administrative operation holds the directory"
            ):
                self._hash()

            # Fixed path reuses and verifies the already-held exclusive lock.
            self.assertEqual(self._hash(locked_fd=directory_fd), hashlib.sha256(self.raw).hexdigest())
        finally:
            os.close(directory_fd)

    def test_reuse_rejects_a_different_locked_directory(self) -> None:
        other = Path(self.temp.name) / "other"
        other.mkdir(mode=0o700)
        other.chmod(0o700)
        directory_fd = admin._open_private_directory(
            other, owner_uid=self.owner, exact_mode=0o700
        )
        try:
            with self.assertRaisesRegex(admin.AdminError, "directory path changed"):
                self._hash(locked_fd=directory_fd)
        finally:
            os.close(directory_fd)

    def test_competing_process_lock_is_still_fail_closed(self) -> None:
        child_code = (
            "import fcntl,os,sys; "
            "fd=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY); "
            "fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); "
            "print('LOCK_HELD',flush=True); sys.stdin.readline(); os.close(fd)"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", child_code, str(self.directory)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdout is not None
            self.assertEqual(child.stdout.readline().strip(), "LOCK_HELD")
            with self.assertRaisesRegex(
                admin.AdminError, "another administrative operation holds the directory"
            ):
                admin._open_private_directory(
                    self.directory, owner_uid=self.owner, exact_mode=0o700
                )
        finally:
            if child.poll() is None:
                assert child.stdin is not None
                child.stdin.write("release\n")
                child.stdin.flush()
                child.wait(timeout=5)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()


if __name__ == "__main__":
    unittest.main()
