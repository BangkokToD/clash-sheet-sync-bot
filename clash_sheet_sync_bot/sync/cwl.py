"""Импорт, state-модель и запись CWL в публичной runtime-архитектуре."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from clash_sheet_sync_bot.coc.client import (
    ClashApiUnavailableError,
    ClashClient,
    ClashCwlNotInProgressError,
)
from clash_sheet_sync_bot.common.time import utc_now_iso as _utc_now_iso
from clash_sheet_sync_bot.models import (
    ColumnProfile,
    RuntimeChatConfig,
    SheetBlock,
    TableType,
    TrackedClan,
    normalize_tag,
)
from clash_sheet_sync_bot.repositories import (
    CwlRowState,
    CwlRowStateRepository,
    SheetBindingRepository,
    SheetBlockRepository,
)
from clash_sheet_sync_bot.sheets.client import (
    CellValue,
    SheetMetadata,
    SheetsClient,
    SheetValues,
    range_from_start_cell,
)
from clash_sheet_sync_bot.sheets.column_profiles import (
    BOT_KEY_COLUMN_KEY,
    BOT_KEY_TITLE,
    column_title_identity,
)
from clash_sheet_sync_bot.sheets.ranges import (
    column_to_number as _shared_column_to_number,
    grid_range_from_start_cell as _shared_grid_range_from_start_cell,
    number_to_column as _shared_number_to_column,
    offset_cell as _shared_offset_cell,
    parse_a1_cell as _shared_parse_a1_cell,
)
from clash_sheet_sync_bot.sync.composition import PlannedPlayerState

logger = logging.getLogger(__name__)

CWL_TABLE: Final[TableType] = "cwl"
CWL_BLOCK_PREFIX: Final = "cwl:"
CWL_MESSAGE_BLOCK_PREFIX: Final = "cwl_message:"
CWL_WIDE_IMPORT_RANGE: Final = "A1:ZZ1000"
CWL_ACTIVE_SHEET_NAME: Final = "CWL"
CWL_STAGING_SHEET_PREFIX: Final = "CWL - staging - "
CWL_ARCHIVE_SHEET_PREFIX: Final = "CWL - "
CWL_ELIGIBLE_WAR_STATES: Final = {"warEnded", "inWar"}
NO_ATTACK_MARKER: Final = "NO_ATTACK"
ATTACK_MARKER_PREFIX: Final = "ATTACK"
DEF_POS_MARKER_PREFIX: Final = "DEF_POS_"
CWL_ROW_KEY_PARTS_COUNT: Final = 5
BOT_KEY_PREFIX: Final = "cwl_row:"
TECHNICAL_HASH_VERSION: Final = "1"
TITLE_ROWS_COUNT: Final = 2
DEFAULT_CWL_START_CELL: Final = "A1"
BOT_STATE_SCHEMA_VERSION: Final = "1"
MANAGED_BY_VALUE: Final = "clash-sheet-sync-bot"

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

GREEN_RGB: Final = {"red": 0.18, "green": 0.42, "blue": 0.31}
DARK_GREEN_RGB: Final = {"red": 0.12, "green": 0.32, "blue": 0.24}
WHITE_RGB: Final = {"red": 1.0, "green": 1.0, "blue": 1.0}
BLACK_RGB: Final = {"red": 0.0, "green": 0.0, "blue": 0.0}
LIGHT_BAND_RGB: Final = {"red": 0.95, "green": 0.97, "blue": 0.96}
BORDER_RGB: Final = {"red": 0.70, "green": 0.76, "blue": 0.73}

JsonObject = dict[str, Any]
JsonValues = dict[str, str]


class CwlSyncError(RuntimeError):
    """Базовая ошибка CWL sync."""


class CwlDataError(CwlSyncError):
    """Ошибка данных CWL, при которой state или лист нельзя обновлять."""


class CwlSeasonMismatchError(CwlDataError):
    """Ошибка несовпадения CWL-сезонов у участвующих кланов."""

    stage: str = "проверка CWL-сезона"


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
    """Строка CWL, построенная из CoC API перед записью."""

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

    @property
    def sort_key(self) -> tuple[int, int, int, str]:
        """Возвращает ключ сортировки строки внутри CWL-таблицы."""

        return (
            self.round_number,
            self.technical_values.attacker_map_position,
            _marker_sort_index(self.marker),
            self.attacker_tag,
        )


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
class CwlDiffItem:
    """Одно техническое изменение CWL."""

    kind: str
    message: str


@dataclass(frozen=True, slots=True)
class CwlClanBlock:
    """Будущий CWL-блок одного active clan."""

    clan: TrackedClan
    rows: tuple[CwlPlannedRow, ...]
    message: str | None = None
    rounds_count: int = 0

    @property
    def block_key(self) -> str:
        """Возвращает block_key для `sheet_blocks`."""

        prefix = CWL_MESSAGE_BLOCK_PREFIX if self.message is not None else CWL_BLOCK_PREFIX
        return f"{prefix}{self.clan.clan_tag}"


@dataclass(frozen=True, slots=True)
class BuiltCwlBlock:
    """Построенный блок CWL-листа."""

    block: SheetBlock
    values: list[list[CellValue]]
    has_table: bool


@dataclass(frozen=True, slots=True)
class CwlPreparedData:
    """Подготовленные CWL-данные до записи в Google Sheets."""

    season: str | None
    clan_blocks: tuple[CwlClanBlock, ...]
    rows: tuple[CwlPlannedRow, ...]
    all_not_in_progress: bool
    not_in_progress_clans: tuple[TrackedClan, ...]
    warnings: tuple[str, ...]
    diff_items: tuple[CwlDiffItem, ...] = ()


@dataclass(frozen=True, slots=True)
class CwlStateSyncResult:
    """Результат обновления CWL row state без записи листа."""

    season: str | None
    rows_count: int
    all_not_in_progress: bool
    not_in_progress_clans: tuple[TrackedClan, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CwlSheetSyncResult:
    """Результат публичной записи CWL-листа."""

    season: str | None
    rows_count: int
    blocks_count: int
    all_not_in_progress: bool
    archived_previous_season: bool = False
    not_in_progress_clans: tuple[TrackedClan, ...] = ()
    warnings: tuple[str, ...] = ()
    diff_items: tuple[CwlDiffItem, ...] = ()


@dataclass(frozen=True, slots=True)
class CwlTableHeader:
    """Найденный блок текущего CWL-листа."""

    row_index: int
    column_index: int
    width: int
    clan_tag: str | None
    system_indexes: dict[str, int]


async def run_cwl_state_sync(
    *,
    runtime_config: RuntimeChatConfig,
    clash_client: ClashClient,
    sheets_client: SheetsClient,
    cwl_repository: CwlRowStateRepository,
    sheet_block_repository: SheetBlockRepository,
    cwl_war_concurrency_limit: int,
) -> CwlStateSyncResult:
    """Обновляет `cwl_row_state` без записи Google Sheets."""

    prepared = await _prepare_cwl_data(
        runtime_config=runtime_config,
        clash_client=clash_client,
        sheets_client=sheets_client,
        cwl_repository=cwl_repository,
        sheet_block_repository=sheet_block_repository,
        cwl_war_concurrency_limit=cwl_war_concurrency_limit,
    )
    if prepared.all_not_in_progress:
        return CwlStateSyncResult(
            season=None,
            rows_count=0,
            all_not_in_progress=True,
            not_in_progress_clans=prepared.not_in_progress_clans,
            warnings=prepared.warnings,
        )

    await _upsert_cwl_row_states(
        runtime_config=runtime_config,
        cwl_repository=cwl_repository,
        rows=prepared.rows,
    )
    return CwlStateSyncResult(
        season=prepared.season,
        rows_count=len(prepared.rows),
        all_not_in_progress=False,
        not_in_progress_clans=prepared.not_in_progress_clans,
        warnings=prepared.warnings,
    )


async def run_public_cwl_sync(
    *,
    runtime_config: RuntimeChatConfig,
    clash_client: ClashClient,
    sheets_client: SheetsClient,
    cwl_repository: CwlRowStateRepository,
    sheet_block_repository: SheetBlockRepository,
    sheet_binding_repository: SheetBindingRepository,
    sync_run_id: int,
    cwl_war_concurrency_limit: int,
    composition_player_states: Sequence[PlannedPlayerState] = (),
) -> CwlSheetSyncResult:
    """Обновляет публичный CWL-лист и `cwl_row_state`.

    Если CWL не проводится у всех активных кланов, лист CWL не меняется.
    Если сезон изменился, новая версия пишется через staging-лист.
    """

    prepared = await prepare_public_cwl_sync(
        runtime_config=runtime_config,
        clash_client=clash_client,
        sheets_client=sheets_client,
        cwl_repository=cwl_repository,
        sheet_block_repository=sheet_block_repository,
        cwl_war_concurrency_limit=cwl_war_concurrency_limit,
        composition_player_states=composition_player_states,
    )
    return await apply_public_cwl_sync(
        runtime_config=runtime_config,
        sheets_client=sheets_client,
        cwl_repository=cwl_repository,
        sheet_block_repository=sheet_block_repository,
        sheet_binding_repository=sheet_binding_repository,
        sync_run_id=sync_run_id,
        prepared=prepared,
    )


async def prepare_public_cwl_sync(
    *,
    runtime_config: RuntimeChatConfig,
    clash_client: ClashClient,
    sheets_client: SheetsClient,
    cwl_repository: CwlRowStateRepository,
    sheet_block_repository: SheetBlockRepository,
    cwl_war_concurrency_limit: int,
    composition_player_states: Sequence[PlannedPlayerState] = (),
) -> CwlPreparedData:
    """Готовит CWL-данные без записи в Google Sheets и SQLite."""

    return await _prepare_cwl_data(
        runtime_config=runtime_config,
        clash_client=clash_client,
        sheets_client=sheets_client,
        cwl_repository=cwl_repository,
        sheet_block_repository=sheet_block_repository,
        cwl_war_concurrency_limit=cwl_war_concurrency_limit,
    )


async def apply_public_cwl_sync(
    *,
    runtime_config: RuntimeChatConfig,
    sheets_client: SheetsClient,
    cwl_repository: CwlRowStateRepository,
    sheet_block_repository: SheetBlockRepository,
    sheet_binding_repository: SheetBindingRepository,
    sync_run_id: int,
    prepared: CwlPreparedData,
) -> CwlSheetSyncResult:
    """Записывает подготовленный CWL в Google Sheets и SQLite."""

    if prepared.all_not_in_progress or prepared.season is None:
        return CwlSheetSyncResult(
            season=None,
            rows_count=0,
            blocks_count=0,
            all_not_in_progress=True,
            not_in_progress_clans=prepared.not_in_progress_clans,
            warnings=prepared.warnings,
        )

    columns = _physical_columns(runtime_config.column_profiles)
    old_season = runtime_config.sheet_binding.active_cwl_season
    season_changed = old_season is not None and old_season != prepared.season

    if season_changed:
        active_sheet = await _write_cwl_with_staging_archive(
            runtime_config=runtime_config,
            sheets_client=sheets_client,
            prepared=prepared,
            columns=columns,
            sync_run_id=sync_run_id,
            old_season=old_season,
        )
    else:
        active_sheet = await _rewrite_active_cwl_sheet(
            runtime_config=runtime_config,
            sheets_client=sheets_client,
            sheet_block_repository=sheet_block_repository,
            prepared=prepared,
            columns=columns,
        )

    built_blocks = build_cwl_sheet_blocks(
        runtime_config=runtime_config,
        sheet_name=active_sheet.title,
        sheet_id=active_sheet.sheet_id,
        prepared=prepared,
        columns=columns,
    )
    await _write_bot_state(
        runtime_config=runtime_config,
        sheets_client=sheets_client,
        active_cwl_sheet_name=active_sheet.title,
        active_cwl_sheet_id=active_sheet.sheet_id,
        active_cwl_season=prepared.season,
    )
    await sheet_binding_repository.update_active_cwl_binding(
        chat_id=runtime_config.chat_id,
        active_cwl_sheet_name=active_sheet.title,
        active_cwl_sheet_id=active_sheet.sheet_id,
        active_cwl_season=prepared.season,
        now=_utc_now_iso(),
    )
    await sheet_block_repository.replace_blocks_by_prefixes(
        chat_id=runtime_config.chat_id,
        sheet_name=active_sheet.title,
        block_key_prefixes=(CWL_BLOCK_PREFIX, CWL_MESSAGE_BLOCK_PREFIX),
        blocks=tuple(block.block for block in built_blocks),
        updated_at=_utc_now_iso(),
    )
    await _upsert_cwl_row_states(
        runtime_config=runtime_config,
        cwl_repository=cwl_repository,
        rows=prepared.rows,
    )

    return CwlSheetSyncResult(
        season=prepared.season,
        rows_count=len(prepared.rows),
        blocks_count=len(prepared.clan_blocks),
        all_not_in_progress=False,
        archived_previous_season=season_changed,
        not_in_progress_clans=prepared.not_in_progress_clans,
        warnings=prepared.warnings,
        diff_items=prepared.diff_items,
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
            if block.block_key.startswith(CWL_MESSAGE_BLOCK_PREFIX):
                continue
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


def build_cwl_sheet_blocks(
    *,
    runtime_config: RuntimeChatConfig,
    sheet_name: str,
    sheet_id: int | None,
    prepared: CwlPreparedData,
    columns: Sequence[ColumnProfile],
) -> tuple[BuiltCwlBlock, ...]:
    """Строит блоки CWL для записи в Google Sheets."""

    built_blocks: list[BuiltCwlBlock] = []
    row_cursor = 3
    width = len(columns)

    for index, clan_block in enumerate(prepared.clan_blocks):
        if index > 0:
            row_cursor += 1

        if clan_block.message is None:
            values = _build_cwl_table_values(clan_block=clan_block, columns=columns)
            block_key = f"{CWL_BLOCK_PREFIX}{clan_block.clan.clan_tag}"
            has_table = True
        else:
            values = _build_cwl_message_values(clan_block=clan_block, columns=columns)
            block_key = f"{CWL_MESSAGE_BLOCK_PREFIX}{clan_block.clan.clan_tag}"
            has_table = False

        block = SheetBlock(
            chat_id=runtime_config.chat_id,
            sheet_name=sheet_name,
            sheet_id=sheet_id,
            block_key=block_key,
            start_cell=f"A{row_cursor}",
            rows_count=len(values),
            columns_count=width,
        )
        built_blocks.append(BuiltCwlBlock(block=block, values=values, has_table=has_table))
        row_cursor += len(values)

    return tuple(built_blocks)


def build_cwl_sheet_matrix(
    *,
    prepared: CwlPreparedData,
    columns: Sequence[ColumnProfile],
    built_blocks: Sequence[BuiltCwlBlock],
) -> list[list[CellValue]]:
    """Строит полную матрицу активного CWL-листа."""

    if prepared.season is None:
        raise CwlDataError("Нельзя строить CWL-лист без сезона.")

    width = len(columns)
    matrix: list[list[CellValue]] = [
        _title_row(f"CWL season: {prepared.season}", width),
        _empty_row(width),
    ]
    current_row = 3
    for index, built_block in enumerate(built_blocks):
        if index > 0:
            matrix.append(_empty_row(width))
            current_row += 1
        expected_row = _row_number_from_start_cell(built_block.block.start_cell)
        while current_row < expected_row:
            matrix.append(_empty_row(width))
            current_row += 1
        matrix.extend(built_block.values)
        current_row += len(built_block.values)
    return matrix


async def _prepare_cwl_data(
    *,
    runtime_config: RuntimeChatConfig,
    clash_client: ClashClient,
    sheets_client: SheetsClient,
    cwl_repository: CwlRowStateRepository,
    sheet_block_repository: SheetBlockRepository,
    cwl_war_concurrency_limit: int,
    composition_player_states: Sequence[PlannedPlayerState] = (),
) -> CwlPreparedData:
    """Загружает CoC/Sheets/SQLite и готовит CWL rows."""

    if not runtime_config.active_clans:
        raise CwlDataError("Для CWL sync нужен хотя бы один активный клан.")

    league_groups = await _load_league_groups(runtime_config.active_clans, clash_client)
    participating_groups = {
        clan_tag: group for clan_tag, group in league_groups.items() if group is not None
    }
    not_in_progress_clans = tuple(
        clan for clan in runtime_config.active_clans if league_groups.get(clan.clan_tag) is None
    )
    if not participating_groups:
        return CwlPreparedData(
            season=None,
            clan_blocks=(),
            rows=(),
            all_not_in_progress=True,
            not_in_progress_clans=not_in_progress_clans,
            warnings=(),
            diff_items=(),
        )

    season = _resolve_cwl_season(runtime_config.active_clans, participating_groups)
    sheet_name = runtime_config.sheet_binding.active_cwl_sheet_name
    previous_blocks = await sheet_block_repository.list_blocks(runtime_config.chat_id, sheet_name)
    cwl_blocks = tuple(
        block
        for block in previous_blocks
        if block.block_key.startswith(CWL_BLOCK_PREFIX)
        or block.block_key.startswith(CWL_MESSAGE_BLOCK_PREFIX)
    )
    imported = await import_current_cwl_sheet(
        runtime_config=runtime_config,
        sheets_client=sheets_client,
        blocks=cwl_blocks,
        season=season,
    )

    war_tags = _collect_unique_war_tags(participating_groups.values())
    wars_by_tag = await _load_cwl_wars(
        clash_client=clash_client,
        war_tags=war_tags,
        concurrency_limit=cwl_war_concurrency_limit,
    )
    clan_blocks = _build_cwl_clan_blocks(
        runtime_config=runtime_config,
        season=season,
        league_groups=league_groups,
        wars_by_tag=wars_by_tag,
    )
    planned_rows = tuple(row for block in clan_blocks for row in block.rows)
    existing_rows = await cwl_repository.list_rows(
        chat_id=runtime_config.chat_id,
        season=season,
    )
    rows_with_user_values = _apply_user_values(
        planned_rows=planned_rows,
        imported=imported,
        existing_rows=existing_rows,
        composition_user_values_by_player=_composition_user_values_by_player(
            composition_player_states,
        ),
        cwl_composition_user_column_links=_cwl_composition_user_column_links(
            runtime_config.column_profiles,
        ),
    )
    diff_items = _build_cwl_diff_items(
        planned_rows=rows_with_user_values,
        existing_rows=existing_rows,
    )
    rows_by_key = {row.row_key: row for row in rows_with_user_values}
    blocks_with_user_values = tuple(
        CwlClanBlock(
            clan=block.clan,
            rows=tuple(rows_by_key[row.row_key] for row in block.rows),
            message=block.message,
            rounds_count=block.rounds_count,
        )
        for block in clan_blocks
    )

    return CwlPreparedData(
        season=season,
        clan_blocks=blocks_with_user_values,
        rows=rows_with_user_values,
        all_not_in_progress=False,
        not_in_progress_clans=not_in_progress_clans,
        warnings=imported.warnings,
        diff_items=diff_items,
    )


async def _rewrite_active_cwl_sheet(
    *,
    runtime_config: RuntimeChatConfig,
    sheets_client: SheetsClient,
    sheet_block_repository: SheetBlockRepository,
    prepared: CwlPreparedData,
    columns: Sequence[ColumnProfile],
) -> SheetMetadata:
    """Перезаписывает текущий active CWL-лист без архивирования."""

    active_sheet = await _resolve_active_cwl_sheet(runtime_config, sheets_client)
    built_blocks = build_cwl_sheet_blocks(
        runtime_config=runtime_config,
        sheet_name=active_sheet.title,
        sheet_id=active_sheet.sheet_id,
        prepared=prepared,
        columns=columns,
    )
    matrix = build_cwl_sheet_matrix(
        prepared=prepared,
        columns=columns,
        built_blocks=built_blocks,
    )
    previous_blocks = await sheet_block_repository.list_blocks(
        runtime_config.chat_id, active_sheet.title
    )
    previous_cwl_blocks = tuple(
        block
        for block in previous_blocks
        if block.block_key.startswith(CWL_BLOCK_PREFIX)
        or block.block_key.startswith(CWL_MESSAGE_BLOCK_PREFIX)
    )
    await _rewrite_cwl_values(
        sheets_client=sheets_client,
        sheet_name=active_sheet.title,
        previous_blocks=previous_cwl_blocks,
        matrix=matrix,
    )
    await _format_cwl_sheet(
        sheets_client=sheets_client,
        sheet_id=active_sheet.sheet_id,
        matrix_rows_count=len(matrix),
        columns_count=len(columns),
        built_blocks=built_blocks,
    )
    await _hide_bot_key_column(sheets_client=sheets_client, sheet_id=active_sheet.sheet_id)
    return active_sheet


async def _write_cwl_with_staging_archive(
    *,
    runtime_config: RuntimeChatConfig,
    sheets_client: SheetsClient,
    prepared: CwlPreparedData,
    columns: Sequence[ColumnProfile],
    sync_run_id: int,
    old_season: str,
) -> SheetMetadata:
    """Создаёт новый CWL через staging и архивирует старый активный лист."""

    old_active = await _resolve_active_cwl_sheet(runtime_config, sheets_client)
    staging_title = f"{CWL_STAGING_SHEET_PREFIX}{sync_run_id}"
    staging = await sheets_client.add_sheet(staging_title)
    staging_blocks = build_cwl_sheet_blocks(
        runtime_config=runtime_config,
        sheet_name=staging.title,
        sheet_id=staging.sheet_id,
        prepared=prepared,
        columns=columns,
    )
    matrix = build_cwl_sheet_matrix(
        prepared=prepared,
        columns=columns,
        built_blocks=staging_blocks,
    )

    await sheets_client.write_values(
        sheet_name=staging.title,
        range_a1=range_from_start_cell(
            start_cell=DEFAULT_CWL_START_CELL,
            rows_count=len(matrix),
            columns_count=len(columns),
        ),
        values=matrix,
    )
    await _format_cwl_sheet(
        sheets_client=sheets_client,
        sheet_id=staging.sheet_id,
        matrix_rows_count=len(matrix),
        columns_count=len(columns),
        built_blocks=staging_blocks,
    )
    await _hide_bot_key_column(sheets_client=sheets_client, sheet_id=staging.sheet_id)

    metadata = await sheets_client.get_spreadsheet_metadata()
    archive_title = _unique_sheet_title(
        base_title=f"{CWL_ARCHIVE_SHEET_PREFIX}{old_season}",
        existing_titles={
            sheet.title for sheet in metadata.sheets if sheet.sheet_id != old_active.sheet_id
        },
    )
    await sheets_client.rename_sheet(old_active.sheet_id, archive_title)
    await sheets_client.rename_sheet(staging.sheet_id, CWL_ACTIVE_SHEET_NAME)
    await _move_cwl_before_composition(
        runtime_config=runtime_config,
        sheets_client=sheets_client,
        cwl_sheet_id=staging.sheet_id,
    )
    return SheetMetadata(sheet_id=staging.sheet_id, title=CWL_ACTIVE_SHEET_NAME)


async def _rewrite_cwl_values(
    *,
    sheets_client: SheetsClient,
    sheet_name: str,
    previous_blocks: Sequence[SheetBlock],
    matrix: Sequence[Sequence[CellValue]],
) -> None:
    """Очищает прошлые CWL-блоки и записывает новую матрицу."""

    updates: list[SheetValues] = []
    for block in previous_blocks:
        if block.rows_count <= 0 or block.columns_count <= 0:
            continue
        updates.append(
            SheetValues(
                sheet_name=sheet_name,
                range_a1=range_from_start_cell(
                    start_cell=block.start_cell,
                    rows_count=block.rows_count,
                    columns_count=block.columns_count,
                ),
                values=[["" for _ in range(block.columns_count)] for _ in range(block.rows_count)],
            ),
        )
    updates.append(
        SheetValues(
            sheet_name=sheet_name,
            range_a1=range_from_start_cell(
                start_cell=DEFAULT_CWL_START_CELL,
                rows_count=len(matrix),
                columns_count=len(matrix[0]) if matrix else 1,
            ),
            values=matrix,
        ),
    )
    await sheets_client.batch_update_values(updates)


async def _upsert_cwl_row_states(
    *,
    runtime_config: RuntimeChatConfig,
    cwl_repository: CwlRowStateRepository,
    rows: Sequence[CwlPlannedRow],
) -> None:
    """Сохраняет planned rows в `cwl_row_state`."""

    for row in rows:
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


def _resolve_cwl_season(
    active_clans: Sequence[TrackedClan],
    groups: dict[str, JsonObject],
) -> str:
    """Определяет активный CWL-сезон по participating league groups.

    Если API вернул разные сезоны для активных кланов, sync отменяется.
    """

    season_by_clan_tag = {
        clan_tag: _require_str(group, "season", "leaguegroup") for clan_tag, group in groups.items()
    }
    seasons = set(season_by_clan_tag.values())
    if not seasons:
        raise CwlDataError("Не удалось определить CWL-сезон.")
    if len(seasons) == 1:
        return next(iter(seasons))

    details = []
    for clan in active_clans:
        season = season_by_clan_tag.get(clan.clan_tag)
        if season is None:
            continue
        details.append(f"{clan.clan_name} | {clan.clan_tag}: {season}")

    raise CwlSeasonMismatchError(
        "CWL-сезоны активных кланов не совпадают: " + "; ".join(details) + ".",
    )


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
    *,
    clash_client: ClashClient,
    war_tags: Sequence[str],
    concurrency_limit: int,
) -> dict[str, JsonObject]:
    """Загружает CWL wars с ограничением конкурентности."""

    if concurrency_limit <= 0:
        raise CwlDataError("CWL war concurrency limit должен быть положительным.")

    semaphore = asyncio.Semaphore(concurrency_limit)

    async def load_one(war_tag: str) -> tuple[str, JsonObject]:
        async with semaphore:
            return war_tag, await clash_client.get_cwl_war(war_tag)

    loaded = await asyncio.gather(*(load_one(war_tag) for war_tag in war_tags))
    return dict(loaded)


def _build_cwl_clan_blocks(
    *,
    runtime_config: RuntimeChatConfig,
    season: str,
    league_groups: dict[str, JsonObject | None],
    wars_by_tag: dict[str, JsonObject],
) -> tuple[CwlClanBlock, ...]:
    """Строит CWL-блоки по active clans."""

    blocks: list[CwlClanBlock] = []
    for clan in runtime_config.active_clans:
        group = league_groups.get(clan.clan_tag)
        if group is None:
            blocks.append(CwlClanBlock(clan=clan, rows=(), message="CWL не проводится"))
            continue

        rows, rounds_count = _build_clan_rows(
            clan=clan,
            group=group,
            season=season,
            wars_by_tag=wars_by_tag,
        )
        if not rows:
            blocks.append(
                CwlClanBlock(
                    clan=clan,
                    rows=(),
                    message="Нет завершённых или текущих раундов",
                    rounds_count=0,
                ),
            )
            continue

        blocks.append(
            CwlClanBlock(
                clan=clan,
                rows=tuple(sorted(rows, key=lambda row: row.sort_key)),
                rounds_count=rounds_count,
            ),
        )
    return tuple(blocks)


def _build_clan_rows(
    *,
    clan: TrackedClan,
    group: JsonObject,
    season: str,
    wars_by_tag: dict[str, JsonObject],
) -> tuple[list[CwlPlannedRow], int]:
    """Строит строки CWL одного клана."""

    rows: list[CwlPlannedRow] = []
    included_rounds: set[int] = set()
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

            included_rounds.add(round_number)
            rows.extend(
                _build_war_rows(
                    season=season,
                    clan_tag=clan.clan_tag,
                    round_number=round_number,
                    our_side=side[0],
                    opponent_side=side[1],
                ),
            )

    return rows, len(included_rounds)


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
        attacker_tag = _json_str(member, "tag")
        no_attack_key = make_cwl_row_key(
            season=season,
            clan_tag=clan_tag,
            round_number=round_number,
            attacker_tag=attacker_tag,
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
                attacker_tag=attacker_tag,
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

        for row_offset, row in enumerate(
            rows[header.row_index + 1 : next_row], start=header.row_index + 2
        ):
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
    """Ищет таблицы CWL в values.

    В строке заголовка обычно есть и `__bot_key`, и `Раунд`. Оба признака
    указывают на одну и ту же таблицу, поэтому найденные заголовки нужно
    дедуплицировать по строке и стартовой колонке. Иначе импорт обходит один
    CWL-блок дважды и создаёт ложные warnings о дублях `row_key`.
    """

    profiles = _profiles(runtime_config.column_profiles)
    headers: list[CwlTableHeader] = []
    seen_positions: set[tuple[int, int]] = set()
    for row_index, row in enumerate(rows):
        for column_index, cell in enumerate(row):
            normalized = cell.strip()
            if normalized != BOT_KEY_TITLE and normalized not in SYSTEM_HEADER_ALIASES["round"]:
                continue
            start_column_index = (
                column_index if normalized == BOT_KEY_TITLE else max(column_index - 1, 0)
            )
            position = (row_index, start_column_index)
            if position in seen_positions:
                continue
            system_indexes = _system_indexes_from_header(profiles, row, start_column_index)
            if not _looks_like_cwl_header(system_indexes):
                continue
            seen_positions.add(position)
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
                    system_indexes=system_indexes,
                ),
            )

    return tuple(headers)


def _looks_like_cwl_header(system_indexes: dict[str, int]) -> bool:
    """Проверяет, похожа ли строка на заголовок CWL-таблицы."""

    required = {"round", "attacker_tag", "attacker_name"}
    return required.issubset(system_indexes)


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

    if block_key is not None:
        for prefix in (CWL_BLOCK_PREFIX, CWL_MESSAGE_BLOCK_PREFIX):
            if block_key.startswith(prefix):
                raw_tag = block_key.removeprefix(prefix)
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
    user_values = {column_key: _cell_at(row, index) for column_key, index in user_indexes.items()}

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
    round_index = header.system_indexes.get("round")
    attacker_index = header.system_indexes.get("attacker_tag")
    if round_index is None or attacker_index is None:
        return None

    round_raw = _cell_at(row, round_index).strip()
    attacker_raw = _cell_at(row, attacker_index).strip()
    if not round_raw.isdigit() or attacker_raw == "":
        return None

    try:
        round_number = int(round_raw)
        attacker_tag = normalize_tag(attacker_raw)
    except ValueError:
        return None

    defender_raw = _cell_at(row, header.system_indexes.get("defender_town_hall", -1)).strip()
    stars_raw = _cell_at(row, header.system_indexes.get("stars", -1)).strip()
    destruction_raw = _cell_at(row, header.system_indexes.get("destruction_percentage", -1)).strip()

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
    composition_user_values_by_player: dict[str, JsonValues] | None = None,
    cwl_composition_user_column_links: dict[str, tuple[str, ...]] | None = None,
) -> tuple[CwlPlannedRow, ...]:
    """Переносит user fields из CWL state и пустые значения из состава."""

    existing_by_key = {row.row_key: row.user_values for row in existing_rows}
    used_no_attack_keys: set[str] = set()
    result: list[CwlPlannedRow] = []
    composition_user_values_by_player = composition_user_values_by_player or {}
    cwl_composition_user_column_links = cwl_composition_user_column_links or {}

    for row in planned_rows:
        user_values = _lookup_user_values(
            row=row,
            imported=imported.rows_by_key,
            existing=existing_by_key,
            used_no_attack_keys=used_no_attack_keys,
        )
        user_values = _fill_empty_cwl_user_values_from_composition(
            row=row,
            user_values=user_values,
            composition_user_values_by_player=composition_user_values_by_player,
            cwl_composition_user_column_links=cwl_composition_user_column_links,
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


def _fill_empty_cwl_user_values_from_composition(
    *,
    row: CwlPlannedRow,
    user_values: JsonValues,
    composition_user_values_by_player: dict[str, JsonValues],
    cwl_composition_user_column_links: dict[str, tuple[str, ...]],
) -> JsonValues:
    """Подставляет пустые CWL user-values из состава по совпадающему title."""

    if not cwl_composition_user_column_links:
        return dict(user_values)

    result = dict(user_values)
    composition_values = composition_user_values_by_player.get(row.attacker_tag, {})
    if not composition_values:
        return result

    for cwl_column_key, composition_column_keys in cwl_composition_user_column_links.items():
        if _is_non_empty_user_value(result.get(cwl_column_key)):
            continue
        for composition_column_key in composition_column_keys:
            composition_value = composition_values.get(composition_column_key)
            if _is_non_empty_user_value(composition_value):
                result[cwl_column_key] = composition_value
                break

    return result


def _is_non_empty_user_value(value: str | None) -> bool:
    """Проверяет, считается ли user-value заполненным."""

    return value is not None and value.strip() != ""


def _composition_user_values_by_player(
    states: Sequence[PlannedPlayerState],
) -> dict[str, JsonValues]:
    """Индексирует composition user-values по player tag."""

    return {state.player_tag: dict(state.user_values) for state in states}


def _cwl_composition_user_column_links(
    column_profiles: Sequence[ColumnProfile],
) -> dict[str, tuple[str, ...]]:
    """Связывает CWL user columns с composition user columns по title."""

    composition_title_to_keys: dict[str, list[str]] = {}
    composition_table_order: dict[TableType, int] = {
        "composition_active": 0,
        "composition_exited": 1,
        "composition": 2,
        "cwl": 3,
    }
    composition_profiles = sorted(
        (
            profile
            for profile in column_profiles
            if profile.table_type in {"composition_active", "composition_exited", "composition"}
            and profile.kind == "user"
            and profile.visible
            and profile.is_active
        ),
        key=lambda profile: (
            composition_table_order.get(profile.table_type, 99),
            profile.sort_order,
            profile.column_key,
        ),
    )
    for profile in composition_profiles:
        composition_title_to_keys.setdefault(
            column_title_identity(profile.title),
            [],
        ).append(profile.column_key)

    result: dict[str, tuple[str, ...]] = {}
    cwl_profiles = sorted(
        (
            profile
            for profile in column_profiles
            if profile.table_type == CWL_TABLE
            and profile.kind == "user"
            and profile.visible
            and profile.is_active
        ),
        key=lambda profile: (profile.sort_order, profile.column_key),
    )
    for profile in cwl_profiles:
        composition_column_keys = composition_title_to_keys.get(
            column_title_identity(profile.title)
        )
        if composition_column_keys:
            result[profile.column_key] = tuple(composition_column_keys)

    return result


def _build_cwl_diff_items(
    *,
    planned_rows: Sequence[CwlPlannedRow],
    existing_rows: Sequence[CwlRowState],
) -> tuple[CwlDiffItem, ...]:
    """Строит технический diff CWL по row_hash."""

    existing_by_key = {row.row_key: row for row in existing_rows}
    items: list[CwlDiffItem] = []
    for row in planned_rows:
        existing = existing_by_key.get(row.row_key)
        if existing is None:
            items.append(CwlDiffItem("added", _new_cwl_row_message(row)))
            continue
        if existing.row_hash is not None and existing.row_hash != row.row_hash:
            items.append(CwlDiffItem("updated", _updated_cwl_row_message(row)))
    return tuple(items)


def _new_cwl_row_message(row: CwlPlannedRow) -> str:
    """Формирует текст новой CWL-строки."""

    technical = row.technical_values
    if row.marker == NO_ATTACK_MARKER:
        return f"Без атаки: {technical.attacker_name} | Раунд {technical.round_number}."
    target = (
        format_town_hall(technical.defender_town_hall)
        if technical.defender_town_hall is not None
        else "-"
    )
    stars = technical.stars if technical.stars is not None else "-"
    destruction = (
        technical.destruction_percentage if technical.destruction_percentage is not None else "-"
    )
    return f"Атака: {technical.attacker_name} → {target} | {stars}⭐ {destruction}%."


def _updated_cwl_row_message(row: CwlPlannedRow) -> str:
    """Формирует текст обновления CWL-строки."""

    technical = row.technical_values
    if row.marker == NO_ATTACK_MARKER:
        return f"Без атаки обновлено: {technical.attacker_name} | Раунд {technical.round_number}."
    target = (
        format_town_hall(technical.defender_town_hall)
        if technical.defender_town_hall is not None
        else "-"
    )
    stars = technical.stars if technical.stars is not None else "-"
    destruction = (
        technical.destruction_percentage if technical.destruction_percentage is not None else "-"
    )
    return f"Атака обновлена: {technical.attacker_name} → {target} | {stars}⭐ {destruction}%."


def _physical_columns(column_profiles: Sequence[ColumnProfile]) -> tuple[ColumnProfile, ...]:
    """Возвращает физические CWL-колонки: service + visible non-service."""

    profiles = sorted(
        (
            profile
            for profile in column_profiles
            if profile.table_type == CWL_TABLE and profile.is_active
        ),
        key=lambda profile: (profile.sort_order, profile.column_key),
    )
    service = [
        profile
        for profile in profiles
        if profile.column_key == BOT_KEY_COLUMN_KEY and profile.kind == "service"
    ]
    if not service:
        raise CwlDataError(f"Профиль CWL не содержит service-колонку {BOT_KEY_TITLE}.")
    visible = [profile for profile in profiles if profile.kind != "service" and profile.visible]
    return tuple([service[0], *visible])


def _build_cwl_table_values(
    *,
    clan_block: CwlClanBlock,
    columns: Sequence[ColumnProfile],
) -> list[list[CellValue]]:
    """Строит values одного табличного CWL-блока."""

    title = f"{clan_block.clan.clan_name} | {clan_block.clan.clan_tag}"
    values: list[list[CellValue]] = [
        _title_row(title, len(columns)),
        [column.title for column in columns],
    ]
    values.extend(_cwl_row_to_values(row=row, columns=columns) for row in clan_block.rows)
    if len(values) == TITLE_ROWS_COUNT:
        values.append(_empty_row(len(columns)))
    return values


def _build_cwl_message_values(
    *,
    clan_block: CwlClanBlock,
    columns: Sequence[ColumnProfile],
) -> list[list[CellValue]]:
    """Строит values CWL-блока со служебным сообщением."""

    title = f"{clan_block.clan.clan_name} | {clan_block.clan.clan_tag}"
    message = clan_block.message or ""
    return [
        _title_row(title, len(columns)),
        _message_row(message, len(columns)),
    ]


def _cwl_row_to_values(*, row: CwlPlannedRow, columns: Sequence[ColumnProfile]) -> list[CellValue]:
    """Преобразует planned row в строку Google Sheets."""

    values: list[CellValue] = []
    technical = row.technical_values
    for column in columns:
        if column.kind == "service" and column.column_key == BOT_KEY_COLUMN_KEY:
            values.append(f"{BOT_KEY_PREFIX}{row.row_key}")
        elif column.kind == "user":
            values.append(row.user_values.get(column.column_key, ""))
        elif column.column_key == "round":
            values.append(technical.round_number)
        elif column.column_key == "attacker_tag":
            values.append(technical.attacker_tag)
        elif column.column_key == "attacker_name":
            values.append(technical.attacker_name)
        elif column.column_key == "attacker_town_hall":
            values.append(format_town_hall(technical.attacker_town_hall))
        elif column.column_key == "defender_town_hall":
            values.append(
                format_town_hall(technical.defender_town_hall)
                if technical.defender_town_hall is not None
                else ""
            )
        elif column.column_key == "stars":
            values.append(technical.stars if technical.stars is not None else "")
        elif column.column_key == "destruction_percentage":
            values.append(
                technical.destruction_percentage
                if technical.destruction_percentage is not None
                else ""
            )
        else:
            values.append("")
    return values


def format_town_hall(town_hall: int) -> str:
    """Форматирует ратушу для CWL без номера карты."""

    return f"TH{town_hall}"


async def _format_cwl_sheet(
    *,
    sheets_client: SheetsClient,
    sheet_id: int,
    matrix_rows_count: int,
    columns_count: int,
    built_blocks: Sequence[BuiltCwlBlock],
) -> None:
    """Форматирует управляемые CWL-блоки."""

    requests = _build_cwl_format_requests(
        sheet_id=sheet_id,
        matrix_rows_count=matrix_rows_count,
        columns_count=columns_count,
        built_blocks=built_blocks,
    )
    await sheets_client.batch_update_spreadsheet(requests)


def _build_cwl_format_requests(
    *,
    sheet_id: int,
    matrix_rows_count: int,
    columns_count: int,
    built_blocks: Sequence[BuiltCwlBlock],
) -> list[JsonObject]:
    """Строит Google Sheets formatting requests."""

    requests: list[JsonObject] = []
    if matrix_rows_count > 0:
        requests.append(
            _repeat_cell_request(
                _grid_range_from_start_cell(
                    sheet_id=sheet_id,
                    start_cell=DEFAULT_CWL_START_CELL,
                    rows_count=matrix_rows_count,
                    columns_count=columns_count,
                ),
                _base_cell_format(),
                "userEnteredFormat(backgroundColorStyle,textFormat,verticalAlignment,wrapStrategy)",
            ),
        )
        requests.append(
            _repeat_cell_request(
                _grid_range_from_start_cell(
                    sheet_id=sheet_id,
                    start_cell=DEFAULT_CWL_START_CELL,
                    rows_count=1,
                    columns_count=columns_count,
                ),
                _title_cell_format(),
                "userEnteredFormat(backgroundColorStyle,textFormat,verticalAlignment,wrapStrategy)",
            ),
        )

    for built_block in built_blocks:
        block_range = _grid_range_from_start_cell(
            sheet_id=sheet_id,
            start_cell=built_block.block.start_cell,
            rows_count=built_block.block.rows_count,
            columns_count=built_block.block.columns_count,
        )
        requests.append(_update_borders_request(block_range))
        requests.append(
            _repeat_cell_request(
                _grid_range_for_block_row(sheet_id, built_block.block, row_offset=0),
                _title_cell_format(),
                "userEnteredFormat(backgroundColorStyle,textFormat,verticalAlignment,wrapStrategy)",
            ),
        )
        if built_block.has_table:
            requests.append(
                _repeat_cell_request(
                    _grid_range_for_block_row(sheet_id, built_block.block, row_offset=1),
                    _header_cell_format(),
                    "userEnteredFormat(backgroundColorStyle,textFormat,verticalAlignment,wrapStrategy)",
                ),
            )
            data_rows_count = max(built_block.block.rows_count - 2, 0)
            for data_row_offset in range(data_rows_count):
                if data_row_offset % 2 == 0:
                    continue
                requests.append(
                    _repeat_cell_request(
                        _grid_range_for_block_row(
                            sheet_id,
                            built_block.block,
                            row_offset=2 + data_row_offset,
                        ),
                        {
                            "userEnteredFormat": {
                                "backgroundColorStyle": {"rgbColor": LIGHT_BAND_RGB}
                            }
                        },
                        "userEnteredFormat.backgroundColorStyle",
                    ),
                )
        else:
            requests.append(
                _repeat_cell_request(
                    _grid_range_for_block_row(sheet_id, built_block.block, row_offset=1),
                    _message_cell_format(),
                    "userEnteredFormat(backgroundColorStyle,textFormat,verticalAlignment,wrapStrategy)",
                ),
            )

    return requests


async def _hide_bot_key_column(*, sheets_client: SheetsClient, sheet_id: int) -> None:
    """Скрывает первую физическую колонку CWL-листа."""

    await sheets_client.hide_dimension(
        sheet_id=sheet_id,
        dimension="COLUMNS",
        start_index=0,
        end_index=1,
        hidden=True,
    )


async def _resolve_active_cwl_sheet(
    runtime_config: RuntimeChatConfig,
    sheets_client: SheetsClient,
) -> SheetMetadata:
    """Находит активный CWL-лист по sheet_id, затем по названию, либо создаёт."""

    metadata = await sheets_client.get_spreadsheet_metadata()
    active_sheet_id = runtime_config.sheet_binding.active_cwl_sheet_id
    if active_sheet_id is not None:
        for sheet in metadata.sheets:
            if sheet.sheet_id == active_sheet_id:
                return sheet

    for sheet in metadata.sheets:
        if sheet.title == runtime_config.sheet_binding.active_cwl_sheet_name:
            return sheet

    for sheet in metadata.sheets:
        if sheet.title == CWL_ACTIVE_SHEET_NAME:
            return sheet

    return await sheets_client.add_sheet(CWL_ACTIVE_SHEET_NAME)


async def _move_cwl_before_composition(
    *,
    runtime_config: RuntimeChatConfig,
    sheets_client: SheetsClient,
    cwl_sheet_id: int,
) -> None:
    """Ставит новый active CWL перед листом `Состав`."""

    metadata = await sheets_client.get_spreadsheet_metadata()
    composition_sheet_id = runtime_config.sheet_binding.composition_sheet_id
    composition_sheet: SheetMetadata | None = None
    if composition_sheet_id is not None:
        composition_sheet = next(
            (sheet for sheet in metadata.sheets if sheet.sheet_id == composition_sheet_id),
            None,
        )
    if composition_sheet is None:
        composition_sheet = next(
            (
                sheet
                for sheet in metadata.sheets
                if sheet.title == runtime_config.sheet_binding.composition_sheet_name
            ),
            None,
        )
    if composition_sheet is None:
        return
    await sheets_client.move_sheet(cwl_sheet_id, composition_sheet.index or 0)


async def _write_bot_state(
    *,
    runtime_config: RuntimeChatConfig,
    sheets_client: SheetsClient,
    active_cwl_sheet_name: str,
    active_cwl_sheet_id: int,
    active_cwl_season: str,
) -> None:
    """Обновляет `_bot_state` после CWL-записи."""

    binding = runtime_config.sheet_binding
    values: list[list[CellValue]] = [
        ["managed_by", MANAGED_BY_VALUE],
        ["schema_version", BOT_STATE_SCHEMA_VERSION],
        ["chat_id", runtime_config.chat_id],
        ["google_sheet_id", binding.google_sheet_id],
        ["composition_sheet_name", binding.composition_sheet_name],
        ["composition_sheet_id", binding.composition_sheet_id or ""],
        ["active_cwl_sheet_name", active_cwl_sheet_name],
        ["active_cwl_sheet_id", active_cwl_sheet_id],
        ["active_cwl_season", active_cwl_season],
        ["bot_state_sheet_name", binding.bot_state_sheet_name],
        ["bot_state_sheet_id", binding.bot_state_sheet_id or ""],
        ["timezone", binding.timezone],
        ["updated_at", _utc_now_iso()],
    ]
    await sheets_client.write_values(
        sheet_name=binding.bot_state_sheet_name,
        range_a1=f"A1:B{len(values)}",
        values=values,
    )


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


def _system_indexes_from_header(
    profiles: Sequence[ColumnProfile],
    header_row: Sequence[str],
    block_start_index: int,
) -> dict[str, int]:
    """Ищет system column indexes по текущим title и legacy aliases."""

    normalized_row = [cell.strip() for cell in header_row]
    result: dict[str, int] = {}
    for profile in profiles:
        if profile.kind != "system" or not profile.visible:
            continue
        aliases = set(SYSTEM_HEADER_ALIASES.get(profile.column_key, set()))
        aliases.add(profile.title)
        for index, title in enumerate(normalized_row):
            if index < block_start_index:
                continue
            if title in aliases:
                result[profile.column_key] = index
                break
    return result


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


def _marker_sort_index(marker: str) -> int:
    """Возвращает порядок marker внутри атакующего."""

    if marker == NO_ATTACK_MARKER:
        return 0
    prefix = f"{ATTACK_MARKER_PREFIX}_"
    if marker.startswith(prefix):
        raw_number = marker.removeprefix(prefix)
        if raw_number.isdigit():
            return int(raw_number)
    return 10**6


def _unique_sheet_title(*, base_title: str, existing_titles: set[str]) -> str:
    """Подбирает свободное имя листа с суффиксом `- N`."""

    if base_title not in existing_titles:
        return base_title
    suffix = 2
    while True:
        title = f"{base_title} - {suffix}"
        if title not in existing_titles:
            return title
        suffix += 1


def _title_row(title: str, width: int) -> list[CellValue]:
    """Создаёт строку заголовка с видимым title после hidden key column."""

    if width <= 1:
        return [title]
    return ["", title, *["" for _ in range(width - 2)]]


def _message_row(message: str, width: int) -> list[CellValue]:
    """Создаёт строку служебного сообщения."""

    if width <= 1:
        return [message]
    return ["", message, *["" for _ in range(width - 2)]]


def _empty_row(width: int) -> list[CellValue]:
    """Создаёт пустую строку."""

    return ["" for _ in range(width)]


def _base_cell_format() -> JsonObject:
    """Возвращает базовый формат managed range."""

    return {
        "userEnteredFormat": {
            "backgroundColorStyle": {"rgbColor": WHITE_RGB},
            "textFormat": {
                "foregroundColorStyle": {"rgbColor": BLACK_RGB},
                "bold": False,
            },
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "WRAP",
        },
    }


def _title_cell_format() -> JsonObject:
    """Возвращает формат строки названия."""

    return {
        "userEnteredFormat": {
            "backgroundColorStyle": {"rgbColor": DARK_GREEN_RGB},
            "textFormat": {
                "foregroundColorStyle": {"rgbColor": WHITE_RGB},
                "bold": True,
            },
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "WRAP",
        },
    }


def _header_cell_format() -> JsonObject:
    """Возвращает формат заголовков."""

    return {
        "userEnteredFormat": {
            "backgroundColorStyle": {"rgbColor": GREEN_RGB},
            "textFormat": {
                "foregroundColorStyle": {"rgbColor": WHITE_RGB},
                "bold": True,
            },
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "WRAP",
        },
    }


def _message_cell_format() -> JsonObject:
    """Возвращает формат служебной строки."""

    return {
        "userEnteredFormat": {
            "backgroundColorStyle": {"rgbColor": LIGHT_BAND_RGB},
            "textFormat": {
                "foregroundColorStyle": {"rgbColor": BLACK_RGB},
                "bold": False,
            },
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "WRAP",
        },
    }


def _repeat_cell_request(grid_range: JsonObject, cell: JsonObject, fields: str) -> JsonObject:
    """Создаёт repeatCell request."""

    return {"repeatCell": {"range": grid_range, "cell": cell, "fields": fields}}


def _update_borders_request(grid_range: JsonObject) -> JsonObject:
    """Создаёт updateBorders request."""

    border = {
        "style": "SOLID",
        "width": 1,
        "colorStyle": {"rgbColor": BORDER_RGB},
    }
    return {
        "updateBorders": {
            "range": grid_range,
            "top": border,
            "bottom": border,
            "left": border,
            "right": border,
            "innerHorizontal": border,
            "innerVertical": border,
        },
    }


def _grid_range_for_block_row(sheet_id: int, block: SheetBlock, *, row_offset: int) -> JsonObject:
    """Строит GridRange строки блока."""

    return _grid_range_from_start_cell(
        sheet_id=sheet_id,
        start_cell=_offset_cell(block.start_cell, row_offset=row_offset, column_offset=0),
        rows_count=1,
        columns_count=block.columns_count,
    )


def _grid_range_from_start_cell(
    *,
    sheet_id: int,
    start_cell: str,
    rows_count: int,
    columns_count: int,
) -> JsonObject:
    """Строит GridRange по start cell и размеру."""

    return _shared_grid_range_from_start_cell(
        sheet_id=sheet_id,
        start_cell=start_cell,
        rows_count=rows_count,
        columns_count=columns_count,
        error_cls=CwlDataError,
    )


def _offset_cell(start_cell: str, *, row_offset: int, column_offset: int) -> str:
    """Сдвигает A1-ячейку."""

    return _shared_offset_cell(
        start_cell,
        row_offset=row_offset,
        column_offset=column_offset,
        error_cls=CwlDataError,
    )


def _row_number_from_start_cell(start_cell: str) -> int:
    """Возвращает номер строки из A1-ячейки."""

    _, row_number = _parse_a1_cell(start_cell)
    return row_number


def _parse_a1_cell(cell: str) -> tuple[int, int]:
    """Парсит A1-ячейку."""

    return _shared_parse_a1_cell(cell, error_cls=CwlDataError)


def _column_to_number(column: str) -> int:
    """Преобразует имя колонки в номер."""

    return _shared_column_to_number(column)


def _number_to_column(number: int) -> str:
    """Преобразует номер колонки в имя."""

    return _shared_number_to_column(number, error_cls=CwlDataError)


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
