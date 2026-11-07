"""Deterministic local PNG renderer for a user's Guild Armory overview."""

from __future__ import annotations

import math
import random
import re
import unicodedata
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from .guild_armory_store import StoredCharacter
from .models import Character, Profession

_ASSET_DIR: Final = Path(__file__).with_name("assets")
_LOGO_PATH: Final = _ASSET_DIR / "wow_forever_logo.png"
_FONT_DIR: Final = _ASSET_DIR / "fonts"
_OUTPUT_SIZES: Final = {1: (800, 900), 2: (1200, 900), 3: (1800, 900)}
_STALE_AFTER: Final = timedelta(days=30)
_MAX_LOGO_BYTES: Final = 8 * 1024 * 1024
_MAX_PORTRAIT_BYTES: Final = 2 * 1024 * 1024
_MAX_SOURCE_PIXELS: Final = 4_000_000
_MAX_OUTPUT_BYTES: Final = 8 * 1024 * 1024

_GOLD = (229, 184, 91)
_GOLD_LIGHT = (255, 224, 145)
_PAPER = (238, 231, 211)
_MUTED = (166, 177, 192)
_NAVY = (6, 14, 28)
_CARD = (8, 19, 35)
_CARD_LIGHT = (14, 32, 52)
_CLASS_COLORS: Final = {
    "druid": (255, 125, 10),
    "hunter": (171, 212, 115),
    "mage": (105, 204, 240),
    "paladin": (245, 140, 186),
    "priest": (245, 245, 245),
    "rogue": (255, 245, 105),
    "shaman": (0, 112, 222),
    "warlock": (148, 130, 201),
    "warrior": (199, 156, 110),
}
_PRIMARY_ORDER: Final = (
    "alchemy",
    "blacksmithing",
    "enchanting",
    "engineering",
    "herbalism",
    "inscription",
    "jewelcrafting",
    "leatherworking",
    "mining",
    "skinning",
    "tailoring",
)
_SECONDARY_NAMES: Final = ("Cooking", "Fishing", "First Aid")
_BIDI_CONTROLS = re.compile("[\u202a-\u202e\u2066-\u2069]")
UI_LABELS: Final = (
    "CHARACTER OVERVIEW",
    "CHARACTER STATS",
    "PROFESSIONS",
    "SECONDARY SKILLS",
    "PVP",
    "LAST UPDATED",
    "No Stats Available",
    "No PvP Data",
    "No Primary Professions",
    "CACHED DATA",
    "FRESH DATA",
    "OUTDATED",
    "SOURCE UNKNOWN",
)


class GraphicRenderError(RuntimeError):
    """The overview could not be safely rendered."""


class GraphicAssetError(GraphicRenderError):
    """A required, bundled visual asset is missing or invalid."""


@dataclass(frozen=True, slots=True)
class GraphicStat:
    """Optional verified stat value for future authorized data sources."""

    name: str
    value: str | int | float
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class PvPInfo:
    """Optional PvP values; callers must supply only semantically verified data."""

    rank: str | None = None
    honorable_kills: int | None = None


@dataclass(frozen=True, slots=True)
class GraphicCharacter:
    """Renderer-facing normalized data, independent of API and database access."""

    name: str | None
    level: int | None
    race: str | None
    character_class: str | None
    specialization: str | None
    talent_points_spent: int | None
    professions: tuple[Profession, ...] = ()
    last_updated: datetime | None = None
    surname: str | None = None
    portrait_png: bytes | None = None
    stats: tuple[GraphicStat, ...] = ()
    pvp: PvPInfo | None = None
    cached: bool | None = None

    @classmethod
    def from_core(
        cls, character: Character, *, cached: bool | None = None
    ) -> GraphicCharacter:
        """Adapt only fields the existing Armory Core actually normalizes."""
        return cls(
            name=character.name,
            level=character.level,
            race=character.race,
            character_class=character.character_class,
            specialization=character.active_specialization,
            talent_points_spent=character.spent_talent_points,
            professions=character.professions,
            last_updated=character.armory_updated_at,
            cached=cached,
        )


