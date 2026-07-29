"""Contract-тесты Raid API, parser и доменной формулы."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from clash_sheet_sync_bot.coc.client import ClashApiUnavailableError, ClashClient
from clash_sheet_sync_bot.repositories import RaidDataError, RaidPlayerState
from clash_sheet_sync_bot.sheets.column_profiles import default_columns
from clash_sheet_sync_bot.sync.composition import PlannedPlayerState
from clash_sheet_sync_bot.sync.raids import (
    CAPITAL_PEAK_DISTRICT_ID,
    RaidContractError,
    RaidRetryableDataError,
    RaidTechnicalValues,
    aggregate_raid_season,
    classify_raid_district,
    parse_raid_season,
    prepare_public_raid_sync,
)
from tests.fakes.factories import (
    make_app_config,
    make_column_profile,
    make_runtime_config,
    make_sheet_binding,
    make_sheet_block,
    make_tracked_clan,
)
from tests.fakes.sheets import FakeSheetsClient, RecordingSheetBlockRepository

JsonObject = dict[str, Any]
FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _member(
    tag: str,
    *,
    attacks: int,
    name: str = "Player",
    attack_limit: int = 5,
    bonus_attack_limit: int = 1,
    capital_resources_looted: int = 1000,
) -> JsonObject:
    """Создаёт member object минимального raid-контракта."""

    return {
        "tag": tag,
        "name": name,
        "attacks": attacks,
        "attackLimit": attack_limit,
        "bonusAttackLimit": bonus_attack_limit,
        "capitalResourcesLooted": capital_resources_looted,
    }


def _attack(tag: str, destruction_percent: int) -> JsonObject:
    """Создаёт attack object минимального raid-контракта."""

    return {
        "attacker": {"tag": tag},
        "destructionPercent": destruction_percent,
    }


def _district(
    district_id: int,
    attacks: list[JsonObject],
    *,
    name: str = "Ordinary District",
) -> JsonObject:
    """Создаёт district object минимального raid-контракта."""

    return {
        "id": district_id,
        "name": name,
        "attackCount": len(attacks),
        "attacks": attacks,
    }


def _season(
    *,
    members: list[JsonObject],
    districts: list[JsonObject],
    state: str = "ended",
    start_time: str = "20260724T070000.000Z",
    end_time: str = "20260727T070000.000Z",
) -> JsonObject:
    """Создаёт season object минимального raid-контракта."""

    return {
        "state": state,
        "startTime": start_time,
        "endTime": end_time,
        "members": members,
        "attackLog": [{"districts": districts}],
    }


def _aggregate(
    payload: JsonObject,
    *,
    attacks_target: int = 6,
    normal_norm: int = 2,
    capital_norm: int = 3,
) -> tuple[RaidTechnicalValues, ...]:
    """Разбирает и агрегирует тестовый сезон."""

    parsed = parse_raid_season(payload, clan_tag="#CLAN")
    return aggregate_raid_season(
        parsed,
        attacks_target=attacks_target,
        normal_district_attack_norm=normal_norm,
        capital_district_attack_norm=capital_norm,
    )


@pytest.mark.asyncio
async def test_get_capital_raid_seasons_encodes_tag_and_sends_limit() -> None:
    """Проверяет URL encoding тега и query-параметр limit."""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"items": [{"state": "ended"}, {"state": "ended"}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = ClashClient("token", http_client)
        seasons = await client.get_capital_raid_seasons(" #abc123 ", limit=5)

    assert len(seasons) == 2
    assert len(requests) == 1
    assert "%23ABC123" in str(requests[0].url)
    assert requests[0].url.params["limit"] == "5"


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", (0, -1, True, 1.5))
async def test_get_capital_raid_seasons_rejects_invalid_limit_before_request(
    limit: Any,
) -> None:
    """Проверяет строгую валидацию limit до HTTP-вызова."""

    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"items": []}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = ClashClient("token", http_client)
        with pytest.raises(ValueError, match="limit"):
            await client.get_capital_raid_seasons("#ABC123", limit=limit)

    assert called is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"items": None},
        {"items": {}},
        {"items": ["not-an-object"]},
    ),
)
async def test_get_capital_raid_seasons_rejects_invalid_items(payload: JsonObject) -> None:
    """Проверяет envelope и тип каждого season object."""

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = ClashClient("token", http_client)
        with pytest.raises(ClashApiUnavailableError, match=r"items|season"):
            await client.get_capital_raid_seasons("#ABC123", limit=5)


@pytest.mark.asyncio
async def test_get_capital_raid_seasons_maps_network_error() -> None:
    """Проверяет преобразование сетевой ошибки."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network unavailable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = ClashClient("token", http_client)
        with pytest.raises(ClashApiUnavailableError, match="network"):
            await client.get_capital_raid_seasons("#ABC123", limit=5)


@pytest.mark.asyncio
async def test_get_capital_raid_seasons_maps_http_error() -> None:
    """Проверяет преобразование HTTP-ошибки."""

    transport = httpx.MockTransport(lambda request: httpx.Response(503, json={}))
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = ClashClient("token", http_client)
        with pytest.raises(ClashApiUnavailableError, match="HTTP 503"):
            await client.get_capital_raid_seasons("#ABC123", limit=5)


@pytest.mark.asyncio
async def test_get_capital_raid_seasons_maps_invalid_json() -> None:
    """Проверяет преобразование ошибки JSON."""

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            content=b"{",
            headers={"content-type": "application/json"},
        ),
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = ClashClient("token", http_client)
        with pytest.raises(ClashApiUnavailableError, match="JSON"):
            await client.get_capital_raid_seasons("#ABC123", limit=5)


