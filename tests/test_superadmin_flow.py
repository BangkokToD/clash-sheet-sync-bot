"""Tests for the role-gated support and broadcast flows."""

from __future__ import annotations

import aiosqlite
import pytest

from clash_sheet_sync_bot.admin.flow import SuperadminFlow
from clash_sheet_sync_bot.admin.keyboards import (
    CALLBACK_ADMIN_MENU,
    CALLBACK_BROADCAST_START,
    CALLBACK_SUPPORT_CONNECT,
)
from clash_sheet_sync_bot.repositories import SuperadminRepository
from clash_sheet_sync_bot.setup.flow import TelegramChatInfo
from tests.fakes.factories import make_app_config
from tests.fakes.telegram import FakeTelegram, RecordingAccessService

SUPERADMIN_ID = 1001
REGULAR_USER_ID = 2002
NOW = "2026-08-02T12:00:00+00:00"


def _flow(
    connection: aiosqlite.Connection,
    telegram: FakeTelegram,
    *,
    access: RecordingAccessService | None = None,
) -> SuperadminFlow:
    return SuperadminFlow(
        config=make_app_config(superadmin_user_id=SUPERADMIN_ID),
        telegram=telegram,  # type: ignore[arg-type]
        connection=connection,
        access=access or RecordingAccessService(),  # type: ignore[arg-type]
    )


def _buttons(message: dict[str, object]) -> list[dict[str, str]]:
    markup = message["reply_markup"]
    assert isinstance(markup, dict)
    return [button for row in markup["inline_keyboard"] for button in row]


async def _insert_group(
    connection: aiosqlite.Connection,
    *,
    chat_id: int,
    status: str,
) -> None:
    await connection.execute(
        """
        INSERT INTO telegram_chats(
            chat_id, title, type, status, created_at, updated_at
        ) VALUES (?, ?, 'supergroup', ?, ?, ?)
        """,
        (chat_id, f"Group {chat_id}", status, NOW, NOW),
    )


@pytest.mark.asyncio
async def test_private_menu_is_role_aware_and_contains_support_link(
    migrated_connection: aiosqlite.Connection,
) -> None:
    telegram = FakeTelegram()
    flow = _flow(migrated_connection, telegram)
    await SuperadminRepository(migrated_connection).set_support_group(
        chat_id=-9001,
        title="Support",
        url="https://t.me/support",
        updated_by_user_id=SUPERADMIN_ID,
        updated_at=NOW,
    )
    await migrated_connection.commit()

    await flow.send_private_start(chat_id=REGULAR_USER_ID, user_id=REGULAR_USER_ID)
    await flow.send_private_start(chat_id=SUPERADMIN_ID, user_id=SUPERADMIN_ID)

    regular_buttons = _buttons(telegram.sent_messages[0])
    admin_buttons = _buttons(telegram.sent_messages[1])
    assert {"text": "Техподдержка", "url": "https://t.me/support"} in regular_buttons
    assert not any(button["text"] == "Администрирование" for button in regular_buttons)
    assert any(button.get("callback_data") == CALLBACK_ADMIN_MENU for button in admin_buttons)


@pytest.mark.asyncio
async def test_forged_admin_callback_is_denied(
    migrated_connection: aiosqlite.Connection,
) -> None:
    telegram = FakeTelegram()
    handled = await _flow(migrated_connection, telegram).handle_callback(
        callback_data=CALLBACK_ADMIN_MENU,
        callback_query_id="forged",
        chat_id=REGULAR_USER_ID,
        message_id=1,
        user_id=REGULAR_USER_ID,
    )

    assert handled is True
    assert telegram.answered_callbacks == [
        {"callback_query_id": "forged", "text": "Нет доступа.", "show_alert": True}
    ]
    assert telegram.edited_messages == []


@pytest.mark.asyncio
async def test_superadmin_connects_public_support_group_with_one_time_token(
    migrated_connection: aiosqlite.Connection,
) -> None:
    telegram = FakeTelegram()
    access = RecordingAccessService()
    flow = _flow(migrated_connection, telegram, access=access)
    await flow.observe_private_user(user_id=SUPERADMIN_ID, private_chat_id=SUPERADMIN_ID)
    await flow.handle_callback(
        callback_data=CALLBACK_SUPPORT_CONNECT,
        callback_query_id="support-token",
        chat_id=SUPERADMIN_ID,
        message_id=1,
        user_id=SUPERADMIN_ID,
    )
    token_row = await (
        await migrated_connection.execute(
            "SELECT token FROM support_setup_tokens WHERE used_at IS NULL"
        )
    ).fetchone()
    assert token_row is not None

    await flow.connect_support_group(
        chat=TelegramChatInfo(
            chat_id=-9001,
            title="Support",
            type="supergroup",
            username="public_support",
        ),
        user_id=SUPERADMIN_ID,
        raw_token=token_row["token"],
    )

    support = await SuperadminRepository(migrated_connection).get_support_group()
    assert support is not None
    assert (support.chat_id, support.title, support.url) == (
        -9001,
        "Support",
        "https://t.me/public_support",
    )
    assert access.calls[-1] == {
        "chat_id": -9001,
        "user_id": SUPERADMIN_ID,
        "force_refresh": True,
    }
    assert telegram.invite_link_requests == []


