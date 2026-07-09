"""Repository Telegram-чатов и lifecycle-операций."""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite

from clash_sheet_sync_bot.models import TelegramChatStatus

from .base import as_chat_status, as_int, as_optional_str, as_str, fetch_one


@dataclass(frozen=True, slots=True)
class PendingSheetLinkSetup:
    """Ожидаемый ввод ссылки на таблицу в личном чате.

    Attributes:
        chat_id: ID настраиваемой Telegram-группы.
        title: Название Telegram-группы.
        setup_state: Текущее состояние setup-flow.
    """

    chat_id: int
    title: str
    setup_state: str


@dataclass(frozen=True, slots=True)
class SyncStatusSummary:
    """Сводка последнего sync для `/status`.

    Attributes:
        chat_id: ID Telegram-чата.
        status: Статус настройки чата.
        last_sync_started_at: Дата последнего принятого `/sync` или `None`.
        last_sync_finished_at: Дата завершения последнего `/sync` или `None`.
        last_sync_status: Статус последнего `/sync` или `None`.
        last_sync_error: Ошибка последнего `/sync` или `None`.
        active_clans_count: Количество active clans.
        active_cwl_season: Активный CWL-сезон или `None`.
        spreadsheet_url: Ссылка на таблицу или `None`.
    """

    chat_id: int
    status: TelegramChatStatus
    last_sync_started_at: str | None
    last_sync_finished_at: str | None
    last_sync_status: str | None
    last_sync_error: str | None
    active_clans_count: int
    active_cwl_season: str | None
    spreadsheet_url: str | None