@pytest.mark.parametrize("field", ("state", "startTime", "endTime", "members", "attackLog"))
def test_parse_raid_season_requires_season_fields(field: str) -> None:
    """Проверяет обязательные поля используемого сезона."""

    payload = _season(members=[], districts=[])
    payload.pop(field)

    with pytest.raises(RaidContractError, match=field):
        parse_raid_season(payload, clan_tag="#CLAN")


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("tag", None),
        ("name", None),
        ("attacks", True),
        ("attackLimit", "5"),
        ("bonusAttackLimit", -1),
        ("capitalResourcesLooted", False),
    ),
)
def test_parse_raid_season_rejects_malformed_member(field: str, value: Any) -> None:
    """Проверяет строгий контракт участника и запрет bool как int."""

    member = _member("#PLAYER", attacks=0)
    member[field] = value
    payload = _season(members=[member], districts=[])

    with pytest.raises(RaidContractError, match=field):
        parse_raid_season(payload, clan_tag="#CLAN")


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("id", None),
        ("id", True),
        ("attacks", None),
    ),
)
def test_parse_raid_season_rejects_malformed_district(
    field: str,
    replacement: Any,
) -> None:
    """Проверяет обязательные поля использованного района."""

    district = _district(123, [_attack("#PLAYER", 100)])
    if replacement is None:
        district.pop(field)
    else:
        district[field] = replacement
    payload = _season(members=[_member("#PLAYER", attacks=1)], districts=[district])

    with pytest.raises(RaidContractError, match=field):
        parse_raid_season(payload, clan_tag="#CLAN")


def test_parse_raid_season_accepts_zero_attack_district_without_attacks() -> None:
    """Проверяет реальное представление района без выполненных атак."""

    district: JsonObject = {
        "id": CAPITAL_PEAK_DISTRICT_ID,
        "name": "Capital Peak",
        "attackCount": 0,
    }
    payload = _season(members=[], districts=[district])

    parsed = parse_raid_season(payload, clan_tag="#CLAN")

    assert parsed.attacks == ()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("attacker", None),
        ("destructionPercent", True),
        ("destructionPercent", -1),
        ("destructionPercent", 101),
    ),
)
def test_parse_raid_season_rejects_malformed_attack(field: str, value: Any) -> None:
    """Проверяет обязательные поля атаки и диапазон destruction."""

    attack = _attack("#PLAYER", 100)
    attack[field] = value
    payload = _season(
        members=[_member("#PLAYER", attacks=1)],
        districts=[_district(123, [attack])],
    )

    with pytest.raises(RaidContractError, match=field):
        parse_raid_season(payload, clan_tag="#CLAN")


def test_parse_raid_season_normalizes_clan_and_player_tags() -> None:
    """Проверяет существующую нормализацию clan/player tags."""

    payload = _season(
        members=[_member(" #player ", attacks=1)],
        districts=[_district(123, [_attack(" #player ", 100)])],
    )

    parsed = parse_raid_season(payload, clan_tag=" #clan ")

    assert parsed.clan_tag == "#CLAN"
    assert parsed.members[0].player_tag == "#PLAYER"
    assert parsed.attacks[0].player_tag == "#PLAYER"


def test_classify_raid_district_uses_confirmed_id() -> None:
    """Проверяет ID-based классификацию Capital Peak и обычного района."""

    assert classify_raid_district(CAPITAL_PEAK_DISTRICT_ID, "Локализованное имя") == "capital"
    assert classify_raid_district(987654321, "Arbitrary District") == "normal"


def test_classify_raid_district_rejects_capital_peak_name_with_other_id() -> None:
    """Проверяет конфликт имени Capital Peak с подтверждённым ID."""

    with pytest.raises(RaidContractError, match="70000000"):
        classify_raid_district(987654321, "Capital Peak")


def test_parse_raid_season_rejects_ended_attack_counter_mismatch() -> None:
    """Проверяет strict error для рассинхронизации ended-сезона."""

    payload = _season(
        members=[_member("#PLAYER", attacks=2)],
        districts=[_district(123, [_attack("#PLAYER", 100)])],
    )

    with pytest.raises(RaidContractError, match="не совпадает"):
        parse_raid_season(payload, clan_tag="#CLAN")


def test_parse_raid_season_returns_retryable_ongoing_attack_counter_mismatch() -> None:
    """Проверяет retryable error для временной рассинхронизации ongoing."""

    payload = _season(
        state="ongoing",
        members=[_member("#PLAYER", attacks=2)],
        districts=[_district(123, [_attack("#PLAYER", 100)])],
    )

    with pytest.raises(RaidRetryableDataError, match="Повторите /sync позже"):
        parse_raid_season(payload, clan_tag="#CLAN")


def test_normal_district_33_and_67_give_two_normal_points() -> None:
    """Проверяет формулу обычного района без float drift."""

    rows = _aggregate(
        _season(
            members=[_member("#PLAYER", attacks=2)],
            districts=[_district(123, [_attack("#PLAYER", 33), _attack("#PLAYER", 67)])],
        ),
    )

    assert rows[0].weighted_damage_units == 200
    assert rows[0].normal_points == Decimal("2")
    assert isinstance(rows[0].normal_points, Decimal)


