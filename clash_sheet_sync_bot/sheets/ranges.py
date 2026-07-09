"""Общие helpers для A1-координат и Google Sheets GridRange."""

from __future__ import annotations

import re
from typing import Any

JsonObject = dict[str, Any]


def parse_a1_cell(cell: str, *, error_cls: type[Exception] = ValueError) -> tuple[int, int]:
    """Парсит A1-ячейку в `(column_number, row_number)` с 1-based индексами."""

    match = re.fullmatch(r"^\$?([A-Za-z]+)\$?([1-9][0-9]*)$", cell.strip())
    if match is None:
        raise error_cls(f"Некорректная A1-ячейка: {cell}.")
    column, row_raw = match.groups()
    return column_to_number(column), int(row_raw)


def column_to_number(column: str) -> int:
    """Преобразует буквенное имя колонки в 1-based номер."""

    number = 0
    for char in column.upper():
        number = number * 26 + ord(char) - ord("A") + 1
    return number


def number_to_column(number: int, *, error_cls: type[Exception] = ValueError) -> str:
    """Преобразует 1-based номер колонки в буквенное имя."""

    if number <= 0:
        raise error_cls("Номер колонки должен быть положительным.")
    chars: list[str] = []
    while number > 0:
        number, remainder = divmod(number - 1, 26)
        chars.append(chr(ord("A") + remainder))
    return "".join(reversed(chars))


def offset_cell(
    start_cell: str,
    *,
    row_offset: int,
    column_offset: int,
    error_cls: type[Exception] = ValueError,
) -> str:
    """Сдвигает A1-ячейку на заданное количество строк и колонок."""

    start_column_number, start_row = parse_a1_cell(start_cell, error_cls=error_cls)
    return (
        f"{number_to_column(start_column_number + column_offset, error_cls=error_cls)}"
        f"{start_row + row_offset}"
    )


def grid_range_from_start_cell(
    *,
    sheet_id: int,
    start_cell: str,
    rows_count: int,
    columns_count: int,
    error_cls: type[Exception] = ValueError,
) -> JsonObject:
    """Строит Google Sheets GridRange по start cell и размеру."""

    start_column_number, start_row = parse_a1_cell(start_cell, error_cls=error_cls)
    return {
        "sheetId": sheet_id,
        "startRowIndex": start_row - 1,
        "endRowIndex": start_row - 1 + rows_count,
        "startColumnIndex": start_column_number - 1,
        "endColumnIndex": start_column_number - 1 + columns_count,
    }
