"""SQLite repository прогноза и ручного расписания ЛВК."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

import aiosqlite

from clash_sheet_sync_bot.storage import transaction

from .base import as_int, as_optional_int, as_str, fetch_all, fetch_one

RoundSource = Literal["api", "manual"]


class CwlForecastSessionConflictError(RuntimeError):
    """Расписание уже редактируется в активной сессии."""


@dataclass(frozen=True, slots=True)
class CwlForecastScheduleKey:
    """Глобальный ключ расписания одной ЛВК и нашего клана."""

    season: str
    group_fingerprint: str
    clan_tag: str


@dataclass(frozen=True, slots=True)
class CwlForecastRound:
    """Сохранённый соперник одного раунда ЛВК."""

    round_number: int
    opponent_clan_tag: str
    source: RoundSource


@dataclass(frozen=True, slots=True)
class CwlForecastSchedule:
    """Полное сохранённое расписание с audit metadata."""

    id: int
    key: CwlForecastScheduleKey
    rounds: tuple[CwlForecastRound, ...]
    created_by_user_id: int
    source_chat_id: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CwlForecastSession:
    """Временная сессия кнопочного ввода расписания."""

    id: str
    key: CwlForecastScheduleKey
    source_chat_id: int
    created_by_user_id: int
    message_id: int | None
    draft: dict[str, object]
    current_step: int
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


class CwlForecastRepository:
    """Владеет cooldown, расписаниями, раундами и сессиями прогноза."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def get_last_started_at(self, chat_id: int) -> datetime | None:
        """Читает время последнего принятого прогноза для чата."""

        row = await fetch_one(
            self._connection,
            "SELECT last_started_at FROM cwl_forecast_chat_state WHERE chat_id = ?",
            (chat_id,),
        )
        return None if row is None else _parse_datetime(row["last_started_at"], "last_started_at")

    async def set_last_started_at(self, *, chat_id: int, started_at: datetime) -> None:
        """Атомарно создаёт или обновляет persisted cooldown."""

        _require_aware(started_at, "started_at")
        await self._connection.execute(
            """
            INSERT INTO cwl_forecast_chat_state(chat_id, last_started_at)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET last_started_at = excluded.last_started_at
            """,
            (chat_id, started_at.isoformat()),
        )
        await self._connection.commit()

    async def get_schedule(self, key: CwlForecastScheduleKey) -> CwlForecastSchedule | None:
        """Читает расписание и его раунды по глобальному ключу."""

        row = await fetch_one(
            self._connection,
            """
            SELECT * FROM cwl_forecast_schedules
            WHERE season = ? AND group_fingerprint = ? AND clan_tag = ?
            """,
            (key.season, key.group_fingerprint, key.clan_tag),
        )
        if row is None:
            return None
        schedule_id = as_int(row["id"], "id")
        round_rows = await fetch_all(
            self._connection,
            """
            SELECT round_number, opponent_clan_tag, source
            FROM cwl_forecast_rounds WHERE schedule_id = ? ORDER BY round_number
            """,
            (schedule_id,),
        )
        rounds = tuple(_round_from_row(item) for item in round_rows)
        return CwlForecastSchedule(
            id=schedule_id,
            key=key,
            rounds=rounds,
            created_by_user_id=as_int(row["created_by_user_id"], "created_by_user_id"),
            source_chat_id=as_int(row["source_chat_id"], "source_chat_id"),
            created_at=_parse_datetime(row["created_at"], "created_at"),
            updated_at=_parse_datetime(row["updated_at"], "updated_at"),
        )

    async def replace_schedule(
        self,
        *,
        key: CwlForecastScheduleKey,
        rounds: tuple[CwlForecastRound, ...],
        created_by_user_id: int,
        source_chat_id: int,
        now: datetime,
    ) -> CwlForecastSchedule:
        """Заменяет всё расписание одной транзакцией."""

        _require_aware(now, "now")
        timestamp = now.isoformat()
        async with transaction(self._connection):
            await self._connection.execute(
                """
                INSERT INTO cwl_forecast_schedules(
                    season, group_fingerprint, clan_tag, created_by_user_id,
                    source_chat_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(season, group_fingerprint, clan_tag) DO UPDATE SET
                    created_by_user_id = excluded.created_by_user_id,
                    source_chat_id = excluded.source_chat_id,
                    updated_at = excluded.updated_at
                """,
                (*_key_values(key), created_by_user_id, source_chat_id, timestamp, timestamp),
            )
            row = await fetch_one(
                self._connection,
                """SELECT id FROM cwl_forecast_schedules
                WHERE season = ? AND group_fingerprint = ? AND clan_tag = ?""",
                _key_values(key),
            )
            if row is None:
                raise RuntimeError("Не удалось получить сохранённое расписание.")
            schedule_id = as_int(row["id"], "id")
            await self._connection.execute(
                "DELETE FROM cwl_forecast_rounds WHERE schedule_id = ?", (schedule_id,)
            )
            await self._connection.executemany(
                """INSERT INTO cwl_forecast_rounds(
                    schedule_id, round_number, opponent_clan_tag, source
                ) VALUES (?, ?, ?, ?)""",
                [
                    (schedule_id, item.round_number, item.opponent_clan_tag, item.source)
                    for item in rounds
                ],
            )
        schedule = await self.get_schedule(key)
        if schedule is None:
            raise RuntimeError("Сохранённое расписание исчезло после транзакции.")
        return schedule

    async def confirm_schedule_from_session(
        self,
        *,
        session_id: str,
        key: CwlForecastScheduleKey,
        rounds: tuple[CwlForecastRound, ...],
        created_by_user_id: int,
        source_chat_id: int,
        now: datetime,
    ) -> CwlForecastSchedule:
        """Сохраняет schedule и удаляет подтверждённую сессию одной транзакцией."""

        _require_aware(now, "now")
        timestamp = now.isoformat()
        async with transaction(self._connection):
            session_row = await fetch_one(
                self._connection,
                """SELECT 1 FROM cwl_forecast_schedule_sessions
                WHERE id = ? AND season = ? AND group_fingerprint = ? AND clan_tag = ?""",
                (session_id, *_key_values(key)),
            )
            if session_row is None:
                raise RuntimeError("Сессия подтверждения не найдена.")
            await self._upsert_schedule_rows(
                key=key,
                rounds=rounds,
                created_by_user_id=created_by_user_id,
                source_chat_id=source_chat_id,
                timestamp=timestamp,
            )
            await self._connection.execute(
                "DELETE FROM cwl_forecast_schedule_sessions WHERE id = ?", (session_id,)
            )
        schedule = await self.get_schedule(key)
        if schedule is None:
            raise RuntimeError("Расписание исчезло после подтверждения.")
        return schedule

    async def acquire_session(
        self,
        *,
        session: CwlForecastSession,
        now: datetime,
    ) -> None:
        """Удаляет истёкшую сессию и захватывает глобальный ключ."""

        _require_aware(now, "now")
        async with transaction(self._connection):
            await self._connection.execute(
                """DELETE FROM cwl_forecast_schedule_sessions
                WHERE season = ? AND group_fingerprint = ? AND clan_tag = ?
                  AND expires_at <= ?""",
                (*_key_values(session.key), now.isoformat()),
            )
            try:
                await self._insert_session(session)
            except aiosqlite.IntegrityError as exc:
                raise CwlForecastSessionConflictError(
                    "Расписание уже редактируется другим администратором."
                ) from exc

    async def get_session(self, session_id: str) -> CwlForecastSession | None:
        """Читает сессию по компактному ID."""

        row = await fetch_one(
            self._connection,
            "SELECT * FROM cwl_forecast_schedule_sessions WHERE id = ?",
            (session_id,),
        )
        return None if row is None else _session_from_row(row)

    async def update_session(
        self,
        *,
        session_id: str,
        draft: dict[str, object],
        current_step: int,
        message_id: int | None,
        updated_at: datetime,
    ) -> bool:
        """Обновляет validated draft и позицию активной сессии."""

        _require_aware(updated_at, "updated_at")
        cursor = await self._connection.execute(
            """UPDATE cwl_forecast_schedule_sessions
            SET draft_json = ?, current_step = ?, message_id = ?, updated_at = ?
            WHERE id = ?""",
            (_dump_draft(draft), current_step, message_id, updated_at.isoformat(), session_id),
        )
        await self._connection.commit()
        return cursor.rowcount == 1

    async def delete_session(self, session_id: str) -> bool:
        """Удаляет завершённую или отменённую сессию."""

        cursor = await self._connection.execute(
            "DELETE FROM cwl_forecast_schedule_sessions WHERE id = ?", (session_id,)
        )
        await self._connection.commit()
        return cursor.rowcount == 1

    async def _insert_session(self, session: CwlForecastSession) -> None:
        """Вставляет сессию внутри транзакции acquire."""

        for value, name in (
            (session.expires_at, "expires_at"),
            (session.created_at, "created_at"),
            (session.updated_at, "updated_at"),
        ):
            _require_aware(value, name)
        await self._connection.execute(
            """INSERT INTO cwl_forecast_schedule_sessions(
                id, season, group_fingerprint, clan_tag, source_chat_id,
                created_by_user_id, message_id, draft_json, current_step,
                expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session.id,
                *_key_values(session.key),
                session.source_chat_id,
                session.created_by_user_id,
                session.message_id,
                _dump_draft(session.draft),
                session.current_step,
                session.expires_at.isoformat(),
                session.created_at.isoformat(),
                session.updated_at.isoformat(),
            ),
        )

    async def _upsert_schedule_rows(
        self,
        *,
        key: CwlForecastScheduleKey,
        rounds: tuple[CwlForecastRound, ...],
        created_by_user_id: int,
        source_chat_id: int,
        timestamp: str,
    ) -> None:
        """Заменяет schedule rows внутри уже открытой транзакции."""

        await self._connection.execute(
            """INSERT INTO cwl_forecast_schedules(
                season, group_fingerprint, clan_tag, created_by_user_id,
                source_chat_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(season, group_fingerprint, clan_tag) DO UPDATE SET
                created_by_user_id = excluded.created_by_user_id,
                source_chat_id = excluded.source_chat_id,
                updated_at = excluded.updated_at""",
            (*_key_values(key), created_by_user_id, source_chat_id, timestamp, timestamp),
        )
        row = await fetch_one(
            self._connection,
            """SELECT id FROM cwl_forecast_schedules
            WHERE season = ? AND group_fingerprint = ? AND clan_tag = ?""",
            _key_values(key),
        )
        if row is None:
            raise RuntimeError("Не удалось получить schedule id.")
        schedule_id = as_int(row["id"], "id")
        await self._connection.execute(
            "DELETE FROM cwl_forecast_rounds WHERE schedule_id = ?", (schedule_id,)
        )
        await self._connection.executemany(
            """INSERT INTO cwl_forecast_rounds(
                schedule_id, round_number, opponent_clan_tag, source
            ) VALUES (?, ?, ?, ?)""",
            [
                (schedule_id, item.round_number, item.opponent_clan_tag, item.source)
                for item in rounds
            ],
        )


def _key_values(key: CwlForecastScheduleKey) -> tuple[str, str, str]:
    return key.season, key.group_fingerprint, key.clan_tag


def _round_from_row(row: aiosqlite.Row) -> CwlForecastRound:
    source = as_str(row["source"], "source")
    if source not in {"api", "manual"}:
        raise RuntimeError(f"Некорректный source расписания: {source}.")
    return CwlForecastRound(
        round_number=as_int(row["round_number"], "round_number"),
        opponent_clan_tag=as_str(row["opponent_clan_tag"], "opponent_clan_tag"),
        source=cast(RoundSource, source),
    )


def _session_from_row(row: aiosqlite.Row) -> CwlForecastSession:
    raw_draft = as_str(row["draft_json"], "draft_json")
    try:
        draft = json.loads(raw_draft)
    except json.JSONDecodeError as exc:
        raise RuntimeError("draft_json сессии содержит битый JSON.") from exc
    if not isinstance(draft, dict):
        raise RuntimeError("draft_json сессии должен быть объектом.")
    return CwlForecastSession(
        id=as_str(row["id"], "id"),
        key=CwlForecastScheduleKey(
            season=as_str(row["season"], "season"),
            group_fingerprint=as_str(row["group_fingerprint"], "group_fingerprint"),
            clan_tag=as_str(row["clan_tag"], "clan_tag"),
        ),
        source_chat_id=as_int(row["source_chat_id"], "source_chat_id"),
        created_by_user_id=as_int(row["created_by_user_id"], "created_by_user_id"),
        message_id=as_optional_int(row["message_id"], "message_id"),
        draft=dict(draft),
        current_step=as_int(row["current_step"], "current_step"),
        expires_at=_parse_datetime(row["expires_at"], "expires_at"),
        created_at=_parse_datetime(row["created_at"], "created_at"),
        updated_at=_parse_datetime(row["updated_at"], "updated_at"),
    )


def _dump_draft(draft: dict[str, object]) -> str:
    return json.dumps(draft, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} должен содержать timezone.")


def _parse_datetime(value: object, name: str) -> datetime:
    raw = as_str(value, name)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} содержит некорректную дату.") from exc
    _require_aware(parsed, name)
    return parsed