def test_capital_peak_40_35_and_25_give_three_normal_points() -> None:
    """Проверяет формулу Capital Peak по подтверждённому ID."""

    rows = _aggregate(
        _season(
            members=[_member("#PLAYER", attacks=3)],
            districts=[
                _district(
                    CAPITAL_PEAK_DISTRICT_ID,
                    [
                        _attack("#PLAYER", 40),
                        _attack("#PLAYER", 35),
                        _attack("#PLAYER", 25),
                    ],
                    name="Capital Peak",
                ),
            ],
        ),
    )

    assert rows[0].weighted_damage_units == 300
    assert rows[0].normal_points == Decimal("3")


def test_six_normative_attacks_give_coefficient_one() -> None:
    """Проверяет идеальный ориентир шести нормативных атак."""

    attacks = [_attack("#PLAYER", 50) for _ in range(6)]
    rows = _aggregate(
        _season(
            members=[_member("#PLAYER", attacks=6)],
            districts=[_district(123, attacks)],
        ),
    )

    assert rows[0].weighted_damage_units == 600
    assert rows[0].coefficient == Decimal("1")


def test_five_normative_attacks_display_as_point_83() -> None:
    """Проверяет отображаемое округление коэффициента пяти атак."""

    attacks = [_attack("#PLAYER", 50) for _ in range(5)]
    rows = _aggregate(
        _season(
            members=[_member("#PLAYER", attacks=5)],
            districts=[_district(123, attacks)],
        ),
    )

    assert rows[0].weighted_damage_units == 500
    assert f"{rows[0].coefficient:.2f}" == "0.83"


def test_aggregation_handles_multiple_players_and_district_kinds() -> None:
    """Проверяет независимый вклад игроков в нескольких районах."""

    payload = _season(
        members=[
            _member("#ONE", attacks=2, name="One"),
            _member("#TWO", attacks=2, name="Two"),
        ],
        districts=[
            _district(
                123,
                [_attack("#ONE", 100), _attack("#TWO", 50)],
            ),
            _district(
                CAPITAL_PEAK_DISTRICT_ID,
                [_attack("#ONE", 50), _attack("#TWO", 100)],
                name="Capital Peak",
            ),
        ],
    )

    rows = _aggregate(payload)

    assert [row.player_tag for row in rows] == ["#ONE", "#TWO"]
    assert rows[0].weighted_damage_units == 350
    assert rows[1].weighted_damage_units == 400


def test_coefficient_may_exceed_one_and_zero_attack_has_zero_points() -> None:
    """Проверяет сильные атаки и участника с нулевым вкладом."""

    payload = _season(
        members=[
            _member("#STRONG", attacks=6),
            _member("#ZERO", attacks=1),
        ],
        districts=[
            _district(
                123,
                [_attack("#STRONG", 100) for _ in range(6)] + [_attack("#ZERO", 0)],
            ),
        ],
    )

    strong, zero = _aggregate(payload)

    assert strong.weighted_damage_units == 1200
    assert strong.coefficient == Decimal("2")
    assert zero.weighted_damage_units == 0
    assert zero.normal_points == Decimal("0")


def test_one_strong_attack_has_only_its_weighted_contribution() -> None:
    """Проверяет отсутствие отдельного бонуса за сильную атаку или добивку."""

    payload = _season(
        members=[_member("#PLAYER", attacks=1)],
        districts=[
            _district(
                CAPITAL_PEAK_DISTRICT_ID,
                [_attack("#PLAYER", 100)],
                name="Capital Peak",
            ),
        ],
    )

    rows = _aggregate(payload)

    assert rows[0].weighted_damage_units == 300
    assert rows[0].normal_points == Decimal("3")
    assert rows[0].coefficient == Decimal("0.5")


def test_aggregate_raid_season_uses_supplied_config_values() -> None:
    """Проверяет отсутствие бизнес-литералов в расчёте."""

    payload = _season(
        members=[_member("#PLAYER", attacks=1)],
        districts=[_district(123, [_attack("#PLAYER", 25)])],
    )

    rows = _aggregate(payload, attacks_target=8, normal_norm=4, capital_norm=5)

    assert rows[0].weighted_damage_units == 100
    assert rows[0].normal_points == Decimal("1")
    assert rows[0].coefficient == Decimal("0.125")


@pytest.mark.parametrize(
    ("attacks_target", "normal_norm", "capital_norm"),
    (
        (0, 2, 3),
        (6, True, 3),
        (6, 2, -1),
    ),
)
def test_aggregate_raid_season_rejects_invalid_formula_config(
    attacks_target: Any,
    normal_norm: Any,
    capital_norm: Any,
) -> None:
    """Проверяет строгие положительные параметры доменной формулы."""

    parsed = parse_raid_season(_season(members=[], districts=[]), clan_tag="#CLAN")

    with pytest.raises(ValueError, match="положительным"):
        aggregate_raid_season(
            parsed,
            attacks_target=attacks_target,
            normal_district_attack_norm=normal_norm,
            capital_district_attack_norm=capital_norm,
        )


@pytest.mark.asyncio
async def test_real_fixture_envelope_and_first_complete_season() -> None:
    """Проверяет committed fixture целиком и полный первый season object."""

    fixture_path = FIXTURE_DIR / "capital_raid_seasons.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))

    async with httpx.AsyncClient(transport=transport) as http_client:
        client = ClashClient("token", http_client)
        seasons = await client.get_capital_raid_seasons("#CLAN", limit=5)

    assert len(seasons) == 5
    assert {season["state"] for season in seasons} == {"ended"}

    parsed = parse_raid_season(seasons[0], clan_tag="#CLAN")
    rows = aggregate_raid_season(
        parsed,
        attacks_target=6,
        normal_district_attack_norm=2,
        capital_district_attack_norm=3,
    )

    assert len(parsed.members) == 50
    assert len(parsed.attacks) == 299
    assert sum(member.attacks for member in parsed.members) == 299
    assert sum(row.attacks for row in rows) == 299
    assert any(attack.district_kind == "capital" for attack in parsed.attacks)
    assert any(member.bonus_attack_limit == 1 for member in parsed.members)


