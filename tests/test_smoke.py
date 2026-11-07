"""The opt-in live command is tested without making a request."""

import io
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from unittest.mock import patch

from wow_forever_armory.client import ArmoryClient
from wow_forever_armory.models import Character
from wow_forever_armory.smoke import _run


class SmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_displays_aware_utc_timestamp(self) -> None:
        character = Character(
            name="Pepe",
            armory_updated_at=datetime(2026, 11, 6, 23, 21, 23, tzinfo=UTC),
        )
        output = io.StringIO()
        with (
            patch.object(ArmoryClient, "fetch_character", return_value=character),
            redirect_stdout(output),
        ):
            result = await _run(
                "en-us/forever/eu/armory/character/pepe-feels",
                "https://example.org",
            )
        self.assertEqual(result, 0)
        self.assertIn("Last updated: 2026-11-06T23:21:23+00:00", output.getvalue())
