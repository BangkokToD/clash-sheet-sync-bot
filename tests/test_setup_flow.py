"""SQLite-backed тесты setup-flow access-контрактов."""

from __future__ import annotations

import aiosqlite
import pytest
from fakes import FakeTelegram, RecordingAccessService, make_app_config, make_sheet_block

from clash_sheet_sync_bot.repositories import (
    RaidSheetArchive,
    RaidSheetArchiveRepository,
    RuntimeConfigRepository,
    SheetBindingRepository,
    SheetBlockRepository,
)
from clash_sheet_sync_bot.setup.flow import (
    AWAITING_CLAN_TAG_STATE_PREFIX,
    AWAITING_COLUMN_RENAME_STATE_PREFIX,
    AWAITING_USER_COLUMN_TITLE_STATE_PREFIX,
    CALLBACK_CLAN_ADD_PREFIX,
    SetupFlow,
    _column_rename_state,
    _edit_or_send_message,
    _parse_column_callback,
    _parse_column_section_callback,
    _rename_state_payload,
    _table_type_from_state,
    _user_column_title_state,
)
from clash_sheet_sync_bot.setup.keyboards import (
    CALLBACK_CHECK_SHEET_PREFIX,
    CALLBACK_COLUMN_ADD_PREFIX,
    CALLBACK_COLUMN_DELETE_PREFIX,
    CALLBACK_COLUMN_MOVE_UP_PREFIX,
    CALLBACK_COLUMN_RENAME_PREFIX,
    CALLBACK_COLUMN_RESTORE_PREFIX,
    CALLBACK_COLUMN_TOGGLE_PREFIX,
    CALLBACK_FIX_SHEET_PREFIX,
)
from clash_sheet_sync_bot.sheets.admin import RaidArchiveCleanup, SheetSetupResult
from clash_sheet_sync_bot.sheets.client import GoogleSheetsWriteError

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


def _setup_flow(
    connection: aiosqlite.Connection,
    *,
    telegram: FakeTelegram | None = None,
    access: RecordingAccessService | None = None,
) -> SetupFlow:
    """Создаёт SetupFlow с fake Telegram/access."""

    return SetupFlow(
        config=make_app_config(),
        telegram=telegram or FakeTelegram(),
        connection=connection,
        access=access or RecordingAccessService(),
        bot_username="test_bot",
    )


def test_raid_column_callbacks_and_setup_states_round_trip_strictly() -> None:
    """Проверяет строгие callback/state parsers для профиля raids."""

    user_id = 1001
    group_chat_id = -1001
    assert _parse_column_section_callback(
        f"{CALLBACK_COLUMN_ADD_PREFIX}{group_chat_id}:r",
        CALLBACK_COLUMN_ADD_PREFIX,
    ) == (group_chat_id, "raids")
    assert _parse_column_callback(
        f"{CALLBACK_COLUMN_RENAME_PREFIX}{group_chat_id}:r:attacks",
        CALLBACK_COLUMN_RENAME_PREFIX,
    ) == (group_chat_id, "raids", "attacks")

    add_state = _user_column_title_state(user_id, "raids")
    rename_state = _column_rename_state(user_id, "raids", "attacks")
    assert (
        _table_type_from_state(
            add_state,
            prefix=AWAITING_USER_COLUMN_TITLE_STATE_PREFIX,
            user_id=user_id,
        )
        == "raids"
    )
    assert _rename_state_payload(rename_state, user_id) == ("raids", "attacks")
    assert (
        _parse_column_section_callback(
            f"{CALLBACK_COLUMN_ADD_PREFIX}{group_chat_id}:raid",
            CALLBACK_COLUMN_ADD_PREFIX,
        )
        is None
    )


