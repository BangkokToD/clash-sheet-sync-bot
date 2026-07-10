"""Unit-тесты planning и block contracts синхронизации состава."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fakes import (
    FakeSheetsClient,
    RecordingCompositionRepository,
    RecordingSheetBlockRepository,
    make_composition_state,
    make_runtime_config,
    make_sheet_block,
    make_tracked_clan,
)

from clash_sheet_sync_bot.sync.composition import (
    CompositionDiffItem,
    CompositionImportResult,
    CurrentClanMember,
    ImportedPlayerValues,
    PlannedPlayerState,
    PreparedCompositionSync,
    _plan_player_states,
    apply_prepared_composition_sync,
    build_composition_blocks,
)

DETECTED_AT = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)
DETECTED_AT_TEXT = "2026-07-09T12:00:00+00:00"


def _member(
    *,
    player_tag: str,
    nickname: str = "Player",
    town_hall: int = 15,
    clan_tag: str = "#AAA111",
    clan_name: str = "Alpha",
) -> CurrentClanMember:
    """Создаёт CurrentClanMember для planning tests."""

    return CurrentClanMember(
        player_tag=player_tag,
        nickname=nickname,
        town_hall=town_hall,
        clan_tag=clan_tag,
        clan_name=clan_name,
    )


def test_plan_player_states_marks_new_player_active() -> None:
    """Проверяет, что новый игрок становится active."""

    runtime_config = make_runtime_config()
    planned, diff_items = _plan_player_states(
        runtime_config=runtime_config,
        existing_state=(),
        imported=CompositionImportResult(players={}, warnings=()),
        current_members={
            "#P1": _member(player_tag="#P1", nickname="Newbie"),
        },
        detected_at=DETECTED_AT,
    )

    assert planned["#P1"].status == "active"
    assert planned["#P1"].clan_tag == "#AAA111"
    assert planned["#P1"].town_hall == 15
    assert planned["#P1"].nickname == "Newbie"
    assert planned["#P1"].exited_at is None
    assert planned["#P1"].last_seen_at == DETECTED_AT_TEXT
    assert [item.kind for item in diff_items] == ["added"]


def test_plan_player_states_marks_missing_active_player_exited() -> None:
    """Проверяет, что active игрок из отслеживаемого клана становится exited."""

    runtime_config = make_runtime_config()
    previous = make_composition_state(
        player_tag="#P1",
        status="active",
        clan_tag="#AAA111",
        nickname="Old Player",
        user_values={"note": "manual"},
    )

    planned, diff_items = _plan_player_states(
        runtime_config=runtime_config,
        existing_state=(previous,),
        imported=CompositionImportResult(players={}, warnings=()),
        current_members={},
        detected_at=DETECTED_AT,
    )

    assert planned["#P1"].status == "exited"
    assert planned["#P1"].clan_tag is None
    assert planned["#P1"].exited_at == DETECTED_AT_TEXT
    assert planned["#P1"].user_values == {"note": "manual"}
    assert [item.kind for item in diff_items] == ["exited"]


def test_plan_player_states_marks_returned_player_active() -> None:
    """Проверяет, что exited игрок при возвращении снова становится active."""

    runtime_config = make_runtime_config()
    previous = make_composition_state(
        player_tag="#P1",
        status="exited",
        clan_tag=None,
        nickname="Returned",
        exited_at="2026-07-01T00:00:00+00:00",
        user_values={"note": "saved"},
    )

    planned, diff_items = _plan_player_states(
        runtime_config=runtime_config,
        existing_state=(previous,),
        imported=CompositionImportResult(players={}, warnings=()),
        current_members={
            "#P1": _member(player_tag="#P1", nickname="Returned"),
        },
        detected_at=DETECTED_AT,
    )

    assert planned["#P1"].status == "active"
    assert planned["#P1"].clan_tag == "#AAA111"
    assert planned["#P1"].exited_at is None
    assert planned["#P1"].user_values == {"note": "saved"}
    assert [item.kind for item in diff_items] == ["returned"]


def test_plan_player_states_marks_removed_clan_player_untracked() -> None:
    """Проверяет, что игрок удалённого из отслеживания клана становится untracked."""

    runtime_config = make_runtime_config()
    previous = make_composition_state(
        player_tag="#P1",
        status="active",
        clan_tag="#OLD999",
        nickname="Old Clan Player",
        user_values={"note": "keep"},
    )

    planned, diff_items = _plan_player_states(
        runtime_config=runtime_config,
        existing_state=(previous,),
        imported=CompositionImportResult(players={}, warnings=()),
        current_members={},
        detected_at=DETECTED_AT,
    )

    assert planned["#P1"].status == "untracked"
    assert planned["#P1"].clan_tag == "#OLD999"
    assert planned["#P1"].exited_at is None
    assert planned["#P1"].user_values == {"note": "keep"}
    assert diff_items == []


def test_plan_player_states_prefers_imported_user_values() -> None:
    """Проверяет перенос ручных user-values из текущего листа."""

    runtime_config = make_runtime_config()
    previous = make_composition_state(
        player_tag="#P1",
        status="active",
        clan_tag="#AAA111",
        nickname="Player",
        user_values={"note": "old sqlite"},
    )
    imported = CompositionImportResult(
        players={
            "#P1": ImportedPlayerValues(
                player_tag="#P1",
                is_exited=False,
                clan_tag="#AAA111",
                town_hall=14,
                nickname="Player",
                exited_at=None,
                user_values={"note": "from sheet"},
            ),
        },
        warnings=(),
    )

    planned, _ = _plan_player_states(
        runtime_config=runtime_config,
        existing_state=(previous,),
        imported=imported,
        current_members={
            "#P1": _member(player_tag="#P1", nickname="Player"),
        },
        detected_at=DETECTED_AT,
    )

    assert planned["#P1"].user_values == {"note": "from sheet"}


def test_plan_player_states_preserves_user_values_missing_from_current_profile() -> None:
    """Проверяет, что смена active/exited профиля не стирает отсутствующие колонки."""

    runtime_config = make_runtime_config()
    previous = make_composition_state(
        player_tag="#P1",
        status="exited",
        clan_tag=None,
        nickname="Player",
        exited_at="2026-07-01T00:00:00+00:00",
        user_values={
            "active_username": "@player",
            "exited_reason": "ушёл временно",
        },
    )
    imported = CompositionImportResult(
        players={
            "#P1": ImportedPlayerValues(
                player_tag="#P1",
                is_exited=True,
                clan_tag=None,
                town_hall=15,
                nickname="Player",
                exited_at="2026-07-01T00:00:00+00:00",
                user_values={
                    "exited_reason": "вернётся",
                },
            ),
        },
        warnings=(),
        saw_exited_block=True,
    )

    planned, _ = _plan_player_states(
        runtime_config=runtime_config,
        existing_state=(previous,),
        imported=imported,
        current_members={
            "#P1": _member(player_tag="#P1", nickname="Player"),
        },
        detected_at=DETECTED_AT,
    )

    assert planned["#P1"].status == "active"
    assert planned["#P1"].user_values == {
        "active_username": "@player",
        "exited_reason": "вернётся",
    }


def test_build_composition_blocks_creates_active_and_exited_blocks() -> None:
    """Проверяет построение active blocks и exited block."""

    alpha = make_tracked_clan(tag="#AAA111", name="Alpha", sort_order=10)
    beta = make_tracked_clan(tag="#BBB222", name="Beta", sort_order=20)
    runtime_config = make_runtime_config(active_clans=(alpha, beta))
    planned_states = {
        "#P1": PlannedPlayerState(
            player_tag="#P1",
            status="active",
            clan_tag="#AAA111",
            town_hall=16,
            nickname="Alpha Player",
            exited_at=None,
            user_values={"note": "alpha note"},
            last_seen_at=DETECTED_AT_TEXT,
        ),
        "#P2": PlannedPlayerState(
            player_tag="#P2",
            status="active",
            clan_tag="#BBB222",
            town_hall=15,
            nickname="Beta Player",
            exited_at=None,
            user_values={},
            last_seen_at=DETECTED_AT_TEXT,
        ),
        "#P3": PlannedPlayerState(
            player_tag="#P3",
            status="exited",
            clan_tag=None,
            town_hall=14,
            nickname="Exited Player",
            exited_at=DETECTED_AT_TEXT,
            user_values={"note": "exited note"},
            last_seen_at=None,
        ),
    }

    blocks = build_composition_blocks(
        runtime_config=runtime_config,
        planned_states=planned_states,
    )

    assert [block.block.block_key for block in blocks] == [
        "composition_active:#AAA111",
        "composition_active:#BBB222",
        "composition_exited",
    ]

    active_block = blocks[0]
    exited_block = blocks[-1]

    assert active_block.block.start_cell == "A1"
    assert active_block.values[1] == ["__bot_key", "№", "Тег", "Ратуша", "Никнейм", "Заметка"]
    assert active_block.values[2] == [
        "composition_player:#P1",
        1,
        "#P1",
        16,
        "Alpha Player",
        "alpha note",
    ]

    assert exited_block.block.start_cell == "H1"
    assert exited_block.values[1] == [
        "__bot_key",
        "№",
        "Тег",
        "Ратуша",
        "Никнейм",
        "Заметка",
        "Дата выхода",
    ]
    assert exited_block.values[2] == [
        "composition_player:#P3",
        1,
        "#P3",
        14,
        "Exited Player",
        "exited note",
        DETECTED_AT_TEXT,
    ]


@pytest.mark.asyncio
async def test_apply_prepared_composition_sync_replaces_sheet_blocks_instead_of_upsert() -> None:
    """Проверяет замену composition sheet_blocks через replace_blocks_by_prefixes."""

    runtime_config = make_runtime_config()
    planned_states = {
        "#P1": PlannedPlayerState(
            player_tag="#P1",
            status="active",
            clan_tag="#AAA111",
            town_hall=15,
            nickname="Player",
            exited_at=None,
            user_values={"note": "manual"},
            last_seen_at=DETECTED_AT_TEXT,
        ),
    }
    built_blocks = build_composition_blocks(
        runtime_config=runtime_config,
        planned_states=planned_states,
    )
    prepared = PreparedCompositionSync(
        planned_states=planned_states,
        built_blocks=built_blocks,
        active_counts=(("Alpha", 1),),
        exited_count=0,
        diff_items=(CompositionDiffItem(kind="added", message="Новый игрок."),),
        warnings=(),
    )

    sheets_client = FakeSheetsClient()
    composition_repository = RecordingCompositionRepository()
    sheet_block_repository = RecordingSheetBlockRepository(
        blocks=(
            make_sheet_block(
                block_key="composition_active:#OLD",
                start_cell="A1",
            ),
            make_sheet_block(
                block_key="composition_exited",
                start_cell="H1",
            ),
        ),
        fail_on_upsert=True,
    )

    await apply_prepared_composition_sync(
        runtime_config=runtime_config,
        sheets_client=sheets_client,  # type: ignore[arg-type]
        composition_repository=composition_repository,  # type: ignore[arg-type]
        sheet_block_repository=sheet_block_repository,  # type: ignore[arg-type]
        detected_at=DETECTED_AT,
        prepared=prepared,
    )

    assert len(composition_repository.upserted_players) == 1
    assert composition_repository.upserted_players[0]["player_tag"] == "#P1"
    assert composition_repository.upserted_players[0]["status"] == "active"

    assert sheet_block_repository.upsert_calls == []
    assert len(sheet_block_repository.replace_calls) == 1

    replace_call = sheet_block_repository.replace_calls[0]

    assert replace_call["chat_id"] == runtime_config.chat_id
    assert replace_call["sheet_name"] == "Состав"
    assert replace_call["block_key_prefixes"] == ("composition_active:", "composition_exited")
    assert [block.block_key for block in replace_call["blocks"]] == [
        "composition_active:#AAA111",
        "composition_exited",
    ]

    assert sheets_client.batch_value_updates
    assert sheets_client.spreadsheet_requests
    assert sheets_client.hidden_dimensions
