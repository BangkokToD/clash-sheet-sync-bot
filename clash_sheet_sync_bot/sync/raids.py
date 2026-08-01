"""Строгий разбор и доменный расчёт рейдовых сезонов."""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Literal

from clash_sheet_sync_bot.common.time import utc_now_iso
from clash_sheet_sync_bot.models import (
    AppConfig,
    ColumnProfile,
    RuntimeChatConfig,
    SheetBlock,
    TableType,
    TrackedClan,
    normalize_tag,
)
from clash_sheet_sync_bot.repositories.bindings import SheetBindingRepository
from clash_sheet_sync_bot.repositories.raid_state import (
    RaidDataError,
    RaidPlayerState,
    RaidPlayerStateRepository,
    RaidSheetArchive,
    RaidSheetArchiveRepository,
    decode_raid_technical_values,
    encode_raid_technical_values,
    validate_raid_player_state,
)
from clash_sheet_sync_bot.repositories.sheet_blocks import SheetBlockRepository
from clash_sheet_sync_bot.sheets.client import (
    CellValue,
    GoogleSheetsWriteError,
    SheetMetadata,
    SheetsClient,
    SheetValues,
    SpreadsheetMetadata,
    range_from_start_cell,
)
from clash_sheet_sync_bot.sheets.column_profiles import (
    BOT_KEY_COLUMN_KEY,
    BOT_KEY_TITLE,
    column_title_identity,
)
from clash_sheet_sync_bot.sheets.ranges import (
    grid_range_from_start_cell,
    offset_cell,
)
from clash_sheet_sync_bot.sync.composition import PlannedPlayerState
from clash_sheet_sync_bot.sync.cwl import resolve_active_cwl_sheet_metadata

CAPITAL_PEAK_DISTRICT_ID: Final = 70_000_000
CAPITAL_PEAK_NAME: Final = "Capital Peak"
RAID_TABLE: Final[TableType] = "raids"
RAID_ACTIVE_SHEET_NAME: Final = "Рейды"
RAID_STAGING_SHEET_PREFIX: Final = "Рейды - staging - "
RAID_ARCHIVE_SHEET_PREFIX: Final = "Рейды "
RAID_BLOCK_PREFIX: Final = "raid:"
RAID_MESSAGE_BLOCK_PREFIX: Final = "raid_message:"
RAID_TITLE_ROWS_COUNT: Final = 2
RAID_BLOCK_GAP_ROWS: Final = 1

WHITE_RGB: Final = {"red": 1.0, "green": 1.0, "blue": 1.0}
BLACK_RGB: Final = {"red": 0.0, "green": 0.0, "blue": 0.0}
DARK_HEADER_RGB: Final = {"red": 0.20, "green": 0.24, "blue": 0.29}
HEADER_RGB: Final = {"red": 0.78, "green": 0.82, "blue": 0.86}
LIGHT_BAND_RGB: Final = {"red": 0.96, "green": 0.96, "blue": 0.96}
SOFT_PINK_RGB: Final = {"red": 0.98, "green": 0.86, "blue": 0.88}
WHITE_TEXT_RGB: Final = {"red": 1.0, "green": 1.0, "blue": 1.0}

RaidSeasonState = Literal["ongoing", "ended"]
RaidDistrictKind = Literal["normal", "capital"]
JsonObject = dict[str, Any]


class RaidContractError(RaidDataError):
    """Строгая ошибка контракта завершённого сезона."""


class RaidRetryableDataError(RaidDataError):
    """Временная ошибка ongoing-сезона, для которой допустим повтор sync."""


@dataclass(frozen=True, slots=True)
class RaidMember:
    """Участник рейдового сезона из CoC API."""

    player_tag: str
    player_name: str
    attacks: int
    attack_limit: int
    bonus_attack_limit: int
    capital_resources_looted: int


@dataclass(frozen=True, slots=True)
class RaidAttack:
    """Одна атака с вычисленным приростом разрушения района."""

    player_tag: str
    district_kind: RaidDistrictKind
    destruction_delta_percent: int


@dataclass(frozen=True, slots=True)
class ParsedRaidSeason:
    """Проверенный сезон до доменной агрегации."""

    clan_tag: str
    state: RaidSeasonState
    start_time: str
    end_time: str
    members: tuple[RaidMember, ...]
    attacks: tuple[RaidAttack, ...]


@dataclass(frozen=True, slots=True)
class RaidTechnicalValues:
    """Агрегированные технические значения одного участника."""

    player_tag: str
    player_name: str
    attacks: int
    attack_limit: int
    bonus_attack_limit: int
    capital_resources_looted: int
    weighted_damage_units: int
    normal_points: Decimal
    coefficient: Decimal


@dataclass(frozen=True, slots=True)
class RaidPlannedRow:
    """Полностью подготовленная строка рейдового листа."""

    row_key: str
    season_key: str
    clan_tag: str
    rank: int
    technical_values: RaidTechnicalValues
    user_values: dict[str, str]


@dataclass(frozen=True, slots=True)
class RaidClanBlock:
    """Подготовленный клановый data- или message-block."""

    clan_tag: str
    clan_name: str
    rows: tuple[RaidPlannedRow, ...] = ()
    message: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedRaidSeason:
    """Полностью подготовленное состояние одного raid season."""

    season_key: str
    start_time: str
    end_time: str
    state: RaidSeasonState
    blocks: tuple[RaidClanBlock, ...]


@dataclass(frozen=True, slots=True)
class PreparedRaidSync:
    """Результат raid preparation без каких-либо write-операций."""

    selected_season: PreparedRaidSeason | None
    previous_active_season: PreparedRaidSeason | None
    empty_blocks: tuple[RaidClanBlock, ...] = ()
    warnings: tuple[str, ...] = ()
    diff: tuple[str, ...] = ()
    showing_saved_season: bool = False

    @property
    def season_key(self) -> str | None:
        """Возвращает выбранный season key для совместимого read-only доступа."""

        return None if self.selected_season is None else self.selected_season.season_key

    @property
    def season_state(self) -> RaidSeasonState | None:
        """Возвращает общее состояние выбранного сезона."""

        return None if self.selected_season is None else self.selected_season.state

    @property
    def blocks(self) -> tuple[RaidClanBlock, ...]:
        """Возвращает selected blocks либо message-only blocks без сезона."""

        return self.empty_blocks if self.selected_season is None else self.selected_season.blocks


@dataclass(frozen=True, slots=True)
class BuiltRaidBlock:
    """Построенный managed block рейдового листа."""

    block: SheetBlock
    values: list[list[CellValue]]
    rows: tuple[RaidPlannedRow, ...]
    has_table: bool


@dataclass(frozen=True, slots=True)
class RaidSheetSyncResult:
    """Результат изолированной записи active raid sheet."""

    season_key: str | None
    season_state: RaidSeasonState | None
    sheet_name: str
    sheet_id: int
    rows_count: int
    blocks_count: int
    season_start_at: str | None = None
    season_end_at: str | None = None
    attacks_target: int = 6
    attacks_complete_count: int = 0
    attacks_below_target_count: int = 0
    showing_saved_season: bool = False
    warnings: tuple[str, ...] = ()
    archived_previous_season: bool = False
    archive_sheet_name: str | None = None
    pruned_archive_sheet_names: tuple[str, ...] = ()