@pytest.mark.asyncio
async def test_new_binding_persists_raid_sheet_and_default_profiles(
    migrated_connection: aiosqlite.Connection,
) -> None:
    """Проверяет raid binding и default profiles новой установки."""

    user_id = 1001
    group_chat_id = -1101
    await _insert_chat(
        migrated_connection,
        chat_id=group_chat_id,
        status="waiting_for_access",
        setup_state=f"awaiting_sheet_access:{user_id}:sheet-id",
    )
    await _insert_admin_link(migrated_connection, chat_id=group_chat_id, user_id=user_id)
    await migrated_connection.commit()
    flow = _setup_flow(migrated_connection)

    async def initialize_sheet(_group_chat_id: int, _spreadsheet_id: str) -> SheetSetupResult:
        return SheetSetupResult(
            spreadsheet_id="sheet-id",
            spreadsheet_url="https://docs.google.com/spreadsheets/d/sheet-id/edit",
            composition_sheet_name="Состав",
            composition_sheet_id=111,
            active_cwl_sheet_name="CWL",
            active_cwl_sheet_id=222,
            active_cwl_season=None,
            active_raid_sheet_name="Рейды",
            active_raid_sheet_id=444,
            active_raid_season=None,
            bot_state_sheet_name="_bot_state",
            bot_state_sheet_id=333,
        )

    flow._initialize_sheet = initialize_sheet  # type: ignore[method-assign]
    await flow.handle_callback(
        callback_data=f"{CALLBACK_CHECK_SHEET_PREFIX}{group_chat_id}",
        callback_query_id="bind-raids",
        chat_id=user_id,
        message_id=10,
        user_id=user_id,
    )

    row = await (
        await migrated_connection.execute(
            """
            SELECT active_raid_sheet_name, active_raid_sheet_id, active_raid_season
            FROM sheet_bindings WHERE chat_id = ?
            """,
            (group_chat_id,),
        )
    ).fetchone()
    assert row is not None
    assert tuple(row) == ("Рейды", 444, None)
    profile_count = await (
        await migrated_connection.execute(
            "SELECT COUNT(*) AS count FROM column_profiles WHERE chat_id = ? AND table_type = 'raids'",
            (group_chat_id,),
        )
    ).fetchone()
    assert profile_count is not None
    assert profile_count["count"] == 8


@pytest.mark.asyncio
async def test_autofix_updates_all_binding_fields_and_cleans_selected_raid_metadata(
    migrated_connection: aiosqlite.Connection,
) -> None:
    """Проверяет единый SQLite update после подтверждённого Sheets cleanup."""

    user_id = 1001
    group_chat_id = -1103
    await _insert_chat(migrated_connection, chat_id=group_chat_id)
    await _insert_admin_link(migrated_connection, chat_id=group_chat_id, user_id=user_id)
    await SheetBindingRepository(migrated_connection).upsert_active_binding(
        chat_id=group_chat_id,
        google_sheet_id="sheet-id",
        spreadsheet_url="https://docs.google.com/spreadsheets/d/sheet-id/edit",
        composition_sheet_name="Состав",
        composition_sheet_id=111,
        active_cwl_sheet_name="CWL",
        active_cwl_sheet_id=222,
        active_cwl_season="2026-07",
        active_raid_sheet_name="Рейды",
        active_raid_sheet_id=444,
        active_raid_season="2026-07-17T07:00:00+00:00",
        bot_state_sheet_name="_bot_state",
        bot_state_sheet_id=333,
        timezone="Europe/Kyiv",
        now=NOW,
    )
    archive = RaidSheetArchive(
        chat_id=group_chat_id,
        season_key="2026-06-01T07:00:00+00:00",
        season_start_at="2026-06-01T07:00:00+00:00",
        sheet_name="Рейды 2026-06-01",
        sheet_id=500,
        archived_at=NOW,
    )
    await RaidSheetArchiveRepository(migrated_connection).upsert(archive)
    await SheetBlockRepository(migrated_connection).upsert_block(
        block=make_sheet_block(
            chat_id=group_chat_id,
            sheet_name=archive.sheet_name,
            sheet_id=archive.sheet_id,
            block_key="raid:#AAA111",
            start_cell="A1",
        ),
        updated_at=NOW,
    )
    await migrated_connection.commit()
    flow = _setup_flow(migrated_connection)

    async def run_autofix(*, binding: object) -> SheetSetupResult:
        assert binding is not None
        return SheetSetupResult(
            spreadsheet_id="sheet-id",
            spreadsheet_url="https://docs.google.com/spreadsheets/d/sheet-id/edit",
            composition_sheet_name="Состав",
            composition_sheet_id=112,
            active_cwl_sheet_name="CWL",
            active_cwl_sheet_id=223,
            active_cwl_season="2026-07",
            active_raid_sheet_name="Рейды",
            active_raid_sheet_id=445,
            active_raid_season="2026-07-17T07:00:00+00:00",
            bot_state_sheet_name="_bot_state",
            bot_state_sheet_id=334,
            raid_archive_cleanup=(RaidArchiveCleanup(archive.season_key, archive.sheet_id),),
        )

    flow._run_table_autofix = run_autofix  # type: ignore[method-assign]
    await flow.handle_callback(
        callback_data=f"{CALLBACK_FIX_SHEET_PREFIX}{group_chat_id}",
        callback_query_id="fix-raids",
        chat_id=user_id,
        message_id=10,
        user_id=user_id,
    )

    binding = await RuntimeConfigRepository(migrated_connection).get_active_sheet_binding(
        group_chat_id
    )
    assert binding is not None
    assert (
        binding.composition_sheet_id,
        binding.active_cwl_sheet_id,
        binding.active_raid_sheet_id,
        binding.bot_state_sheet_id,
    ) == (112, 223, 445, 334)
    assert (
        await RaidSheetArchiveRepository(migrated_connection).get_by_season(
            chat_id=group_chat_id,
            season_key=archive.season_key,
        )
        is None
    )
    assert all(
        block.sheet_id != archive.sheet_id
        for block in await SheetBlockRepository(migrated_connection).list_blocks(group_chat_id)
    )


