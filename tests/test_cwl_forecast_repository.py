"""Тесты SQLite persistence прогноза ЛВК."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from clash_sheet_sync_bot.repositories import (
    CwlForecastRepository,
    CwlForecastRound,
    CwlForecastScheduleKey,
    CwlForecastSession,
    CwlForecastSessionConflictError,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
KEY = CwlForecastScheduleKey("2026-08", "a" * 64, "#AAA111")


async def _insert_chat(connection: aiosqlite.Connection, chat_id: int = -1001) -> None:
    await connection.execute(
        """INSERT INTO telegram_chats(
            chat_id, title, type, status, created_at, updated_at
        ) VALUES (?, 'Group', 'supergroup', 'ready', ?, ?)""",
        (chat_id, NOW.isoformat(), NOW.isoformat()),
    )
    await connection.commit()


def _session(*, session_id: str, created_at: datetime, expires_at: datetime) -> CwlForecastSession:
    return CwlForecastSession(
        id=session_id,
        key=KEY,
        source_chat_id=-1001,
        created_by_user_id=42,
        message_id=None,
        draft={"rounds": []},
        current_step=0,
        expires_at=expires_at,
        created_at=created_at,
        updated_at=created_at,
    )


@pytest.mark.asyncio
async def test_forecast_cooldown_round_trips_timezone_aware(
    migrated_connection: aiosqlite.Connection,
) -> None:
    await _insert_chat(migrated_connection)
    repository = CwlForecastRepository(migrated_connection)

    assert await repository.get_last_started_at(-1001) is None
    await repository.set_last_started_at(chat_id=-1001, started_at=NOW)

    assert await repository.get_last_started_at(-1001) == NOW


@pytest.mark.asyncio
async def test_replace_schedule_is_atomic_and_preserves_single_key(
    migrated_connection: aiosqlite.Connection,
) -> None:
    repository = CwlForecastRepository(migrated_connection)
    first = await repository.replace_schedule(
        key=KEY,
        rounds=(
            CwlForecastRound(1, "#BBB222", "api"),
            CwlForecastRound(2, "#CCC333", "manual"),
        ),
        created_by_user_id=42,
        source_chat_id=-1001,
        now=NOW,
    )
    second = await repository.replace_schedule(
        key=KEY,
        rounds=(CwlForecastRound(1, "#DDD444", "manual"),),
        created_by_user_id=43,
        source_chat_id=-1002,
        now=NOW + timedelta(minutes=1),
    )

    assert second.id == first.id
    assert second.rounds == (CwlForecastRound(1, "#DDD444", "manual"),)
    cursor = await migrated_connection.execute("SELECT COUNT(*) FROM cwl_forecast_schedules")
    assert (await cursor.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_schedule_constraints_reject_duplicate_round_or_opponent(
    migrated_connection: aiosqlite.Connection,
) -> None:
    repository = CwlForecastRepository(migrated_connection)
    for rounds in (
        (CwlForecastRound(1, "#BBB222", "manual"), CwlForecastRound(1, "#CCC333", "manual")),
        (CwlForecastRound(1, "#BBB222", "manual"), CwlForecastRound(2, "#BBB222", "manual")),
    ):
        with pytest.raises(aiosqlite.IntegrityError):
            await repository.replace_schedule(
                key=KEY,
                rounds=rounds,
                created_by_user_id=42,
                source_chat_id=-1001,
                now=NOW,
            )
        assert await repository.get_schedule(KEY) is None


@pytest.mark.asyncio
async def test_active_session_conflicts_and_expired_session_is_replaced(
    migrated_connection: aiosqlite.Connection,
) -> None:
    repository = CwlForecastRepository(migrated_connection)
    await repository.acquire_session(
        session=_session(
            session_id="active", created_at=NOW, expires_at=NOW + timedelta(minutes=10)
        ),
        now=NOW,
    )
    with pytest.raises(CwlForecastSessionConflictError):
        await repository.acquire_session(
            session=_session(
                session_id="other", created_at=NOW, expires_at=NOW + timedelta(minutes=10)
            ),
            now=NOW,
        )

    await repository.acquire_session(
        session=_session(
            session_id="replacement",
            created_at=NOW + timedelta(minutes=11),
            expires_at=NOW + timedelta(minutes=21),
        ),
        now=NOW + timedelta(minutes=11),
    )
    assert await repository.get_session("active") is None
    replacement = await repository.get_session("replacement")
    assert replacement is not None
    assert replacement.draft == {"rounds": []}
