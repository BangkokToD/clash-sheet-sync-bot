"""Доменные модели и общие типы проекта."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SyncResultStatus = Literal["success", "error"]


@dataclass(frozen=True, slots=True)
class ClanConfig:
    """Конфигурация одного семейного клана.

    Attributes:
        tag: Нормализованный тег клана Clash of Clans.
        name: Человекочитаемое название клана для отчётов и листов.
    """

    tag: str
    name: str


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Полная конфигурация приложения из переменных окружения.

    Attributes:
        telegram_bot_token: Токен Telegram Bot API.
        telegram_allowed_user_ids: Разрешённые Telegram user ID.
        coc_api_token: Токен Clash of Clans API.
        google_sheet_id: ID Google Sheets документа.
        google_service_account_file: Путь к JSON-файлу service account.
        composition_sheet_name: Название листа состава.
        cwl_sheet_name: Название листа CWL.
        clans: Три семейных клана в порядке из `.env`.
        composition_active_start_cell: Стартовая ячейка активных таблиц состава.
        composition_exited_start_cell: Стартовая ячейка таблицы вышедших.
        composition_managed_range: Управляемая область листа состава.
        cwl_start_cell: Стартовая ячейка листа CWL.
        cwl_managed_range: Управляемая область листа CWL.
        timezone: IANA-таймзона для дат в отчётах и листах.
        sync_settings_file: Путь к JSON-файлу статусов ручных запусков.
    """

    telegram_bot_token: str
    telegram_allowed_user_ids: frozenset[int]
    coc_api_token: str
    google_sheet_id: str
    google_service_account_file: Path
    composition_sheet_name: str
    cwl_sheet_name: str
    clans: tuple[ClanConfig, ClanConfig, ClanConfig]
    composition_active_start_cell: str
    composition_exited_start_cell: str
    composition_managed_range: str
    cwl_start_cell: str
    cwl_managed_range: str
    timezone: str
    sync_settings_file: Path = Path("sync_settings.json")


@dataclass(slots=True)
class SyncSettings:
    """Состояние последних ручных синхронизаций.

    Attributes:
        last_composition_sync_at: ISO-дата последнего запуска состава.
        last_composition_sync_status: Статус последнего запуска состава.
        last_composition_sync_error: Текст последней ошибки состава.
        last_cwl_sync_at: ISO-дата последнего запуска CWL.
        last_cwl_sync_status: Статус последнего запуска CWL.
        last_cwl_sync_error: Текст последней ошибки CWL.
    """

    last_composition_sync_at: str | None = None
    last_composition_sync_status: SyncResultStatus | None = None
    last_composition_sync_error: str | None = None
    last_cwl_sync_at: str | None = None
    last_cwl_sync_status: SyncResultStatus | None = None
    last_cwl_sync_error: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> SyncSettings:
        """Создаёт настройки из JSON-словаря.

        Args:
            data: Словарь, прочитанный из `sync_settings.json`.

        Returns:
            Нормализованный объект настроек синхронизации.
        """

        return cls(
            last_composition_sync_at=_read_optional_str(data, "last_composition_sync_at"),
            last_composition_sync_status=_read_optional_status(
                data,
                "last_composition_sync_status",
            ),
            last_composition_sync_error=_read_optional_str(
                data,
                "last_composition_sync_error",
            ),
            last_cwl_sync_at=_read_optional_str(data, "last_cwl_sync_at"),
            last_cwl_sync_status=_read_optional_status(data, "last_cwl_sync_status"),
            last_cwl_sync_error=_read_optional_str(data, "last_cwl_sync_error"),
        )

    def to_dict(self) -> dict[str, str | None]:
        """Преобразует настройки в JSON-совместимый словарь.

        Returns:
            Словарь с полями `sync_settings.json`.
        """

        return {
            "last_composition_sync_at": self.last_composition_sync_at,
            "last_composition_sync_status": self.last_composition_sync_status,
            "last_composition_sync_error": self.last_composition_sync_error,
            "last_cwl_sync_at": self.last_cwl_sync_at,
            "last_cwl_sync_status": self.last_cwl_sync_status,
            "last_cwl_sync_error": self.last_cwl_sync_error,
        }


def normalize_tag(value: str) -> str:
    """Нормализует тег Clash of Clans.

    Args:
        value: Исходный тег из `.env`, API или Google Sheets.

    Returns:
        Тег без пробелов по краям и в верхнем регистре.

    Raises:
        ValueError: Если тег пустой или не начинается с `#`.
    """

    normalized = value.strip().upper()
    if not normalized:
        raise ValueError("Тег не может быть пустым.")
    if not normalized.startswith("#"):
        raise ValueError(f"Тег должен начинаться с '#': {normalized}")
    return normalized


def _read_optional_str(data: dict[str, object], key: str) -> str | None:
    """Читает необязательную строку из JSON-словаря.

    Args:
        data: Словарь с данными.
        key: Имя поля.

    Returns:
        Строка или `None`, если поле пустое/отсутствует.
    """

    value = data.get(key)
    return value if isinstance(value, str) else None


def _read_optional_status(data: dict[str, object], key: str) -> SyncResultStatus | None:
    """Читает статус синхронизации из JSON-словаря.

    Args:
        data: Словарь с данными.
        key: Имя поля.

    Returns:
        `success`, `error` или `None`.
    """

    value = data.get(key)
    if value == "success" or value == "error":
        return value
    return None