@pytest.mark.asyncio
async def test_raid_column_ui_supports_add_rename_toggle_move_restore_and_delete(
    migrated_connection: aiosqlite.Connection,
) -> None:
    """Проверяет полный набор существующих column operations для raids."""

    user_id = 1001
    group_chat_id = -1102
    telegram = FakeTelegram()
    await _insert_chat(migrated_connection, chat_id=group_chat_id)
    await _insert_admin_link(migrated_connection, chat_id=group_chat_id, user_id=user_id)
    await migrated_connection.commit()
    flow = _setup_flow(migrated_connection, telegram=telegram)

    await flow.handle_callback(
        callback_data=f"{CALLBACK_COLUMN_ADD_PREFIX}{group_chat_id}:r",
        callback_query_id="raid-add",
        chat_id=user_id,
        message_id=10,
        user_id=user_id,
    )
    await flow.handle_private_text(chat_id=user_id, user_id=user_id, text="Комментарий рейда")
    user_row = await (
        await migrated_connection.execute(
            """
            SELECT column_key FROM column_profiles
            WHERE chat_id = ? AND table_type = 'raids' AND kind = 'user' AND is_active = 1
            """,
            (group_chat_id,),
        )
    ).fetchone()
    assert user_row is not None
    user_key = str(user_row["column_key"])

    await flow.handle_callback(
        callback_data=f"{CALLBACK_COLUMN_RENAME_PREFIX}{group_chat_id}:r:{user_key}",
        callback_query_id="raid-rename",
        chat_id=user_id,
        message_id=10,
        user_id=user_id,
    )
    await flow.handle_private_text(chat_id=user_id, user_id=user_id, text="Заметка рейда")
    await flow.handle_callback(
        callback_data=f"{CALLBACK_COLUMN_TOGGLE_PREFIX}{group_chat_id}:r:attacks",
        callback_query_id="raid-toggle",
        chat_id=user_id,
        message_id=10,
        user_id=user_id,
    )
    before_move = await (
        await migrated_connection.execute(
            "SELECT sort_order FROM column_profiles WHERE chat_id = ? AND table_type = 'raids' AND column_key = ?",
            (group_chat_id, user_key),
        )
    ).fetchone()
    assert before_move is not None
    await flow.handle_callback(
        callback_data=f"{CALLBACK_COLUMN_MOVE_UP_PREFIX}{group_chat_id}:r:{user_key}",
        callback_query_id="raid-move",
        chat_id=user_id,
        message_id=10,
        user_id=user_id,
    )
    after_move = await (
        await migrated_connection.execute(
            "SELECT sort_order FROM column_profiles WHERE chat_id = ? AND table_type = 'raids' AND column_key = ?",
            (group_chat_id, user_key),
        )
    ).fetchone()
    assert after_move is not None
    assert after_move["sort_order"] < before_move["sort_order"]
    await flow.handle_callback(
        callback_data=f"{CALLBACK_COLUMN_RESTORE_PREFIX}{group_chat_id}:r",
        callback_query_id="raid-restore",
        chat_id=user_id,
        message_id=10,
        user_id=user_id,
    )

    attacks = await (
        await migrated_connection.execute(
            """
            SELECT visible, title FROM column_profiles
            WHERE chat_id = ? AND table_type = 'raids' AND column_key = 'attacks'
            """,
            (group_chat_id,),
        )
    ).fetchone()
    renamed = await (
        await migrated_connection.execute(
            """
            SELECT title, is_active FROM column_profiles
            WHERE chat_id = ? AND table_type = 'raids' AND column_key = ?
            """,
            (group_chat_id, user_key),
        )
    ).fetchone()
    assert attacks is not None and tuple(attacks) == (1, "Атаки")
    assert renamed is not None and tuple(renamed) == ("Заметка рейда", 1)

    await flow.handle_callback(
        callback_data=f"{CALLBACK_COLUMN_DELETE_PREFIX}{group_chat_id}:r:{user_key}",
        callback_query_id="raid-delete",
        chat_id=user_id,
        message_id=10,
        user_id=user_id,
    )
    deleted = await (
        await migrated_connection.execute(
            """
            SELECT is_active FROM column_profiles
            WHERE chat_id = ? AND table_type = 'raids' AND column_key = ?
            """,
            (group_chat_id, user_key),
        )
    ).fetchone()
    assert deleted is not None and deleted["is_active"] == 0


