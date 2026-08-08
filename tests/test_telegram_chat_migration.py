"""Регрессии преобразования Telegram basic group в supergroup."""

from __future__ import annotations

from typing import Any

import aiosqlite
import pytest

from clash_sheet_sync_bot.bot import BotApp
from clash_sheet_sync_bot.repositories import (
    ChatMigrationConflictError,
    TelegramChatRepository,
)
from clash_sheet_sync_bot.setup.flow import AWAITING_CLAN_TAG_STATE_PREFIX, SetupFlow
from clash_sheet_sync_bot.setup.keyboards import CALLBACK_CLAN_ADD_PREFIX
from clash_sheet_sync_bot.storage import Database
from clash_sheet_sync_bot.telegram.access import AdminCheckResult, TelegramAccessService
from clash_sheet_sync_bot.telegram.client import TelegramChatMigratedError
from tests.fakes.factories import make_app_config
from tests.fakes.telegram import FakeTelegram, RecordingAccessService

OLD_CHAT_ID = -5367551907
NEW_CHAT_ID = -1004441868861
USER_ID = 1001
NOW = "2026-08-08T12:00:00+00:00"


async def _insert_chat(connection: aiosqlite.Connection, chat_id: int, title: str) -> None:
    await connection.execute(
        """
        INSERT INTO telegram_chats(
            chat_id, title, type, status, created_by_user_id, created_at, updated_at
        )
        VALUES (?, ?, 'group', 'ready', ?, ?, ?)
        """,
        (chat_id, title, USER_ID, NOW, NOW),
    )


async def _insert_complete_runtime_state(connection: aiosqlite.Connection) -> None:
    await _insert_chat(connection, OLD_CHAT_ID, "Old group")
    await connection.execute(
        "INSERT INTO chat_admin_links(chat_id, user_id, linked_at) VALUES (?, ?, ?)",
        (OLD_CHAT_ID, USER_ID, NOW),
    )
    await connection.execute(
        """
        INSERT INTO sheet_bindings(
            chat_id, google_sheet_id, spreadsheet_url, timezone, created_at, updated_at
        ) VALUES (?, 'sheet-id', 'https://docs.google.com/spreadsheets/d/sheet-id/edit',
                  'Europe/Kyiv', ?, ?)
        """,
        (OLD_CHAT_ID, NOW, NOW),
    )
    await connection.execute(
        """
        INSERT INTO tracked_clans(
            chat_id, clan_tag, clan_name, sort_order, created_at, updated_at
        ) VALUES (?, '#CLAN', 'Clan', 10, ?, ?)
        """,
        (OLD_CHAT_ID, NOW, NOW),
    )
    await connection.execute(
        """
        INSERT INTO column_profiles(
            chat_id, table_type, column_key, title, visible, sort_order, kind,
            value_type, created_at, updated_at
        ) VALUES (?, 'cwl', 'stars', 'Звезды', 1, 10, 'system', 'integer', ?, ?)
        """,
        (OLD_CHAT_ID, NOW, NOW),
    )
    await connection.execute(
        """
        INSERT INTO composition_player_state(
            chat_id, player_tag, status, updated_at
        ) VALUES (?, '#PLAYER', 'active', ?)
        """,
        (OLD_CHAT_ID, NOW),
    )
    await connection.execute(
        """
        INSERT INTO cwl_row_state(
            chat_id, season, row_key, clan_tag, marker, technical_values_json, updated_at
        ) VALUES (?, '2026-08', 'row', '#CLAN', 'active', '{}', ?)
        """,
        (OLD_CHAT_ID, NOW),
    )
    await connection.execute(
        """
        INSERT INTO sheet_blocks(
            chat_id, sheet_name, block_key, start_cell, rows_count, columns_count, updated_at
        ) VALUES (?, 'CWL', 'block', 'A1', 1, 1, ?)
        """,
        (OLD_CHAT_ID, NOW),
    )
    await connection.execute(
        """
        INSERT INTO sync_runs(chat_id, started_by_user_id, status, started_at)
        VALUES (?, ?, 'success', ?)
        """,
        (OLD_CHAT_ID, USER_ID, NOW),
    )
    await connection.execute(
        """
        INSERT INTO raid_player_state(
            chat_id, season_key, season_start_at, season_end_at, season_state, row_key,
            clan_tag, player_tag, technical_values_json, updated_at
        ) VALUES (?, 'season', ?, ?, 'ended', 'raid-row', '#CLAN', '#PLAYER', '{}', ?)
        """,
        (OLD_CHAT_ID, NOW, NOW, NOW),
    )
    await connection.execute(
        """
        INSERT INTO raid_sheet_archives(
            chat_id, season_key, season_start_at, sheet_name, sheet_id, archived_at
        ) VALUES (?, 'season', ?, 'Raid archive', 999, ?)
        """,
        (OLD_CHAT_ID, NOW, NOW),
    )
    await connection.execute(
        "INSERT INTO cwl_forecast_chat_state(chat_id, last_started_at) VALUES (?, ?)",
        (OLD_CHAT_ID, NOW),
    )
    await connection.execute(
        """
        INSERT INTO setup_tokens(
            token, created_by_user_id, expires_at, used_chat_id, used_at, created_at
        ) VALUES ('setup', ?, ?, ?, ?, ?)
        """,
        (USER_ID, NOW, OLD_CHAT_ID, NOW, NOW),
    )
    await connection.execute(
        """
        INSERT INTO transfer_tokens(
            token, source_chat_id, created_by_user_id, expires_at, created_at
        ) VALUES ('transfer', ?, ?, ?, ?)
        """,
        (OLD_CHAT_ID, USER_ID, NOW, NOW),
    )
    await connection.execute(
        """
        UPDATE bot_settings
        SET support_chat_id = ?, support_chat_title = 'Old support title'
        WHERE singleton_id = 1
        """,
        (OLD_CHAT_ID,),
    )
    await connection.execute(
        """
        INSERT INTO support_setup_tokens(
            token, created_by_user_id, expires_at, used_chat_id, used_at, created_at
        ) VALUES ('support', ?, ?, ?, ?, ?)
        """,
        (USER_ID, NOW, OLD_CHAT_ID, NOW, NOW),
    )
    await connection.execute(
        """
        INSERT INTO cwl_forecast_schedules(
            season, group_fingerprint, clan_tag, created_by_user_id, source_chat_id,
            created_at, updated_at
        ) VALUES ('2026-08', 'group', '#CLAN', ?, ?, ?, ?)
        """,
        (USER_ID, OLD_CHAT_ID, NOW, NOW),
    )
    await connection.execute(
        """
        INSERT INTO cwl_forecast_schedule_sessions(
            id, season, group_fingerprint, clan_tag, source_chat_id, created_by_user_id,
            draft_json, current_step, expires_at, created_at, updated_at
        ) VALUES ('session', '2026-09', 'group-2', '#CLAN', ?, ?, '{}', 0, ?, ?, ?)
        """,
        (OLD_CHAT_ID, USER_ID, NOW, NOW, NOW),
    )
    await connection.commit()