def test_synthetic_ongoing_overlay_changes_only_state() -> None:
    """Проверяет committed synthetic overlay поверх реального season object."""

    fixture = json.loads(
        (FIXTURE_DIR / "capital_raid_seasons.json").read_text(encoding="utf-8"),
    )
    overlay = json.loads(
        (FIXTURE_DIR / "capital_raid_seasons_ongoing.synthetic.json").read_text(
            encoding="utf-8",
        ),
    )
    base = fixture["items"][overlay["_base_item_index"]]
    ongoing = deepcopy(base)
    ongoing.update(overlay["replace"])

    assert overlay == {
        "_fixture_type": "synthetic_overlay",
        "_base_fixture": "capital_raid_seasons.json",
        "_base_item_index": 0,
        "replace": {"state": "ongoing"},
    }
    assert {key for key in ongoing if ongoing[key] != base[key]} == {"state"}
    assert parse_raid_season(ongoing, clan_tag="#CLAN").state == "ongoing"


class FakeRaidClash:
    """Настраиваемое read-only окно raid seasons."""

    def __init__(self, items_by_clan: dict[str, list[JsonObject]]) -> None:
        self.items_by_clan = items_by_clan
        self.calls: list[tuple[str, int]] = []

    async def get_capital_raid_seasons(
        self,
        clan_tag: str,
        *,
        limit: int,
    ) -> list[JsonObject]:
        self.calls.append((clan_tag, limit))
        return self.items_by_clan.get(clan_tag, [])


def _raid_profiles(*, include_user: bool = False) -> tuple[Any, ...]:
    profiles = [
        make_column_profile(
            table_type=item.table_type,
            column_key=item.column_key,
            title=item.title,
            visible=item.visible,
            kind=item.kind,
            value_type=item.value_type,
            sort_order=item.sort_order,
        )
        for item in default_columns("raids")
    ]
    if include_user:
        profiles.extend(
            (
                make_column_profile(
                    table_type="raids",
                    column_key="raid_note",
                    title=" ЗАМЕТКА ",
                    visible=True,
                    kind="user",
                    value_type="string",
                    sort_order=80,
                ),
                make_column_profile(
                    table_type="raids",
                    column_key="raid_hidden",
                    title="Скрытая",
                    visible=False,
                    kind="user",
                    value_type="string",
                    sort_order=90,
                    is_active=False,
                ),
                make_column_profile(
                    table_type="composition_active",
                    column_key="composition_note",
                    title="заметка",
                    visible=True,
                    kind="user",
                    value_type="string",
                    sort_order=80,
                ),
            )
        )
    return tuple(profiles)


def _runtime(
    *,
    clans: tuple[Any, ...] | None = None,
    active_raid_season: str | None = None,
    profiles: tuple[Any, ...] | None = None,
) -> Any:
    binding = make_sheet_binding(
        active_raid_sheet_name="Рейды",
        active_raid_sheet_id=444,
        active_raid_season=active_raid_season,
    )
    return make_runtime_config(
        sheet_binding=binding,
        active_clans=clans,
        column_profiles=profiles or _raid_profiles(),
    )


def _saved_raid_state(
    *,
    chat_id: int = -1001,
    season_key: str = "2026-07-24T07:00:00+00:00",
    season_end_at: str = "2026-07-27T07:00:00+00:00",
    season_state: str = "ended",
    clan_tag: str = "#CLAN",
    player_tag: str = "#PLAYER",
    player_name: str = "Player",
    weighted_damage_units: int = 200,
    user_values: dict[str, str] | None = None,
) -> RaidPlayerState:
    return RaidPlayerState(
        chat_id=chat_id,
        season_key=season_key,
        season_start_at=season_key,
        season_end_at=season_end_at,
        season_state=season_state,
        row_key=f"raid_row:{season_key}|{clan_tag}|{player_tag}",
        clan_tag=clan_tag,
        player_tag=player_tag,
        technical_values={
            "player_name": player_name,
            "attacks": 1,
            "attack_limit": 5,
            "bonus_attack_limit": 1,
            "capital_resources_looted": 1000,
            "weighted_damage_units": weighted_damage_units,
            "normal_points": Decimal(weighted_damage_units) / Decimal(100),
            "coefficient": Decimal(weighted_damage_units) / Decimal(600),
        },
        user_values=user_values or {},
        row_hash=None,
        updated_at="2026-07-28T00:00:00+00:00",
    )


async def _prepare(
    *,
    runtime: Any,
    clash: FakeRaidClash,
    sheets: FakeSheetsClient | None = None,
    blocks: RecordingSheetBlockRepository | None = None,
    saved_rows: tuple[RaidPlayerState, ...] = (),
    composition: tuple[PlannedPlayerState, ...] = (),
) -> Any:
    return await prepare_public_raid_sync(
        runtime_config=runtime,
        clash_client=clash,
        sheets_client=sheets or FakeSheetsClient(),
        sheet_block_repository=blocks or RecordingSheetBlockRepository(),
        config=make_app_config(),
        saved_rows=saved_rows,
        composition_player_states=composition,
    )