@pytest.mark.asyncio
async def test_cancel_private_setup_clears_user_setup_state(
    migrated_connection: aiosqlite.Connection,
) -> None:
    """Проверяет, что /cancel чистит setup_state текущего пользователя."""

    user_id = 1001
    group_chat_id = -1001
    telegram = FakeTelegram()
    await _insert_chat(
        migrated_connection,
        chat_id=group_chat_id,
        setup_state=f"{AWAITING_USER_COLUMN_TITLE_STATE_PREFIX}{user_id}:cwl",
    )
    await _insert_admin_link(migrated_connection, chat_id=group_chat_id, user_id=user_id)
    await migrated_connection.commit()

    flow = _setup_flow(migrated_connection, telegram=telegram)

    await flow.cancel_private_setup(chat_id=user_id, user_id=user_id)

    cursor = await migrated_connection.execute(
        "SELECT setup_state FROM telegram_chats WHERE chat_id = ?",
        (group_chat_id,),
    )
    row = await cursor.fetchone()

    assert row is not None
    assert row["setup_state"] is None
    assert telegram.sent_messages[-1]["text"] == "Текущая настройка сброшена."


@pytest.mark.asyncio
async def test_cancel_private_setup_does_not_clear_other_user_state(
    migrated_connection: aiosqlite.Connection,
) -> None:
    """Проверяет, что /cancel не чистит setup_state другого пользователя."""

    user_id = 1001
    other_user_id = 2002
    group_chat_id = -1002
    setup_state = f"{AWAITING_USER_COLUMN_TITLE_STATE_PREFIX}{other_user_id}:cwl"
    telegram = FakeTelegram()

    await _insert_chat(
        migrated_connection,
        chat_id=group_chat_id,
        setup_state=setup_state,
    )
    await _insert_admin_link(migrated_connection, chat_id=group_chat_id, user_id=other_user_id)
    await migrated_connection.commit()

    flow = _setup_flow(migrated_connection, telegram=telegram)

    await flow.cancel_private_setup(chat_id=user_id, user_id=user_id)

    cursor = await migrated_connection.execute(
        "SELECT setup_state FROM telegram_chats WHERE chat_id = ?",
        (group_chat_id,),
    )
    row = await cursor.fetchone()

    assert row is not None
    assert row["setup_state"] == setup_state
    assert telegram.sent_messages[-1]["text"] == "Активной настройки нет."