@pytest.mark.asyncio
async def test_repository_migrates_every_chat_reference_atomically(
    migrated_connection: aiosqlite.Connection,
) -> None:
    await _insert_complete_runtime_state(migrated_connection)

    migrated = await TelegramChatRepository(migrated_connection).migrate_chat_id(
        source_chat_id=OLD_CHAT_ID,
        target_chat_id=NEW_CHAT_ID,
        title="New supergroup",
        now=NOW,
    )

    assert migrated is True
    chat = await (
        await migrated_connection.execute(
            "SELECT chat_id, title, type FROM telegram_chats WHERE chat_id = ?",
            (NEW_CHAT_ID,),
        )
    ).fetchone()
    assert chat is not None and tuple(chat) == (NEW_CHAT_ID, "New supergroup", "supergroup")

    foreign_key_tables = (
        "chat_admin_links",
        "sheet_bindings",
        "tracked_clans",
        "column_profiles",
        "composition_player_state",
        "cwl_row_state",
        "sheet_blocks",
        "sync_runs",
        "raid_player_state",
        "raid_sheet_archives",
        "cwl_forecast_chat_state",
    )
    reference_columns = (
        ("setup_tokens", "used_chat_id"),
        ("transfer_tokens", "source_chat_id"),
        ("bot_settings", "support_chat_id"),
        ("support_setup_tokens", "used_chat_id"),
        ("cwl_forecast_schedules", "source_chat_id"),
        ("cwl_forecast_schedule_sessions", "source_chat_id"),
    )
    for table_name in foreign_key_tables:
        row = await (
            await migrated_connection.execute(
                f"SELECT COUNT(*) FROM {table_name} WHERE chat_id = ?", (NEW_CHAT_ID,)
            )
        ).fetchone()
        assert row is not None and row[0] == 1
    for table_name, column_name in reference_columns:
        row = await (
            await migrated_connection.execute(
                f"SELECT COUNT(*) FROM {table_name} WHERE {column_name} = ?", (NEW_CHAT_ID,)
            )
        ).fetchone()
        assert row is not None and row[0] == 1
    support = await (
        await migrated_connection.execute(
            "SELECT support_chat_id, support_chat_title FROM bot_settings WHERE singleton_id = 1"
        )
    ).fetchone()
    assert support is not None and tuple(support) == (NEW_CHAT_ID, "New supergroup")

    assert await (await migrated_connection.execute("PRAGMA foreign_key_check")).fetchall() == []
    assert (
        await TelegramChatRepository(migrated_connection).migrate_chat_id(
            source_chat_id=OLD_CHAT_ID,
            target_chat_id=NEW_CHAT_ID,
            now=NOW,
        )
        is False
    )


