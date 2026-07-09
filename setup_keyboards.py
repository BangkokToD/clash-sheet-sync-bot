"""Telegram inline keyboard builders для setup-flow."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from models import TableType
from telegram_client import JsonObject

CALLBACK_CONNECT_GROUP: Final = "setup:create_token"
CALLBACK_PRIVATE_START: Final = "setup:start"
CALLBACK_MY_GROUPS: Final = "setup:my_groups"
CALLBACK_HELP: Final = "setup:help"
CALLBACK_SETTINGS_PREFIX: Final = "settings:open:"
CALLBACK_SETTINGS_SECTION_PREFIX: Final = "settings:section:"
CALLBACK_BIND_SHEET_PREFIX: Final = "sheet:bind:"
CALLBACK_CHANGE_SHEET_PREFIX: Final = "sheet:change:"
CALLBACK_UNLINK_SHEET_PREFIX: Final = "sheet:unlink:"
CALLBACK_CONFIRM_UNLINK_SHEET_PREFIX: Final = "sheet:unlink_confirm:"
CALLBACK_DIAGNOSE_SHEET_PREFIX: Final = "sheet:diagnose:"
CALLBACK_FIX_SHEET_PREFIX: Final = "sheet:fix:"
CALLBACK_CREATE_TRANSFER_PREFIX: Final = "transfer:create:"
CALLBACK_CHECK_SHEET_PREFIX: Final = "sheet:check:"
CALLBACK_CLAN_ADD_PREFIX: Final = "clans:add:"
CALLBACK_CLAN_CONFIRM_PREFIX: Final = "clans:confirm:"
CALLBACK_CLAN_REMOVE_PREFIX: Final = "clans:remove:"
CALLBACK_CLAN_MOVE_UP_PREFIX: Final = "clans:up:"
CALLBACK_CLAN_MOVE_DOWN_PREFIX: Final = "clans:down:"
CALLBACK_COLUMN_ADD_PREFIX: Final = "columns:add:"
CALLBACK_COLUMN_TOGGLE_PREFIX: Final = "columns:toggle:"
CALLBACK_COLUMN_RENAME_PREFIX: Final = "columns:rename:"
CALLBACK_COLUMN_DELETE_PREFIX: Final = "columns:delete:"
CALLBACK_COLUMN_MOVE_UP_PREFIX: Final = "columns:up:"
CALLBACK_COLUMN_MOVE_DOWN_PREFIX: Final = "columns:down:"
CALLBACK_COLUMN_RESTORE_PREFIX: Final = "columns:restore:"

SETTINGS_SECTIONS: Final = {
    "table": "Таблица",
    "clans": "Кланы",
    "composition_active_columns": "Колонки активного состава",
    "composition_exited_columns": "Колонки вышедших",
    "cwl_columns": "Колонки CWL",
}


@dataclass(frozen=True, slots=True)
class KnownGroupButton:
    """Данные кнопки известной группы.

    Attributes:
        chat_id: ID Telegram-группы.
        title: Название группы.
    """

    chat_id: int
    title: str


def main_private_keyboard() -> JsonObject:
    """Создаёт главное меню личного чата."""

    return {
        "inline_keyboard": [
            [{"text": "Подключить группу", "callback_data": CALLBACK_CONNECT_GROUP}],
            [{"text": "Мои группы", "callback_data": CALLBACK_MY_GROUPS}],
            [{"text": "Помощь", "callback_data": CALLBACK_HELP}],
        ],
    }


def known_groups_keyboard(groups: Sequence[KnownGroupButton]) -> JsonObject:
    """Создаёт клавиатуру известных групп."""

    return {
        "inline_keyboard": [
            [
                {
                    "text": group.title,
                    "callback_data": f"{CALLBACK_SETTINGS_PREFIX}{group.chat_id}",
                },
            ]
            for group in groups
        ]
        + [[{"text": "Назад", "callback_data": CALLBACK_PRIVATE_START}]],
    }


def private_chat_keyboard(bot_username: str | None) -> JsonObject | None:
    """Создаёт кнопку перехода в личный чат с ботом."""

    if bot_username is None:
        return None
    return {
        "inline_keyboard": [
            [{"text": "Открыть личный чат", "url": f"https://t.me/{bot_username}"}],
        ],
    }


def settings_menu_keyboard(group_chat_id: int) -> JsonObject:
    """Создаёт skeleton-меню настроек группы."""

    return {
        "inline_keyboard": [
            [
                {
                    "text": title,
                    "callback_data": (f"{CALLBACK_SETTINGS_SECTION_PREFIX}{group_chat_id}:{key}"),
                },
            ]
            for key, title in SETTINGS_SECTIONS.items()
        ]
        + [[{"text": "Назад", "callback_data": CALLBACK_MY_GROUPS}]],
    }


def sheet_section_keyboard(group_chat_id: int, *, has_binding: bool) -> JsonObject:
    """Создаёт клавиатуру раздела таблицы."""

    keyboard: list[list[dict[str, str]]] = []
    if has_binding:
        keyboard.extend(
            [
                [
                    {
                        "text": "Проверить таблицу",
                        "callback_data": f"{CALLBACK_DIAGNOSE_SHEET_PREFIX}{group_chat_id}",
                    },
                ],
                [
                    {
                        "text": "Сменить таблицу",
                        "callback_data": f"{CALLBACK_CHANGE_SHEET_PREFIX}{group_chat_id}",
                    },
                ],
                [
                    {
                        "text": "Отвязать таблицу",
                        "callback_data": f"{CALLBACK_UNLINK_SHEET_PREFIX}{group_chat_id}",
                    },
                ],
                [
                    {
                        "text": "Перенести в другую группу",
                        "callback_data": f"{CALLBACK_CREATE_TRANSFER_PREFIX}{group_chat_id}",
                    },
                ],
            ],
        )
    else:
        keyboard.append(
            [
                {
                    "text": "Привязать таблицу",
                    "callback_data": f"{CALLBACK_BIND_SHEET_PREFIX}{group_chat_id}",
                },
            ],
        )
    keyboard.append(
        [
            {
                "text": "Назад",
                "callback_data": f"{CALLBACK_SETTINGS_PREFIX}{group_chat_id}",
            },
        ],
    )
    return {"inline_keyboard": keyboard}


def confirm_change_sheet_keyboard(group_chat_id: int) -> JsonObject:
    """Создаёт клавиатуру подтверждения смены таблицы."""

    return {
        "inline_keyboard": [
            [
                {
                    "text": "Продолжить",
                    "callback_data": f"{CALLBACK_BIND_SHEET_PREFIX}{group_chat_id}",
                }
            ],
            [
                {
                    "text": "Назад",
                    "callback_data": f"{CALLBACK_SETTINGS_SECTION_PREFIX}{group_chat_id}:table",
                }
            ],
        ],
    }


def confirm_unlink_sheet_keyboard(group_chat_id: int) -> JsonObject:
    """Создаёт клавиатуру подтверждения отвязки таблицы."""

    return {
        "inline_keyboard": [
            [
                {
                    "text": "Отвязать таблицу",
                    "callback_data": f"{CALLBACK_CONFIRM_UNLINK_SHEET_PREFIX}{group_chat_id}",
                }
            ],
            [
                {
                    "text": "Назад",
                    "callback_data": f"{CALLBACK_SETTINGS_SECTION_PREFIX}{group_chat_id}:table",
                }
            ],
        ],
    }


def diagnostic_keyboard(group_chat_id: int, has_fixable_issues: bool) -> JsonObject:
    """Создаёт клавиатуру результата диагностики."""

    keyboard: list[list[dict[str, str]]] = []
    if has_fixable_issues:
        keyboard.append(
            [{"text": "Исправить", "callback_data": f"{CALLBACK_FIX_SHEET_PREFIX}{group_chat_id}"}]
        )
    keyboard.append(
        [
            {
                "text": "Назад",
                "callback_data": f"{CALLBACK_SETTINGS_SECTION_PREFIX}{group_chat_id}:table",
            }
        ]
    )
    return {"inline_keyboard": keyboard}


def check_sheet_access_keyboard(group_chat_id: int) -> JsonObject:
    """Создаёт клавиатуру проверки доступа к таблице."""

    return {
        "inline_keyboard": [
            [
                {
                    "text": "Проверить доступ",
                    "callback_data": f"{CALLBACK_CHECK_SHEET_PREFIX}{group_chat_id}",
                },
            ],
            [
                {
                    "text": "Назад",
                    "callback_data": f"{CALLBACK_SETTINGS_SECTION_PREFIX}{group_chat_id}:table",
                },
            ],
        ],
    }


def clans_section_keyboard(group_chat_id: int, clans: Sequence[Any]) -> JsonObject:
    """Создаёт клавиатуру раздела кланов."""

    keyboard: list[list[dict[str, str]]] = [
        [{"text": "Добавить клан", "callback_data": f"{CALLBACK_CLAN_ADD_PREFIX}{group_chat_id}"}],
    ]
    total = len(clans)
    for index, clan in enumerate(clans):
        tag_payload = _tag_payload(clan.clan_tag)
        up_callback = f"{CALLBACK_CLAN_MOVE_UP_PREFIX}{group_chat_id}:{tag_payload}"
        down_callback = f"{CALLBACK_CLAN_MOVE_DOWN_PREFIX}{group_chat_id}:{tag_payload}"
        keyboard.append(
            [
                {"text": clan.clan_name, "callback_data": "noop"},
                *_move_buttons(
                    index=index,
                    total=total,
                    up_callback=up_callback,
                    down_callback=down_callback,
                ),
                {
                    "text": "Удалить",
                    "callback_data": f"{CALLBACK_CLAN_REMOVE_PREFIX}{group_chat_id}:{tag_payload}",
                },
            ],
        )
    keyboard.append(
        [{"text": "Назад", "callback_data": f"{CALLBACK_SETTINGS_PREFIX}{group_chat_id}"}]
    )
    return {"inline_keyboard": keyboard}


def confirm_clan_keyboard(group_chat_id: int, clan_tag: str) -> JsonObject:
    """Создаёт клавиатуру подтверждения добавления клана."""

    return {
        "inline_keyboard": [
            [
                {
                    "text": "Добавить",
                    "callback_data": f"{CALLBACK_CLAN_CONFIRM_PREFIX}{group_chat_id}:{_tag_payload(clan_tag)}",
                },
            ],
            [
                {
                    "text": "Кланы",
                    "callback_data": f"{CALLBACK_SETTINGS_SECTION_PREFIX}{group_chat_id}:clans",
                }
            ],
        ],
    }


def columns_section_keyboard(
    group_chat_id: int, table_type: TableType, columns: Sequence[Any]
) -> JsonObject:
    """Создаёт клавиатуру управления колонками."""

    keyboard: list[list[dict[str, str]]] = [
        [
            {
                "text": "Добавить пользовательскую колонку",
                "callback_data": f"{CALLBACK_COLUMN_ADD_PREFIX}{group_chat_id}:{table_type}",
            },
        ],
        [
            {
                "text": "Восстановить обязательные",
                "callback_data": f"{CALLBACK_COLUMN_RESTORE_PREFIX}{group_chat_id}:{table_type}",
            },
        ],
    ]
    editable_columns = [column for column in columns if column.kind != "service"]
    total = len(editable_columns)

    for index, column in enumerate(editable_columns):
        action_text = "Удалить" if column.kind == "user" else ("✅" if column.visible else "❌")
        action_callback = (
            f"{CALLBACK_COLUMN_DELETE_PREFIX}{group_chat_id}:{table_type}:{column.column_key}"
            if column.kind == "user"
            else f"{CALLBACK_COLUMN_TOGGLE_PREFIX}{group_chat_id}:{table_type}:{column.column_key}"
        )
        up_callback = (
            f"{CALLBACK_COLUMN_MOVE_UP_PREFIX}{group_chat_id}:{table_type}:{column.column_key}"
        )
        down_callback = (
            f"{CALLBACK_COLUMN_MOVE_DOWN_PREFIX}{group_chat_id}:{table_type}:{column.column_key}"
        )
        keyboard.append(
            [
                {"text": column.title, "callback_data": "noop"},
                *_move_buttons(
                    index=index,
                    total=total,
                    up_callback=up_callback,
                    down_callback=down_callback,
                ),
                {"text": action_text, "callback_data": action_callback},
            ],
        )
    keyboard.append(
        [{"text": "Назад", "callback_data": f"{CALLBACK_SETTINGS_PREFIX}{group_chat_id}"}]
    )
    return {"inline_keyboard": keyboard}


def _move_buttons(
    *,
    index: int,
    total: int,
    up_callback: str,
    down_callback: str,
) -> list[dict[str, str]]:
    """Создаёт кнопки изменения порядка для inline-списков."""

    if total <= 1:
        return [{"text": "·", "callback_data": "noop"}, {"text": "·", "callback_data": "noop"}]
    if index == 0:
        return [
            {"text": "·", "callback_data": "noop"},
            {"text": "↓", "callback_data": down_callback},
        ]
    if index == total - 1:
        return [{"text": "↑", "callback_data": up_callback}, {"text": "·", "callback_data": "noop"}]
    return [
        {"text": "↑", "callback_data": up_callback},
        {"text": "↓", "callback_data": down_callback},
    ]


def _tag_payload(clan_tag: str) -> str:
    """Преобразует тег клана в безопасный payload callback."""

    return clan_tag.removeprefix("#")
