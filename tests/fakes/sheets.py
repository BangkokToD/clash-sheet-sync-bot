"""Fake Google Sheets и repository-объекты для sync tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from clash_sheet_sync_bot.models import SheetBlock
from clash_sheet_sync_bot.repositories import RaidPlayerState
from clash_sheet_sync_bot.sheets.client import SheetMetadata, SpreadsheetMetadata


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

    async def read_values(self, sheet_name: str, range_a1: str) -> list[list[Any]]:
        """Возвращает настроенные значения и запоминает read-only вызов."""

        self.read_calls.append((sheet_name, range_a1))
        return self.values_by_range.get((sheet_name, range_a1), [])

    async def batch_update_values(self, updates: Any) -> dict[str, Any]:
        """Запоминает values batch update."""

        self.batch_value_updates.append(tuple(updates))
        if self.fail_values_update is not None:
            raise self.fail_values_update
        return {}

    async def batch_update_spreadsheet(self, requests: Any) -> dict[str, Any]:
        """Запоминает spreadsheets.batchUpdate requests."""

        self.spreadsheet_requests.append(list(requests))
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
        return SpreadsheetMetadata(
            spreadsheet_id="sheet-id",
            title="Test Spreadsheet",
            sheets=self.metadata_sheets,
        )

    async def add_sheet(self, title: str) -> SheetMetadata:
        """Создаёт fake-лист и запоминает только эту разрешённую операцию."""

        self.added_sheets.append(title)
        next_sheet_id = max((sheet.sheet_id for sheet in self.metadata_sheets), default=8999) + 1
        sheet = SheetMetadata(
            sheet_id=next_sheet_id,
            title=title,
            index=len(self.metadata_sheets),
        )
        self.metadata_sheets = (*self.metadata_sheets, sheet)
        return sheet


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
class RecordingSheetBlockRepository:
    """Fake repository managed-блоков Google Sheets."""

    blocks: tuple[SheetBlock, ...] = ()
    fail_on_upsert: bool = False
    replace_calls: list[dict[str, Any]] = field(default_factory=list)
    upsert_calls: list[dict[str, Any]] = field(default_factory=list)
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

    async def upsert_block(self, *, block: SheetBlock, updated_at: str) -> None:
        """Запоминает или запрещает legacy upsert блока."""

        if self.fail_on_upsert:
            raise AssertionError("composition apply must use replace_blocks_by_prefixes")
        self.upsert_calls.append({"block": block, "updated_at": updated_at})

    async def commit(self) -> None:
        """Фиксирует запрещённый самостоятельный commit repository."""

        self.commit_calls += 1
