"""SQLite-backed тесты setup-flow access-контрактов."""

from __future__ import annotations

import aiosqlite
import pytest
from fakes import FakeTelegram, RecordingAccessService, make_app_config

from setup_flow import (
    AWAITING_CLAN_TAG_STATE_PREFIX,
    AWAITING_COLUMN_RENAME_STATE_PREFIX,
    AWAITING_USER_COLUMN_TITLE_STATE_PREFIX,
    CALLBACK_CLAN_ADD_PREFIX,
    SetupFlow,
    _edit_or_send_message,
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
