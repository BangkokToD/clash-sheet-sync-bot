"""Repository привязок Google Sheets и runtime-конфига."""

from __future__ import annotations

import aiosqlite

from models import ColumnProfile, RuntimeChatConfig, SheetBinding, TelegramChatStatus, TrackedClan

from .base import (
    as_chat_status,
    as_int,
    as_optional_int,
    as_optional_str,
    as_str,
    fetch_all,
    fetch_one,
)
from .columns import _row_to_column_profile


class RuntimeConfigRepository:
    """Repository для чтения runtime-настроек чата.

    Args:
        connection: Открытое SQLite-подключение.
    """

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def get_chat_status(self, chat_id: int) -> TelegramChatStatus | None:
        """Читает статус Telegram-чата.

        Args:
            chat_id: ID Telegram-чата.

        Returns:
            Статус чата или `None`, если чат неизвестен.
        """

        row = await fetch_one(
            self._connection,
            "SELECT status FROM telegram_chats WHERE chat_id = ?",
            (chat_id,),
        )
        if row is None:
            return None
        return as_chat_status(row["status"])

    async def get_runtime_chat_config(self, chat_id: int) -> RuntimeChatConfig | None:
        """Собирает runtime-конфиг чата.

        Args:
            chat_id: ID Telegram-чата.

        Returns:
            Runtime-конфиг или `None`, если чат/активная таблица не найдены.
        """

        chat_row = await fetch_one(
            self._connection,
            "SELECT chat_id, status FROM telegram_chats WHERE chat_id = ?",
            (chat_id,),
        )
        if chat_row is None:
            return None

        binding = await self.get_active_sheet_binding(chat_id)
        if binding is None:
            return None

        return RuntimeChatConfig(
            chat_id=chat_id,
            status=as_chat_status(chat_row["status"]),
            sheet_binding=binding,
            active_clans=await self.list_active_clans(chat_id),
            column_profiles=await self.list_column_profiles(chat_id),
            timezone=binding.timezone,
        )

    async def get_active_sheet_binding(self, chat_id: int) -> SheetBinding | None:
        """Читает активную привязку Google Sheets для чата.

        Args:
            chat_id: ID Telegram-чата.

        Returns:
            Привязка таблицы или `None`.
        """

        row = await fetch_one(
            self._connection,
            """
            SELECT
                chat_id,
                google_sheet_id,
                spreadsheet_url,
                composition_sheet_name,
                composition_sheet_id,
                active_cwl_sheet_name,
                active_cwl_sheet_id,
                active_cwl_season,
                bot_state_sheet_name,
                bot_state_sheet_id,
                timezone
            FROM sheet_bindings
            WHERE chat_id = ? AND is_active = 1
            """,
            (chat_id,),
        )
        if row is None:
            return None
        return SheetBinding(
            chat_id=as_int(row["chat_id"], "chat_id"),
            google_sheet_id=as_str(row["google_sheet_id"], "google_sheet_id"),
            spreadsheet_url=as_str(row["spreadsheet_url"], "spreadsheet_url"),
            composition_sheet_name=as_str(row["composition_sheet_name"], "composition_sheet_name"),
            composition_sheet_id=as_optional_int(
                row["composition_sheet_id"], "composition_sheet_id"
            ),
            active_cwl_sheet_name=as_str(row["active_cwl_sheet_name"], "active_cwl_sheet_name"),
            active_cwl_sheet_id=as_optional_int(row["active_cwl_sheet_id"], "active_cwl_sheet_id"),
            active_cwl_season=as_optional_str(row["active_cwl_season"], "active_cwl_season"),
            bot_state_sheet_name=as_str(row["bot_state_sheet_name"], "bot_state_sheet_name"),
            bot_state_sheet_id=as_optional_int(row["bot_state_sheet_id"], "bot_state_sheet_id"),
            timezone=as_str(row["timezone"], "timezone"),
        )

    async def list_active_clans(self, chat_id: int) -> tuple[TrackedClan, ...]:
        """Читает активные кланы чата в порядке вывода.

        Args:
            chat_id: ID Telegram-чата.

        Returns:
            Кортеж активных кланов.
        """

        rows = await fetch_all(
            self._connection,
            """
            SELECT chat_id, clan_tag, clan_name, sort_order
            FROM tracked_clans
            WHERE chat_id = ? AND is_active = 1
            ORDER BY sort_order ASC, clan_tag ASC
            """,
            (chat_id,),
        )
        return tuple(
            TrackedClan(
                chat_id=as_int(row["chat_id"], "chat_id"),
                clan_tag=as_str(row["clan_tag"], "clan_tag"),
                clan_name=as_str(row["clan_name"], "clan_name"),
                sort_order=as_int(row["sort_order"], "sort_order"),
            )
            for row in rows
        )

    async def list_column_profiles(self, chat_id: int) -> tuple[ColumnProfile, ...]:
        """Читает активные профили колонок чата.

        Args:
            chat_id: ID Telegram-чата.

        Returns:
            Кортеж колонок всех профилей.
        """

        rows = await fetch_all(
            self._connection,
            """
            SELECT
                chat_id,
                table_type,
                column_key,
                title,
                visible,
                is_active,
                sort_order,
                kind,
                value_type
            FROM column_profiles
            WHERE chat_id = ? AND is_active = 1
            ORDER BY table_type ASC, sort_order ASC, column_key ASC
            """,
            (chat_id,),
        )
        return tuple(_row_to_column_profile(row) for row in rows)

    async def is_google_sheet_bound_elsewhere(
        self,
        google_sheet_id: str,
        *,
        current_chat_id: int | None = None,
    ) -> bool:
        """Проверяет, занята ли Google-таблица другим активным чатом.

        Args:
            google_sheet_id: ID Google Spreadsheet.
            current_chat_id: ID текущего чата, который нужно исключить из проверки.

        Returns:
            `True`, если таблица уже активно привязана к другому чату.
        """

        sql = """
            SELECT 1
            FROM sheet_bindings
            WHERE google_sheet_id = ? AND is_active = 1
        """
        parameters: tuple[object, ...] = (google_sheet_id,)
        if current_chat_id is not None:
            sql += " AND chat_id != ?"
            parameters = (google_sheet_id, current_chat_id)
        row = await fetch_one(self._connection, f"{sql} LIMIT 1", parameters)
        return row is not None


