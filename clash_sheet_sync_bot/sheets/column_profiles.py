"""Дефолтные профили колонок и операции нормализации."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Final

from clash_sheet_sync_bot.models import ColumnKind, ColumnValueType, TableType

BOT_KEY_COLUMN_KEY: Final = "bot_key"
BOT_KEY_TITLE: Final = "__bot_key"
USER_COLUMN_KEY_PREFIX: Final = "user_"
MAX_COLUMN_TITLE_LENGTH: Final = 80
USER_COLUMN_KEY_RE: Final = re.compile(r"[^a-zA-Z0-9_]+")

TABLE_TITLES: Final[dict[TableType, str]] = {
    "composition_active": "Колонки активного состава",
    "composition_exited": "Колонки вышедших",
    "cwl": "Колонки CWL",
}


@dataclass(frozen=True, slots=True)
class ColumnDefinition:
    """Описание дефолтной колонки профиля.

    Attributes:
        table_type: Тип управляемой таблицы.
        column_key: Стабильный внутренний ключ колонки.
        title: Заголовок в Google Sheets.
        visible: Нужно ли выводить колонку в Google Sheets.
        kind: Тип колонки: system, user или service.
        value_type: Тип значения.
        sort_order: Порядок колонки внутри профиля.
    """

    table_type: TableType
    column_key: str
    title: str
    visible: bool
    kind: ColumnKind
    value_type: ColumnValueType
    sort_order: int


DEFAULT_COLUMN_DEFINITIONS: Final[tuple[ColumnDefinition, ...]] = (
    ColumnDefinition(
        "composition_active", BOT_KEY_COLUMN_KEY, BOT_KEY_TITLE, False, "service", "string", 0
    ),
    ColumnDefinition("composition_active", "number", "№", True, "system", "integer", 10),
    ColumnDefinition("composition_active", "tag", "Тег", True, "system", "string", 20),
    ColumnDefinition("composition_active", "town_hall", "Ратуша", True, "system", "integer", 30),
    ColumnDefinition("composition_active", "nickname", "Никнейм", True, "system", "string", 40),
    ColumnDefinition(
        "composition_exited", BOT_KEY_COLUMN_KEY, BOT_KEY_TITLE, False, "service", "string", 0
    ),
    ColumnDefinition("composition_exited", "number", "№", True, "system", "integer", 10),
    ColumnDefinition("composition_exited", "tag", "Тег", True, "system", "string", 20),
    ColumnDefinition("composition_exited", "town_hall", "Ратуша", True, "system", "integer", 30),
    ColumnDefinition("composition_exited", "nickname", "Никнейм", True, "system", "string", 40),
    ColumnDefinition(
        "composition_exited", "exited_at", "Дата выхода", True, "system", "datetime", 50
    ),
    ColumnDefinition("cwl", BOT_KEY_COLUMN_KEY, BOT_KEY_TITLE, False, "service", "string", 0),
    ColumnDefinition("cwl", "round", "Раунд", True, "system", "integer", 10),
    ColumnDefinition("cwl", "attacker_tag", "Тег", True, "system", "string", 20),
    ColumnDefinition("cwl", "attacker_name", "Ник", True, "system", "string", 30),
    ColumnDefinition("cwl", "attacker_town_hall", "ТХ", True, "system", "string", 40),
    ColumnDefinition("cwl", "defender_town_hall", "ТХ соперника", True, "system", "string", 50),
    ColumnDefinition("cwl", "stars", "Звезды", True, "system", "integer", 60),
    ColumnDefinition(
        "cwl", "destruction_percentage", "Процент разрушений", True, "system", "integer", 70
    ),
)


def default_columns(table_type: TableType) -> tuple[ColumnDefinition, ...]:
    """Возвращает дефолтные колонки указанного профиля.

    Args:
        table_type: Тип управляемой таблицы.

    Returns:
        Кортеж дефолтных колонок.
    """

    return tuple(item for item in DEFAULT_COLUMN_DEFINITIONS if item.table_type == table_type)


def all_default_columns() -> tuple[ColumnDefinition, ...]:
    """Возвращает все дефолтные колонки всех профилей.

    Returns:
        Кортеж дефолтных колонок.
    """

    return DEFAULT_COLUMN_DEFINITIONS


def table_title(table_type: TableType) -> str:
    """Возвращает человекочитаемое название профиля.

    Args:
        table_type: Тип управляемой таблицы.

    Returns:
        Название профиля для Telegram UI.
    """

    return TABLE_TITLES[table_type]


def normalize_column_title(value: str) -> str:
    """Нормализует пользовательский заголовок колонки.

    Args:
        value: Исходный заголовок.

    Returns:
        Заголовок без пробелов по краям.

    Raises:
        ValueError: Если заголовок пустой или слишком длинный.
    """

    title = value.strip()
    if title == "":
        raise ValueError("Название колонки не может быть пустым.")
    if len(title) > MAX_COLUMN_TITLE_LENGTH:
        raise ValueError(
            f"Название колонки не должно быть длиннее {MAX_COLUMN_TITLE_LENGTH} символов."
        )
    return title


def column_title_identity(value: str) -> str:
    """Возвращает identity названия колонки для поиска дублей и связей."""

    return normalize_column_title(value).casefold()


def new_user_column_key() -> str:
    """Создаёт стабильный ключ новой пользовательской колонки.

    Returns:
        Ключ вида `user_<token>`.
    """

    token = USER_COLUMN_KEY_RE.sub("_", secrets.token_urlsafe(8)).strip("_").lower()
    return f"{USER_COLUMN_KEY_PREFIX}{token}"
