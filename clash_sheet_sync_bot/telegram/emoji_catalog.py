"""Загрузка и строгая валидация каталога Telegram custom emoji."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


class EmojiCatalogError(RuntimeError):
    """Каталог custom emoji отсутствует или нарушает schema contract."""


@dataclass(frozen=True, slots=True)
class EmojiCatalogEntry:
    """Одна immutable запись custom emoji и двух fallback-представлений."""

    custom_emoji_id: str
    fallback: str
    fallback_text: str
    description: str


@dataclass(frozen=True, slots=True)
class TelegramEmojiCatalog:
    """Проверенный каталог Telegram emoji schema version 1."""

    schema_version: int
    emojis: Mapping[str, EmojiCatalogEntry]

    def require(self, key: str) -> EmojiCatalogEntry:
        """Возвращает обязательную запись или сообщает имя ключа."""

        try:
            return self.emojis[key]
        except KeyError as exc:
            raise EmojiCatalogError(f"В каталоге отсутствует emojis.{key}.") from exc


def load_telegram_emoji_catalog(path: str | Path) -> TelegramEmojiCatalog:
    """Читает UTF-8 JSON catalog и валидирует полный набор ключей."""

    catalog_path = Path(path)
    try:
        raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EmojiCatalogError(f"Не удалось прочитать каталог emoji: {catalog_path}.") from exc
    except json.JSONDecodeError as exc:
        raise EmojiCatalogError(f"Каталог emoji содержит битый JSON: {catalog_path}.") from exc
    if not isinstance(raw, dict):
        raise EmojiCatalogError(f"Корень каталога {catalog_path} должен быть объектом.")
    schema_version = raw.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
    ):
        raise EmojiCatalogError(f"{catalog_path}: schema_version должен быть равен 1.")
    raw_emojis = raw.get("emojis")
    if not isinstance(raw_emojis, dict):
        raise EmojiCatalogError(f"{catalog_path}: emojis должен быть объектом.")

    required_keys = {"clan_war", "defense_shield"} | {f"townhall_{level}" for level in range(1, 19)}
    missing = sorted(required_keys - raw_emojis.keys())
    if missing:
        raise EmojiCatalogError(f"{catalog_path}: отсутствует emojis.{missing[0]}.")

    entries: dict[str, EmojiCatalogEntry] = {}
    used_ids: set[str] = set()
    for key, value in raw_emojis.items():
        field_path = f"{catalog_path}: emojis.{key}"
        if not isinstance(key, str) or not isinstance(value, dict):
            raise EmojiCatalogError(f"{field_path} должен быть объектом.")
        custom_emoji_id = _nonempty_string(value, "custom_emoji_id", field_path)
        if not custom_emoji_id.isascii() or not custom_emoji_id.isdecimal():
            raise EmojiCatalogError(f"{field_path}.custom_emoji_id должен содержать только цифры.")
        if custom_emoji_id in used_ids:
            raise EmojiCatalogError(f"{field_path}.custom_emoji_id дублируется.")
        used_ids.add(custom_emoji_id)
        entries[key] = EmojiCatalogEntry(
            custom_emoji_id=custom_emoji_id,
            fallback=_nonempty_string(value, "fallback", field_path),
            fallback_text=_nonempty_string(value, "fallback_text", field_path),
            description=_nonempty_string(value, "description", field_path),
        )
    return TelegramEmojiCatalog(
        schema_version=1,
        emojis=MappingProxyType(entries),
    )


def _nonempty_string(value: dict[object, object], key: str, field_path: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise EmojiCatalogError(f"{field_path}.{key} должен быть непустой строкой.")
    return raw
