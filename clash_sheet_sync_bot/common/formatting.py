"""Общие helpers отображения игровых значений."""

from __future__ import annotations


def format_town_hall(town_hall: int) -> str:
    """Форматирует уровень ратуши единообразно для состава и CWL."""

    return f"TH{town_hall}"
