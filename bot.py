"""Точка входа Telegram-бота.

На первом коммите модуль только проверяет, что проект запускается и
конфигурация читается. Telegram long polling добавляется отдельным коммитом.
"""

from __future__ import annotations

import logging

from config import ConfigError, load_config
from settings_store import SettingsStore, SettingsStoreError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> int:
    """Запускает минимальную проверку приложения.

    Returns:
        Код завершения процесса.
    """

    try:
        config = load_config()
        SettingsStore(config.sync_settings_file).load()
    except (ConfigError, SettingsStoreError) as exc:
        logger.error("bot startup failed: %s", exc)
        return 1

    logger.info("bot started")
    logger.info(
        "config loaded: clans=%s, composition_sheet=%s, cwl_sheet=%s",
        len(config.clans),
        config.composition_sheet_name,
        config.cwl_sheet_name,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
