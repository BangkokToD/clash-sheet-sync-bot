"""Unit-тесты чистого домена прогноза ЛВК."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from clash_sheet_sync_bot.cwl_forecast import (
    CwlForecastDataError,
    CwlScheduleError,
    build_predicted_roster,
    extract_known_opponents,
    league_group_fingerprint,
    parse_cwl_war,
    parse_league_group,
    select_current_war,
    validate_schedule,
)
from clash_sheet_sync_bot.cwl_forecast.domain import actual_roster
from clash_sheet_sync_bot.cwl_forecast.models import CreatedWar

FIXTURE = Path("tests/fixtures/current_war_league_group.json")


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _member(tag: str, th: int, position: int) -> dict[str, object]:
    return {"tag": tag, "name": tag, "townHallLevel": th, "mapPosition": position}


def _war(
    *,
    state: str,
    own_home: bool = True,
    start: str = "20260805T120000.000Z",
    opponent_tag: str = "#CLAN002",
) -> dict[str, object]:
    own = {
        "tag": "#CLAN001",
        "name": "Own",
        "members": [_member("#P2", 16, 2), _member("#P1", 17, 1)],
    }
    enemy = {
        "tag": opponent_tag,
        "name": "Enemy",
        "members": [_member("#E2", 14, 2), _member("#E1", 15, 1)],
    }
    return {
        "state": state,
        "teamSize": 2,
        "startTime": start,
        "clan": own if own_home else enemy,
        "opponent": enemy if own_home else own,
    }


def _created(round_number: int, war_tag: str, **kwargs: object) -> CreatedWar:
    return CreatedWar(round_number, war_tag, parse_cwl_war(_war(**kwargs)))


def test_real_fixture_parses_eight_clans_seven_rounds_and_zero_tags() -> None:
    group = parse_league_group(_fixture())

    assert group.season == "2026-08-01"
    assert len(group.clans) == 8
    assert len(group.rounds) == 7
    assert group.rounds[4].war_tags == ("#0", "#0", "#0", "#0")


def test_fingerprint_ignores_clan_order_names_and_levels_but_not_tags() -> None:
    data = _fixture()
    original = parse_league_group(data)
    changed = deepcopy(data)
    clans = changed["clans"]
    assert isinstance(clans, list)
    clans.reverse()
    assert isinstance(clans[0], dict)
    clans[0]["name"] = "Renamed"
    clans[0]["clanLevel"] = 99
    assert league_group_fingerprint(parse_league_group(changed)) == league_group_fingerprint(
        original
    )

    clans[0]["tag"] = "#CHANGED"
    assert league_group_fingerprint(parse_league_group(changed)) != league_group_fingerprint(
        original
    )


@pytest.mark.parametrize(
    "mutation",
    (
        lambda item: item.update(townHallLevel=True),
        lambda item: item.update(townHallLevel=19),
        lambda item: item.update(tag="bad"),
    ),
)
def test_league_parser_rejects_invalid_member_fields(mutation: object) -> None:
    data = _fixture()
    clans = data["clans"]
    assert isinstance(clans, list) and isinstance(clans[0], dict)
    members = clans[0]["members"]
    assert isinstance(members, list) and isinstance(members[0], dict)
    mutation(members[0])  # type: ignore[operator]

    with pytest.raises(CwlForecastDataError):
        parse_league_group(data)


def test_select_inwar_over_preparation_and_home_away_independent() -> None:
    selected = select_current_war(
        own_clan_tag="#clan001",
        created_wars=(
            _created(2, "#WAR2", state="preparation", start="20260805T100000.000Z"),
            _created(1, "#WAR1", state="inWar", own_home=False),
        ),
    )

    assert selected is not None
    assert selected.war_tag == "#WAR1"
    assert selected.own_clan.tag == "#CLAN001"
    assert selected.opponent_clan.tag == "#CLAN002"


def test_nearest_preparation_uses_time_then_round_then_tag() -> None:
    selected = select_current_war(
        own_clan_tag="#CLAN001",
        created_wars=(
            _created(3, "#WAR3", state="preparation", start="20260805T130000.000Z"),
            _created(2, "#WARZ", state="preparation", start="20260805T120000.000Z"),
            _created(2, "#WARA", state="preparation", start="20260805T120000.000Z"),
            _created(1, "#ENDED", state="warEnded"),
        ),
    )

    assert selected is not None and selected.war_tag == "#WARA"


def test_actual_roster_sorts_map_position_and_prediction_sorts_th_then_tag() -> None:
    war = parse_cwl_war(_war(state="inWar"))
    group = parse_league_group(_fixture())
    clan = group.clan("#CLAN001")

    assert actual_roster(war.clan, 2) == (17, 16)
    expected = tuple(
        member.town_hall_level
        for member in sorted(
            clan.members, key=lambda member: (-member.town_hall_level, member.tag)
        )[:3]
    )
    assert build_predicted_roster(clan, 3) == expected


def test_known_opponents_extracts_only_our_pairs() -> None:
    group = parse_league_group(_fixture())
    other_war = parse_cwl_war(
        {
            **_war(state="warEnded"),
            "clan": {"tag": "#CLAN003", "name": "3", "members": []},
            "opponent": {"tag": "#CLAN004", "name": "4", "members": []},
        }
    )
    pairs = extract_known_opponents(
        group=group,
        own_clan_tag="#CLAN001",
        created_wars=(
            CreatedWar(1, "#WAR001", other_war),
            _created(2, "#WAR005", state="inWar"),
        ),
    )

    assert pairs == {2: "#CLAN002"}


def test_schedule_validates_full_rounds_and_returns_all_future() -> None:
    group = parse_league_group(_fixture())
    schedule = {
        number: tag
        for number, tag in enumerate(
            ("#CLAN008", "#CLAN002", "#CLAN003", "#CLAN004", "#CLAN005", "#CLAN006", "#CLAN007"),
            start=1,
        )
    }

    future = validate_schedule(
        group=group,
        own_clan_tag="#CLAN001",
        selected_round=4,
        known_opponents={2: "#CLAN002"},
        saved_opponents=schedule,
    )

    assert future == {5: "#CLAN005", 6: "#CLAN006", 7: "#CLAN007"}


@pytest.mark.parametrize("kind", ("missing", "duplicate", "foreign", "own", "conflict"))
def test_schedule_rejects_invalid_variants(kind: str) -> None:
    group = parse_league_group(_fixture())
    schedule = {
        number: tag
        for number, tag in enumerate(
            ("#CLAN008", "#CLAN002", "#CLAN003", "#CLAN004", "#CLAN005", "#CLAN006", "#CLAN007"),
            start=1,
        )
    }
    if kind == "missing":
        schedule.pop(7)
    elif kind == "duplicate":
        schedule[7] = schedule[6]
    elif kind == "foreign":
        schedule[7] = "#FOREIGN"
    elif kind == "own":
        schedule[7] = "#CLAN001"
    elif kind == "conflict":
        schedule[2], schedule[3] = schedule[3], schedule[2]

    with pytest.raises(CwlScheduleError):
        validate_schedule(
            group=group,
            own_clan_tag="#CLAN001",
            selected_round=4,
            known_opponents={2: "#CLAN002"},
            saved_opponents=schedule,
        )


def test_last_round_needs_no_saved_schedule() -> None:
    group = parse_league_group(_fixture())
    assert (
        validate_schedule(
            group=group,
            own_clan_tag="#CLAN001",
            selected_round=7,
            known_opponents={},
            saved_opponents=None,
        )
        == {}
    )


def test_api_time_is_timezone_aware() -> None:
    war = parse_cwl_war(_war(state="inWar"))
    assert war.start_time == datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
