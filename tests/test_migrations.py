"""Smoke-тесты SQLite migrations."""

from __future__ import annotations

import aiosqlite
import pytest

from migrations import SCHEMA_VERSION, apply_migrations

NOW = "2026-07-09T12:00:00+00:00"


async def _insert_chat(connection: aiosqlite.Connection, *, chat_id: int) -> None:
    """Создаёт Telegram chat для FK column_profiles."""

    await connection.execute(
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
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (chat_id, "Test group", "supergroup", "ready", 1001, NOW, NOW),
    )


async def _insert_column_profile(
    connection: aiosqlite.Connection,
    *,
    chat_id: int,
    table_type: str,
    column_key: str,
    title: str,
    visible: bool = True,
    is_active: bool = True,
    sort_order: int = 10,
    kind: str = "system",
    value_type: str = "string",
) -> None:
    """Создаёт column_profile для migration tests."""

    await connection.execute(
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
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chat_id,
            table_type,
            column_key,
            title,
            int(visible),
            int(is_active),
            sort_order,
            kind,
            value_type,
            NOW,
            NOW,
        ),
    )


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


@pytest.mark.asyncio
async def test_migration_2_copies_legacy_composition_profile_to_active_and_exited(
    migrated_connection: aiosqlite.Connection,
) -> None:
    """Проверяет миграцию legacy composition profile в active/exited профили."""

    chat_id = -2001
    await _insert_chat(migrated_connection, chat_id=chat_id)
    await _insert_column_profile(
        migrated_connection,
        chat_id=chat_id,
        table_type="composition",
        column_key="bot_key",
        title="__bot_key",
        visible=False,
        kind="service",
        sort_order=0,
    )
    await _insert_column_profile(
        migrated_connection,
        chat_id=chat_id,
        table_type="composition",
        column_key="nickname",
        title="Никнейм",
        sort_order=40,
    )
    await _insert_column_profile(
        migrated_connection,
        chat_id=chat_id,
        table_type="composition",
        column_key="note",
        title="Заметка",
        sort_order=45,
        kind="user",
    )
    await _insert_column_profile(
        migrated_connection,
        chat_id=chat_id,
        table_type="composition",
        column_key="exited_at",
        title="Дата выхода",
        sort_order=50,
        value_type="datetime",
    )
    await _insert_column_profile(
        migrated_connection,
        chat_id=chat_id,
        table_type="composition_active",
        column_key="exited_at",
        title="Дата выхода",
        sort_order=50,
        value_type="datetime",
    )

    await migrated_connection.execute(
        "DELETE FROM schema_migrations WHERE version = ?",
        (SCHEMA_VERSION,),
    )
    await migrated_connection.commit()

    await apply_migrations(migrated_connection)
    await apply_migrations(migrated_connection)

    cursor = await migrated_connection.execute(
        """
        SELECT table_type, column_key, visible, is_active
        FROM column_profiles
        WHERE chat_id = ?
          AND table_type IN ('composition_active', 'composition_exited')
        ORDER BY table_type, column_key
        """,
        (chat_id,),
    )
    rows = await cursor.fetchall()
    state_by_key = {
        (row["table_type"], row["column_key"]): (row["visible"], row["is_active"]) for row in rows
    }

    assert ("composition_active", "bot_key") in state_by_key
    assert ("composition_active", "nickname") in state_by_key
    assert ("composition_active", "note") in state_by_key
    assert state_by_key[("composition_active", "exited_at")] == (0, 0)

    assert ("composition_exited", "bot_key") in state_by_key
    assert ("composition_exited", "nickname") in state_by_key
    assert ("composition_exited", "note") in state_by_key
    assert state_by_key[("composition_exited", "exited_at")] == (1, 1)

    cursor = await migrated_connection.execute(
        "SELECT COUNT(*) AS count FROM schema_migrations WHERE version = ?",
        (SCHEMA_VERSION,),
    )
    row = await cursor.fetchone()

    assert row is not None
    assert row["count"] == 1