async def apply_public_raid_sync(
    *,
    runtime_config: RuntimeChatConfig,
    sheets_client: SheetsClient,
    raid_player_state_repository: RaidPlayerStateRepository,
    raid_sheet_archive_repository: RaidSheetArchiveRepository,
    sheet_block_repository: SheetBlockRepository,
    sheet_binding_repository: SheetBindingRepository,
    config: AppConfig,
    prepared: PreparedRaidSync,
    sync_run_id: int,
) -> RaidSheetSyncResult:
    """Записывает active raid season с recoverable staging-ротацией."""

    selected = prepared.selected_season
    active_season_key = runtime_config.sheet_binding.active_raid_season
    if active_season_key is not None and selected is None:
        raise RaidDataError(
            "Известный active raid season нельзя заменить пустым состоянием.",
        )

    columns = _physical_raid_columns(runtime_config.column_profiles)
    metadata = await sheets_client.get_spreadsheet_metadata()
    active_sheet = await _resolve_active_raid_sheet(
        runtime_config,
        sheets_client,
        metadata=metadata,
    )
    registered_blocks = await sheet_block_repository.list_blocks(runtime_config.chat_id)
    season_changed = (
        active_season_key is not None
        and selected is not None
        and selected.season_key != active_season_key
    )
    old_archive_sheet: SheetMetadata | None = None
    archive_sheet_name: str | None = None
    old_blocks: tuple[SheetBlock, ...] = ()
    archive_built_blocks: tuple[BuiltRaidBlock, ...] = ()
    active_previous_blocks: tuple[SheetBlock, ...] = ()

    if season_changed:
        previous_active = prepared.previous_active_season
        if previous_active is None or previous_active.season_key != active_season_key:
            raise RaidDataError(
                "Preparation не содержит состояние известного old active raid season.",
            )
        bound_sheet = _bound_active_raid_sheet(runtime_config, metadata)
        recovery = (
            active_sheet.title == RAID_ACTIVE_SHEET_NAME
            and active_sheet.sheet_id != runtime_config.sheet_binding.active_raid_sheet_id
            and bound_sheet is not None
            and not _is_raid_staging_sheet(bound_sheet)
        )
        if recovery:
            old_archive_sheet = bound_sheet
            archive_sheet_name = bound_sheet.title
            old_blocks = _registered_raid_blocks_for_sheet_id(
                registered_blocks,
                bound_sheet.sheet_id,
            )
            archive_built_blocks = build_raid_sheet_blocks(
                runtime_config=runtime_config,
                sheet_name=bound_sheet.title,
                sheet_id=bound_sheet.sheet_id,
                prepared=PreparedRaidSync(
                    selected_season=previous_active,
                    previous_active_season=None,
                ),
                columns=columns,
                attacks_target=config.raid_attacks_target,
            )
            active_previous_blocks = _registered_blocks_for_active_raid_sheet(
                registered_blocks=registered_blocks,
                active_sheet=active_sheet,
            )
            built_blocks = await _write_raid_sheet_content(
                runtime_config=runtime_config,
                sheets_client=sheets_client,
                active_sheet=active_sheet,
                prepared=prepared,
                columns=columns,
                previous_blocks=active_previous_blocks,
                attacks_target=config.raid_attacks_target,
            )
        else:
            (
                active_sheet,
                built_blocks,
                old_archive_sheet,
                archive_sheet_name,
                old_blocks,
                archive_built_blocks,
            ) = await _rotate_raid_sheet(
                runtime_config=runtime_config,
                sheets_client=sheets_client,
                prepared=prepared,
                columns=columns,
                registered_blocks=registered_blocks,
                metadata=metadata,
                old_active=active_sheet,
                previous_active=previous_active,
                attacks_target=config.raid_attacks_target,
                sync_run_id=sync_run_id,
            )
    else:
        active_previous_blocks = _registered_blocks_for_active_raid_sheet(
            registered_blocks=registered_blocks,
            active_sheet=active_sheet,
        )
        built_blocks = await _write_raid_sheet_content(
            runtime_config=runtime_config,
            sheets_client=sheets_client,
            active_sheet=active_sheet,
            prepared=prepared,
            columns=columns,
            previous_blocks=active_previous_blocks,
            attacks_target=config.raid_attacks_target,
        )

    updated_at = utc_now_iso()
    if season_changed and prepared.previous_active_season is not None:
        await _store_raid_player_states(
            runtime_config=runtime_config,
            repository=raid_player_state_repository,
            season=prepared.previous_active_season,
            updated_at=updated_at,
        )
    if selected is not None:
        await _store_raid_player_states(
            runtime_config=runtime_config,
            repository=raid_player_state_repository,
            season=selected,
            updated_at=updated_at,
        )

    if season_changed:
        if old_archive_sheet is None or archive_sheet_name is None:
            raise RaidDataError("Rotation завершилась без identity старого archive sheet.")
        existing_archives = await raid_sheet_archive_repository.list_ordered(
            runtime_config.chat_id,
        )
        existing_archive = next(
            (
                archive
                for archive in existing_archives
                if archive.season_key == active_season_key
                and archive.sheet_id == old_archive_sheet.sheet_id
            ),
            None,
        )
        await raid_sheet_archive_repository.upsert(
            RaidSheetArchive(
                chat_id=runtime_config.chat_id,
                season_key=active_season_key,
                season_start_at=prepared.previous_active_season.start_time,
                sheet_name=archive_sheet_name,
                sheet_id=old_archive_sheet.sheet_id,
                archived_at=(
                    existing_archive.archived_at if existing_archive is not None else updated_at
                ),
            ),
        )
        await _replace_raid_block_metadata(
            runtime_config=runtime_config,
            repository=sheet_block_repository,
            active_sheet=old_archive_sheet,
            previous_blocks=old_blocks,
            built_blocks=archive_built_blocks,
            updated_at=updated_at,
        )

    await _replace_raid_block_metadata(
        runtime_config=runtime_config,
        repository=sheet_block_repository,
        active_sheet=active_sheet,
        previous_blocks=active_previous_blocks,
        built_blocks=built_blocks,
        updated_at=updated_at,
    )

    if selected is not None:
        await sheet_binding_repository.update_active_raid_binding(
            chat_id=runtime_config.chat_id,
            active_raid_sheet_name=active_sheet.title,
            active_raid_sheet_id=active_sheet.sheet_id,
            active_raid_season=selected.season_key,
            now=updated_at,
        )

    pruned_names, cleanup_warnings = await _prune_raid_archives(
        runtime_config=runtime_config,
        sheets_client=sheets_client,
        raid_sheet_archive_repository=raid_sheet_archive_repository,
        sheet_block_repository=sheet_block_repository,
        active_sheet_id=active_sheet.sheet_id,
        archive_limit=config.raid_archive_sheets_limit,
    )
    warnings = (*prepared.warnings, *cleanup_warnings)
    active_rows = tuple(row for block in built_blocks for row in block.rows)

    return RaidSheetSyncResult(
        season_key=None if selected is None else selected.season_key,
        season_state=None if selected is None else selected.state,
        sheet_name=active_sheet.title,
        sheet_id=active_sheet.sheet_id,
        rows_count=sum(len(block.rows) for block in built_blocks),
        blocks_count=len(built_blocks),
        season_start_at=None if selected is None else selected.start_time,
        season_end_at=None if selected is None else selected.end_time,
        attacks_target=config.raid_attacks_target,
        attacks_complete_count=sum(
            row.technical_values.attacks >= config.raid_attacks_target for row in active_rows
        ),
        attacks_below_target_count=sum(
            row.technical_values.attacks < config.raid_attacks_target for row in active_rows
        ),
        showing_saved_season=prepared.showing_saved_season,
        warnings=warnings,
        archived_previous_season=season_changed,
        archive_sheet_name=archive_sheet_name,
        pruned_archive_sheet_names=pruned_names,
    )


async def _resolve_active_raid_sheet(
    runtime_config: RuntimeChatConfig,
    sheets_client: SheetsClient,
    *,
    metadata: SpreadsheetMetadata | None = None,
) -> SheetMetadata:
    """Разрешает active raid sheet, предпочитая canonical после rotation."""

    metadata = metadata or await sheets_client.get_spreadsheet_metadata()
    matches_by_canonical_title = tuple(
        sheet for sheet in metadata.sheets if sheet.title == RAID_ACTIVE_SHEET_NAME
    )
    canonical_sheet = _unique_active_raid_sheet_match(
        matches_by_canonical_title,
        criterion=f"canonical title={RAID_ACTIVE_SHEET_NAME!r}",
    )

    configured_title = runtime_config.sheet_binding.active_raid_sheet_name
    active_sheet_id = runtime_config.sheet_binding.active_raid_sheet_id
    if active_sheet_id is not None:
        matches_by_id = tuple(
            sheet
            for sheet in metadata.sheets
            if sheet.sheet_id == active_sheet_id and not _is_raid_staging_sheet(sheet)
        )
        sheet_by_id = _unique_active_raid_sheet_match(
            matches_by_id,
            criterion=f"sheet_id={active_sheet_id}",
        )
        if sheet_by_id is not None:
            if sheet_by_id.title == configured_title or canonical_sheet is None:
                return sheet_by_id
            return canonical_sheet

    matches_by_configured_title = tuple(
        sheet
        for sheet in metadata.sheets
        if sheet.title == configured_title and not _is_raid_staging_sheet(sheet)
    )
    sheet_by_configured_title = _unique_active_raid_sheet_match(
        matches_by_configured_title,
        criterion=f"binding title={configured_title!r}",
    )
    if sheet_by_configured_title is not None:
        return sheet_by_configured_title
    if canonical_sheet is not None:
        return canonical_sheet
    return await sheets_client.add_sheet(RAID_ACTIVE_SHEET_NAME)


def _unique_active_raid_sheet_match(
    matches: Sequence[SheetMetadata],
    *,
    criterion: str,
) -> SheetMetadata | None:
    """Возвращает единственный resolver match или отклоняет неоднозначность."""

    if len(matches) > 1:
        raise RaidDataError(
            f"Неоднозначный active raid sheet для {criterion}: найдено {len(matches)} листа.",
        )
    return matches[0] if matches else None


def _is_raid_staging_sheet(sheet: SheetMetadata) -> bool:
    """Проверяет служебный staging title без признания его active."""

    return sheet.title.startswith(RAID_STAGING_SHEET_PREFIX)


def _bound_active_raid_sheet(
    runtime_config: RuntimeChatConfig,
    metadata: SpreadsheetMetadata,
) -> SheetMetadata | None:
    """Возвращает физический лист binding без title fallback."""

    sheet_id = runtime_config.sheet_binding.active_raid_sheet_id
    if sheet_id is None:
        return None
    matches = tuple(sheet for sheet in metadata.sheets if sheet.sheet_id == sheet_id)
    return _unique_active_raid_sheet_match(
        matches,
        criterion=f"binding sheet_id={sheet_id}",
    )


async def _write_raid_sheet_content(
    *,
    runtime_config: RuntimeChatConfig,
    sheets_client: SheetsClient,
    active_sheet: SheetMetadata,
    prepared: PreparedRaidSync,
    columns: Sequence[ColumnProfile],
    previous_blocks: Sequence[SheetBlock],
    attacks_target: int,
) -> tuple[BuiltRaidBlock, ...]:
    """Полностью записывает, форматирует и скрывает key одного raid-листа."""

    built_blocks = build_raid_sheet_blocks(
        runtime_config=runtime_config,
        sheet_name=active_sheet.title,
        sheet_id=active_sheet.sheet_id,
        prepared=prepared,
        columns=columns,
        attacks_target=attacks_target,
    )
    await _rewrite_raid_values(
        sheets_client=sheets_client,
        sheet_name=active_sheet.title,
        previous_blocks=previous_blocks,
        built_blocks=built_blocks,
    )
    await _format_raid_sheet(
        sheets_client=sheets_client,
        sheet_id=active_sheet.sheet_id,
        previous_blocks=previous_blocks,
        built_blocks=built_blocks,
        columns=columns,
        season_state=prepared.season_state,
        attacks_target=attacks_target,
    )
    await sheets_client.hide_dimension(
        sheet_id=active_sheet.sheet_id,
        dimension="COLUMNS",
        start_index=0,
        end_index=1,
        hidden=True,
    )
    return built_blocks


