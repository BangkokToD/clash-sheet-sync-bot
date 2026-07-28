"""Smoke-тесты SQLite repository-слоя."""

from __future__ import annotations

import aiosqlite
import pytest

from clash_sheet_sync_bot.models import SheetBlock
from clash_sheet_sync_bot.repositories import (
    CwlRowStateRepository,
    RaidPlayerState,
    RaidPlayerStateRepository,
    RaidSheetArchive,
    RaidSheetArchiveRepository,
    RuntimeConfigRepository,
    SheetBlockRepository,
    SyncRunRepository,
    TelegramChatRepository,
)

NOW = "2026-07-09T12:00:00+00:00"


async def _insert_chat(
    connection: aiosqlite.Connection,
    *,
    chat_id: int,
    title: str = "Test group",
    chat_type: str = "supergroup",
    status: str = "ready",
    setup_state: str | None = None,
    created_by_user_id: int = 1001,
) -> None:
    await connection.execute(
        """
        INSERT INTO telegram_chats(
            chat_id,
            title,
            type,
            status,
            setup_state,
            created_by_user_id,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chat_id,
            title,
            chat_type,
            status,
            setup_state,
            created_by_user_id,
            NOW,
            NOW,
        ),
    )


async def _insert_admin_link(
    connection: aiosqlite.Connection,
    *,
    chat_id: int,
    user_id: int,
    is_active: bool = True,
) -> None:
    await connection.execute(
        """
        INSERT INTO chat_admin_links(
            chat_id,
            user_id,
            is_active,
            linked_at,
            last_admin_check_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (chat_id, user_id, int(is_active), NOW, NOW),
    )


async def _insert_sheet_binding(
    connection: aiosqlite.Connection,
    *,
    chat_id: int,
    google_sheet_id: str = "sheet-id",
    spreadsheet_url: str = "https://docs.google.com/spreadsheets/d/sheet-id/edit",
) -> None:
    await connection.execute(
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
        """,
        (
            chat_id,
            google_sheet_id,
            spreadsheet_url,
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


async def _insert_tracked_clan(
    connection: aiosqlite.Connection,
    *,
    chat_id: int,
    clan_tag: str = "#ABC123",
    clan_name: str = "Test Clan",
    sort_order: int = 10,
    is_active: bool = True,
) -> None:
    await connection.execute(
        """
        INSERT INTO tracked_clans(
            chat_id,
            clan_tag,
            clan_name,
            sort_order,
            is_active,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (chat_id, clan_tag, clan_name, sort_order, int(is_active), NOW, NOW),
    )


async def _insert_column_profile(
    connection: aiosqlite.Connection,
    *,
    chat_id: int,
    table_type: str = "composition_active",
    column_key: str = "bot_key",
    title: str = "__bot_key",
    visible: bool = False,
    kind: str = "service",
    value_type: str = "string",
    sort_order: int = 0,
    is_active: bool = True,
) -> None:
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


async def _insert_sheet_block(
    connection: aiosqlite.Connection,
    *,
    chat_id: int,
    sheet_name: str,
    block_key: str,
    start_cell: str,
) -> None:
    await connection.execute(
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
        """,
        (chat_id, sheet_name, 111, block_key, start_cell, 3, 4, NOW),
    )


@pytest.mark.asyncio
async def test_clear_setup_states_for_user_only_clears_owned_states(
    migrated_connection: aiosqlite.Connection,
) -> None:
    """Проверяет очистку setup_state только для указанного пользователя."""

    user_id = 1001
    other_user_id = 2002

    await _insert_chat(
        migrated_connection,
        chat_id=-101,
        setup_state=f"awaiting_sheet_link:{user_id}",
    )
    await _insert_admin_link(migrated_connection, chat_id=-101, user_id=user_id)

    await _insert_chat(
        migrated_connection,
        chat_id=-102,
        setup_state=f"awaiting_column_rename:{user_id}:cwl:stars",
    )
    await _insert_admin_link(migrated_connection, chat_id=-102, user_id=user_id)

    await _insert_chat(
        migrated_connection,
        chat_id=-103,
        setup_state=f"awaiting_sheet_link:{other_user_id}",
    )
    await _insert_admin_link(migrated_connection, chat_id=-103, user_id=other_user_id)

    await _insert_chat(
        migrated_connection,
        chat_id=-104,
        setup_state=f"awaiting_clan_tag:{user_id}",
    )
    await _insert_admin_link(migrated_connection, chat_id=-104, user_id=user_id, is_active=False)

    repository = TelegramChatRepository(migrated_connection)
    cleared_count = await repository.clear_setup_states_for_user(user_id=user_id, now=NOW)

    assert cleared_count == 2

    cursor = await migrated_connection.execute(
        """
        SELECT chat_id, setup_state
        FROM telegram_chats
        WHERE chat_id IN (-101, -102, -103, -104)
        ORDER BY chat_id
        """
    )
    rows = await cursor.fetchall()
    states_by_chat_id = {row["chat_id"]: row["setup_state"] for row in rows}

    assert states_by_chat_id[-101] is None
    assert states_by_chat_id[-102] is None
    assert states_by_chat_id[-103] == f"awaiting_sheet_link:{other_user_id}"
    assert states_by_chat_id[-104] == f"awaiting_clan_tag:{user_id}"


@pytest.mark.asyncio
async def test_replace_blocks_by_prefixes_replaces_only_matching_blocks(
    migrated_connection: aiosqlite.Connection,
) -> None:
    """Проверяет атомарную замену metadata managed-блоков по prefix."""

    chat_id = -201
    sheet_name = "Состав"
    await _insert_chat(migrated_connection, chat_id=chat_id)
    await _insert_sheet_block(
        migrated_connection,
        chat_id=chat_id,
        sheet_name=sheet_name,
        block_key="composition_active:#OLD",
        start_cell="A1",
    )
    await _insert_sheet_block(
        migrated_connection,
        chat_id=chat_id,
        sheet_name=sheet_name,
        block_key="composition_exited",
        start_cell="G1",
    )
    await _insert_sheet_block(
        migrated_connection,
        chat_id=chat_id,
        sheet_name=sheet_name,
        block_key="cwl:#KEEP",
        start_cell="A20",
    )

    repository = SheetBlockRepository(migrated_connection)
    await repository.replace_blocks_by_prefixes(
        chat_id=chat_id,
        sheet_name=sheet_name,
        block_key_prefixes=("composition_active:", "composition_exited"),
        blocks=(
            SheetBlock(
                chat_id=chat_id,
                sheet_name=sheet_name,
                sheet_id=111,
                block_key="composition_active:#NEW",
                start_cell="A1",
                rows_count=5,
                columns_count=6,
            ),
            SheetBlock(
                chat_id=chat_id,
                sheet_name=sheet_name,
                sheet_id=111,
                block_key="composition_exited",
                start_cell="I1",
                rows_count=4,
                columns_count=7,
            ),
        ),
        updated_at=NOW,
    )

    blocks = await repository.list_blocks(chat_id, sheet_name)
    block_keys = {block.block_key for block in blocks}

    assert "composition_active:#OLD" not in block_keys
    assert "composition_active:#NEW" in block_keys
    assert "composition_exited" in block_keys
    assert "cwl:#KEEP" in block_keys


@pytest.mark.asyncio
async def test_cwl_repository_get_latest_season_is_scoped_to_chat_and_active_clans(
    migrated_connection: aiosqlite.Connection,
) -> None:
    """Проверяет выбор истории только этой группы и запрошенных кланов."""

    await _insert_chat(migrated_connection, chat_id=-211)
    await _insert_chat(migrated_connection, chat_id=-212)
    repository = CwlRowStateRepository(migrated_connection)

    async def save(*, chat_id: int, season: str, clan_tag: str) -> None:
        row_key = f"{season}|{clan_tag}|1|#PLAYER|NO_ATTACK"
        await repository.upsert_row_state(
            chat_id=chat_id,
            season=season,
            row_key=row_key,
            clan_tag=clan_tag,
            round_number=1,
            attacker_tag="#PLAYER",
            marker="NO_ATTACK",
            technical_values={"round": 1},
            user_values={},
            row_hash=f"hash-{chat_id}-{season}-{clan_tag}",
        )

    await save(chat_id=-211, season="2026-05", clan_tag="#ACTIVE")
    await save(chat_id=-211, season="2026-07", clan_tag="#INACTIVE")
    await save(chat_id=-212, season="2026-09", clan_tag="#ACTIVE")

    assert (
        await repository.get_latest_season(
            chat_id=-211,
            clan_tags=("#ACTIVE",),
        )
        == "2026-05"
    )
    assert (
        await repository.get_latest_season(
            chat_id=-211,
            clan_tags=("#MISSING",),
        )
        is None
    )
    assert await repository.get_latest_season(chat_id=-211, clan_tags=()) is None


@pytest.mark.asyncio
async def test_finish_sync_run_writes_error_stage_and_message(
    migrated_connection: aiosqlite.Connection,
) -> None:
    """Проверяет сохранение error_stage/error_message в sync_runs."""

    chat_id = -301
    await _insert_chat(migrated_connection, chat_id=chat_id)

    repository = SyncRunRepository(migrated_connection)
    sync_run_id = await repository.create_sync_run(
        chat_id=chat_id,
        started_by_user_id=1001,
        status="skipped",
        started_at=NOW,
    )
    await repository.finish_sync_run(
        sync_run_id=sync_run_id,
        status="error",
        finished_at=NOW,
        error_stage="composition_written",
        error_message="Таблица могла быть частично обновлена.",
    )

    cursor = await migrated_connection.execute(
        """
        SELECT status, finished_at, error_stage, error_message
        FROM sync_runs
        WHERE id = ?
        """,
        (sync_run_id,),
    )
    row = await cursor.fetchone()

    assert row is not None
    assert row["status"] == "error"
    assert row["finished_at"] == NOW
    assert row["error_stage"] == "composition_written"
    assert row["error_message"] == "Таблица могла быть частично обновлена."


@pytest.mark.asyncio
async def test_runtime_config_repository_builds_ready_chat_config(
    migrated_connection: aiosqlite.Connection,
) -> None:
    """Проверяет сборку RuntimeChatConfig для готовой группы."""

    chat_id = -401
    await _insert_chat(migrated_connection, chat_id=chat_id, status="ready")
    await _insert_sheet_binding(migrated_connection, chat_id=chat_id)
    await _insert_tracked_clan(
        migrated_connection,
        chat_id=chat_id,
        clan_tag="#AAA111",
        clan_name="Alpha",
        sort_order=10,
    )
    await _insert_tracked_clan(
        migrated_connection,
        chat_id=chat_id,
        clan_tag="#BBB222",
        clan_name="Beta",
        sort_order=20,
    )
    await _insert_column_profile(
        migrated_connection,
        chat_id=chat_id,
        table_type="composition_active",
        column_key="bot_key",
        title="__bot_key",
        visible=False,
        kind="service",
        value_type="string",
        sort_order=0,
    )
    await _insert_column_profile(
        migrated_connection,
        chat_id=chat_id,
        table_type="composition_exited",
        column_key="bot_key",
        title="__bot_key",
        visible=False,
        kind="service",
        value_type="string",
        sort_order=0,
    )
    await _insert_column_profile(
        migrated_connection,
        chat_id=chat_id,
        table_type="cwl",
        column_key="round",
        title="Раунд",
        visible=True,
        kind="system",
        value_type="integer",
        sort_order=10,
    )

    repository = RuntimeConfigRepository(migrated_connection)
    runtime_config = await repository.get_runtime_chat_config(chat_id)

    assert runtime_config is not None
    assert runtime_config.chat_id == chat_id
    assert runtime_config.status == "ready"
    assert runtime_config.sheet_binding.google_sheet_id == "sheet-id"
    assert runtime_config.sheet_binding.composition_sheet_name == "Состав"
    assert runtime_config.sheet_binding.active_cwl_sheet_name == "CWL"
    assert runtime_config.timezone == "Europe/Kyiv"
    assert [clan.clan_tag for clan in runtime_config.active_clans] == ["#AAA111", "#BBB222"]
    assert {profile.table_type for profile in runtime_config.column_profiles} == {
        "composition_active",
        "composition_exited",
        "cwl",
    }
    assert runtime_config.sheet_binding.active_raid_sheet_name == "Рейды"
    assert runtime_config.sheet_binding.active_raid_sheet_id is None
    assert runtime_config.sheet_binding.active_raid_season is None


@pytest.mark.asyncio
async def test_raid_repositories_round_trip_and_archive_order(
    migrated_connection: aiosqlite.Connection,
) -> None:
    """Проверяет raid snapshot и канонический порядок registry."""

    await _insert_chat(migrated_connection, chat_id=-1001)
    rows = RaidPlayerStateRepository(migrated_connection)
    archives = RaidSheetArchiveRepository(migrated_connection)
    state = RaidPlayerState(
        chat_id=-1001,
        season_key="2026-07-24T07:00:00+00:00",
        season_start_at="2026-07-24T07:00:00+00:00",
        season_end_at="2026-07-27T07:00:00+00:00",
        season_state="ended",
        row_key="raid_row:season|#CLAN|#PLAYER",
        clan_tag="#CLAN",
        player_tag="#PLAYER",
        technical_values={"weighted_damage_units": 600, "coefficient": 1.0},
        user_values={"note": "manual"},
        row_hash="hash",
        updated_at=NOW,
    )
    await rows.upsert(state)
    assert await rows.list_for_season(chat_id=-1001, season_key=state.season_key) == (state,)

    older = RaidSheetArchive(-1001, "old", "2026-07-17", "Рейды 2026-07-17", 201, NOW)
    newer = RaidSheetArchive(-1001, "new", "2026-07-24", "Рейды 2026-07-24", 202, NOW)
    await archives.upsert(newer)
    await archives.upsert(older)
    assert await archives.list_ordered(-1001) == (older, newer)
    assert await archives.get_by_sheet_id(chat_id=-1001, sheet_id=202) == newer
