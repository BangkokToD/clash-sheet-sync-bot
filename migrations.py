"""SQLite-миграции runtime-хранилища."""

from __future__ import annotations

import aiosqlite
from typing import Final

SCHEMA_VERSION: Final = 2

SCHEMA_SQL: Final = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS telegram_chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL UNIQUE,
    title TEXT NOT NULL,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    setup_state TEXT,
    created_by_user_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_sync_started_at TEXT,
    last_sync_finished_at TEXT,
    last_sync_status TEXT,
    last_sync_error TEXT
);

CREATE TABLE IF NOT EXISTS chat_admin_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    linked_at TEXT NOT NULL,
    last_admin_check_at TEXT,
    UNIQUE(chat_id, user_id),
    FOREIGN KEY(chat_id) REFERENCES telegram_chats(chat_id)
);

CREATE TABLE IF NOT EXISTS setup_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT NOT NULL UNIQUE,
    created_by_user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    used_chat_id INTEGER,
    used_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sheet_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL UNIQUE,
    google_sheet_id TEXT NOT NULL,
    spreadsheet_url TEXT NOT NULL,
    composition_sheet_name TEXT NOT NULL DEFAULT 'Состав',
    composition_sheet_id INTEGER,
    active_cwl_sheet_name TEXT NOT NULL DEFAULT 'CWL',
    active_cwl_sheet_id INTEGER,
    active_cwl_season TEXT,
    bot_state_sheet_name TEXT NOT NULL DEFAULT '_bot_state',
    bot_state_sheet_id INTEGER,
    timezone TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(chat_id) REFERENCES telegram_chats(chat_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sheet_bindings_active_sheet
ON sheet_bindings(google_sheet_id)
WHERE is_active = 1;

CREATE TABLE IF NOT EXISTS tracked_clans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    clan_tag TEXT NOT NULL,
    clan_name TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(chat_id, clan_tag),
    FOREIGN KEY(chat_id) REFERENCES telegram_chats(chat_id)
);

CREATE INDEX IF NOT EXISTS idx_tracked_clans_chat_active_order
ON tracked_clans(chat_id, is_active, sort_order);

CREATE TABLE IF NOT EXISTS column_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    table_type TEXT NOT NULL,
    column_key TEXT NOT NULL,
    title TEXT NOT NULL,
    visible INTEGER NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL,
    kind TEXT NOT NULL,
    value_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(chat_id, table_type, column_key),
    FOREIGN KEY(chat_id) REFERENCES telegram_chats(chat_id)
);

CREATE INDEX IF NOT EXISTS idx_column_profiles_chat_table_order
ON column_profiles(chat_id, table_type, is_active, sort_order);

CREATE TABLE IF NOT EXISTS composition_player_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    player_tag TEXT NOT NULL,
    clan_tag TEXT,
    status TEXT NOT NULL,
    town_hall INTEGER,
    nickname TEXT,
    exited_at TEXT,
    user_values_json TEXT NOT NULL DEFAULT '{}',
    last_seen_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(chat_id, player_tag),
    FOREIGN KEY(chat_id) REFERENCES telegram_chats(chat_id)
);

CREATE INDEX IF NOT EXISTS idx_composition_player_state_chat_status
ON composition_player_state(chat_id, status);

CREATE TABLE IF NOT EXISTS cwl_row_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    season TEXT NOT NULL,
    row_key TEXT NOT NULL,
    clan_tag TEXT NOT NULL,
    round_number INTEGER,
    attacker_tag TEXT,
    marker TEXT NOT NULL,
    technical_values_json TEXT NOT NULL,
    user_values_json TEXT NOT NULL DEFAULT '{}',
    row_hash TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(chat_id, season, row_key),
    FOREIGN KEY(chat_id) REFERENCES telegram_chats(chat_id)
);

CREATE INDEX IF NOT EXISTS idx_cwl_row_state_chat_season_clan
ON cwl_row_state(chat_id, season, clan_tag);

CREATE TABLE IF NOT EXISTS sheet_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    sheet_name TEXT NOT NULL,
    sheet_id INTEGER,
    block_key TEXT NOT NULL,
    start_cell TEXT NOT NULL,
    rows_count INTEGER NOT NULL,
    columns_count INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(chat_id, sheet_name, block_key),
    FOREIGN KEY(chat_id) REFERENCES telegram_chats(chat_id)
);

CREATE INDEX IF NOT EXISTS idx_sheet_blocks_chat_sheet
ON sheet_blocks(chat_id, sheet_name);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    started_by_user_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_stage TEXT,
    error_clan_tag TEXT,
    error_war_tag TEXT,
    error_message TEXT,
    report_json TEXT,
    FOREIGN KEY(chat_id) REFERENCES telegram_chats(chat_id)
);

CREATE INDEX IF NOT EXISTS idx_sync_runs_chat_started_at
ON sync_runs(chat_id, started_at);

CREATE TABLE IF NOT EXISTS transfer_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT NOT NULL UNIQUE,
    source_chat_id INTEGER NOT NULL,
    created_by_user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL
);
"""

MIGRATION_SQL_BY_VERSION: Final[dict[int, str]] = {
    2: """
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
    SELECT
        chat_id,
        'composition_active',
        column_key,
        title,
        visible,
        is_active,
        sort_order,
        kind,
        value_type,
        created_at,
        updated_at
    FROM column_profiles
    WHERE table_type = 'composition'
      AND column_key != 'exited_at'
    ON CONFLICT(chat_id, table_type, column_key) DO NOTHING;

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
    SELECT
        chat_id,
        'composition_exited',
        column_key,
        title,
        visible,
        is_active,
        sort_order,
        kind,
        value_type,
        created_at,
        updated_at
    FROM column_profiles
    WHERE table_type = 'composition'
    ON CONFLICT(chat_id, table_type, column_key) DO NOTHING;

    UPDATE column_profiles
    SET visible = 0,
        is_active = 0,
        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE table_type = 'composition_active'
      AND column_key = 'exited_at';
    """,
}


async def apply_migrations(connection: aiosqlite.Connection) -> None:
    """Создаёт или обновляет схему SQLite до актуальной версии.

    Args:
        connection: Открытое SQLite-подключение.
    """

    await connection.executescript(SCHEMA_SQL)
    await _ensure_baseline_version(connection)
    applied_versions = await _applied_versions(connection)
    for version in sorted(MIGRATION_SQL_BY_VERSION):
        if version in applied_versions:
            continue
        await connection.executescript(MIGRATION_SQL_BY_VERSION[version])
        await connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
            (version,),
        )
    await connection.commit()


async def _ensure_baseline_version(connection: aiosqlite.Connection) -> None:
    """Фиксирует baseline-схему как migration version 1."""

    await connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
        (1,),
    )


async def _applied_versions(connection: aiosqlite.Connection) -> set[int]:
    """Читает уже применённые версии миграций."""

    cursor = await connection.execute("SELECT version FROM schema_migrations")
    rows = await cursor.fetchall()
    return {int(row[0]) for row in rows}