@pytest.mark.asyncio
async def test_prepare_selects_ongoing_members_ranks_ties_and_performs_no_sheet_writes() -> None:
    """Покрывает ongoing, API members, leaver, no-zero row, tie-break и row key."""

    payload = _season(
        state="ongoing",
        members=[
            _member("#TWO", attacks=1, name="same"),
            _member("#ONE", attacks=1, name="Same"),
            _member("#LEFT", attacks=1, name="Left"),
        ],
        districts=[
            _district(
                123,
                [_attack("#ONE", 50), _attack("#TWO", 50), _attack("#LEFT", 25)],
            )
        ],
    )
    sheets = FakeSheetsClient()
    prepared = await _prepare(
        runtime=_runtime(),
        clash=FakeRaidClash({"#AAA111": [payload]}),
        sheets=sheets,
        composition=(
            PlannedPlayerState(
                "#NOT_MEMBER",
                "active",
                "#AAA111",
                16,
                "Not member",
                None,
                {},
                None,
            ),
        ),
    )

    assert prepared.selected_season is not None
    assert prepared.selected_season.state == "ongoing"
    rows = prepared.selected_season.blocks[0].rows
    assert [row.technical_values.player_tag for row in rows] == ["#ONE", "#TWO", "#LEFT"]
    assert [row.rank for row in rows] == [1, 2, 3]
    assert all("#NOT_MEMBER" not in row.row_key for row in rows)
    assert rows[0].row_key == ("raid_row:2026-07-24T07:00:00+00:00|#AAA111|#ONE")
    assert sheets.batch_value_updates == []
    assert sheets.spreadsheet_requests == []
    assert sheets.hidden_dimensions == []


@pytest.mark.asyncio
async def test_prepare_public_raid_sync_selects_season_ranks_and_merges_values() -> None:
    """Сохраняет исходный regression сценарий preparation из коммита 3."""

    payload = _season(
        state="ongoing",
        members=[_member("#TWO", attacks=1, name="Two"), _member("#ONE", attacks=1, name="One")],
        districts=[_district(123, [_attack("#ONE", 100), _attack("#TWO", 50)])],
    )
    prepared = await _prepare(
        runtime=_runtime(profiles=_raid_profiles(include_user=True)),
        clash=FakeRaidClash({"#AAA111": [payload]}),
        composition=(
            PlannedPlayerState(
                "#ONE",
                "active",
                "#AAA111",
                16,
                "One",
                None,
                {"composition_note": "composition"},
                None,
            ),
        ),
    )

    assert prepared.season_state == "ongoing"
    rows = prepared.blocks[0].rows
    assert [row.technical_values.player_tag for row in rows] == ["#ONE", "#TWO"]
    assert [row.rank for row in rows] == [1, 2]
    assert rows[0].user_values == {"raid_note": "composition"}
    assert rows[0].row_key.startswith("raid_row:2026-07-24T07:00:00+00:00|#AAA111|")


@pytest.mark.asyncio
async def test_prepare_same_ongoing_across_clans_and_ongoing_state_has_priority() -> None:
    """Проверяет общий key и детерминированный приоритет ongoing над ended."""

    ongoing = _season(members=[], districts=[], state="ongoing")
    ended = _season(members=[], districts=[], state="ended")
    clans = (
        make_tracked_clan(tag="#ONE", name="One"),
        make_tracked_clan(tag="#TWO", name="Two"),
    )

    prepared = await _prepare(
        runtime=_runtime(clans=clans),
        clash=FakeRaidClash({"#ONE": [ongoing], "#TWO": [ended]}),
    )

    assert prepared.selected_season is not None
    assert prepared.selected_season.state == "ongoing"


@pytest.mark.asyncio
async def test_prepare_public_raid_sync_rejects_different_ongoing_seasons() -> None:
    """Проверяет общий season contract нескольких кланов."""

    first = _season(members=[], districts=[], state="ongoing")
    second = _season(
        members=[],
        districts=[],
        state="ongoing",
        start_time="20260725T070000.000Z",
        end_time="20260728T070000.000Z",
    )
    clans = (make_tracked_clan(tag="#ONE"), make_tracked_clan(tag="#TWO"))

    with pytest.raises(RaidContractError, match="разные ongoing"):
        await _prepare(
            runtime=_runtime(clans=clans),
            clash=FakeRaidClash({"#ONE": [first], "#TWO": [second]}),
        )


@pytest.mark.asyncio
async def test_prepare_selects_newest_ended_without_backfill() -> None:
    """Проверяет newest ended и отсутствие промежуточных planned seasons."""

    oldest = _season(
        members=[],
        districts=[],
        start_time="20260710T070000.000Z",
        end_time="20260713T070000.000Z",
    )
    middle = _season(
        members=[],
        districts=[],
        start_time="20260717T070000.000Z",
        end_time="20260720T070000.000Z",
    )
    newest = _season(members=[], districts=[])

    prepared = await _prepare(
        runtime=_runtime(),
        clash=FakeRaidClash({"#AAA111": [middle, oldest, newest]}),
    )

    assert prepared.selected_season is not None
    assert prepared.selected_season.season_key == "2026-07-24T07:00:00+00:00"
    assert prepared.previous_active_season is None