async def _rotate_raid_sheet(
    *,
    runtime_config: RuntimeChatConfig,
    sheets_client: SheetsClient,
    prepared: PreparedRaidSync,
    columns: Sequence[ColumnProfile],
    registered_blocks: Sequence[SheetBlock],
    metadata: SpreadsheetMetadata,
    old_active: SheetMetadata,
    previous_active: PreparedRaidSeason,
    attacks_target: int,
    sync_run_id: int,
) -> tuple[
    SheetMetadata,
    tuple[BuiltRaidBlock, ...],
    SheetMetadata,
    str,
    tuple[SheetBlock, ...],
    tuple[BuiltRaidBlock, ...],
]:
    """Готовит staging, финализирует old active и выполняет atomic rotation."""

    cwl_index = _resolve_cwl_sheet_index(runtime_config, metadata)
    existing_titles = {sheet.title for sheet in metadata.sheets}
    staging_title = _unique_sheet_title(
        base_title=f"{RAID_STAGING_SHEET_PREFIX}{sync_run_id}",
        existing_titles=existing_titles,
    )
    staging = await sheets_client.add_sheet(staging_title)
    await _write_raid_sheet_content(
        runtime_config=runtime_config,
        sheets_client=sheets_client,
        active_sheet=staging,
        prepared=prepared,
        columns=columns,
        previous_blocks=(),
        attacks_target=attacks_target,
    )

    old_blocks = _registered_blocks_for_active_raid_sheet(
        registered_blocks=registered_blocks,
        active_sheet=old_active,
    )
    previous_prepared = PreparedRaidSync(
        selected_season=previous_active,
        previous_active_season=None,
    )
    finalized_old_blocks = await _write_raid_sheet_content(
        runtime_config=runtime_config,
        sheets_client=sheets_client,
        active_sheet=old_active,
        prepared=previous_prepared,
        columns=columns,
        previous_blocks=old_blocks,
        attacks_target=attacks_target,
    )

    current_metadata = await sheets_client.get_spreadsheet_metadata()
    archive_title = _unique_sheet_title(
        base_title=f"{RAID_ARCHIVE_SHEET_PREFIX}{previous_active.start_time[:10]}",
        existing_titles={
            sheet.title
            for sheet in current_metadata.sheets
            if sheet.sheet_id != old_active.sheet_id
        },
    )
    await sheets_client.batch_update_spreadsheet(
        [
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": old_active.sheet_id,
                        "title": archive_title,
                    },
                    "fields": "title",
                },
            },
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": staging.sheet_id,
                        "title": RAID_ACTIVE_SHEET_NAME,
                    },
                    "fields": "title",
                },
            },
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": staging.sheet_id,
                        "index": cwl_index,
                    },
                    "fields": "index",
                },
            },
        ],
    )
    new_active = SheetMetadata(
        sheet_id=staging.sheet_id,
        title=RAID_ACTIVE_SHEET_NAME,
        index=cwl_index,
    )
    archived_old = SheetMetadata(
        sheet_id=old_active.sheet_id,
        title=archive_title,
        index=old_active.index,
    )
    built_blocks = build_raid_sheet_blocks(
        runtime_config=runtime_config,
        sheet_name=new_active.title,
        sheet_id=new_active.sheet_id,
        prepared=prepared,
        columns=columns,
        attacks_target=attacks_target,
    )
    archive_built_blocks = _retarget_built_raid_blocks(
        finalized_old_blocks,
        sheet_name=archive_title,
        sheet_id=old_active.sheet_id,
    )
    return (
        new_active,
        built_blocks,
        archived_old,
        archive_title,
        old_blocks,
        archive_built_blocks,
    )


def _resolve_cwl_sheet_index(
    runtime_config: RuntimeChatConfig,
    metadata: SpreadsheetMetadata,
) -> int:
    """Находит целевую позицию нового raid active непосредственно перед CWL."""

    binding = runtime_config.sheet_binding
    cwl_sheet = resolve_active_cwl_sheet_metadata(
        metadata,
        configured_title=binding.active_cwl_sheet_name,
        active_sheet_id=binding.active_cwl_sheet_id,
        error_cls=RaidDataError,
    )
    if cwl_sheet is None or cwl_sheet.index is None:
        raise RaidDataError("Не удалось определить позицию active CWL для raid rotation.")
    return cwl_sheet.index


def _retarget_built_raid_blocks(
    built_blocks: Sequence[BuiltRaidBlock],
    *,
    sheet_name: str,
    sheet_id: int,
) -> tuple[BuiltRaidBlock, ...]:
    """Переносит фактическую built metadata на финальный title/physical ID."""

    return tuple(
        replace(
            built,
            block=replace(
                built.block,
                sheet_name=sheet_name,
                sheet_id=sheet_id,
            ),
        )
        for built in built_blocks
    )


def _unique_sheet_title(*, base_title: str, existing_titles: set[str]) -> str:
    """Подбирает свободное название с безопасным суффиксом ` - N`."""

    if base_title not in existing_titles:
        return base_title
    suffix = 2
    while True:
        title = f"{base_title} - {suffix}"
        if title not in existing_titles:
            return title
        suffix += 1


def _registered_raid_blocks_for_sheet_id(
    registered_blocks: Sequence[SheetBlock],
    sheet_id: int,
) -> tuple[SheetBlock, ...]:
    """Выбирает raid metadata только конкретного физического листа."""

    return tuple(
        block
        for block in registered_blocks
        if block.sheet_id == sheet_id
        and (
            block.block_key.startswith(RAID_BLOCK_PREFIX)
            or block.block_key.startswith(RAID_MESSAGE_BLOCK_PREFIX)
        )
    )


