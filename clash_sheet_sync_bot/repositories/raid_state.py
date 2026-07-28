"""Repository агрегированного raid state и registry архивов."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass

import aiosqlite

from .base import (
    as_int,
    as_json_dict,
    as_optional_str,
    as_str,
    as_user_values,
    fetch_all,
    fetch_one,
)


@dataclass(frozen=True, slots=True)
class RaidPlayerState:
    """Сохранённая агрегированная строка рейдового сезона."""

    chat_id: int
    season_key: str
    season_start_at: str
    season_end_at: str
    season_state: str
    row_key: str
    clan_tag: str
    player_tag: str
    technical_values: dict[str, object]
    user_values: dict[str, str]
    row_hash: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class RaidSheetArchive:
    """Зарегистрированный bot-owned архив рейдового листа."""

    chat_id: int
    season_key: str
    season_start_at: str
    sheet_name: str
    sheet_id: int
    archived_at: str


class RaidPlayerStateRepository:
    """Repository агрегированных строк рейдовых сезонов."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def list_for_season(
        self,
        *,
        chat_id: int,
        season_key: str,
        clan_tags: tuple[str, ...] | None = None,
    ) -> tuple[RaidPlayerState, ...]:
        """Читает строки сезона, при необходимости ограниченные кланами."""

        sql = "SELECT * FROM raid_player_state WHERE chat_id = ? AND season_key = ?"
        parameters: tuple[object, ...] = (chat_id, season_key)
        if clan_tags is not None:
            if not clan_tags:
                return ()
            placeholders = ", ".join("?" for _ in clan_tags)
            sql += f" AND clan_tag IN ({placeholders})"
            parameters = (*parameters, *clan_tags)
        sql += " ORDER BY clan_tag ASC, row_key ASC"
        return tuple(
            _row_to_player_state(row) for row in await fetch_all(self._connection, sql, parameters)
        )

    async def get_latest_season_key(
        self,
        *,
        chat_id: int,
        clan_tags: tuple[str, ...],
    ) -> str | None:
        """Возвращает самый новый сохранённый сезон активных кланов."""

        if not clan_tags:
            return None
        placeholders = ", ".join("?" for _ in clan_tags)
        row = await fetch_one(
            self._connection,
            f"""
            SELECT season_key
            FROM raid_player_state
            WHERE chat_id = ? AND clan_tag IN ({placeholders})
            ORDER BY season_start_at DESC, season_key DESC
            LIMIT 1
            """,
            (chat_id, *clan_tags),
        )
        return None if row is None else as_str(row["season_key"], "season_key")

    async def upsert(self, state: RaidPlayerState) -> None:
        """Идемпотентно сохраняет агрегированную строку."""

        await self._connection.execute(
            """
            INSERT INTO raid_player_state(
                chat_id, season_key, season_start_at, season_end_at, season_state,
                row_key, clan_tag, player_tag, technical_values_json,
                user_values_json, row_hash, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, season_key, row_key) DO UPDATE SET
                season_start_at = excluded.season_start_at,
                season_end_at = excluded.season_end_at,
                season_state = excluded.season_state,
                clan_tag = excluded.clan_tag,
                player_tag = excluded.player_tag,
                technical_values_json = excluded.technical_values_json,
                user_values_json = excluded.user_values_json,
                row_hash = excluded.row_hash,
                updated_at = excluded.updated_at
            """,
            (
                state.chat_id,
                state.season_key,
                state.season_start_at,
                state.season_end_at,
                state.season_state,
                state.row_key,
                state.clan_tag,
                state.player_tag,
                json.dumps(state.technical_values, ensure_ascii=False, separators=(",", ":")),
                json.dumps(state.user_values, ensure_ascii=False, separators=(",", ":")),
                state.row_hash,
                state.updated_at,
            ),
        )


