"""Импорт и state-модель CWL в публичной runtime-архитектуре."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from coc_client import ClashApiUnavailableError, ClashClient, ClashCwlNotInProgressError
from column_profiles import BOT_KEY_COLUMN_KEY, BOT_KEY_TITLE
from models import ColumnProfile, RuntimeChatConfig, SheetBlock, TableType, TrackedClan, normalize_tag
from repositories import CwlRowState, CwlRowStateRepository, SheetBlockRepository
from sheets_client import CellValue, SheetsClient, range_from_start_cell

logger = logging.getLogger(__name__)

CWL_TABLE: Final[TableType] = "cwl"
CWL_BLOCK_PREFIX: Final = "cwl:"
CWL_WIDE_IMPORT_RANGE: Final = "A1:ZZ1000"
CWL_ELIGIBLE_WAR_STATES: Final = {"warEnded", "inWar"}
CWL_WAR_CONCURRENCY_LIMIT: Final = 5
NO_ATTACK_MARKER: Final = "NO_ATTACK"
ATTACK_MARKER_PREFIX: Final = "ATTACK"
DEF_POS_MARKER_PREFIX: Final = "DEF_POS_"
CWL_ROW_KEY_PARTS_COUNT: Final = 5
BOT_KEY_PREFIX: Final = "cwl_row:"
TECHNICAL_HASH_VERSION: Final = "1"
DEFENDER_POSITION_RE: Final = re.compile(r"^\s*(\d+)\b")

SYSTEM_HEADER_ALIASES: Final[dict[str, set[str]]] = {
    "round": {"Раунд"},
    "attacker_tag": {"Тег", "Тег атакующего"},
    "attacker_name": {"Ник", "Никнейм", "Ник атакующего"},
    "attacker_town_hall": {"ТХ", "ТХ - номер"},
    "defender_town_hall": {"ТХ соперника", "ТХ соперника - номер"},
    "stars": {"Звезды", "Звёзды"},
    "destruction_percentage": {"Процент разрушений"},
}

JsonObject = dict[str, Any]
JsonValues = dict[str, str]


class CwlSyncError(RuntimeError):
    """Базовая ошибка CWL sync-state."""


class CwlDataError(CwlSyncError):
    """Ошибка данных CWL, при которой state нельзя обновлять."""


@dataclass(frozen=True, slots=True)
class CwlTechnicalValues:
    """Технические значения CWL-строки."""

    round_number: int
    attacker_tag: str
    attacker_name: str
    attacker_town_hall: int
    defender_town_hall: int | None
    stars: int | None
    destruction_percentage: int | None
    marker: str
    attacker_map_position: int
    defender_map_position: int | None

    def to_json(self) -> JsonObject:
        """Преобразует technical values в JSON-словарь."""

        return {
            "round": self.round_number,
            "attacker_tag": self.attacker_tag,
            "attacker_name": self.attacker_name,
            "attacker_town_hall": self.attacker_town_hall,
            "defender_town_hall": self.defender_town_hall,
            "stars": self.stars,
            "destruction_percentage": self.destruction_percentage,
            "marker": self.marker,
            "attacker_map_position": self.attacker_map_position,
            "defender_map_position": self.defender_map_position,
        }


@dataclass(frozen=True, slots=True)
class CwlPlannedRow:
    """Строка CWL, построенная из CoC API перед записью в SQLite."""

    row_key: str
    season: str
    clan_tag: str
    round_number: int
    attacker_tag: str
    marker: str
    technical_values: CwlTechnicalValues
    no_attack_key: str
    old_alias_keys: tuple[str, ...] = ()
    user_values: JsonValues = field(default_factory=dict)

    @property
    def row_hash(self) -> str:
        """Считает hash только по technical values."""

        payload = {
            "version": TECHNICAL_HASH_VERSION,
            "technical_values": self.technical_values.to_json(),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CwlImportedRow:
    """User fields, импортированные из текущего CWL-листа."""

    row_key: str
    user_values: JsonValues
    old_alias_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CwlImportResult:
    """Результат импорта текущего активного CWL-листа."""

    rows_by_key: dict[str, JsonValues]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CwlStateSyncResult:
    """Результат обновления CWL row state."""

    season: str | None
    rows_count: int
    all_not_in_progress: bool
    not_in_progress_clans: tuple[TrackedClan, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CwlTableHeader:
    """Найденный блок текущего CWL-листа."""

    row_index: int
    column_index: int
    width: int
    clan_tag: str | None


async def run_cwl_state_sync(
    *,
    runtime_config: RuntimeChatConfig,
    clash_client: ClashClient,
    sheets_client: SheetsClient,
    cwl_repository: CwlRowStateRepository,
    sheet_block_repository: SheetBlockRepository,
) -> CwlStateSyncResult:
    """Обновляет `cwl_row_state` без записи Google Sheets."""

    if not runtime_config.active_clans:
        raise CwlDataError("Для CWL sync-state нужен хотя бы один активный клан.")

    league_groups = await _load_league_groups(runtime_config.active_clans, clash_client)
    participating_groups = {
        clan_tag: group
        for clan_tag, group in league_groups.items()
        if group is not None
    }
    not_in_progress_clans = tuple(
        clan for clan in runtime_config.active_clans if league_groups.get(clan.clan_tag) is None
    )
    if not participating_groups:
        return CwlStateSyncResult(
            season=None,
            rows_count=0,
            all_not_in_progress=True,
            not_in_progress_clans=not_in_progress_clans,
        )

    season = _resolve_cwl_season(participating_groups)
    sheet_name = runtime_config.sheet_binding.active_cwl_sheet_name
    previous_blocks = await sheet_block_repository.list_blocks(runtime_config.chat_id, sheet_name)
    cwl_blocks = tuple(
        block for block in previous_blocks if block.block_key.startswith(CWL_BLOCK_PREFIX)
    )
    imported = await import_current_cwl_sheet(
        runtime_config=runtime_config,
        sheets_client=sheets_client,
        blocks=cwl_blocks,
        season=season,
    )

    war_tags = _collect_unique_war_tags(participating_groups.values())
    wars_by_tag = await _load_cwl_wars(clash_client, war_tags)
    planned_rows = _build_planned_rows(
        runtime_config=runtime_config,
        season=season,
        league_groups=league_groups,
        wars_by_tag=wars_by_tag,
    )
    existing_rows = await cwl_repository.list_rows(
        chat_id=runtime_config.chat_id,
        season=season,
    )
    rows_with_user_values = _apply_user_values(
        planned_rows=planned_rows,
        imported=imported,
        existing_rows=existing_rows,
    )

    for row in rows_with_user_values:
        await cwl_repository.upsert_row_state(
            chat_id=runtime_config.chat_id,
            season=row.season,
            row_key=row.row_key,
            clan_tag=row.clan_tag,
            round_number=row.round_number,
            attacker_tag=row.attacker_tag,
            marker=row.marker,
            technical_values=row.technical_values.to_json(),
            user_values=row.user_values,
            row_hash=row.row_hash,
        )

    return CwlStateSyncResult(
        season=season,
        rows_count=len(rows_with_user_values),
        all_not_in_progress=False,
        not_in_progress_clans=not_in_progress_clans,
        warnings=imported.warnings,
    )


async def import_current_cwl_sheet(
    *,
    runtime_config: RuntimeChatConfig,
    sheets_client: SheetsClient,
    blocks: Sequence[SheetBlock],
    season: str,
) -> CwlImportResult:
    """Импортирует user fields из текущего активного CWL-листа."""

    sheet_name = runtime_config.sheet_binding.active_cwl_sheet_name
    warnings: list[str] = []
    imported_rows: list[CwlImportedRow] = []

    if blocks:
        for block in blocks:
            values = await sheets_client.read_values(
                sheet_name,
                range_from_start_cell(
                    start_cell=block.start_cell,
                    rows_count=block.rows_count,
                    columns_count=block.columns_count,
                ),
            )
            imported_rows.extend(
                _parse_cwl_values(
                    runtime_config=runtime_config,
                    values=values,
                    season=season,
                    block_key=block.block_key,
                    warnings=warnings,
                ),
            )
    else:
        values = await sheets_client.read_values(sheet_name, CWL_WIDE_IMPORT_RANGE)
        imported_rows.extend(
            _parse_cwl_values(
                runtime_config=runtime_config,
                values=values,
                season=season,
                block_key=None,
                warnings=warnings,
            ),
        )

    rows_by_key: dict[str, JsonValues] = {}
    for row in imported_rows:
        _put_imported_values(rows_by_key, row.row_key, row.user_values, warnings)
        for alias_key in row.old_alias_keys:
            _put_imported_values(rows_by_key, alias_key, row.user_values, warnings)

    return CwlImportResult(rows_by_key=rows_by_key, warnings=tuple(warnings))


def make_cwl_row_key(
    *,
    season: str,
    clan_tag: str,
    round_number: int,
    attacker_tag: str,
    marker: str,
) -> str:
    """Создаёт стабильный ключ CWL-строки."""

    return "|".join(
        [
            season,
            normalize_tag(clan_tag),
            str(round_number),
            normalize_tag(attacker_tag),
            marker,
        ],
    )


async def _load_league_groups(
    active_clans: Sequence[TrackedClan],
    clash_client: ClashClient,
) -> dict[str, JsonObject | None]:
    """Загружает CWL leaguegroup для всех active tracked clans."""

    groups: dict[str, JsonObject | None] = {}
    for clan in active_clans:
        try:
            groups[clan.clan_tag] = await clash_client.get_current_war_league_group(
                clan.clan_tag,
            )
        except ClashCwlNotInProgressError:
            groups[clan.clan_tag] = None
    return groups


def _resolve_cwl_season(groups: dict[str, JsonObject]) -> str:
    """Определяет единый CWL-сезон по participating league groups."""

    seasons = {_require_str(group, "season", "leaguegroup") for group in groups.values()}
    if len(seasons) != 1:
        raise CwlDataError("Участвующие кланы вернули разные сезоны CWL.")
    return next(iter(seasons))


def _collect_unique_war_tags(groups: Sequence[JsonObject]) -> list[str]:
    """Собирает уникальные warTag из league groups."""

    seen: set[str] = set()
    war_tags: list[str] = []
    for group in groups:
        for round_payload in _require_list(group, "rounds", "leaguegroup"):
            if not isinstance(round_payload, dict):
                raise ClashApiUnavailableError("leaguegroup содержит некорректный round.")
            for raw_tag in _require_list(round_payload, "warTags", "leaguegroup round"):
                if not isinstance(raw_tag, str):
                    raise ClashApiUnavailableError("leaguegroup содержит некорректный warTag.")
                tag = normalize_tag(raw_tag)
                if tag == "#0" or tag in seen:
                    continue
                seen.add(tag)
                war_tags.append(tag)
    return war_tags


async def _load_cwl_wars(
    clash_client: ClashClient,
    war_tags: Sequence[str],
) -> dict[str, JsonObject]:
    """Загружает CWL wars с ограничением конкурентности."""

    semaphore = asyncio.Semaphore(CWL_WAR_CONCURRENCY_LIMIT)

    async def load_one(war_tag: str) -> tuple[str, JsonObject]:
        async with semaphore:
            return war_tag, await clash_client.get_cwl_war(war_tag)

    loaded = await asyncio.gather(*(load_one(war_tag) for war_tag in war_tags))
    return dict(loaded)


def _build_planned_rows(
    *,
    runtime_config: RuntimeChatConfig,
    season: str,
    league_groups: dict[str, JsonObject | None],
    wars_by_tag: dict[str, JsonObject],
) -> tuple[CwlPlannedRow, ...]:
    """Строит внутренние CWL rows из CoC API."""

    rows: list[CwlPlannedRow] = []
    for clan in runtime_config.active_clans:
        group = league_groups.get(clan.clan_tag)
        if group is None:
            continue

        rounds = _require_list(group, "rounds", "leaguegroup")
        for round_number, round_payload in enumerate(rounds, start=1):
            if not isinstance(round_payload, dict):
                raise ClashApiUnavailableError("leaguegroup содержит некорректный round.")

            for raw_tag in _require_list(round_payload, "warTags", "leaguegroup round"):
                if not isinstance(raw_tag, str):
                    raise ClashApiUnavailableError("leaguegroup содержит некорректный warTag.")
                war_tag = normalize_tag(raw_tag)
                if war_tag == "#0":
                    continue

                war = wars_by_tag.get(war_tag)
                if war is None:
                    continue
                state = _require_str(war, "state", f"war {war_tag}")
                if state not in CWL_ELIGIBLE_WAR_STATES:
                    continue

                side = _extract_war_side(war, clan.clan_tag)
                if side is None:
                    continue

                rows.extend(
                    _build_war_rows(
                        season=season,
                        clan_tag=clan.clan_tag,
                        round_number=round_number,
                        our_side=side[0],
                        opponent_side=side[1],
                    ),
                )

    return tuple(rows)


def _extract_war_side(
    war: JsonObject,
    clan_tag: str,
) -> tuple[JsonObject, JsonObject] | None:
    """Определяет сторону tracked clan в CWL war."""

    clan_side = _require_dict(war, "clan", "cwl war")
    opponent_side = _require_dict(war, "opponent", "cwl war")
    api_clan_tag = normalize_tag(_require_str(clan_side, "tag", "war clan"))
    api_opponent_tag = normalize_tag(_require_str(opponent_side, "tag", "war opponent"))

    if api_clan_tag == clan_tag:
        return clan_side, opponent_side
    if api_opponent_tag == clan_tag:
        return opponent_side, clan_side
    return None


def _build_war_rows(
    *,
    season: str,
    clan_tag: str,
    round_number: int,
    our_side: JsonObject,
    opponent_side: JsonObject,
) -> list[CwlPlannedRow]:
    """Строит строки одной CWL-войны для tracked clan."""

    opponent_members = _read_war_members(opponent_side, "opponent")
    opponent_by_tag = {member["tag"]: member for member in opponent_members}
    our_members = sorted(
        _read_war_members(our_side, "clan"),
        key=lambda member: (member["map_position"], member["tag"]),
    )

    rows: list[CwlPlannedRow] = []
    for member in our_members:
        no_attack_key = make_cwl_row_key(
            season=season,
            clan_tag=clan_tag,
            round_number=round_number,
            attacker_tag=_json_str(member, "tag"),
            marker=NO_ATTACK_MARKER,
        )
        attacks = member["attacks"]

        if not attacks:
            rows.append(
                _planned_row(
                    season=season,
                    clan_tag=clan_tag,
                    round_number=round_number,
                    attacker=member,
                    marker=NO_ATTACK_MARKER,
                    defender=None,
                    stars=None,
                    destruction_percentage=None,
                    no_attack_key=no_attack_key,
                    old_alias_keys=(),
                ),
            )
            continue

        for attack_index, attack in enumerate(attacks, start=1):
            defender_tag = normalize_tag(_require_str(attack, "defenderTag", "attack"))
            defender = opponent_by_tag.get(defender_tag)
            if defender is None:
                raise ClashApiUnavailableError(
                    f"CWL attack содержит неизвестный defenderTag {defender_tag}.",
                )

            marker = f"{ATTACK_MARKER_PREFIX}_{attack_index}"
            old_alias = make_cwl_row_key(
                season=season,
                clan_tag=clan_tag,
                round_number=round_number,
                attacker_tag=_json_str(member, "tag"),
                marker=f"{DEF_POS_MARKER_PREFIX}{defender['map_position']}",
            )
            rows.append(
                _planned_row(
                    season=season,
                    clan_tag=clan_tag,
                    round_number=round_number,
                    attacker=member,
                    marker=marker,
                    defender=defender,
                    stars=_require_int(attack, "stars", "attack"),
                    destruction_percentage=_require_int(
                        attack,
                        "destructionPercentage",
                        "attack",
                    ),
                    no_attack_key=no_attack_key,
                    old_alias_keys=(old_alias,),
                ),
            )

    return rows


def _planned_row(
    *,
    season: str,
    clan_tag: str,
    round_number: int,
    attacker: JsonObject,
    marker: str,
    defender: JsonObject | None,
    stars: int | None,
    destruction_percentage: int | None,
    no_attack_key: str,
    old_alias_keys: tuple[str, ...],
) -> CwlPlannedRow:
    """Создаёт planned row и technical values."""

    attacker_tag = _json_str(attacker, "tag")
    row_key = make_cwl_row_key(
        season=season,
        clan_tag=clan_tag,
        round_number=round_number,
        attacker_tag=attacker_tag,
        marker=marker,
    )
    technical_values = CwlTechnicalValues(
        round_number=round_number,
        attacker_tag=attacker_tag,
        attacker_name=_json_str(attacker, "name"),
        attacker_town_hall=_json_int(attacker, "town_hall"),
        defender_town_hall=_json_int(defender, "town_hall") if defender is not None else None,
        stars=stars,
        destruction_percentage=destruction_percentage,
        marker=marker,
        attacker_map_position=_json_int(attacker, "map_position"),
        defender_map_position=_json_int(defender, "map_position") if defender is not None else None,
    )
    return CwlPlannedRow(
        row_key=row_key,
        season=season,
        clan_tag=clan_tag,
        round_number=round_number,
        attacker_tag=attacker_tag,
        marker=marker,
        technical_values=technical_values,
        no_attack_key=no_attack_key,
        old_alias_keys=old_alias_keys,
    )


def _read_war_members(side: JsonObject, context: str) -> list[JsonObject]:
    """Читает и нормализует участников стороны CWL war."""

    members: list[JsonObject] = []
    for index, raw_member in enumerate(_require_list(side, "members", context), start=1):
        if not isinstance(raw_member, dict):
            raise ClashApiUnavailableError(f"{context}: member #{index} должен быть объектом.")
        tag = normalize_tag(_require_str(raw_member, "tag", f"{context} member #{index}"))
        raw_attacks = raw_member.get("attacks") or []
        if not isinstance(raw_attacks, list):
            raise ClashApiUnavailableError(f"{context} member {tag}: attacks должен быть списком.")

        attacks: list[JsonObject] = []
        for attack_index, raw_attack in enumerate(raw_attacks, start=1):
            if not isinstance(raw_attack, dict):
                raise ClashApiUnavailableError(
                    f"{context} member {tag}: attack #{attack_index} должен быть объектом.",
                )
            attacks.append(raw_attack)

        members.append(
            {
                "tag": tag,
                "name": _require_str(raw_member, "name", f"{context} member {tag}"),
                "town_hall": _require_int(raw_member, "townhallLevel", f"{context} member {tag}"),
                "map_position": _require_int(raw_member, "mapPosition", f"{context} member {tag}"),
                "attacks": attacks,
            },
        )

    return members


def _parse_cwl_values(
    *,
    runtime_config: RuntimeChatConfig,
    values: Sequence[Sequence[CellValue]],
    season: str,
    block_key: str | None,
    warnings: list[str],
) -> tuple[CwlImportedRow, ...]:
    """Парсит CWL values и возвращает imported user fields."""

    rows = [_string_row(row) for row in values]
    headers = _find_table_headers(rows, runtime_config, block_key)
    imported: list[CwlImportedRow] = []

    for header in headers:
        next_row = _find_next_header_row(headers, header)
        occurrence_counter: dict[tuple[str, int, str], int] = {}
        header_row = rows[header.row_index]
        profiles = _profiles(runtime_config.column_profiles)
        user_indexes = _user_indexes_from_header(profiles, header_row, header.column_index)

        for row_offset, row in enumerate(rows[header.row_index + 1 : next_row], start=header.row_index + 2):
            parsed = _parse_imported_row(
                row=row,
                header=header,
                season=season,
                occurrence_counter=occurrence_counter,
                row_number=row_offset,
                user_indexes=user_indexes,
                warnings=warnings,
            )
            if parsed is not None:
                imported.append(parsed)

    return tuple(imported)


def _find_table_headers(
    rows: Sequence[list[str]],
    runtime_config: RuntimeChatConfig,
    block_key: str | None,
) -> tuple[CwlTableHeader, ...]:
    """Ищет таблицы CWL в values."""

    headers: list[CwlTableHeader] = []
    for row_index, row in enumerate(rows):
        for column_index, cell in enumerate(row):
            normalized = cell.strip()
            round_column_index: int | None = None
            if normalized == BOT_KEY_TITLE:
                next_cell = _cell_at(row, column_index + 1).strip()
                if next_cell in SYSTEM_HEADER_ALIASES["round"]:
                    round_column_index = column_index + 1
            elif normalized in SYSTEM_HEADER_ALIASES["round"]:
                round_column_index = column_index

            if round_column_index is None:
                continue
            if not _looks_like_cwl_header(row, round_column_index):
                continue

            start_column_index = column_index if normalized == BOT_KEY_TITLE else round_column_index
            width = max(_last_non_empty_index(row) - start_column_index + 1, 1)
            headers.append(
                CwlTableHeader(
                    row_index=row_index,
                    column_index=start_column_index,
                    width=width,
                    clan_tag=_header_clan_tag(
                        rows=rows,
                        header_row_index=row_index,
                        column_index=start_column_index,
                        runtime_config=runtime_config,
                        block_key=block_key,
                        header_number=len(headers),
                    ),
                ),
            )

    return tuple(headers)


def _looks_like_cwl_header(row: Sequence[str], round_column_index: int) -> bool:
    """Проверяет, похожа ли строка на заголовок CWL-таблицы."""

    attacker_tag = _cell_at(row, round_column_index + 1).strip()
    attacker_name = _cell_at(row, round_column_index + 2).strip()
    return (
        attacker_tag in SYSTEM_HEADER_ALIASES["attacker_tag"]
        and attacker_name in SYSTEM_HEADER_ALIASES["attacker_name"]
    )


def _header_clan_tag(
    *,
    rows: Sequence[list[str]],
    header_row_index: int,
    column_index: int,
    runtime_config: RuntimeChatConfig,
    block_key: str | None,
    header_number: int,
) -> str | None:
    """Определяет clan_tag таблицы CWL."""

    if block_key is not None and block_key.startswith(CWL_BLOCK_PREFIX):
        raw_tag = block_key.removeprefix(CWL_BLOCK_PREFIX)
        try:
            return normalize_tag(raw_tag)
        except ValueError:
            pass

    detected = _find_clan_tag_above(rows, header_row_index, column_index)
    if detected is not None:
        return detected

    if header_number < len(runtime_config.active_clans):
        return runtime_config.active_clans[header_number].clan_tag
    return None


def _parse_imported_row(
    *,
    row: Sequence[str],
    header: CwlTableHeader,
    season: str,
    occurrence_counter: dict[tuple[str, int, str], int],
    row_number: int,
    user_indexes: dict[str, int],
    warnings: list[str],
) -> CwlImportedRow | None:
    """Парсит одну строку текущего CWL-листа."""

    if header.clan_tag is None:
        return None

    bot_key = _cell_at(row, header.column_index)
    exact_key = _row_key_from_bot_key(bot_key)
    user_values = {
        column_key: _cell_at(row, index)
        for column_key, index in user_indexes.items()
    }

    if exact_key is not None:
        return CwlImportedRow(row_key=exact_key, user_values=user_values)

    fallback = _fallback_row_key(
        row=row,
        header=header,
        season=season,
        occurrence_counter=occurrence_counter,
    )
    if fallback is None:
        if _row_has_any_payload(row, header):
            warnings.append(
                f"Строка {row_number}: повреждён __bot_key, строгий fallback невозможен.",
            )
        return None

    row_key, old_alias_keys = fallback
    warnings.append(f"Строка {row_number}: использован fallback по техническим колонкам.")
    return CwlImportedRow(row_key=row_key, user_values=user_values, old_alias_keys=old_alias_keys)


def _fallback_row_key(
    *,
    row: Sequence[str],
    header: CwlTableHeader,
    season: str,
    occurrence_counter: dict[tuple[str, int, str], int],
) -> tuple[str, tuple[str, ...]] | None:
    """Строго восстанавливает CWL key по техническим колонкам."""

    if header.clan_tag is None:
        return None
    system_indexes = _default_system_indexes(header)
    round_raw = _cell_at(row, system_indexes["round"]).strip()
    attacker_raw = _cell_at(row, system_indexes["attacker_tag"]).strip()
    if not round_raw.isdigit() or attacker_raw == "":
        return None

    try:
        round_number = int(round_raw)
        attacker_tag = normalize_tag(attacker_raw)
    except ValueError:
        return None

    defender_raw = _cell_at(row, system_indexes["defender_town_hall"]).strip()
    stars_raw = _cell_at(row, system_indexes["stars"]).strip()
    destruction_raw = _cell_at(row, system_indexes["destruction_percentage"]).strip()

    if defender_raw == "" and stars_raw == "" and destruction_raw == "":
        marker = NO_ATTACK_MARKER
        old_alias_keys: tuple[str, ...] = ()
    else:
        occurrence_key = (header.clan_tag, round_number, attacker_tag)
        attack_index = occurrence_counter.get(occurrence_key, 0) + 1
        occurrence_counter[occurrence_key] = attack_index
        marker = f"{ATTACK_MARKER_PREFIX}_{attack_index}"
        defender_position = _parse_defender_position(defender_raw)
        old_alias_keys = ()
        if defender_position is not None:
            old_alias_keys = (
                make_cwl_row_key(
                    season=season,
                    clan_tag=header.clan_tag,
                    round_number=round_number,
                    attacker_tag=attacker_tag,
                    marker=f"{DEF_POS_MARKER_PREFIX}{defender_position}",
                ),
            )

    row_key = make_cwl_row_key(
        season=season,
        clan_tag=header.clan_tag,
        round_number=round_number,
        attacker_tag=attacker_tag,
        marker=marker,
    )
    return row_key, old_alias_keys


def _apply_user_values(
    *,
    planned_rows: Sequence[CwlPlannedRow],
    imported: CwlImportResult,
    existing_rows: Sequence[CwlRowState],
) -> tuple[CwlPlannedRow, ...]:
    """Переносит user fields из текущего листа и SQLite state в planned rows."""

    existing_by_key = {row.row_key: row.user_values for row in existing_rows}
    used_no_attack_keys: set[str] = set()
    result: list[CwlPlannedRow] = []

    for row in planned_rows:
        user_values = _lookup_user_values(
            row=row,
            imported=imported.rows_by_key,
            existing=existing_by_key,
            used_no_attack_keys=used_no_attack_keys,
        )
        result.append(
            CwlPlannedRow(
                row_key=row.row_key,
                season=row.season,
                clan_tag=row.clan_tag,
                round_number=row.round_number,
                attacker_tag=row.attacker_tag,
                marker=row.marker,
                technical_values=row.technical_values,
                no_attack_key=row.no_attack_key,
                old_alias_keys=row.old_alias_keys,
                user_values=user_values,
            ),
        )

    return tuple(result)


def _lookup_user_values(
    *,
    row: CwlPlannedRow,
    imported: dict[str, JsonValues],
    existing: dict[str, JsonValues],
    used_no_attack_keys: set[str],
) -> JsonValues:
    """Ищет user values по exact key, old aliases и NO_ATTACK transfer."""

    exact = imported.get(row.row_key) or existing.get(row.row_key)
    if exact is not None:
        return dict(exact)

    for alias_key in row.old_alias_keys:
        alias = imported.get(alias_key) or existing.get(alias_key)
        if alias is not None:
            return dict(alias)

    if row.marker == f"{ATTACK_MARKER_PREFIX}_1" and row.no_attack_key not in used_no_attack_keys:
        fallback = imported.get(row.no_attack_key) or existing.get(row.no_attack_key)
        if fallback is not None:
            used_no_attack_keys.add(row.no_attack_key)
            return dict(fallback)

    return {}


def _put_imported_values(
    rows_by_key: dict[str, JsonValues],
    row_key: str,
    user_values: JsonValues,
    warnings: list[str],
) -> None:
    """Кладёт imported user values, сохраняя первое значение при дубле."""

    if row_key in rows_by_key:
        warnings.append(f"Дубль CWL row key {row_key}; использована первая строка.")
        return
    rows_by_key[row_key] = dict(user_values)


def _row_key_from_bot_key(value: str) -> str | None:
    """Извлекает CWL row key из служебной колонки."""

    raw = value.strip()
    if raw.startswith(BOT_KEY_PREFIX):
        raw = raw.removeprefix(BOT_KEY_PREFIX)
    parts = raw.split("|")
    if len(parts) != CWL_ROW_KEY_PARTS_COUNT:
        return None
    season, clan_tag, round_raw, attacker_tag, marker = parts
    if not round_raw.isdigit() or marker == "":
        return None
    try:
        return make_cwl_row_key(
            season=season,
            clan_tag=clan_tag,
            round_number=int(round_raw),
            attacker_tag=attacker_tag,
            marker=marker,
        )
    except ValueError:
        return None


def _profiles(column_profiles: Sequence[ColumnProfile]) -> tuple[ColumnProfile, ...]:
    """Возвращает активный CWL column profile."""

    return tuple(
        profile
        for profile in column_profiles
        if profile.table_type == CWL_TABLE and profile.is_active
    )


def _user_indexes_from_header(
    profiles: Sequence[ColumnProfile],
    header_row: Sequence[str],
    block_start_index: int,
) -> dict[str, int]:
    """Ищет user column indexes по title в текущем заголовке блока."""

    title_to_indexes: dict[str, list[int]] = {}
    for index, title in enumerate(header_row):
        title_to_indexes.setdefault(title.strip(), []).append(index)

    result: dict[str, int] = {}
    for profile in profiles:
        if profile.kind != "user" or not profile.visible:
            continue
        indexes = title_to_indexes.get(profile.title)
        if not indexes:
            continue
        selected = next((index for index in indexes if index >= block_start_index), indexes[0])
        result[profile.column_key] = selected
    return result


def _default_system_indexes(header: CwlTableHeader) -> dict[str, int]:
    """Возвращает индексы system columns для текущего или старого CWL layout."""

    start = header.column_index
    round_index = start + 1
    return {
        "round": round_index,
        "attacker_tag": round_index + 1,
        "attacker_name": round_index + 2,
        "attacker_town_hall": round_index + 3,
        "defender_town_hall": round_index + 4,
        "stars": round_index + 5,
        "destruction_percentage": round_index + 6,
    }


def _find_next_header_row(headers: Sequence[CwlTableHeader], current: CwlTableHeader) -> int:
    """Ищет следующую таблицу в том же столбце."""

    candidates = [
        header.row_index
        for header in headers
        if header.column_index == current.column_index and header.row_index > current.row_index
    ]
    return min(candidates, default=10**9)


def _find_clan_tag_above(
    rows: Sequence[list[str]],
    header_row_index: int,
    column_index: int,
) -> str | None:
    """Ищет тег клана над таблицей CWL."""

    for row_index in range(header_row_index - 1, -1, -1):
        row = rows[row_index]
        segment = row[column_index : column_index + 12]
        if not any(cell.strip() for cell in segment):
            continue
        for cell in segment:
            for raw_part in cell.replace("|", " ").split():
                try:
                    return normalize_tag(raw_part)
                except ValueError:
                    continue
        return None
    return None


def _row_has_any_payload(row: Sequence[str], header: CwlTableHeader) -> bool:
    """Проверяет, похожа ли строка на заполненную CWL-строку."""

    segment = row[header.column_index : header.column_index + header.width]
    return any(cell.strip() for cell in segment)


def _last_non_empty_index(row: Sequence[str]) -> int:
    """Ищет индекс последней непустой ячейки."""

    for index in range(len(row) - 1, -1, -1):
        if row[index].strip():
            return index
    return 0


def _parse_defender_position(value: str) -> int | None:
    """Парсит defender map position из старого формата."""

    match = DEFENDER_POSITION_RE.match(value)
    if match is None:
        return None
    return int(match.group(1))


def _json_str(data: JsonObject, key: str) -> str:
    """Читает уже нормализованную строку из внутреннего JSON."""

    value = data.get(key)
    if not isinstance(value, str):
        raise CwlDataError(f"Внутреннее поле {key} должно быть строкой.")
    return value


def _json_int(data: JsonObject | None, key: str) -> int:
    """Читает уже нормализованное число из внутреннего JSON."""

    if data is None:
        raise CwlDataError(f"Внутреннее поле {key} отсутствует.")
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise CwlDataError(f"Внутреннее поле {key} должно быть числом.")
    return value


def _string_row(row: Sequence[CellValue]) -> list[str]:
    """Преобразует строку Google Sheets в строки."""

    return [_cell_to_str(cell) for cell in row]


def _cell_to_str(value: CellValue) -> str:
    """Преобразует значение ячейки в строку."""

    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _cell_at(row: Sequence[str], index: int) -> str:
    """Безопасно читает ячейку."""

    if index < 0 or index >= len(row):
        return ""
    return row[index]


def _require_str(data: JsonObject, key: str, context: str) -> str:
    """Читает обязательную строку API."""

    value = data.get(key)
    if not isinstance(value, str):
        raise ClashApiUnavailableError(f"{context}: поле {key} должно быть строкой.")
    return value


def _require_int(data: JsonObject, key: str, context: str) -> int:
    """Читает обязательное целое число API."""

    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ClashApiUnavailableError(f"{context}: поле {key} должно быть числом.")
    return value


def _require_list(data: JsonObject, key: str, context: str) -> list[Any]:
    """Читает обязательный список API."""

    value = data.get(key)
    if not isinstance(value, list):
        raise ClashApiUnavailableError(f"{context}: поле {key} должно быть списком.")
    return value


def _require_dict(data: JsonObject, key: str, context: str) -> JsonObject:
    """Читает обязательный объект API."""

    value = data.get(key)
    if not isinstance(value, dict):
        raise ClashApiUnavailableError(f"{context}: поле {key} должно быть объектом.")
    return value
