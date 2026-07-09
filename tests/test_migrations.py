"""Smoke-тесты SQLite migrations."""

from __future__ import annotations

import aiosqlite
import pytest

from migrations import SCHEMA_VERSION, apply_migrations


@pytest.mark.asyncio
async def test_apply_migrations_creates_required_tables(
    migrated_connection: aiosqlite.Connection,
) -> None:
    """Проверяет, что migrations создают основные runtime-таблицы."""

    cursor = await migrated_connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        """
    )
    rows = await cursor.fetchall()
    table_names = {row["name"] for row in rows}

    assert {
        "schema_migrations",
        "telegram_chats",
        "chat_admin_links",
        "setup_tokens",
        "sheet_bindings",
        "tracked_clans",
        "column_profiles",
        "composition_player_state",
        "cwl_row_state",
        "sheet_blocks",
        "sync_runs",
        "transfer_tokens",
    }.issubset(table_names)


@pytest.mark.asyncio
async def test_apply_migrations_is_idempotent(
    migrated_connection: aiosqlite.Connection,
) -> None:
    """Проверяет, что повторное применение migrations безопасно."""

    await apply_migrations(migrated_connection)
    await apply_migrations(migrated_connection)

    cursor = await migrated_connection.execute(
        "SELECT COUNT(*) AS count FROM schema_migrations WHERE version = ?",
        (SCHEMA_VERSION,),
    )
    row = await cursor.fetchone()

    assert row is not None
    assert row["count"] == 1


@pytest.mark.asyncio
async def test_apply_migrations_records_current_schema_version(
    migrated_connection: aiosqlite.Connection,
) -> None:
    """Проверяет запись актуальной версии схемы."""

    cursor = await migrated_connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
    )
    row = await cursor.fetchone()

    assert row is not None
    assert row["version"] == SCHEMA_VERSION