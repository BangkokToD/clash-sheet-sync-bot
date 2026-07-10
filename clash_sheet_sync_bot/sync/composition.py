"""Синхронизация листа состава в публичной runtime-архитектуре."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from clash_sheet_sync_bot.coc.client import ClashClient
from clash_sheet_sync_bot.common.time import format_dt as _format_dt
from clash_sheet_sync_bot.models import (
    ColumnProfile,
    RuntimeChatConfig,
    SheetBlock,
    TableType,
    TrackedClan,
    normalize_tag,
)
from clash_sheet_sync_bot.repositories import (
    CompositionPlayerState,
    CompositionPlayerStateRepository,
    SheetBlockRepository,
)
from clash_sheet_sync_bot.sheets.client import (
    CellValue,
    SheetsClient,
    SheetValues,
    range_from_start_cell,
)
from clash_sheet_sync_bot.sheets.column_profiles import (
    BOT_KEY_COLUMN_KEY,
    BOT_KEY_TITLE,
    table_title,
)
from clash_sheet_sync_bot.sheets.ranges import (
    column_to_number as _shared_column_to_number,
    grid_range_from_start_cell as _shared_grid_range_from_start_cell,
    number_to_column as _shared_number_to_column,
    offset_cell as _shared_offset_cell,
    parse_a1_cell as _shared_parse_a1_cell,
)

COMPOSITION_PLAYER_KEY_PREFIX: Final = "composition_player:"
ACTIVE_BLOCK_PREFIX: Final = "composition_active:"
EXITED_BLOCK_KEY: Final = "composition_exited"
COMPOSITION_ACTIVE_TABLE: Final[TableType] = "composition_active"
COMPOSITION_EXITED_TABLE: Final[TableType] = "composition_exited"
DEFAULT_ACTIVE_START_CELL: Final = "A1"
TITLE_ROWS_COUNT: Final = 2

GREEN_RGB: Final = {"red": 0.18, "green": 0.42, "blue": 0.31}
DARK_GREEN_RGB: Final = {"red": 0.12, "green": 0.32, "blue": 0.24}
WHITE_RGB: Final = {"red": 1.0, "green": 1.0, "blue": 1.0}
BLACK_RGB: Final = {"red": 0.0, "green": 0.0, "blue": 0.0}
LIGHT_BAND_RGB: Final = {"red": 0.95, "green": 0.97, "blue": 0.96}
BORDER_RGB: Final = {"red": 0.70, "green": 0.76, "blue": 0.73}

JsonObject = dict[str, Any]
JsonDict = dict[str, str]


class CompositionSyncError(RuntimeError):
    """Базовая ошибка синхронизации состава."""


class CompositionDataError(CompositionSyncError):
    """Ошибка данных листа или API, при которой лист нельзя менять."""


@dataclass(frozen=True, slots=True)
class ImportedPlayerValues:
    """Ручные и технические данные игрока, импортированные из текущего листа.

    Attributes:
        player_tag: Нормализованный тег игрока.
        is_exited: Была ли строка прочитана из блока `Вышедшие`.
        clan_tag: Тег клана для active-блока или `None`.
        town_hall: Ратуша из видимой system-колонки или `None`.
        nickname: Ник из видимой system-колонки или `None`.
        exited_at: Дата выхода из visible system-колонки или `None`.
        user_values: Значения видимых user-колонок по `column_key`.
    """

    player_tag: str
    is_exited: bool
    clan_tag: str | None
    town_hall: int | None
    nickname: str | None
    exited_at: str | None
    user_values: JsonDict


@dataclass(frozen=True, slots=True)
class CompositionImportResult:
    """Результат чтения текущего листа состава.

    Attributes:
        players: Игроки по player tag.
        warnings: Предупреждения импорта ручных полей.
        saw_exited_block: Был ли прочитан предыдущий managed block `Вышедшие`.
    """

    players: dict[str, ImportedPlayerValues]
    warnings: tuple[str, ...]
    saw_exited_block: bool = False


@dataclass(frozen=True, slots=True)
class CurrentClanMember:
    """Текущий участник активного отслеживаемого клана.

    Attributes:
        player_tag: Нормализованный тег игрока.
        nickname: Никнейм из CoC API.
        town_hall: Уровень ратуши из CoC API.
        clan_tag: Тег текущего клана.
        clan_name: Название текущего клана.
    """

    player_tag: str
    nickname: str
    town_hall: int
    clan_tag: str
    clan_name: str


@dataclass(frozen=True, slots=True)
class PlannedPlayerState:
    """Новое состояние игрока после сверки с CoC API.

    Attributes:
        player_tag: Нормализованный тег игрока.
        status: `active`, `exited` или `untracked`.
        clan_tag: Тег активного клана или `None`.
        town_hall: Уровень ратуши или `None`.
        nickname: Никнейм или `None`.
        exited_at: ISO-дата выхода или `None`.
        user_values: Пользовательские поля по `column_key`.
        last_seen_at: ISO-дата последнего наблюдения в CoC API или `None`.
    """

    player_tag: str
    status: str
    clan_tag: str | None
    town_hall: int | None
    nickname: str | None
    exited_at: str | None
    user_values: JsonDict
    last_seen_at: str | None


@dataclass(frozen=True, slots=True)
class CompositionDiffItem:
    """Одно изменение состава.

    Attributes:
        kind: Тип изменения.
        message: Человекочитаемое сообщение.
    """

    kind: str
    message: str


@dataclass(frozen=True, slots=True)
class CompositionSyncResult:
    """Результат синхронизации состава.

    Attributes:
        active_counts: Количество активных игроков по кланам.
        exited_count: Количество игроков в блоке вышедших.
        diff_items: Изменения состава.
        warnings: Предупреждения импорта ручных полей.
    """

    active_counts: tuple[tuple[str, int], ...]
    exited_count: int
    diff_items: tuple[CompositionDiffItem, ...]
    warnings: tuple[str, ...]

    def to_telegram_message(self) -> str:
        """Формирует Telegram-отчёт по составу.

        Returns:
            Короткий отчёт для будущего `/sync` orchestration.
        """

        lines = ["Состав обновлён.", ""]
        lines.extend(f"{clan_name}: {count}" for clan_name, count in self.active_counts)
        lines.append(f"Вышедшие: {self.exited_count}")
        if self.diff_items:
            lines.extend(["", "Изменения:"])
            lines.extend(item.message for item in self.diff_items)
        if self.warnings:
            lines.extend(["", "Предупреждения:"])
            lines.extend(self.warnings)
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class BuiltBlock:
    """Построенный блок листа состава.

    Attributes:
        block: Описание блока для `sheet_blocks`.
        values: Матрица значений.
    """

    block: SheetBlock
    values: list[list[CellValue]]


@dataclass(frozen=True, slots=True)
class PreparedCompositionSync:
    """Подготовленная синхронизация состава без записи в Google Sheets.

    Attributes:
        planned_states: Новые состояния игроков по player tag.
        built_blocks: Будущие managed-блоки листа `Состав`.
        active_counts: Количество активных игроков по кланам.
        exited_count: Количество игроков в блоке вышедших.
        diff_items: Технические изменения состава.
        warnings: Предупреждения импорта ручных полей.
    """

    planned_states: dict[str, PlannedPlayerState]
    built_blocks: tuple[BuiltBlock, ...]
    active_counts: tuple[tuple[str, int], ...]
    exited_count: int
    diff_items: tuple[CompositionDiffItem, ...]
    warnings: tuple[str, ...]


async def run_composition_sync(
    *,
    runtime_config: RuntimeChatConfig,
    clash_client: ClashClient,
    sheets_client: SheetsClient,
    composition_repository: CompositionPlayerStateRepository,
    sheet_block_repository: SheetBlockRepository,
    detected_at: datetime,
) -> CompositionSyncResult:
    """Выполняет синхронизацию листа `Состав`.

    Args:
        runtime_config: Runtime-настройки Telegram-чата.
        clash_client: Клиент Clash of Clans API.
        sheets_client: Клиент Google Sheets API.
        composition_repository: Repository состояния игроков состава.
        sheet_block_repository: Repository последних записанных блоков листа.
        detected_at: Дата обнаружения изменений.

    Returns:
        Результат синхронизации состава.

    Raises:
        CompositionDataError: Если runtime-конфиг или данные листа некорректны.
        ClashApiUnavailableError: Если CoC API недоступно.
    """

    prepared = await prepare_composition_sync(
        runtime_config=runtime_config,
        clash_client=clash_client,
        sheets_client=sheets_client,
        composition_repository=composition_repository,
        sheet_block_repository=sheet_block_repository,
        detected_at=detected_at,
    )
    await apply_prepared_composition_sync(
        runtime_config=runtime_config,
        sheets_client=sheets_client,
        composition_repository=composition_repository,
        sheet_block_repository=sheet_block_repository,
        detected_at=detected_at,
        prepared=prepared,
    )
    return CompositionSyncResult(
        active_counts=prepared.active_counts,
        exited_count=prepared.exited_count,
        diff_items=prepared.diff_items,
        warnings=prepared.warnings,
    )


async def prepare_composition_sync(
    *,
    runtime_config: RuntimeChatConfig,
    clash_client: ClashClient,
    sheets_client: SheetsClient,
    composition_repository: CompositionPlayerStateRepository,
    sheet_block_repository: SheetBlockRepository,
    detected_at: datetime,
) -> PreparedCompositionSync:
    """Готовит данные состава без записи в Google Sheets и SQLite.

    Метод нужен для staged `/sync`: сначала загружаются все CoC/Sheets-данные
    и строятся будущие состояния, и только потом оркестратор начинает запись.
    """

    if not runtime_config.active_clans:
        raise CompositionDataError("Для синхронизации состава нужен хотя бы один активный клан.")

    sheet_name = runtime_config.sheet_binding.composition_sheet_name
    previous_blocks = await sheet_block_repository.list_blocks(runtime_config.chat_id, sheet_name)
    composition_blocks = tuple(
        block
        for block in previous_blocks
        if block.block_key.startswith(ACTIVE_BLOCK_PREFIX) or block.block_key == EXITED_BLOCK_KEY
    )
    imported = await import_current_composition_sheet(
        runtime_config=runtime_config,
        sheets_client=sheets_client,
        blocks=composition_blocks,
    )
    existing_state = await composition_repository.list_players(runtime_config.chat_id)
    current_members = await _load_current_members(runtime_config.active_clans, clash_client)
    planned_states, diff_items = _plan_player_states(
        runtime_config=runtime_config,
        existing_state=existing_state,
        imported=imported,
        current_members=current_members,
        detected_at=detected_at,
    )
    built_blocks = build_composition_blocks(
        runtime_config=runtime_config,
        planned_states=planned_states,
    )
    active_counts = tuple(
        (
            clan.clan_name,
            sum(
                1
                for state in planned_states.values()
                if state.status == "active" and state.clan_tag == clan.clan_tag
            ),
        )
        for clan in runtime_config.active_clans
    )
    exited_count = sum(1 for state in planned_states.values() if state.status == "exited")
    return PreparedCompositionSync(
        planned_states=planned_states,
        built_blocks=built_blocks,
        active_counts=active_counts,
        exited_count=exited_count,
        diff_items=tuple(diff_items),
        warnings=imported.warnings,
    )


async def apply_prepared_composition_sync(
    *,
    runtime_config: RuntimeChatConfig,
    sheets_client: SheetsClient,
    composition_repository: CompositionPlayerStateRepository,
    sheet_block_repository: SheetBlockRepository,
    detected_at: datetime,
    prepared: PreparedCompositionSync,
) -> None:
    """Записывает подготовленный состав в Google Sheets и SQLite."""

    sheet_name = runtime_config.sheet_binding.composition_sheet_name
    previous_blocks = await sheet_block_repository.list_blocks(runtime_config.chat_id, sheet_name)
    composition_blocks = tuple(
        block
        for block in previous_blocks
        if block.block_key.startswith(ACTIVE_BLOCK_PREFIX) or block.block_key == EXITED_BLOCK_KEY
    )
    await _rewrite_composition_blocks(
        sheets_client=sheets_client,
        sheet_name=sheet_name,
        previous_blocks=composition_blocks,
        built_blocks=prepared.built_blocks,
    )
    await _format_composition_sheet(
        runtime_config=runtime_config,
        sheets_client=sheets_client,
        built_blocks=prepared.built_blocks,
    )
    await _hide_bot_key_columns(
        runtime_config=runtime_config,
        sheets_client=sheets_client,
        built_blocks=prepared.built_blocks,
    )

    updated_at = _format_dt(detected_at)
    for planned in prepared.planned_states.values():
        await composition_repository.upsert_player_state(
            chat_id=runtime_config.chat_id,
            player_tag=planned.player_tag,
            status=planned.status,
            clan_tag=planned.clan_tag,
            town_hall=planned.town_hall,
            nickname=planned.nickname,
            exited_at=planned.exited_at,
            user_values=planned.user_values,
            last_seen_at=planned.last_seen_at,
            updated_at=updated_at,
        )

    await sheet_block_repository.replace_blocks_by_prefixes(
        chat_id=runtime_config.chat_id,
        sheet_name=sheet_name,
        block_key_prefixes=(ACTIVE_BLOCK_PREFIX, EXITED_BLOCK_KEY),
        blocks=tuple(built_block.block for built_block in prepared.built_blocks),
        updated_at=updated_at,
    )


async def import_current_composition_sheet(
    *,
    runtime_config: RuntimeChatConfig,
    sheets_client: SheetsClient,
    blocks: Sequence[SheetBlock],
) -> CompositionImportResult:
    """Импортирует ручные правки из текущих managed-блоков листа `Состав`.

    Args:
        runtime_config: Runtime-настройки Telegram-чата.
        sheets_client: Клиент Google Sheets API.
        blocks: Последние записанные блоки состава из `sheet_blocks`.

    Returns:
        Импортированные user-values и warnings.
    """

    players: dict[str, ImportedPlayerValues] = {}
    warnings: list[str] = []
    sheet_name = runtime_config.sheet_binding.composition_sheet_name

    saw_exited_block = False
    for block in blocks:
        table_type = _table_type_from_block_key(block.block_key)
        if table_type is None:
            continue
        is_exited_block = _is_exited_block_key(block.block_key)
        saw_exited_block = saw_exited_block or is_exited_block
        block_range = range_from_start_cell(
            start_cell=block.start_cell,
            rows_count=block.rows_count,
            columns_count=block.columns_count,
        )
        values = await sheets_client.read_values(sheet_name, block_range)
        block_players, block_warnings = _parse_imported_block(
            runtime_config=runtime_config,
            block=block,
            table_type=table_type,
            is_exited=is_exited_block,
            values=values,
        )
        warnings.extend(block_warnings)
        for player in block_players:
            if player.player_tag in players:
                warnings.append(
                    f"Дубль строки игрока {player.player_tag} на листе `Состав`; использована первая строка."
                )
                continue
            players[player.player_tag] = player

    return CompositionImportResult(
        players=players,
        warnings=tuple(warnings),
        saw_exited_block=saw_exited_block,
    )


def build_composition_blocks(
    *,
    runtime_config: RuntimeChatConfig,
    planned_states: dict[str, PlannedPlayerState],
) -> tuple[BuiltBlock, ...]:
    """Строит блоки активных кланов и блока `Вышедшие`.

    Args:
        runtime_config: Runtime-настройки Telegram-чата.
        planned_states: Запланированное состояние игроков.

    Returns:
        Блоки значений и metadata для `sheet_blocks`.
    """

    active_columns = _physical_columns(runtime_config.column_profiles, COMPOSITION_ACTIVE_TABLE)
    exited_columns = _physical_columns(runtime_config.column_profiles, COMPOSITION_EXITED_TABLE)
    active_width = len(active_columns)
    exited_start_column = active_width + 2
    built_blocks: list[BuiltBlock] = []
    row_cursor = 1

    for clan in runtime_config.active_clans:
        states = _sorted_active_states(planned_states.values(), clan.clan_tag)
        values = _build_block_values(
            title=f"{clan.clan_name} | {clan.clan_tag}",
            columns=active_columns,
            states=states,
            is_exited=False,
        )
        block = SheetBlock(
            chat_id=runtime_config.chat_id,
            sheet_name=runtime_config.sheet_binding.composition_sheet_name,
            sheet_id=runtime_config.sheet_binding.composition_sheet_id,
            block_key=f"{ACTIVE_BLOCK_PREFIX}{clan.clan_tag}",
            start_cell=f"A{row_cursor}",
            rows_count=len(values),
            columns_count=active_width,
        )
        built_blocks.append(BuiltBlock(block=block, values=values))
        row_cursor += len(values) + 1

    exited_states = _sorted_exited_states(planned_states.values())
    exited_values = _build_block_values(
        title="Вышедшие",
        columns=exited_columns,
        states=exited_states,
        is_exited=True,
    )
    exited_block = SheetBlock(
        chat_id=runtime_config.chat_id,
        sheet_name=runtime_config.sheet_binding.composition_sheet_name,
        sheet_id=runtime_config.sheet_binding.composition_sheet_id,
        block_key=EXITED_BLOCK_KEY,
        start_cell=f"{_number_to_column(exited_start_column)}1",
        rows_count=len(exited_values),
        columns_count=len(exited_columns),
    )
    built_blocks.append(BuiltBlock(block=exited_block, values=exited_values))
    return tuple(built_blocks)


async def _load_current_members(
    active_clans: Sequence[TrackedClan],
    clash_client: ClashClient,
) -> dict[str, CurrentClanMember]:
    """Загружает участников всех active tracked clans.

    Args:
        active_clans: Активные кланы runtime-конфига.
        clash_client: Клиент CoC API.

    Returns:
        Участники по player tag.
    """

    members: dict[str, CurrentClanMember] = {}
    for clan in active_clans:
        raw_members = await clash_client.get_clan_members(clan.clan_tag)
        for raw_member in raw_members:
            player_tag = _member_tag(raw_member)
            if player_tag in members:
                previous = members[player_tag]
                raise CompositionDataError(
                    f"Игрок {player_tag} найден сразу в двух активных кланах: "
                    f"{previous.clan_tag} и {clan.clan_tag}.",
                )
            members[player_tag] = CurrentClanMember(
                player_tag=player_tag,
                nickname=_member_name(raw_member, player_tag),
                town_hall=_member_town_hall(raw_member, player_tag),
                clan_tag=clan.clan_tag,
                clan_name=clan.clan_name,
            )
    return members


def _plan_player_states(
    *,
    runtime_config: RuntimeChatConfig,
    existing_state: Sequence[CompositionPlayerState],
    imported: CompositionImportResult,
    current_members: dict[str, CurrentClanMember],
    detected_at: datetime,
) -> tuple[dict[str, PlannedPlayerState], list[CompositionDiffItem]]:
    """Планирует новое состояние игроков состава.

    Args:
        runtime_config: Runtime-настройки чата.
        existing_state: Текущее состояние из SQLite.
        imported: Ручные значения из текущего листа.
        current_members: Текущие участники active tracked clans.
        detected_at: Дата обнаружения изменений.

    Returns:
        Новые состояния и diff.
    """

    detected_at_text = _format_dt(detected_at)
    active_clan_tags = {clan.clan_tag for clan in runtime_config.active_clans}
    state_by_tag = {state.player_tag: state for state in existing_state}
    state_by_tag = _merge_imported_sheet_state(state_by_tag, imported)
    planned: dict[str, PlannedPlayerState] = {}
    diff_items: list[CompositionDiffItem] = []

    for player_tag, member in current_members.items():
        previous = state_by_tag.get(player_tag)
        user_values = _user_values_for(player_tag, previous, imported)
        if previous is None or previous.status == "untracked":
            diff_items.append(
                CompositionDiffItem("added", f"Новый игрок: {member.nickname} ({player_tag}).")
            )
        elif previous.status == "exited":
            diff_items.append(
                CompositionDiffItem("returned", f"Вернулся: {member.nickname} ({player_tag}).")
            )
        else:
            if previous.clan_tag is not None and previous.clan_tag != member.clan_tag:
                diff_items.append(
                    CompositionDiffItem(
                        "moved",
                        f"Перешёл: {member.nickname} ({player_tag}) {previous.clan_tag} → {member.clan_tag}.",
                    ),
                )
            if _technical_changed(previous, member):
                diff_items.append(
                    CompositionDiffItem("updated", _technical_update_message(previous, member))
                )

        planned[player_tag] = PlannedPlayerState(
            player_tag=player_tag,
            status="active",
            clan_tag=member.clan_tag,
            town_hall=member.town_hall,
            nickname=member.nickname,
            exited_at=None,
            user_values=user_values,
            last_seen_at=detected_at_text,
        )

    for player_tag, previous in state_by_tag.items():
        if player_tag in current_members:
            continue
        user_values = _user_values_for(player_tag, previous, imported)
        if previous.status == "active":
            if previous.clan_tag in active_clan_tags:
                exited_at = detected_at_text
                diff_items.append(
                    CompositionDiffItem(
                        "exited", f"Вышел: {previous.nickname or player_tag} ({player_tag})."
                    )
                )
                planned[player_tag] = PlannedPlayerState(
                    player_tag=player_tag,
                    status="exited",
                    clan_tag=None,
                    town_hall=previous.town_hall,
                    nickname=previous.nickname,
                    exited_at=exited_at,
                    user_values=user_values,
                    last_seen_at=previous.last_seen_at,
                )
            else:
                planned[player_tag] = PlannedPlayerState(
                    player_tag=player_tag,
                    status="untracked",
                    clan_tag=previous.clan_tag,
                    town_hall=previous.town_hall,
                    nickname=previous.nickname,
                    exited_at=previous.exited_at,
                    user_values=user_values,
                    last_seen_at=previous.last_seen_at,
                )
            continue
        if previous.status == "exited":
            if imported.saw_exited_block and player_tag not in imported.players:
                planned[player_tag] = PlannedPlayerState(
                    player_tag=player_tag,
                    status="untracked",
                    clan_tag=previous.clan_tag,
                    town_hall=previous.town_hall,
                    nickname=previous.nickname,
                    exited_at=previous.exited_at,
                    user_values=user_values,
                    last_seen_at=previous.last_seen_at,
                )
                continue
            planned[player_tag] = PlannedPlayerState(
                player_tag=player_tag,
                status="exited",
                clan_tag=None,
                town_hall=previous.town_hall,
                nickname=previous.nickname,
                exited_at=previous.exited_at,
                user_values=user_values,
                last_seen_at=previous.last_seen_at,
            )
        elif previous.status == "untracked":
            planned[player_tag] = PlannedPlayerState(
                player_tag=player_tag,
                status="untracked",
                clan_tag=previous.clan_tag,
                town_hall=previous.town_hall,
                nickname=previous.nickname,
                exited_at=previous.exited_at,
                user_values=user_values,
                last_seen_at=previous.last_seen_at,
            )

    return planned, diff_items


def _merge_imported_sheet_state(
    state_by_tag: dict[str, CompositionPlayerState],
    imported: CompositionImportResult,
) -> dict[str, CompositionPlayerState]:
    """Дополняет SQLite-state данными с листа, если state отсутствует.

    Args:
        state_by_tag: Состояния из SQLite.
        imported: Данные с листа.

    Returns:
        Состояния с добавленными sheet-only игроками.
    """

    merged = dict(state_by_tag)
    for player_tag, imported_player in imported.players.items():
        if player_tag in merged:
            continue
        merged[player_tag] = CompositionPlayerState(
            player_tag=player_tag,
            status="exited" if imported_player.is_exited else "active",
            clan_tag=None if imported_player.is_exited else imported_player.clan_tag,
            town_hall=imported_player.town_hall,
            nickname=imported_player.nickname,
            exited_at=imported_player.exited_at,
            user_values=imported_player.user_values,
            last_seen_at=None,
        )
    return merged


def _parse_imported_block(
    *,
    runtime_config: RuntimeChatConfig,
    block: SheetBlock,
    table_type: TableType,
    is_exited: bool,
    values: Sequence[Sequence[CellValue]],
) -> tuple[list[ImportedPlayerValues], list[str]]:
    """Парсит один сохранённый block range.

    Args:
        runtime_config: Runtime-конфиг чата.
        block: Описание блока.
        table_type: Тип профиля колонок блока.
        is_exited: Является ли block таблицей вышедших.
        values: Значения блока из Google Sheets.

    Returns:
        Игроки и warnings.
    """

    warnings: list[str] = []
    rows = [_string_row(row) for row in values]
    if len(rows) < TITLE_ROWS_COUNT:
        return [], []
    header = rows[1]
    profiles = _profiles(runtime_config.column_profiles, table_type)
    user_title_to_key = _user_title_to_key(profiles, warnings)
    user_indexes = {
        column_key: header_index
        for header_index, title in enumerate(header)
        if (column_key := user_title_to_key.get(title)) is not None
    }
    system_indexes = _system_indexes(profiles, header)
    clan_tag = _clan_tag_from_block_key(block.block_key)
    imported: list[ImportedPlayerValues] = []

    for row_offset, row in enumerate(rows[TITLE_ROWS_COUNT:], start=TITLE_ROWS_COUNT + 1):
        if _is_empty_row(row):
            continue
        bot_key = _cell_at(row, 0)
        player_tag = _player_tag_from_bot_key(bot_key)
        if player_tag is None:
            fallback_tag = _fallback_tag_from_row(row, system_indexes)
            if fallback_tag is None:
                warnings.append(
                    f"Строка {row_offset} блока {block.block_key}: повреждён __bot_key, fallback по Тегу невозможен.",
                )
                continue
            warnings.append(
                f"Строка {row_offset} блока {block.block_key}: использован fallback по Тегу."
            )
            player_tag = fallback_tag

        imported.append(
            ImportedPlayerValues(
                player_tag=player_tag,
                is_exited=is_exited,
                clan_tag=None if is_exited else clan_tag,
                town_hall=_optional_int_cell(row, system_indexes.get("town_hall")),
                nickname=_optional_str_cell(row, system_indexes.get("nickname")),
                exited_at=_optional_str_cell(row, system_indexes.get("exited_at")),
                user_values={
                    column_key: _cell_at(row, index) for column_key, index in user_indexes.items()
                },
            ),
        )

    return imported, warnings


def _physical_columns(
    column_profiles: Sequence[ColumnProfile], table_type: TableType
) -> tuple[ColumnProfile, ...]:
    """Возвращает физические колонки блока: service + visible non-service.

    Args:
        column_profiles: Все активные профили чата.
        table_type: Тип таблицы.

    Returns:
        Колонки в порядке вывода.
    """

    profiles = sorted(
        (
            profile
            for profile in column_profiles
            if profile.table_type == table_type and profile.is_active
        ),
        key=lambda profile: (profile.sort_order, profile.column_key),
    )
    service = [
        profile
        for profile in profiles
        if profile.column_key == BOT_KEY_COLUMN_KEY and profile.kind == "service"
    ]
    if not service:
        raise CompositionDataError(
            f"Профиль {table_title(table_type)} не содержит service-колонку {BOT_KEY_TITLE}."
        )
    visible = [profile for profile in profiles if profile.kind != "service" and profile.visible]
    return tuple([service[0], *visible])


def _profiles(
    column_profiles: Sequence[ColumnProfile], table_type: TableType
) -> tuple[ColumnProfile, ...]:
    """Возвращает активные профили одного table_type.

    Args:
        column_profiles: Все активные профили чата.
        table_type: Тип таблицы.

    Returns:
        Активные профили указанного типа.
    """

    return tuple(
        profile
        for profile in column_profiles
        if profile.table_type == table_type and profile.is_active
    )


def _system_indexes(profiles: Sequence[ColumnProfile], header: Sequence[str]) -> dict[str, int]:
    """Ищет индексы visible system columns по текущим title.

    Args:
        profiles: Профили колонок table_type.
        header: Строка заголовков блока.

    Returns:
        Индексы system columns по column_key.
    """

    indexes: dict[str, int] = {}
    title_to_indexes: dict[str, list[int]] = {}
    for index, title in enumerate(header):
        title_to_indexes.setdefault(title, []).append(index)

    for profile in profiles:
        if profile.kind != "system" or not profile.visible:
            continue
        title_indexes = title_to_indexes.get(profile.title)
        if title_indexes:
            indexes[profile.column_key] = title_indexes[0]

    return indexes


def _user_title_to_key(profiles: Sequence[ColumnProfile], warnings: list[str]) -> dict[str, str]:
    """Строит соответствие title -> column_key для visible user columns.

    Args:
        profiles: Профили колонок table_type.
        warnings: Список warnings, дополняемый при неоднозначном импорте.

    Returns:
        Словарь соответствия заголовка user-колонки к column_key.
    """

    result: dict[str, str] = {}
    for profile in profiles:
        if profile.kind != "user" or not profile.visible:
            continue
        previous = result.get(profile.title)
        if previous is not None:
            warnings.append(
                f"Дублирующийся заголовок user-колонки `{profile.title}`; импорт неоднозначен.",
            )
            continue
        result[profile.title] = profile.column_key

    return result


def _build_block_values(
    *,
    title: str,
    columns: Sequence[ColumnProfile],
    states: Sequence[PlannedPlayerState],
    is_exited: bool,
) -> list[list[CellValue]]:
    """Строит значения одного блока состава."""

    values: list[list[CellValue]] = [
        _title_row(title, len(columns)),
        [column.title for column in columns],
    ]
    for row_number, state in enumerate(states, start=1):
        values.append(
            _state_to_row(row_number=row_number, state=state, columns=columns, is_exited=is_exited)
        )
    if len(values) == TITLE_ROWS_COUNT:
        values.append(["" for _ in columns])
    return values


def _state_to_row(
    *,
    row_number: int,
    state: PlannedPlayerState,
    columns: Sequence[ColumnProfile],
    is_exited: bool,
) -> list[CellValue]:
    """Преобразует состояние игрока в строку листа."""

    row: list[CellValue] = []
    for column in columns:
        if column.kind == "service" and column.column_key == BOT_KEY_COLUMN_KEY:
            row.append(_bot_key(state.player_tag))
        elif column.kind == "user":
            row.append(state.user_values.get(column.column_key, ""))
        elif column.column_key == "number":
            row.append(row_number)
        elif column.column_key == "tag":
            row.append(state.player_tag)
        elif column.column_key == "town_hall":
            row.append(state.town_hall or "")
        elif column.column_key == "nickname":
            row.append(state.nickname or "")
        elif column.column_key == "exited_at" and is_exited:
            row.append(state.exited_at or "")
        else:
            row.append("")
    return row


def _sorted_active_states(
    states: Iterable[PlannedPlayerState], clan_tag: str
) -> list[PlannedPlayerState]:
    """Сортирует active rows внутри клана."""

    return sorted(
        (state for state in states if state.status == "active" and state.clan_tag == clan_tag),
        key=lambda state: (
            -(state.town_hall or 0),
            (state.nickname or "").lower(),
            state.player_tag,
        ),
    )


def _sorted_exited_states(states: Iterable[PlannedPlayerState]) -> list[PlannedPlayerState]:
    """Сортирует exited rows."""

    sorted_by_name = sorted(
        (state for state in states if state.status == "exited"),
        key=lambda state: ((state.nickname or "").lower(), state.player_tag),
    )
    return sorted(sorted_by_name, key=lambda state: state.exited_at or "", reverse=True)


async def _rewrite_composition_blocks(
    *,
    sheets_client: SheetsClient,
    sheet_name: str,
    previous_blocks: Sequence[SheetBlock],
    built_blocks: Sequence[BuiltBlock],
) -> None:
    """Очищает прошлые managed blocks и пишет новые blocks одним values batch."""

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
    for built_block in built_blocks:
        updates.append(
            SheetValues(
                sheet_name=sheet_name,
                range_a1=range_from_start_cell(
                    start_cell=built_block.block.start_cell,
                    rows_count=built_block.block.rows_count,
                    columns_count=built_block.block.columns_count,
                ),
                values=built_block.values,
            ),
        )
    await sheets_client.batch_update_values(updates)


async def _format_composition_sheet(
    *,
    runtime_config: RuntimeChatConfig,
    sheets_client: SheetsClient,
    built_blocks: Sequence[BuiltBlock],
) -> None:
    """Форматирует управляемые блоки листа `Состав` как CWL-таблицы."""

    if not built_blocks:
        return
    sheet_id = runtime_config.sheet_binding.composition_sheet_id
    if sheet_id is None:
        metadata = await sheets_client.get_sheet_metadata(
            runtime_config.sheet_binding.composition_sheet_name
        )
        sheet_id = metadata.sheet_id
    requests = _build_composition_format_requests(sheet_id=sheet_id, built_blocks=built_blocks)
    await sheets_client.batch_update_spreadsheet(requests)


def _build_composition_format_requests(
    *,
    sheet_id: int,
    built_blocks: Sequence[BuiltBlock],
) -> list[JsonObject]:
    """Строит batchUpdate requests для форматирования состава."""

    requests: list[JsonObject] = []
    for built_block in built_blocks:
        block = built_block.block
        block_range = _grid_range_from_start_cell(
            sheet_id=sheet_id,
            start_cell=block.start_cell,
            rows_count=block.rows_count,
            columns_count=block.columns_count,
        )
        requests.append(
            _repeat_cell_request(
                block_range,
                _base_cell_format(),
                "userEnteredFormat(backgroundColorStyle,textFormat,verticalAlignment,wrapStrategy)",
            ),
        )
        requests.append(_update_borders_request(block_range))
        requests.append(
            _repeat_cell_request(
                _grid_range_for_block_row(sheet_id, block, row_offset=0),
                _title_cell_format(),
                "userEnteredFormat(backgroundColorStyle,textFormat,verticalAlignment,wrapStrategy)",
            ),
        )
        requests.append(
            _repeat_cell_request(
                _grid_range_for_block_row(sheet_id, block, row_offset=1),
                _header_cell_format(),
                "userEnteredFormat(backgroundColorStyle,textFormat,verticalAlignment,wrapStrategy)",
            ),
        )
        data_rows_count = max(block.rows_count - TITLE_ROWS_COUNT, 0)
        for data_row_offset in range(data_rows_count):
            if data_row_offset % 2 == 0:
                continue
            requests.append(
                _repeat_cell_request(
                    _grid_range_for_block_row(
                        sheet_id,
                        block,
                        row_offset=TITLE_ROWS_COUNT + data_row_offset,
                    ),
                    {"userEnteredFormat": {"backgroundColorStyle": {"rgbColor": LIGHT_BAND_RGB}}},
                    "userEnteredFormat.backgroundColorStyle",
                ),
            )
    return requests


async def _hide_bot_key_columns(
    *,
    runtime_config: RuntimeChatConfig,
    sheets_client: SheetsClient,
    built_blocks: Sequence[BuiltBlock],
) -> None:
    """Скрывает физические колонки `__bot_key` для всех composition blocks."""

    sheet_id = runtime_config.sheet_binding.composition_sheet_id
    if sheet_id is None:
        metadata = await sheets_client.get_sheet_metadata(
            runtime_config.sheet_binding.composition_sheet_name
        )
        sheet_id = metadata.sheet_id
    hidden_columns: set[int] = set()
    for built_block in built_blocks:
        column_number, _ = _parse_a1_cell(built_block.block.start_cell)
        column_index = column_number - 1
        if column_index in hidden_columns:
            continue
        hidden_columns.add(column_index)
        await sheets_client.hide_dimension(
            sheet_id=sheet_id,
            dimension="COLUMNS",
            start_index=column_index,
            end_index=column_index + 1,
            hidden=True,
        )


def _title_row(title: str, width: int) -> list[CellValue]:
    """Создаёт строку заголовка с видимым title после hidden key column."""

    if width <= 1:
        return [title]
    return ["", title, *["" for _ in range(width - 2)]]


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
    """Возвращает формат строки названия блока."""

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
    """Возвращает формат строки заголовков."""

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
    """Строит GridRange строки managed-блока."""

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
        error_cls=CompositionDataError,
    )


def _offset_cell(start_cell: str, *, row_offset: int, column_offset: int) -> str:
    """Сдвигает A1-ячейку."""

    return _shared_offset_cell(
        start_cell,
        row_offset=row_offset,
        column_offset=column_offset,
        error_cls=CompositionDataError,
    )


def _table_type_from_block_key(block_key: str) -> TableType | None:
    if block_key.startswith(ACTIVE_BLOCK_PREFIX):
        return COMPOSITION_ACTIVE_TABLE
    if block_key == EXITED_BLOCK_KEY:
        return COMPOSITION_EXITED_TABLE
    return None


def _is_exited_block_key(block_key: str) -> bool:
    return block_key == EXITED_BLOCK_KEY


def _clan_tag_from_block_key(block_key: str) -> str | None:
    if not block_key.startswith(ACTIVE_BLOCK_PREFIX):
        return None
    raw_tag = block_key.removeprefix(ACTIVE_BLOCK_PREFIX)
    try:
        return normalize_tag(raw_tag)
    except ValueError:
        return None


def _bot_key(player_tag: str) -> str:
    return f"{COMPOSITION_PLAYER_KEY_PREFIX}{player_tag}"


def _player_tag_from_bot_key(value: str) -> str | None:
    if not value.startswith(COMPOSITION_PLAYER_KEY_PREFIX):
        return None
    raw_tag = value.removeprefix(COMPOSITION_PLAYER_KEY_PREFIX)
    try:
        return normalize_tag(raw_tag)
    except ValueError:
        return None


def _fallback_tag_from_row(row: Sequence[str], system_indexes: dict[str, int]) -> str | None:
    tag_index = system_indexes.get("tag")
    if tag_index is None:
        return None
    try:
        return normalize_tag(_cell_at(row, tag_index))
    except ValueError:
        return None


def _user_values_for(
    player_tag: str,
    previous: CompositionPlayerState | None,
    imported: CompositionImportResult,
) -> JsonDict:
    if player_tag in imported.players:
        return dict(imported.players[player_tag].user_values)
    if previous is not None:
        return dict(previous.user_values)
    return {}


def _technical_changed(previous: CompositionPlayerState, member: CurrentClanMember) -> bool:
    return previous.town_hall != member.town_hall or previous.nickname != member.nickname


def _technical_update_message(previous: CompositionPlayerState, member: CurrentClanMember) -> str:
    changes: list[str] = []
    if previous.town_hall != member.town_hall:
        changes.append(f"ТХ {previous.town_hall or '-'} → {member.town_hall}")
    if previous.nickname != member.nickname:
        changes.append(f"ник {previous.nickname or '-'} → {member.nickname}")
    return f"Обновлён: {member.player_tag} ({', '.join(changes)})."


def _member_tag(raw_member: dict[str, Any]) -> str:
    value = raw_member.get("tag")
    if not isinstance(value, str):
        raise CompositionDataError("CoC API вернул участника без tag.")
    try:
        return normalize_tag(value)
    except ValueError as exc:
        raise CompositionDataError(f"CoC API вернул некорректный player tag: {value}.") from exc


def _member_name(raw_member: dict[str, Any], player_tag: str) -> str:
    value = raw_member.get("name")
    if not isinstance(value, str):
        raise CompositionDataError(f"CoC API вернул игрока {player_tag} без name.")
    return value


def _member_town_hall(raw_member: dict[str, Any], player_tag: str) -> int:
    value = raw_member.get("townHallLevel")
    if not isinstance(value, int) or isinstance(value, bool):
        raise CompositionDataError(f"CoC API вернул игрока {player_tag} без townHallLevel.")
    return value


def _cell_at(row: Sequence[str], index: int) -> str:
    if index < 0 or index >= len(row):
        return ""
    return row[index]


def _optional_str_cell(row: Sequence[str], index: int | None) -> str | None:
    if index is None:
        return None
    value = _cell_at(row, index).strip()
    return value or None


def _optional_int_cell(row: Sequence[str], index: int | None) -> int | None:
    if index is None:
        return None
    value = _cell_at(row, index).strip()
    if value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _string_row(row: Sequence[CellValue]) -> list[str]:
    return [_cell_to_str(cell) for cell in row]


def _cell_to_str(value: CellValue) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _is_empty_row(row: Sequence[str]) -> bool:
    return all(cell.strip() == "" for cell in row)


def _parse_a1_cell(cell: str) -> tuple[int, int]:
    return _shared_parse_a1_cell(cell, error_cls=CompositionDataError)


def _column_to_number(column: str) -> int:
    return _shared_column_to_number(column)


def _number_to_column(number: int) -> str:
    return _shared_number_to_column(number, error_cls=CompositionDataError)