@pytest.mark.asyncio
async def test_prepare_empty_api_uses_latest_saved_active_clan_and_chat_only() -> None:
    """Проверяет SQLite fallback с фильтром chat и активных clan tags."""

    active = _saved_raid_state(season_key="2026-07-17T07:00:00+00:00", clan_tag="#AAA111")
    inactive = _saved_raid_state(
        season_key="2026-07-24T07:00:00+00:00",
        clan_tag="#INACTIVE",
    )
    other_chat = _saved_raid_state(
        chat_id=-9999,
        season_key="2026-07-31T07:00:00+00:00",
        clan_tag="#AAA111",
    )

    prepared = await _prepare(
        runtime=_runtime(),
        clash=FakeRaidClash({"#AAA111": []}),
        saved_rows=(active, inactive, other_chat),
    )

    assert prepared.selected_season is not None
    assert prepared.selected_season.season_key == active.season_key
    assert prepared.selected_season.blocks[0].rows[0].technical_values.player_tag == "#PLAYER"


@pytest.mark.asyncio
async def test_prepare_without_api_or_saved_state_returns_message_only() -> None:
    """Проверяет отсутствие любых raid-данных."""

    prepared = await _prepare(
        runtime=_runtime(),
        clash=FakeRaidClash({"#AAA111": []}),
    )

    assert prepared.selected_season is None
    assert prepared.blocks[0].rows == ()
    assert prepared.blocks[0].message is not None


@pytest.mark.asyncio
async def test_missing_clan_selected_api_season_gets_period_message_not_sqlite_row() -> None:
    """Проверяет запрет произвольного per-clan SQLite fallback."""

    selected = _season(members=[], districts=[], state="ongoing")
    clans = (
        make_tracked_clan(tag="#ONE", name="One"),
        make_tracked_clan(tag="#TWO", name="Two"),
    )
    stale = _saved_raid_state(clan_tag="#TWO")

    prepared = await _prepare(
        runtime=_runtime(clans=clans),
        clash=FakeRaidClash({"#ONE": [selected], "#TWO": []}),
        saved_rows=(stale,),
    )

    second = prepared.selected_season.blocks[1]
    assert second.rows == ()
    assert "2026-07-24" in (second.message or "")
    assert "2026-07-27" in (second.message or "")


@pytest.mark.asyncio
async def test_prepare_finds_and_finalizes_old_active_from_api_without_backfill() -> None:
    """Проверяет отдельный old active state из загруженного API-окна."""

    old_key = "2026-07-10T07:00:00+00:00"
    old = _season(
        members=[_member("#PLAYER", attacks=1)],
        districts=[_district(123, [_attack("#PLAYER", 100)])],
        start_time="20260710T070000.000Z",
        end_time="20260713T070000.000Z",
    )
    middle = _season(
        members=[],
        districts=[],
        start_time="20260717T070000.000Z",
        end_time="20260720T070000.000Z",
    )
    newest = _season(members=[], districts=[])

    prepared = await _prepare(
        runtime=_runtime(active_raid_season=old_key),
        clash=FakeRaidClash({"#AAA111": [newest, middle, old]}),
    )

    assert prepared.selected_season.season_key == "2026-07-24T07:00:00+00:00"
    assert prepared.previous_active_season is not None
    assert prepared.previous_active_season.season_key == old_key
    assert prepared.previous_active_season.end_time == "2026-07-13T07:00:00+00:00"


@pytest.mark.asyncio
async def test_prepare_restores_missing_old_active_from_sqlite_with_warning() -> None:
    """Проверяет fallback старого active без восстановления промежуточных сезонов."""

    old_key = "2026-07-10T07:00:00+00:00"
    saved = _saved_raid_state(season_key=old_key, clan_tag="#AAA111")

    prepared = await _prepare(
        runtime=_runtime(active_raid_season=old_key),
        clash=FakeRaidClash({"#AAA111": [_season(members=[], districts=[])]}),
        saved_rows=(saved,),
    )

    assert prepared.previous_active_season is not None
    assert prepared.previous_active_season.season_key == old_key
    assert any("SQLite" in warning and old_key in warning for warning in prepared.warnings)


@pytest.mark.asyncio
async def test_prepare_maps_malformed_sqlite_technical_state_to_domain_error() -> None:
    """Проверяет остановку preparation на повреждённом persisted state."""

    saved = _saved_raid_state(clan_tag="#AAA111")
    saved.technical_values["attacks"] = "1"

    with pytest.raises(RaidDataError, match="attacks"):
        await _prepare(
            runtime=_runtime(),
            clash=FakeRaidClash({"#AAA111": []}),
            saved_rows=(saved,),
        )


