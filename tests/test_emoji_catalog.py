"""Тесты загрузчика Telegram custom emoji catalog."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clash_sheet_sync_bot.telegram.emoji_catalog import (
    EmojiCatalogError,
    load_telegram_emoji_catalog,
)

CATALOG_PATH = Path("resources/telegram_emoji_catalog.json")


def _catalog_data() -> dict[str, object]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def _write_catalog(tmp_path: Path, data: dict[str, object]) -> Path:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_real_catalog_has_all_town_halls_and_is_immutable() -> None:
    catalog = load_telegram_emoji_catalog(CATALOG_PATH)

    assert catalog.schema_version == 1
    assert {f"townhall_{level}" for level in range(1, 19)}.issubset(catalog.emojis)
    assert catalog.require("defense_shield").fallback == "🛡️"
    with pytest.raises(TypeError):
        catalog.emojis["other"] = catalog.require("clan_war")  # type: ignore[index]


@pytest.mark.parametrize("schema_version", (2, True, "1"))
def test_catalog_rejects_unknown_schema(tmp_path: Path, schema_version: object) -> None:
    data = _catalog_data()
    data["schema_version"] = schema_version

    with pytest.raises(EmojiCatalogError, match="schema_version"):
        load_telegram_emoji_catalog(_write_catalog(tmp_path, data))


def test_catalog_rejects_missing_required_key(tmp_path: Path) -> None:
    data = _catalog_data()
    emojis = data["emojis"]
    assert isinstance(emojis, dict)
    emojis.pop("townhall_18")

    with pytest.raises(EmojiCatalogError, match="townhall_18"):
        load_telegram_emoji_catalog(_write_catalog(tmp_path, data))


@pytest.mark.parametrize("bad_id", ("", "abc", "12 3", 123))
def test_catalog_rejects_invalid_custom_emoji_id(tmp_path: Path, bad_id: object) -> None:
    data = _catalog_data()
    emojis = data["emojis"]
    assert isinstance(emojis, dict)
    item = emojis["clan_war"]
    assert isinstance(item, dict)
    item["custom_emoji_id"] = bad_id

    with pytest.raises(EmojiCatalogError, match="custom_emoji_id"):
        load_telegram_emoji_catalog(_write_catalog(tmp_path, data))


def test_catalog_rejects_duplicate_custom_emoji_id(tmp_path: Path) -> None:
    data = _catalog_data()
    emojis = data["emojis"]
    assert isinstance(emojis, dict)
    first = emojis["clan_war"]
    second = emojis["defense_shield"]
    assert isinstance(first, dict) and isinstance(second, dict)
    second["custom_emoji_id"] = first["custom_emoji_id"]

    with pytest.raises(EmojiCatalogError, match="дублируется"):
        load_telegram_emoji_catalog(_write_catalog(tmp_path, data))


@pytest.mark.parametrize("field", ("fallback", "fallback_text", "description"))
def test_catalog_rejects_empty_text_fields(tmp_path: Path, field: str) -> None:
    data = _catalog_data()
    emojis = data["emojis"]
    assert isinstance(emojis, dict)
    item = emojis["clan_war"]
    assert isinstance(item, dict)
    item[field] = " "

    with pytest.raises(EmojiCatalogError, match=field):
        load_telegram_emoji_catalog(_write_catalog(tmp_path, data))
