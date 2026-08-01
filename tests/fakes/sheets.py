"""Fake Google Sheets и repository-объекты для sync tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from clash_sheet_sync_bot.models import SheetBlock
from clash_sheet_sync_bot.repositories import RaidPlayerState, RaidSheetArchive
from clash_sheet_sync_bot.sheets.client import (
    SheetMetadata,
    SpreadsheetMetadata,
    validate_sheet_id,
)


@dataclass(slots=True)
class FakeSheetsClient:
    """Fake Google Sheets client для composition apply tests."""

    values_by_range: dict[tuple[str, str], list[list[Any]]] = field(default_factory=dict)
    read_calls: list[tuple[str, str]] = field(default_factory=list)
    batch_value_updates: list[Any] = field(default_factory=list)
    spreadsheet_requests: list[Any] = field(default_factory=list)
    hidden_dimensions: list[dict[str, Any]] = field(default_factory=list)
    metadata_sheets: tuple[SheetMetadata, ...] = (
        SheetMetadata(sheet_id=444, title="Рейды", index=0),
    )
    added_sheets: list[str] = field(default_factory=list)
    metadata_calls: int = 0
    fail_values_update: Exception | None = None
    fail_spreadsheet_update: Exception | None = None
    fail_hide: Exception | None = None
    fail_add_sheet: Exception | None = None
    fail_atomic_rotation: Exception | None = None
    fail_delete_sheet: Exception | None = None
    fail_metadata_on_call: int | None = None
    fail_metadata_error: Exception | None = None
    deleted_sheet_ids: list[int] = field(default_factory=list)
    operation_log: list[str] = field(default_factory=list)

    async def read_values(self, sheet_name: str, range_a1: str) -> list[list[Any]]:
        """Возвращает настроенные значения и запоминает read-only вызов."""

        self.read_calls.append((sheet_name, range_a1))
        return self.values_by_range.get((sheet_name, range_a1), [])

    async def batch_update_values(self, updates: Any) -> dict[str, Any]:
        """Запоминает values batch update."""

        self.batch_value_updates.append(tuple(updates))
        self.operation_log.append("values")
        if self.fail_values_update is not None:
            raise self.fail_values_update
        return {}

    async def batch_update_spreadsheet(self, requests: Any) -> dict[str, Any]:
        """Запоминает spreadsheets.batchUpdate requests."""

        request_list = list(requests)
        self.spreadsheet_requests.append(request_list)
        if _is_atomic_raid_rotation(request_list):
            self.operation_log.append("atomic_rotation")
            if self.fail_atomic_rotation is not None:
                raise self.fail_atomic_rotation
            self._apply_sheet_property_requests(request_list)
            return {}
        self.operation_log.append("format")
        if self.fail_spreadsheet_update is not None:
            raise self.fail_spreadsheet_update
        return {}

    async def hide_dimension(
        self,
        *,
        sheet_id: int,
        dimension: str,
        start_index: int,
        end_index: int,
        hidden: bool = True,
    ) -> None:
        """Запоминает скрытие строки или колонки."""

        self.hidden_dimensions.append(
            {
                "sheet_id": sheet_id,
                "dimension": dimension,
                "start_index": start_index,
                "end_index": end_index,
                "hidden": hidden,
            },
        )
        self.operation_log.append("hide")
        if self.fail_hide is not None:
            raise self.fail_hide

    async def get_sheet_metadata(self, sheet_name: str) -> SheetMetadata:
        """Возвращает metadata листа, если тестовый binding без sheet_id."""

        for sheet in self.metadata_sheets:
            if sheet.title == sheet_name:
                return sheet
        return SheetMetadata(sheet_id=111, title=sheet_name)

    async def get_spreadsheet_metadata(self) -> SpreadsheetMetadata:
        """Возвращает настроенные metadata всего Spreadsheet."""

        self.metadata_calls += 1
        if (
            self.fail_metadata_on_call == self.metadata_calls
            and self.fail_metadata_error is not None
        ):
            raise self.fail_metadata_error
        return SpreadsheetMetadata(
            spreadsheet_id="sheet-id",
            title="Test Spreadsheet",
            sheets=self.metadata_sheets,
        )

    async def add_sheet(self, title: str) -> SheetMetadata:
        """Создаёт fake-лист и запоминает только эту разрешённую операцию."""

        self.added_sheets.append(title)
        self.operation_log.append("add_sheet")
        if self.fail_add_sheet is not None:
            raise self.fail_add_sheet
        next_sheet_id = max((sheet.sheet_id for sheet in self.metadata_sheets), default=8999) + 1
        sheet = SheetMetadata(
            sheet_id=next_sheet_id,
            title=title,
            index=len(self.metadata_sheets),
        )
        self.metadata_sheets = (*self.metadata_sheets, sheet)
        return sheet

    async def delete_sheet(self, sheet_id: int) -> None:
        """Удаляет fake-лист только по физическому ID."""

        validated_sheet_id = validate_sheet_id(sheet_id)
        self.deleted_sheet_ids.append(validated_sheet_id)
        self.operation_log.append("delete_sheet")
        if self.fail_delete_sheet is not None:
            raise self.fail_delete_sheet
        self.metadata_sheets = tuple(
            sheet for sheet in self.metadata_sheets if sheet.sheet_id != validated_sheet_id
        )

    def _apply_sheet_property_requests(self, requests: list[dict[str, Any]]) -> None:
        """Применяет title/index свойства успешного atomic fake batch."""

        sheets = list(self.metadata_sheets)
        for request in requests:
            update = request.get("updateSheetProperties")
            if not isinstance(update, dict):
                continue
            properties = update.get("properties")
            if not isinstance(properties, dict):
                continue
            sheet_id = properties.get("sheetId")
            match_index = next(
                (index for index, sheet in enumerate(sheets) if sheet.sheet_id == sheet_id),
                None,
            )
            if match_index is None:
                continue
            current = sheets[match_index]
            sheets[match_index] = SheetMetadata(
                sheet_id=current.sheet_id,
                title=properties.get("title", current.title),
                index=properties.get("index", current.index),
            )
        self.metadata_sheets = tuple(sheets)


@dataclass(slots=True)
class RecordingCompositionRepository:
    """Fake repository состояния состава."""

    upserted_players: list[dict[str, Any]] = field(default_factory=list)

    async def upsert_player_state(self, **kwargs: Any) -> None:
        """Запоминает upsert игрока."""

        self.upserted_players.append(dict(kwargs))


@dataclass(slots=True)
class RecordingRaidPlayerStateRepository:
    """Fake repository агрегированного состояния raid rows."""

    upserted_states: list[RaidPlayerState] = field(default_factory=list)
    commit_calls: int = 0

    async def upsert(self, state: RaidPlayerState) -> None:
        """Запоминает upsert raid row."""

        self.upserted_states.append(state)

    async def commit(self) -> None:
        """Фиксирует запрещённый самостоятельный commit repository."""

        self.commit_calls += 1


@dataclass(slots=True)
class RecordingRaidSheetArchiveRepository:
    """Fake repository bot-owned raid archive registry."""

    archives: tuple[RaidSheetArchive, ...] = ()
    upserted_archives: list[RaidSheetArchive] = field(default_factory=list)
    deleted_seasons: list[tuple[int, str]] = field(default_factory=list)
    commit_calls: int = 0
    fail_upsert: Exception | None = None

    async def list_ordered(self, chat_id: int) -> tuple[RaidSheetArchive, ...]:
        """Возвращает архивы чата в каноническом порядке старшинства."""

        return tuple(
            sorted(
                (archive for archive in self.archives if archive.chat_id == chat_id),
                key=lambda archive: (
                    archive.season_start_at,
                    archive.archived_at,
                    archive.sheet_id,
                ),
            ),
        )

    async def upsert(self, archive: RaidSheetArchive) -> None:
        """Запоминает и применяет archive registry upsert."""

        if self.fail_upsert is not None:
            raise self.fail_upsert
        self.upserted_archives.append(archive)
        self.archives = (
            *(
                existing
                for existing in self.archives
                if existing.chat_id != archive.chat_id
                or (
                    existing.season_key != archive.season_key
                    and existing.sheet_id != archive.sheet_id
                )
            ),
            archive,
        )

    async def delete(self, *, chat_id: int, season_key: str) -> None:
        """Удаляет registry запись после подтверждённого Sheet delete."""

        self.deleted_seasons.append((chat_id, season_key))
        self.archives = tuple(
            archive
            for archive in self.archives
            if archive.chat_id != chat_id or archive.season_key != season_key
        )

    async def commit(self) -> None:
        """Фиксирует запрещённый самостоятельный commit repository."""

        self.commit_calls += 1


@dataclass(slots=True)
class RecordingSheetBindingRepository:
    """Fake repository active raid binding."""

    update_calls: list[dict[str, Any]] = field(default_factory=list)
    commit_calls: int = 0
    fail_update: Exception | None = None

    async def update_active_raid_binding(self, **kwargs: Any) -> None:
        """Запоминает binding update после успешной Sheet rotation."""

        if self.fail_update is not None:
            raise self.fail_update
        self.update_calls.append(dict(kwargs))

    async def commit(self) -> None:
        """Фиксирует запрещённый самостоятельный commit repository."""

        self.commit_calls += 1


@dataclass(slots=True)
class RecordingSheetBlockRepository:
    """Fake repository managed-блоков Google Sheets."""

    blocks: tuple[SheetBlock, ...] = ()
    fail_on_upsert: bool = False
    replace_calls: list[dict[str, Any]] = field(default_factory=list)
    upsert_calls: list[dict[str, Any]] = field(default_factory=list)
    rebind_calls: list[dict[str, Any]] = field(default_factory=list)
    delete_calls: list[dict[str, Any]] = field(default_factory=list)
    commit_calls: int = 0

    async def list_blocks(
        self, chat_id: int, sheet_name: str | None = None
    ) -> tuple[SheetBlock, ...]:
        """Возвращает сохранённые блоки с фильтром по чату и листу."""

        return tuple(
            block
            for block in self.blocks
            if block.chat_id == chat_id and (sheet_name is None or block.sheet_name == sheet_name)
        )

    async def replace_blocks_by_prefixes(
        self,
        *,
        chat_id: int,
        sheet_name: str,
        block_key_prefixes: tuple[str, ...],
        blocks: tuple[SheetBlock, ...],
        updated_at: str,
    ) -> None:
        """Запоминает replace_blocks_by_prefixes call."""

        self.replace_calls.append(
            {
                "chat_id": chat_id,
                "sheet_name": sheet_name,
                "block_key_prefixes": block_key_prefixes,
                "blocks": blocks,
                "updated_at": updated_at,
            },
        )
        prefixes = block_key_prefixes
        self.blocks = (
            tuple(
                block
                for block in self.blocks
                if not (
                    block.chat_id == chat_id
                    and block.sheet_name == sheet_name
                    and any(block.block_key.startswith(prefix) for prefix in prefixes)
                )
            )
            + blocks
        )

    async def rebind_blocks(self, **kwargs: Any) -> None:
        """Перепривязывает только blocks выбранного физического листа."""

        call = dict(kwargs)
        self.rebind_calls.append(call)
        prefixes = call["block_key_prefixes"]
        self.blocks = tuple(
            SheetBlock(
                chat_id=block.chat_id,
                sheet_name=call["new_sheet_name"],
                sheet_id=block.sheet_id,
                block_key=block.block_key,
                start_cell=block.start_cell,
                rows_count=block.rows_count,
                columns_count=block.columns_count,
            )
            if block.chat_id == call["chat_id"]
            and block.sheet_name == call["old_sheet_name"]
            and block.sheet_id == call["sheet_id"]
            and any(block.block_key.startswith(prefix) for prefix in prefixes)
            else block
            for block in self.blocks
        )

    async def delete_blocks(self, **kwargs: Any) -> None:
        """Удаляет только metadata выбранного физического листа."""

        call = dict(kwargs)
        self.delete_calls.append(call)
        prefixes = call["block_key_prefixes"]
        self.blocks = tuple(
            block
            for block in self.blocks
            if not (
                block.chat_id == call["chat_id"]
                and block.sheet_id == call["sheet_id"]
                and any(block.block_key.startswith(prefix) for prefix in prefixes)
            )
        )

    async def upsert_block(self, *, block: SheetBlock, updated_at: str) -> None:
        """Запоминает или запрещает legacy upsert блока."""

        if self.fail_on_upsert:
            raise AssertionError("composition apply must use replace_blocks_by_prefixes")
        self.upsert_calls.append({"block": block, "updated_at": updated_at})

    async def commit(self) -> None:
        """Фиксирует запрещённый самостоятельный commit repository."""

        self.commit_calls += 1


def _is_atomic_raid_rotation(requests: list[dict[str, Any]]) -> bool:
    """Распознаёт единый batch rename old/staging и move нового active."""

    property_updates = [
        request.get("updateSheetProperties")
        for request in requests
        if isinstance(request.get("updateSheetProperties"), dict)
    ]
    title_updates = [update for update in property_updates if update.get("fields") == "title"]
    index_updates = [update for update in property_updates if update.get("fields") == "index"]
    return len(title_updates) == 2 and len(index_updates) == 1
