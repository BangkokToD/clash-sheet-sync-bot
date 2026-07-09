"""Общие helpers repository-слоя."""

from __future__ import annotations

import json
from typing import Any, cast

import aiosqlite

from clash_sheet_sync_bot.models import (
    ColumnKind,
    ColumnValueType,
    CompositionPlayerStatus,
    TableType,
    TelegramChatStatus,
)


class RepositoryError(RuntimeError):
    """Ошибка repository-слоя."""


async def fetch_one(
    connection: aiosqlite.Connection,
    sql: str,
    parameters: tuple[object, ...] = (),
) -> aiosqlite.Row | None:
    """Выполняет SELECT и возвращает одну строку."""

    cursor = await connection.execute(sql, parameters)
    return await cursor.fetchone()


async def fetch_all(
    connection: aiosqlite.Connection,
    sql: str,
    parameters: tuple[object, ...] = (),
) -> tuple[aiosqlite.Row, ...]:
    """Выполняет SELECT и возвращает все строки."""

    cursor = await connection.execute(sql, parameters)
    rows = await cursor.fetchall()
    return tuple(rows)


def as_str(value: Any, field_name: str) -> str:
    """Проверяет строковое значение из SQLite."""

    if not isinstance(value, str):
        raise RepositoryError(f"Поле {field_name} должно быть строкой.")
    return value


def as_optional_str(value: Any, field_name: str) -> str | None:
    """Проверяет nullable-строку из SQLite."""

    if value is None:
        return None
    return as_str(value, field_name)


def as_int(value: Any, field_name: str) -> int:
    """Проверяет целое значение из SQLite."""

    if not isinstance(value, int) or isinstance(value, bool):
        raise RepositoryError(f"Поле {field_name} должно быть числом.")
    return value


def as_optional_int(value: Any, field_name: str) -> int | None:
    """Проверяет nullable-число из SQLite."""

    if value is None:
        return None
    return as_int(value, field_name)


def as_bool_int(value: Any, field_name: str) -> bool:
    """Проверяет SQLite boolean, сохранённый как 0/1."""

    if value == 0:
        return False
    if value == 1:
        return True
    raise RepositoryError(f"Поле {field_name} должно быть 0 или 1.")


def as_chat_status(value: Any) -> TelegramChatStatus:
    """Проверяет статус Telegram-чата."""

    raw = as_str(value, "status")
    allowed = {
        "not_configured",
        "waiting_for_sheet",
        "waiting_for_access",
        "waiting_for_clans",
        "ready",
        "disabled",
    }
    if raw not in allowed:
        raise RepositoryError(f"Некорректный status чата: {raw}.")
    return cast(TelegramChatStatus, raw)


def as_table_type(value: Any) -> TableType:
    """Проверяет тип таблицы профиля колонок."""

    raw = as_str(value, "table_type")
    if raw not in {"composition", "composition_active", "composition_exited", "cwl"}:
        raise RepositoryError(f"Некорректный table_type: {raw}.")
    return cast(TableType, raw)


def as_column_kind(value: Any) -> ColumnKind:
    """Проверяет kind профиля колонки."""

    raw = as_str(value, "kind")
    if raw not in {"system", "user", "service"}:
        raise RepositoryError(f"Некорректный column kind: {raw}.")
    return cast(ColumnKind, raw)


def as_column_value_type(value: Any) -> ColumnValueType:
    """Проверяет value_type профиля колонки."""

    raw = as_str(value, "value_type")
    if raw not in {"string", "integer", "datetime"}:
        raise RepositoryError(f"Некорректный column value_type: {raw}.")
    return cast(ColumnValueType, raw)


def as_composition_player_status(value: Any) -> CompositionPlayerStatus:
    """Проверяет статус игрока состава."""

    raw = as_str(value, "status")
    if raw not in {"active", "exited", "untracked"}:
        raise RepositoryError(f"Некорректный status игрока состава: {raw}.")
    return cast(CompositionPlayerStatus, raw)


def as_json_dict(value: Any, field_name: str) -> dict[str, object]:
    """Парсит JSON-объект из SQLite.

    Args:
        value: Значение SQLite.
        field_name: Имя поля для текста ошибки.

    Returns:
        JSON-словарь.
    """

    raw_json = as_str(value, field_name)
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise RepositoryError(f"{field_name} содержит битый JSON.") from exc
    if not isinstance(data, dict):
        raise RepositoryError(f"{field_name} должен быть JSON-объектом.")
    return dict(data)


def as_user_values(value: Any) -> dict[str, str]:
    """Парсит JSON пользовательских значений."""

    if value is None:
        return {}
    raw_json = as_str(value, "user_values_json")
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise RepositoryError("user_values_json содержит битый JSON.") from exc
    if not isinstance(data, dict):
        raise RepositoryError("user_values_json должен быть JSON-объектом.")
    result: dict[str, str] = {}
    for key, raw_value in data.items():
        if isinstance(key, str) and isinstance(raw_value, str):
            result[key] = raw_value
    return result
