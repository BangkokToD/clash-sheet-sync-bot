"""Repository агрегированного raid state и registry архивов."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import aiosqlite

from .base import (
    as_int,
    as_optional_str,
    as_str,
    as_user_values,
    fetch_all,
    fetch_one,
)

RAID_TECHNICAL_FIELDS: Final[tuple[str, ...]] = (
    "player_name",
    "attacks",
    "attack_limit",
    "bonus_attack_limit",
    "capital_resources_looted",
    "weighted_damage_units",
    "normal_points",
    "coefficient",
)
RAID_INTEGER_FIELDS: Final[tuple[str, ...]] = (
    "attacks",
    "attack_limit",
    "bonus_attack_limit",
    "capital_resources_looted",
    "weighted_damage_units",
)
RAID_DECIMAL_FIELDS: Final[tuple[str, ...]] = ("normal_points", "coefficient")


class RaidDataError(RuntimeError):
    """Ошибка канонического persisted raid state."""


@dataclass(frozen=True, slots=True)
class RaidPlayerState:
    """Сохранённая агрегированная строка рейдового сезона."""

    chat_id: int
    season_key: str
    season_start_at: str
    season_end_at: str
    season_state: str
    row_key: str
    clan_tag: str
    player_tag: str
    technical_values: dict[str, object]
    user_values: dict[str, str]
    row_hash: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class RaidSheetArchive:
    """Зарегистрированный bot-owned архив рейдового листа."""

    chat_id: int
    season_key: str
    season_start_at: str
    sheet_name: str
    sheet_id: int
    archived_at: str


class RaidPlayerStateRepository:
    """Repository агрегированных строк рейдовых сезонов."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def list_for_season(
        self,
        *,
        chat_id: int,
        season_key: str,
        clan_tags: tuple[str, ...] | None = None,
    ) -> tuple[RaidPlayerState, ...]:
        """Читает строки сезона, при необходимости ограниченные кланами."""

        sql = "SELECT * FROM raid_player_state WHERE chat_id = ? AND season_key = ?"
        parameters: tuple[object, ...] = (chat_id, season_key)
        if clan_tags is not None:
            if not clan_tags:
                return ()
            placeholders = ", ".join("?" for _ in clan_tags)
            sql += f" AND clan_tag IN ({placeholders})"
            parameters = (*parameters, *clan_tags)
        sql += " ORDER BY clan_tag ASC, row_key ASC"
        return tuple(
            _row_to_player_state(row) for row in await fetch_all(self._connection, sql, parameters)
        )

    async def get_latest_season_key(
        self,
        *,
        chat_id: int,
        clan_tags: tuple[str, ...],
    ) -> str | None:
        """Возвращает самый новый сохранённый сезон активных кланов."""

        if not clan_tags:
            return None
        placeholders = ", ".join("?" for _ in clan_tags)
        row = await fetch_one(
            self._connection,
            f"""
            SELECT season_key
            FROM raid_player_state
            WHERE chat_id = ? AND clan_tag IN ({placeholders})
            ORDER BY season_start_at DESC, season_key DESC
            LIMIT 1
            """,
            (chat_id, *clan_tags),
        )
        return None if row is None else as_str(row["season_key"], "season_key")

    async def upsert(self, state: RaidPlayerState) -> None:
        """Идемпотентно сохраняет агрегированную строку."""

        await self._connection.execute(
            """
            INSERT INTO raid_player_state(
                chat_id, season_key, season_start_at, season_end_at, season_state,
                row_key, clan_tag, player_tag, technical_values_json,
                user_values_json, row_hash, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, season_key, row_key) DO UPDATE SET
                season_start_at = excluded.season_start_at,
                season_end_at = excluded.season_end_at,
                season_state = excluded.season_state,
                clan_tag = excluded.clan_tag,
                player_tag = excluded.player_tag,
                technical_values_json = excluded.technical_values_json,
                user_values_json = excluded.user_values_json,
                row_hash = excluded.row_hash,
                updated_at = excluded.updated_at
            """,
            (
                state.chat_id,
                state.season_key,
                state.season_start_at,
                state.season_end_at,
                state.season_state,
                state.row_key,
                state.clan_tag,
                state.player_tag,
                encode_raid_technical_values(state.technical_values),
                json.dumps(state.user_values, ensure_ascii=False, separators=(",", ":")),
                state.row_hash,
                state.updated_at,
            ),
        )