class SheetBindingRepository:
    """Repository привязок Telegram-чата к Google Sheets.

    Args:
        connection: Открытое SQLite-подключение.
    """

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def upsert_active_binding(
        self,
        *,
        chat_id: int,
        google_sheet_id: str,
        spreadsheet_url: str,
        composition_sheet_name: str,
        composition_sheet_id: int,
        active_cwl_sheet_name: str,
        active_cwl_sheet_id: int,
        active_cwl_season: str | None,
        bot_state_sheet_name: str,
        bot_state_sheet_id: int,
        timezone: str,
        now: str,
    ) -> None:
        """Создаёт или обновляет активную привязку таблицы."""

        await self._connection.execute(
            """
            INSERT INTO sheet_bindings(
                chat_id,
                google_sheet_id,
                spreadsheet_url,
                composition_sheet_name,
                composition_sheet_id,
                active_cwl_sheet_name,
                active_cwl_sheet_id,
                active_cwl_season,
                bot_state_sheet_name,
                bot_state_sheet_id,
                timezone,
                is_active,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                google_sheet_id = excluded.google_sheet_id,
                spreadsheet_url = excluded.spreadsheet_url,
                composition_sheet_name = excluded.composition_sheet_name,
                composition_sheet_id = excluded.composition_sheet_id,
                active_cwl_sheet_name = excluded.active_cwl_sheet_name,
                active_cwl_sheet_id = excluded.active_cwl_sheet_id,
                active_cwl_season = excluded.active_cwl_season,
                bot_state_sheet_name = excluded.bot_state_sheet_name,
                bot_state_sheet_id = excluded.bot_state_sheet_id,
                timezone = excluded.timezone,
                is_active = 1,
                updated_at = excluded.updated_at
            """,
            (
                chat_id,
                google_sheet_id,
                spreadsheet_url,
                composition_sheet_name,
                composition_sheet_id,
                active_cwl_sheet_name,
                active_cwl_sheet_id,
                active_cwl_season,
                bot_state_sheet_name,
                bot_state_sheet_id,
                timezone,
                now,
                now,
            ),
        )

    async def update_active_cwl_binding(
        self,
        *,
        chat_id: int,
        active_cwl_sheet_name: str,
        active_cwl_sheet_id: int,
        active_cwl_season: str,
        now: str,
    ) -> None:
        """Обновляет активный CWL-лист binding после записи/архивации.

        Args:
            chat_id: ID Telegram-чата.
            active_cwl_sheet_name: Название активного CWL-листа.
            active_cwl_sheet_id: Числовой ID активного CWL-листа.
            active_cwl_season: Активный CWL-сезон.
            now: ISO-дата обновления.
        """

        await self._connection.execute(
            """
            UPDATE sheet_bindings
            SET active_cwl_sheet_name = ?,
                active_cwl_sheet_id = ?,
                active_cwl_season = ?,
                updated_at = ?
            WHERE chat_id = ? AND is_active = 1
            """,
            (active_cwl_sheet_name, active_cwl_sheet_id, active_cwl_season, now, chat_id),
        )

    async def update_sheet_ids(
        self,
        *,
        chat_id: int,
        composition_sheet_name: str,
        composition_sheet_id: int,
        active_cwl_sheet_name: str,
        active_cwl_sheet_id: int,
        active_cwl_season: str | None,
        bot_state_sheet_name: str,
        bot_state_sheet_id: int,
        now: str,
    ) -> None:
        """Обновляет sheet IDs после диагностики/auto-fix."""

        await self._connection.execute(
            """
            UPDATE sheet_bindings
            SET composition_sheet_name = ?,
                composition_sheet_id = ?,
                active_cwl_sheet_name = ?,
                active_cwl_sheet_id = ?,
                active_cwl_season = ?,
                bot_state_sheet_name = ?,
                bot_state_sheet_id = ?,
                updated_at = ?
            WHERE chat_id = ? AND is_active = 1
            """,
            (
                composition_sheet_name,
                composition_sheet_id,
                active_cwl_sheet_name,
                active_cwl_sheet_id,
                active_cwl_season,
                bot_state_sheet_name,
                bot_state_sheet_id,
                now,
                chat_id,
            ),
        )

    async def deactivate_binding(self, *, chat_id: int, now: str) -> None:
        """Деактивирует привязку таблицы без изменения Google Sheets."""

        await self._connection.execute(
            """
            UPDATE sheet_bindings
            SET is_active = 0, updated_at = ?
            WHERE chat_id = ? AND is_active = 1
            """,
            (now, chat_id),
        )

    async def has_active_binding(self, chat_id: int) -> bool:
        """Проверяет, есть ли у чата активная привязка таблицы."""

        row = await fetch_one(
            self._connection,
            "SELECT 1 FROM sheet_bindings WHERE chat_id = ? AND is_active = 1 LIMIT 1",
            (chat_id,),
        )
        return row is not None

    async def transfer_binding_to_chat(
        self,
        *,
        source_chat_id: int,
        target_chat_id: int,
        now: str,
    ) -> None:
        """Переносит активную привязку таблицы на другой Telegram-чат."""

        await self._connection.execute(
            "DELETE FROM sheet_bindings WHERE chat_id = ? AND is_active = 0",
            (target_chat_id,),
        )
        await self._connection.execute(
            """
            UPDATE sheet_bindings
            SET chat_id = ?, updated_at = ?
            WHERE chat_id = ? AND is_active = 1
            """,
            (target_chat_id, now, source_chat_id),
        )
