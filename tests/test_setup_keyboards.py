"""Tests for setup inline keyboard callback payloads."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from clash_sheet_sync_bot.models import ColumnProfile, TableType
from clash_sheet_sync_bot.setup.keyboards import columns_section_keyboard
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
    table_types: tuple[TableType, ...] = ("composition_active", "composition_exited", "cwl")

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
