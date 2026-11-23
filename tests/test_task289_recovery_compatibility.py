from __future__ import annotations

import hashlib
import ast
import unittest
from pathlib import Path

from deploy import production_admin, recovery_audit
from deploy import production_preflight, production_wrapper


class Task289RecoveryCompatibilityTests(unittest.TestCase):
    def test_validator_hash_matches_all_runtime_pins(self) -> None:
        validator_hash = hashlib.sha256(
            Path(recovery_audit.__file__).read_bytes()
        ).hexdigest()
        self.assertEqual(production_admin.RECOVERY_AUDIT_SHA256, validator_hash)
        self.assertEqual(production_preflight.RECOVERY_AUDIT_SHA256, validator_hash)
        self.assertEqual(production_wrapper.RECOVERY_AUDIT_SHA256, validator_hash)
        systemd_source = Path("deploy/systemd/armory-db-preflight.py").read_text()
        self.assertIn(f'"{validator_hash}"', systemd_source)

    def test_preflight_hash_pins_the_exact_systemd_source(self) -> None:
        self.assertEqual(
            production_preflight.APPROVED_PREFLIGHT_SHA256,
            hashlib.sha256(
                Path("deploy/systemd/armory-db-preflight.py").read_bytes()
            ).hexdigest(),
        )

    def test_recovery_review_filename_binds_journal_and_review_hash(self) -> None:
        journal_sha = "a" * 64
        review_sha = "b" * 64
        match = recovery_audit.REVIEW.fullmatch(
            f"recovery-review-{journal_sha}-{review_sha}.json"
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.groups(), (journal_sha, review_sha))

    def test_recovery_review_rejects_malformed_hash_binding(self) -> None:
        self.assertIsNone(
            recovery_audit.REVIEW.fullmatch(
                f"recovery-review-{'a' * 64}-{'B' * 64}.json"
            )
        )


if __name__ == "__main__":
    unittest.main()
