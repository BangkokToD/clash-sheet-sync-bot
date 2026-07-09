"""Unit-тесты загрузки глобальной конфигурации."""

from __future__ import annotations

from pathlib import Path

import pytest

from clash_sheet_sync_bot.config import ConfigError, load_config

CONFIG_ENV_NAMES = (
    "TELEGRAM_BOT_TOKEN",
    "COC_API_TOKEN",
    "GOOGLE_SERVICE_ACCOUNT_FILE",
    "GOOGLE_SERVICE_ACCOUNT_EMAIL",
    "DB_PATH",
    "DEFAULT_TIMEZONE",
    "MAX_CLANS_PER_CHAT",
    "SYNC_COOLDOWN_SECONDS",
    "MAX_CONCURRENT_SYNCS",
    "CWL_WAR_CONCURRENCY_LIMIT",
    "ADMIN_CACHE_TTL_SECONDS",
    "SETUP_TOKEN_TTL_SECONDS",
    "TRANSFER_TOKEN_TTL_SECONDS",
    "REPORT_MAX_ITEMS",
)


def _empty_env_file(tmp_path: Path) -> Path:
    """Создаёт пустой .env, чтобы тесты не читали локальный проектный .env."""

    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    return env_file


def _clear_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Удаляет env-переменные, влияющие на load_config."""

    for name in CONFIG_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Выставляет минимальные обязательные env-переменные."""

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token")
    monkeypatch.setenv("COC_API_TOKEN", "coc-token")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials.json")


def test_load_config_requires_telegram_bot_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Проверяет обязательность TELEGRAM_BOT_TOKEN."""

    _clear_config_env(monkeypatch)
    monkeypatch.setenv("COC_API_TOKEN", "coc-token")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials.json")

    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        load_config(_empty_env_file(tmp_path))


def test_load_config_uses_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Проверяет дефолтные значения необязательных настроек."""

    _clear_config_env(monkeypatch)
    _set_required_env(monkeypatch)

    config = load_config(_empty_env_file(tmp_path))

    assert config.telegram_bot_token == "telegram-token"
    assert config.coc_api_token == "coc-token"
    assert config.google_service_account_file == Path("credentials.json")
    assert config.google_service_account_email is None
    assert config.db_path == Path("bot.db")
    assert config.default_timezone == "Europe/Kyiv"
    assert config.max_clans_per_chat == 20
    assert config.sync_cooldown_seconds == 60
    assert config.max_concurrent_syncs == 3
    assert config.cwl_war_concurrency_limit == 5
    assert config.admin_cache_ttl_seconds == 300
    assert config.setup_token_ttl_seconds == 900
    assert config.transfer_token_ttl_seconds == 900
    assert config.report_max_items == 50


@pytest.mark.parametrize(
    ("env_name", "env_value", "message"),
    (
        ("MAX_CLANS_PER_CHAT", "not-int", "целым числом"),
        ("MAX_CONCURRENT_SYNCS", "0", "положительным числом"),
        ("SYNC_COOLDOWN_SECONDS", "-1", "неотрицательным числом"),
    ),
)
def test_load_config_rejects_invalid_int_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    env_name: str,
    env_value: str,
    message: str,
) -> None:
    """Проверяет ошибки невалидных числовых env-переменных."""

    _clear_config_env(monkeypatch)
    _set_required_env(monkeypatch)
    monkeypatch.setenv(env_name, env_value)

    with pytest.raises(ConfigError, match=message):
        load_config(_empty_env_file(tmp_path))


def test_load_config_rejects_invalid_timezone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Проверяет валидацию DEFAULT_TIMEZONE."""

    _clear_config_env(monkeypatch)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("DEFAULT_TIMEZONE", "Invalid/Timezone")

    with pytest.raises(ConfigError, match="DEFAULT_TIMEZONE"):
        load_config(_empty_env_file(tmp_path))


@pytest.mark.parametrize(
    ("raw_email", "expected_email"),
    (
        (None, None),
        ("", None),
        ("bot@example.iam.gserviceaccount.com", "bot@example.iam.gserviceaccount.com"),
    ),
)
def test_load_config_reads_optional_google_service_account_email(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    raw_email: str | None,
    expected_email: str | None,
) -> None:
    """Проверяет GOOGLE_SERVICE_ACCOUNT_EMAIL как None или строку."""

    _clear_config_env(monkeypatch)
    _set_required_env(monkeypatch)
    if raw_email is not None:
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_EMAIL", raw_email)

    config = load_config(_empty_env_file(tmp_path))

    assert config.google_service_account_email == expected_email
