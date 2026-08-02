"""Tests for setup inline keyboard callback payloads."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from clash_sheet_sync_bot.models import ColumnProfile, TableType
from clash_sheet_sync_bot.setup.keyboards import (
    columns_section_keyboard,
    main_private_keyboard,
    settings_menu_keyboard,
    table_type_from_payload,
    table_type_payload,
)
from clash_sheet_sync_bot.sheets.column_profiles import default_columns

TELEGRAM_CALLBACK_DATA_LIMIT = 64


def _callback_data_items(markup: dict[str, Any]) -> Iterator[str]:
    for row in markup.get("inline_keyboard", []):
        for button in row:
            value = button.get("callback_data")
            if isinstance(value, str):
                yield value


def test_column_keyboard_callback_data_fits_telegram_limit() -> None:
    """Проверяет, что callback_data кнопок колонок не превышает лимит Telegram."""

    chat_id = -1001234567890
    table_types: tuple[TableType, ...] = (
        "composition_active",
        "composition_exited",
        "cwl",
        "raids",
    )

    for table_type in table_types:
        columns = tuple(
            ColumnProfile(
                chat_id=chat_id,
                table_type=definition.table_type,
                column_key=definition.column_key,
                title=definition.title,
                visible=definition.visible,
                kind=definition.kind,
                value_type=definition.value_type,
                sort_order=definition.sort_order,
            )
            for definition in default_columns(table_type)
        )
        markup = columns_section_keyboard(chat_id, table_type, columns)
        callback_data_items = list(_callback_data_items(markup))

        assert callback_data_items
        assert all(
            len(callback_data.encode("utf-8")) <= TELEGRAM_CALLBACK_DATA_LIMIT
            for callback_data in callback_data_items
        )


def test_raid_column_payload_is_short_and_round_trips() -> None:
    """Проверяет стабильный короткий callback payload рейдовых колонок."""

    assert table_type_payload("raids") == "r"
    assert table_type_from_payload("r") == "raids"
    assert table_type_from_payload("raids") == "raids"
    assert table_type_from_payload("raid") is None


def test_settings_menu_contains_raid_columns_section() -> None:
    """Проверяет отдельный раздел настройки рейдовых колонок."""

    markup = settings_menu_keyboard(-1001)

    assert {
        "text": "Колонки рейдов",
        "callback_data": "settings:section:-1001:raids_columns",
    } in [button for row in markup["inline_keyboard"] for button in row]


def test_main_menu_adds_support_and_admin_buttons_by_role() -> None:
    """Проверяет, что привилегированная кнопка не видна обычному пользователю."""

    regular = main_private_keyboard(support_url="https://t.me/support")
    superadmin = main_private_keyboard(
        support_url="https://t.me/support",
        is_superadmin=True,
    )

    regular_buttons = [button for row in regular["inline_keyboard"] for button in row]
    admin_buttons = [button for row in superadmin["inline_keyboard"] for button in row]
    assert {"text": "Техподдержка", "url": "https://t.me/support"} in regular_buttons
    assert not any(button["text"] == "Администрирование" for button in regular_buttons)
    assert {
        "text": "Администрирование",
        "callback_data": "admin:menu",
    } in admin_buttons
