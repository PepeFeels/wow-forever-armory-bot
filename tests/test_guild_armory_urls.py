"""Armory URL allowlist/path validation tests; no HTTP requests are made."""

import unittest
from unittest.mock import MagicMock

from wow_forever_armory.client import ArmoryClient, _NoRedirect
from wow_forever_armory.guild_armory_urls import validate_armory_url


class ArmoryUrlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ArmoryClient()

    def test_accepts_official_character_urls_and_normalizes_unicode(self) -> None:
        self.assertEqual(
            validate_armory_url(
                "https://worldofwarcraft.blizzard.com/en-us/forever/eu/armory/character/%C3%89owyn",
                self.client,
            ),
            "en-us/forever/eu/armory/character/%C3%89owyn",
        )
        self.assertEqual(
            validate_armory_url(
                "https://WORLDofWARCRAFT.BLIZZARD.COM/en-us/forever/EU/armory/character/Ayla%20Star",
                self.client,
            ),
            "en-us/forever/eu/armory/character/Ayla%20Star",
        )

    def test_rejects_non_official_or_unsafe_urls_before_core_fetch(self) -> None:
        invalid = (
            "http://worldofwarcraft.blizzard.com/en-us/forever/eu/armory/character/a",
            "https://worldofwarcraft.blizzard.com.evil.example/en-us/forever/eu/armory/character/a",
            "https://user@worldofwarcraft.blizzard.com/en-us/forever/eu/armory/character/a",
            "https://worldofwarcraft.blizzard.com:444/en-us/forever/eu/armory/character/a",
            "https://worldofwarcraft.blizzard.com/en-us/forever/eu/armory/character/a?x=1",
            "https://worldofwarcraft.blizzard.com/en-us/forever/eu/armory/character/a#fragment",
            "https://worldofwarcraft.blizzard.com/en-us/forever/eu/armory/character/%2e%2e",
            "https://worldofwarcraft.blizzard.com/en-us/forever/eu/armory/character/%252e%252e",
            "https://worldofwarcraft.blizzard.com/en-us/forever/eu/armory/character/%GG",
            "https://worldofwarcraft.blizzard.com/en-us/forever/eu/profile/a",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_armory_url(value, self.client)

    def test_core_url_guard_is_applied_after_allowlist_validation(self) -> None:
        client = MagicMock(spec=ArmoryClient)
        client._url.side_effect = ValueError("unsafe relative path")
        with self.assertRaises(ValueError):
            validate_armory_url(
                "https://worldofwarcraft.blizzard.com/en-us/forever/eu/armory/character/a",
                client,
            )
        client._url.assert_called_once_with("en-us/forever/eu/armory/character/a")

    def test_core_refuses_all_redirects_without_following_location(self) -> None:
        handler = _NoRedirect()
        request = MagicMock()
        result = handler.redirect_request(
            request,
            MagicMock(),
            302,
            "Found",
            MagicMock(),
            "https://evil.example/steal",
        )
        self.assertIsNone(result)
