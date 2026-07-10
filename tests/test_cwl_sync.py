"""Unit-тесты CWL season, row key и concurrency helpers."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from clash_sheet_sync_bot.coc.client import ClashApiUnavailableError
from clash_sheet_sync_bot.models import ColumnProfile, TrackedClan
from clash_sheet_sync_bot.sync.cwl import (
    CwlDataError,
    CwlImportResult,
    CwlPlannedRow,
    CwlSeasonMismatchError,
    CwlTechnicalValues,
    _apply_user_values,
    _cwl_composition_user_column_links,
    _load_cwl_wars,
    _resolve_cwl_season,
    make_cwl_row_key,
)

JsonObject = dict[str, Any]


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
