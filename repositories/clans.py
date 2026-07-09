"""Repository настроек отслеживаемых кланов."""

from __future__ import annotations

import aiosqlite

from models import TrackedClan

from .base import as_int, fetch_one
from .bindings import RuntimeConfigRepository


class ClanSettingsRepository:
    """Repository настроек отслеживаемых кланов."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def count_active_clans(self, chat_id: int) -> int:
        """Считает активные кланы чата.

        Args:
            chat_id: ID Telegram-чата.

        Returns:
            Количество активных кланов.
        """

        row = await fetch_one(
            self._connection,
            "SELECT COUNT(*) AS cnt FROM tracked_clans WHERE chat_id = ? AND is_active = 1",
            (chat_id,),
        )
        return 0 if row is None else as_int(row["cnt"], "cnt")

    async def list_active_clans(self, chat_id: int) -> tuple[TrackedClan, ...]:
        """Читает активные кланы чата.

        Args:
            chat_id: ID Telegram-чата.

        Returns:
            Активные кланы.
        """

        return await RuntimeConfigRepository(self._connection).list_active_clans(chat_id)

    async def is_active_clan(self, *, chat_id: int, clan_tag: str) -> bool:
        """Проверяет, активен ли клан в чате."""

        row = await fetch_one(
            self._connection,
            """
            SELECT 1
            FROM tracked_clans
            WHERE chat_id = ? AND clan_tag = ? AND is_active = 1
            LIMIT 1
            """,
            (chat_id, clan_tag),
        )
        return row is not None

    async def upsert_or_reactivate_clan(
        self,
        *,
        chat_id: int,
        clan_tag: str,
        clan_name: str,
        now: str,
    ) -> None:
        """Создаёт или реактивирует отслеживаемый клан."""

        next_order = await self._next_sort_order(chat_id)
        await self._connection.execute(
            """
            INSERT INTO tracked_clans(
                chat_id,
                clan_tag,
                clan_name,
                sort_order,
                is_active,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(chat_id, clan_tag) DO UPDATE SET
                clan_name = excluded.clan_name,
                sort_order = excluded.sort_order,
                is_active = 1,
                updated_at = excluded.updated_at
            """,
            (chat_id, clan_tag, clan_name, next_order, now, now),
        )

    async def soft_delete_clan(self, *, chat_id: int, clan_tag: str, now: str) -> bool:
        """Мягко удаляет клан из отслеживания."""

        cursor = await self._connection.execute(
            """
            UPDATE tracked_clans
            SET is_active = 0, updated_at = ?
            WHERE chat_id = ? AND clan_tag = ? AND is_active = 1
            """,
            (now, chat_id, clan_tag),
        )
        return cursor.rowcount == 1

    async def mark_players_untracked(self, *, chat_id: int, clan_tag: str, now: str) -> None:
        """Помечает игроков удалённого клана как `untracked`."""

        await self._connection.execute(
            """
            UPDATE composition_player_state
            SET status = 'untracked', updated_at = ?
            WHERE chat_id = ? AND clan_tag = ? AND status = 'active'
            """,
            (now, chat_id, clan_tag),
        )

    async def move_clan(self, *, chat_id: int, clan_tag: str, direction: str, now: str) -> bool:
        """Меняет порядок активного клана."""

        clans = list(await self.list_active_clans(chat_id))
        current_index = next(
            (index for index, clan in enumerate(clans) if clan.clan_tag == clan_tag),
            None,
        )
        if current_index is None:
            return False
        target_index = current_index - 1 if direction == "up" else current_index + 1
        if target_index < 0 or target_index >= len(clans):
            return False
        current = clans[current_index]
        target = clans[target_index]
        await self._connection.execute(
            """
            UPDATE tracked_clans
            SET sort_order = ?, updated_at = ?
            WHERE chat_id = ? AND clan_tag = ?
            """,
            (target.sort_order, now, chat_id, current.clan_tag),
        )
        await self._connection.execute(
            """
            UPDATE tracked_clans
            SET sort_order = ?, updated_at = ?
            WHERE chat_id = ? AND clan_tag = ?
            """,
            (current.sort_order, now, chat_id, target.clan_tag),
        )
        return True

    async def _next_sort_order(self, chat_id: int) -> int:
        """Вычисляет следующий sort_order для клана."""

        row = await fetch_one(
            self._connection,
            """
            SELECT COALESCE(MAX(sort_order), 0) AS max_order
            FROM tracked_clans
            WHERE chat_id = ?
            """,
            (chat_id,),
        )
        return 10 if row is None else as_int(row["max_order"], "max_order") + 10
