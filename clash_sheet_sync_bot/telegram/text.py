"""Безопасная сборка Telegram-текста с UTF-16 entities."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .client import TelegramMessageEntity


class TelegramEntityError(ValueError):
    """Некорректные или повреждённые Telegram message entities."""


def utf16_length(value: str) -> int:
    """Возвращает длину строки в UTF-16 code units Telegram Bot API."""

    return len(value.encode("utf-16-le")) // 2


def parse_custom_emoji_entities(
    raw_entities: object,
    *,
    text: str,
) -> tuple[TelegramMessageEntity, ...]:
    """Извлекает и проверяет custom emoji entities входящего сообщения.

    Остальные типы entities намеренно не сохраняются: broadcast flow пока
    поддерживает только premium/custom emoji. Offsets проверяются в UTF-16,
    как требует Telegram Bot API.
    """

    if raw_entities is None:
        return ()
    if not isinstance(raw_entities, list):
        raise TelegramEntityError("Telegram entities должны быть списком.")

    text_length = utf16_length(text)
    text_boundaries = {0}
    boundary = 0
    for character in text:
        boundary += utf16_length(character)
        text_boundaries.add(boundary)
    entities: list[TelegramMessageEntity] = []
    for raw_entity in raw_entities:
        if not isinstance(raw_entity, dict):
            raise TelegramEntityError("Telegram entity должна быть объектом.")
        if raw_entity.get("type") != "custom_emoji":
            continue
        offset = raw_entity.get("offset")
        length = raw_entity.get("length")
        custom_emoji_id = raw_entity.get("custom_emoji_id")
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not isinstance(length, int)
            or isinstance(length, bool)
            or length <= 0
            or offset + length > text_length
            or offset not in text_boundaries
            or offset + length not in text_boundaries
        ):
            raise TelegramEntityError("Custom emoji entity выходит за границы текста.")
        if (
            not isinstance(custom_emoji_id, str)
            or not custom_emoji_id.isascii()
            or not custom_emoji_id.isdecimal()
        ):
            raise TelegramEntityError("Custom emoji entity содержит некорректный ID.")
        entities.append(
            TelegramMessageEntity(
                type="custom_emoji",
                offset=offset,
                length=length,
                custom_emoji_id=custom_emoji_id,
            )
        )

    entities.sort(key=lambda entity: (entity.offset, entity.length, entity.custom_emoji_id or ""))
    previous_end = 0
    for entity in entities:
        if entity.offset < previous_end:
            raise TelegramEntityError("Custom emoji entities пересекаются.")
        previous_end = entity.offset + entity.length
    return tuple(entities)


def encode_custom_emoji_entities(
    entities: tuple[TelegramMessageEntity, ...],
    *,
    text: str,
) -> str:
    """Проверяет и сериализует custom emoji entities для SQLite."""

    payload = [entity.to_payload() for entity in entities]
    validated = parse_custom_emoji_entities(payload, text=text)
    payload = [entity.to_payload() for entity in validated]
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def decode_custom_emoji_entities(
    raw_entities_json: str,
    *,
    text: str,
) -> tuple[TelegramMessageEntity, ...]:
    """Восстанавливает и повторно валидирует custom emoji entities из SQLite."""

    try:
        raw_entities = json.loads(raw_entities_json)
    except (TypeError, ValueError) as exc:
        raise TelegramEntityError("Broadcast entities содержат некорректный JSON.") from exc
    return parse_custom_emoji_entities(raw_entities, text=text)


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
