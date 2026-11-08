"""Strict official Armory URL validation built on the existing Core path guard."""

from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlparse

from .client import ArmoryClient

OFFICIAL_ARMORY_HOSTS = frozenset({"worldofwarcraft.blizzard.com"})
_CHARACTER_PATH = re.compile(
    r"^(?P<locale>[a-z]{2}-[a-z]{2})/forever/"
    r"(?P<region>eu|us|kr|tw)/armory/character/(?P<slug>[^/]+)$",
    re.IGNORECASE,
)
_BAD_PERCENT = re.compile(r"%(?![0-9a-fA-F]{2})")
_NESTED_ESCAPE = re.compile(r"%[0-9a-fA-F]{2}")


def validate_armory_url(value: str, client: ArmoryClient) -> str:
    """Return a normalized Core path for an official HTTPS character URL."""
    value = value.strip()
    if not value or len(value) > 1024:
        raise ValueError("Enter the full official WoW Armory character URL.")
    if any(ord(char) < 0x20 for char in value):
        raise ValueError("The Armory URL contains invalid whitespace.")
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("The Armory URL has an invalid port.") from exc
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.hostname.lower() not in OFFICIAL_ARMORY_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
        or "?" in value
        or "#" in value
        or "\\" in value
    ):
        raise ValueError("Use an HTTPS character URL from the official WoW Armory.")
    raw_path = parsed.path.lstrip("/")
    match = _CHARACTER_PATH.fullmatch(raw_path)
    if match is None or _BAD_PERCENT.search(raw_path):
        raise ValueError("Use a full supported Armory character path.")
    try:
        slug = unquote(match.group("slug"), errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("The Armory URL contains invalid text encoding.") from exc
    if (
        slug in {".", ".."}
        or "/" in slug
        or "\\" in slug
        or _NESTED_ESCAPE.search(slug)
        or any(ord(char) < 0x20 for char in slug)
    ):
        raise ValueError("The Armory URL contains an invalid character path.")
    path = (
        f"{match.group('locale').lower()}/forever/{match.group('region').lower()}"
        f"/armory/character/{quote(slug, safe='-_.~')}"
    )
    canonical = client._url(path)
    canonical_parts = urlparse(canonical)
    if (
        canonical_parts.scheme != "https"
        or canonical_parts.hostname not in OFFICIAL_ARMORY_HOSTS
        or canonical_parts.query
        or canonical_parts.fragment
    ):
        raise ValueError("The Armory URL could not be safely validated.")
    return path
