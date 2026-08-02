"""Smoke-тесты SQLite migrations."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from clash_sheet_sync_bot.migrations import (
    MIGRATION_SQL_BY_VERSION,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    apply_migrations,
)
from clash_sheet_sync_bot.storage import Database

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
        "raid_player_state",
        "raid_sheet_archives",
        "sheet_blocks",
        "sync_runs",
        "transfer_tokens",
    }.issubset(table_names)

    cursor = await migrated_connection.execute("PRAGMA table_info(sheet_bindings)")
    binding_columns = {row["name"] for row in await cursor.fetchall()}
    assert {
        "active_raid_sheet_name",
        "active_raid_sheet_id",
        "active_raid_season",
    }.issubset(binding_columns)


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
async def test_migration_4_upgrades_version_3_without_losing_existing_state(
    tmp_path: Path,
) -> None:
    """Проверяет реальный upgrade v3 и сохранность прежних данных/профилей."""

    database = Database(tmp_path / "version-3.db")
    async with database.connect() as connection:
        await connection.executescript(SCHEMA_SQL)
        for version in (1, 2, 3):
            if version > 1:
                await connection.executescript(MIGRATION_SQL_BY_VERSION[version])
            await connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
                (version,),
            )
        await _insert_chat(connection, chat_id=-4001)
        await connection.execute(
            """
            INSERT INTO sheet_bindings(
                chat_id, google_sheet_id, spreadsheet_url,
                composition_sheet_name, composition_sheet_id,
                active_cwl_sheet_name, active_cwl_sheet_id, active_cwl_season,
                bot_state_sheet_name, bot_state_sheet_id, timezone,
                is_active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                -4001,
                "sheet-v3",
                "https://example.test/sheet-v3",
                "Состав",
                111,
                "CWL",
                222,
                "2026-07",
                "_bot_state",
                333,
                "Europe/Kyiv",
                NOW,
                NOW,
            ),
        )
        await _insert_column_profile(
            connection,
            chat_id=-4001,
            table_type="composition_active",
            column_key="user_custom",
            title="Моя колонка",
            kind="user",
        )
        await connection.execute(
            """
            INSERT INTO composition_player_state(
                chat_id, player_tag, clan_tag, status, town_hall, nickname,
                user_values_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (-4001, "#PLAYER", "#CLAN", "active", 16, "Player", '{"user_custom":"keep"}', NOW),
        )
        await connection.execute(
            """
            INSERT INTO cwl_row_state(
                chat_id, season, row_key, clan_tag, round_number, attacker_tag,
                marker, technical_values_json, user_values_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (-4001, "2026-07", "row", "#CLAN", 1, "#PLAYER", "ATTACK", "{}", "{}", NOW),
        )
        await connection.commit()

        await apply_migrations(connection)
        await apply_migrations(connection)

        cursor = await connection.execute(
            """
            SELECT active_raid_sheet_name, active_raid_sheet_id, active_raid_season
            FROM sheet_bindings WHERE chat_id = ?
            """,
            (-4001,),
        )
        binding = await cursor.fetchone()
        assert tuple(binding) == ("Рейды", None, None)

        cursor = await connection.execute(
            "SELECT title FROM column_profiles WHERE chat_id = ? AND column_key = ?",
            (-4001, "user_custom"),
        )
        assert (await cursor.fetchone())["title"] == "Моя колонка"
        cursor = await connection.execute(
            "SELECT user_values_json FROM composition_player_state WHERE chat_id = ?",
            (-4001,),
        )
        assert (await cursor.fetchone())["user_values_json"] == '{"user_custom":"keep"}'
        cursor = await connection.execute(
            "SELECT season FROM cwl_row_state WHERE chat_id = ?",
            (-4001,),
        )
        assert (await cursor.fetchone())["season"] == "2026-07"
        cursor = await connection.execute(
            "SELECT COUNT(*) AS count FROM column_profiles WHERE chat_id = ? AND table_type = 'raids'",
            (-4001,),
        )
        assert (await cursor.fetchone())["count"] == 8


@pytest.mark.asyncio
async def test_migration_4_creates_raid_indexes_foreign_keys_and_player_uniqueness(
    migrated_connection: aiosqlite.Connection,
) -> None:
    """Проверяет полный schema contract migration 4 и повторное применение."""

    await apply_migrations(migrated_connection)
    cursor = await migrated_connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'index' AND name IN (?, ?)
        """,
        (
            "idx_raid_player_state_chat_season_clan",
            "idx_raid_sheet_archives_order",
        ),
    )
    assert {row["name"] for row in await cursor.fetchall()} == {
        "idx_raid_player_state_chat_season_clan",
        "idx_raid_sheet_archives_order",
    }

    for table_name in ("raid_player_state", "raid_sheet_archives"):
        cursor = await migrated_connection.execute(f"PRAGMA foreign_key_list({table_name})")
        foreign_keys = await cursor.fetchall()
        assert any(
            row["table"] == "telegram_chats" and row["from"] == "chat_id" and row["to"] == "chat_id"
            for row in foreign_keys
        )

    chat_id = -4101
    await _insert_chat(migrated_connection, chat_id=chat_id)
    player_values = (
        chat_id,
        "2026-07-24T07:00:00+00:00",
        "2026-07-24T07:00:00+00:00",
        "2026-07-27T07:00:00+00:00",
        "ended",
        "raid_row:2026-07-24T07:00:00+00:00|#CLAN|#PLAYER",
        "#CLAN",
        "#PLAYER",
        "{}",
        "{}",
        NOW,
    )
    await migrated_connection.execute(
        """
        INSERT INTO raid_player_state(
            chat_id, season_key, season_start_at, season_end_at, season_state,
            row_key, clan_tag, player_tag, technical_values_json,
            user_values_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        player_values,
    )
    with pytest.raises(aiosqlite.IntegrityError):
        await migrated_connection.execute(
            """
            INSERT INTO raid_player_state(
                chat_id, season_key, season_start_at, season_end_at, season_state,
                row_key, clan_tag, player_tag, technical_values_json,
                user_values_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            player_values,
        )

    orphan_values = (
        -999_999,
        *player_values[1:],
    )
    with pytest.raises(aiosqlite.IntegrityError):
        await migrated_connection.execute(
            """
            INSERT INTO raid_player_state(
                chat_id, season_key, season_start_at, season_end_at, season_state,
                row_key, clan_tag, player_tag, technical_values_json,
                user_values_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            orphan_values,
        )
    with pytest.raises(aiosqlite.IntegrityError):
        await migrated_connection.execute(
            """
            INSERT INTO raid_sheet_archives(
                chat_id, season_key, season_start_at, sheet_name, sheet_id, archived_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                -999_999,
                "2026-07-24T07:00:00+00:00",
                "2026-07-24T07:00:00+00:00",
                "Рейды 2026-07-24",
                901,
                NOW,
            ),
        )

    await apply_migrations(migrated_connection)
    cursor = await migrated_connection.execute(
        "SELECT COUNT(*) AS count FROM raid_player_state WHERE chat_id = ?",
        (chat_id,),
    )
    assert (await cursor.fetchone())["count"] == 1


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

    await migrated_connection.execute("DELETE FROM schema_migrations WHERE version = 2")
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
        (2,),
    )
    row = await cursor.fetchone()

    assert row is not None
    assert row["count"] == 1


@pytest.mark.asyncio
async def test_migration_3_renames_only_default_town_hall_titles(
    migrated_connection: aiosqlite.Connection,
) -> None:
    """Проверяет новый формат ТХ без перезаписи пользовательского названия."""

    chat_id = -3001
    await _insert_chat(migrated_connection, chat_id=chat_id)
    await _insert_column_profile(
        migrated_connection,
        chat_id=chat_id,
        table_type="composition_active",
        column_key="town_hall",
        title="Ратуша",
        value_type="integer",
    )
    await _insert_column_profile(
        migrated_connection,
        chat_id=chat_id,
        table_type="composition_exited",
        column_key="town_hall",
        title="Мой уровень",
        value_type="integer",
    )
    await _insert_column_profile(
        migrated_connection,
        chat_id=chat_id,
        table_type="cwl",
        column_key="stars",
        title="Звезды",
        value_type="integer",
    )
    await migrated_connection.execute("DELETE FROM schema_migrations WHERE version = 3")
    await migrated_connection.commit()

    await apply_migrations(migrated_connection)
    await apply_migrations(migrated_connection)

    cursor = await migrated_connection.execute(
        """
        SELECT table_type, column_key, title, value_type
        FROM column_profiles
        WHERE chat_id = ?
        ORDER BY table_type, column_key
        """,
        (chat_id,),
    )
    rows = await cursor.fetchall()
    profiles = {
        (row["table_type"], row["column_key"]): (row["title"], row["value_type"]) for row in rows
    }

    assert profiles[("composition_active", "town_hall")] == ("ТХ", "string")
    assert profiles[("composition_exited", "town_hall")] == ("Мой уровень", "string")
    assert profiles[("cwl", "stars")] == ("Звезды", "string")