@dataclass(frozen=True, slots=True)
class ArmoryGraphicRequest:
    """Validated input for one Discord user's 1–3 character overview."""

    discord_display_name: str
    characters: tuple[GraphicCharacter, ...]

    def __post_init__(self) -> None:
        if not 1 <= len(self.characters) <= 3:
            raise ValueError("An overview requires one to three characters")
        if not _clean_text(self.discord_display_name, limit=256):
            raise ValueError("A Discord display name is required")


def request_from_member_state(
    state: object,
    *,
    display_name: str | None = None,
    fresh_paths: frozenset[str] = frozenset(),
) -> ArmoryGraphicRequest:
    """Adapt a member snapshot without querying storage or accepting user paths."""
    stored = getattr(state, "characters", None)
    if not isinstance(stored, tuple):
        raise ValueError("Member state does not contain a character tuple")
    if not 1 <= len(stored) <= 3:
        raise ValueError("A member overview requires one to three characters")
    chosen_name = display_name or getattr(state, "display_name", None)
    if not isinstance(chosen_name, str):
        raise ValueError("Member state does not contain a display name")
    graphic_characters: list[GraphicCharacter] = []
    for entry in stored:
        if not isinstance(entry, StoredCharacter):
            raise ValueError("Member state contains an invalid character record")
        graphic_characters.append(
            GraphicCharacter.from_core(
                entry.character, cached=entry.path not in fresh_paths
            )
        )
    return ArmoryGraphicRequest(chosen_name, tuple(graphic_characters))


def output_size(character_count: int) -> tuple[int, int]:
    """Return the fixed canvas dimensions for a supported character count."""
    try:
        return _OUTPUT_SIZES[character_count]
    except KeyError as exc:
        raise ValueError("An overview requires one to three characters") from exc


def _panel_section_layout(has_stats: bool) -> tuple[int, int, int, int]:
    """Return stat divider, profession heading/row positions, and row spacing."""
    if has_stats:
        return 360, 378, 416, 43
    return 305, 323, 366, 50


def _clean_text(value: object, *, limit: int = 512) -> str:
    if not isinstance(value, str):
        return ""
    normalized = _BIDI_CONTROLS.sub("", value)
    normalized = " ".join(normalized.split())
    normalized = "".join(
        character
        for character in normalized
        if unicodedata.category(character) not in {"Cc", "Cs"}
    )
    return normalized[:limit].strip()


def _font(
    size: int, *, bold: bool = False, serif: bool = False
) -> ImageFont.FreeTypeFont:
    family = "Serif" if serif else "Sans"
    weight = "Bold" if bold else ""
    path = _FONT_DIR / f"DejaVu{family}{('-' + weight) if weight else ''}.ttf"
    try:
        return ImageFont.truetype(str(path), size=size)
    except OSError as exc:
        raise GraphicAssetError("A bundled overview font is unavailable") from exc


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    *,
    limit: int = 512,
) -> str:
    safe = _clean_text(text, limit=limit)
    if draw.textlength(safe, font=font) <= max_width:
        return safe
    suffix = "…"
    left, right = 0, len(safe)
    while left < right:
        middle = (left + right + 1) // 2
        if draw.textlength(safe[:middle].rstrip() + suffix, font=font) <= max_width:
            left = middle
        else:
            right = middle - 1
    return safe[:left].rstrip() + suffix


