"""Repository истории запусков sync."""

from __future__ import annotations

import aiosqlite

from models import SyncRunStatus

from .base import RepositoryError, fetch_one


class SyncRunRepository:
    """Repository для истории запусков `/sync`.

    Args:
        connection: Открытое SQLite-подключение.
    """

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def create_sync_run(
        self,
        *,
        chat_id: int,
        started_by_user_id: int,
        status: SyncRunStatus,
        started_at: str,
    ) -> int:
        """Создаёт запись запуска sync."""

        cursor = await self._connection.execute(
            """
            INSERT INTO sync_runs(chat_id, started_by_user_id, status, started_at)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, started_by_user_id, status, started_at),
        )
        if cursor.lastrowid is None:
            raise RepositoryError("SQLite не вернул id созданного sync_runs.")
        return cursor.lastrowid

    async def has_successful_sync(self, chat_id: int) -> bool:
        """Проверяет, был ли успешный sync у чата."""

        row = await fetch_one(
            self._connection,
            """
            SELECT 1
            FROM sync_runs
            WHERE chat_id = ? AND status = 'success'
            LIMIT 1
            """,
            (chat_id,),
        )
        return row is not None

    async def finish_sync_run(
        self,
        *,
        sync_run_id: int,
        status: SyncRunStatus,
        finished_at: str,
        error_stage: str | None = None,
        error_clan_tag: str | None = None,
        error_war_tag: str | None = None,
        error_message: str | None = None,
        report_json: str | None = None,
    ) -> None:
        """Завершает запись `sync_runs`."""

        await self._connection.execute(
            """
            UPDATE sync_runs
            SET status = ?,
                finished_at = ?,
                error_stage = ?,
                error_clan_tag = ?,
                error_war_tag = ?,
                error_message = ?,
                report_json = ?
            WHERE id = ?
            """,
            (
                status,
                finished_at,
                error_stage,
                error_clan_tag,
                error_war_tag,
                error_message,
                report_json,
                sync_run_id,
            ),
        )
