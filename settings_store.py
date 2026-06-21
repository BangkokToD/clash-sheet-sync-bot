"""Хранилище статусов ручных синхронизаций."""

from __future__ import annotations

import json
import os
from pathlib import Path

from models import SyncSettings


class SettingsStoreError(RuntimeError):
    """Ошибка чтения или записи `sync_settings.json`."""


class SettingsStore:
    """Файловое хранилище статусов синхронизаций.

    Args:
        path: Путь к JSON-файлу статусов.
    """

    def __init__(self, path: str | Path = "sync_settings.json") -> None:
        self.path = Path(path)

    def load(self) -> SyncSettings:
        """Читает текущие статусы синхронизаций.

        Returns:
            Настройки по умолчанию, если файл ещё не создан, иначе данные файла.

        Raises:
            SettingsStoreError: Если файл существует, но не читается как JSON-объект.
        """

        if not self.path.exists():
            return SyncSettings()

        try:
            raw_data = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SettingsStoreError(f"Не удалось прочитать {self.path}.") from exc
        except json.JSONDecodeError as exc:
            raise SettingsStoreError(f"Файл {self.path} содержит битый JSON.") from exc

        if not isinstance(raw_data, dict):
            raise SettingsStoreError(f"Файл {self.path} должен содержать JSON-объект.")

        return SyncSettings.from_dict(raw_data)

    def save(self, settings: SyncSettings) -> None:
        """Атомарно сохраняет статусы синхронизаций.

        Args:
            settings: Новое состояние `sync_settings.json`.

        Raises:
            SettingsStoreError: Если файл не удалось записать или заменить.
        """

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(f".{self.path.name}.tmp")
        payload = json.dumps(settings.to_dict(), ensure_ascii=False, indent=2)

        try:
            temp_path.write_text(f"{payload}\n", encoding="utf-8")
            os.replace(temp_path, self.path)
        except OSError as exc:
            raise SettingsStoreError(f"Не удалось записать {self.path}.") from exc
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)