class TelegramChatRepository:
    """Repository Telegram-чатов и связей с администраторами.

    Args:
        connection: Открытое SQLite-подключение.
    """

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def upsert_connected_chat(
        self,
        *,
        chat_id: int,
        title: str,
        chat_type: str,
        created_by_user_id: int,
        now: str,
    ) -> None:
        """Создаёт или обновляет подключённый Telegram-чат."""

        await self._connection.execute(
            """
            INSERT INTO telegram_chats(
                chat_id,
                title,
                type,
                status,
                created_by_user_id,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, 'waiting_for_sheet', ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                type = excluded.type,
                status = CASE
                    WHEN telegram_chats.status IN ('disabled', 'not_configured')
                    THEN 'waiting_for_sheet'
                    ELSE telegram_chats.status
                END,
                created_by_user_id = COALESCE(
                    telegram_chats.created_by_user_id,
                    excluded.created_by_user_id
                ),
                updated_at = excluded.updated_at
            """,
            (chat_id, title, chat_type, created_by_user_id, now, now),
        )

    async def upsert_known_chat(
        self,
        *,
        chat_id: int,
        title: str,
        chat_type: str,
        now: str,
    ) -> None:
        """Создаёт или обновляет известный, но ещё не настроенный Telegram-чат."""

        await self._connection.execute(
            """
            INSERT INTO telegram_chats(
                chat_id,
                title,
                type,
                status,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, 'not_configured', ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                type = excluded.type,
                updated_at = excluded.updated_at
            """,
            (chat_id, title, chat_type, now, now),
        )

    async def upsert_admin_link(
        self,
        *,
        chat_id: int,
        user_id: int,
        linked_at: str,
        last_admin_check_at: str | None = None,
    ) -> None:
        """Создаёт или реактивирует связь пользователя с группой."""

        await self._connection.execute(
            """
            INSERT INTO chat_admin_links(chat_id, user_id, is_active, linked_at, last_admin_check_at)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                is_active = 1,
                last_admin_check_at = COALESCE(
                    excluded.last_admin_check_at,
                    chat_admin_links.last_admin_check_at
                )
            """,
            (chat_id, user_id, linked_at, last_admin_check_at),
        )

    async def update_admin_check_at(self, *, chat_id: int, user_id: int, checked_at: str) -> None:
        """Обновляет дату положительной проверки Telegram-админа."""

        await self._connection.execute(
            """
            UPDATE chat_admin_links
            SET last_admin_check_at = ?
            WHERE chat_id = ? AND user_id = ? AND is_active = 1
            """,
            (checked_at, chat_id, user_id),
        )

    async def has_active_admin_link(self, *, chat_id: int, user_id: int) -> bool:
        """Проверяет, есть ли активная связь пользователя с группой."""

        row = await fetch_one(
            self._connection,
            """
            SELECT 1
            FROM chat_admin_links
            WHERE chat_id = ? AND user_id = ? AND is_active = 1
            LIMIT 1
            """,
            (chat_id, user_id),
        )
        return row is not None

    async def get_setup_state(self, chat_id: int) -> str | None:
        """Читает setup_state Telegram-чата."""
        row = await fetch_one(
            self._connection,
            "SELECT setup_state FROM telegram_chats WHERE chat_id = ?",
            (chat_id,),
        )
        if row is None:
            return None
        return as_optional_str(row["setup_state"], "setup_state")

    async def set_setup_state(self, *, chat_id: int, setup_state: str | None, now: str) -> None:
        """Обновляет setup_state Telegram-чата."""

        await self._connection.execute(
            """
            UPDATE telegram_chats
            SET setup_state = ?, updated_at = ?
            WHERE chat_id = ?
            """,
            (setup_state, now, chat_id),
        )

    async def clear_setup_states_for_user(self, *, user_id: int, now: str) -> int:
        """Сбрасывает ожидающие setup_state, принадлежащие пользователю.

        Метод чистит только состояния групп, где у пользователя есть активная
        admin-связь. Это не даёт одному пользователю сбросить чужую настройку
        группы через личную команду `/cancel`.

        Args:
            user_id: Telegram user ID пользователя, отправившего `/cancel`.
            now: ISO-дата обновления.

        Returns:
            Количество Telegram-групп, где setup_state был сброшен.
        """

        setup_state_patterns = (
            f"awaiting_sheet_link:{user_id}",
            f"awaiting_sheet_access:{user_id}:*",
            f"awaiting_clan_tag:{user_id}",
            f"awaiting_user_column_title:{user_id}:*",
            f"awaiting_column_rename:{user_id}:*",
        )
        conditions = " OR ".join("telegram_chats.setup_state GLOB ?" for _ in setup_state_patterns)
        cursor = await self._connection.execute(
            f"""
            UPDATE telegram_chats
            SET setup_state = NULL,
                updated_at = ?
            WHERE setup_state IS NOT NULL
              AND ({conditions})
              AND EXISTS (
                  SELECT 1
                  FROM chat_admin_links
                  WHERE chat_admin_links.chat_id = telegram_chats.chat_id
                    AND chat_admin_links.user_id = ?
                    AND chat_admin_links.is_active = 1
              )
            """,
            (now, *setup_state_patterns, user_id),
        )
        return cursor.rowcount

    async def set_status(self, *, chat_id: int, status: TelegramChatStatus, now: str) -> None:
        """Обновляет статус Telegram-чата."""

        await self._connection.execute(
            """
            UPDATE telegram_chats
            SET status = ?, updated_at = ?
            WHERE chat_id = ?
            """,
            (status, now, chat_id),
        )

    async def disable_chat(self, *, chat_id: int, now: str) -> None:
        """Отключает Telegram-группу без удаления исторических данных."""

        await self._connection.execute(
            """
            UPDATE telegram_chats
            SET status = 'disabled', setup_state = NULL, updated_at = ?
            WHERE chat_id = ?
            """,
            (now, chat_id),
        )

    async def mark_sync_started(self, *, chat_id: int, started_at: str) -> None:
        """Фиксирует момент принятия `/sync` для rate limit."""

        await self._connection.execute(
            """
            UPDATE telegram_chats
            SET last_sync_started_at = ?, updated_at = ?
            WHERE chat_id = ?
            """,
            (started_at, started_at, chat_id),
        )

    async def mark_sync_finished(
        self,
        *,
        chat_id: int,
        finished_at: str,
        status: str,
        error: str | None,
    ) -> None:
        """Фиксирует результат последнего `/sync` для `/status`."""

        await self._connection.execute(
            """
            UPDATE telegram_chats
            SET last_sync_finished_at = ?,
                last_sync_status = ?,
                last_sync_error = ?,
                updated_at = ?
            WHERE chat_id = ?
            """,
            (finished_at, status, error, finished_at, chat_id),
        )

    async def get_last_sync_started_at(self, chat_id: int) -> str | None:
        """Читает дату последнего принятого `/sync`."""

        row = await fetch_one(
            self._connection,
            "SELECT last_sync_started_at FROM telegram_chats WHERE chat_id = ?",
            (chat_id,),
        )
        if row is None:
            return None
        return as_optional_str(row["last_sync_started_at"], "last_sync_started_at")

    async def get_sync_status_summary(self, chat_id: int) -> SyncStatusSummary | None:
        """Собирает данные для `/status`."""

        row = await fetch_one(
            self._connection,
            """
            SELECT
                c.chat_id,
                c.status,
                c.last_sync_started_at,
                c.last_sync_finished_at,
                c.last_sync_status,
                c.last_sync_error,
                b.spreadsheet_url,
                b.active_cwl_season,
                COUNT(tc.clan_tag) AS active_clans_count
            FROM telegram_chats AS c
            LEFT JOIN sheet_bindings AS b
                ON b.chat_id = c.chat_id AND b.is_active = 1
            LEFT JOIN tracked_clans AS tc
                ON tc.chat_id = c.chat_id AND tc.is_active = 1
            WHERE c.chat_id = ?
            GROUP BY c.chat_id
            """,
            (chat_id,),
        )
        if row is None:
            return None
        return SyncStatusSummary(
            chat_id=as_int(row["chat_id"], "chat_id"),
            status=as_chat_status(row["status"]),
            last_sync_started_at=as_optional_str(
                row["last_sync_started_at"], "last_sync_started_at"
            ),
            last_sync_finished_at=as_optional_str(
                row["last_sync_finished_at"], "last_sync_finished_at"
            ),
            last_sync_status=as_optional_str(row["last_sync_status"], "last_sync_status"),
            last_sync_error=as_optional_str(row["last_sync_error"], "last_sync_error"),
            active_clans_count=as_int(row["active_clans_count"], "active_clans_count"),
            active_cwl_season=as_optional_str(row["active_cwl_season"], "active_cwl_season"),
            spreadsheet_url=as_optional_str(row["spreadsheet_url"], "spreadsheet_url"),
        )

    async def find_pending_sheet_link_setup(
        self,
        *,
        user_id: int,
        state_prefix: str,
    ) -> PendingSheetLinkSetup | None:
        """Ищет группу, ожидающую ссылку на таблицу от пользователя."""

        row = await fetch_one(
            self._connection,
            """
            SELECT c.chat_id, c.title, c.setup_state
            FROM telegram_chats AS c
            JOIN chat_admin_links AS l ON l.chat_id = c.chat_id
            WHERE l.user_id = ?
              AND l.is_active = 1
              AND c.setup_state LIKE ?
            ORDER BY c.updated_at DESC
            LIMIT 1
            """,
            (user_id, f"{state_prefix}%"),
        )
        if row is None:
            return None
        return PendingSheetLinkSetup(
            chat_id=as_int(row["chat_id"], "chat_id"),
            title=as_str(row["title"], "title"),
            setup_state=as_str(row["setup_state"], "setup_state"),
        )

    async def get_admin_check_at(self, *, chat_id: int, user_id: int) -> str | None:
        """Читает дату последней положительной проверки Telegram-админа."""

        row = await fetch_one(
            self._connection,
            """
            SELECT last_admin_check_at
            FROM chat_admin_links
            WHERE chat_id = ? AND user_id = ? AND is_active = 1
            """,
            (chat_id, user_id),
        )
        if row is None:
            return None
        return as_optional_str(row["last_admin_check_at"], "last_admin_check_at")


class ChatLifecycleRepository:
    """Repository массовых lifecycle-операций над настройками чата."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def move_runtime_state(
        self,
        *,
        source_chat_id: int,
        target_chat_id: int,
        now: str,
    ) -> None:
        """Переносит кланы, профили, state и blocks на новый chat_id."""

        for table_name in (
            "tracked_clans",
            "column_profiles",
            "composition_player_state",
            "cwl_row_state",
            "sheet_blocks",
        ):
            await self._connection.execute(
                f"DELETE FROM {table_name} WHERE chat_id = ?",
                (target_chat_id,),
            )
            await self._connection.execute(
                f"UPDATE {table_name} SET chat_id = ? WHERE chat_id = ?",
                (target_chat_id, source_chat_id),
            )

        await self._connection.execute(
            """
            UPDATE telegram_chats
            SET status = 'disabled', setup_state = NULL, updated_at = ?
            WHERE chat_id = ?
            """,
            (now, source_chat_id),
        )
        await self._connection.execute(
            """
            UPDATE telegram_chats
            SET status = 'ready', setup_state = NULL, updated_at = ?
            WHERE chat_id = ?
            """,
            (now, target_chat_id),
        )
