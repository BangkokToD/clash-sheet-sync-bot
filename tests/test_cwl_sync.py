"""Unit-тесты CWL season, row key и concurrency helpers."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from clash_sheet_sync_bot.coc.client import ClashApiUnavailableError
from clash_sheet_sync_bot.models import ColumnProfile, TrackedClan
from clash_sheet_sync_bot.repositories import CwlRowState
from clash_sheet_sync_bot.sheets.client import SheetMetadata, SheetsClient, SpreadsheetMetadata
from clash_sheet_sync_bot.sheets.column_profiles import default_columns
from clash_sheet_sync_bot.sync.cwl import (
    SOFT_PINK_RGB,
    CwlClanBlock,
    CwlDataError,
    CwlImportResult,
    CwlPlannedRow,
    CwlPreparedData,
    CwlSeasonMismatchError,
    CwlTechnicalValues,
    _apply_user_values,
    _build_cwl_format_requests,
    _cwl_composition_user_column_links,
    _is_missing_attack_display,
    _load_cwl_wars,
    _planned_row_from_state,
    _prepare_saved_cwl_data,
    _resolve_active_cwl_sheet,
    _resolve_cwl_season,
    _write_bot_state,
    build_cwl_sheet_blocks,
    build_cwl_sheet_matrix,
    make_cwl_row_key,
)
from tests.fakes.factories import make_column_profile, make_runtime_config

JsonObject = dict[str, Any]


class RecordingBotStateSheetsClient:
    """Фиксирует запись служебного зеркала из CWL apply."""

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    async def write_values(self, sheet_name: str, range_a1: str, values: Any) -> None:
        self.writes.append(
            {"sheet_name": sheet_name, "range_a1": range_a1, "values": values},
        )


@pytest.mark.asyncio
async def test_cwl_bot_state_write_preserves_raid_binding_fields() -> None:
    """Проверяет, что CWL apply не откатывает `_bot_state` к legacy schema."""

    sheets = RecordingBotStateSheetsClient()
    runtime = make_runtime_config()

    await _write_bot_state(
        runtime_config=runtime,
        sheets_client=sheets,  # type: ignore[arg-type]
        active_cwl_sheet_name="CWL",
        active_cwl_sheet_id=222,
        active_cwl_season="2026-08",
    )

    state = {str(key): value for key, value in sheets.writes[0]["values"]}
    assert state["schema_version"] == "2"
    assert sheets.writes[0]["range_a1"] == "A1:B16"
    assert state["composition_sheet_name"] == runtime.sheet_binding.composition_sheet_name
    assert state["composition_sheet_id"] == runtime.sheet_binding.composition_sheet_id
    assert state["active_cwl_sheet_name"] == "CWL"
    assert state["active_cwl_sheet_id"] == 222
    assert state["active_cwl_season"] == "2026-08"
    assert state["active_raid_sheet_name"] == "Рейды"
    assert state["active_raid_sheet_id"] == 444
    assert state["active_raid_season"] == ""


class FakeClashClient:
    """Fake Clash client для проверки concurrency limit загрузки CWL wars."""

    def __init__(self) -> None:
        self.current_requests = 0
        self.max_concurrent_requests = 0
        self.loaded_war_tags: list[str] = []

    async def get_cwl_war(self, war_tag: str) -> JsonObject:
        """Имитирует загрузку CWL war и фиксирует максимальную конкурентность."""

        self.current_requests += 1
        self.max_concurrent_requests = max(
            self.max_concurrent_requests,
            self.current_requests,
        )
        try:
            await asyncio.sleep(0.01)
            self.loaded_war_tags.append(war_tag)
            return {
                "warTag": war_tag,
                "state": "warEnded",
            }
        finally:
            self.current_requests -= 1


def _tracked_clan(*, tag: str, name: str, sort_order: int) -> TrackedClan:
    """Создаёт tracked clan для CWL season tests."""

    return TrackedClan(
        chat_id=-1001,
        clan_tag=tag,
        clan_name=name,
        sort_order=sort_order,
    )


def test_make_cwl_row_key_normalizes_tags() -> None:
    """Проверяет нормализацию clan_tag и attacker_tag в CWL row key."""

    row_key = make_cwl_row_key(
        season="2026-07",
        clan_tag=" #abc123 ",
        round_number=3,
        attacker_tag=" #pqr9 ",
        marker="ATTACK_1",
    )

    assert row_key == "2026-07|#ABC123|3|#PQR9|ATTACK_1"


def test_resolve_cwl_season_returns_single_season() -> None:
    """Проверяет выбор season, если все active clans вернули один сезон."""

    active_clans = (
        _tracked_clan(tag="#AAA111", name="Alpha", sort_order=10),
        _tracked_clan(tag="#BBB222", name="Beta", sort_order=20),
    )
    groups = {
        "#AAA111": {"season": "2026-07"},
        "#BBB222": {"season": "2026-07"},
    }

    assert _resolve_cwl_season(active_clans, groups) == "2026-07"


def test_resolve_cwl_season_rejects_mismatched_seasons() -> None:
    """Проверяет отказ от sync при разных CWL-сезонах."""

    active_clans = (
        _tracked_clan(tag="#AAA111", name="Alpha", sort_order=10),
        _tracked_clan(tag="#BBB222", name="Beta", sort_order=20),
    )
    groups = {
        "#AAA111": {"season": "2026-07"},
        "#BBB222": {"season": "2026-08"},
    }

    with pytest.raises(CwlSeasonMismatchError) as exc_info:
        _resolve_cwl_season(active_clans, groups)

    message = str(exc_info.value)

    assert "CWL-сезоны активных кланов не совпадают" in message
    assert "Alpha | #AAA111: 2026-07" in message
    assert "Beta | #BBB222: 2026-08" in message


def test_resolve_cwl_season_rejects_missing_season() -> None:
    """Проверяет ошибку, если CoC API не вернул season."""

    active_clans = (_tracked_clan(tag="#AAA111", name="Alpha", sort_order=10),)
    groups = {"#AAA111": {}}

    with pytest.raises(ClashApiUnavailableError, match="season"):
        _resolve_cwl_season(active_clans, groups)


@pytest.mark.asyncio
async def test_load_cwl_wars_respects_concurrency_limit() -> None:
    """Проверяет, что _load_cwl_wars реально ограничивает конкурентность."""

    clash_client = FakeClashClient()
    war_tags = ("#WAR1", "#WAR2", "#WAR3", "#WAR4", "#WAR5")

    wars_by_tag = await _load_cwl_wars(
        clash_client=clash_client,  # type: ignore[arg-type]
        war_tags=war_tags,
        concurrency_limit=2,
    )

    assert set(wars_by_tag) == set(war_tags)
    assert set(clash_client.loaded_war_tags) == set(war_tags)
    assert clash_client.max_concurrent_requests <= 2


@pytest.mark.asyncio
async def test_load_cwl_wars_rejects_non_positive_concurrency_limit() -> None:
    """Проверяет ошибку для concurrency_limit <= 0."""

    clash_client = FakeClashClient()

    with pytest.raises(CwlDataError, match="должен быть положительным"):
        await _load_cwl_wars(
            clash_client=clash_client,  # type: ignore[arg-type]
            war_tags=("#WAR1",),
            concurrency_limit=0,
        )


def _column_profile(
    *,
    table_type: str,
    column_key: str,
    title: str,
    sort_order: int,
) -> ColumnProfile:
    """Создаёт user ColumnProfile для CWL inheritance tests."""

    return ColumnProfile(
        chat_id=-1001,
        table_type=table_type,  # type: ignore[arg-type]
        column_key=column_key,
        title=title,
        visible=True,
        kind="user",
        value_type="string",
        sort_order=sort_order,
    )


def _planned_cwl_row(*, user_values: dict[str, str] | None = None) -> CwlPlannedRow:
    """Создаёт planned CWL row для user-values tests."""

    row_key = make_cwl_row_key(
        season="2026-07",
        clan_tag="#AAA111",
        round_number=1,
        attacker_tag="#P1",
        marker="NO_ATTACK",
    )
    return CwlPlannedRow(
        row_key=row_key,
        season="2026-07",
        clan_tag="#AAA111",
        round_number=1,
        attacker_tag="#P1",
        marker="NO_ATTACK",
        technical_values=CwlTechnicalValues(
            round_number=1,
            attacker_tag="#P1",
            attacker_name="Player",
            attacker_town_hall=15,
            defender_town_hall=None,
            stars=None,
            destruction_percentage=None,
            marker="NO_ATTACK",
            attacker_map_position=1,
            defender_map_position=None,
        ),
        no_attack_key=row_key,
        user_values=user_values or {},
    )


def test_cwl_composition_user_column_links_match_by_title() -> None:
    """Проверяет связь user-колонок CWL и состава по совпадающему названию."""

    profiles = (
        _column_profile(
            table_type="composition_active",
            column_key="composition_username",
            title="Юзернейм",
            sort_order=100,
        ),
        _column_profile(
            table_type="cwl",
            column_key="cwl_username",
            title="Юзернейм",
            sort_order=100,
        ),
        _column_profile(
            table_type="cwl",
            column_key="cwl_note",
            title="Заметка CWL",
            sort_order=110,
        ),
    )

    assert _cwl_composition_user_column_links(profiles) == {
        "cwl_username": ("composition_username",),
    }


def test_apply_user_values_fills_empty_cwl_value_from_composition() -> None:
    """Проверяет, что пустое CWL user-value подтягивается из состава."""

    row = _planned_cwl_row()
    result = _apply_user_values(
        planned_rows=(row,),
        imported=CwlImportResult(rows_by_key={}, warnings=()),
        existing_rows=(),
        composition_user_values_by_player={
            "#P1": {
                "composition_username": "@player",
            },
        },
        cwl_composition_user_column_links={
            "cwl_username": ("composition_username",),
        },
    )

    assert result[0].user_values == {"cwl_username": "@player"}


def test_apply_user_values_does_not_overwrite_existing_cwl_value_from_composition() -> None:
    """Проверяет, что заполненное CWL user-value важнее значения из состава."""

    row = _planned_cwl_row()
    result = _apply_user_values(
        planned_rows=(row,),
        imported=CwlImportResult(
            rows_by_key={
                row.row_key: {
                    "cwl_username": "@manual-cwl",
                },
            },
            warnings=(),
        ),
        existing_rows=(),
        composition_user_values_by_player={
            "#P1": {
                "composition_username": "@from-composition",
            },
        },
        cwl_composition_user_column_links={
            "cwl_username": ("composition_username",),
        },
    )

    assert result[0].user_values == {"cwl_username": "@manual-cwl"}


def test_apply_user_values_treats_blank_cwl_value_as_empty_for_composition_inheritance() -> None:
    """Проверяет, что пустая строка в CWL заменяется значением из состава."""

    row = _planned_cwl_row()
    result = _apply_user_values(
        planned_rows=(row,),
        imported=CwlImportResult(
            rows_by_key={
                row.row_key: {
                    "cwl_username": "   ",
                },
            },
            warnings=(),
        ),
        existing_rows=(),
        composition_user_values_by_player={
            "#P1": {
                "composition_username": "@from-composition",
            },
        },
        cwl_composition_user_column_links={
            "cwl_username": ("composition_username",),
        },
    )

    assert result[0].user_values == {"cwl_username": "@from-composition"}


def _saved_cwl_state(
    *,
    clan_tag: str = "#AAA111",
    season: str = "2026-07",
    user_values: dict[str, str] | None = None,
) -> CwlRowState:
    """Создаёт сохранённую строку CWL для межсезонных тестов."""

    row_key = make_cwl_row_key(
        season=season,
        clan_tag=clan_tag,
        round_number=1,
        attacker_tag="#P1",
        marker="NO_ATTACK",
    )
    technical = CwlTechnicalValues(
        round_number=1,
        attacker_tag="#P1",
        attacker_name="Player",
        attacker_town_hall=16,
        defender_town_hall=None,
        stars=None,
        destruction_percentage=None,
        marker="NO_ATTACK",
        attacker_map_position=1,
        defender_map_position=None,
    )
    return CwlRowState(
        season=season,
        row_key=row_key,
        clan_tag=clan_tag,
        round_number=1,
        attacker_tag="#P1",
        marker="NO_ATTACK",
        technical_values=technical.to_json(),
        user_values=user_values or {},
        row_hash="stored-hash",
    )


def _cwl_profiles() -> tuple[ColumnProfile, ...]:
    """Создаёт минимальный физический профиль CWL-листа."""

    return (
        make_column_profile(
            table_type="cwl",
            column_key="bot_key",
            title="__bot_key",
            visible=False,
            kind="service",
            value_type="string",
            sort_order=0,
        ),
        make_column_profile(
            table_type="cwl",
            column_key="round",
            title="Раунд",
            visible=True,
            kind="system",
            value_type="integer",
            sort_order=10,
        ),
        make_column_profile(
            table_type="cwl",
            column_key="attacker_name",
            title="Ник",
            visible=True,
            kind="system",
            value_type="string",
            sort_order=20,
        ),
    )


def _default_cwl_profiles() -> tuple[ColumnProfile, ...]:
    """Создаёт полный дефолтный профиль CWL."""

    return tuple(
        ColumnProfile(
            chat_id=-1001,
            table_type=definition.table_type,
            column_key=definition.column_key,
            title=definition.title,
            visible=definition.visible,
            kind=definition.kind,
            value_type=definition.value_type,
            sort_order=definition.sort_order,
        )
        for definition in default_columns("cwl")
    )


def test_cwl_values_include_units_and_highlight_missing_attack_cells() -> None:
    """Проверяет единицы CWL и розовые ячейки строки без атаки."""

    clan = _tracked_clan(tag="#AAA111", name="Alpha", sort_order=10)
    no_attack = _planned_cwl_row()
    attack_row_key = make_cwl_row_key(
        season="2026-07",
        clan_tag="#AAA111",
        round_number=1,
        attacker_tag="#P2",
        marker="ATTACK_1",
    )
    attacked = CwlPlannedRow(
        row_key=attack_row_key,
        season="2026-07",
        clan_tag="#AAA111",
        round_number=1,
        attacker_tag="#P2",
        marker="ATTACK_1",
        technical_values=CwlTechnicalValues(
            round_number=1,
            attacker_tag="#P2",
            attacker_name="Attacker",
            attacker_town_hall=16,
            defender_town_hall=15,
            stars=3,
            destruction_percentage=100,
            marker="ATTACK_1",
            attacker_map_position=2,
            defender_map_position=1,
        ),
        no_attack_key=make_cwl_row_key(
            season="2026-07",
            clan_tag="#AAA111",
            round_number=1,
            attacker_tag="#P2",
            marker="NO_ATTACK",
        ),
    )
    prepared = CwlPreparedData(
        season="2026-07",
        clan_blocks=(
            CwlClanBlock(
                clan=clan,
                rows=(no_attack, attacked),
                rounds_count=1,
            ),
        ),
        rows=(no_attack, attacked),
        all_not_in_progress=False,
        not_in_progress_clans=(),
        warnings=(),
    )
    columns = _default_cwl_profiles()
    built_blocks = build_cwl_sheet_blocks(
        runtime_config=make_runtime_config(
            active_clans=(clan,),
            column_profiles=columns,
        ),
        sheet_name="CWL",
        sheet_id=222,
        prepared=prepared,
        columns=columns,
    )
    values = built_blocks[0].values
    indexes = {column.column_key: index for index, column in enumerate(columns)}

    assert values[2][indexes["attacker_town_hall"]] == "TH15"
    assert values[2][indexes["defender_town_hall"]] == "—"
    assert values[2][indexes["stars"]] == "—"
    assert values[2][indexes["destruction_percentage"]] == "—"
    assert values[3][indexes["attacker_town_hall"]] == "TH16"
    assert values[3][indexes["defender_town_hall"]] == "TH15"
    assert values[3][indexes["stars"]] == "★★★"
    assert values[3][indexes["destruction_percentage"]] == "100%"

    format_requests = _build_cwl_format_requests(
        sheet_id=222,
        matrix_rows_count=6,
        columns=columns,
        built_blocks=built_blocks,
    )
    pink_ranges = [
        request["repeatCell"]["range"]
        for request in format_requests
        if request.get("repeatCell", {})
        .get("cell", {})
        .get("userEnteredFormat", {})
        .get("backgroundColorStyle", {})
        .get("rgbColor")
        == SOFT_PINK_RGB
    ]

    assert pink_ranges == [
        {
            "sheetId": 222,
            "startRowIndex": 4,
            "endRowIndex": 5,
            "startColumnIndex": indexes["defender_town_hall"],
            "endColumnIndex": indexes["defender_town_hall"] + 1,
        },
        {
            "sheetId": 222,
            "startRowIndex": 4,
            "endRowIndex": 5,
            "startColumnIndex": indexes["stars"],
            "endColumnIndex": indexes["stars"] + 1,
        },
        {
            "sheetId": 222,
            "startRowIndex": 4,
            "endRowIndex": 5,
            "startColumnIndex": indexes["destruction_percentage"],
            "endColumnIndex": indexes["destruction_percentage"] + 1,
        },
    ]


@pytest.mark.parametrize("value", ("", "-", "—"))
def test_missing_attack_display_accepts_old_and_new_placeholders(value: str) -> None:
    """Проверяет fallback импорта строки без атаки."""

    assert _is_missing_attack_display(value) is True


def test_planned_row_from_state_restores_technical_and_user_values() -> None:
    """Проверяет безопасное восстановление CWL-строки из SQLite."""

    restored = _planned_row_from_state(
        _saved_cwl_state(user_values={"cwl_note": "Капитан"}),
    )

    assert restored.season == "2026-07"
    assert restored.clan_tag == "#AAA111"
    assert restored.technical_values.attacker_name == "Player"
    assert restored.user_values == {"cwl_note": "Капитан"}


def test_planned_row_from_state_rejects_inconsistent_state() -> None:
    """Проверяет отказ от повреждённого CWL state."""

    state = _saved_cwl_state()
    inconsistent = CwlRowState(
        season=state.season,
        row_key=state.row_key,
        clan_tag=state.clan_tag,
        round_number=2,
        attacker_tag=state.attacker_tag,
        marker=state.marker,
        technical_values=state.technical_values,
        user_values=state.user_values,
        row_hash=state.row_hash,
    )

    with pytest.raises(CwlDataError, match="противоречивые поля"):
        _planned_row_from_state(inconsistent)


class FakeSavedCwlRepository:
    """Fake CWL repository для межсезонной подготовки."""

    def __init__(self, *, season: str | None, rows: tuple[CwlRowState, ...] = ()) -> None:
        self.season = season
        self.rows = rows

    async def get_latest_season(
        self,
        *,
        chat_id: int,
        clan_tags: tuple[str, ...],
    ) -> str | None:
        del chat_id, clan_tags
        return self.season

    async def list_rows(self, *, chat_id: int, season: str) -> tuple[CwlRowState, ...]:
        del chat_id
        assert season == self.season
        return self.rows


class FakeSavedSheetBlockRepository:
    """Fake sheet-block repository без сохранённых диапазонов."""

    async def list_blocks(self, chat_id: int, sheet_name: str) -> tuple[()]:
        del chat_id, sheet_name
        return ()


class FakeSavedSheetsClient:
    """Fake Sheets client с пустым активным CWL-листом."""

    async def read_values(self, sheet_name: str, range_a1: str) -> list[list[str]]:
        del sheet_name, range_a1
        return []


@pytest.mark.asyncio
async def test_prepare_saved_cwl_data_restores_latest_season_for_matching_clan() -> None:
    """Проверяет показ последнего сезона и сообщение клану без истории."""

    clans = (
        _tracked_clan(tag="#AAA111", name="Alpha", sort_order=10),
        _tracked_clan(tag="#BBB222", name="Beta", sort_order=20),
    )
    runtime_config = make_runtime_config(
        active_clans=clans,
        column_profiles=_cwl_profiles(),
    )

    prepared = await _prepare_saved_cwl_data(
        runtime_config=runtime_config,
        sheets_client=FakeSavedSheetsClient(),  # type: ignore[arg-type]
        cwl_repository=FakeSavedCwlRepository(  # type: ignore[arg-type]
            season="2026-07",
            rows=(_saved_cwl_state(),),
        ),
        sheet_block_repository=FakeSavedSheetBlockRepository(),  # type: ignore[arg-type]
        not_in_progress_clans=clans,
        composition_player_states=(),
    )

    assert prepared.season == "2026-07"
    assert prepared.all_not_in_progress is True
    assert prepared.showing_previous_season is True
    assert len(prepared.rows) == 1
    assert prepared.clan_blocks[0].rows == prepared.rows
    assert prepared.clan_blocks[1].message == "Нет сохранённых данных CWL за сезон 2026-07"


@pytest.mark.asyncio
async def test_prepare_saved_cwl_data_builds_explanation_when_history_is_empty() -> None:
    """Проверяет понятный лист вместо пустой страницы без истории."""

    clan = _tracked_clan(tag="#AAA111", name="Alpha", sort_order=10)
    runtime_config = make_runtime_config(
        active_clans=(clan,),
        column_profiles=_cwl_profiles(),
    )

    prepared = await _prepare_saved_cwl_data(
        runtime_config=runtime_config,
        sheets_client=FakeSavedSheetsClient(),  # type: ignore[arg-type]
        cwl_repository=FakeSavedCwlRepository(season=None),  # type: ignore[arg-type]
        sheet_block_repository=FakeSavedSheetBlockRepository(),  # type: ignore[arg-type]
        not_in_progress_clans=(clan,),
        composition_player_states=(),
    )
    blocks = build_cwl_sheet_blocks(
        runtime_config=runtime_config,
        sheet_name="CWL",
        sheet_id=222,
        prepared=prepared,
        columns=_cwl_profiles(),
    )
    matrix = build_cwl_sheet_matrix(
        prepared=prepared,
        columns=_cwl_profiles(),
        built_blocks=blocks,
    )

    assert prepared.season is None
    assert prepared.showing_previous_season is False
    assert "CWL сейчас не проводится" in matrix[0]
    assert any("Нет сохранённых данных за предыдущий сезон" in row for row in matrix)


class FakeResolverSheetsClient:
    """Fake Sheets client для восстановления после ротации."""

    async def get_spreadsheet_metadata(self) -> SpreadsheetMetadata:
        return SpreadsheetMetadata(
            spreadsheet_id="sheet-id",
            title="Test",
            sheets=(
                SheetMetadata(sheet_id=222, title="CWL 2026-07", index=1),
                SheetMetadata(sheet_id=444, title="CWL", index=2),
            ),
        )


@pytest.mark.asyncio
async def test_resolve_active_cwl_sheet_prefers_canonical_after_partial_rotation() -> None:
    """Проверяет, что устаревший ID не возвращает бота в архивный лист."""

    resolved = await _resolve_active_cwl_sheet(
        make_runtime_config(column_profiles=_cwl_profiles()),
        FakeResolverSheetsClient(),  # type: ignore[arg-type]
    )

    assert resolved.sheet_id == 444
    assert resolved.title == "CWL"


@pytest.mark.asyncio
async def test_rename_sheets_atomically_uses_one_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверяет атомарную смену имён архивного и нового CWL-листов."""

    client = object.__new__(SheetsClient)
    calls: list[list[JsonObject]] = []

    async def capture(requests: list[JsonObject]) -> JsonObject:
        calls.append(requests)
        return {}

    monkeypatch.setattr(client, "batch_update_spreadsheet", capture)

    await client.rename_sheets_atomically(
        (
            (222, "CWL 2026-07"),
            (444, "CWL"),
        ),
    )

    assert len(calls) == 1
    assert calls[0] == [
        {
            "updateSheetProperties": {
                "properties": {"sheetId": 222, "title": "CWL 2026-07"},
                "fields": "title",
            },
        },
        {
            "updateSheetProperties": {
                "properties": {"sheetId": 444, "title": "CWL"},
                "fields": "title",
            },
        },
    ]
