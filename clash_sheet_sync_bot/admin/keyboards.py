"""Inline keyboards for superadmin actions."""

from __future__ import annotations

from typing import Final

from clash_sheet_sync_bot.setup.keyboards import (
    CALLBACK_ADMIN_MENU as _CALLBACK_ADMIN_MENU,
    CALLBACK_PRIVATE_START,
)
from clash_sheet_sync_bot.telegram.client import JsonObject

CALLBACK_ADMIN_MENU: Final = _CALLBACK_ADMIN_MENU
CALLBACK_SUPPORT_CONNECT: Final = "admin:support:connect"
CALLBACK_BROADCAST_START: Final = "admin:broadcast:start"
CALLBACK_BROADCAST_CONFIRM_PREFIX: Final = "admin:broadcast:confirm:"
CALLBACK_BROADCAST_CANCEL_PREFIX: Final = "admin:broadcast:cancel:"


def admin_menu_keyboard() -> JsonObject:
    """Builds the superadmin menu."""

    return {
        "inline_keyboard": [
            [
                {
                    "text": "Подключить техподдержку",
                    "callback_data": CALLBACK_SUPPORT_CONNECT,
                }
            ],
            [{"text": "Рассылка всем", "callback_data": CALLBACK_BROADCAST_START}],
            [{"text": "Назад", "callback_data": CALLBACK_PRIVATE_START}],
        ]
    }


def broadcast_confirmation_keyboard(broadcast_id: int) -> JsonObject:
    """Builds explicit send/cancel controls for a broadcast draft."""

    return {
        "inline_keyboard": [
            [
                {
                    "text": "Отправить всем",
                    "callback_data": f"{CALLBACK_BROADCAST_CONFIRM_PREFIX}{broadcast_id}",
                }
            ],
            [
                {
                    "text": "Отмена",
                    "callback_data": f"{CALLBACK_BROADCAST_CANCEL_PREFIX}{broadcast_id}",
                }
            ],
        ]
    }
