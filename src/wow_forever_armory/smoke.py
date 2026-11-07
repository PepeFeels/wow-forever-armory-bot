"""Opt-in, one-request live Armory summary command."""

import argparse
import asyncio
import sys

from .client import ArmoryClient, ArmoryClientError
from .parser import ArmoryParseError


async def _run(path: str, base_url: str) -> int:
    client = ArmoryClient(base_url=base_url, max_retries=0)
    try:
        character = await client.fetch_character(path)
    except (ArmoryClientError, ArmoryParseError, ValueError) as exc:
        print(f"Armory smoke test failed: {exc}", file=sys.stderr)
        return 1
    print(f"Name: {character.name or 'unknown'}")
    print(f"Level: {character.level if character.level is not None else 'unknown'}")
    print(f"Class: {character.character_class or 'unknown'}")
    print(f"Race: {character.race or 'unknown'}")
    print(f"Specialization: {character.active_specialization or 'unknown'}")
    points = character.spent_talent_points
    print(f"Spent talent points: {points if points is not None else 'unknown'}")
    for profession in character.professions:
        for tier in profession.tiers:
            skill = tier.skill if tier.skill is not None else "?"
            maximum = tier.max_skill if tier.max_skill is not None else "?"
            print(
                f"{profession.category}: {profession.name or 'unknown'}: "
                f"{skill}/{maximum}"
            )
    updated = character.armory_updated_at
    print(f"Last updated: {updated.isoformat() if updated else 'unknown'}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="Public character path relative to base URL")
    parser.add_argument("--base-url", default="https://worldofwarcraft.blizzard.com/")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.path, args.base_url)))


if __name__ == "__main__":
    main()
