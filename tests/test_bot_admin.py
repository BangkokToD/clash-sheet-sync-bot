"""Integration tests for BotApp superadmin routing."""

from __future__ import annotations

import aiosqlite
import pytest

from clash_sheet_sync_bot.admin.flow import PENDING_BROADCAST_TEXT
from clash_sheet_sync_bot.bot import BotApp
from clash_sheet_sync_bot.repositories import BotUserRepository, SuperadminRepository
from clash_sheet_sync_bot.storage import Database
from clash_sheet_sync_bot.telegram.client import TelegramMessageEntity
from tests.fakes.factories import make_app_config
from tests.fakes.telegram import FakeTelegram


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_id", "expects_admin_button"),
    ((1001, True), (2002, False)),
)
async def test_private_start_registers_user_and_renders_role_menu(
    migrated_connection: aiosqlite.Connection,
    user_id: int,
    expects_admin_button: bool,
) -> None:
    telegram = FakeTelegram()
    app = BotApp(
        config=make_app_config(superadmin_user_id=1001),
        telegram=telegram,  # type: ignore[arg-type]
        database=Database("unused-test.db"),
        bot_username="test_bot",
    )
    await app._handle_update_with_connection(
        update={
            "message": {
                "from": {"id": user_id},
                "chat": {"id": user_id, "type": "private", "first_name": "User"},
                "text": "/start",
            }
        },
        connection=migrated_connection,
    )

    row = await (
        await migrated_connection.execute(
            "SELECT private_chat_id, is_active FROM bot_users WHERE user_id = ?",
            (user_id,),
        )
    ).fetchone()
    assert row is not None and tuple(row) == (user_id, 1)
    markup = telegram.sent_messages[-1]["reply_markup"]
    buttons = [button for keyboard_row in markup["inline_keyboard"] for button in keyboard_row]
    assert (
        any(button.get("callback_data") == "admin:menu" for button in buttons)
        is expects_admin_button
    )


@pytest.mark.asyncio
async def test_bot_routes_incoming_custom_emoji_entities_to_broadcast(
    migrated_connection: aiosqlite.Connection,
) -> None:
    user_id = 1001
    users = BotUserRepository(migrated_connection)
    await users.observe_private_user(
        user_id=user_id,
        private_chat_id=user_id,
        now="2026-08-08T20:00:00+00:00",
    )
    await users.set_pending_action(
        user_id=user_id,
        action=PENDING_BROADCAST_TEXT,
        now="2026-08-08T20:00:00+00:00",
    )
    await migrated_connection.commit()
    telegram = FakeTelegram()
    app = BotApp(
        config=make_app_config(superadmin_user_id=user_id),
        telegram=telegram,  # type: ignore[arg-type]
        database=Database("unused-test.db"),
        bot_username="test_bot",
    )

    await app._handle_update_with_connection(
        update={
            "message": {
                "from": {"id": user_id},
                "chat": {"id": user_id, "type": "private", "first_name": "User"},
                "text": "🏠 Обновление",
                "entities": [
                    {
                        "type": "custom_emoji",
                        "offset": 0,
                        "length": 2,
                        "custom_emoji_id": "5377544228656815478",
                    }
                ],
            }
        },
        connection=migrated_connection,
    )

    entity = TelegramMessageEntity(
        type="custom_emoji",
        offset=0,
        length=2,
        custom_emoji_id="5377544228656815478",
    )
    assert telegram.sent_messages[-1]["entities"] == (entity,)
    draft = await SuperadminRepository(migrated_connection).get_broadcast(1)
    assert draft is not None
    assert draft.entities_json == (
        '[{"type":"custom_emoji","offset":0,"length":2,"custom_emoji_id":"5377544228656815478"}]'
    )
