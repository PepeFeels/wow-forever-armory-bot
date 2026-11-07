"""Transactional SQLite persistence for the Discord Guild Armory."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from .models import Character, Profession, ProfessionTier

T = TypeVar("T")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _character_json(character: Character) -> str:
    return json.dumps(
        {
            "name": character.name,
            "level": character.level,
            "race": character.race,
            "character_class": character.character_class,
            "active_specialization": character.active_specialization,
            "specialization_id": character.specialization_id,
            "spent_talent_points": character.spent_talent_points,
            "armory_updated_at": (
                character.armory_updated_at.isoformat()
                if character.armory_updated_at
                else None
            ),
            "professions": [
                {
                    "name": profession.name,
                    "skill": profession.skill,
                    "max_skill": profession.max_skill,
                    "id": profession.id,
                    "category": profession.category,
                    "tiers": [
                        {
                            "id": tier.id,
                            "name": tier.name,
                            "skill": tier.skill,
                            "max_skill": tier.max_skill,
                        }
                        for tier in profession.tiers
                    ],
                }
                for profession in character.professions
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _character_from_json(payload: str) -> Character:
    value = json.loads(payload)
    timestamp = value.get("armory_updated_at")
    professions = tuple(
        Profession(
            name=item.get("name"),
            skill=item.get("skill"),
            max_skill=item.get("max_skill"),
            id=item.get("id"),
            category=item.get("category"),
            tiers=tuple(
                ProfessionTier(
                    id=tier.get("id"),
                    name=tier.get("name"),
                    skill=tier.get("skill"),
                    max_skill=tier.get("max_skill"),
                )
                for tier in item.get("tiers", [])
            ),
        )
        for item in value.get("professions", [])
    )
    return Character(
        name=value.get("name"),
        level=value.get("level"),
        race=value.get("race"),
        character_class=value.get("character_class"),
        active_specialization=value.get("active_specialization"),
        specialization_id=value.get("specialization_id"),
        spent_talent_points=value.get("spent_talent_points"),
        professions=professions,
        armory_updated_at=datetime.fromisoformat(timestamp) if timestamp else None,
    )


@dataclass(frozen=True, slots=True)
class GuildConfig:
    guild_id: int
    channel_id: int
    panel_message_id: int


@dataclass(frozen=True, slots=True)
class StoredCharacter:
    path: str
    slot: int
    character: Character
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class MemberState:
    guild_id: int
    user_id: int
    display_name: str
    row_message_id: int | None
    manage_message_id: int | None
    characters: tuple[StoredCharacter, ...]


class CharacterLimitReached(ValueError):
    """Member already has the maximum number of characters."""


class DuplicateCharacter(ValueError):
    """Character path is already registered by this member."""


class CharacterNotFound(ValueError):
    """Character to remove or replace is not registered by this member."""


class GuildArmoryStore:
    """Run short SQLite operations under a process-local async lock."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    @contextmanager
    def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            connection.rollback()
            raise
        else:
            try:
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    async def _run(self, operation: Callable[[], T]) -> T:
        async with self._lock:
            return operation()

    async def initialize(self) -> None:
        def operation() -> None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS guilds (
                        guild_id INTEGER PRIMARY KEY,
                        channel_id INTEGER NOT NULL,
                        panel_message_id INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS members (
                        guild_id INTEGER NOT NULL REFERENCES guilds(guild_id)
                            ON DELETE CASCADE,
                        user_id INTEGER NOT NULL,
                        display_name TEXT NOT NULL,
                        row_message_id INTEGER,
                        manage_message_id INTEGER,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (guild_id, user_id)
                    );
                    CREATE TABLE IF NOT EXISTS characters (
                        guild_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        path TEXT NOT NULL,
                        slot INTEGER NOT NULL CHECK (slot BETWEEN 0 AND 2),
                        character_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (guild_id, user_id, path),
                        UNIQUE (guild_id, user_id, slot),
                        FOREIGN KEY (guild_id, user_id)
                            REFERENCES members(guild_id, user_id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS flows (
                        guild_id INTEGER NOT NULL REFERENCES guilds(guild_id)
                            ON DELETE CASCADE,
                        user_id INTEGER NOT NULL,
                        panel_message_id INTEGER NOT NULL,
                        dm_message_id INTEGER,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (guild_id, user_id)
                    );
                    """
                )

        await self._run(operation)

    async def activate_guild(
        self, guild_id: int, channel_id: int, panel_message_id: int
    ) -> None:
        now = _now()

        def operation() -> None:
            with self._connection() as connection:
                connection.execute(
                    """INSERT INTO guilds VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id) DO UPDATE SET
                        channel_id=excluded.channel_id,
                        panel_message_id=excluded.panel_message_id,
                        updated_at=excluded.updated_at""",
                    (guild_id, channel_id, panel_message_id, now, now),
                )

        await self._run(operation)

    @staticmethod
    def _guild(row: sqlite3.Row | None) -> GuildConfig | None:
        if row is None:
            return None
        return GuildConfig(row["guild_id"], row["channel_id"], row["panel_message_id"])

    async def get_guild(self, guild_id: int) -> GuildConfig | None:
        def operation() -> GuildConfig | None:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT * FROM guilds WHERE guild_id=?", (guild_id,)
                ).fetchone()
                return self._guild(row)

        return await self._run(operation)

    async def get_guild_by_panel(self, message_id: int) -> GuildConfig | None:
        def operation() -> GuildConfig | None:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT * FROM guilds WHERE panel_message_id=?", (message_id,)
                ).fetchone()
                return self._guild(row)

        return await self._run(operation)

    async def guilds(self) -> tuple[GuildConfig, ...]:
        def operation() -> tuple[GuildConfig, ...]:
            with self._connection() as connection:
                return tuple(
                    config
                    for row in connection.execute("SELECT * FROM guilds").fetchall()
                    if (config := self._guild(row)) is not None
                )

        return await self._run(operation)

    async def begin_flow(
        self,
        guild_id: int,
        user_id: int,
        panel_message_id: int,
        *,
        allow_registered: bool = False,
    ) -> str:
        """Return started, already_pending, already_full, or unconfigured."""

        def operation() -> str:
            with self._connection() as connection, self._transaction(connection):
                if (
                    connection.execute(
                        "SELECT 1 FROM guilds WHERE guild_id=?", (guild_id,)
                    ).fetchone()
                    is None
                ):
                    return "unconfigured"
                if connection.execute(
                    "SELECT 1 FROM flows WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                ).fetchone():
                    return "already_pending"
                count = connection.execute(
                    "SELECT count(*) FROM characters WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                ).fetchone()[0]
                if count and not allow_registered:
                    return "already_registered"
                if count >= 3:
                    return "already_full"
                connection.execute(
                    "INSERT INTO flows VALUES (?, ?, ?, NULL, ?)",
                    (guild_id, user_id, panel_message_id, _now()),
                )
                return "started"

        return await self._run(operation)

    async def flow_message(self, guild_id: int, user_id: int) -> int | None:
        def operation() -> int | None:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT dm_message_id FROM flows WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                ).fetchone()
                return row[0] if row else None

        return await self._run(operation)

    async def has_character(self, guild_id: int, user_id: int, path: str) -> bool:
        def operation() -> bool:
            with self._connection() as connection:
                return (
                    connection.execute(
                        "SELECT 1 FROM characters "
                        "WHERE guild_id=? AND user_id=? AND path=?",
                        (guild_id, user_id, path),
                    ).fetchone()
                    is not None
                )

        return await self._run(operation)

    async def set_flow_message(
        self, guild_id: int, user_id: int, message_id: int
    ) -> None:
        def operation() -> None:
            with self._connection() as connection:
                connection.execute(
                    "UPDATE flows SET dm_message_id=? WHERE guild_id=? AND user_id=?",
                    (message_id, guild_id, user_id),
                )

        await self._run(operation)

    async def pending_flows(self) -> tuple[tuple[int, int, int | None], ...]:
        def operation() -> tuple[tuple[int, int, int | None], ...]:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT guild_id, user_id, dm_message_id FROM flows"
                ).fetchall()
                return tuple((row[0], row[1], row[2]) for row in rows)

        return await self._run(operation)

    async def finish_flow(
        self,
        guild_id: int,
        user_id: int,
        display_name: str,
        path: str,
        character: Character,
        *,
        replace_path: str | None = None,
    ) -> MemberState:
        """Atomically add or replace a character and clear its pending flow."""
        payload = _character_json(character)

        def operation() -> MemberState:
            with self._connection() as connection, self._transaction(connection):
                now = _now()
                connection.execute(
                    """INSERT INTO members
                        (guild_id, user_id, display_name, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id) DO UPDATE SET
                        display_name=excluded.display_name,
                        updated_at=excluded.updated_at""",
                    (guild_id, user_id, display_name, now),
                )
                if replace_path is None:
                    if connection.execute(
                        "SELECT 1 FROM characters "
                        "WHERE guild_id=? AND user_id=? AND path=?",
                        (guild_id, user_id, path),
                    ).fetchone():
                        raise DuplicateCharacter
                    count = connection.execute(
                        "SELECT count(*) FROM characters "
                        "WHERE guild_id=? AND user_id=?",
                        (guild_id, user_id),
                    ).fetchone()[0]
                    if count >= 3:
                        raise CharacterLimitReached
                    slot = count
                    connection.execute(
                        """INSERT INTO characters VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (guild_id, user_id, path, slot, payload, now, now),
                    )
                else:
                    target = connection.execute(
                        """SELECT slot FROM characters
                        WHERE guild_id=? AND user_id=? AND path=?""",
                        (guild_id, user_id, replace_path),
                    ).fetchone()
                    if target is None:
                        raise CharacterNotFound
                    if (
                        path != replace_path
                        and connection.execute(
                            "SELECT 1 FROM characters "
                            "WHERE guild_id=? AND user_id=? AND path=?",
                            (guild_id, user_id, path),
                        ).fetchone()
                    ):
                        raise DuplicateCharacter
                    connection.execute(
                        """UPDATE characters SET path=?, character_json=?, updated_at=?
                        WHERE guild_id=? AND user_id=? AND path=?""",
                        (path, payload, now, guild_id, user_id, replace_path),
                    )
                connection.execute(
                    "DELETE FROM flows WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                )
                return self._member(connection, guild_id, user_id)

        return await self._run(operation)

    async def remove_character(
        self, guild_id: int, user_id: int, path: str
    ) -> MemberState:
        def operation() -> MemberState:
            with self._connection() as connection, self._transaction(connection):
                deleted = connection.execute(
                    "DELETE FROM characters WHERE guild_id=? AND user_id=? AND path=?",
                    (guild_id, user_id, path),
                ).rowcount
                if not deleted:
                    raise CharacterNotFound
                rows = connection.execute(
                    """SELECT path FROM characters WHERE guild_id=? AND user_id=?
                    ORDER BY slot""",
                    (guild_id, user_id),
                ).fetchall()
                for slot, row in enumerate(rows):
                    connection.execute(
                        """UPDATE characters SET slot=?
                        WHERE guild_id=? AND user_id=? AND path=?""",
                        (slot, guild_id, user_id, row[0]),
                    )
                return self._member(connection, guild_id, user_id)

        return await self._run(operation)

    @staticmethod
    def _member(
        connection: sqlite3.Connection, guild_id: int, user_id: int
    ) -> MemberState:
        member = connection.execute(
            "SELECT * FROM members WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ).fetchone()
        if member is None:
            raise CharacterNotFound
        rows = connection.execute(
            """SELECT * FROM characters WHERE guild_id=? AND user_id=? ORDER BY slot""",
            (guild_id, user_id),
        ).fetchall()
        return MemberState(
            guild_id=guild_id,
            user_id=user_id,
            display_name=member["display_name"],
            row_message_id=member["row_message_id"],
            manage_message_id=member["manage_message_id"],
            characters=tuple(
                StoredCharacter(
                    path=row["path"],
                    slot=row["slot"],
                    character=_character_from_json(row["character_json"]),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                for row in rows
            ),
        )

    async def member(self, guild_id: int, user_id: int) -> MemberState | None:
        def operation() -> MemberState | None:
            with self._connection() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM members WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                ).fetchone()
                return self._member(connection, guild_id, user_id) if exists else None

        return await self._run(operation)

    async def members(self, guild_id: int) -> tuple[MemberState, ...]:
        def operation() -> tuple[MemberState, ...]:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT user_id FROM members WHERE guild_id=?", (guild_id,)
                ).fetchall()
                return tuple(self._member(connection, guild_id, row[0]) for row in rows)

        return await self._run(operation)

    async def set_row_message(
        self, guild_id: int, user_id: int, message_id: int | None
    ) -> None:
        def operation() -> None:
            with self._connection() as connection:
                connection.execute(
                    """UPDATE members SET row_message_id=?, updated_at=?
                    WHERE guild_id=? AND user_id=?""",
                    (message_id, _now(), guild_id, user_id),
                )

        await self._run(operation)

    async def set_manage_message(
        self, guild_id: int, user_id: int, message_id: int
    ) -> None:
        def operation() -> None:
            with self._connection() as connection:
                connection.execute(
                    """UPDATE members SET manage_message_id=?, updated_at=?
                    WHERE guild_id=? AND user_id=?""",
                    (message_id, _now(), guild_id, user_id),
                )

        await self._run(operation)
