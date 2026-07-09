"""Repository managed-блоков Google Sheets."""

from __future__ import annotations

import aiosqlite

from models import SheetBlock

from .base import as_int, as_optional_int, as_str, fetch_all


class SheetBlockRepository:
    """Repository последних управляемых прямоугольников Google Sheets.

    Args:
        connection: Открытое SQLite-подключение.
    """

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def list_blocks(
        self, chat_id: int, sheet_name: str | None = None
    ) -> tuple[SheetBlock, ...]:
        """Читает последние записанные блоки чата."""

        sql = """
            SELECT chat_id, sheet_name, sheet_id, block_key, start_cell, rows_count, columns_count
            FROM sheet_blocks
            WHERE chat_id = ?
        """
        parameters: tuple[object, ...] = (chat_id,)
        if sheet_name is not None:
            sql += " AND sheet_name = ?"
            parameters = (chat_id, sheet_name)
        sql += " ORDER BY sheet_name ASC, block_key ASC"
        rows = await fetch_all(self._connection, sql, parameters)
        return tuple(
            SheetBlock(
                chat_id=as_int(row["chat_id"], "chat_id"),
                sheet_name=as_str(row["sheet_name"], "sheet_name"),
                sheet_id=as_optional_int(row["sheet_id"], "sheet_id"),
                block_key=as_str(row["block_key"], "block_key"),
                start_cell=as_str(row["start_cell"], "start_cell"),
                rows_count=as_int(row["rows_count"], "rows_count"),
                columns_count=as_int(row["columns_count"], "columns_count"),
            )
            for row in rows
        )

    async def upsert_block(self, *, block: SheetBlock, updated_at: str) -> None:
        """Создаёт или обновляет запись управляемого блока."""

        await self._connection.execute(
            """
            INSERT INTO sheet_blocks(
                chat_id,
                sheet_name,
                sheet_id,
                block_key,
                start_cell,
                rows_count,
                columns_count,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, sheet_name, block_key) DO UPDATE SET
                sheet_id = excluded.sheet_id,
                start_cell = excluded.start_cell,
                rows_count = excluded.rows_count,
                columns_count = excluded.columns_count,
                updated_at = excluded.updated_at
            """,
            (
                block.chat_id,
                block.sheet_name,
                block.sheet_id,
                block.block_key,
                block.start_cell,
                block.rows_count,
                block.columns_count,
                updated_at,
            ),
        )

    async def replace_blocks_by_prefixes(
        self,
        *,
        chat_id: int,
        sheet_name: str,
        block_key_prefixes: tuple[str, ...],
        blocks: tuple[SheetBlock, ...],
        updated_at: str,
    ) -> None:
        """Заменяет набор block records для листа по prefix-фильтру.

        Args:
            chat_id: ID Telegram-чата.
            sheet_name: Название листа.
            block_key_prefixes: Prefixes block_key, которые нужно заменить.
            blocks: Новый набор блоков.
            updated_at: ISO-дата обновления.
        """

        if block_key_prefixes:
            conditions = " OR ".join("block_key LIKE ?" for _ in block_key_prefixes)
            parameters: tuple[object, ...] = (
                chat_id,
                sheet_name,
                *(f"{prefix}%" for prefix in block_key_prefixes),
            )
            await self._connection.execute(
                f"""
                DELETE FROM sheet_blocks
                WHERE chat_id = ?
                  AND sheet_name = ?
                  AND ({conditions})
                """,
                parameters,
            )

        for block in blocks:
            await self.upsert_block(block=block, updated_at=updated_at)
