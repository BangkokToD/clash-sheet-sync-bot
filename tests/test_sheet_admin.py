"""Unit-тесты Google Sheets admin service."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from clash_sheet_sync_bot.models import SheetBinding, SheetBlock
from clash_sheet_sync_bot.repositories import RaidSheetArchive
from clash_sheet_sync_bot.sheets.admin import (
    BOT_STATE_SCHEMA_VERSION,
    DEFAULT_RAID_SHEET_NAME,
    SheetAdminError,
    SheetAdminService,
)
from clash_sheet_sync_bot.sheets.client import (
    GoogleSheetsReadError,
    GoogleSheetsWriteError,
    SheetMetadata,
    SheetsClient,
    SpreadsheetMetadata,
)


@dataclass(slots=True)
class FakeAdminSheetsClient:
    """Fake SheetsClient для SheetAdminService tests."""

    sheets: list[SheetMetadata]
    hidden_sheets: list[dict[str, Any]] = field(default_factory=list)
    hidden_dimensions: list[dict[str, Any]] = field(default_factory=list)
    written_values: list[dict[str, Any]] = field(default_factory=list)
    added_sheets: list[str] = field(default_factory=list)
    moved_sheets: list[dict[str, int]] = field(default_factory=list)
    deleted_sheet_ids: list[int] = field(default_factory=list)
    read_ranges: dict[tuple[str, str], list[list[Any]]] = field(default_factory=dict)
    delete_error: Exception | None = None
    hidden_columns: set[tuple[int, int]] = field(default_factory=set)

    async def get_spreadsheet_metadata(self) -> SpreadsheetMetadata:
        return SpreadsheetMetadata(
            spreadsheet_id="sheet-id",
            title="Test spreadsheet",
            sheets=tuple(self.sheets),
        )

    async def add_sheet(self, title: str) -> SheetMetadata:
        sheet = SheetMetadata(
            sheet_id=9000 + len(self.sheets),
            title=title,
            index=len(self.sheets),
        )
        self.sheets.append(sheet)
        self.added_sheets.append(title)
        return sheet

    async def read_values(self, sheet_name: str, range_a1: str) -> list[list[Any]]:
        return self.read_ranges.get((sheet_name, range_a1), [])

    async def move_sheet(self, sheet_id: int, index: int) -> None:
        self.moved_sheets.append({"sheet_id": sheet_id, "index": index})

    async def is_column_hidden(
        self,
        *,
        sheet_id: int,
        sheet_name: str,
        column_index: int,
    ) -> bool:
        assert any(
            sheet.sheet_id == sheet_id and sheet.title == sheet_name for sheet in self.sheets
        )
        return (sheet_id, column_index) in self.hidden_columns

    async def delete_sheet(self, sheet_id: int) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted_sheet_ids.append(sheet_id)
        self.sheets = [sheet for sheet in self.sheets if sheet.sheet_id != sheet_id]

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
        if hidden:
            self.hidden_columns.add((sheet_id, start_index))
        else:
            self.hidden_columns.discard((sheet_id, start_index))
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
        active_raid_sheet_name="Рейды",
        active_raid_sheet_id=None,
        active_raid_season=None,
        bot_state_sheet_name="_bot_state",
        bot_state_sheet_id=999333,
        timezone="Europe/Kyiv",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("hidden", (False, True))
async def test_sheets_client_reads_exact_column_hidden_metadata(
    monkeypatch: pytest.MonkeyPatch,
    hidden: bool,
) -> None:
    """Проверяет ограниченный GridData request и strict physical identity."""

    client = object.__new__(SheetsClient)
    client._sheet_id = "sheet-id"
    calls: list[dict[str, Any]] = []

    async def request_json(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "sheets": [
                {
                    "properties": {"sheetId": 444},
                    "data": [
                        {
                            "startColumn": 0,
                            "columnMetadata": [{"hiddenByUser": hidden}],
                        },
                    ],
                },
            ],
        }

    monkeypatch.setattr(client, "_request_json", request_json)

    assert (
        await client.is_column_hidden(
            sheet_id=444,
            sheet_name="Рейды",
            column_index=0,
        )
        is hidden
    )
    query = parse_qs(urlsplit(calls[0]["path"]).query)
    assert query["ranges"] == ["'Рейды'!A:A"]
    assert query["fields"] == [
        "sheets(properties(sheetId),data(startColumn,columnMetadata(hiddenByUser)))"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sheet_id", "column_index"),
    ((True, 0), (444, True), (-1, 0), (444, -1), (1.0, 0), (444, 1.0), (None, 0)),
)
async def test_sheets_client_rejects_invalid_hidden_column_identity(
    sheet_id: object,
    column_index: object,
) -> None:
    """Проверяет strict integer contract metadata-запроса."""

    client = object.__new__(SheetsClient)
    client._sheet_id = "sheet-id"

    with pytest.raises(GoogleSheetsReadError, match="целым числом"):
        await client.is_column_hidden(
            sheet_id=sheet_id,  # type: ignore[arg-type]
            sheet_name="Рейды",
            column_index=column_index,  # type: ignore[arg-type]
        )


def _archive(index: int) -> RaidSheetArchive:
    """Создаёт упорядоченную запись raid archive registry."""

    day = index + 1
    return RaidSheetArchive(
        chat_id=-1001,
        season_key=f"2026-06-{day:02d}T07:00:00+00:00",
        season_start_at=f"2026-06-{day:02d}T07:00:00+00:00",
        sheet_name=f"Рейды 2026-06-{day:02d}",
        sheet_id=500 + index,
        archived_at=f"2026-07-{day:02d}T08:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_initialize_binding_adds_raid_before_cwl_and_writes_bot_state_v2() -> None:
    """Проверяет четвёртый обязательный лист и raid fields служебного зеркала."""

    sheets_client = FakeAdminSheetsClient(
        sheets=[
            SheetMetadata(sheet_id=111, title="Состав", index=0),
            SheetMetadata(sheet_id=222, title="CWL", index=1),
            SheetMetadata(sheet_id=333, title="_bot_state", index=2),
        ],
    )
    admin = SheetAdminService(
        sheets_client=sheets_client,  # type: ignore[arg-type]
        spreadsheet_id="sheet-id",
        service_account_email="bot@example.com",
        expected_service_account_email=None,
    )

    result = await admin.initialize_new_binding(chat_id=-1001, timezone="Europe/Kyiv")

    assert sheets_client.added_sheets == [DEFAULT_RAID_SHEET_NAME]
    assert result.active_raid_sheet_name == DEFAULT_RAID_SHEET_NAME
    assert result.active_raid_sheet_id == 9003
    assert result.active_raid_season is None
    assert sheets_client.moved_sheets == [{"sheet_id": 9003, "index": 1}]
    state_write = sheets_client.written_values[-1]
    state = {str(key): value for key, value in state_write["values"]}
    assert state["schema_version"] == BOT_STATE_SCHEMA_VERSION == "2"
    assert state["active_raid_sheet_name"] == DEFAULT_RAID_SHEET_NAME
    assert state["active_raid_sheet_id"] == 9003
    assert state["active_raid_season"] == ""
    assert state["composition_sheet_name"] == "Состав"
    assert state["composition_sheet_id"] == 111
    assert state["active_cwl_sheet_name"] == "CWL"
    assert state["active_cwl_sheet_id"] == 222


@pytest.mark.asyncio
async def test_initialize_binding_reuses_single_existing_canonical_raid() -> None:
    """Проверяет повторное использование canonical raid без создания дубля."""

    sheets_client = FakeAdminSheetsClient(
        sheets=[
            SheetMetadata(sheet_id=111, title="Состав", index=0),
            SheetMetadata(sheet_id=444, title="Рейды", index=1),
            SheetMetadata(sheet_id=222, title="CWL", index=2),
            SheetMetadata(sheet_id=333, title="_bot_state", index=3),
        ],
    )
    admin = SheetAdminService(
        sheets_client=sheets_client,  # type: ignore[arg-type]
        spreadsheet_id="sheet-id",
        service_account_email="bot@example.com",
        expected_service_account_email=None,
    )

    result = await admin.initialize_new_binding(chat_id=-1001, timezone="Europe/Kyiv")

    assert result.active_raid_sheet_id == 444
    assert sheets_client.added_sheets == []
    assert sheets_client.moved_sheets == []


@pytest.mark.asyncio
async def test_initialize_binding_rejects_duplicate_canonical_raid() -> None:
    """Проверяет остановку неоднозначного обязательного raid-листа."""

    sheets_client = FakeAdminSheetsClient(
        sheets=[
            SheetMetadata(sheet_id=111, title="Состав", index=0),
            SheetMetadata(sheet_id=444, title="Рейды", index=1),
            SheetMetadata(sheet_id=445, title="Рейды", index=2),
            SheetMetadata(sheet_id=222, title="CWL", index=3),
            SheetMetadata(sheet_id=333, title="_bot_state", index=4),
        ],
    )
    admin = SheetAdminService(
        sheets_client=sheets_client,  # type: ignore[arg-type]
        spreadsheet_id="sheet-id",
        service_account_email="bot@example.com",
        expected_service_account_email=None,
    )

    with pytest.raises(SheetAdminError, match="несколько обязательных листов"):
        await admin.initialize_new_binding(chat_id=-1001, timezone="Europe/Kyiv")

    assert sheets_client.added_sheets == []
    assert sheets_client.moved_sheets == []
    assert sheets_client.written_values == []


@pytest.mark.asyncio
async def test_diagnose_marks_stale_raid_binding_fixable_against_canonical() -> None:
    """Проверяет physical sheet ID canonical raid против stale SQLite binding."""

    binding = replace(
        _binding(),
        active_raid_sheet_name="Рейды 2026-07-24",
        active_raid_sheet_id=555,
        active_raid_season="2026-07-24T07:00:00+00:00",
    )
    sheets_client = FakeAdminSheetsClient(
        sheets=[
            SheetMetadata(sheet_id=111, title="Состав", index=0),
            SheetMetadata(sheet_id=444, title="Рейды", index=1),
            SheetMetadata(sheet_id=555, title="Рейды 2026-07-24", index=2),
            SheetMetadata(sheet_id=222, title="CWL", index=3),
            SheetMetadata(sheet_id=333, title="_bot_state", index=4),
        ],
        read_ranges={
            ("_bot_state", "A1:B30"): [
                ["schema_version", "2"],
                ["google_sheet_id", "sheet-id"],
                ["active_raid_sheet_name", "Рейды 2026-07-24"],
                ["active_raid_sheet_id", "555"],
                ["active_raid_season", "2026-07-24T07:00:00+00:00"],
            ],
        },
    )
    admin = SheetAdminService(
        sheets_client=sheets_client,  # type: ignore[arg-type]
        spreadsheet_id="sheet-id",
        service_account_email="bot@example.com",
        expected_service_account_email=None,
    )

    result = await admin.diagnose_binding(binding=binding, blocks=())

    assert any("canonical" in issue.message and issue.fixable for issue in result.issues)


@pytest.mark.asyncio
async def test_autofix_positions_raid_before_canonical_cwl_not_stale_bound_archive() -> None:
    """Проверяет общий CWL recovery resolver в setup auto-fix."""

    binding = replace(
        _binding(),
        active_cwl_sheet_name="CWL 2026-07",
        active_cwl_sheet_id=555,
        active_raid_sheet_id=444,
    )
    sheets_client = FakeAdminSheetsClient(
        sheets=[
            SheetMetadata(sheet_id=444, title="Рейды", index=0),
            SheetMetadata(sheet_id=111, title="Состав", index=1),
            SheetMetadata(sheet_id=555, title="CWL 2026-07", index=2),
            SheetMetadata(sheet_id=222, title="CWL", index=3),
            SheetMetadata(sheet_id=333, title="_bot_state", index=4),
        ],
    )
    admin = SheetAdminService(
        sheets_client=sheets_client,  # type: ignore[arg-type]
        spreadsheet_id="sheet-id",
        service_account_email="bot@example.com",
        expected_service_account_email=None,
    )

    result = await admin.auto_fix_binding(binding=binding, blocks=())

    assert result.active_cwl_sheet_name == "CWL"
    assert result.active_cwl_sheet_id == 222
    assert sheets_client.moved_sheets == [{"sheet_id": 444, "index": 3}]
    state_write = next(
        write for write in sheets_client.written_values if write["sheet_name"] == "_bot_state"
    )
    state = {str(key): value for key, value in state_write["values"]}
    assert state["active_cwl_sheet_name"] == "CWL"
    assert state["active_cwl_sheet_id"] == 222


@pytest.mark.asyncio
async def test_diagnose_reports_legacy_raid_state_staging_stale_registry_and_overflow() -> None:
    """Проверяет fixable raid diagnostics без удаления пользовательских листов."""

    archives = tuple(_archive(index) for index in range(5))
    sheets_client = FakeAdminSheetsClient(
        sheets=[
            SheetMetadata(sheet_id=111, title="Состав", index=0),
            SheetMetadata(sheet_id=444, title="Рейды", index=1),
            SheetMetadata(sheet_id=222, title="CWL", index=2),
            SheetMetadata(sheet_id=333, title="_bot_state", index=3),
            SheetMetadata(sheet_id=700, title="Рейды - staging - 77", index=4),
            *[
                SheetMetadata(sheet_id=archive.sheet_id, title=archive.sheet_name, index=5 + index)
                for index, archive in enumerate(archives[1:])
            ],
        ],
        read_ranges={
            ("_bot_state", "A1:B30"): [
                ["managed_by", "clash-sheet-sync-bot"],
                ["schema_version", "1"],
                ["google_sheet_id", "sheet-id"],
            ],
        },
    )
    admin = SheetAdminService(
        sheets_client=sheets_client,  # type: ignore[arg-type]
        spreadsheet_id="sheet-id",
        service_account_email="bot@example.com",
        expected_service_account_email=None,
    )

    result = await admin.diagnose_binding(
        binding=_binding(),
        blocks=(),
        raid_archives=archives,
        raid_archive_limit=4,
    )

    messages = [issue.message for issue in result.issues]
    assert any("active raid" in message.casefold() and "найден" in message for message in messages)
    assert any("legacy" in issue.message.casefold() and issue.fixable for issue in result.issues)
    assert any("staging" in message.casefold() for message in messages)
    assert any("sheet_id=500" in issue.message and issue.fixable for issue in result.issues)
    assert any("retention" in issue.message.casefold() and issue.fixable for issue in result.issues)
    assert sheets_client.deleted_sheet_ids == []


@pytest.mark.asyncio
async def test_autofix_creates_active_raid_and_returns_safe_archive_cleanup() -> None:
    """Проверяет создание active, stale cleanup и pruning только registry IDs."""

    archives = tuple(_archive(index) for index in range(6))
    sheets_client = FakeAdminSheetsClient(
        sheets=[
            SheetMetadata(sheet_id=111, title="Состав", index=0),
            SheetMetadata(sheet_id=222, title="CWL", index=1),
            SheetMetadata(sheet_id=333, title="_bot_state", index=2),
            *[
                SheetMetadata(sheet_id=archive.sheet_id, title=archive.sheet_name, index=3 + index)
                for index, archive in enumerate(archives[1:])
            ],
            SheetMetadata(sheet_id=999, title="Рейды 2026-06-01", index=9),
        ],
    )
    admin = SheetAdminService(
        sheets_client=sheets_client,  # type: ignore[arg-type]
        spreadsheet_id="sheet-id",
        service_account_email="bot@example.com",
        expected_service_account_email=None,
    )

    result = await admin.auto_fix_binding(
        binding=_binding(),
        blocks=(),
        raid_archives=archives,
        raid_archive_limit=4,
    )

    assert result.active_raid_sheet_name == "Рейды"
    assert result.active_raid_sheet_id is not None
    assert result.active_raid_season is None
    assert {(item.season_key, item.sheet_id) for item in result.raid_archive_cleanup} == {
        (archives[0].season_key, archives[0].sheet_id),
        (archives[1].season_key, archives[1].sheet_id),
    }
    assert sheets_client.deleted_sheet_ids == [archives[1].sheet_id]
    assert 999 not in sheets_client.deleted_sheet_ids
    state_write = next(
        write for write in sheets_client.written_values if write["sheet_name"] == "_bot_state"
    )
    state = {str(key): value for key, value in state_write["values"]}
    assert state["schema_version"] == "2"
    assert state["composition_sheet_name"] == "Состав"
    assert state["composition_sheet_id"] == 111
    assert state["active_cwl_sheet_name"] == "CWL"
    assert state["active_cwl_sheet_id"] == 222
    assert state["active_cwl_season"] == "2026-07"
    assert state["active_raid_sheet_name"] == "Рейды"
    assert state["active_raid_sheet_id"] == result.active_raid_sheet_id
    assert state["active_raid_season"] == ""


@pytest.mark.asyncio
async def test_diagnose_missing_active_raid_is_fixable_without_sheet_creation() -> None:
    """Проверяет read-only обнаружение отсутствующего обязательного active raid."""

    sheets_client = FakeAdminSheetsClient(
        sheets=[
            SheetMetadata(sheet_id=111, title="Состав", index=0),
            SheetMetadata(sheet_id=222, title="CWL", index=1),
            SheetMetadata(sheet_id=333, title="_bot_state", index=2),
        ],
        read_ranges={
            ("_bot_state", "A1:B30"): [
                ["schema_version", "2"],
                ["google_sheet_id", "sheet-id"],
                ["active_raid_sheet_name", "Рейды"],
                ["active_raid_sheet_id", ""],
                ["active_raid_season", ""],
            ],
        },
    )
    admin = SheetAdminService(
        sheets_client=sheets_client,  # type: ignore[arg-type]
        spreadsheet_id="sheet-id",
        service_account_email="bot@example.com",
        expected_service_account_email=None,
    )

    result = await admin.diagnose_binding(binding=_binding(), blocks=())

    assert any(
        issue.message == "Активный лист Рейды отсутствует." and issue.fixable
        for issue in result.issues
    )
    assert sheets_client.added_sheets == []
    assert sheets_client.moved_sheets == []
    assert sheets_client.deleted_sheet_ids == []


@pytest.mark.asyncio
async def test_autofix_rejects_foreign_bot_state_spreadsheet_id() -> None:
    """Проверяет запрет перепривязки служебного зеркала другой таблицы."""

    sheets_client = FakeAdminSheetsClient(
        sheets=[
            SheetMetadata(sheet_id=111, title="Состав", index=0),
            SheetMetadata(sheet_id=444, title="Рейды", index=1),
            SheetMetadata(sheet_id=222, title="CWL", index=2),
            SheetMetadata(sheet_id=333, title="_bot_state", index=3),
        ],
        read_ranges={
            ("_bot_state", "A1:B30"): [
                ["schema_version", "2"],
                ["google_sheet_id", "foreign-sheet-id"],
            ],
        },
    )
    admin = SheetAdminService(
        sheets_client=sheets_client,  # type: ignore[arg-type]
        spreadsheet_id="sheet-id",
        service_account_email="bot@example.com",
        expected_service_account_email=None,
    )

    with pytest.raises(SheetAdminError, match="auto-fix запрещён"):
        await admin.auto_fix_binding(
            binding=replace(_binding(), active_raid_sheet_id=444),
            blocks=(),
        )

    assert sheets_client.written_values == []
    assert sheets_client.hidden_dimensions == []
    assert sheets_client.deleted_sheet_ids == []


@pytest.mark.asyncio
async def test_diagnose_and_autofix_raid_bot_key_use_registered_physical_sheet_id() -> None:
    """Проверяет raid bot key и запрет title fallback для stale block metadata."""

    binding = replace(_binding(), active_raid_sheet_id=444)
    sheets_client = FakeAdminSheetsClient(
        sheets=[
            SheetMetadata(sheet_id=111, title="Состав", index=0),
            SheetMetadata(sheet_id=444, title="Рейды", index=1),
            SheetMetadata(sheet_id=222, title="CWL", index=2),
            SheetMetadata(sheet_id=333, title="_bot_state", index=3),
        ],
        read_ranges={
            ("_bot_state", "A1:B30"): [
                ["schema_version", "2"],
                ["google_sheet_id", "sheet-id"],
                ["active_raid_sheet_name", "Рейды"],
                ["active_raid_sheet_id", "444"],
                ["active_raid_season", ""],
            ],
            ("Рейды", "A1:A2"): [["Alpha"], ["__bot_key"]],
        },
    )
    admin = SheetAdminService(
        sheets_client=sheets_client,  # type: ignore[arg-type]
        spreadsheet_id="sheet-id",
        service_account_email="bot@example.com",
        expected_service_account_email=None,
    )
    valid = SheetBlock(-1001, "Рейды", 444, "raid:#AAA111", "A1", 3, 8)
    stale = SheetBlock(-1001, "Рейды", 999, "raid:#BBB222", "A10", 3, 8)

    diagnostic = await admin.diagnose_binding(
        binding=binding,
        blocks=(valid, stale),
    )
    assert any("stale physical" in issue.message for issue in diagnostic.issues)
    assert any("__bot_key найден" in issue.message for issue in diagnostic.issues)
    assert any(
        "__bot_key" in issue.message and "не скрыт" in issue.message for issue in diagnostic.issues
    )

    await admin.auto_fix_binding(binding=binding, blocks=(valid, stale))
    assert sheets_client.hidden_dimensions == [
        {
            "sheet_id": 444,
            "dimension": "COLUMNS",
            "start_index": 0,
            "end_index": 1,
            "hidden": True,
        },
    ]
    assert sheets_client.hidden_columns == {(444, 0)}


@pytest.mark.asyncio
async def test_autofix_pruning_expected_error_is_recoverable_and_unexpected_propagates() -> None:
    """Проверяет узкую recoverable границу auto-fix pruning."""

    archives = tuple(_archive(index) for index in range(5))
    metadata = [
        SheetMetadata(sheet_id=111, title="Состав", index=0),
        SheetMetadata(sheet_id=444, title="Рейды", index=1),
        SheetMetadata(sheet_id=222, title="CWL", index=2),
        SheetMetadata(sheet_id=333, title="_bot_state", index=3),
        *[
            SheetMetadata(sheet_id=archive.sheet_id, title=archive.sheet_name, index=4 + index)
            for index, archive in enumerate(archives)
        ],
    ]
    expected_client = FakeAdminSheetsClient(
        sheets=list(metadata),
        delete_error=GoogleSheetsWriteError("delete failed"),
    )
    expected_admin = SheetAdminService(
        sheets_client=expected_client,  # type: ignore[arg-type]
        spreadsheet_id="sheet-id",
        service_account_email="bot@example.com",
        expected_service_account_email=None,
    )

    result = await expected_admin.auto_fix_binding(
        binding=replace(_binding(), active_raid_sheet_id=444),
        blocks=(),
        raid_archives=archives,
        raid_archive_limit=4,
    )

    assert result.raid_archive_cleanup == ()
    assert len(result.cleanup_warnings) == 1
    assert "pruning будет повторён" in result.cleanup_warnings[0]
    assert expected_client.deleted_sheet_ids == []

    unexpected = RuntimeError("programming error")
    unexpected_client = FakeAdminSheetsClient(
        sheets=list(metadata),
        delete_error=unexpected,
    )
    unexpected_admin = SheetAdminService(
        sheets_client=unexpected_client,  # type: ignore[arg-type]
        spreadsheet_id="sheet-id",
        service_account_email="bot@example.com",
        expected_service_account_email=None,
    )
    with pytest.raises(RuntimeError) as caught:
        await unexpected_admin.auto_fix_binding(
            binding=replace(_binding(), active_raid_sheet_id=444),
            blocks=(),
            raid_archives=archives,
            raid_archive_limit=4,
        )
    assert caught.value is unexpected


@pytest.mark.asyncio
async def test_autofix_rejects_duplicate_archive_sheet_id_without_delete() -> None:
    """Проверяет остановку неоднозначного physical registry до удаления."""

    archive = _archive(0)
    sheets_client = FakeAdminSheetsClient(
        sheets=[
            SheetMetadata(sheet_id=111, title="Состав", index=0),
            SheetMetadata(sheet_id=444, title="Рейды", index=1),
            SheetMetadata(sheet_id=222, title="CWL", index=2),
            SheetMetadata(sheet_id=333, title="_bot_state", index=3),
            SheetMetadata(sheet_id=archive.sheet_id, title=archive.sheet_name, index=4),
            SheetMetadata(sheet_id=archive.sheet_id, title="duplicate", index=5),
        ],
    )
    admin = SheetAdminService(
        sheets_client=sheets_client,  # type: ignore[arg-type]
        spreadsheet_id="sheet-id",
        service_account_email="bot@example.com",
        expected_service_account_email=None,
    )

    with pytest.raises(SheetAdminError, match="Неоднозначный raid archive"):
        await admin.auto_fix_binding(
            binding=replace(_binding(), active_raid_sheet_id=444),
            blocks=(),
            raid_archives=(archive,),
            raid_archive_limit=1,
        )

    assert sheets_client.deleted_sheet_ids == []


@pytest.mark.asyncio
async def test_autofix_never_prunes_required_or_staging_sheet_ids() -> None:
    """Проверяет запрет delete обязательных и staging листов из corrupt registry."""

    required = replace(_archive(0), sheet_id=222, sheet_name="CWL")
    staging = replace(
        _archive(1),
        sheet_id=700,
        sheet_name="Рейды - staging - orphan",
    )
    safe = tuple(_archive(index) for index in range(2, 7))
    archives = (required, staging, *safe)
    sheets_client = FakeAdminSheetsClient(
        sheets=[
            SheetMetadata(sheet_id=111, title="Состав", index=0),
            SheetMetadata(sheet_id=444, title="Рейды", index=1),
            SheetMetadata(sheet_id=222, title="CWL", index=2),
            SheetMetadata(sheet_id=333, title="_bot_state", index=3),
            SheetMetadata(sheet_id=700, title=staging.sheet_name, index=4),
            *[
                SheetMetadata(sheet_id=archive.sheet_id, title=archive.sheet_name, index=5 + index)
                for index, archive in enumerate(safe)
            ],
        ],
    )
    admin = SheetAdminService(
        sheets_client=sheets_client,  # type: ignore[arg-type]
        spreadsheet_id="sheet-id",
        service_account_email="bot@example.com",
        expected_service_account_email=None,
    )

    result = await admin.auto_fix_binding(
        binding=replace(_binding(), active_raid_sheet_id=444),
        blocks=(),
        raid_archives=archives,
        raid_archive_limit=4,
    )

    assert sheets_client.deleted_sheet_ids == []
    assert result.raid_archive_cleanup == ()
    assert any("protected/staging" in warning for warning in result.cleanup_warnings)


@pytest.mark.asyncio
async def test_autofix_uses_current_sheet_id_for_stale_sheet_blocks() -> None:
    """Проверяет, что auto-fix не использует stale sheet_id из sheet_blocks."""

    sheets_client = FakeAdminSheetsClient(
        sheets=[
            SheetMetadata(sheet_id=111, title="Состав", index=0),
            SheetMetadata(sheet_id=444, title="Рейды", index=1),
            SheetMetadata(sheet_id=222, title="CWL", index=2),
            SheetMetadata(sheet_id=333, title="_bot_state", index=3),
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
    assert result.active_raid_sheet_id == 444
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
            SheetMetadata(sheet_id=111, title="Состав", index=0),
            SheetMetadata(sheet_id=444, title="Рейды", index=1),
            SheetMetadata(sheet_id=222, title="CWL", index=2),
            SheetMetadata(sheet_id=333, title="_bot_state", index=3),
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
