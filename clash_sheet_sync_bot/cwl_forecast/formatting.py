"""Форматирование forecast matrix и custom emoji entities."""

from __future__ import annotations

from clash_sheet_sync_bot.models import normalize_tag
from clash_sheet_sync_bot.telegram.emoji_catalog import TelegramEmojiCatalog
from clash_sheet_sync_bot.telegram.text import TelegramTextBuilder

from .models import ForecastColumn, ForecastMessage


def build_forecast_message(
    *,
    clan_name: str,
    clan_tag: str,
    own_roster: tuple[int | None, ...],
    current_opponent_roster: tuple[int | None, ...],
    future_columns: tuple[ForecastColumn, ...],
    catalog: TelegramEmojiCatalog,
) -> ForecastMessage:
    """Строит custom и plain варианты одной матрицы без parse mode."""

    row_count = len(own_roster)
    rosters = (own_roster, current_opponent_roster, *(column.roster for column in future_columns))
    if row_count == 0 or any(len(roster) != row_count for roster in rosters):
        raise ValueError("Все roster forecast matrix должны иметь одинаковую ненулевую высоту.")
    custom = TelegramTextBuilder()
    plain_parts: list[str] = []
    title = f"{clan_name} | {normalize_tag(clan_tag)}\n"
    custom.append(title)
    plain_parts.append(title)
    header_keys = ("defense_shield", "clan_war")
    for index, key in enumerate(header_keys):
        if index:
            custom.append("  ")
            plain_parts.append("  ")
        entry = catalog.require(key)
        custom.append_custom_emoji(entry.fallback, entry.custom_emoji_id)
        plain_parts.append(entry.fallback_text)
    for column in future_columns:
        label = f"  {_keycap(column.round_number)}"
        custom.append(label)
        plain_parts.append(label)
    custom.append("\n")
    plain_parts.append("\n")

    for row_index in range(row_count):
        for column_index, roster in enumerate(rosters):
            if column_index:
                custom.append("|")
                plain_parts.append("|")
            town_hall = roster[row_index]
            if town_hall is None:
                custom.append("—")
                plain_parts.append("—")
            else:
                entry = catalog.require(f"townhall_{town_hall}")
                custom.append_custom_emoji(entry.fallback, entry.custom_emoji_id)
                plain_parts.append(entry.fallback_text)
        if row_index + 1 < row_count:
            custom.append("\n")
            plain_parts.append("\n")
    custom.append("\n")
    plain_parts.append("\n")
    totals = "  ".join(
        str(sum(value for value in roster if value is not None)) for roster in rosters
    )
    custom.append(totals)
    plain_parts.append(totals)
    text, entities = custom.build()
    return ForecastMessage(text=text, entities=entities, fallback_text="".join(plain_parts))


def _keycap(number: int) -> str:
    if not 1 <= number <= 9:
        raise ValueError("Номер CWL-раунда для keycap должен быть от 1 до 9.")
    return f"{number}\ufe0f\u20e3"
