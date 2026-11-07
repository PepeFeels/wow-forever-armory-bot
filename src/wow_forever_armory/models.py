"""Normalized, optional Armory fields."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True, slots=True)
class ProfessionTier:
    id: int | None = None
    name: str | None = None
    skill: int | None = None
    max_skill: int | None = None


@dataclass(frozen=True, slots=True)
class Profession:
    name: str | None
    skill: int | None = None
    max_skill: int | None = None
    id: int | None = None
    category: Literal["primary", "secondary"] | None = None
    tiers: tuple[ProfessionTier, ...] = ()


@dataclass(frozen=True, slots=True)
class Character:
    name: str | None = None
    level: int | None = None
    race: str | None = None
    character_class: str | None = None
    active_specialization: str | None = None
    specialization_id: str | None = None
    spent_talent_points: int | None = None
    professions: tuple[Profession, ...] = ()
    armory_updated_at: datetime | None = None
