"""Unit-тесты Google Sheets admin service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from clash_sheet_sync_bot.models import SheetBinding, SheetBlock
from clash_sheet_sync_bot.sheets.admin import SheetAdminService
from clash_sheet_sync_bot.sheets.client import SheetMetadata, SpreadsheetMetadata


@dataclass(slots=True)
class FakeAdminSheetsClient:
    """Fake SheetsClient для SheetAdminService tests."""

    sheets: list[SheetMetadata]
    hidden_sheets: list[dict[str, Any]] = field(default_factory=list)
    hidden_dimensions: list[dict[str, Any]] = field(default_factory=list)
    written_values: list[dict[str, Any]] = field(default_factory=list)
    added_sheets: list[str] = field(default_factory=list)

    async def get_spreadsheet_metadata(self) -> SpreadsheetMetadata:
        return SpreadsheetMetadata(
            spreadsheet_id="sheet-id",
            title="Test spreadsheet",
            sheets=tuple(self.sheets),
        )

    async def add_sheet(self, title: str) -> SheetMetadata:
        sheet = SheetMetadata(sheet_id=9000 + len(self.sheets), title=title)
        self.sheets.append(sheet)
        self.added_sheets.append(title)
        return sheet

    async def write_values(self, sheet_name: str, range_a1: str, values: Any) -> None:
        self.written_values.append(
            {
                "sheet_name": sheet_name,
                "range_a1": range_a1,
                "values": values,
            },
        )

    async def hide_sheet(self, sheet_id: int, *, hidden: bool = True) -> None:
        self.hidden_sheets.append({"sheet_id": sheet_id, "hidden": hidden})

    async def hide_dimension(
        self,
        *,
        sheet_id: int,
        dimension: str,
        start_index: int,
        end_index: int,
        hidden: bool = True,
    ) -> None:
        self.hidden_dimensions.append(
            {
                "sheet_id": sheet_id,
                "dimension": dimension,
                "start_index": start_index,
                "end_index": end_index,
                "hidden": hidden,
            },
        )


def _binding() -> SheetBinding:
    return SheetBinding(
        chat_id=-1001,
        google_sheet_id="sheet-id",
        spreadsheet_url="https://docs.google.com/spreadsheets/d/sheet-id/edit",
        composition_sheet_name="Состав",
        composition_sheet_id=999111,
        active_cwl_sheet_name="CWL",
        active_cwl_sheet_id=999222,
        active_cwl_season="2026-07",
        bot_state_sheet_name="_bot_state",
        bot_state_sheet_id=999333,
        timezone="Europe/Kyiv",
    )


@pytest.mark.asyncio
async def test_autofix_uses_current_sheet_id_for_stale_sheet_blocks() -> None:
    """Проверяет, что auto-fix не использует stale sheet_id из sheet_blocks."""

    sheets_client = FakeAdminSheetsClient(
        sheets=[
            SheetMetadata(sheet_id=111, title="Состав"),
            SheetMetadata(sheet_id=222, title="CWL"),
            SheetMetadata(sheet_id=333, title="_bot_state"),
        ],
    )
    admin = SheetAdminService(
        sheets_client=sheets_client,  # type: ignore[arg-type]
        spreadsheet_id="sheet-id",
        service_account_email="bot@example.com",
        expected_service_account_email=None,
    )
    blocks = (
        SheetBlock(
            chat_id=-1001,
            sheet_name="Состав",
            sheet_id=999111,
            block_key="composition_active:#AAA111",
            start_cell="A1",
            rows_count=10,
            columns_count=4,
        ),
    )

    result = await admin.auto_fix_binding(binding=_binding(), blocks=blocks)

    assert result.composition_sheet_id == 111
    assert result.active_cwl_sheet_id == 222
    assert result.bot_state_sheet_id == 333
    assert sheets_client.hidden_dimensions == [
        {
            "sheet_id": 111,
            "dimension": "COLUMNS",
            "start_index": 0,
            "end_index": 1,
            "hidden": True,
        },
    ]


@pytest.mark.asyncio
async def test_autofix_ignores_blocks_for_missing_sheets() -> None:
    """Проверяет, что stale block неизвестного листа не валит auto-fix."""

    sheets_client = FakeAdminSheetsClient(
        sheets=[
            SheetMetadata(sheet_id=111, title="Состав"),
            SheetMetadata(sheet_id=222, title="CWL"),
            SheetMetadata(sheet_id=333, title="_bot_state"),
        ],
    )
    admin = SheetAdminService(
        sheets_client=sheets_client,  # type: ignore[arg-type]
        spreadsheet_id="sheet-id",
        service_account_email="bot@example.com",
        expected_service_account_email=None,
    )
    blocks = (
        SheetBlock(
            chat_id=-1001,
            sheet_name="Удалённый лист",
            sheet_id=999999,
            block_key="composition_active:#OLD",
            start_cell="A1",
            rows_count=10,
            columns_count=4,
        ),
    )

    await admin.auto_fix_binding(binding=_binding(), blocks=blocks)

    assert sheets_client.hidden_dimensions == []