@pytest.mark.asyncio
async def test_prepare_imports_registered_block_and_merges_snapshot_and_composition_by_title() -> (
    None
):
    """Проверяет import, layered merge, empty inheritance и hidden snapshot."""

    payload = _season(
        members=[_member("#PLAYER", attacks=1)],
        districts=[_district(123, [_attack("#PLAYER", 100)])],
    )
    row_key = "raid_row:2026-07-24T07:00:00+00:00|#AAA111|#PLAYER"
    saved = _saved_raid_state(
        clan_tag="#AAA111",
        user_values={"raid_note": "old", "raid_hidden": "keep"},
    )
    sheets = FakeSheetsClient(
        values_by_range={
            ("Рейды", "A1:I3"): [
                ["Alpha"],
                [
                    "__bot_key",
                    "№",
                    "Тег",
                    "Ник",
                    "Атаки",
                    "Нормо-очки",
                    "Коэффициент",
                    "Золото столицы",
                    "ЗАМЕТКА",
                ],
                [row_key, 1, "#PLAYER", "Player", "1/6", 2, "0.33", 1000, ""],
            ],
        }
    )
    blocks = RecordingSheetBlockRepository(
        blocks=(
            make_sheet_block(
                sheet_name="Рейды",
                sheet_id=444,
                block_key="raid:#AAA111",
                start_cell="A1",
                rows_count=3,
                columns_count=9,
            ),
        )
    )
    composition = (
        PlannedPlayerState(
            "#PLAYER",
            "active",
            "#AAA111",
            16,
            "Player",
            None,
            {"composition_note": "from composition"},
            None,
        ),
    )

    prepared = await _prepare(
        runtime=_runtime(
            active_raid_season="2026-07-24T07:00:00+00:00",
            profiles=_raid_profiles(include_user=True),
        ),
        clash=FakeRaidClash({"#AAA111": [payload]}),
        sheets=sheets,
        blocks=blocks,
        saved_rows=(saved,),
        composition=composition,
    )

    values = prepared.selected_season.blocks[0].rows[0].user_values
    assert values == {
        "raid_note": "from composition",
        "raid_hidden": "keep",
    }
    assert sheets.read_calls == [("Рейды", "A1:I3")]


@pytest.mark.asyncio
async def test_prepare_rejects_registered_raid_block_without_required_header() -> None:
    """Проверяет preparation error для повреждённого обязательного managed block."""

    sheets = FakeSheetsClient(
        values_by_range={("Рейды", "A1:C2"): [["Тег", "Ник"], ["#PLAYER", "Player"]]}
    )
    blocks = RecordingSheetBlockRepository(
        blocks=(
            make_sheet_block(
                sheet_name="Рейды",
                sheet_id=444,
                block_key="raid:#AAA111",
                start_cell="A1",
                rows_count=2,
                columns_count=3,
            ),
        )
    )

    with pytest.raises(RaidDataError, match="header"):
        await _prepare(
            runtime=_runtime(active_raid_season="2026-07-24T07:00:00+00:00"),
            clash=FakeRaidClash({"#AAA111": [_season(members=[], districts=[])]}),
            sheets=sheets,
            blocks=blocks,
        )


@pytest.mark.asyncio
async def test_prepare_manual_import_wins_over_composition() -> None:
    """Проверяет приоритет непустого ручного raid value."""

    payload = _season(
        members=[_member("#PLAYER", attacks=1)],
        districts=[_district(123, [_attack("#PLAYER", 100)])],
    )
    row_key = "raid_row:2026-07-24T07:00:00+00:00|#AAA111|#PLAYER"
    sheets = FakeSheetsClient(
        values_by_range={
            ("Рейды", "A1:I2"): [
                [
                    "__bot_key",
                    "№",
                    "Тег",
                    "Ник",
                    "Атаки",
                    "Нормо-очки",
                    "Коэффициент",
                    "Золото столицы",
                    "ЗАМЕТКА",
                ],
                [row_key, 1, "#PLAYER", "Player", "1/6", 2, "0.33", 1000, "manual"],
            ],
        }
    )
    blocks = RecordingSheetBlockRepository(
        blocks=(
            make_sheet_block(
                sheet_name="Рейды",
                sheet_id=444,
                block_key="raid:#AAA111",
                start_cell="A1",
                rows_count=2,
                columns_count=9,
            ),
        )
    )
    composition = (
        PlannedPlayerState(
            "#PLAYER",
            "active",
            "#AAA111",
            16,
            "Player",
            None,
            {"composition_note": "composition"},
            None,
        ),
    )

    prepared = await _prepare(
        runtime=_runtime(
            active_raid_season="2026-07-24T07:00:00+00:00",
            profiles=_raid_profiles(include_user=True),
        ),
        clash=FakeRaidClash({"#AAA111": [payload]}),
        sheets=sheets,
        blocks=blocks,
        composition=composition,
    )

    assert prepared.selected_season.blocks[0].rows[0].user_values["raid_note"] == "manual"


@pytest.mark.asyncio
async def test_prepare_uses_unique_technical_fallback_and_rejects_ambiguous_rows() -> None:
    """Проверяет допустимый fallback и preparation error при дубле identity."""

    payload = _season(
        members=[_member("#PLAYER", attacks=1)],
        districts=[_district(123, [_attack("#PLAYER", 100)])],
    )
    header = [
        "__bot_key",
        "№",
        "Тег",
        "Ник",
        "Атаки",
        "Нормо-очки",
        "Коэффициент",
        "Золото столицы",
        "ЗАМЕТКА",
    ]
    data_row = ["", 1, "#PLAYER", "Player", "1/6", 2, "0.33", 1000, "manual"]
    sheets = FakeSheetsClient(values_by_range={("Рейды", "A1:I3"): [header, data_row]})
    blocks = RecordingSheetBlockRepository(
        blocks=(
            make_sheet_block(
                sheet_name="Рейды",
                sheet_id=444,
                block_key="raid:#AAA111",
                start_cell="A1",
                rows_count=3,
                columns_count=9,
            ),
        )
    )

    runtime = _runtime(
        active_raid_season="2026-07-24T07:00:00+00:00",
        profiles=_raid_profiles(include_user=True),
    )
    prepared = await _prepare(
        runtime=runtime,
        clash=FakeRaidClash({"#AAA111": [payload]}),
        sheets=sheets,
        blocks=blocks,
    )
    assert prepared.selected_season.blocks[0].rows[0].user_values["raid_note"] == "manual"
    assert any("fallback" in warning for warning in prepared.warnings)

    sheets.values_by_range[("Рейды", "A1:I3")] = [header, data_row, list(data_row)]
    with pytest.raises(RaidDataError, match=r"неоднознач|дублик"):
        await _prepare(
            runtime=runtime,
            clash=FakeRaidClash({"#AAA111": [payload]}),
            sheets=sheets,
            blocks=blocks,
        )


