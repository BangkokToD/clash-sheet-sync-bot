"""Repository состояния CWL-строк."""

from __future__ import annotations

import json
from dataclasses import dataclass

import aiosqlite

from .base import (
    as_json_dict,
    as_optional_int,
    as_optional_str,
    as_str,
    as_user_values,
    fetch_all,
)


@dataclass(frozen=True, slots=True)
class CwlRowState:
    """Состояние строки CWL из SQLite.

    Attributes:
        season: CWL-сезон.
        row_key: Стабильный ключ строки.
        clan_tag: Тег отслеживаемого клана.
        round_number: Номер раунда.
        attacker_tag: Тег атакующего или `None`.
        marker: Маркер строки.
        technical_values: Технические значения строки.
        user_values: Пользовательские значения по `column_key`.
        row_hash: Hash технических значений или `None`.
    """

    season: str
    row_key: str
    clan_tag: str
    round_number: int | None
    attacker_tag: str | None
    marker: str
    technical_values: dict[str, object]
    user_values: dict[str, str]
    row_hash: str | None


class CwlRowStateRepository:
    """Repository состояния CWL-строк.

    Args:
        connection: Открытое SQLite-подключение.
    """

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def list_rows(self, *, chat_id: int, season: str) -> tuple[CwlRowState, ...]:
        """Читает CWL row state одного сезона.

        Args:
            chat_id: ID Telegram-чата.
            season: CWL-сезон.

        Returns:
            Строки CWL state.
        """

        rows = await fetch_all(
            self._connection,
            """
            SELECT
                season,
                row_key,
                clan_tag,
                round_number,
                attacker_tag,
                marker,
                technical_values_json,
                user_values_json,
                row_hash
            FROM cwl_row_state
            WHERE chat_id = ? AND season = ?
            """,
            (chat_id, season),
        )
        return tuple(_row_to_cwl_row_state(row) for row in rows)

    async def upsert_row_state(
        self,
        *,
        chat_id: int,
        season: str,
        row_key: str,
        clan_tag: str,
        round_number: int | None,
        attacker_tag: str | None,
        marker: str,
        technical_values: dict[str, object],
        user_values: dict[str, str],
        row_hash: str,
    ) -> None:
        """Создаёт или обновляет CWL row state.

        Args:
            chat_id: ID Telegram-чата.
            season: CWL-сезон.
            row_key: Стабильный ключ строки.
            clan_tag: Тег отслеживаемого клана.
            round_number: Номер раунда.
            attacker_tag: Тег атакующего или `None`.
            marker: Маркер строки.
            technical_values: Технические значения строки.
            user_values: Пользовательские значения.
            row_hash: Hash технических значений.
        """

        await self._connection.execute(
            """
            INSERT INTO cwl_row_state(
                chat_id,
                season,
                row_key,
                clan_tag,
                round_number,
                attacker_tag,
                marker,
                technical_values_json,
                user_values_json,
                row_hash,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            ON CONFLICT(chat_id, season, row_key) DO UPDATE SET
                clan_tag = excluded.clan_tag,
                round_number = excluded.round_number,
                attacker_tag = excluded.attacker_tag,
                marker = excluded.marker,
                technical_values_json = excluded.technical_values_json,
                user_values_json = excluded.user_values_json,
                row_hash = excluded.row_hash,
                updated_at = excluded.updated_at
            """,
            (
                chat_id,
                season,
                row_key,
                clan_tag,
                round_number,
                attacker_tag,
                marker,
                json.dumps(technical_values, ensure_ascii=False, sort_keys=True),
                json.dumps(user_values, ensure_ascii=False, sort_keys=True),
                row_hash,
            ),
        )


def _row_to_cwl_row_state(row: aiosqlite.Row) -> CwlRowState:
    """Преобразует SQLite-строку в `CwlRowState`."""

    return CwlRowState(
        season=as_str(row["season"], "season"),
        row_key=as_str(row["row_key"], "row_key"),
        clan_tag=as_str(row["clan_tag"], "clan_tag"),
        round_number=as_optional_int(row["round_number"], "round_number"),
        attacker_tag=as_optional_str(row["attacker_tag"], "attacker_tag"),
        marker=as_str(row["marker"], "marker"),
        technical_values=as_json_dict(row["technical_values_json"], "technical_values_json"),
        user_values=as_user_values(row["user_values_json"]),
        row_hash=as_optional_str(row["row_hash"], "row_hash"),
    )