@pytest.mark.asyncio
async def test_private_support_group_uses_bot_created_invite_link(
    migrated_connection: aiosqlite.Connection,
) -> None:
    telegram = FakeTelegram(invite_link="https://t.me/+private-support")
    flow = _flow(migrated_connection, telegram)
    await flow.observe_private_user(user_id=SUPERADMIN_ID, private_chat_id=SUPERADMIN_ID)
    await flow.handle_callback(
        callback_data=CALLBACK_SUPPORT_CONNECT,
        callback_query_id="private-support-token",
        chat_id=SUPERADMIN_ID,
        message_id=1,
        user_id=SUPERADMIN_ID,
    )
    token_row = await (
        await migrated_connection.execute("SELECT token FROM support_setup_tokens")
    ).fetchone()
    assert token_row is not None

    await flow.connect_support_group(
        chat=TelegramChatInfo(chat_id=-9002, title="Private support", type="supergroup"),
        user_id=SUPERADMIN_ID,
        raw_token=token_row["token"],
    )

    support = await SuperadminRepository(migrated_connection).get_support_group()
    assert support is not None and support.url == "https://t.me/+private-support"
    assert telegram.invite_link_requests == [{"chat_id": -9002, "name": "Техподдержка бота"}]


@pytest.mark.asyncio
async def test_broadcast_requires_preview_and_reaches_users_and_active_groups(
    migrated_connection: aiosqlite.Connection,
) -> None:
    telegram = FakeTelegram()
    flow = _flow(migrated_connection, telegram)
    await flow.observe_private_user(user_id=SUPERADMIN_ID, private_chat_id=SUPERADMIN_ID)
    await flow.observe_private_user(user_id=REGULAR_USER_ID, private_chat_id=REGULAR_USER_ID)
    await _insert_group(migrated_connection, chat_id=-7001, status="ready")
    await _insert_group(migrated_connection, chat_id=-7002, status="not_configured")
    await _insert_group(migrated_connection, chat_id=-7003, status="disabled")
    await SuperadminRepository(migrated_connection).set_support_group(
        chat_id=-9001,
        title="Support",
        url="https://t.me/support",
        updated_by_user_id=SUPERADMIN_ID,
        updated_at=NOW,
    )
    await migrated_connection.commit()

    await flow.handle_callback(
        callback_data=CALLBACK_BROADCAST_START,
        callback_query_id="start-broadcast",
        chat_id=SUPERADMIN_ID,
        message_id=1,
        user_id=SUPERADMIN_ID,
    )
    assert await flow.handle_private_text(
        chat_id=SUPERADMIN_ID,
        user_id=SUPERADMIN_ID,
        text="Важное объявление",
    )
    draft = await SuperadminRepository(migrated_connection).get_broadcast(1)
    assert draft is not None and draft.status == "draft"
    assert "2 пользователей и 2 групп" in telegram.sent_messages[-2]["text"]

    await flow.handle_callback(
        callback_data="admin:broadcast:confirm:1",
        callback_query_id="confirm-broadcast",
        chat_id=SUPERADMIN_ID,
        message_id=3,
        user_id=SUPERADMIN_ID,
    )

    announcement_targets = {
        message["chat_id"]
        for message in telegram.sent_messages
        if message["text"] == "Важное объявление"
    }
    assert announcement_targets == {SUPERADMIN_ID, REGULAR_USER_ID, -7001, -9001}
    row = await (
        await migrated_connection.execute(
            """
            SELECT status, user_targets_count, group_targets_count,
                   delivered_count, failed_count
            FROM broadcasts WHERE id = 1
            """
        )
    ).fetchone()
    assert row is not None
    assert tuple(row) == ("completed", 2, 2, 4, 0)

    delivery_count = len(
        [message for message in telegram.sent_messages if message["text"] == "Важное объявление"]
    )
    await flow.handle_callback(
        callback_data="admin:broadcast:confirm:1",
        callback_query_id="duplicate-confirm",
        chat_id=SUPERADMIN_ID,
        message_id=3,
        user_id=SUPERADMIN_ID,
    )
    assert (
        len(
            [
                message
                for message in telegram.sent_messages
                if message["text"] == "Важное объявление"
            ]
        )
        == delivery_count
    )
