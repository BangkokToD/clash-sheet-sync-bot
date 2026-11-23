"""Защита ручных ячеек при скрытых колонках и сдвиге managed blocks."""

from dataclasses import replace

import pytest

from clash_sheet_sync_bot.models import ColumnProfile, TableType
from clash_sheet_sync_bot.sheets.column_profiles import default_columns
from clash_sheet_sync_bot.sync.composition import (
    CompositionDataError,
    import_current_composition_sheet,
)
from clash_sheet_sync_bot.sync.cwl import (
    CWL_WIDE_IMPORT_RANGE,
    CwlDataError,
    import_current_cwl_sheet,
)
from tests.fakes.factories import make_column_profile, make_runtime_config, make_sheet_block
from tests.fakes.sheets import FakeSheetsClient


def _note_profile(table_type: TableType) -> ColumnProfile:
    return make_column_profile(
        table_type=table_type,
        column_key="note",
        title="Комментарий",
        visible=True,
        kind="user",
        value_type="string",
        sort_order=1000,
    )


def _profiles(table_type: TableType) -> tuple[ColumnProfile, ...]:
    return tuple(
        make_column_profile(
            table_type=table_type,
            column_key=p.column_key,
            title=p.title,
            visible=p.visible,
            kind=p.kind,
            value_type=p.value_type,
            sort_order=p.sort_order,
        )
        for p in default_columns(table_type)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hidden_columns", [("attacker_tag",), ("round", "attacker_tag", "attacker_name")]
)
async def test_cwl_import_preserves_notes_with_hidden_system_columns(hidden_columns) -> None:
    profiles = (
        *(
            replace(p, visible=False) if p.column_key in hidden_columns else p
            for p in _profiles("cwl")
        ),
        _note_profile("cwl"),
    )
    runtime = make_runtime_config(column_profiles=profiles)
    columns = sorted(
        (p for p in profiles if p.table_type == "cwl" and (p.visible or p.kind == "service")),
        key=lambda p: p.sort_order,
    )
    row_key = "2026-10-01|#AAA111|1|#PLAYER|ATTACK_1"
    cells = {
        "bot_key": f"cwl_row:{row_key}",
        "round": 1,
        "attacker_name": "Player",
        "note": "Свежий разбор атаки",
    }
    values = [
        ["Alpha | #AAA111"],
        [p.title for p in columns],
        [cells.get(p.column_key, "") for p in columns],
    ]
    sheets = FakeSheetsClient(values_by_range={("CWL", CWL_WIDE_IMPORT_RANGE): values})
    imported = await import_current_cwl_sheet(
        runtime_config=runtime, sheets_client=sheets, blocks=(), season="2026-10-01"
    )

    assert imported.rows_by_key[row_key] == {"note": "Свежий разбор атаки"}


@pytest.mark.asyncio
async def test_cwl_import_stops_before_discarding_notes_without_row_identity() -> None:
    profiles = (
        *(
            replace(p, visible=False) if p.column_key == "attacker_tag" else p
            for p in _profiles("cwl")
        ),
        _note_profile("cwl"),
    )
    runtime = make_runtime_config(column_profiles=profiles)
    sheets = FakeSheetsClient(
        values_by_range={
            ("CWL", CWL_WIDE_IMPORT_RANGE): [
                ["", "Alpha | #AAA111"],
                ["__bot_key", "Раунд", "Ник", "Комментарий"],
                ["", 7, "Player", "Это нельзя стирать"],
            ]
        }
    )

    with pytest.raises(CwlDataError, match="заполнены ручные ячейки"):
        await import_current_cwl_sheet(
            runtime_config=runtime, sheets_client=sheets, blocks=(), season="2026-10-01"
        )

    assert sheets.batch_value_updates == []


def _shifted_composition():
    profiles = (*_profiles("composition_active"), _note_profile("composition_active"))
    runtime = make_runtime_config(column_profiles=profiles)
    blocks = (
        make_sheet_block(
            sheet_name="Состав",
            block_key="composition_active:#AAA111",
            start_cell="A1",
            rows_count=4,
            columns_count=3,
        ),
        make_sheet_block(
            sheet_name="Состав",
            block_key="composition_active:#BBB222",
            start_cell="A6",
            rows_count=3,
            columns_count=3,
        ),
    )
    header = ["__bot_key", "Тег", "Комментарий"]
    # Пользователь удалил одну строку первого клана: второй блок сдвинулся вверх.
    values = [
        ["", "Alpha | #AAA111"],
        header,
        ["composition_player:#P1", "#P1", "Первый клан"],
        [],
        ["", "Beta | #BBB222"],
        header,
        ["composition_player:#P2", "#P2", "Свежая заметка второго клана"],
    ]
    sheets = FakeSheetsClient(
        values_by_range={
            ("Состав", "A1:C4"): values[:4],
            ("Состав", "A6:C8"): values[5:8],
            ("Состав", "A1:C8"): values,
        }
    )
    return runtime, blocks, sheets


@pytest.mark.asyncio
async def test_composition_import_recovers_shifted_blocks_within_registered_area() -> None:
    runtime, blocks, sheets = _shifted_composition()

    imported = await import_current_composition_sheet(
        runtime_config=runtime, sheets_client=sheets, blocks=blocks
    )

    assert imported.players["#P1"].user_values == {"note": "Первый клан"}
    assert imported.players["#P2"].user_values == {"note": "Свежая заметка второго клана"}
    assert imported.players["#P2"].clan_tag == "#BBB222"
    assert imported.warnings
    assert sheets.batch_value_updates == []


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing_header", "duplicate_clan", "truncated"])
async def test_composition_import_rejects_ambiguous_or_incomplete_shifted_layout(damage) -> None:
    runtime, blocks, sheets = _shifted_composition()
    values = sheets.values_by_range[("Состав", "A1:C8")]
    if damage == "missing_header":
        values[5] = ["Повреждённый заголовок"]
    elif damage == "duplicate_clan":
        values[4] = ["", "Alpha | #AAA111"]
    else:
        values.append(["composition_player:#P3", "#P3", "Продолжение за границей"])

    with pytest.raises(CompositionDataError):
        await import_current_composition_sheet(
            runtime_config=runtime, sheets_client=sheets, blocks=blocks
        )

    assert sheets.batch_value_updates == []
