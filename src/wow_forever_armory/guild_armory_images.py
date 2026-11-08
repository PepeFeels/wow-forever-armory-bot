"""Local, deterministic PNG rendering for Guild Armory panels and member rows."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFont

from .guild_armory_store import StoredCharacter

_INTRO_SIZE: Final = (1600, 560)
_ROW_SIZE: Final = (1800, 480)
_ASSET = Path(__file__).with_name("assets") / "armory_intro_background.png"
_FONT_CANDIDATES = {
    False: (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ),
    True: (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ),
}
_GOLD = (222, 181, 101)
_INK = (237, 234, 222)
_MUTED = (175, 186, 204)
_PANEL = (12, 22, 40)
_CARD = (23, 37, 59)
_INTRO_COPY: Final = (
    "GUILD ARMORY",
    "Register your characters",
    "1. Click the Add & Edit button below to open character management.",
    "2. Select Add Character to open the registration form.",
    "3. Paste your full WoW Armory URL and submit the form.",
    "4. Add up to three characters. Use Add & Edit to add, replace, or remove "
    "characters.",
    "Only HTTPS URLs from worldofwarcraft.blizzard.com are accepted.",
)


def _font(
    size: int, bold: bool = False
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in _FONT_CANDIDATES[bold]:
        path = Path(candidate)
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                continue
    try:
        return ImageFont.truetype(
            "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", size
        )
    except OSError:
        return ImageFont.load_default()


def _png(image: Image.Image) -> bytes:
    stream = BytesIO()
    image.save(stream, format="PNG", optimize=True)
    return stream.getvalue()


def render_intro_panel() -> bytes:
    """Compose the generated decorative background with crisp English copy."""
    with Image.open(_ASSET) as source:
        converted = source.convert("RGB")
        target_ratio = _INTRO_SIZE[0] / _INTRO_SIZE[1]
        source_ratio = converted.width / converted.height
        if source_ratio > target_ratio:
            width = round(converted.height * target_ratio)
            left = (converted.width - width) // 2
            cropped = converted.crop((left, 0, left + width, converted.height))
        else:
            height = round(converted.width / target_ratio)
            top = (converted.height - height) // 2
            cropped = converted.crop((0, top, converted.width, top + height))
        image: Image.Image = cropped.resize(_INTRO_SIZE, Image.Resampling.LANCZOS)
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rounded_rectangle(
        (100, 80, 1500, 485),
        radius=24,
        fill=(8, 17, 32, 210),
        outline=_GOLD + (220,),
        width=3,
    )
    image = Image.alpha_composite(image.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(image)
    draw.text((145, 108), _INTRO_COPY[0], font=_font(42, True), fill=_GOLD)
    draw.text((145, 170), _INTRO_COPY[1], font=_font(34, True), fill=_INK)
    instructions = _INTRO_COPY[2:]
    y = 236
    for line in instructions:
        draw.text((145, y), line, font=_font(23), fill=_INK if y < 330 else _MUTED)
        y += 48
    return _png(image.convert("RGB"))


def _fit(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    width: int,
) -> str:
    text = " ".join(text.split())[:2000]
    if draw.textlength(text, font=font) <= width:
        return text
    suffix = "…"
    left, right = 0, len(text)
    while left < right:
        middle = (left + right + 1) // 2
        if draw.textlength(text[:middle] + suffix, font=font) <= width:
            left = middle
        else:
            right = middle - 1
    return text[:left].rstrip() + suffix


def _professions(entry: StoredCharacter) -> str:
    labels: list[str] = []
    for profession in entry.character.professions:
        if profession.name:
            if profession.tiers:
                tier_names = [tier.name for tier in profession.tiers if tier.name]
                label = (
                    f"{profession.name} ({', '.join(tier_names)})"
                    if tier_names
                    else profession.name
                )
            elif profession.skill is not None:
                maximum = (
                    f"/{profession.max_skill}"
                    if profession.max_skill is not None
                    else ""
                )
                label = f"{profession.name} {profession.skill}{maximum}"
            else:
                label = profession.name
            labels.append(label)
    return (", ".join(labels)[:2000]) if labels else "No professions available"


def render_member_row(
    display_name: str, characters: tuple[StoredCharacter, ...]
) -> bytes:
    """Render a single wide image with at most three character cards."""
    if not 1 <= len(characters) <= 3:
        raise ValueError("A member row needs one to three characters")
    image = Image.new("RGB", _ROW_SIZE, _PANEL)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (14, 14, 1786, 466), radius=24, outline=(113, 91, 58), width=3
    )
    draw.text(
        (52, 34),
        _fit(draw, display_name, _font(34, True), 1000),
        font=_font(34, True),
        fill=_GOLD,
    )
    draw.text((52, 82), "GUILD CHARACTERS", font=_font(18, True), fill=_MUTED)

    gap = 20
    card_width = (1700 - gap * (len(characters) - 1)) // len(characters)
    top = 132
    bottom = 438
    for index, entry in enumerate(characters):
        left = 50 + index * (card_width + gap)
        right = left + card_width
        draw.rounded_rectangle(
            (left, top, right, bottom),
            radius=18,
            fill=_CARD,
            outline=(66, 86, 113),
            width=2,
        )
        character = entry.character
        character_name = " ".join(
            filter(None, (character.name, character.surname))
        ) or "Unknown Character"
        name = _fit(draw, character_name, _font(28, True), card_width - 44)
        draw.text((left + 22, top + 20), name, font=_font(28, True), fill=_GOLD)
        details = (
            f"Level {character.level if character.level is not None else '—'} · "
            f"{character.race or 'Unknown race'} · "
            f"{character.character_class or 'Unknown class'}"
        )
        draw.text(
            (left + 22, top + 78),
            _fit(draw, details, _font(18), card_width - 44),
            font=_font(18),
            fill=_INK,
        )
        spec = character.active_specialization or "No specialization available"
        draw.text(
            (left + 22, top + 124),
            _fit(draw, spec, _font(18), card_width - 44),
            font=_font(18),
            fill=_MUTED,
        )
        professions = _professions(entry)
        draw.text(
            (left + 22, top + 174),
            _fit(draw, professions, _font(16), card_width - 44),
            font=_font(16),
            fill=_MUTED,
        )
    return _png(image)