@pytest.mark.asyncio
async def test_prepare_diff_contains_only_real_aggregated_changes() -> None:
    """Проверяет пустой diff для равного state и агрегированный updated item."""

    payload = _season(
        members=[_member("#PLAYER", attacks=1)],
        districts=[_district(123, [_attack("#PLAYER", 100)])],
    )
    unchanged = _saved_raid_state(clan_tag="#AAA111")

    first = await _prepare(
        runtime=_runtime(),
        clash=FakeRaidClash({"#AAA111": [payload]}),
        saved_rows=(unchanged,),
    )
    changed = _saved_raid_state(clan_tag="#AAA111", weighted_damage_units=100)
    second = await _prepare(
        runtime=_runtime(),
        clash=FakeRaidClash({"#AAA111": [payload]}),
        saved_rows=(changed,),
    )

    assert first.diff == ()
    assert len(second.diff) == 1
    assert "#PLAYER" in second.diff[0]
    assert "атака #" not in second.diff[0].casefold()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("state", None),
        ("state", 1),
        ("state", "unknown"),
        ("startTime", None),
        ("startTime", "2026-07-24"),
    ),
)
@pytest.mark.parametrize("with_saved_snapshot", (False, True))
@pytest.mark.asyncio
async def test_prepare_rejects_invalid_api_selection_metadata_before_fallback_or_writes(
    field: str,
    value: object,
    with_saved_snapshot: bool,
) -> None:
    """Проверяет strict selection metadata до message/SQLite fallback."""

    payload = _season(members=[], districts=[])
    if value is None:
        payload.pop(field)
    else:
        payload[field] = value
    saved_rows = (
        (
            _saved_raid_state(
                season_key="2026-07-17T07:00:00+00:00",
                clan_tag="#AAA111",
            ),
        )
        if with_saved_snapshot
        else ()
    )
    sheets = FakeSheetsClient()

    with pytest.raises(RaidDataError):
        await _prepare(
            runtime=_runtime(),
            clash=FakeRaidClash({"#AAA111": [payload]}),
            sheets=sheets,
            saved_rows=saved_rows,
        )

    assert sheets.batch_value_updates == []
    assert sheets.spreadsheet_requests == []
    assert sheets.hidden_dimensions == []


@pytest.mark.parametrize(
    "mutate",
    (
        lambda state: replace(state, clan_tag="BROKEN"),
        lambda state: replace(state, player_tag="BROKEN"),
        lambda state: replace(state, season_key="broken-season"),
        lambda state: replace(state, season_start_at="broken-start"),
        lambda state: replace(state, season_end_at="broken-end"),
        lambda state: replace(
            state,
            season_end_at="2026-07-20T07:00:00+00:00",
        ),
        lambda state: replace(state, season_state="unknown"),
        lambda state: replace(state, row_key="raid_row:broken"),
    ),
)
@pytest.mark.asyncio
async def test_prepare_maps_corrupted_sqlite_outer_fields_to_raid_data_error(
    mutate: Any,
) -> None:
    """Проверяет строгий persisted contract всех outer raid fields."""

    saved = mutate(_saved_raid_state(clan_tag="#AAA111"))
    sheets = FakeSheetsClient()

    with pytest.raises(RaidDataError):
        await _prepare(
            runtime=_runtime(),
            clash=FakeRaidClash({"#AAA111": []}),
            sheets=sheets,
            saved_rows=(saved,),
        )

    assert sheets.batch_value_updates == []
    assert sheets.spreadsheet_requests == []
    assert sheets.hidden_dimensions == []


@pytest.mark.asyncio
async def test_prepare_warns_for_each_clan_using_partial_old_active_sqlite_fallback() -> None:
    """Проверяет warning частичного per-clan fallback старого active."""

    old_key = "2026-07-10T07:00:00+00:00"
    old_api = _season(
        members=[_member("#ONE_PLAYER", attacks=1)],
        districts=[_district(123, [_attack("#ONE_PLAYER", 100)])],
        start_time="20260710T070000.000Z",
        end_time="20260713T070000.000Z",
    )
    newest = _season(members=[], districts=[])
    clans = (
        make_tracked_clan(tag="#ONE", name="One"),
        make_tracked_clan(tag="#TWO", name="Two"),
    )
    saved_second = _saved_raid_state(
        season_key=old_key,
        season_end_at="2026-07-13T07:00:00+00:00",
        clan_tag="#TWO",
        player_tag="#TWO_PLAYER",
    )

    prepared = await _prepare(
        runtime=_runtime(clans=clans, active_raid_season=old_key),
        clash=FakeRaidClash(
            {
                "#ONE": [newest, old_api],
                "#TWO": [newest],
            }
        ),
        saved_rows=(saved_second,),
    )

    assert prepared.previous_active_season is not None
    assert [len(block.rows) for block in prepared.previous_active_season.blocks] == [1, 1]
    fallback_warnings = [
        warning for warning in prepared.warnings if "SQLite" in warning and old_key in warning
    ]
    assert fallback_warnings == [
        f"{old_key}: старый active raid season восстановлен из SQLite для кланов #TWO."
    ]