@pytest.mark.asyncio
async def test_user_column_text_completion_requires_fresh_admin_check(
    migrated_connection: aiosqlite.Connection,
) -> None:
    """Проверяет fresh admin check перед созданием user-колонки из текста."""

    user_id = 1001
    group_chat_id = -1003
    telegram = FakeTelegram()
    access = RecordingAccessService(is_admin_result=False)

    await _insert_chat(
        migrated_connection,
        chat_id=group_chat_id,
        setup_state=f"{AWAITING_USER_COLUMN_TITLE_STATE_PREFIX}{user_id}:cwl",
    )
    await _insert_admin_link(migrated_connection, chat_id=group_chat_id, user_id=user_id)
    await migrated_connection.commit()

    flow = _setup_flow(migrated_connection, telegram=telegram, access=access)

    await flow.handle_private_text(chat_id=user_id, user_id=user_id, text="Новая колонка")

    assert access.calls == [
        {
            "chat_id": group_chat_id,
            "user_id": user_id,
            "force_refresh": True,
        },
    ]
    assert telegram.sent_messages[-1]["text"] == "Нет доступа."

    cursor = await migrated_connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM column_profiles
        WHERE chat_id = ? AND table_type = 'cwl' AND kind = 'user'
        """,
        (group_chat_id,),
    )
    row = await cursor.fetchone()

    assert row is not None
    assert row["count"] == 0


@pytest.mark.asyncio
async def test_column_rename_text_completion_requires_fresh_admin_check(
    migrated_connection: aiosqlite.Connection,
) -> None:
    """Проверяет fresh admin check перед rename колонки из текста."""

    user_id = 1001
    group_chat_id = -1004
    telegram = FakeTelegram()
    access = RecordingAccessService(is_admin_result=False)

    await _insert_chat(
        migrated_connection,
        chat_id=group_chat_id,
        setup_state=f"{AWAITING_COLUMN_RENAME_STATE_PREFIX}{user_id}:cwl:stars",
    )
    await _insert_admin_link(migrated_connection, chat_id=group_chat_id, user_id=user_id)
    await _insert_column_profile(
        migrated_connection,
        chat_id=group_chat_id,
        table_type="cwl",
        column_key="stars",
        title="Звезды",
        value_type="integer",
    )
    await migrated_connection.commit()

    flow = _setup_flow(migrated_connection, telegram=telegram, access=access)

    await flow.handle_private_text(chat_id=user_id, user_id=user_id, text="Новые звезды")

    assert access.calls == [
        {
            "chat_id": group_chat_id,
            "user_id": user_id,
            "force_refresh": True,
        },
    ]
    assert telegram.sent_messages[-1]["text"] == "Нет доступа."

    cursor = await migrated_connection.execute(
        """
        SELECT title
        FROM column_profiles
        WHERE chat_id = ? AND table_type = 'cwl' AND column_key = 'stars'
        """,
        (group_chat_id,),
    )
    row = await cursor.fetchone()

    assert row is not None
    assert row["title"] == "Звезды"


@pytest.mark.asyncio
async def test_edit_or_send_message_ignores_message_not_modified_without_new_message() -> None:
    """Проверяет, что TelegramMessageNotModifiedError не создаёт дубль сообщения."""

    telegram = FakeTelegram(raise_not_modified_on_edit=True)

    await _edit_or_send_message(
        telegram=telegram,  # type: ignore[arg-type]
        chat_id=1001,
        message_id=10,
        text="Тот же текст",
        reply_markup={"inline_keyboard": []},
    )

    assert len(telegram.edit_attempts) == 1
    assert telegram.edited_messages == []
    assert telegram.sent_messages == []


@pytest.mark.asyncio
async def test_sensitive_callback_uses_force_refresh_admin_check(
    migrated_connection: aiosqlite.Connection,
) -> None:
    """Проверяет force_refresh=True для чувствительного callback."""

    user_id = 1001
    group_chat_id = -1005
    telegram = FakeTelegram()
    access = RecordingAccessService(is_admin_result=True)

    await _insert_chat(migrated_connection, chat_id=group_chat_id)
    await _insert_admin_link(migrated_connection, chat_id=group_chat_id, user_id=user_id)
    await migrated_connection.commit()

    flow = _setup_flow(migrated_connection, telegram=telegram, access=access)

    await flow.handle_callback(
        callback_data=f"{CALLBACK_CLAN_ADD_PREFIX}{group_chat_id}",
        callback_query_id="callback-1",
        chat_id=user_id,
        message_id=10,
        user_id=user_id,
    )

    assert access.calls == [
        {
            "chat_id": group_chat_id,
            "user_id": user_id,
            "force_refresh": True,
        },
    ]
    assert telegram.answered_callbacks[-1]["text"] == "Принято."
    assert telegram.sent_messages[-1]["text"] == "Отправьте тег клана, например #2RVJ0CUR9."

    cursor = await migrated_connection.execute(
        "SELECT setup_state FROM telegram_chats WHERE chat_id = ?",
        (group_chat_id,),
    )
    row = await cursor.fetchone()

    assert row is not None
    assert row["setup_state"] == f"{AWAITING_CLAN_TAG_STATE_PREFIX}{user_id}"


@pytest.mark.asyncio
async def test_user_column_text_completion_rejects_duplicate_title(
    migrated_connection: aiosqlite.Connection,
) -> None:
    """Проверяет запрет создания двух колонок с одинаковым title в одном table_type."""

    user_id = 1001
    group_chat_id = -1006
    telegram = FakeTelegram()
    access = RecordingAccessService(is_admin_result=True)

    await _insert_chat(
        migrated_connection,
        chat_id=group_chat_id,
        setup_state=f"{AWAITING_USER_COLUMN_TITLE_STATE_PREFIX}{user_id}:cwl",
    )
    await _insert_admin_link(migrated_connection, chat_id=group_chat_id, user_id=user_id)
    await _insert_column_profile(
        migrated_connection,
        chat_id=group_chat_id,
        table_type="cwl",
        column_key="username",
        title="Юзернейм",
        kind="user",
        sort_order=100,
    )
    await migrated_connection.commit()

    flow = _setup_flow(migrated_connection, telegram=telegram, access=access)

    await flow.handle_private_text(chat_id=user_id, user_id=user_id, text=" юзернейм ")

    assert (
        telegram.sent_messages[-1]["text"] == "Колонка с таким названием уже есть в этом разделе."
    )

    cursor = await migrated_connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM column_profiles
        WHERE chat_id = ? AND table_type = 'cwl' AND title = 'Юзернейм'
        """,
        (group_chat_id,),
    )
    row = await cursor.fetchone()

    assert row is not None
    assert row["count"] == 1