def _background(size: tuple[int, int]) -> Image.Image:
    width, height = size
    low = Image.new("RGB", (max(1, width // 8), max(1, height // 8)))
    rng = random.Random(0xA46E2)
    pixels = [
        (5 + shade // 18, 14 + shade // 8, 28 + shade // 4)
        for shade in (rng.randrange(0, 80) for _ in range(low.width * low.height))
    ]
    low.putdata(pixels)
    image = low.resize(size, Image.Resampling.BICUBIC)
    draw = ImageDraw.Draw(image, "RGBA")
    for index in range(14):
        y = 26 + index * (height // 13)
        draw.line((0, y, width, y - 60), fill=(47, 78, 111, 10), width=1)
    draw.ellipse(
        (-width // 3, -height // 2, width * 4 // 3, height * 3 // 2),
        outline=(88, 123, 154, 12),
        width=2,
    )
    return image


def _load_logo() -> Image.Image:
    try:
        if _LOGO_PATH.stat().st_size > _MAX_LOGO_BYTES:
            raise GraphicAssetError("The bundled logo exceeds the size limit")
        with Image.open(_LOGO_PATH) as source:
            if source.format != "PNG" or source.width > 4096 or source.height > 4096:
                raise GraphicAssetError("The bundled logo is not a safe PNG")
            if source.width * source.height > _MAX_SOURCE_PIXELS:
                raise GraphicAssetError("The bundled logo exceeds the pixel limit")
            source.load()
            return source.convert("RGBA")
    except (OSError, UnidentifiedImageError) as exc:
        raise GraphicAssetError("The bundled WoW Forever logo is unavailable") from exc


def _load_portrait(png: bytes | None) -> Image.Image | None:
    if not png or len(png) > _MAX_PORTRAIT_BYTES:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(png)) as source:
                if source.format not in {"PNG", "JPEG", "WEBP"}:
                    return None
                if (
                    source.width < 1
                    or source.height < 1
                    or source.width > 2048
                    or source.height > 2048
                    or source.width * source.height > _MAX_SOURCE_PIXELS
                ):
                    return None
                source.load()
                return source.convert("RGBA")
    except (
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return None


def _rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, *size), radius=radius, fill=255)
    return mask


def _class_color(character_class: str | None) -> tuple[int, int, int]:
    return _CLASS_COLORS.get(_clean_text(character_class).casefold(), (159, 170, 186))


def _draw_class_placeholder(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    character: GraphicCharacter,
) -> None:
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    color = _class_color(character.character_class)
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(box, radius=13, fill=(12, 25, 42), outline=_GOLD, width=3)
    inner = (left + 7, top + 7, right - 7, bottom - 7)
    draw.rounded_rectangle(
        inner, radius=10, fill=(color[0] // 5, color[1] // 6, color[2] // 5)
    )
    center = (left + width // 2, top + height // 2 - 9)
    emblem_radius = min(width, height) // 4
    draw.ellipse(
        (
            center[0] - emblem_radius,
            center[1] - emblem_radius,
            center[0] + emblem_radius,
            center[1] + emblem_radius,
        ),
        fill=color,
        outline=_GOLD_LIGHT,
        width=2,
    )
    _draw_class_symbol(
        draw,
        center,
        max(10, emblem_radius - 5),
        _clean_text(character.character_class).casefold(),
    )
    class_name = _fit_text(
        draw,
        _clean_text(character.character_class).upper() or "UNKNOWN CLASS",
        _font(max(10, min(13, width // 9)), bold=True),
        width - 12,
        limit=48,
    )
    draw.text(
        (left + width // 2, bottom - 25),
        class_name,
        font=_font(11, bold=True),
        fill=_PAPER,
        anchor="mm",
    )


def _draw_class_symbol(
    draw: ImageDraw.ImageDraw,
    center: tuple[int, int],
    radius: int,
    class_key: str,
) -> None:
    """Draw original vector motifs without redistributing game artwork."""
    cx, cy = center
    dark = (12, 18, 28)
    highlight = _GOLD_LIGHT
    stroke = max(2, radius // 5)
    if class_key == "druid":
        draw.polygon(
            [
                (cx, cy + radius),
                (cx - radius, cy),
                (cx - radius // 2, cy - radius),
                (cx + radius, cy - radius),
                (cx + radius // 2, cy + radius // 2),
            ],
            fill=dark,
        )
        draw.line((cx, cy + radius, cx, cy - radius // 2), fill=highlight, width=stroke)
        draw.line(
            (cx, cy, cx - radius // 2, cy - radius // 2), fill=highlight, width=stroke
        )
        draw.line(
            (cx, cy - radius // 4, cx + radius // 2, cy - radius // 2),
            fill=highlight,
            width=stroke,
        )
    elif class_key == "warrior":
        draw.line(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            fill=highlight,
            width=stroke + 1,
        )
        draw.line(
            (cx + radius, cy - radius, cx - radius, cy + radius),
            fill=dark,
            width=stroke + 3,
        )
        draw.line(
            (cx + radius, cy - radius, cx - radius, cy + radius),
            fill=highlight,
            width=stroke,
        )
        draw.line(
            (cx - radius, cy - radius, cx - radius // 2, cy - radius // 2),
            fill=dark,
            width=stroke + 2,
        )
        draw.line(
            (cx + radius, cy - radius, cx + radius // 2, cy - radius // 2),
            fill=dark,
            width=stroke + 2,
        )
    elif class_key == "mage":
        draw.line(
            (cx - radius, cy + radius, cx + radius // 2, cy - radius // 2),
            fill=dark,
            width=stroke + 3,
        )
        draw.line(
            (cx - radius, cy + radius, cx + radius // 2, cy - radius // 2),
            fill=highlight,
            width=stroke,
        )
        draw.polygon(
            [
                (cx + radius // 2, cy - radius),
                (cx + radius * 2 // 3, cy - radius // 3),
                (cx + radius, cy),
                (cx + radius // 3, cy + radius // 8),
                (cx, cy - radius // 2),
                (cx + radius // 2, cy - radius // 2),
            ],
            fill=highlight,
        )
    else:
        points = []
        for index in range(10):
            angle = -math.pi / 2 + index * math.pi / 5
            distance = radius if index % 2 == 0 else radius // 2
            points.append(
                (
                    cx + round(math.cos(angle) * distance),
                    cy + round(math.sin(angle) * distance),
                )
            )
        draw.polygon(points, fill=dark, outline=highlight)


def _draw_portrait(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    character: GraphicCharacter,
) -> None:
    portrait = _load_portrait(character.portrait_png)
    if portrait is None:
        _draw_class_placeholder(canvas, box, character)
        return
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    portrait.thumbnail((width - 12, height - 12), Image.Resampling.LANCZOS)
    tile = Image.new("RGBA", (width, height), (8, 18, 32, 255))
    x = (width - portrait.width) // 2
    y = (height - portrait.height) // 2
    tile.alpha_composite(portrait, (x, y))
    canvas.paste(tile.convert("RGB"), (left, top), _rounded_mask((width, height), 12))
    ImageDraw.Draw(canvas).rounded_rectangle(box, radius=12, outline=_GOLD, width=3)


def _character_title(character: GraphicCharacter) -> str:
    name = _clean_text(character.name, limit=120) or "Unknown Character"
    surname = _clean_text(character.surname, limit=80)
    if surname and not name.casefold().endswith(surname.casefold()):
        return f"{name} {surname}"
    return name


def _valid_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value < 0:
        return None
    return value


def _grouped_number(value: int | float) -> str:
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:,.1f}".rstrip("0").rstrip(".")


def _stat_lines(character: GraphicCharacter) -> tuple[tuple[str, str], ...]:
    lines: list[tuple[str, str]] = []
    for stat in character.stats:
        if not isinstance(stat, GraphicStat):
            continue
        label = _clean_text(stat.name, limit=40)
        value: str
        number = _valid_number(stat.value)
        if number is not None:
            value = _grouped_number(number)
        elif isinstance(stat.value, str):
            value = _clean_text(stat.value, limit=48)
        else:
            continue
        if not label or not value:
            continue
        if stat.unit in {"%", "s", "ms"}:
            value += stat.unit
        lines.append((label, value))
        if len(lines) == 5:
            break
    return tuple(lines)


def _profession_sort_key(profession: Profession) -> tuple[int, str]:
    name = _clean_text(profession.name, limit=80)
    normalized = name.casefold()
    try:
        order = _PRIMARY_ORDER.index(normalized)
    except ValueError:
        order = len(_PRIMARY_ORDER)
    return order, normalized


def _skill_value(profession: Profession | None) -> str:
    if profession is None:
        return "—"
    skill = _valid_number(profession.skill)
    maximum = _valid_number(profession.max_skill)
    if skill is None and maximum is None:
        return "—"
    current_label = _grouped_number(skill) if skill is not None else "—"
    if maximum is None:
        return current_label
    return f"{current_label}/{_grouped_number(maximum)}"


def _profession_map(
    character: GraphicCharacter,
) -> tuple[list[Profession], dict[str, Profession]]:
    primary = sorted(
        (
            item
            for item in character.professions
            if item.category == "primary" and _clean_text(item.name)
        ),
        key=_profession_sort_key,
    )[:2]
    secondary: dict[str, Profession] = {}
    for item in character.professions:
        if item.category != "secondary" or not item.name:
            continue
        normalized = _clean_text(item.name).casefold().replace("-", " ")
        for expected in _SECONDARY_NAMES:
            if normalized == expected.casefold():
                secondary[expected] = item
    return primary, secondary


def _timestamp_label(value: datetime | None) -> str:
    if value is None or value.tzinfo is None:
        return "—"
    utc_value = value.astimezone(UTC)
    return utc_value.strftime("%b %d, %Y %H:%M UTC").replace(" 0", " ", 1)


def _is_outdated(value: datetime | None) -> bool:
    if value is None or value.tzinfo is None:
        return False
    return datetime.now(UTC) - value.astimezone(UTC) > _STALE_AFTER


def format_pvp(pvp: PvPInfo | None) -> tuple[str, ...]:
    """Format only explicit, semantically verified PvP fields."""
    if pvp is None:
        return ("No PvP Data",)
    lines: list[str] = []
    rank = _clean_text(pvp.rank, limit=64)
    if rank:
        lines.append(f"Rank: {rank}")
    kills = _valid_number(pvp.honorable_kills)
    if kills is not None:
        lines.append(f"Honorable Kills: {_grouped_number(kills)}")
    return tuple(lines) or ("No PvP Data",)


def _draw_discord_mark(draw: ImageDraw.ImageDraw, x: int, y: int, size: int) -> None:
    width, height = size, round(size * 0.76)
    draw.rounded_rectangle(
        (x, y + 3, x + width, y + height), radius=10, fill=(231, 234, 244)
    )
    draw.arc((x + 3, y, x + width - 3, y + 23), 180, 360, fill=(231, 234, 244), width=8)
    eye_y = y + height // 2
    for eye_x in (x + width // 3, x + width * 2 // 3):
        draw.ellipse((eye_x - 3, eye_y - 2, eye_x + 3, eye_y + 4), fill=(11, 22, 39))
    draw.arc(
        (x + 16, y + height - 16, x + width - 16, y + height + 1),
        0,
        180,
        fill=(11, 22, 39),
        width=2,
    )


def _draw_header(
    canvas: Image.Image,
    display_name: str,
    logo: Image.Image | None,
) -> None:
    width, _ = canvas.size
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        (17, 15, width - 17, 124), radius=18, fill=(7, 17, 31), outline=_GOLD, width=3
    )
    draw.line((29, 117, width - 29, 117), fill=(119, 86, 39), width=1)

    if logo is not None:
        logo_box = (36, 21, 132, 113)
        logo.thumbnail(
            (logo_box[2] - logo_box[0], logo_box[3] - logo_box[1]),
            Image.Resampling.LANCZOS,
        )
        logo_x = logo_box[0] + (logo_box[2] - logo_box[0] - logo.width) // 2
        logo_y = logo_box[1] + (logo_box[3] - logo_box[1] - logo.height) // 2
        canvas.paste(logo, (logo_x, logo_y), logo)

    right_title = "CHARACTER OVERVIEW"
    title_font = _font(21 if width <= 800 else 25, bold=True, serif=True)
    # Reserve enough room for the complete heading even on the 800px layout.
    title_box = (width - 36 - 350, 42, width - 36, 89)
    title_text = _fit_text(
        draw, right_title, title_font, title_box[2] - title_box[0], limit=40
    )
    draw.text(
        (title_box[2], (title_box[1] + title_box[3]) // 2),
        title_text,
        font=title_font,
        fill=_GOLD_LIGHT,
        anchor="rm",
    )

    icon_x = 157
    icon_size = 41 if width <= 800 else 46
    _draw_discord_mark(draw, icon_x, 42, icon_size)
    name_left = icon_x + icon_size + 18
    name_right = title_box[0] - 23
    name_font = _font(28 if width <= 800 else 34, bold=True, serif=True)
    name_text = _fit_text(
        draw, display_name, name_font, max(40, name_right - name_left), limit=256
    )
    draw.text((name_left, 46), name_text, font=name_font, fill=_GOLD_LIGHT)


def _draw_ornamental_border(
    draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int]
) -> None:
    left, top, right, bottom = box
    draw.rounded_rectangle(box, radius=18, fill=_CARD, outline=(115, 82, 36), width=5)
    inner = (left + 8, top + 8, right - 8, bottom - 8)
    draw.rounded_rectangle(inner, radius=13, outline=_GOLD, width=2)
    draw.rounded_rectangle(
        (left + 13, top + 13, right - 13, bottom - 13),
        radius=11,
        outline=(72, 58, 43),
        width=1,
    )
    for x, y in (
        (left + 20, top + 20),
        (right - 20, top + 20),
        (left + 20, bottom - 20),
        (right - 20, bottom - 20),
    ):
        radius = 7
        draw.polygon(
            [(x, y - radius), (x + radius, y), (x, y + radius), (x - radius, y)],
            fill=_GOLD,
        )


def _section_heading(
    draw: ImageDraw.ImageDraw,
    title: str,
    x: int,
    y: int,
    width: int,
    *,
    size: int,
) -> None:
    font = _font(size, bold=True, serif=True)
    fitted = _fit_text(draw, title, font, width, limit=48)
    draw.text((x, y), fitted, font=font, fill=_GOLD_LIGHT)


def _draw_identity(
    canvas: Image.Image,
    character: GraphicCharacter,
    box: tuple[int, int, int, int],
) -> None:
    left, top, right, _ = box
    draw = ImageDraw.Draw(canvas)
    panel_width = right - left
    portrait_size = min(132, max(104, panel_width // 5))
    portrait_box = (
        left + 18,
        top + 16,
        left + 18 + portrait_size,
        top + 16 + portrait_size,
    )
    _draw_portrait(canvas, portrait_box, character)

    info_x = portrait_box[2] + 15
    info_width = right - 18 - info_x
    title_font = _font(26 if panel_width < 650 else 31, bold=True, serif=True)
    title = _fit_text(
        draw, _character_title(character), title_font, info_width, limit=160
    )
    draw.text((info_x, top + 20), title, font=title_font, fill=_GOLD_LIGHT)

    level = _valid_number(character.level)
    level_text = f"Level {_grouped_number(level)}" if level is not None else "Level —"
    race = _clean_text(character.race, limit=72) or "—"
    character_class = _clean_text(character.character_class, limit=48) or "—"
    identity = f"{level_text} · {race} {character_class}"
    identity_font = _font(16 if panel_width < 650 else 18, serif=True)
    identity_text = _fit_text(draw, identity, identity_font, info_width, limit=180)
    draw.text((info_x, top + 66), identity_text, font=identity_font, fill=_PAPER)

    spec = (
        _clean_text(character.specialization, limit=80) or "No Specialization Available"
    )
    talent_points = _valid_number(character.talent_points_spent)
    talent = (
        f" · {_grouped_number(talent_points)} Points Spent"
        if talent_points is not None
        else ""
    )
    spec_font = _font(16 if panel_width < 650 else 18, serif=True)
    spec_text = _fit_text(draw, f"{spec}{talent}", spec_font, info_width, limit=160)
    draw.text((info_x, top + 94), spec_text, font=spec_font, fill=_MUTED)


def _draw_stats(
    draw: ImageDraw.ImageDraw,
    character: GraphicCharacter,
    panel: tuple[int, int, int, int],
    count: int,
) -> None:
    left, top, right, _ = panel
    padding = 23
    inner_left, inner_right = left + padding, right - padding
    font_size = 17 if count == 3 else 19
    _section_heading(
        draw,
        "CHARACTER STATS",
        inner_left,
        top + 196,
        inner_right - inner_left,
        size=22,
    )
    stats = _stat_lines(character)
    if not stats:
        draw.text(
            (inner_left, top + 235),
            "No Stats Available",
            font=_font(font_size),
            fill=_MUTED,
        )
        return
    row_y = top + 235
    row_font = _font(font_size)
    value_width = 100
    for label, value in stats:
        label_text = _fit_text(
            draw, label, row_font, inner_right - inner_left - value_width - 12, limit=48
        )
        value_text = _fit_text(draw, value, row_font, value_width, limit=48)
        draw.text((inner_left, row_y), label_text, font=row_font, fill=_PAPER)
        draw.text(
            (inner_right, row_y), value_text, font=row_font, fill=_PAPER, anchor="ra"
        )
        row_y += 25


def _draw_professions(
    draw: ImageDraw.ImageDraw,
    character: GraphicCharacter,
    panel: tuple[int, int, int, int],
    count: int,
    *,
    heading_y: int,
    row_start: int,
    row_gap: int,
) -> None:
    left, top, right, _ = panel
    padding = 22
    inner_left, inner_right = left + padding, right - padding
    half_gap = 16
    column_width = (inner_right - inner_left - half_gap) // 2
    primary_x = inner_left
    secondary_x = inner_left + column_width + half_gap
    heading_size = 16 if count > 1 else 21
    font_size = 14 if count > 1 else 16
    _section_heading(
        draw, "PROFESSIONS", primary_x, heading_y, column_width, size=heading_size
    )
    _section_heading(
        draw,
        "SECONDARY SKILLS",
        secondary_x,
        heading_y,
        column_width,
        size=heading_size,
    )
    primary, secondary = _profession_map(character)
    row_font = _font(font_size)
    icon_font = _font(max(10, font_size - 2), bold=True)

    if not primary:
        draw.text(
            (primary_x, heading_y + 19),
            "No Primary Professions",
            font=_font(max(12, font_size - 1)),
            fill=_MUTED,
        )
    for index, profession in enumerate(primary):
        _draw_profession_row(
            draw,
            profession.name or "",
            _skill_value(profession),
            primary_x,
            row_start + index * row_gap,
            column_width,
            row_font,
            icon_font,
        )

    for index, name in enumerate(_SECONDARY_NAMES):
        secondary_profession = secondary.get(name)
        _draw_profession_row(
            draw,
            name,
            _skill_value(secondary_profession),
            secondary_x,
            row_start + index * row_gap,
            column_width,
            row_font,
            icon_font,
        )


def _profession_initials(name: str) -> str:
    words = _clean_text(name).split()
    if len(words) > 1:
        return "".join(word[0] for word in words[:2]).upper()
    return words[0][:2].upper() if words else "—"


def _draw_profession_row(
    draw: ImageDraw.ImageDraw,
    name: str,
    value: str,
    x: int,
    y: int,
    width: int,
    font: ImageFont.FreeTypeFont,
    icon_font: ImageFont.FreeTypeFont,
) -> None:
    icon_size = 23
    draw.rounded_rectangle(
        (x, y, x + icon_size, y + icon_size),
        radius=5,
        fill=(28, 38, 48),
        outline=(149, 111, 55),
        width=1,
    )
    draw.text(
        (x + icon_size // 2, y + icon_size // 2),
        _profession_initials(name),
        font=icon_font,
        fill=_GOLD_LIGHT,
        anchor="mm",
    )
    value_width = min(82, max(54, width // 4))
    label_width = max(20, width - icon_size - 8 - value_width - 4)
    label_font = font
    label = _fit_text(draw, name, label_font, label_width, limit=48)
    value_text = _fit_text(draw, value, font, value_width, limit=32)
    draw.text((x + icon_size + 7, y + 3), label, font=label_font, fill=_PAPER)
    draw.text((x + width, y + 3), value_text, font=font, fill=_PAPER, anchor="ra")


def _draw_footer(
    draw: ImageDraw.ImageDraw,
    character: GraphicCharacter,
    panel: tuple[int, int, int, int],
    count: int,
) -> None:
    left, top, right, _ = panel
    padding = 22
    inner_left, inner_right = left + padding, right - padding
    gap = 14
    column_width = (inner_right - inner_left - gap) // 2
    pvp_x = inner_left
    updated_x = inner_left + column_width + gap
    heading_size = 19 if count == 3 else 21
    body_size = 15 if count == 3 else 17
    _section_heading(draw, "PVP", pvp_x, top + 615, column_width, size=heading_size)
    _section_heading(
        draw, "LAST UPDATED", updated_x, top + 615, column_width, size=heading_size
    )
    body_font = _font(body_size)

    pvp_lines = format_pvp(character.pvp)
    pvp_color = _MUTED if pvp_lines == ("No PvP Data",) else _PAPER
    for index, line in enumerate(pvp_lines[:2]):
        fitted = _fit_text(draw, line, body_font, column_width, limit=90)
        draw.text(
            (pvp_x, top + 652 + index * 24), fitted, font=body_font, fill=pvp_color
        )

    updated = _timestamp_label(character.last_updated)
    updated_text = _fit_text(draw, updated, body_font, column_width, limit=64)
    draw.text((updated_x, top + 652), updated_text, font=body_font, fill=_PAPER)
    freshness = _freshness_label(character)
    if freshness:
        freshness_font = _font(11, bold=True)
        draw.text((updated_x, top + 681), freshness, font=freshness_font, fill=_MUTED)


def _freshness_label(character: GraphicCharacter) -> str:
    if _is_outdated(character.last_updated):
        return "OUTDATED"
    if character.cached is True:
        return "CACHED DATA"
    if character.cached is False:
        return "FRESH DATA"
    return "SOURCE UNKNOWN"


def _draw_panel(
    canvas: Image.Image,
    character: GraphicCharacter,
    panel: tuple[int, int, int, int],
    count: int,
) -> None:
    left, top, right, bottom = panel
    draw = ImageDraw.Draw(canvas)
    _draw_ornamental_border(draw, panel)
    # A restrained inner highlight keeps the dark textured canvas readable.
    draw.rounded_rectangle(
        (left + 13, top + 13, right - 13, bottom - 13), radius=11, fill=(8, 18, 32)
    )
    draw.rounded_rectangle(
        (left + 13, top + 13, right - 13, bottom - 13),
        radius=11,
        outline=(54, 65, 76),
        width=1,
    )

    _draw_identity(canvas, character, panel)
    draw = ImageDraw.Draw(canvas)
    inner_left, inner_right = left + 20, right - 20
    has_stats = bool(_stat_lines(character))
    middle_offset, profession_heading, profession_rows, profession_gap = (
        _panel_section_layout(has_stats)
    )
    middle_divider = top + middle_offset
    for y in (top + 180, middle_divider, top + 585):
        draw.line((inner_left, y, inner_right, y), fill=(157, 110, 48), width=2)
        draw.line(
            (inner_left + 3, y + 3, inner_right - 3, y + 3), fill=(58, 51, 43), width=1
        )
    _draw_stats(draw, character, panel, count)
    _draw_professions(
        draw,
        character,
        panel,
        count,
        heading_y=top + profession_heading,
        row_start=top + profession_rows,
        row_gap=profession_gap,
    )
    _draw_footer(draw, character, panel, count)


def render_armory_overview(request: ArmoryGraphicRequest) -> bytes:
    """Render a safe, in-memory PNG for one, two, or three linked characters."""
    count = len(request.characters)
    width, height = output_size(count)
    canvas = _background((width, height))
    try:
        logo = _load_logo()
    except GraphicAssetError:
        # Keep development/test builds usable without redistributing an
        # unlicensed logo; do not draw a substitute for the official mark.
        logo = None
    _draw_header(canvas, request.discord_display_name, logo)

    margin, gap, top, bottom = 20, 18, 142, height - 20
    panel_width = (width - margin * 2 - gap * (count - 1)) // count
    for index, character in enumerate(request.characters):
        left = margin + index * (panel_width + gap)
        right = left + panel_width
        _draw_panel(canvas, character, (left, top, right, bottom), count)

    stream = BytesIO()
    canvas.save(stream, format="PNG", optimize=True, compress_level=6)
    result = stream.getvalue()
    if len(result) > _MAX_OUTPUT_BYTES:
        raise GraphicRenderError("The generated overview exceeds the upload limit")
    return result


def render_stored_member_overview(
    state: object,
    *,
    display_name: str | None = None,
    fresh_paths: frozenset[str] = frozenset(),
) -> bytes:
    """Convenience adapter for the existing SQLite member snapshot workflow."""
    return render_armory_overview(
        request_from_member_state(
            state, display_name=display_name, fresh_paths=fresh_paths
        )
    )
