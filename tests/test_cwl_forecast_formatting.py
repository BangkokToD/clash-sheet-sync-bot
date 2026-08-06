"""Exact tests forecast layout, fallback и UTF-16 entities."""

from __future__ import annotations

from pathlib import Path

from clash_sheet_sync_bot.cwl_forecast import ForecastColumn, build_forecast_message
from clash_sheet_sync_bot.telegram.emoji_catalog import load_telegram_emoji_catalog
from clash_sheet_sync_bot.telegram.text import utf16_length


def test_forecast_message_exact_layout_and_entities() -> None:
    catalog = load_telegram_emoji_catalog(Path("resources/telegram_emoji_catalog.json"))
    message = build_forecast_message(
        clan_name="Клан 🏆 | test",
        clan_tag="#abc123",
        own_roster=(18, 17, None),
        current_opponent_roster=(16, 15, 14),
        future_columns=(
            ForecastColumn(4, (13, None, 11)),
            ForecastColumn(5, (10, 9, 8)),
        ),
        catalog=catalog,
    )

    lines = message.text.splitlines()
    assert lines[0] == "Клан 🏆 | test | #ABC123"
    assert lines[1] == ""
    assert lines[2] == "🛡️  🔰  4️⃣  5️⃣"
    assert "|" not in lines[2]
    assert lines[3:-1] == [
        "🏠|🏠|🏠|🏠",
        "🏠|🏠|—|🏠",
        "—|🏠|🏠|🏠",
    ]
    assert lines[-1] == "35  45  24  27"
    assert all(" | " not in line for line in lines[3:-1])
    assert message.fallback_text.splitlines() == [
        "Клан 🏆 | test | #ABC123",
        "",
        "⭕️  ⚔️  4️⃣  5️⃣",
        "18|16|13|10",
        "17|15|—|9",
        "—|14|11|8",
        "35  45  24  27",
    ]
    assert len(message.entities) == 2 + 10
    first = message.entities[0]
    assert first.offset == utf16_length("Клан 🏆 | test | #ABC123\n\n")
    assert first.length == utf16_length("🛡️")
    for entity in message.entities:
        encoded = message.text.encode("utf-16-le")
        token = encoded[entity.offset * 2 : (entity.offset + entity.length) * 2].decode("utf-16-le")
        assert token in {"🛡️", "🔰", "🏠"}


def test_forecast_rejects_different_roster_heights() -> None:
    catalog = load_telegram_emoji_catalog(Path("resources/telegram_emoji_catalog.json"))
    try:
        build_forecast_message(
            clan_name="Clan",
            clan_tag="#TAG",
            own_roster=(18,),
            current_opponent_roster=(17, 16),
            future_columns=(),
            catalog=catalog,
        )
    except ValueError as exc:
        assert "одинаковую" in str(exc)
    else:
        raise AssertionError("Different roster heights were accepted")
