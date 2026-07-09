"""Fake Google Sheets и repository-объекты для sync tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from models import SheetBlock
from sheets_client import SheetMetadata


@dataclass(slots=True)
class FakeSheetsClient:
    """Fake Google Sheets client для composition apply tests."""

    batch_value_updates: list[Any] = field(default_factory=list)
    spreadsheet_requests: list[Any] = field(default_factory=list)
    hidden_dimensions: list[dict[str, Any]] = field(default_factory=list)

    async def batch_update_values(self, updates: Any) -> dict[str, Any]:
        """Запоминает values batch update."""

        self.batch_value_updates.append(tuple(updates))
        return {}

    async def batch_update_spreadsheet(self, requests: Any) -> dict[str, Any]:
        """Запоминает spreadsheets.batchUpdate requests."""

        self.spreadsheet_requests.append(list(requests))
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

    async def get_sheet_metadata(self, sheet_name: str) -> SheetMetadata:
        """Возвращает metadata листа, если тестовый binding без sheet_id."""

        return SheetMetadata(sheet_id=111, title=sheet_name)


@dataclass(slots=True)
class RecordingCompositionRepository:
    """Fake repository состояния состава."""

    upserted_players: list[dict[str, Any]] = field(default_factory=list)

    async def upsert_player_state(self, **kwargs: Any) -> None:
        """Запоминает upsert игрока."""

        self.upserted_players.append(dict(kwargs))


@dataclass(slots=True)
class RecordingSheetBlockRepository:
    """Fake repository managed-блоков Google Sheets."""

    blocks: tuple[SheetBlock, ...] = ()
    fail_on_upsert: bool = False
    replace_calls: list[dict[str, Any]] = field(default_factory=list)
    upsert_calls: list[dict[str, Any]] = field(default_factory=list)

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
