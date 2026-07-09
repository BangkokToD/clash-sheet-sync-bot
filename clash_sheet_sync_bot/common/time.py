"""Общие helpers для UTC-времени."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Возвращает текущую UTC-дату без microseconds."""

    return datetime.now(UTC).replace(microsecond=0)


def utc_now_iso() -> str:
    """Возвращает текущую UTC-дату в ISO-формате."""

    return utc_now().isoformat()


def format_dt(value: datetime) -> str:
    """Форматирует datetime для SQLite/runtime-state."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0).isoformat()