class RaidSheetArchiveRepository:
    """Repository bot-owned registry рейдовых архивов."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def list_ordered(self, chat_id: int) -> tuple[RaidSheetArchive, ...]:
        """Читает архивы от самого старого к новому."""

        rows = await fetch_all(
            self._connection,
            """
            SELECT * FROM raid_sheet_archives
            WHERE chat_id = ?
            ORDER BY season_start_at ASC, archived_at ASC, sheet_id ASC
            """,
            (chat_id,),
        )
        return tuple(_row_to_archive(row) for row in rows)

    async def get_by_season(self, *, chat_id: int, season_key: str) -> RaidSheetArchive | None:
        """Читает архив по сезону."""

        row = await fetch_one(
            self._connection,
            "SELECT * FROM raid_sheet_archives WHERE chat_id = ? AND season_key = ?",
            (chat_id, season_key),
        )
        return None if row is None else _row_to_archive(row)

    async def get_by_sheet_id(self, *, chat_id: int, sheet_id: int) -> RaidSheetArchive | None:
        """Читает архив по физическому sheet ID."""

        row = await fetch_one(
            self._connection,
            "SELECT * FROM raid_sheet_archives WHERE chat_id = ? AND sheet_id = ?",
            (chat_id, sheet_id),
        )
        return None if row is None else _row_to_archive(row)

    async def upsert(self, archive: RaidSheetArchive) -> None:
        """Регистрирует архив по season identity."""

        await self._connection.execute(
            """
            INSERT INTO raid_sheet_archives(
                chat_id, season_key, season_start_at, sheet_name, sheet_id, archived_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, season_key) DO UPDATE SET
                season_start_at = excluded.season_start_at,
                sheet_name = excluded.sheet_name,
                sheet_id = excluded.sheet_id,
                archived_at = excluded.archived_at
            """,
            (
                archive.chat_id,
                archive.season_key,
                archive.season_start_at,
                archive.sheet_name,
                archive.sheet_id,
                archive.archived_at,
            ),
        )

    async def delete(self, *, chat_id: int, season_key: str) -> None:
        """Удаляет registry одного сезона, не затрагивая player state."""

        await self._connection.execute(
            "DELETE FROM raid_sheet_archives WHERE chat_id = ? AND season_key = ?",
            (chat_id, season_key),
        )

    async def remove_stale(self, *, chat_id: int, existing_sheet_ids: Iterable[int]) -> int:
        """Удаляет registry записей, физические sheet ID которых отсутствуют."""

        existing = tuple(existing_sheet_ids)
        if not existing:
            cursor = await self._connection.execute(
                "DELETE FROM raid_sheet_archives WHERE chat_id = ?",
                (chat_id,),
            )
            return cursor.rowcount
        placeholders = ", ".join("?" for _ in existing)
        cursor = await self._connection.execute(
            f"DELETE FROM raid_sheet_archives WHERE chat_id = ? AND sheet_id NOT IN ({placeholders})",
            (chat_id, *existing),
        )
        return cursor.rowcount


def _row_to_player_state(row: aiosqlite.Row) -> RaidPlayerState:
    return RaidPlayerState(
        chat_id=as_int(row["chat_id"], "chat_id"),
        season_key=as_str(row["season_key"], "season_key"),
        season_start_at=as_str(row["season_start_at"], "season_start_at"),
        season_end_at=as_str(row["season_end_at"], "season_end_at"),
        season_state=as_str(row["season_state"], "season_state"),
        row_key=as_str(row["row_key"], "row_key"),
        clan_tag=as_str(row["clan_tag"], "clan_tag"),
        player_tag=as_str(row["player_tag"], "player_tag"),
        technical_values=as_json_dict(row["technical_values_json"], "technical_values_json"),
        user_values=as_user_values(row["user_values_json"]),
        row_hash=as_optional_str(row["row_hash"], "row_hash"),
        updated_at=as_str(row["updated_at"], "updated_at"),
    )


def _row_to_archive(row: aiosqlite.Row) -> RaidSheetArchive:
    return RaidSheetArchive(
        chat_id=as_int(row["chat_id"], "chat_id"),
        season_key=as_str(row["season_key"], "season_key"),
        season_start_at=as_str(row["season_start_at"], "season_start_at"),
        sheet_name=as_str(row["sheet_name"], "sheet_name"),
        sheet_id=as_int(row["sheet_id"], "sheet_id"),
        archived_at=as_str(row["archived_at"], "archived_at"),
    )
