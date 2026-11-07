"""Golden and safety tests for local Guild Armory overview rendering."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import tracemalloc
import unittest
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

from wow_forever_armory.graphic_renderer import (
    UI_LABELS,
    ArmoryGraphicRequest,
    GraphicAssetError,
    GraphicCharacter,
    GraphicStat,
    PvPInfo,
    _character_title,
    _fit_text,
    _freshness_label,
    _is_outdated,
    _load_logo,
    _load_portrait,
    _panel_section_layout,
    _profession_map,
    _skill_value,
    _stat_lines,
    _timestamp_label,
    format_pvp,
    output_size,
    render_armory_overview,
    render_stored_member_overview,
    request_from_member_state,
)
from wow_forever_armory.guild_armory_store import StoredCharacter
from wow_forever_armory.models import Character, Profession, ProfessionTier

FIXTURES = Path(__file__).parent / "fixtures" / "graphic_renderer"
GOLDEN = FIXTURES / "golden_sha256.json"
PREVIEWS = Path("data/ops/previews")


def _snapshot_digest(png: bytes) -> str:
    """Hash layout pixels while excluding the separately licensed logo area."""
    image = Image.open(BytesIO(png)).convert("RGB")
    ImageDraw.Draw(image).rectangle((32, 19, 136, 114), fill=(7, 17, 31))
    return hashlib.sha256(image.tobytes()).hexdigest()


def _character(
    name: str,
    character_class: str,
    *,
    professions: tuple[Profession, ...] = (),
    **overrides: object,
) -> GraphicCharacter:
    values: dict[str, object] = {
        "name": name,
        "level": 60,
        "race": "Night Elf",
        "character_class": character_class,
        "specialization": None,
        "talent_points_spent": 51,
        "professions": professions,
        "last_updated": None,
        "cached": True,
    }
    values.update(overrides)
    return GraphicCharacter(**values)  # type: ignore[arg-type]


def _fixture_requests() -> dict[str, ArmoryGraphicRequest]:
    primary_druid = (
        Profession("Leatherworking", skill=300, max_skill=300, category="primary"),
        Profession("Skinning", skill=300, max_skill=300, category="primary"),
        Profession("Cooking", skill=300, max_skill=300, category="secondary"),
        Profession("Fishing", skill=225, max_skill=300, category="secondary"),
        Profession("First Aid", skill=300, max_skill=300, category="secondary"),
    )
    primary_warrior = (
        Profession("Blacksmithing", skill=280, max_skill=300, category="primary"),
        Profession("Mining", skill=300, max_skill=300, category="primary"),
        Profession("Cooking", skill=200, max_skill=300, category="secondary"),
        Profession("Fishing", skill=180, max_skill=300, category="secondary"),
        Profession("First Aid", skill=300, max_skill=300, category="secondary"),
    )
    primary_mage = (
        Profession("Enchanting", skill=270, max_skill=300, category="primary"),
        Profession("Tailoring", skill=300, max_skill=300, category="primary"),
        Profession("Cooking", skill=160, max_skill=300, category="secondary"),
        Profession("Fishing", skill=120, max_skill=300, category="secondary"),
        Profession("First Aid", skill=240, max_skill=300, category="secondary"),
    )
    characters = (
        _character(
            "Aelwyn", "Druid", specialization="Feral Combat", professions=primary_druid
        ),
        _character(
            "Thorin",
            "Warrior",
            race="Dwarf",
            specialization="Arms",
            professions=primary_warrior,
        ),
        _character(
            "Mira",
            "Mage",
            race="Human",
            specialization="Frost",
            professions=primary_mage,
        ),
    )
    return {
        "one": ArmoryGraphicRequest("GuildMember", (characters[0],)),
        "two": ArmoryGraphicRequest("GuildMember", characters[:2]),
        "three": ArmoryGraphicRequest("GuildMember", characters),
    }


class GraphicRendererTests(unittest.TestCase):
    def test_dimensions_and_golden_snapshots_for_all_modes(self) -> None:
        requests = _fixture_requests()
        expected_sizes = {"one": (800, 900), "two": (1200, 900), "three": (1800, 900)}
        expected_hashes = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
        observed: dict[str, str] = {}
        PREVIEWS.mkdir(parents=True, exist_ok=True)
        for mode, request in requests.items():
            with self.subTest(mode=mode):
                rendered = render_armory_overview(request)
                observed[mode] = _snapshot_digest(rendered)
                image = Image.open(BytesIO(rendered))
                self.assertEqual(image.size, expected_sizes[mode])
                self.assertEqual(image.format, "PNG")
                self.assertLess(len(rendered), 8 * 1024 * 1024)
                filename = f"armory-{mode}-character{'s' if mode != 'one' else ''}.png"
                (PREVIEWS / filename).write_bytes(rendered)
        if expected_hashes:
            self.assertEqual(observed, expected_hashes)

    def test_golden_layout_is_stable_when_unlicensed_logo_is_absent(self) -> None:
        expected = json.loads(GOLDEN.read_text())["one"]
        with patch.dict(os.environ, {"ARMORY_LOGO_PATH": ""}):
            rendered = render_armory_overview(_fixture_requests()["one"])
        self.assertEqual(_snapshot_digest(rendered), expected)

    def test_count_boundary_rejects_zero_and_more_than_three(self) -> None:
        hero = _character("Aelwyn", "Druid")
        for characters in ((), (hero, hero, hero, hero)):
            with self.subTest(count=len(characters)), self.assertRaises(ValueError):
                ArmoryGraphicRequest("Member", characters)
        for count in (0, 4):
            with self.subTest(count=count), self.assertRaises(ValueError):
                output_size(count)

    def test_missing_statistics_compact_that_section_and_rebalance_professions(
        self,
    ) -> None:
        with_stats = _panel_section_layout(True)
        without_stats = _panel_section_layout(False)
        self.assertEqual(with_stats, (360, 378, 416, 43))
        self.assertEqual(without_stats, (305, 323, 366, 50))

    def test_request_requires_display_name_and_safe_character_count(self) -> None:
        hero = _character("Hero", "Mage")
        with self.assertRaises(ValueError):
            ArmoryGraphicRequest(" \n\t ", (hero,))
        self.assertEqual(ArmoryGraphicRequest("Me", (hero,)).discord_display_name, "Me")

    def test_missing_fields_use_explicit_placeholders_and_do_not_make_surnames(
        self,
    ) -> None:
        hero = _character(
            "Éowyn 長い名前",
            "Druid",
            level=None,
            race=None,
            specialization=None,
            talent_points_spent=None,
        )
        self.assertEqual(_character_title(hero), "Éowyn 長い名前")
        self.assertEqual(_stat_lines(hero), ())
        self.assertEqual(format_pvp(None), ("No PvP Data",))
        self.assertTrue(all(label.isascii() for label in UI_LABELS))
        rendered = render_armory_overview(
            ArmoryGraphicRequest("GuildMember 名字", (hero,))
        )
        self.assertTrue(rendered.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_each_character_keeps_its_own_class_specialization(self) -> None:
        characters = _fixture_requests()["three"].characters
        self.assertEqual(
            [(item.character_class, item.specialization) for item in characters],
            [("Druid", "Feral Combat"), ("Warrior", "Arms"), ("Mage", "Frost")],
        )

    def test_surname_is_used_only_when_explicit_and_not_duplicated(self) -> None:
        self.assertEqual(
            _character_title(_character("Aelwyn", "Druid", surname="Woodsong")),
            "Aelwyn Woodsong",
        )
        self.assertEqual(
            _character_title(
                _character("Aelwyn Woodsong", "Druid", surname="Woodsong")
            ),
            "Aelwyn Woodsong",
        )

    def test_long_unicode_names_fit_without_overflow(self) -> None:
        image = Image.new("RGB", (500, 100))
        draw = ImageDraw.Draw(image)
        from wow_forever_armory.graphic_renderer import _font

        font = _font(22, bold=True, serif=True)
        text = _fit_text(draw, "Éowyn 長い名前 🔥 " * 100, font, 180, limit=256)
        self.assertLessEqual(draw.textlength(text, font=font), 180)
        character = _character("Éowyn 長い名前 🔥 " * 20, "Druid")
        request = ArmoryGraphicRequest("Discord 名字 👑 " * 20, (character,))
        self.assertTrue(render_armory_overview(request).startswith(b"\x89PNG"))

    def test_primary_order_secondary_reservations_and_missing_skills(self) -> None:
        professions = (
            Profession("Tailoring", skill=None, max_skill=300, category="primary"),
            Profession("Alchemy", skill=275, max_skill=300, category="primary"),
            Profession("Blacksmithing", skill=300, max_skill=300, category="primary"),
            Profession("Fishing", skill=110, max_skill=225, category="secondary"),
        )
        primary, secondary = _profession_map(
            _character("Hero", "Warrior", professions=professions)
        )
        self.assertEqual([item.name for item in primary], ["Alchemy", "Blacksmithing"])
        self.assertEqual(_skill_value(Profession("Tailoring", category="primary")), "—")
        self.assertEqual(
            _skill_value(Profession("Tailoring", max_skill=300, category="primary")),
            "—/300",
        )
        self.assertEqual(_skill_value(None), "—")
        self.assertEqual(set(secondary), {"Fishing"})
        # Renderer always reserves Cooking, Fishing, and First Aid rows.
        result = render_armory_overview(
            ArmoryGraphicRequest(
                "Member", (_character("Hero", "Warrior", professions=professions),)
            )
        )
        self.assertTrue(result.startswith(b"\x89PNG"))

    def test_all_three_secondary_skills_and_profession_tiers(self) -> None:
        profs = (
            Profession("Cooking", 275, 300, category="secondary"),
            Profession("Fishing", 199, 225, category="secondary"),
            Profession("First Aid", 300, 300, category="secondary"),
            Profession(
                "Leatherworking",
                category="primary",
                tiers=(
                    ProfessionTier(name="Classic", skill=300, max_skill=300),
                    ProfessionTier(name="Outland", skill=75, max_skill=75),
                ),
            ),
        )
        primary, secondary = _profession_map(
            _character("Hero", "Druid", professions=profs)
        )
        self.assertEqual(len(primary), 1)
        self.assertEqual(set(secondary), {"Cooking", "Fishing", "First Aid"})
        self.assertEqual(_skill_value(primary[0]), "—")

    def test_stats_accept_verified_numbers_and_reject_malformed_values(self) -> None:
        character = _character(
            "Mage",
            "Mage",
            stats=(
                GraphicStat("Mana", 3892),
                GraphicStat("Critical Strike", 12.5, "%"),
                GraphicStat("Invalid bool", True),
                GraphicStat("Invalid nan", float("nan")),
                GraphicStat("Invalid negative", -1),
            ),
        )
        self.assertEqual(
            _stat_lines(character), (("Mana", "3,892"), ("Critical Strike", "12.5%"))
        )
        self.assertEqual(_stat_lines(_character("Warrior", "Warrior")), ())
        self.assertEqual(
            _stat_lines(
                _character("Druid", "Druid", stats=(GraphicStat("Stamina", 1),) * 7)
            ),
            (("Stamina", "1"),) * 5,
        )

    def test_ranked_unranked_and_missing_pvp(self) -> None:
        self.assertEqual(
            format_pvp(PvPInfo(rank="Sergeant", honorable_kills=124)),
            ("Rank: Sergeant", "Honorable Kills: 124"),
        )
        self.assertEqual(format_pvp(PvPInfo(rank="Unranked")), ("Rank: Unranked",))
        self.assertEqual(format_pvp(PvPInfo()), ("No PvP Data",))
        self.assertEqual(format_pvp(PvPInfo(honorable_kills=-1)), ("No PvP Data",))

    def test_timestamp_uses_utc_and_outdated_marker_is_explicit(self) -> None:
        timestamp = datetime(2026, 11, 7, 12, 30, tzinfo=UTC)
        self.assertEqual(_timestamp_label(timestamp), "Nov 7, 2026 12:30 UTC")
        self.assertEqual(_timestamp_label(None), "—")
        self.assertTrue(_is_outdated(datetime.now(UTC) - timedelta(days=31)))
        self.assertFalse(_is_outdated(datetime.now(UTC) - timedelta(days=1)))

    def test_freshness_label_requires_explicit_source_state(self) -> None:
        self.assertEqual(
            _freshness_label(_character("Cache", "Druid", cached=True)), "CACHED DATA"
        )
        self.assertEqual(
            _freshness_label(_character("Live", "Mage", cached=False)), "FRESH DATA"
        )
        self.assertEqual(
            _freshness_label(_character("Unknown", "Warrior", cached=None)),
            "SOURCE UNKNOWN",
        )

    def test_corrupt_portrait_uses_placeholder_and_transparent_logo_loads(self) -> None:
        self.assertIsNone(_load_portrait(b"not an image"))
        with tempfile.TemporaryDirectory() as directory:
            logo_path = Path(directory) / "test-logo.png"
            Image.new("RGBA", (24, 24), (10, 20, 30, 128)).save(logo_path)
            with patch.dict(os.environ, {"ARMORY_LOGO_PATH": str(logo_path)}):
                logo = _load_logo()
            self.assertEqual(logo.mode, "RGBA")
            self.assertIsNotNone(logo.getchannel("A").getbbox())
        request = ArmoryGraphicRequest(
            "Member", (_character("Hero", "Mage", portrait_png=b"broken"),)
        )
        self.assertTrue(render_armory_overview(request).startswith(b"\x89PNG"))

    def test_missing_logo_fails_loader_but_renderer_uses_blank_header_space(
        self,
    ) -> None:
        with (
            patch.dict(os.environ, {"ARMORY_LOGO_PATH": "missing-logo.png"}),
        ):
            with self.assertRaises(GraphicAssetError):
                _load_logo()
            rendered = render_armory_overview(
                ArmoryGraphicRequest("Member", (_character("Hero", "Druid"),))
            )
        self.assertTrue(rendered.startswith(b"\x89PNG"))

    def test_separately_provisioned_logo_is_optional_and_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logo_path = root / "licensed-logo.png"
            Image.new("RGBA", (24, 24), (10, 20, 30, 255)).save(logo_path)
            with patch.dict(os.environ, {"ARMORY_LOGO_PATH": str(logo_path)}):
                self.assertEqual(_load_logo().size, (24, 24))
                alias = root / "alias.png"
                alias.symlink_to(logo_path)
                with patch.dict(os.environ, {"ARMORY_LOGO_PATH": str(alias)}):
                    with self.assertRaises(GraphicAssetError):
                        _load_logo()

    def test_member_state_adapter_uses_normalized_snapshots_only(self) -> None:
        core_character = Character(
            name="Aelwyn",
            level=60,
            race="Night Elf",
            character_class="Druid",
            active_specialization="Feral Combat",
            spent_talent_points=51,
        )
        entry = StoredCharacter("safe/path", 0, core_character, "created", "updated")
        state = SimpleNamespace(display_name="CachedName", characters=(entry,))
        request = request_from_member_state(state, display_name="ActualName 名")
        self.assertEqual(request.discord_display_name, "ActualName 名")
        self.assertTrue(request.characters[0].cached)
        cached_request = request_from_member_state(
            state, display_name="ActualName 名", fresh_paths=frozenset({"safe/path"})
        )
        self.assertFalse(cached_request.characters[0].cached)
        png = render_stored_member_overview(state, display_name="CurrentName 名")
        self.assertTrue(png.startswith(b"\x89PNG"))
        with self.assertRaises(ValueError):
            render_stored_member_overview(
                SimpleNamespace(display_name="Member", characters=())
            )

    def test_performance_and_memory_are_bounded(self) -> None:
        request = _fixture_requests()["three"]
        tracemalloc.start()
        started = time.perf_counter()
        rendered = render_armory_overview(request)
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertLess(elapsed, 3.0)
        self.assertLess(peak, 128 * 1024 * 1024)
        self.assertLess(len(rendered), 8 * 1024 * 1024)

    def test_class_icons_are_distinct_and_unknown_class_has_safe_fallback(self) -> None:
        signatures = []
        for name, character_class in (
            ("Druid", "Druid"),
            ("Warrior", "Warrior"),
            ("Mage", "Mage"),
            ("Unknown", "Unknown Class"),
        ):
            png = render_armory_overview(
                ArmoryGraphicRequest("Member", (_character(name, character_class),))
            )
            image = Image.open(BytesIO(png)).convert("RGB")
            signatures.append(image.crop((38, 158, 170, 290)).tobytes())
        self.assertEqual(len(set(signatures)), len(signatures))


if __name__ == "__main__":
    unittest.main()
