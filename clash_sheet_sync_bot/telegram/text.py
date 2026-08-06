"""Безопасная сборка Telegram-текста с UTF-16 entities."""

from __future__ import annotations

from dataclasses import dataclass, field

from .client import TelegramMessageEntity


def utf16_length(value: str) -> int:
    """Возвращает длину строки в UTF-16 code units Telegram Bot API."""

    return len(value.encode("utf-16-le")) // 2


@dataclass(slots=True)
class TelegramTextBuilder:
    """Одновременно строит текст и точные custom emoji entities."""

    _parts: list[str] = field(default_factory=list)
    _entities: list[TelegramMessageEntity] = field(default_factory=list)
    _offset: int = 0

    def append(self, value: str) -> None:
        """Добавляет обычный текст."""

        self._parts.append(value)
        self._offset += utf16_length(value)

    def append_custom_emoji(self, fallback: str, custom_emoji_id: str) -> None:
        """Добавляет fallback token и entity ровно поверх него."""

        length = utf16_length(fallback)
        self._parts.append(fallback)
        self._entities.append(
            TelegramMessageEntity(
                type="custom_emoji",
                offset=self._offset,
                length=length,
                custom_emoji_id=custom_emoji_id,
            )
        )
        self._offset += length

    def build(self) -> tuple[str, tuple[TelegramMessageEntity, ...]]:
        """Возвращает согласованные immutable text/entities."""

        return "".join(self._parts), tuple(self._entities)
