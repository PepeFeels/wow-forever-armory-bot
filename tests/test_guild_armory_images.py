"""Image renderer tests use bundled assets and local fonts only."""

import unittest
from datetime import UTC, datetime
from io import BytesIO

from PIL import Image, ImageDraw

from wow_forever_armory.guild_armory_images import (
    _INTRO_COPY,
    _font,
    render_intro_panel,
    render_member_row,
)
from wow_forever_armory.guild_armory_store import StoredCharacter
from wow_forever_armory.models import Character


class GuildArmoryImageTests(unittest.TestCase):
    def test_intro_copy_is_english_and_explains_registration_controls(self) -> None:
        self.assertTrue(all(text.isascii() for text in _INTRO_COPY))
        copy = " ".join(_INTRO_COPY)
        self.assertEqual(
            _INTRO_COPY,
            (
                "GUILD ARMORY",
                "Register your characters",
                "1. Click the Add & Edit button below to open character management.",
                "2. Select Add Character to open the registration form.",
                "3. Paste your full WoW Armory URL and submit the form.",
                "4. Add up to three characters. Use Add & Edit to add, replace, or "
                "remove characters.",
                "Only HTTPS URLs from worldofwarcraft.blizzard.com are accepted.",
            ),
        )
        self.assertNotIn("sword", copy.lower())
        self.assertNotIn("Register Privately", copy)
        draw = ImageDraw.Draw(Image.new("RGB", (1600, 560)))
        for index, line in enumerate(_INTRO_COPY[2:]):
            bounds = draw.textbbox((145, 236 + index * 48), line, font=_font(23))
            self.assertLessEqual(bounds[2], 1500)
            self.assertLessEqual(bounds[3], 485)

    def test_intro_panel_is_a_readable_sized_png(self) -> None:
        result = render_intro_panel()
        self.assertTrue(result.startswith(b"\x89PNG\r\n\x1a\n"))
        with Image.open(BytesIO(result)) as image:
            self.assertEqual(image.size, (1600, 560))

    def test_member_row_handles_long_unicode_and_missing_fields(self) -> None:
        character = Character(name="Éowyn 長い名前 🔥" * 200, level=None)
        entry = StoredCharacter(
            path="en-us/forever/eu/armory/character/example",
            slot=0,
            character=character,
            created_at=datetime.now(UTC).isoformat(),
            updated_at=datetime.now(UTC).isoformat(),
        )
        result = render_member_row("Guild member 名字" * 200, (entry,))
        self.assertTrue(result.startswith(b"\x89PNG\r\n\x1a\n"))
        with Image.open(BytesIO(result)) as image:
            self.assertEqual(image.size, (1800, 480))

    def test_member_row_accepts_three_character_cards(self) -> None:
        entries = tuple(
            StoredCharacter(
                path=f"en-us/forever/eu/armory/character/{index}",
                slot=index,
                character=Character(
                    name=f"Hero {index}",
                    level=60 + index,
                    character_class="Druid",
                    race="Night Elf",
                ),
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            )
            for index in range(3)
        )
        with Image.open(BytesIO(render_member_row("Roster", entries))) as image:
            self.assertEqual(image.size, (1800, 480))

    def test_rejects_empty_or_oversized_rows(self) -> None:
        for entries in ((), (None, None, None, None)):
            with self.subTest(length=len(entries)), self.assertRaises(ValueError):
                render_member_row("Roster", entries)  # type: ignore[arg-type]