@pytest.mark.asyncio
async def test_column_rename_text_completion_rejects_duplicate_title(
    migrated_connection: aiosqlite.Connection,
) -> None:
    """Проверяет запрет переименования колонки в существующий title."""

    user_id = 1001
    group_chat_id = -1007
    telegram = FakeTelegram()
    access = RecordingAccessService(is_admin_result=True)

    await _insert_chat(
        migrated_connection,
        chat_id=group_chat_id,
        setup_state=f"{AWAITING_COLUMN_RENAME_STATE_PREFIX}{user_id}:cwl:second",
    )
    await _insert_admin_link(migrated_connection, chat_id=group_chat_id, user_id=user_id)
    await _insert_column_profile(
        migrated_connection,
        chat_id=group_chat_id,
        table_type="cwl",
        column_key="first",
        title="Юзернейм",
        kind="user",
        sort_order=100,
    )
    await _insert_column_profile(
        migrated_connection,
        chat_id=group_chat_id,
        table_type="cwl",
        column_key="second",
        title="Discord",
        kind="user",
        sort_order=110,
    )
    await migrated_connection.commit()

    flow = _setup_flow(migrated_connection, telegram=telegram, access=access)

    await flow.handle_private_text(chat_id=user_id, user_id=user_id, text="юзернейм")

    assert (
        telegram.sent_messages[-1]["text"] == "Колонка с таким названием уже есть в этом разделе."
    )

    cursor = await migrated_connection.execute(
        """
        SELECT title
        FROM column_profiles
        WHERE chat_id = ? AND table_type = 'cwl' AND column_key = 'second'
        """,
        (group_chat_id,),
    )
    row = await cursor.fetchone()

    assert row is not None
    assert row["title"] == "Discord"


@pytest.mark.asyncio
async def test_sheet_permission_error_text_is_human(
    migrated_connection: aiosqlite.Connection,
) -> None:
    """Проверяет, что 403 Google Sheets не показывается пользователю сырой ошибкой."""

    flow = _setup_flow(migrated_connection)

    text = flow._sheet_error_text(
        GoogleSheetsWriteError(
            "Google Sheets API HTTP 403: The caller does not have permission",
        ),
    )

    assert "Нет доступа к Google-таблице." in text
    assert "Редактор" in text
    assert "Проверить доступ" in text
    assert "Google Sheets API HTTP 403" not in text
    assert "The caller does not have permission" not in text
