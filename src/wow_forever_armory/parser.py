"""Safely extract the character profile's JSON assignment from Armory HTML."""

import json
import re
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any, Literal, cast

from .models import Character, Profession, ProfessionTier

_SCRIPT_ID = "character-profile-mount-initial-state"
_ASSIGNMENT = re.compile(r"\s*var\s+characterProfileInitialState\s*=\s*")


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"Non-JSON constant: {value}")


class ArmoryParseError(ValueError):
    """The page lacks a usable character profile initial state."""


class _StateScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._collecting = False
        self._parts: list[str] = []
        self.script: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and dict(attrs).get("id") == _SCRIPT_ID:
            if self.script is not None or self._collecting:
                raise ArmoryParseError("Duplicate character profile script")
            self._collecting = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._collecting:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._collecting:
            self.script = "".join(self._parts)
            self._collecting = False


def parse_initial_state(html: str) -> dict[str, Any]:
    """Decode the JSON object from the verified assignment, never executing JS."""
    parser = _StateScriptParser()
    try:
        parser.feed(html)
        parser.close()
    except (ValueError, AssertionError) as exc:
        raise ArmoryParseError("Malformed Armory HTML") from exc
    if parser.script is None:
        raise ArmoryParseError("Character profile script is missing")
    prefix = _ASSIGNMENT.match(parser.script)
    if prefix is None:
        raise ArmoryParseError("Character profile assignment is missing or malformed")
    try:
        document, end = json.JSONDecoder(
            parse_constant=_reject_non_json_constant
        ).raw_decode(parser.script, prefix.end())
    except (json.JSONDecodeError, ValueError) as exc:
        raise ArmoryParseError(
            "Character profile script contains invalid JSON"
        ) from exc
    if parser.script[end:].strip() != ";":
        raise ArmoryParseError("Unexpected content after character profile JSON")
    if not isinstance(document, dict):
        raise ArmoryParseError("Character profile JSON must be an object")
    return cast(dict[str, Any], document)


def _object(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _label(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    name = _object(value).get("name")
    if isinstance(name, str):
        return name.strip() or None
    return None


def _number(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _timestamp(value: object) -> datetime | None:
    data = _object(value)
    raw = data.get("iso8601")
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            if parsed.tzinfo is not None:
                return parsed.astimezone(UTC)
    epoch_ms = _number(data.get("epoch"))
    if epoch_ms is not None:
        try:
            return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=epoch_ms)
        except OverflowError:
            pass
    return None


def _professions(profile: dict[str, Any]) -> tuple[Profession, ...]:
    groups = _object(profile.get("professions"))
    normalized: list[Profession] = []
    for key, category in (
        ("primaries", "primary"),
        ("secondaries", "secondary"),
    ):
        entries = groups.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            item = _object(entry)
            if not item:
                continue
            tiers: list[ProfessionTier] = []
            raw_tiers = item.get("tiers")
            if isinstance(raw_tiers, list):
                for raw_tier in raw_tiers:
                    if isinstance(raw_tier, dict):
                        tier = _object(raw_tier)
                        tiers.append(
                            ProfessionTier(
                                id=_number(tier.get("id")),
                                name=_label(tier.get("name")),
                                skill=_number(tier.get("skillPoints")),
                                max_skill=_number(tier.get("maxSkillPoints")),
                            )
                        )
            only_tier = tiers[0] if len(tiers) == 1 else None
            normalized.append(
                Profession(
                    name=_label(item.get("name")),
                    skill=only_tier.skill if only_tier else None,
                    max_skill=only_tier.max_skill if only_tier else None,
                    id=_number(item.get("id")),
                    category=cast(Literal["primary", "secondary"], category),
                    tiers=tuple(tiers),
                )
            )
    return tuple(normalized)


def _active_spec(profile: dict[str, Any]) -> dict[str, Any]:
    specs = profile.get("specs")
    if isinstance(specs, list):
        for entry in specs:
            spec = _object(entry)
            if spec.get("active") is True:
                return spec
    return {}


def normalize_character(state: dict[str, Any]) -> Character:
    """Normalize the verified character schema; missing fields stay optional."""
    profile = _object(state.get("character"))
    spec = _active_spec(profile)
    summary = _object(state.get("summary"))
    updated = (
        _timestamp(profile.get("lastUpdatedTimestamp"))
        or _timestamp(summary.get("lastUpdatedTimestamp"))
        or _timestamp(state.get("lastUpdatedTimestamp"))
        or _timestamp(profile.get("lastUpdated"))
        or _timestamp(summary.get("lastUpdated"))
        or _timestamp(state.get("lastUpdated"))
    )
    return Character(
        name=_label(profile.get("name")),
        surname=_label(profile.get("surname") or profile.get("lastName")),
        level=_number(profile.get("level")),
        race=_label(profile.get("race")),
        character_class=_label(profile.get("class")),
        active_specialization=_label(spec.get("name")),
        specialization_id=_label(spec.get("id")),
        spent_talent_points=_number(spec.get("spentPoints")),
        professions=_professions(profile),
        armory_updated_at=updated,
    )


def parse_character(html: str) -> Character:
    """Extract and normalize a character page."""
    return normalize_character(parse_initial_state(html))
