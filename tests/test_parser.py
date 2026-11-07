"""Offline tests using a minimal synthetic version of the verified page shape."""

import json
import unittest
from datetime import UTC, datetime

from wow_forever_armory.parser import (
    ArmoryParseError,
    parse_character,
    parse_initial_state,
)

SCRIPT_ID = "character-profile-mount-initial-state"


def script(content: str) -> str:
    return f'<html><script id="{SCRIPT_ID}">{content}</script></html>'


def page(state: object) -> str:
    payload = json.dumps(state)
    return script(f"var characterProfileInitialState = {payload};")


def profession(name: str, category_id: int, skill: int, maximum: int) -> dict:
    return {
        "id": category_id,
        "name": name,
        "tiers": [
            {
                "id": category_id + 1000,
                "name": name,
                "skillPoints": skill,
                "maxSkillPoints": maximum,
            }
        ],
    }


class ParserTests(unittest.TestCase):
    def test_verified_character_snapshot(self) -> None:
        state = {
            "character": {
                "name": "Pepe",
                "level": 28,
                "class": {"id": 11, "name": "Druid"},
                "race": {"id": 4, "name": "Night Elf"},
                "specs": [
                    {
                        "active": False,
                        "id": "balance",
                        "name": "Balance",
                        "spentPoints": 7,
                    },
                    {
                        "active": True,
                        "id": "feral-combat",
                        "name": "Feral Combat",
                        "spentPoints": 19,
                    },
                ],
                "professions": {
                    "primaries": [
                        profession("Leatherworking", 165, 101, 150),
                        profession("Skinning", 393, 176, 225),
                    ],
                    "secondaries": [
                        profession("Cooking", 185, 129, 150),
                        profession("Fishing", 356, 119, 150),
                        profession("First Aid", 129, 149, 150),
                    ],
                },
                "lastUpdatedTimestamp": {
                    "epoch": 1794007283000,
                    "iso8601": "2026-11-06T23:21:23Z",
                },
            },
            "summary": {},
            "supportedArmoryTabs": [],
        }
        character = parse_character(page(state))
        self.assertEqual(character.name, "Pepe")
        self.assertEqual(character.level, 28)
        self.assertEqual(character.character_class, "Druid")
        self.assertEqual(character.race, "Night Elf")
        self.assertEqual(character.active_specialization, "Feral Combat")
        self.assertEqual(character.specialization_id, "feral-combat")
        self.assertEqual(character.spent_talent_points, 19)
        self.assertEqual(
            [(p.name, p.category, p.skill, p.max_skill) for p in character.professions],
            [
                ("Leatherworking", "primary", 101, 150),
                ("Skinning", "primary", 176, 225),
                ("Cooking", "secondary", 129, 150),
                ("Fishing", "secondary", 119, 150),
                ("First Aid", "secondary", 149, 150),
            ],
        )
        self.assertEqual(
            character.armory_updated_at,
            datetime(2026, 11, 6, 23, 21, 23, tzinfo=UTC),
        )

    def test_assignment_whitespace(self) -> None:
        state = parse_initial_state(
            script(' \n var\tcharacterProfileInitialState\n=\n{"character":{}} ; \n')
        )
        self.assertEqual(state, {"character": {}})

    def test_missing_or_malformed_assignment(self) -> None:
        for content in (
            "{}",
            "let characterProfileInitialState = {};",
            "var other = {};",
            "var characterProfileInitialState = {broken};",
            'var characterProfileInitialState = {"level": NaN};',
            "var characterProfileInitialState = [];",
            "var characterProfileInitialState = {}",
        ):
            with self.subTest(content=content), self.assertRaises(ArmoryParseError):
                parse_initial_state(script(content))

    def test_reject_trailing_executable_content(self) -> None:
        for content in (
            "var characterProfileInitialState = {}; alert(1)",
            "var characterProfileInitialState = {}; // trailing comment",
            "var characterProfileInitialState = {};;",
        ):
            with (
                self.subTest(content=content),
                self.assertRaisesRegex(ArmoryParseError, "Unexpected content"),
            ):
                parse_initial_state(script(content))

    def test_missing_script(self) -> None:
        with self.assertRaisesRegex(ArmoryParseError, "script is missing"):
            parse_initial_state("<html><body>none</body></html>")

    def test_malformed_html_unclosed_script(self) -> None:
        with self.assertRaises(ArmoryParseError):
            parse_initial_state(
                f'<script id="{SCRIPT_ID}">var characterProfileInitialState = {{}};'
            )

    def test_missing_character_fields_and_specialization(self) -> None:
        character = parse_character(page({"character": {"specs": [{"active": False}]}}))
        self.assertIsNone(character.name)
        self.assertIsNone(character.level)
        self.assertIsNone(character.character_class)
        self.assertIsNone(character.race)
        self.assertIsNone(character.active_specialization)
        self.assertIsNone(character.specialization_id)
        self.assertIsNone(character.spent_talent_points)
        self.assertEqual(character.professions, ())

    def test_multiple_tiers_preserved(self) -> None:
        character = parse_character(
            page(
                {
                    "character": {
                        "professions": {
                            "primaries": [
                                {
                                    "id": 165,
                                    "name": "Leatherworking",
                                    "tiers": [
                                        {
                                            "id": 1,
                                            "name": "Apprentice",
                                            "skillPoints": 70,
                                        },
                                        {
                                            "id": 2,
                                            "name": "Journeyman",
                                            "skillPoints": 101,
                                            "maxSkillPoints": 150,
                                        },
                                    ],
                                }
                            ]
                        }
                    }
                }
            )
        )
        item = character.professions[0]
        self.assertEqual(len(item.tiers), 2)
        self.assertEqual(item.tiers[0].skill, 70)
        self.assertIsNone(item.tiers[0].max_skill)
        self.assertEqual(item.tiers[1].skill, 101)
        self.assertEqual(item.tiers[1].max_skill, 150)
        self.assertIsNone(item.skill)
        self.assertIsNone(item.max_skill)

    def test_missing_profession_values(self) -> None:
        character = parse_character(
            page(
                {
                    "character": {
                        "professions": {
                            "primaries": [
                                {"id": 165, "name": "Leatherworking", "tiers": []},
                                {"id": 393, "name": "Skinning", "tiers": [{}]},
                            ],
                            "secondaries": None,
                        }
                    }
                }
            )
        )
        self.assertEqual(len(character.professions), 2)
        self.assertIsNone(character.professions[0].skill)
        self.assertIsNone(character.professions[1].skill)
        self.assertEqual(len(character.professions[1].tiers), 1)
        self.assertIsNone(character.professions[1].tiers[0].max_skill)

    def test_iso_timestamp_is_normalized_to_utc(self) -> None:
        character = parse_character(
            page(
                {
                    "character": {
                        "lastUpdatedTimestamp": {
                            "iso8601": "2026-11-07T00:21:23+01:00",
                            "epoch": 1,
                        }
                    }
                }
            )
        )
        self.assertEqual(
            character.armory_updated_at,
            datetime(2026, 11, 6, 23, 21, 23, tzinfo=UTC),
        )

    def test_epoch_milliseconds_fallback(self) -> None:
        for timestamp in (
            {"epoch": 1794007283000},
            {"iso8601": "invalid", "epoch": 1794007283000},
        ):
            with self.subTest(timestamp=timestamp):
                character = parse_character(
                    page({"character": {"lastUpdatedTimestamp": timestamp}})
                )
                self.assertEqual(
                    character.armory_updated_at,
                    datetime(2026, 11, 6, 23, 21, 23, tzinfo=UTC),
                )

    def test_missing_or_malformed_timestamps(self) -> None:
        for value in (
            None,
            {"iso8601": "not-a-date"},
            {"iso8601": "2026-11-06T23:21:23"},
            {"epoch": -1},
            {"epoch": True},
            {"epoch": 10**30},
        ):
            with self.subTest(value=value):
                character = parse_character(
                    page({"character": {"lastUpdatedTimestamp": value}})
                )
                self.assertIsNone(character.armory_updated_at)

    def test_legacy_last_updated_key_still_parses(self) -> None:
        character = parse_character(
            page({"character": {"lastUpdated": {"epoch": 1794007283000}}})
        )
        self.assertEqual(
            character.armory_updated_at,
            datetime(2026, 11, 6, 23, 21, 23, tzinfo=UTC),
        )

    def test_script_is_not_executed(self) -> None:
        with self.assertRaises(ArmoryParseError):
            parse_initial_state(script("var characterProfileInitialState = alert(1);"))
