"""Загрузка и валидация глобальной конфигурации приложения."""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from clash_sheet_sync_bot.models import AppConfig


class ConfigError(RuntimeError):
    """Ошибка конфигурации приложения."""


def load_config(env_file: str | Path = ".env") -> AppConfig:
    """Загружает глобальную конфигурацию из `.env` и окружения.

    Runtime-настройки конкретных Telegram-групп не читаются из `.env`.
    Они должны храниться в SQLite и загружаться через repository-слой.

    Args:
        env_file: Путь к `.env`-файлу. Переменные окружения имеют приоритет.

    Returns:
        Проверенная глобальная конфигурация приложения.

    Raises:
        ConfigError: Если обязательная переменная отсутствует или некорректна.
    """

    env_path = Path(env_file)
    if env_path.exists():
        load_dotenv(env_path, override=False)
    else:
        load_dotenv(override=False)

    default_timezone = _optional_env("DEFAULT_TIMEZONE", "Europe/Kyiv")
    _validate_timezone(default_timezone)

    return AppConfig(
        telegram_bot_token=_required_env("TELEGRAM_BOT_TOKEN"),
        coc_api_token=_required_env("COC_API_TOKEN"),
        google_service_account_file=Path(_required_env("GOOGLE_SERVICE_ACCOUNT_FILE")),
        google_service_account_email=_optional_nullable_env("GOOGLE_SERVICE_ACCOUNT_EMAIL"),
        db_path=Path(_optional_env("DB_PATH", "bot.db")),
        default_timezone=default_timezone,
        max_clans_per_chat=_positive_int_env("MAX_CLANS_PER_CHAT", 20),
        sync_cooldown_seconds=_non_negative_int_env("SYNC_COOLDOWN_SECONDS", 60),
        max_concurrent_syncs=_positive_int_env("MAX_CONCURRENT_SYNCS", 3),
        cwl_war_concurrency_limit=_positive_int_env("CWL_WAR_CONCURRENCY_LIMIT", 5),
        admin_cache_ttl_seconds=_non_negative_int_env("ADMIN_CACHE_TTL_SECONDS", 300),
        setup_token_ttl_seconds=_positive_int_env("SETUP_TOKEN_TTL_SECONDS", 900),
        transfer_token_ttl_seconds=_positive_int_env("TRANSFER_TOKEN_TTL_SECONDS", 900),
        report_max_items=_positive_int_env("REPORT_MAX_ITEMS", 50),
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


def _optional_env(name: str, default: str) -> str:
    """Читает необязательную строковую переменную окружения.

    Args:
        name: Имя переменной окружения.
        default: Значение по умолчанию.

    Returns:
        Непустое значение переменной или `default`.
    """

    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _optional_nullable_env(name: str) -> str | None:
    """Читает необязательную переменную окружения как строку или `None`.

    Args:
        name: Имя переменной окружения.

    Returns:
        Непустое значение переменной или `None`.
    """

    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    return value.strip()


def _positive_int_env(name: str, default: int) -> int:
    """Читает положительную целочисленную переменную окружения.

    Args:
        name: Имя переменной окружения.
        default: Значение по умолчанию.

    Returns:
        Положительное целое число.

    Raises:
        ConfigError: Если значение не является положительным числом.
    """

    value = _int_env(name, default)
    if value <= 0:
        raise ConfigError(f"{name} должен быть положительным числом.")
    return value


def _non_negative_int_env(name: str, default: int) -> int:
    """Читает неотрицательную целочисленную переменную окружения.

    Args:
        name: Имя переменной окружения.
        default: Значение по умолчанию.

    Returns:
        Неотрицательное целое число.

    Raises:
        ConfigError: Если значение отрицательное или не является числом.
    """

    value = _int_env(name, default)
    if value < 0:
        raise ConfigError(f"{name} должен быть неотрицательным числом.")
    return value


def _int_env(name: str, default: int) -> int:
    """Читает целочисленную переменную окружения.

    Args:
        name: Имя переменной окружения.
        default: Значение по умолчанию.

    Returns:
        Целое число.

    Raises:
        ConfigError: Если значение не является числом.
    """

    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default

    try:
        return int(raw_value.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} должен быть целым числом.") from exc


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
        raise ConfigError(f"Некорректная DEFAULT_TIMEZONE: {timezone}.") from exc
