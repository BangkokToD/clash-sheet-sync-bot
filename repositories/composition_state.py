"""Repository состояния игроков состава."""

from __future__ import annotations

import json
from dataclasses import dataclass

import aiosqlite

from models import CompositionPlayerStatus

from .base import (
    as_composition_player_status,
    as_optional_int,
    as_optional_str,
    as_str,
    as_user_values,
    fetch_all,
)


@dataclass(frozen=True, slots=True)
class CompositionPlayerState:
    """Состояние игрока состава из SQLite.

    Attributes:
        player_tag: Нормализованный тег игрока.
        status: Статус игрока: active, exited или untracked.
        clan_tag: Тег активного клана или `None`.
        town_hall: Уровень ратуши или `None`.
        nickname: Никнейм игрока или `None`.
        exited_at: ISO-дата выхода или `None`.
        user_values: Пользовательские значения по `column_key`.
        last_seen_at: ISO-дата последнего наблюдения в CoC API или `None`.
    """

    player_tag: str
    status: CompositionPlayerStatus
    clan_tag: str | None
    town_hall: int | None
    nickname: str | None
    exited_at: str | None
    user_values: dict[str, str]
    last_seen_at: str | None


class CompositionPlayerStateRepository:
    """Repository состояния игроков состава.

    Args:
        connection: Открытое SQLite-подключение.
    """

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def list_players(self, chat_id: int) -> tuple[CompositionPlayerState, ...]:
        """Читает все состояния игроков состава чата."""

        rows = await fetch_all(
            self._connection,
            """
            SELECT
                player_tag,
                clan_tag,
                status,
                town_hall,
                nickname,
                exited_at,
                user_values_json,
                last_seen_at
            FROM composition_player_state
            WHERE chat_id = ?
            """,
            (chat_id,),
        )
        return tuple(_row_to_composition_player_state(row) for row in rows)

    async def upsert_player_state(
        self,
        *,
        chat_id: int,
        player_tag: str,
        status: str,
        clan_tag: str | None,
        town_hall: int | None,
        nickname: str | None,
        exited_at: str | None,
        user_values: dict[str, str],
        last_seen_at: str | None,
        updated_at: str,
    ) -> None:
        """Создаёт или обновляет состояние игрока состава."""

        await self._connection.execute(
            """
            INSERT INTO composition_player_state(
                chat_id,
                player_tag,
                clan_tag,
                status,
                town_hall,
                nickname,
                exited_at,
                user_values_json,
                last_seen_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, player_tag) DO UPDATE SET
                clan_tag = excluded.clan_tag,
                status = excluded.status,
                town_hall = excluded.town_hall,
                nickname = excluded.nickname,
                exited_at = excluded.exited_at,
                user_values_json = excluded.user_values_json,
                last_seen_at = excluded.last_seen_at,
                updated_at = excluded.updated_at
            """,
            (
                chat_id,
                player_tag,
                clan_tag,
                status,
                town_hall,
                nickname,
                exited_at,
                json.dumps(user_values, ensure_ascii=False, sort_keys=True),
                last_seen_at,
                updated_at,
            ),
        )


def _row_to_composition_player_state(row: aiosqlite.Row) -> CompositionPlayerState:
    """Преобразует SQLite-строку в `CompositionPlayerState`."""

    return CompositionPlayerState(
        player_tag=as_str(row["player_tag"], "player_tag"),
        status=as_composition_player_status(row["status"]),
        clan_tag=as_optional_str(row["clan_tag"], "clan_tag"),
        town_hall=as_optional_int(row["town_hall"], "town_hall"),
        nickname=as_optional_str(row["nickname"], "nickname"),
        exited_at=as_optional_str(row["exited_at"], "exited_at"),
        user_values=as_user_values(row["user_values_json"]),
        last_seen_at=as_optional_str(row["last_seen_at"], "last_seen_at"),
    )