async def _prune_raid_archives(
    *,
    runtime_config: RuntimeChatConfig,
    sheets_client: SheetsClient,
    raid_sheet_archive_repository: RaidSheetArchiveRepository,
    sheet_block_repository: SheetBlockRepository,
    active_sheet_id: int,
    archive_limit: int,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Удаляет oldest registry archives и возвращает recoverable warnings."""

    archives = [
        archive
        for archive in await raid_sheet_archive_repository.list_ordered(
            runtime_config.chat_id,
        )
        if archive.sheet_id != active_sheet_id
    ]
    pruned: list[str] = []
    warnings: list[str] = []
    while len(archives) > archive_limit:
        oldest = archives[0]
        metadata = await sheets_client.get_spreadsheet_metadata()
        physical_matches = tuple(
            sheet for sheet in metadata.sheets if sheet.sheet_id == oldest.sheet_id
        )
        if len(physical_matches) > 1:
            raise RaidDataError(
                "Неоднозначный physical raid archive для "
                f"registry sheet_id={oldest.sheet_id}: найдено {len(physical_matches)} листа.",
            )
        physical = physical_matches[0] if physical_matches else None
        if (
            physical is None
            or physical.sheet_id == active_sheet_id
            or _is_raid_staging_sheet(physical)
        ):
            warnings.append(
                "Raid cleanup не выполнен: oldest archive registry не подтверждён "
                f"безопасным физическим sheet_id={oldest.sheet_id}.",
            )
            break
        try:
            await sheets_client.delete_sheet(oldest.sheet_id)
        except GoogleSheetsWriteError:
            warnings.append(
                "Raid cleanup не завершён: не удалось удалить зарегистрированный "
                f"архив {oldest.sheet_name!r}; pruning будет повторён.",
            )
            break
        await raid_sheet_archive_repository.delete(
            chat_id=runtime_config.chat_id,
            season_key=oldest.season_key,
        )
        await sheet_block_repository.delete_blocks(
            chat_id=runtime_config.chat_id,
            sheet_id=oldest.sheet_id,
            block_key_prefixes=(RAID_BLOCK_PREFIX, RAID_MESSAGE_BLOCK_PREFIX),
        )
        pruned.append(oldest.sheet_name)
        archives.pop(0)
    return tuple(pruned), tuple(warnings)


def build_raid_sheet_blocks(
    *,
    runtime_config: RuntimeChatConfig,
    sheet_name: str,
    sheet_id: int,
    prepared: PreparedRaidSync,
    columns: Sequence[ColumnProfile],
    attacks_target: int,
) -> tuple[BuiltRaidBlock, ...]:
    """Строит отдельный managed block для каждого active clan."""

    built_blocks: list[BuiltRaidBlock] = []
    row_cursor = 1
    selected = prepared.selected_season
    for index, clan_block in enumerate(prepared.blocks):
        if index > 0:
            row_cursor += RAID_BLOCK_GAP_ROWS
        if clan_block.message is None:
            values = _build_raid_table_values(
                clan_block=clan_block,
                season=selected,
                columns=columns,
                attacks_target=attacks_target,
            )
            block_key = f"{RAID_BLOCK_PREFIX}{normalize_tag(clan_block.clan_tag)}"
            has_table = True
        else:
            values = _build_raid_message_values(
                clan_block=clan_block,
                season=selected,
                columns=columns,
            )
            block_key = f"{RAID_MESSAGE_BLOCK_PREFIX}{normalize_tag(clan_block.clan_tag)}"
            has_table = False

        block = SheetBlock(
            chat_id=runtime_config.chat_id,
            sheet_name=sheet_name,
            sheet_id=sheet_id,
            block_key=block_key,
            start_cell=f"A{row_cursor}",
            rows_count=len(values),
            columns_count=len(columns),
        )
        built_blocks.append(
            BuiltRaidBlock(
                block=block,
                values=values,
                rows=clan_block.rows,
                has_table=has_table,
            ),
        )
        row_cursor += len(values)
    return tuple(built_blocks)


def _physical_raid_columns(
    profiles: Sequence[ColumnProfile],
) -> tuple[ColumnProfile, ...]:
    """Возвращает service key и активные видимые raid columns."""

    raid_profiles = sorted(
        (profile for profile in profiles if profile.table_type == RAID_TABLE and profile.is_active),
        key=lambda profile: (profile.sort_order, profile.column_key),
    )
    service = tuple(
        profile
        for profile in raid_profiles
        if profile.kind == "service" and profile.column_key == BOT_KEY_COLUMN_KEY
    )
    if len(service) != 1:
        raise RaidDataError(
            f"Профиль рейдов должен содержать одну service-колонку {BOT_KEY_TITLE}.",
        )
    visible = tuple(
        profile for profile in raid_profiles if profile.kind != "service" and profile.visible
    )
    return (service[0], *visible)


def _build_raid_table_values(
    *,
    clan_block: RaidClanBlock,
    season: PreparedRaidSeason | None,
    columns: Sequence[ColumnProfile],
    attacks_target: int,
) -> list[list[CellValue]]:
    """Строит values табличного raid block."""

    values: list[list[CellValue]] = [
        _raid_title_row(clan_block, season, len(columns)),
        [column.title for column in columns],
    ]
    values.extend(
        _raid_row_to_values(row=row, columns=columns, attacks_target=attacks_target)
        for row in clan_block.rows
    )
    if len(values) == RAID_TITLE_ROWS_COUNT:
        values.append(["" for _ in columns])
    return values


def _build_raid_message_values(
    *,
    clan_block: RaidClanBlock,
    season: PreparedRaidSeason | None,
    columns: Sequence[ColumnProfile],
) -> list[list[CellValue]]:
    """Строит message-only raid block."""

    return [
        _raid_title_row(clan_block, season, len(columns)),
        _visible_text_row(clan_block.message or "", len(columns)),
    ]


def _raid_title_row(
    clan_block: RaidClanBlock,
    season: PreparedRaidSeason | None,
    width: int,
) -> list[CellValue]:
    """Строит верхнюю строку блока с кланом, периодом и состоянием."""

    title = f"{clan_block.clan_name} | {normalize_tag(clan_block.clan_tag)}"
    if season is not None:
        period = f"{season.start_time[:10]} — {season.end_time[:10]}"
        status = "проводится" if season.state == "ongoing" else "завершён"
        title = f"{title} | {period} | {status}"
    return _visible_text_row(title, width)


def _visible_text_row(value: str, width: int) -> list[CellValue]:
    """Помещает видимый текст после скрытой service-колонки."""

    if width <= 1:
        return [value]
    return ["", value, *["" for _ in range(width - 2)]]


def _raid_row_to_values(
    *,
    row: RaidPlannedRow,
    columns: Sequence[ColumnProfile],
    attacks_target: int,
) -> list[CellValue]:
    """Преобразует planned row в типизированные Sheets values."""

    technical = row.technical_values
    values: list[CellValue] = []
    for column in columns:
        if column.kind == "service" and column.column_key == BOT_KEY_COLUMN_KEY:
            values.append(row.row_key)
        elif column.kind == "user":
            values.append(row.user_values.get(column.column_key, ""))
        elif column.column_key == "number":
            values.append(row.rank)
        elif column.column_key == "player_tag":
            values.append(normalize_tag(technical.player_tag))
        elif column.column_key == "player_name":
            values.append(technical.player_name)
        elif column.column_key == "attacks":
            values.append(f"{technical.attacks}/{attacks_target}")
        elif column.column_key == "normal_points":
            values.append(float(technical.normal_points))
        elif column.column_key == "coefficient":
            values.append(float(technical.coefficient))
        elif column.column_key == "capital_resources_looted":
            values.append(technical.capital_resources_looted)
        else:
            values.append("")
    return values


def _registered_blocks_for_active_raid_sheet(
    *,
    registered_blocks: Sequence[SheetBlock],
    active_sheet: SheetMetadata,
) -> tuple[SheetBlock, ...]:
    """Выбирает только raid blocks разрешённого физического active-листа."""

    return tuple(
        block
        for block in registered_blocks
        if (
            block.block_key.startswith(RAID_BLOCK_PREFIX)
            or block.block_key.startswith(RAID_MESSAGE_BLOCK_PREFIX)
        )
        and block.sheet_id == active_sheet.sheet_id
    )


async def _rewrite_raid_values(
    *,
    sheets_client: SheetsClient,
    sheet_name: str,
    previous_blocks: Sequence[SheetBlock],
    built_blocks: Sequence[BuiltRaidBlock],
) -> None:
    """Очищает прежние owned ranges и пишет только новые managed ranges."""

    updates: list[SheetValues] = []
    for block in previous_blocks:
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
    updates.extend(
        SheetValues(
            sheet_name=sheet_name,
            range_a1=range_from_start_cell(
                start_cell=built.block.start_cell,
                rows_count=built.block.rows_count,
                columns_count=built.block.columns_count,
            ),
            values=built.values,
        )
        for built in built_blocks
    )
    if updates:
        await sheets_client.batch_update_values(updates)


async def _format_raid_sheet(
    *,
    sheets_client: SheetsClient,
    sheet_id: int,
    previous_blocks: Sequence[SheetBlock],
    built_blocks: Sequence[BuiltRaidBlock],
    columns: Sequence[ColumnProfile],
    season_state: RaidSeasonState | None,
    attacks_target: int,
) -> None:
    """Форматирует только прежние и новые managed raid ranges."""

    requests = _build_raid_format_requests(
        sheet_id=sheet_id,
        previous_blocks=previous_blocks,
        built_blocks=built_blocks,
        columns=columns,
        season_state=season_state,
        attacks_target=attacks_target,
    )
    if requests:
        await sheets_client.batch_update_spreadsheet(requests)


def _build_raid_format_requests(
    *,
    sheet_id: int,
    previous_blocks: Sequence[SheetBlock],
    built_blocks: Sequence[BuiltRaidBlock],
    columns: Sequence[ColumnProfile],
    season_state: RaidSeasonState | None,
    attacks_target: int,
) -> list[JsonObject]:
    """Строит formatting requests без width и horizontal alignment."""

    requests: list[JsonObject] = []
    reset_fields = (
        "userEnteredFormat(backgroundColorStyle,borders,textFormat,"
        "verticalAlignment,wrapStrategy,numberFormat)"
    )
    for block in previous_blocks:
        requests.append(
            _repeat_raid_cell_request(
                grid_range_from_start_cell(
                    sheet_id=sheet_id,
                    start_cell=block.start_cell,
                    rows_count=block.rows_count,
                    columns_count=block.columns_count,
                    error_cls=RaidDataError,
                ),
                {"userEnteredFormat": {}},
                reset_fields,
            ),
        )

    number_column_indexes = tuple(
        index
        for index, column in enumerate(columns)
        if column.column_key in {"normal_points", "coefficient"}
    )
    attacks_column_index = next(
        (index for index, column in enumerate(columns) if column.column_key == "attacks"),
        None,
    )
    for built in built_blocks:
        block = built.block
        block_range = grid_range_from_start_cell(
            sheet_id=sheet_id,
            start_cell=block.start_cell,
            rows_count=block.rows_count,
            columns_count=block.columns_count,
            error_cls=RaidDataError,
        )
        requests.append(
            _repeat_raid_cell_request(
                block_range,
                _raid_base_cell_format(),
                "userEnteredFormat(backgroundColorStyle,textFormat,verticalAlignment,wrapStrategy)",
            ),
        )
        requests.append(
            _repeat_raid_cell_request(
                _raid_block_row_range(sheet_id, block, row_offset=0),
                _raid_title_cell_format(),
                "userEnteredFormat(backgroundColorStyle,textFormat,verticalAlignment,wrapStrategy)",
            ),
        )
        if not built.has_table:
            requests.append(
                _repeat_raid_cell_request(
                    _raid_block_row_range(sheet_id, block, row_offset=1),
                    _raid_message_cell_format(),
                    "userEnteredFormat(backgroundColorStyle,textFormat,"
                    "verticalAlignment,wrapStrategy)",
                ),
            )
            continue

        requests.append(
            _repeat_raid_cell_request(
                _raid_block_row_range(sheet_id, block, row_offset=1),
                _raid_header_cell_format(),
                "userEnteredFormat(backgroundColorStyle,textFormat,verticalAlignment,wrapStrategy)",
            ),
        )
        data_rows_count = max(block.rows_count - RAID_TITLE_ROWS_COUNT, 0)
        for data_row_offset in range(data_rows_count):
            if data_row_offset % 2 != 0:
                requests.append(
                    _repeat_raid_cell_request(
                        _raid_block_row_range(
                            sheet_id,
                            block,
                            row_offset=RAID_TITLE_ROWS_COUNT + data_row_offset,
                        ),
                        {
                            "userEnteredFormat": {
                                "backgroundColorStyle": {"rgbColor": LIGHT_BAND_RGB},
                            },
                        },
                        "userEnteredFormat.backgroundColorStyle",
                    ),
                )
            for column_index in number_column_indexes:
                requests.append(
                    _repeat_raid_cell_request(
                        _raid_block_cell_range(
                            sheet_id,
                            block,
                            row_offset=RAID_TITLE_ROWS_COUNT + data_row_offset,
                            column_offset=column_index,
                        ),
                        {
                            "userEnteredFormat": {
                                "numberFormat": {
                                    "type": "NUMBER",
                                    "pattern": "0.00",
                                },
                            },
                        },
                        "userEnteredFormat.numberFormat",
                    ),
                )
            if (
                season_state == "ended"
                and attacks_column_index is not None
                and data_row_offset < len(built.rows)
                and built.rows[data_row_offset].technical_values.attacks < attacks_target
            ):
                requests.append(
                    _repeat_raid_cell_request(
                        _raid_block_cell_range(
                            sheet_id,
                            block,
                            row_offset=RAID_TITLE_ROWS_COUNT + data_row_offset,
                            column_offset=attacks_column_index,
                        ),
                        {
                            "userEnteredFormat": {
                                "backgroundColorStyle": {"rgbColor": SOFT_PINK_RGB},
                            },
                        },
                        "userEnteredFormat.backgroundColorStyle",
                    ),
                )
    return requests


def _raid_block_row_range(
    sheet_id: int,
    block: SheetBlock,
    *,
    row_offset: int,
) -> JsonObject:
    """Возвращает GridRange одной строки managed block."""

    return grid_range_from_start_cell(
        sheet_id=sheet_id,
        start_cell=offset_cell(
            block.start_cell,
            row_offset=row_offset,
            column_offset=0,
            error_cls=RaidDataError,
        ),
        rows_count=1,
        columns_count=block.columns_count,
        error_cls=RaidDataError,
    )


def _raid_block_cell_range(
    sheet_id: int,
    block: SheetBlock,
    *,
    row_offset: int,
    column_offset: int,
) -> JsonObject:
    """Возвращает GridRange одной ячейки managed block."""

    return grid_range_from_start_cell(
        sheet_id=sheet_id,
        start_cell=offset_cell(
            block.start_cell,
            row_offset=row_offset,
            column_offset=column_offset,
            error_cls=RaidDataError,
        ),
        rows_count=1,
        columns_count=1,
        error_cls=RaidDataError,
    )


def _repeat_raid_cell_request(
    grid_range: JsonObject,
    cell: JsonObject,
    fields: str,
) -> JsonObject:
    """Строит repeatCell request."""

    return {
        "repeatCell": {
            "range": grid_range,
            "cell": cell,
            "fields": fields,
        },
    }


def _raid_base_cell_format() -> JsonObject:
    """Возвращает нейтральный базовый формат managed range."""

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


def _raid_title_cell_format() -> JsonObject:
    """Возвращает нейтральный формат clan/season title."""

    return {
        "userEnteredFormat": {
            "backgroundColorStyle": {"rgbColor": DARK_HEADER_RGB},
            "textFormat": {
                "foregroundColorStyle": {"rgbColor": WHITE_TEXT_RGB},
                "bold": True,
            },
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "WRAP",
        },
    }


def _raid_header_cell_format() -> JsonObject:
    """Возвращает нейтральный формат заголовков колонок."""

    return {
        "userEnteredFormat": {
            "backgroundColorStyle": {"rgbColor": HEADER_RGB},
            "textFormat": {
                "foregroundColorStyle": {"rgbColor": BLACK_RGB},
                "bold": True,
            },
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "WRAP",
        },
    }


def _raid_message_cell_format() -> JsonObject:
    """Возвращает нейтральный формат message row."""

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


async def _store_raid_player_states(
    *,
    runtime_config: RuntimeChatConfig,
    repository: RaidPlayerStateRepository,
    season: PreparedRaidSeason,
    updated_at: str,
) -> None:
    """Сохраняет отображённые raid rows как SQLite source of truth."""

    for clan_block in season.blocks:
        for row in clan_block.rows:
            technical = row.technical_values
            await repository.upsert(
                RaidPlayerState(
                    chat_id=runtime_config.chat_id,
                    season_key=season.season_key,
                    season_start_at=season.start_time,
                    season_end_at=season.end_time,
                    season_state=season.state,
                    row_key=row.row_key,
                    clan_tag=normalize_tag(row.clan_tag),
                    player_tag=normalize_tag(technical.player_tag),
                    technical_values={
                        "player_name": technical.player_name,
                        "attacks": technical.attacks,
                        "attack_limit": technical.attack_limit,
                        "bonus_attack_limit": technical.bonus_attack_limit,
                        "capital_resources_looted": technical.capital_resources_looted,
                        "weighted_damage_units": technical.weighted_damage_units,
                        "normal_points": technical.normal_points,
                        "coefficient": technical.coefficient,
                    },
                    user_values=dict(row.user_values),
                    row_hash=None,
                    updated_at=updated_at,
                ),
            )


async def _replace_raid_block_metadata(
    *,
    runtime_config: RuntimeChatConfig,
    repository: SheetBlockRepository,
    active_sheet: SheetMetadata,
    previous_blocks: Sequence[SheetBlock],
    built_blocks: Sequence[BuiltRaidBlock],
    updated_at: str,
) -> None:
    """Заменяет registry текущего active raid sheet без ротации."""

    prefixes = (RAID_BLOCK_PREFIX, RAID_MESSAGE_BLOCK_PREFIX)
    stale_sheet_names = sorted(
        {block.sheet_name for block in previous_blocks if block.sheet_name != active_sheet.title},
    )
    for stale_sheet_name in stale_sheet_names:
        await repository.replace_blocks_by_prefixes(
            chat_id=runtime_config.chat_id,
            sheet_name=stale_sheet_name,
            block_key_prefixes=prefixes,
            blocks=(),
            updated_at=updated_at,
        )
    await repository.replace_blocks_by_prefixes(
        chat_id=runtime_config.chat_id,
        sheet_name=active_sheet.title,
        block_key_prefixes=prefixes,
        blocks=tuple(built.block for built in built_blocks),
        updated_at=updated_at,
    )


async def prepare_public_raid_sync(
    *,
    runtime_config: RuntimeChatConfig,
    clash_client: Any,
    sheets_client: SheetsClient,
    sheet_block_repository: SheetBlockRepository,
    config: AppConfig,
    saved_rows: Sequence[RaidPlayerState] = (),
    composition_player_states: Sequence[PlannedPlayerState] = (),
) -> PreparedRaidSync:
    """Подготавливает общий raid season всех активных кланов до Sheets write."""

    clans = runtime_config.active_clans
    semaphore = asyncio.Semaphore(config.raid_api_concurrency_limit)

    async def load(clan: TrackedClan) -> tuple[str, list[JsonObject]]:
        async with semaphore:
            items = await clash_client.get_capital_raid_seasons(
                clan.clan_tag,
                limit=config.raid_season_fetch_limit,
            )
        return normalize_tag(clan.clan_tag), items

    windows = dict(await asyncio.gather(*(load(clan) for clan in clans)))
    selection_metadata = tuple(
        _validate_selection_metadata(item, clan_tag=clan_tag, index=index)
        for clan_tag, items in windows.items()
        for index, item in enumerate(items, start=1)
    )
    ongoing_keys = {season_key for state, season_key in selection_metadata if state == "ongoing"}
    if len(ongoing_keys) > 1:
        raise RaidContractError("Активные кланы вернули разные ongoing raid seasons.")
    ended_keys = {season_key for state, season_key in selection_metadata if state == "ended"}
    selected_key = next(iter(ongoing_keys), max(ended_keys, default=None))
    active_clan_tags = {normalize_tag(clan.clan_tag) for clan in clans}
    validated_saved_rows = tuple(
        validate_raid_player_state(row)
        for row in saved_rows
        if row.chat_id == runtime_config.chat_id
    )
    scoped_saved_rows = tuple(
        row for row in validated_saved_rows if normalize_tag(row.clan_tag) in active_clan_tags
    )
    api_history_exists = bool(ongoing_keys or ended_keys)
    if selected_key is None and scoped_saved_rows:
        selected_key = max(
            scoped_saved_rows,
            key=lambda row: (row.season_start_at, row.season_key),
        ).season_key

    warnings: list[str] = []
    imported, import_warnings = await _import_registered_raid_values(
        runtime_config=runtime_config,
        sheets_client=sheets_client,
        sheet_block_repository=sheet_block_repository,
    )
    warnings.extend(import_warnings)
    composition_by_tag = {
        normalize_tag(state.player_tag): state for state in composition_player_states
    }
    saved_by_key = {row.row_key: row for row in scoped_saved_rows}
    user_column_links = _raid_composition_user_column_links(runtime_config.column_profiles)

    selected_season: PreparedRaidSeason | None = None
    showing_saved_season = False
    if selected_key is not None:
        selected_season = _prepare_season_state(
            season_key=selected_key,
            state="ongoing" if selected_key in ongoing_keys else None,
            clans=clans,
            windows=windows,
            saved_rows=scoped_saved_rows,
            use_saved_fallback=not api_history_exists,
            imported=imported,
            imported_season=runtime_config.sheet_binding.active_raid_season,
            saved_by_key=saved_by_key,
            composition_by_tag=composition_by_tag,
            user_column_links=user_column_links,
            config=config,
        )
        if not api_history_exists:
            showing_saved_season = True
            warnings.append(f"{selected_key}: выбран последний сохранённый raid season из SQLite.")

    previous_active_season: PreparedRaidSeason | None = None
    active_season_key = runtime_config.sheet_binding.active_raid_season
    if (
        active_season_key is not None
        and selected_key is not None
        and active_season_key != selected_key
    ):
        fallback_clan_tags: set[str] = set()
        previous_active_season = _prepare_season_state(
            season_key=active_season_key,
            state=None,
            clans=clans,
            windows=windows,
            saved_rows=scoped_saved_rows,
            use_saved_fallback=True,
            imported=imported,
            imported_season=active_season_key,
            saved_by_key=saved_by_key,
            composition_by_tag=composition_by_tag,
            user_column_links=user_column_links,
            config=config,
            fallback_clan_tags=fallback_clan_tags,
        )
        if fallback_clan_tags:
            formatted_tags = ", ".join(sorted(fallback_clan_tags))
            warnings.append(
                f"{active_season_key}: старый active raid season восстановлен "
                f"из SQLite для кланов {formatted_tags}."
            )

    empty_blocks = ()
    if selected_season is None:
        empty_blocks = tuple(
            RaidClanBlock(
                clan_tag=normalize_tag(clan.clan_tag),
                clan_name=clan.clan_name,
                message="Нет данных рейдового уикенда за доступный период",
            )
            for clan in clans
        )
    diff = _build_raid_diff(selected_season, scoped_saved_rows)
    return PreparedRaidSync(
        selected_season=selected_season,
        previous_active_season=previous_active_season,
        empty_blocks=empty_blocks,
        warnings=tuple(warnings),
        diff=diff,
        showing_saved_season=showing_saved_season,
    )


def _prepare_season_state(
    *,
    season_key: str,
    state: RaidSeasonState | None,
    clans: Sequence[TrackedClan],
    windows: Mapping[str, Sequence[JsonObject]],
    saved_rows: Sequence[RaidPlayerState],
    use_saved_fallback: bool,
    imported: Mapping[str, Mapping[str, str]],
    imported_season: str | None,
    saved_by_key: Mapping[str, RaidPlayerState],
    composition_by_tag: Mapping[str, PlannedPlayerState],
    user_column_links: Mapping[str, tuple[str, ...]],
    config: AppConfig,
    fallback_clan_tags: set[str] | None = None,
) -> PreparedRaidSeason:
    matching_raw: list[tuple[str, JsonObject]] = []
    for clan_tag, items in windows.items():
        for item in items:
            if _raw_season_key(item) == season_key:
                matching_raw.append((clan_tag, item))

    saved_for_season = tuple(row for row in saved_rows if row.season_key == season_key)
    if not matching_raw and not saved_for_season:
        raise RaidDataError(
            f"{season_key}: отсутствует API и SQLite state известного active raid season."
        )

    parsed_by_clan: dict[str, ParsedRaidSeason] = {}
    for clan_tag, raw in matching_raw:
        if clan_tag in parsed_by_clan:
            raise RaidContractError(f"{clan_tag}: raid season {season_key} встречается дважды.")
        parsed_by_clan[clan_tag] = parse_raid_season(raw, clan_tag=clan_tag)

    if parsed_by_clan:
        first = next(iter(parsed_by_clan.values()))
        start_time = _season_key(first.start_time)
        end_time = _season_key(first.end_time)
        for parsed in parsed_by_clan.values():
            if _season_key(parsed.end_time) != end_time:
                raise RaidContractError(
                    f"{season_key}: активные кланы вернули разные endTime raid season."
                )
        season_state: RaidSeasonState = (
            "ongoing"
            if state == "ongoing"
            or any(item.state == "ongoing" for item in parsed_by_clan.values())
            else "ended"
        )
    else:
        first_saved = max(
            saved_for_season,
            key=lambda row: (row.season_start_at, row.season_end_at),
        )
        start_time = first_saved.season_start_at
        end_time = first_saved.season_end_at
        season_state = _consistent_saved_state(saved_for_season)

    period = _raid_period(start_time, end_time)
    blocks: list[RaidClanBlock] = []
    for clan in clans:
        clan_tag = normalize_tag(clan.clan_tag)
        parsed = parsed_by_clan.get(clan_tag)
        technical_rows: tuple[RaidTechnicalValues, ...] = ()
        if parsed is not None:
            technical_rows = aggregate_raid_season(
                parsed,
                attacks_target=config.raid_attacks_target,
                normal_district_attack_norm=config.raid_normal_district_attack_norm,
                capital_district_attack_norm=config.raid_capital_district_attack_norm,
            )
        elif use_saved_fallback:
            stored = tuple(
                row for row in saved_for_season if normalize_tag(row.clan_tag) == clan_tag
            )
            technical_rows = tuple(_technical_from_state(row) for row in stored)
            if stored and fallback_clan_tags is not None:
                fallback_clan_tags.add(clan_tag)

        if not technical_rows:
            blocks.append(
                RaidClanBlock(
                    clan_tag=clan_tag,
                    clan_name=clan.clan_name,
                    message=f"Нет данных рейдового уикенда {period}",
                )
            )
            continue

        planned = [
            _planned_row(
                values,
                season_key=season_key,
                clan_tag=clan_tag,
                imported=imported if imported_season == season_key else {},
                saved_by_key=saved_by_key,
                composition_by_tag=composition_by_tag,
                user_column_links=user_column_links,
            )
            for values in technical_rows
        ]
        planned.sort(
            key=lambda row: (
                -row.technical_values.coefficient,
                -row.technical_values.attacks,
                row.technical_values.player_name.casefold(),
                row.technical_values.player_tag,
            )
        )
        ranked = tuple(
            RaidPlannedRow(
                row_key=row.row_key,
                season_key=row.season_key,
                clan_tag=row.clan_tag,
                rank=index,
                technical_values=row.technical_values,
                user_values=row.user_values,
            )
            for index, row in enumerate(planned, start=1)
        )
        blocks.append(RaidClanBlock(clan_tag, clan.clan_name, ranked))

    return PreparedRaidSeason(
        season_key=season_key,
        start_time=start_time,
        end_time=end_time,
        state=season_state,
        blocks=tuple(blocks),
    )


async def _import_registered_raid_values(
    *,
    runtime_config: RuntimeChatConfig,
    sheets_client: SheetsClient,
    sheet_block_repository: SheetBlockRepository,
) -> tuple[dict[str, dict[str, str]], tuple[str, ...]]:
    active_season = runtime_config.sheet_binding.active_raid_season
    if active_season is None:
        return {}, ()

    sheet_name = runtime_config.sheet_binding.active_raid_sheet_name
    sheet_id = runtime_config.sheet_binding.active_raid_sheet_id
    blocks = await sheet_block_repository.list_blocks(runtime_config.chat_id, sheet_name)
    imported: dict[str, dict[str, str]] = {}
    warnings: list[str] = []
    seen_clans: set[str] = set()
    for block in blocks:
        if block.block_key.startswith(RAID_MESSAGE_BLOCK_PREFIX):
            continue
        if not block.block_key.startswith(RAID_BLOCK_PREFIX):
            continue
        if sheet_id is not None and block.sheet_id != sheet_id:
            raise RaidDataError(
                f"{block.block_key}: registered raid block указывает на другой sheet_id."
            )
        try:
            clan_tag = normalize_tag(block.block_key.removeprefix(RAID_BLOCK_PREFIX))
        except ValueError as exc:
            raise RaidDataError(f"{block.block_key}: повреждён clan tag managed block.") from exc
        if clan_tag in seen_clans:
            raise RaidDataError(f"{clan_tag}: найден дубликат registered raid block.")
        seen_clans.add(clan_tag)
        values = await sheets_client.read_values(
            sheet_name,
            range_from_start_cell(
                start_cell=block.start_cell,
                rows_count=block.rows_count,
                columns_count=block.columns_count,
            ),
        )
        rows, block_warnings = _parse_registered_raid_block(
            values=values,
            block_key=block.block_key,
            clan_tag=clan_tag,
            season_key=active_season,
            column_profiles=runtime_config.column_profiles,
        )
        warnings.extend(block_warnings)
        for row_key, user_values in rows.items():
            if row_key in imported:
                raise RaidDataError(f"{row_key}: найден дубликат импортированной raid row.")
            imported[row_key] = user_values
    return imported, tuple(warnings)


def _parse_registered_raid_block(
    *,
    values: Sequence[Sequence[CellValue]],
    block_key: str,
    clan_tag: str,
    season_key: str,
    column_profiles: Sequence[ColumnProfile],
) -> tuple[dict[str, dict[str, str]], tuple[str, ...]]:
    rows = [[_cell_text(cell) for cell in row] for row in values]
    header_rows = [
        (index, row)
        for index, row in enumerate(rows)
        if any(cell.strip() == BOT_KEY_TITLE for cell in row)
    ]
    if len(header_rows) != 1:
        raise RaidDataError(
            f"{block_key}: managed raid block должен содержать один header с {BOT_KEY_TITLE}."
        )
    header_index, header = header_rows[0]
    bot_key_index = _unique_header_index(header, BOT_KEY_TITLE, block_key)
    player_tag_profile = next(
        (
            profile
            for profile in column_profiles
            if profile.table_type == "raids"
            and profile.column_key == "player_tag"
            and profile.kind == "system"
        ),
        None,
    )
    if player_tag_profile is None:
        raise RaidDataError(f"{block_key}: отсутствует обязательный profile player_tag.")
    player_tag_index = _unique_header_identity_index(
        header,
        column_title_identity(player_tag_profile.title),
        block_key,
    )
    user_indexes = _raid_user_indexes_from_header(column_profiles, header, block_key)

    imported: dict[str, dict[str, str]] = {}
    warnings: list[str] = []
    for row_number, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if not any(cell.strip() for cell in row):
            continue
        bot_key = _cell_at_text(row, bot_key_index).strip()
        row_key = _validated_imported_row_key(
            bot_key,
            season_key=season_key,
            clan_tag=clan_tag,
        )
        if row_key is None:
            raw_player_tag = _cell_at_text(row, player_tag_index).strip()
            try:
                player_tag = normalize_tag(raw_player_tag)
            except ValueError as exc:
                raise RaidDataError(
                    f"{block_key}, строка {row_number}: повреждены __bot_key и Тег."
                ) from exc
            row_key = make_raid_row_key(season_key, clan_tag, player_tag)
            warnings.append(f"{block_key}, строка {row_number}: использован fallback по Тег.")
        if row_key in imported:
            raise RaidDataError(
                f"{block_key}, строка {row_number}: неоднозначный дубликат raid row {row_key}."
            )
        imported[row_key] = {
            column_key: _cell_at_text(row, column_index)
            for column_key, column_index in user_indexes.items()
        }
    return imported, tuple(warnings)


def _validated_imported_row_key(
    value: str,
    *,
    season_key: str,
    clan_tag: str,
) -> str | None:
    if value == "":
        return None
    if not value.startswith("raid_row:"):
        return None
    parts = value.removeprefix("raid_row:").split("|")
    if len(parts) != 3:
        return None
    raw_season, raw_clan, raw_player = parts
    try:
        normalized_clan = normalize_tag(raw_clan)
        normalized_player = normalize_tag(raw_player)
    except ValueError:
        return None
    if raw_season != season_key or normalized_clan != clan_tag:
        raise RaidDataError("__bot_key не соответствует active raid season или managed block.")
    return make_raid_row_key(season_key, clan_tag, normalized_player)


def _raid_user_indexes_from_header(
    column_profiles: Sequence[ColumnProfile],
    header: Sequence[str],
    block_key: str,
) -> dict[str, int]:
    result: dict[str, int] = {}
    for profile in column_profiles:
        if (
            profile.table_type != "raids"
            or profile.kind != "user"
            or not profile.visible
            or not profile.is_active
        ):
            continue
        identity = column_title_identity(profile.title)
        indexes = [
            index
            for index, value in enumerate(header)
            if value.strip() and column_title_identity(value) == identity
        ]
        if len(indexes) > 1:
            raise RaidDataError(
                f"{block_key}: заголовок user-колонки {profile.title!r} неоднозначен."
            )
        if indexes:
            result[profile.column_key] = indexes[0]
    return result


def _raid_composition_user_column_links(
    column_profiles: Sequence[ColumnProfile],
) -> dict[str, tuple[str, ...]]:
    table_order = {
        "composition_active": 0,
        "composition_exited": 1,
        "composition": 2,
    }
    composition_profiles = sorted(
        (
            profile
            for profile in column_profiles
            if profile.table_type in table_order
            and profile.kind == "user"
            and profile.visible
            and profile.is_active
        ),
        key=lambda profile: (
            table_order[profile.table_type],
            profile.sort_order,
            profile.column_key,
        ),
    )
    by_title: dict[str, list[str]] = {}
    for profile in composition_profiles:
        by_title.setdefault(column_title_identity(profile.title), []).append(profile.column_key)

    links: dict[str, tuple[str, ...]] = {}
    for profile in sorted(
        (
            item
            for item in column_profiles
            if item.table_type == "raids"
            and item.kind == "user"
            and item.visible
            and item.is_active
        ),
        key=lambda item: (item.sort_order, item.column_key),
    ):
        composition_keys = by_title.get(column_title_identity(profile.title))
        if composition_keys:
            links[profile.column_key] = tuple(composition_keys)
    return links


def _build_raid_diff(
    selected_season: PreparedRaidSeason | None,
    saved_rows: Sequence[RaidPlayerState],
) -> tuple[str, ...]:
    if selected_season is None:
        return ()
    existing = {
        row.row_key: row for row in saved_rows if row.season_key == selected_season.season_key
    }
    planned: dict[str, RaidPlannedRow] = {
        row.row_key: row for block in selected_season.blocks for row in block.rows
    }
    items: list[str] = []
    for row_key, row in planned.items():
        previous = existing.get(row_key)
        if previous is None:
            items.append(f"Добавлен участник {row.technical_values.player_tag}.")
            continue
        previous_values = decode_raid_technical_values(
            encode_raid_technical_values(previous.technical_values)
        )
        if (
            previous_values != _technical_values_dict(row.technical_values)
            or previous.user_values != row.user_values
        ):
            items.append(f"Обновлён участник {row.technical_values.player_tag}.")
    for row_key in sorted(existing.keys() - planned.keys()):
        items.append(f"Удалён участник {existing[row_key].player_tag}.")
    return tuple(items)


def _technical_values_dict(values: RaidTechnicalValues) -> dict[str, object]:
    return {
        "player_name": values.player_name,
        "attacks": values.attacks,
        "attack_limit": values.attack_limit,
        "bonus_attack_limit": values.bonus_attack_limit,
        "capital_resources_looted": values.capital_resources_looted,
        "weighted_damage_units": values.weighted_damage_units,
        "normal_points": values.normal_points,
        "coefficient": values.coefficient,
    }


def _validate_selection_metadata(
    item: object,
    *,
    clan_tag: str,
    index: int,
) -> tuple[RaidSeasonState, str]:
    context = f"{clan_tag}: raid season #{index}"
    if not isinstance(item, dict):
        raise RaidContractError(f"{context} должен быть объектом.")
    state = _season_state(item)
    start_time = _required_non_empty_str(item, "startTime", context)
    return state, _season_key(start_time)


def _raw_season_key(item: JsonObject) -> str:
    return _season_key(_required_non_empty_str(item, "startTime", "raid season"))


def _consistent_saved_state(rows: Sequence[RaidPlayerState]) -> RaidSeasonState:
    states = {_stored_state(row.season_state) for row in rows}
    if len(states) != 1:
        raise RaidDataError("SQLite raid season содержит противоречивые state.")
    return next(iter(states))


def _raid_period(start_time: str, end_time: str) -> str:
    return f"{start_time[:10]} — {end_time[:10]}"


def _unique_header_index(header: Sequence[str], title: str, block_key: str) -> int:
    indexes = [index for index, value in enumerate(header) if value.strip() == title]
    if len(indexes) != 1:
        raise RaidDataError(f"{block_key}: заголовок {title} должен встречаться один раз.")
    return indexes[0]


def _unique_header_identity_index(
    header: Sequence[str],
    identity: str,
    block_key: str,
) -> int:
    indexes = [
        index
        for index, value in enumerate(header)
        if value.strip() and column_title_identity(value) == identity
    ]
    if len(indexes) != 1:
        raise RaidDataError(
            f"{block_key}: обязательный заголовок {identity!r} должен встречаться один раз."
        )
    return indexes[0]


def _cell_text(value: CellValue) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        raise RaidDataError("Managed raid block содержит bool вместо значения ячейки.")
    return str(value)


def _cell_at_text(row: Sequence[str], index: int) -> str:
    return row[index] if index < len(row) else ""


def make_raid_row_key(season_key: str, clan_tag: str, player_tag: str) -> str:
    """Строит стабильный ключ raid row."""

    return f"raid_row:{season_key}|{normalize_tag(clan_tag)}|{normalize_tag(player_tag)}"


def classify_raid_district(
    district_id: int,
    district_name: str | None,
) -> RaidDistrictKind:
    """Классифицирует район по стабильному API ID.

    Args:
        district_id: Числовой идентификатор района.
        district_name: Необязательное имя района из API.

    Returns:
        `capital` для подтверждённого Capital Peak ID, иначе `normal`.

    Raises:
        RaidContractError: Если ID имеет неверный тип или имя Capital Peak
            противоречит подтверждённому ID.
    """

    if not _is_int(district_id):
        raise RaidContractError("district: поле id должно быть целым числом.")
    if (
        district_name is not None
        and district_name.strip().casefold() == CAPITAL_PEAK_NAME.casefold()
        and district_id != CAPITAL_PEAK_DISTRICT_ID
    ):
        raise RaidContractError(
            "district Capital Peak содержит неподтверждённый id "
            f"{district_id}; ожидается {CAPITAL_PEAK_DISTRICT_ID}.",
        )
    if district_id == CAPITAL_PEAK_DISTRICT_ID:
        return "capital"
    return "normal"


def parse_raid_season(data: JsonObject, *, clan_tag: str) -> ParsedRaidSeason:
    """Строго разбирает один используемый raid season object.

    Районы с `attackCount = 0` реальный API может вернуть без поля `attacks`;
    они не содержат вклада и пропускаются. Для района с атаками список
    `attacks` обязателен.

    Args:
        data: JSON-объект одного выбранного сезона.
        clan_tag: Тег клана, которому принадлежит ответ.

    Returns:
        Проверенный сезон с нормализованными тегами и плоским списком
        вкладов атак.

    Raises:
        RaidContractError: Если обязательный контракт нарушен.
        RaidRetryableDataError: Если ongoing-сезон временно не согласовал
            счётчики участников с attack log.
    """

    if not isinstance(data, dict):
        raise RaidContractError("raid season должен быть объектом.")
    normalized_clan_tag = _normalize_api_tag(clan_tag, "raid season clan")
    state = _season_state(data)
    start_time = _required_non_empty_str(data, "startTime", "raid season")
    end_time = _required_non_empty_str(data, "endTime", "raid season")
    members = _parse_members(_required_list(data, "members", "raid season"))
    attacks = _parse_attack_log(_required_list(data, "attackLog", "raid season"))
    _validate_attack_counters(state=state, members=members, attacks=attacks)
    return ParsedRaidSeason(
        clan_tag=normalized_clan_tag,
        state=state,
        start_time=start_time,
        end_time=end_time,
        members=members,
        attacks=attacks,
    )


def aggregate_raid_season(
    season: ParsedRaidSeason,
    *,
    attacks_target: int,
    normal_district_attack_norm: int,
    capital_district_attack_norm: int,
) -> tuple[RaidTechnicalValues, ...]:
    """Агрегирует атаки сезона до технических значений участников.

    Args:
        season: Проверенный рейдовый сезон.
        attacks_target: Знаменатель коэффициента.
        normal_district_attack_norm: Множитель обычного района.
        capital_district_attack_norm: Множитель Capital Peak.

    Returns:
        Строки участников в исходном порядке `members`.

    Raises:
        ValueError: Если норма или целевое число атак не положительны.
    """

    _validate_positive_int(attacks_target, "attacks_target")
    _validate_positive_int(normal_district_attack_norm, "normal_district_attack_norm")
    _validate_positive_int(capital_district_attack_norm, "capital_district_attack_norm")

    weighted_units: defaultdict[str, int] = defaultdict(int)
    for attack in season.attacks:
        multiplier = (
            capital_district_attack_norm
            if attack.district_kind == "capital"
            else normal_district_attack_norm
        )
        weighted_units[attack.player_tag] += attack.destruction_delta_percent * multiplier

    coefficient_denominator = 100 * attacks_target
    return tuple(
        RaidTechnicalValues(
            player_tag=member.player_tag,
            player_name=member.player_name,
            attacks=member.attacks,
            attack_limit=member.attack_limit,
            bonus_attack_limit=member.bonus_attack_limit,
            capital_resources_looted=member.capital_resources_looted,
            weighted_damage_units=weighted_units[member.player_tag],
            normal_points=Decimal(weighted_units[member.player_tag]) / Decimal(100),
            coefficient=Decimal(weighted_units[member.player_tag])
            / Decimal(coefficient_denominator),
        )
        for member in season.members
    )


def _season_key(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise RaidContractError(f"Некорректный raid season time: {value}.") from exc
    return parsed.isoformat()


def _planned_row(
    values: RaidTechnicalValues,
    *,
    season_key: str,
    clan_tag: str,
    imported: Mapping[str, Mapping[str, str]],
    saved_by_key: Mapping[str, RaidPlayerState],
    composition_by_tag: Mapping[str, PlannedPlayerState],
    user_column_links: Mapping[str, tuple[str, ...]],
) -> RaidPlannedRow:
    row_key = make_raid_row_key(season_key, clan_tag, values.player_tag)
    saved = saved_by_key.get(row_key)
    user_values = {} if saved is None else dict(saved.user_values)
    if row_key in imported:
        user_values.update(imported[row_key])

    composition = composition_by_tag.get(values.player_tag)
    if composition is not None:
        for raid_key, composition_keys in user_column_links.items():
            if user_values.get(raid_key, "").strip() != "":
                continue
            for composition_key in composition_keys:
                composition_value = composition.user_values.get(composition_key, "")
                if composition_value.strip() != "":
                    user_values[raid_key] = composition_value
                    break
    return RaidPlannedRow(row_key, season_key, clan_tag, 0, values, user_values)


def _technical_from_state(state: RaidPlayerState) -> RaidTechnicalValues:
    data = decode_raid_technical_values(encode_raid_technical_values(state.technical_values))
    return RaidTechnicalValues(
        player_tag=state.player_tag,
        player_name=data["player_name"],  # type: ignore[arg-type]
        attacks=data["attacks"],  # type: ignore[arg-type]
        attack_limit=data["attack_limit"],  # type: ignore[arg-type]
        bonus_attack_limit=data["bonus_attack_limit"],  # type: ignore[arg-type]
        capital_resources_looted=data["capital_resources_looted"],  # type: ignore[arg-type]
        weighted_damage_units=data["weighted_damage_units"],  # type: ignore[arg-type]
        normal_points=data["normal_points"],  # type: ignore[arg-type]
        coefficient=data["coefficient"],  # type: ignore[arg-type]
    )


def _stored_state(value: str) -> RaidSeasonState:
    if value not in {"ongoing", "ended"}:
        raise RaidContractError(f"Некорректный сохранённый raid state: {value}.")
    return value


def _parse_members(raw_members: list[Any]) -> tuple[RaidMember, ...]:
    """Разбирает и проверяет список участников сезона."""

    members: list[RaidMember] = []
    seen_tags: set[str] = set()
    for index, raw_member in enumerate(raw_members, start=1):
        context = f"raid member #{index}"
        member = _required_dict_item(raw_member, context)
        player_tag = _normalize_api_tag(
            _required_non_empty_str(member, "tag", context),
            context,
        )
        if player_tag in seen_tags:
            raise RaidContractError(f"{context}: повторяющийся tag {player_tag}.")
        seen_tags.add(player_tag)
        members.append(
            RaidMember(
                player_tag=player_tag,
                player_name=_required_non_empty_str(member, "name", context),
                attacks=_required_non_negative_int(member, "attacks", context),
                attack_limit=_required_non_negative_int(member, "attackLimit", context),
                bonus_attack_limit=_required_non_negative_int(
                    member,
                    "bonusAttackLimit",
                    context,
                ),
                capital_resources_looted=_required_non_negative_int(
                    member,
                    "capitalResourcesLooted",
                    context,
                ),
            ),
        )
    return tuple(members)


def _parse_attack_log(raw_logs: list[Any]) -> tuple[RaidAttack, ...]:
    """Разбирает атаки и превращает cumulative-урон в их вклады."""

    attacks: list[RaidAttack] = []
    for log_index, raw_log in enumerate(raw_logs, start=1):
        log_context = f"raid attack log #{log_index}"
        log = _required_dict_item(raw_log, log_context)
        raw_districts = _required_list(log, "districts", log_context)
        for district_index, raw_district in enumerate(raw_districts, start=1):
            district_context = f"{log_context} district #{district_index}"
            district = _required_dict_item(raw_district, district_context)
            district_id = _required_int(district, "id", district_context)
            district_name = _optional_str(district, "name", district_context)
            district_kind = classify_raid_district(district_id, district_name)
            district_destruction = _required_int(
                district,
                "destructionPercent",
                district_context,
            )
            if not 0 <= district_destruction <= 100:
                raise RaidContractError(
                    f"{district_context}: destructionPercent должен быть в диапазоне 0..100.",
                )
            raw_attacks = _district_attacks(district, district_context)
            cumulative_attacks: list[tuple[str, int]] = []
            for attack_index, raw_attack in enumerate(raw_attacks, start=1):
                attack_context = f"{district_context} attack #{attack_index}"
                attack = _required_dict_item(raw_attack, attack_context)
                attacker = _required_dict(attack, "attacker", attack_context)
                player_tag = _normalize_api_tag(
                    _required_non_empty_str(attacker, "tag", f"{attack_context} attacker"),
                    f"{attack_context} attacker",
                )
                destruction_percent = _required_int(
                    attack,
                    "destructionPercent",
                    attack_context,
                )
                if not 0 <= destruction_percent <= 100:
                    raise RaidContractError(
                        f"{attack_context}: destructionPercent должен быть в диапазоне 0..100.",
                    )
                cumulative_attacks.append((player_tag, destruction_percent))

            if not cumulative_attacks:
                if district_destruction != 0:
                    raise RaidContractError(
                        f"{district_context}: итоговый destructionPercent "
                        "не совпадает с вкладом атак.",
                    )
                continue
            if cumulative_attacks[0][1] != district_destruction:
                raise RaidContractError(
                    f"{district_context}: cumulative-атаки не согласованы "
                    "с итоговым destructionPercent района.",
                )

            district_contribution = 0
            for attack_index, (player_tag, cumulative_percent) in enumerate(
                cumulative_attacks,
                start=1,
            ):
                previous_cumulative = (
                    cumulative_attacks[attack_index][1]
                    if attack_index < len(cumulative_attacks)
                    else 0
                )
                if cumulative_percent < previous_cumulative:
                    raise RaidContractError(
                        f"{district_context}: attacks содержат немонотонную "
                        "reverse-cumulative последовательность destructionPercent.",
                    )
                contribution = cumulative_percent - previous_cumulative
                district_contribution += contribution
                attacks.append(
                    RaidAttack(
                        player_tag=player_tag,
                        district_kind=district_kind,
                        destruction_delta_percent=contribution,
                    ),
                )
            if district_contribution != district_destruction:
                raise RaidContractError(
                    f"{district_context}: сумма вкладов атак не совпадает "
                    "с итоговым destructionPercent района.",
                )
    return tuple(attacks)


def _district_attacks(district: JsonObject, context: str) -> list[Any]:
    """Читает атаки района с учётом реального zero-attack представления API."""

    raw_attacks = district.get("attacks")
    if isinstance(raw_attacks, list):
        return raw_attacks
    if raw_attacks is not None:
        raise RaidContractError(f"{context}: поле attacks должно быть списком.")

    attack_count = district.get("attackCount")
    if _is_int(attack_count) and attack_count == 0:
        return []
    raise RaidContractError(f"{context}: отсутствует обязательное поле attacks.")


def _validate_attack_counters(
    *,
    state: RaidSeasonState,
    members: tuple[RaidMember, ...],
    attacks: tuple[RaidAttack, ...],
) -> None:
    """Сверяет member counters с разобранными атаками."""

    expected = Counter({member.player_tag: member.attacks for member in members})
    actual = Counter(attack.player_tag for attack in attacks)
    if expected == actual:
        return

    message = (
        "Количество атак в members не совпадает с raid attack log: "
        f"ожидалось {sum(expected.values())}, разобрано {sum(actual.values())}."
    )
    if state == "ongoing":
        raise RaidRetryableDataError(f"{message} Повторите /sync позже.")
    raise RaidContractError(message)


def _season_state(data: JsonObject) -> RaidSeasonState:
    """Читает поддерживаемое состояние сезона."""

    state = _required_non_empty_str(data, "state", "raid season")
    if state == "ongoing":
        return "ongoing"
    if state == "ended":
        return "ended"
    raise RaidContractError(f"raid season: неподдерживаемое состояние {state!r}.")


def _required_non_empty_str(data: JsonObject, key: str, context: str) -> str:
    """Читает обязательную непустую строку."""

    value = data.get(key)
    if not isinstance(value, str) or value.strip() == "":
        raise RaidContractError(f"{context}: поле {key} должно быть непустой строкой.")
    return value


def _optional_str(data: JsonObject, key: str, context: str) -> str | None:
    """Читает необязательную строку."""

    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RaidContractError(f"{context}: поле {key} должно быть строкой.")
    return value


def _required_int(data: JsonObject, key: str, context: str) -> int:
    """Читает обязательное целое число, исключая bool."""

    value = data.get(key)
    if not _is_int(value):
        raise RaidContractError(f"{context}: поле {key} должно быть целым числом.")
    return value


def _required_non_negative_int(data: JsonObject, key: str, context: str) -> int:
    """Читает обязательное неотрицательное целое число."""

    value = _required_int(data, key, context)
    if value < 0:
        raise RaidContractError(f"{context}: поле {key} не может быть отрицательным.")
    return value


def _required_list(data: JsonObject, key: str, context: str) -> list[Any]:
    """Читает обязательный список."""

    value = data.get(key)
    if not isinstance(value, list):
        raise RaidContractError(f"{context}: поле {key} должно быть списком.")
    return value


def _required_dict(data: JsonObject, key: str, context: str) -> JsonObject:
    """Читает обязательный вложенный объект."""

    value = data.get(key)
    if not isinstance(value, dict):
        raise RaidContractError(f"{context}: поле {key} должно быть объектом.")
    return value


def _required_dict_item(value: Any, context: str) -> JsonObject:
    """Проверяет объект внутри внешнего списка."""

    if not isinstance(value, dict):
        raise RaidContractError(f"{context} должен быть объектом.")
    return value


def _normalize_api_tag(value: str, context: str) -> str:
    """Нормализует тег или преобразует ошибку в доменную."""

    try:
        return normalize_tag(value)
    except ValueError as exc:
        raise RaidContractError(f"{context}: некорректный tag.") from exc


def _validate_positive_int(value: int, name: str) -> None:
    """Проверяет положительный целочисленный аргумент формулы."""

    if not _is_int(value) or value <= 0:
        raise ValueError(f"{name} должен быть положительным целым числом.")


def _is_int(value: object) -> bool:
    """Проверяет строгий int без принятия bool."""

    return isinstance(value, int) and not isinstance(value, bool)
