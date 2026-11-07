import unittest
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError

from test_parser import page

from wow_forever_armory.client import ArmoryClient, ArmoryHTTPError, ArmoryTimeoutError


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_success(self) -> None:
        client = ArmoryClient(max_retries=0)
        with patch.object(
            client,
            "_get_html",
            return_value=page({"character": {"name": "Ayla Star"}}),
        ):
            character = await client.fetch_character(
                "en-us/forever/us/armory/character/example"
            )
        self.assertEqual(character.name, "Ayla Star")

    async def test_timeout(self) -> None:
        client = ArmoryClient(max_retries=1, backoff_base=0)
        with (
            patch.object(client, "_get_html", side_effect=TimeoutError),
            self.assertRaises(ArmoryTimeoutError),
        ):
            await client.fetch_character("character/example")

    async def test_rate_limit_retry_after(self) -> None:
        headers = Message()
        headers["Retry-After"] = "0"
        rate_limit = HTTPError(
            "https://example.org", 429, "Rate limited", headers, None
        )
        client = ArmoryClient(max_retries=1, backoff_base=0)
        with patch.object(
            client,
            "_get_html",
            side_effect=[rate_limit, page({"character": {"name": "Ayla"}})],
        ):
            character = await client.fetch_character("character/example")
        self.assertEqual(character.name, "Ayla")

    async def test_rate_limit_exhausted(self) -> None:
        rate_limit = HTTPError(
            "https://example.org", 429, "Rate limited", Message(), None
        )
        client = ArmoryClient(max_retries=0)
        with (
            patch.object(client, "_get_html", side_effect=rate_limit),
            self.assertRaises(ArmoryHTTPError) as caught,
        ):
            await client.fetch_character("character/example")
        self.assertEqual(caught.exception.status, 429)

    async def test_long_retry_after_is_not_shortened(self) -> None:
        headers = Message()
        headers["Retry-After"] = "120"
        response = HTTPError("https://example.org", 429, "Rate limited", headers, None)
        client = ArmoryClient(max_retries=1, max_retry_delay=30)
        with (
            patch.object(client, "_get_html", side_effect=response),
            self.assertRaisesRegex(ArmoryHTTPError, "retry after 120s"),
        ):
            await client.fetch_character("character/example")

    async def test_invalid_path(self) -> None:
        client = ArmoryClient(base_url="https://example.org/forever/")
        for path in (
            "https://evil.example/x",
            "../private",
            "%2e%2e/private",
            "/absolute",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                await client.fetch_character(path)
