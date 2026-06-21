"""Загрузка и валидация конфигурации приложения."""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from models import AppConfig, ClanConfig, normalize_tag


class ConfigError(RuntimeError):
    """Ошибка конфигурации приложения."""


def load_config(env_file: str | Path = ".env") -> AppConfig:
    """Загружает конфигурацию из `.env` и окружения.

    Args:
        env_file: Путь к `.env`-файлу. Переменные окружения имеют приоритет.

    Returns:
        Проверенная конфигурация приложения.

    Raises:
        ConfigError: Если обязательная переменная отсутствует или некорректна.
    """

    env_path = Path(env_file)
    if env_path.exists():
        load_dotenv(env_path, override=False)
    else:
        load_dotenv(override=False)

    timezone = _required_env("TIMEZONE")
    _validate_timezone(timezone)

    return AppConfig(
        telegram_bot_token=_required_env("TELEGRAM_BOT_TOKEN"),
        telegram_allowed_user_ids=_parse_allowed_user_ids(
            os.getenv("TELEGRAM_ALLOWED_USER_IDS", ""),
        ),
        coc_api_token=_required_env("COC_API_TOKEN"),
        google_sheet_id=_required_env("GOOGLE_SHEET_ID"),
        google_service_account_file=Path(_required_env("GOOGLE_SERVICE_ACCOUNT_FILE")),
        composition_sheet_name=_required_env("COMPOSITION_SHEET_NAME"),
        cwl_sheet_name=_required_env("CWL_SHEET_NAME"),
        clans=(_load_clan(1), _load_clan(2), _load_clan(3)),
        composition_active_start_cell=_required_env("COMPOSITION_ACTIVE_START_CELL"),
        composition_exited_start_cell=_required_env("COMPOSITION_EXITED_START_CELL"),
        composition_managed_range=_required_env("COMPOSITION_MANAGED_RANGE"),
        cwl_start_cell=_required_env("CWL_START_CELL"),
        cwl_managed_range=_required_env("CWL_MANAGED_RANGE"),
        timezone=timezone,
    )


def _required_env(name: str) -> str:
    """Читает обязательную переменную окружения.

    Args:
        name: Имя переменной окружения.

    Returns:
        Непустое строковое значение без пробелов по краям.

    Raises:
        ConfigError: Если переменная не задана или пустая.
    """

    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise ConfigError(f"Не задана обязательная переменная окружения {name}.")
    return value.strip()


def _parse_allowed_user_ids(raw_value: str) -> frozenset[int]:
    """Парсит список разрешённых Telegram user ID.

    Пустое значение трактуется как пустой список разрешённых пользователей,
    а не как публичный доступ для всех.

    Args:
        raw_value: Значение `TELEGRAM_ALLOWED_USER_IDS` из окружения.

    Returns:
        Набор разрешённых Telegram user ID.

    Raises:
        ConfigError: Если один из ID не является положительным числом.
    """

    user_ids: set[int] = set()
    for item in raw_value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            user_id = int(item)
        except ValueError as exc:
            raise ConfigError(
                "TELEGRAM_ALLOWED_USER_IDS должен содержать числа через запятую.",
            ) from exc
        if user_id <= 0:
            raise ConfigError("Telegram user ID должен быть положительным числом.")
        user_ids.add(user_id)
    return frozenset(user_ids)


def _load_clan(index: int) -> ClanConfig:
    """Загружает конфигурацию одного клана по номеру.

    Args:
        index: Номер клана в `.env`.

    Returns:
        Конфигурация клана.

    Raises:
        ConfigError: Если тег клана некорректен.
    """

    raw_tag = _required_env(f"CLAN_{index}_TAG")
    try:
        tag = normalize_tag(raw_tag)
    except ValueError as exc:
        raise ConfigError(f"Некорректный CLAN_{index}_TAG: {exc}") from exc
    return ClanConfig(tag=tag, name=_required_env(f"CLAN_{index}_NAME"))


def _validate_timezone(timezone: str) -> None:
    """Проверяет IANA-таймзону.

    Args:
        timezone: Название таймзоны из `.env`.

    Raises:
        ConfigError: Если таймзона неизвестна Python.
    """

    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"Некорректная TIMEZONE: {timezone}.") from exc