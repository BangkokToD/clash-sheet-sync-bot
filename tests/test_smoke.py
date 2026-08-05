"""Smoke-тест подключения pytest к проекту."""

from clash_sheet_sync_bot import __version__


def test_pytest_is_configured() -> None:
    """Проверяет, что тестовый контур проекта запускается."""

    assert True


def test_release_version() -> None:
    """Фиксирует публичную версию релиза."""

    assert __version__ == "1.1.0"