class RaidSheetArchiveRepository:
    """Repository bot-owned registry рейдовых архивов."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def list_ordered(self, chat_id: int) -> tuple[RaidSheetArchive, ...]:
        """Читает архивы от самого старого к новому."""

        rows = await fetch_all(
            self._connection,
            """
            SELECT * FROM raid_sheet_archives
            WHERE chat_id = ?
            ORDER BY season_start_at ASC, archived_at ASC, sheet_id ASC
            """,
            (chat_id,),
        )
        return tuple(_row_to_archive(row) for row in rows)

    async def get_by_season(self, *, chat_id: int, season_key: str) -> RaidSheetArchive | None:
        """Читает архив по сезону."""

        row = await fetch_one(
            self._connection,
            "SELECT * FROM raid_sheet_archives WHERE chat_id = ? AND season_key = ?",
            (chat_id, season_key),
        )
        return None if row is None else _row_to_archive(row)

    async def get_by_sheet_id(self, *, chat_id: int, sheet_id: int) -> RaidSheetArchive | None:
        """Читает архив по физическому sheet ID."""

        row = await fetch_one(
            self._connection,
            "SELECT * FROM raid_sheet_archives WHERE chat_id = ? AND sheet_id = ?",
            (chat_id, sheet_id),
        )
        return None if row is None else _row_to_archive(row)

    async def upsert(self, archive: RaidSheetArchive) -> None:
        """Регистрирует архив по season identity."""

        await self._connection.execute(
            """
            INSERT INTO raid_sheet_archives(
                chat_id, season_key, season_start_at, sheet_name, sheet_id, archived_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, season_key) DO UPDATE SET
                season_start_at = excluded.season_start_at,
                sheet_name = excluded.sheet_name,
                sheet_id = excluded.sheet_id,
                archived_at = excluded.archived_at
            """,
            (
                archive.chat_id,
                archive.season_key,
                archive.season_start_at,
                archive.sheet_name,
                archive.sheet_id,
                archive.archived_at,
            ),
        )

    async def delete(self, *, chat_id: int, season_key: str) -> None:
        """Удаляет registry одного сезона, не затрагивая player state."""

        await self._connection.execute(
            "DELETE FROM raid_sheet_archives WHERE chat_id = ? AND season_key = ?",
            (chat_id, season_key),
        )

    async def remove_stale(self, *, chat_id: int, existing_sheet_ids: Iterable[int]) -> int:
        """Удаляет registry записей, физические sheet ID которых отсутствуют."""

        existing = tuple(existing_sheet_ids)
        if not existing:
            cursor = await self._connection.execute(
                "DELETE FROM raid_sheet_archives WHERE chat_id = ?",
                (chat_id,),
            )
            return cursor.rowcount
        placeholders = ", ".join("?" for _ in existing)
        cursor = await self._connection.execute(
            f"DELETE FROM raid_sheet_archives WHERE chat_id = ? AND sheet_id NOT IN ({placeholders})",
            (chat_id, *existing),
        )
        return cursor.rowcount


def _row_to_player_state(row: aiosqlite.Row) -> RaidPlayerState:
    return RaidPlayerState(
        chat_id=as_int(row["chat_id"], "chat_id"),
        season_key=as_str(row["season_key"], "season_key"),
        season_start_at=as_str(row["season_start_at"], "season_start_at"),
        season_end_at=as_str(row["season_end_at"], "season_end_at"),
        season_state=as_str(row["season_state"], "season_state"),
        row_key=as_str(row["row_key"], "row_key"),
        clan_tag=as_str(row["clan_tag"], "clan_tag"),
        player_tag=as_str(row["player_tag"], "player_tag"),
        technical_values=decode_raid_technical_values(
            as_str(row["technical_values_json"], "technical_values_json")
        ),
        user_values=as_user_values(row["user_values_json"]),
        row_hash=as_optional_str(row["row_hash"], "row_hash"),
        updated_at=as_str(row["updated_at"], "updated_at"),
    )


def _row_to_archive(row: aiosqlite.Row) -> RaidSheetArchive:
    return RaidSheetArchive(
        chat_id=as_int(row["chat_id"], "chat_id"),
        season_key=as_str(row["season_key"], "season_key"),
        season_start_at=as_str(row["season_start_at"], "season_start_at"),
        sheet_name=as_str(row["sheet_name"], "sheet_name"),
        sheet_id=as_int(row["sheet_id"], "sheet_id"),
        archived_at=as_str(row["archived_at"], "archived_at"),
    )


def encode_raid_technical_values(values: dict[str, object]) -> str:
    """Сериализует полный technical state в канонический JSON без float.

    Decimal-поля записываются JSON-числами напрямую. Это сохраняет точность
    строкового decimal-представления и не вводит промежуточный binary float.
    """

    normalized = _validate_raid_technical_values(values, require_decimal=True)
    parts: list[str] = []
    for field in RAID_TECHNICAL_FIELDS:
        value = normalized[field]
        key_json = json.dumps(field, ensure_ascii=False)
        if isinstance(value, Decimal):
            value_json = _decimal_json_number(value)
        else:
            value_json = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        parts.append(f"{key_json}:{value_json}")
    return "{" + ",".join(parts) + "}"


def decode_raid_technical_values(raw_json: str) -> dict[str, object]:
    """Разбирает persisted technical JSON по строгому raid-контракту."""

    try:
        data = json.loads(
            raw_json,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_json_object,
        )
    except json.JSONDecodeError as exc:
        raise RaidDataError("technical_values_json содержит битый JSON.") from exc
    if not isinstance(data, dict):
        raise RaidDataError("technical_values_json должен быть JSON-объектом.")
    try:
        return _validate_raid_technical_values(data, require_decimal=False)
    except RaidDataError as exc:
        raise RaidDataError(f"technical_values_json: {exc}") from exc


def _validate_raid_technical_values(
    values: dict[str, Any],
    *,
    require_decimal: bool,
) -> dict[str, object]:
    actual_fields = set(values)
    required_fields = set(RAID_TECHNICAL_FIELDS)
    missing = sorted(required_fields - actual_fields)
    unknown = sorted(actual_fields - required_fields)
    if missing:
        raise RaidDataError(f"отсутствует обязательное поле {missing[0]}.")
    if unknown:
        raise RaidDataError(f"неизвестное поле {unknown[0]}.")

    player_name = values["player_name"]
    if not isinstance(player_name, str) or player_name.strip() == "":
        raise RaidDataError("поле player_name должно быть непустой строкой.")

    result: dict[str, object] = {"player_name": player_name}
    for field in RAID_INTEGER_FIELDS:
        value = values[field]
        if not isinstance(value, int) or isinstance(value, bool):
            raise RaidDataError(f"поле {field} должно быть целым JSON-числом.")
        if value < 0:
            raise RaidDataError(f"поле {field} не может быть отрицательным.")
        result[field] = value

    if int(result["attacks"]) > int(result["attack_limit"]) + int(
        result["bonus_attack_limit"]
    ):
        raise RaidDataError("поле attacks превышает attack_limit + bonus_attack_limit.")

    for field in RAID_DECIMAL_FIELDS:
        raw_value = values[field]
        if require_decimal:
            if not isinstance(raw_value, Decimal):
                raise RaidDataError(f"поле {field} должно иметь тип Decimal.")
            value = raw_value
        else:
            if isinstance(raw_value, bool) or not isinstance(raw_value, (int, Decimal)):
                raise RaidDataError(f"поле {field} должно быть JSON-числом.")
            try:
                value = Decimal(raw_value)
            except (InvalidOperation, ValueError) as exc:
                raise RaidDataError(f"поле {field} содержит некорректное число.") from exc
        if not value.is_finite() or value < 0:
            raise RaidDataError(f"поле {field} должно быть конечным неотрицательным числом.")
        result[field] = value

    expected_normal_points = Decimal(int(result["weighted_damage_units"])) / Decimal(100)
    if result["normal_points"] != expected_normal_points:
        raise RaidDataError(
            "поле normal_points не соответствует weighted_damage_units / 100."
        )
    return result


def _decimal_json_number(value: Decimal) -> str:
    if value == 0:
        return "0"
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _reject_json_constant(value: str) -> None:
    raise RaidDataError(f"недопустимая JSON-константа {value}.")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RaidDataError(f"technical_values_json содержит дубликат поля {key}.")
        result[key] = value
    return result