@pytest.mark.asyncio
async def test_repository_rejects_existing_target_without_partial_changes(
    migrated_connection: aiosqlite.Connection,
) -> None:
    await _insert_chat(migrated_connection, OLD_CHAT_ID, "Old")
    await _insert_chat(migrated_connection, NEW_CHAT_ID, "New")
    await migrated_connection.commit()

    with pytest.raises(ChatMigrationConflictError):
        await TelegramChatRepository(migrated_connection).migrate_chat_id(
            source_chat_id=OLD_CHAT_ID,
            target_chat_id=NEW_CHAT_ID,
            now=NOW,
        )

    rows = await (
        await migrated_connection.execute(
            "SELECT chat_id, title FROM telegram_chats ORDER BY chat_id"
        )
    ).fetchall()
    assert {tuple(row) for row in rows} == {
        (OLD_CHAT_ID, "Old"),
        (NEW_CHAT_ID, "New"),
    }


class _MigratedTelegram:
    async def get_chat_member(self, *, chat_id: int, user_id: int) -> Any:
        del chat_id, user_id
        raise TelegramChatMigratedError("migrated", new_chat_id=NEW_CHAT_ID)


@pytest.mark.asyncio
async def test_access_service_preserves_migration_target(
    migrated_connection: aiosqlite.Connection,
) -> None:
    result = await TelegramAccessService(
        telegram=_MigratedTelegram(),  # type: ignore[arg-type]
        connection=migrated_connection,
        admin_cache_ttl_seconds=300,
    ).is_admin(chat_id=OLD_CHAT_ID, user_id=USER_ID, force_refresh=True)

    assert result == AdminCheckResult(
        is_admin=False,
        from_cache=False,
        migrated_to_chat_id=NEW_CHAT_ID,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat", "migration_field", "migration_value"),
    (
        (
            {"id": OLD_CHAT_ID, "type": "group", "title": "Renamed"},
            "migrate_to_chat_id",
            NEW_CHAT_ID,
        ),
        (
            {"id": NEW_CHAT_ID, "type": "supergroup", "title": "Renamed"},
            "migrate_from_chat_id",
            OLD_CHAT_ID,
        ),
    ),
)
async def test_bot_handles_migration_service_message_without_sender(
    migrated_connection: aiosqlite.Connection,
    chat: dict[str, Any],
    migration_field: str,
    migration_value: int,
) -> None:
    await _insert_chat(migrated_connection, OLD_CHAT_ID, "Old")
    await migrated_connection.commit()
    app = BotApp(
        config=make_app_config(),
        telegram=FakeTelegram(),  # type: ignore[arg-type]
        database=Database("unused-test.db"),
        bot_username="test_bot",
    )

    await app._handle_update_with_connection(
        update={
            "message": {
                "chat": chat,
                migration_field: migration_value,
            }
        },
        connection=migrated_connection,
    )

    row = await (
        await migrated_connection.execute(
            "SELECT title, type FROM telegram_chats WHERE chat_id = ?", (NEW_CHAT_ID,)
        )
    ).fetchone()
    assert row is not None and tuple(row) == ("Renamed", "supergroup")


@pytest.mark.asyncio
async def test_old_private_callback_migrates_chat_and_continues_action(
    migrated_connection: aiosqlite.Connection,
) -> None:
    await _insert_chat(migrated_connection, OLD_CHAT_ID, "Old")
    await migrated_connection.execute(
        "INSERT INTO chat_admin_links(chat_id, user_id, linked_at) VALUES (?, ?, ?)",
        (OLD_CHAT_ID, USER_ID, NOW),
    )
    await migrated_connection.commit()
    telegram = FakeTelegram()
    access = RecordingAccessService(
        results=[
            AdminCheckResult(False, migrated_to_chat_id=NEW_CHAT_ID),
            AdminCheckResult(True),
        ]
    )
    flow = SetupFlow(
        config=make_app_config(),
        telegram=telegram,  # type: ignore[arg-type]
        connection=migrated_connection,
        access=access,  # type: ignore[arg-type]
        bot_username="test_bot",
    )

    await flow.handle_callback(
        callback_data=f"{CALLBACK_CLAN_ADD_PREFIX}{OLD_CHAT_ID}",
        callback_query_id="callback",
        chat_id=USER_ID,
        message_id=10,
        user_id=USER_ID,
    )

    assert [call["chat_id"] for call in access.calls] == [OLD_CHAT_ID, NEW_CHAT_ID]
    row = await (
        await migrated_connection.execute(
            "SELECT setup_state FROM telegram_chats WHERE chat_id = ?", (NEW_CHAT_ID,)
        )
    ).fetchone()
    assert row is not None and row["setup_state"] == f"{AWAITING_CLAN_TAG_STATE_PREFIX}{USER_ID}"
    assert telegram.answered_callbacks[-1]["text"] == "Принято."
