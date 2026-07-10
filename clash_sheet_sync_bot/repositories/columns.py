"""Repository профилей колонок."""

from __future__ import annotations

import aiosqlite

from clash_sheet_sync_bot.models import ColumnProfile, TableType
from clash_sheet_sync_bot.sheets.column_profiles import (
    all_default_columns,
    column_title_identity,
    default_columns,
)

from .base import (
    as_bool_int,
    as_column_kind,
    as_column_value_type,
    as_int,
    as_str,
    as_table_type,
    fetch_all,
    fetch_one,
)


class ColumnProfileRepository:
    """Repository управления профилями колонок."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def ensure_default_profiles(self, *, chat_id: int, now: str) -> None:
        """Создаёт отсутствующие дефолтные колонки всех профилей."""

        for definition in all_default_columns():
            await self._connection.execute(
                """
                INSERT OR IGNORE INTO column_profiles(
                    chat_id,
                    table_type,
                    column_key,
                    title,
                    visible,
                    is_active,
                    sort_order,
                    kind,
                    value_type,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    definition.table_type,
                    definition.column_key,
                    definition.title,
                    int(definition.visible),
                    definition.sort_order,
                    definition.kind,
                    definition.value_type,
                    now,
                    now,
                ),
            )

    async def restore_defaults(self, *, chat_id: int, table_type: TableType, now: str) -> None:
        """Восстанавливает обязательные service/system колонки."""

        for definition in default_columns(table_type):
            await self._connection.execute(
                """
                INSERT INTO column_profiles(
                    chat_id,
                    table_type,
                    column_key,
                    title,
                    visible,
                    is_active,
                    sort_order,
                    kind,
                    value_type,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, table_type, column_key) DO UPDATE SET
                    title = excluded.title,
                    visible = excluded.visible,
                    is_active = 1,
                    sort_order = excluded.sort_order,
                    kind = excluded.kind,
                    value_type = excluded.value_type,
                    updated_at = excluded.updated_at
                """,
                (
                    chat_id,
                    definition.table_type,
                    definition.column_key,
                    definition.title,
                    int(definition.visible),
                    definition.sort_order,
                    definition.kind,
                    definition.value_type,
                    now,
                    now,
                ),
            )

    async def list_columns(
        self, *, chat_id: int, table_type: TableType
    ) -> tuple[ColumnProfile, ...]:
        """Читает активные колонки одного профиля."""

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
            WHERE chat_id = ? AND table_type = ? AND is_active = 1
            ORDER BY sort_order ASC, column_key ASC
            """,
            (chat_id, table_type),
        )
        return tuple(_row_to_column_profile(row) for row in rows)

    async def set_visibility(
        self,
        *,
        chat_id: int,
        table_type: TableType,
        column_key: str,
        visible: bool,
        now: str,
    ) -> bool:
        """Меняет видимость колонки, кроме service-колонок."""

        cursor = await self._connection.execute(
            """
            UPDATE column_profiles
            SET visible = ?, updated_at = ?
            WHERE chat_id = ?
              AND table_type = ?
              AND column_key = ?
              AND kind != 'service'
              AND is_active = 1
            """,
            (int(visible), now, chat_id, table_type, column_key),
        )
        return cursor.rowcount == 1

    async def rename_column(
        self,
        *,
        chat_id: int,
        table_type: TableType,
        column_key: str,
        title: str,
        now: str,
    ) -> bool:
        """Переименовывает колонку, кроме service-колонок."""

        cursor = await self._connection.execute(
            """
            UPDATE column_profiles
            SET title = ?, updated_at = ?
            WHERE chat_id = ?
              AND table_type = ?
              AND column_key = ?
              AND kind != 'service'
              AND is_active = 1
            """,
            (title, now, chat_id, table_type, column_key),
        )
        return cursor.rowcount == 1

    async def title_exists(
        self,
        *,
        chat_id: int,
        table_type: TableType,
        title: str,
        excluding_column_key: str | None = None,
    ) -> bool:
        """Проверяет, есть ли активная колонка с таким названием в table_type."""

        target_title = column_title_identity(title)
        columns = await self.list_columns(chat_id=chat_id, table_type=table_type)
        return any(
            column_title_identity(column.title) == target_title
            and column.column_key != excluding_column_key
            for column in columns
        )

    async def create_user_column(
        self,
        *,
        chat_id: int,
        table_type: TableType,
        column_key: str,
        title: str,
        now: str,
    ) -> None:
        """Создаёт пользовательскую колонку."""

        sort_order = await self._next_sort_order(chat_id=chat_id, table_type=table_type)
        await self._connection.execute(
            """
            INSERT INTO column_profiles(
                chat_id,
                table_type,
                column_key,
                title,
                visible,
                is_active,
                sort_order,
                kind,
                value_type,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, 1, 1, ?, 'user', 'string', ?, ?)
            """,
            (chat_id, table_type, column_key, title, sort_order, now, now),
        )

    async def soft_delete_user_column(
        self,
        *,
        chat_id: int,
        table_type: TableType,
        column_key: str,
        now: str,
    ) -> bool:
        """Мягко удаляет пользовательскую колонку."""

        cursor = await self._connection.execute(
            """
            UPDATE column_profiles
            SET visible = 0, is_active = 0, updated_at = ?
            WHERE chat_id = ?
              AND table_type = ?
              AND column_key = ?
              AND kind = 'user'
              AND is_active = 1
            """,
            (now, chat_id, table_type, column_key),
        )
        return cursor.rowcount == 1

    async def move_column(
        self,
        *,
        chat_id: int,
        table_type: TableType,
        column_key: str,
        direction: str,
        now: str,
    ) -> bool:
        """Меняет порядок колонки, кроме service-колонки."""

        columns = [
            column
            for column in await self.list_columns(chat_id=chat_id, table_type=table_type)
            if column.kind != "service"
        ]
        current_index = next(
            (index for index, column in enumerate(columns) if column.column_key == column_key),
            None,
        )
        if current_index is None:
            return False
        target_index = current_index - 1 if direction == "up" else current_index + 1
        if target_index < 0 or target_index >= len(columns):
            return False
        current = columns[current_index]
        target = columns[target_index]
        await self._connection.execute(
            """
            UPDATE column_profiles
            SET sort_order = ?, updated_at = ?
            WHERE chat_id = ? AND table_type = ? AND column_key = ?
            """,
            (target.sort_order, now, chat_id, table_type, current.column_key),
        )
        await self._connection.execute(
            """
            UPDATE column_profiles
            SET sort_order = ?, updated_at = ?
            WHERE chat_id = ? AND table_type = ? AND column_key = ?
            """,
            (current.sort_order, now, chat_id, table_type, target.column_key),
        )
        return True

    async def _next_sort_order(self, *, chat_id: int, table_type: TableType) -> int:
        """Вычисляет sort_order для новой user-колонки."""

        row = await fetch_one(
            self._connection,
            """
            SELECT COALESCE(MAX(sort_order), 0) AS max_order
            FROM column_profiles
            WHERE chat_id = ? AND table_type = ?
            """,
            (chat_id, table_type),
        )
        return 10 if row is None else as_int(row["max_order"], "max_order") + 10


def _row_to_column_profile(row: aiosqlite.Row) -> ColumnProfile:
    """Преобразует SQLite-строку в `ColumnProfile`."""

    return ColumnProfile(
        chat_id=as_int(row["chat_id"], "chat_id"),
        table_type=as_table_type(row["table_type"]),
        column_key=as_str(row["column_key"], "column_key"),
        title=as_str(row["title"], "title"),
        visible=as_bool_int(row["visible"], "visible"),
        is_active=as_bool_int(row["is_active"], "is_active"),
        sort_order=as_int(row["sort_order"], "sort_order"),
        kind=as_column_kind(row["kind"]),
        value_type=as_column_value_type(row["value_type"]),
    )
