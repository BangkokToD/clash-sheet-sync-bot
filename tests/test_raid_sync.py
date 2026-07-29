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
from clash_sheet_sync_bot.sheets.client import SheetMetadata
from clash_sheet_sync_bot.sheets.column_profiles import default_columns
from clash_sheet_sync_bot.sync.composition import PlannedPlayerState
from clash_sheet_sync_bot.sync.raids import (
    CAPITAL_PEAK_DISTRICT_ID,
    RAID_ACTIVE_SHEET_NAME,
    RAID_BLOCK_PREFIX,
    RAID_MESSAGE_BLOCK_PREFIX,
    SOFT_PINK_RGB,
    PreparedRaidSeason,
    PreparedRaidSync,
    RaidClanBlock,
    RaidContractError,
    RaidPlannedRow,
    RaidRetryableDataError,
    RaidSheetSyncResult,
    RaidTechnicalValues,
    aggregate_raid_season,
    apply_public_raid_sync,
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
from tests.fakes.sheets import (
    FakeSheetsClient,
    RecordingRaidPlayerStateRepository,
    RecordingSheetBlockRepository,
)

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
    active_raid_sheet_name: str = "Рейды",
    active_raid_sheet_id: int | None = 444,
    profiles: tuple[Any, ...] | None = None,
) -> Any:
    binding = make_sheet_binding(
        active_raid_sheet_name=active_raid_sheet_name,
        active_raid_sheet_id=active_raid_sheet_id,
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


RAID_SEASON_KEY = "2026-07-24T07:00:00+00:00"
RAID_SEASON_END = "2026-07-27T07:00:00+00:00"


def _planned_raid_row(
    *,
    clan_tag: str = "#AAA111",
    player_tag: str = "#PLAYER",
    player_name: str = "Player",
    rank: int = 1,
    attacks: int = 5,
    normal_points: Decimal = Decimal("2.50"),
    coefficient: Decimal = Decimal("0.42"),
    user_values: dict[str, str] | None = None,
) -> RaidPlannedRow:
    """Создаёт готовую raid row для apply-контрактов."""

    return RaidPlannedRow(
        row_key=f"raid_row:{RAID_SEASON_KEY}|{clan_tag}|{player_tag}",
        season_key=RAID_SEASON_KEY,
        clan_tag=clan_tag,
        rank=rank,
        technical_values=RaidTechnicalValues(
            player_tag=player_tag,
            player_name=player_name,
            attacks=attacks,
            attack_limit=5,
            bonus_attack_limit=1,
            capital_resources_looted=1234,
            weighted_damage_units=250,
            normal_points=normal_points,
            coefficient=coefficient,
        ),
        user_values=user_values or {},
    )


def _prepared_raid_apply(
    *,
    state: str = "ended",
    blocks: tuple[RaidClanBlock, ...] | None = None,
) -> PreparedRaidSync:
    """Создаёт результат preparation для изолированного apply."""

    return PreparedRaidSync(
        selected_season=PreparedRaidSeason(
            season_key=RAID_SEASON_KEY,
            start_time=RAID_SEASON_KEY,
            end_time=RAID_SEASON_END,
            state=state,  # type: ignore[arg-type]
            blocks=blocks
            or (
                RaidClanBlock(
                    clan_tag="#AAA111",
                    clan_name="Alpha",
                    rows=(_planned_raid_row(),),
                ),
            ),
        ),
        previous_active_season=None,
    )


async def _apply_raid(
    *,
    runtime: Any,
    prepared: PreparedRaidSync,
    sheets: FakeSheetsClient | None = None,
    blocks: RecordingSheetBlockRepository | None = None,
    states: RecordingRaidPlayerStateRepository | None = None,
    config: Any | None = None,
) -> tuple[
    RaidSheetSyncResult,
    FakeSheetsClient,
    RecordingSheetBlockRepository,
    RecordingRaidPlayerStateRepository,
]:
    """Запускает raid apply через полностью локальные fakes."""

    sheets = sheets or FakeSheetsClient()
    blocks = blocks or RecordingSheetBlockRepository()
    states = states or RecordingRaidPlayerStateRepository()
    result = await apply_public_raid_sync(
        runtime_config=runtime,
        sheets_client=sheets,  # type: ignore[arg-type]
        raid_player_state_repository=states,  # type: ignore[arg-type]
        sheet_block_repository=blocks,  # type: ignore[arg-type]
        config=config or make_app_config(),
        prepared=prepared,
    )
    return result, sheets, blocks, states


def _pink_ranges(
    sheets: FakeSheetsClient,
    batch_index: int | None = None,
) -> list[dict[str, Any]]:
    """Извлекает ranges статусной розовой заливки."""

    batches = (
        sheets.spreadsheet_requests
        if batch_index is None
        else [sheets.spreadsheet_requests[batch_index]]
    )
    return [
        request["repeatCell"]["range"]
        for request_batch in batches
        for request in request_batch
        if request.get("repeatCell", {})
        .get("cell", {})
        .get("userEnteredFormat", {})
        .get("backgroundColorStyle", {})
        .get("rgbColor")
        == SOFT_PINK_RGB
    ]


def _format_reset_ranges(
    sheets: FakeSheetsClient,
    *,
    batch_index: int,
) -> list[dict[str, Any]]:
    """Извлекает точные ranges сброса старого managed formatting."""

    return [
        request["repeatCell"]["range"]
        for request in sheets.spreadsheet_requests[batch_index]
        if request.get("repeatCell", {}).get("cell") == {"userEnteredFormat": {}}
        and request["repeatCell"]["fields"]
        == (
            "userEnteredFormat(backgroundColorStyle,borders,textFormat,"
            "verticalAlignment,wrapStrategy,numberFormat)"
        )
    ]


def _number_format_requests(
    sheets: FakeSheetsClient,
    *,
    batch_index: int,
) -> list[dict[str, Any]]:
    """Извлекает реальные repeatCell requests числового формата."""

    return [
        request["repeatCell"]
        for request in sheets.spreadsheet_requests[batch_index]
        if request.get("repeatCell", {}).get("fields") == "userEnteredFormat.numberFormat"
    ]


def _grid_range(
    *,
    sheet_id: int,
    start_row: int,
    end_row: int,
    start_column: int,
    end_column: int,
) -> dict[str, int]:
    """Строит ожидаемый zero-based GridRange."""

    return {
        "sheetId": sheet_id,
        "startRowIndex": start_row,
        "endRowIndex": end_row,
        "startColumnIndex": start_column,
        "endColumnIndex": end_column,
    }


def _assert_no_rotation_operations(sheets: FakeSheetsClient) -> None:
    """Проверяет отсутствие операций будущего commit 5."""

    serialized_requests = json.dumps(sheets.spreadsheet_requests, ensure_ascii=False)
    assert sheets.added_sheets == []
    assert "duplicateSheet" not in serialized_requests
    assert "deleteSheet" not in serialized_requests
    assert "updateSheetProperties" not in serialized_requests


async def _apply_raid_twice(
    *,
    first: PreparedRaidSync,
    second: PreparedRaidSync,
) -> tuple[
    FakeSheetsClient,
    RecordingSheetBlockRepository,
    RecordingRaidPlayerStateRepository,
]:
    """Последовательно применяет два состояния одного raid season."""

    runtime = _runtime(active_raid_season=RAID_SEASON_KEY)
    sheets = FakeSheetsClient()
    blocks = RecordingSheetBlockRepository()
    states = RecordingRaidPlayerStateRepository()
    await _apply_raid(
        runtime=runtime,
        prepared=first,
        sheets=sheets,
        blocks=blocks,
        states=states,
    )
    blocks.blocks = blocks.replace_calls[-1]["blocks"]
    await _apply_raid(
        runtime=runtime,
        prepared=second,
        sheets=sheets,
        blocks=blocks,
        states=states,
    )
    return sheets, blocks, states


@pytest.mark.asyncio
async def test_apply_first_active_raid_sheet_writes_matrix_formats_and_runtime_state() -> None:
    """Покрывает первый лист, матрицу, numeric values, format и runtime state."""

    first = _planned_raid_row(player_tag="#P1", player_name="Five", attacks=5)
    complete = _planned_raid_row(
        player_tag="#P2",
        player_name="Six",
        rank=2,
        attacks=6,
        normal_points=Decimal("3.75"),
        coefficient=Decimal("0.625"),
    )
    prepared = _prepared_raid_apply(
        blocks=(
            RaidClanBlock(
                clan_tag="#AAA111",
                clan_name="Alpha",
                rows=(first, complete),
            ),
        ),
    )
    sheets = FakeSheetsClient(metadata_sheets=())
    runtime = _runtime(
        active_raid_season=None,
        active_raid_sheet_name="Старое имя",
        active_raid_sheet_id=None,
        profiles=_raid_profiles(include_user=True),
    )

    result, sheets, block_repository, state_repository = await _apply_raid(
        runtime=runtime,
        prepared=prepared,
        sheets=sheets,
    )

    assert result.sheet_name == RAID_ACTIVE_SHEET_NAME
    assert result.season_key == RAID_SEASON_KEY
    assert sheets.added_sheets == [RAID_ACTIVE_SHEET_NAME]
    assert len(sheets.batch_value_updates) == 1
    updates = sheets.batch_value_updates[0]
    assert len(updates) == 1
    assert updates[0].range_a1 == "A1:I4"
    assert updates[0].values[1] == [
        "__bot_key",
        "№",
        "Тег",
        "Ник",
        "Атаки",
        "Нормо-очки",
        "Коэффициент",
        "Золото столицы",
        " ЗАМЕТКА ",
    ]
    first_values = updates[0].values[2]
    complete_values = updates[0].values[3]
    assert first_values == [
        first.row_key,
        1,
        "#P1",
        "Five",
        "5/6",
        2.5,
        0.42,
        1234,
        "",
    ]
    assert complete_values[1:8] == [2, "#P2", "Six", "6/6", 3.75, 0.625, 1234]
    assert isinstance(first_values[1], int)
    assert isinstance(first_values[5], float)
    assert isinstance(first_values[6], float)
    assert isinstance(first_values[7], int)

    expected_number_format = {
        "userEnteredFormat": {
            "numberFormat": {
                "type": "NUMBER",
                "pattern": "0.00",
            },
        },
    }
    assert _number_format_requests(sheets, batch_index=0) == [
        {
            "range": _grid_range(
                sheet_id=result.sheet_id,
                start_row=2,
                end_row=3,
                start_column=5,
                end_column=6,
            ),
            "cell": expected_number_format,
            "fields": "userEnteredFormat.numberFormat",
        },
        {
            "range": _grid_range(
                sheet_id=result.sheet_id,
                start_row=2,
                end_row=3,
                start_column=6,
                end_column=7,
            ),
            "cell": expected_number_format,
            "fields": "userEnteredFormat.numberFormat",
        },
        {
            "range": _grid_range(
                sheet_id=result.sheet_id,
                start_row=3,
                end_row=4,
                start_column=5,
                end_column=6,
            ),
            "cell": expected_number_format,
            "fields": "userEnteredFormat.numberFormat",
        },
        {
            "range": _grid_range(
                sheet_id=result.sheet_id,
                start_row=3,
                end_row=4,
                start_column=6,
                end_column=7,
            ),
            "cell": expected_number_format,
            "fields": "userEnteredFormat.numberFormat",
        },
    ]
    serialized_requests = json.dumps(sheets.spreadsheet_requests, ensure_ascii=False)
    assert "horizontalAlignment" not in serialized_requests
    assert "pixelSize" not in serialized_requests
    assert "deleteSheet" not in serialized_requests
    assert "updateSheetProperties" not in serialized_requests
    assert _pink_ranges(sheets) == [
        {
            "sheetId": result.sheet_id,
            "startRowIndex": 2,
            "endRowIndex": 3,
            "startColumnIndex": 4,
            "endColumnIndex": 5,
        },
    ]
    assert sheets.hidden_dimensions == [
        {
            "sheet_id": result.sheet_id,
            "dimension": "COLUMNS",
            "start_index": 0,
            "end_index": 1,
            "hidden": True,
        },
    ]
    assert [state.row_key for state in state_repository.upserted_states] == [
        first.row_key,
        complete.row_key,
    ]
    assert {state.season_state for state in state_repository.upserted_states} == {"ended"}
    assert len(block_repository.replace_calls) == 1
    assert block_repository.replace_calls[0]["blocks"] == (
        make_sheet_block(
            sheet_name=RAID_ACTIVE_SHEET_NAME,
            sheet_id=result.sheet_id,
            block_key=f"{RAID_BLOCK_PREFIX}#AAA111",
            start_cell="A1",
            rows_count=4,
            columns_count=9,
        ),
    )


@pytest.mark.asyncio
async def test_apply_ongoing_raid_uses_configured_target_without_status_fill() -> None:
    """Покрывает target из AppConfig и отсутствие ongoing status fill."""

    prepared = _prepared_raid_apply(
        state="ongoing",
        blocks=(
            RaidClanBlock(
                clan_tag="#AAA111",
                clan_name="Alpha",
                rows=(_planned_raid_row(attacks=5),),
            ),
        ),
    )

    _result, sheets, _blocks, _states = await _apply_raid(
        runtime=_runtime(active_raid_season=RAID_SEASON_KEY),
        prepared=prepared,
        config=make_app_config(raid_attacks_target=8),
    )

    assert sheets.batch_value_updates[0][-1].values[2][4] == "5/8"
    assert _pink_ranges(sheets) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("binding_title", "binding_id", "metadata_sheets", "expected_title", "expected_id"),
    (
        (
            "Старое имя",
            777,
            (
                SheetMetadata(sheet_id=444, title="Рейды", index=0),
                SheetMetadata(sheet_id=777, title="Переименованный active", index=1),
            ),
            "Переименованный active",
            777,
        ),
        (
            "Мои рейды",
            999,
            (SheetMetadata(sheet_id=555, title="Мои рейды", index=0),),
            "Мои рейды",
            555,
        ),
        (
            "Старое имя",
            999,
            (SheetMetadata(sheet_id=444, title="Рейды", index=0),),
            "Рейды",
            444,
        ),
    ),
)
async def test_apply_resolves_active_raid_sheet_by_id_binding_title_then_canonical(
    binding_title: str,
    binding_id: int,
    metadata_sheets: tuple[SheetMetadata, ...],
    expected_title: str,
    expected_id: int,
) -> None:
    """Проверяет resolver active-листа без staging/rotation."""

    sheets = FakeSheetsClient(metadata_sheets=metadata_sheets)
    result, sheets, _blocks, _states = await _apply_raid(
        runtime=_runtime(
            active_raid_season=RAID_SEASON_KEY,
            active_raid_sheet_name=binding_title,
            active_raid_sheet_id=binding_id,
        ),
        prepared=_prepared_raid_apply(),
        sheets=sheets,
    )

    assert (result.sheet_name, result.sheet_id) == (expected_title, expected_id)
    assert sheets.added_sheets == []
    assert {update.sheet_name for update in sheets.batch_value_updates[0]} == {expected_title}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("binding_title", "binding_id", "metadata_sheets"),
    (
        (
            "Настроенные рейды",
            444,
            (
                SheetMetadata(sheet_id=444, title="Первый", index=0),
                SheetMetadata(sheet_id=444, title="Второй", index=1),
            ),
        ),
        (
            "Настроенные рейды",
            999,
            (
                SheetMetadata(sheet_id=444, title="Настроенные рейды", index=0),
                SheetMetadata(sheet_id=555, title="Настроенные рейды", index=1),
            ),
        ),
        (
            "Отсутствующие рейды",
            999,
            (
                SheetMetadata(sheet_id=444, title=RAID_ACTIVE_SHEET_NAME, index=0),
                SheetMetadata(sheet_id=555, title=RAID_ACTIVE_SHEET_NAME, index=1),
            ),
        ),
    ),
    ids=("duplicate-sheet-id", "duplicate-binding-title", "duplicate-canonical-title"),
)
async def test_apply_rejects_ambiguous_active_raid_sheet_without_writes(
    binding_title: str,
    binding_id: int,
    metadata_sheets: tuple[SheetMetadata, ...],
) -> None:
    """Проверяет остановку ambiguous resolver до любого write/state."""

    sheets = FakeSheetsClient(metadata_sheets=metadata_sheets)
    blocks = RecordingSheetBlockRepository()
    states = RecordingRaidPlayerStateRepository()

    with pytest.raises(RaidDataError, match=r"(?i)неоднознач"):
        await _apply_raid(
            runtime=_runtime(
                active_raid_season=RAID_SEASON_KEY,
                active_raid_sheet_name=binding_title,
                active_raid_sheet_id=binding_id,
            ),
            prepared=_prepared_raid_apply(),
            sheets=sheets,
            blocks=blocks,
            states=states,
        )

    assert sheets.batch_value_updates == []
    assert sheets.spreadsheet_requests == []
    assert sheets.hidden_dimensions == []
    assert sheets.added_sheets == []
    assert states.upserted_states == []
    assert blocks.replace_calls == []
    assert states.commit_calls == 0
    assert blocks.commit_calls == 0


@pytest.mark.asyncio
async def test_apply_writes_separate_clan_and_message_blocks_and_clears_only_owned_range() -> None:
    """Покрывает multi-clan blocks, message-block и точечную очистку."""

    prepared = _prepared_raid_apply(
        blocks=(
            RaidClanBlock(
                clan_tag="#AAA111",
                clan_name="Alpha",
                rows=(_planned_raid_row(),),
            ),
            RaidClanBlock(
                clan_tag="#BBB222",
                clan_name="Beta",
                message="Нет данных за выбранный рейдовый уикенд",
            ),
        ),
    )
    old_owned = make_sheet_block(
        sheet_name="Рейды",
        sheet_id=444,
        block_key=f"{RAID_BLOCK_PREFIX}#OLD",
        start_cell="K20",
        rows_count=4,
        columns_count=3,
    )
    blocks = RecordingSheetBlockRepository(
        blocks=(
            old_owned,
            make_sheet_block(
                sheet_name="Рейды",
                sheet_id=444,
                block_key="cwl:#USER",
                start_cell="A40",
            ),
            make_sheet_block(
                sheet_name="Рейды",
                sheet_id=999,
                block_key=f"{RAID_BLOCK_PREFIX}#OTHER",
                start_cell="A50",
            ),
            make_sheet_block(
                sheet_name="Рейды",
                sheet_id=None,
                block_key=f"{RAID_BLOCK_PREFIX}#TITLE_ONLY",
                start_cell="A60",
            ),
        ),
    )

    _result, sheets, blocks, _states = await _apply_raid(
        runtime=_runtime(active_raid_season=RAID_SEASON_KEY),
        prepared=prepared,
        blocks=blocks,
    )

    updates = sheets.batch_value_updates[0]
    assert [(update.range_a1, update.values) for update in updates[:1]] == [
        ("K20:M23", [["", "", ""] for _ in range(4)])
    ]
    assert [update.range_a1 for update in updates[1:]] == ["A1:H3", "A5:H6"]
    assert "2026-07-24" in updates[1].values[0][1]
    assert "2026-07-27" in updates[1].values[0][1]
    assert updates[2].values[1][1] == "Нет данных за выбранный рейдовый уикенд"
    replacement = blocks.replace_calls[-1]
    assert replacement["blocks"] == (
        make_sheet_block(
            sheet_name="Рейды",
            sheet_id=444,
            block_key=f"{RAID_BLOCK_PREFIX}#AAA111",
            start_cell="A1",
            rows_count=3,
            columns_count=8,
        ),
        make_sheet_block(
            sheet_name="Рейды",
            sheet_id=444,
            block_key=f"{RAID_MESSAGE_BLOCK_PREFIX}#BBB222",
            start_cell="A5",
            rows_count=2,
            columns_count=8,
        ),
    )


@pytest.mark.asyncio
async def test_apply_rewrites_same_raid_season_idempotently_without_rotation_operations() -> None:
    """Покрывает повторную запись active season и exact previous range clear."""

    runtime = _runtime(active_raid_season=RAID_SEASON_KEY)
    prepared = _prepared_raid_apply()
    blocks = RecordingSheetBlockRepository()
    first_result, sheets, blocks, _states = await _apply_raid(
        runtime=runtime,
        prepared=prepared,
        blocks=blocks,
    )
    blocks.blocks = blocks.replace_calls[-1]["blocks"]

    second_result, sheets, blocks, _states = await _apply_raid(
        runtime=runtime,
        prepared=prepared,
        sheets=sheets,
        blocks=blocks,
    )

    assert second_result == first_result
    assert sheets.added_sheets == []
    second_updates = sheets.batch_value_updates[1]
    assert [update.range_a1 for update in second_updates] == ["A1:H3", "A1:H3"]
    assert second_updates[0].values == [["" for _ in range(8)] for _ in range(3)]
    serialized_requests = json.dumps(sheets.spreadsheet_requests, ensure_ascii=False)
    assert "duplicateSheet" not in serialized_requests
    assert "deleteSheet" not in serialized_requests
    assert "updateSheetProperties" not in serialized_requests


@pytest.mark.asyncio
async def test_apply_clears_attack_fill_when_player_reaches_target() -> None:
    """Проверяет ended 5/6 → 6/6 с полным сбросом прежней заливки."""

    first = _prepared_raid_apply(
        blocks=(
            RaidClanBlock(
                clan_tag="#AAA111",
                clan_name="Alpha",
                rows=(_planned_raid_row(attacks=5),),
            ),
        ),
    )
    second_row = _planned_raid_row(attacks=6)
    second = _prepared_raid_apply(
        blocks=(
            RaidClanBlock(
                clan_tag="#AAA111",
                clan_name="Alpha",
                rows=(second_row,),
            ),
        ),
    )

    sheets, blocks, _states = await _apply_raid_twice(first=first, second=second)

    second_updates = sheets.batch_value_updates[1]
    assert [update.range_a1 for update in second_updates] == ["A1:H3", "A1:H3"]
    assert second_updates[0].values == [["" for _ in range(8)] for _ in range(3)]
    assert second_updates[1].values[2][4] == "6/6"
    assert _format_reset_ranges(sheets, batch_index=1) == [
        _grid_range(
            sheet_id=444,
            start_row=0,
            end_row=3,
            start_column=0,
            end_column=8,
        ),
    ]
    assert _pink_ranges(sheets, batch_index=0) == [
        _grid_range(
            sheet_id=444,
            start_row=2,
            end_row=3,
            start_column=4,
            end_column=5,
        ),
    ]
    assert _pink_ranges(sheets, batch_index=1) == []
    assert blocks.replace_calls[-1]["blocks"] == (
        make_sheet_block(
            sheet_name="Рейды",
            sheet_id=444,
            block_key=f"{RAID_BLOCK_PREFIX}#AAA111",
            start_cell="A1",
            rows_count=3,
            columns_count=8,
        ),
    )
    _assert_no_rotation_operations(sheets)


@pytest.mark.asyncio
async def test_apply_reassigns_attack_fill_after_row_reorder() -> None:
    """Проверяет, что status fill следует за игроком после перестановки строк."""

    incomplete_first = _planned_raid_row(
        player_tag="#P1",
        player_name="Incomplete",
        rank=1,
        attacks=5,
    )
    complete_second = _planned_raid_row(
        player_tag="#P2",
        player_name="Complete",
        rank=2,
        attacks=6,
    )
    complete_first = replace(complete_second, rank=1)
    incomplete_second = replace(incomplete_first, rank=2)
    first = _prepared_raid_apply(
        blocks=(
            RaidClanBlock(
                clan_tag="#AAA111",
                clan_name="Alpha",
                rows=(incomplete_first, complete_second),
            ),
        ),
    )
    second = _prepared_raid_apply(
        blocks=(
            RaidClanBlock(
                clan_tag="#AAA111",
                clan_name="Alpha",
                rows=(complete_first, incomplete_second),
            ),
        ),
    )

    sheets, blocks, _states = await _apply_raid_twice(first=first, second=second)

    second_updates = sheets.batch_value_updates[1]
    assert [update.range_a1 for update in second_updates] == ["A1:H4", "A1:H4"]
    assert second_updates[0].values == [["" for _ in range(8)] for _ in range(4)]
    assert [row[2] for row in second_updates[1].values[2:]] == ["#P2", "#P1"]
    assert _format_reset_ranges(sheets, batch_index=1) == [
        _grid_range(
            sheet_id=444,
            start_row=0,
            end_row=4,
            start_column=0,
            end_column=8,
        ),
    ]
    assert _pink_ranges(sheets, batch_index=0) == [
        _grid_range(
            sheet_id=444,
            start_row=2,
            end_row=3,
            start_column=4,
            end_column=5,
        ),
    ]
    assert _pink_ranges(sheets, batch_index=1) == [
        _grid_range(
            sheet_id=444,
            start_row=3,
            end_row=4,
            start_column=4,
            end_column=5,
        ),
    ]
    assert blocks.replace_calls[-1]["blocks"] == (
        make_sheet_block(
            sheet_name="Рейды",
            sheet_id=444,
            block_key=f"{RAID_BLOCK_PREFIX}#AAA111",
            start_cell="A1",
            rows_count=4,
            columns_count=8,
        ),
    )
    _assert_no_rotation_operations(sheets)


@pytest.mark.asyncio
async def test_apply_replaces_rows_with_message_block() -> None:
    """Проверяет очистку rows и formatting при переходе к message-block."""

    rows = (
        _planned_raid_row(player_tag="#P1", attacks=5),
        _planned_raid_row(player_tag="#P2", rank=2, attacks=6),
    )
    first = _prepared_raid_apply(
        blocks=(RaidClanBlock(clan_tag="#AAA111", clan_name="Alpha", rows=rows),),
    )
    message = "Нет данных рейдового уикенда 2026-07-24 — 2026-07-27"
    second = _prepared_raid_apply(
        blocks=(
            RaidClanBlock(
                clan_tag="#AAA111",
                clan_name="Alpha",
                message=message,
            ),
        ),
    )

    sheets, blocks, _states = await _apply_raid_twice(first=first, second=second)

    second_updates = sheets.batch_value_updates[1]
    assert [update.range_a1 for update in second_updates] == ["A1:H4", "A1:H2"]
    assert second_updates[0].values == [["" for _ in range(8)] for _ in range(4)]
    assert second_updates[0].values[2:] == [["" for _ in range(8)] for _ in range(2)]
    assert second_updates[1].values[1][1] == message
    assert _format_reset_ranges(sheets, batch_index=1) == [
        _grid_range(
            sheet_id=444,
            start_row=0,
            end_row=4,
            start_column=0,
            end_column=8,
        ),
    ]
    assert _pink_ranges(sheets, batch_index=1) == []
    assert blocks.replace_calls[-1]["blocks"] == (
        make_sheet_block(
            sheet_name="Рейды",
            sheet_id=444,
            block_key=f"{RAID_MESSAGE_BLOCK_PREFIX}#AAA111",
            start_cell="A1",
            rows_count=2,
            columns_count=8,
        ),
    )
    _assert_no_rotation_operations(sheets)


@pytest.mark.asyncio
async def test_apply_replaces_message_block_with_rows() -> None:
    """Проверяет очистку message text перед записью data rows."""

    old_message = "Нет данных рейдового уикенда 2026-07-24 — 2026-07-27"
    first = _prepared_raid_apply(
        blocks=(
            RaidClanBlock(
                clan_tag="#AAA111",
                clan_name="Alpha",
                message=old_message,
            ),
        ),
    )
    rows = (
        _planned_raid_row(player_tag="#P1", attacks=6),
        _planned_raid_row(player_tag="#P2", rank=2, attacks=5),
    )
    second = _prepared_raid_apply(
        blocks=(RaidClanBlock(clan_tag="#AAA111", clan_name="Alpha", rows=rows),),
    )

    sheets, blocks, _states = await _apply_raid_twice(first=first, second=second)

    second_updates = sheets.batch_value_updates[1]
    assert [update.range_a1 for update in second_updates] == ["A1:H2", "A1:H4"]
    assert second_updates[0].values == [["" for _ in range(8)] for _ in range(2)]
    assert all(old_message not in str(cell) for row in second_updates[1].values for cell in row)
    assert [row[2] for row in second_updates[1].values[2:]] == ["#P1", "#P2"]
    assert _format_reset_ranges(sheets, batch_index=1) == [
        _grid_range(
            sheet_id=444,
            start_row=0,
            end_row=2,
            start_column=0,
            end_column=8,
        ),
    ]
    assert _pink_ranges(sheets, batch_index=1) == [
        _grid_range(
            sheet_id=444,
            start_row=3,
            end_row=4,
            start_column=4,
            end_column=5,
        ),
    ]
    assert blocks.replace_calls[-1]["blocks"] == (
        make_sheet_block(
            sheet_name="Рейды",
            sheet_id=444,
            block_key=f"{RAID_BLOCK_PREFIX}#AAA111",
            start_cell="A1",
            rows_count=4,
            columns_count=8,
        ),
    )
    _assert_no_rotation_operations(sheets)


@pytest.mark.asyncio
async def test_apply_clears_tail_after_managed_range_shrinks() -> None:
    """Проверяет очистку хвоста прежнего более высокого managed block."""

    first_rows = (
        _planned_raid_row(player_tag="#P1", attacks=6),
        _planned_raid_row(player_tag="#P2", rank=2, attacks=6),
        _planned_raid_row(player_tag="#P3", rank=3, attacks=5),
    )
    second_row = _planned_raid_row(player_tag="#P1", attacks=6)
    first = _prepared_raid_apply(
        blocks=(RaidClanBlock(clan_tag="#AAA111", clan_name="Alpha", rows=first_rows),),
    )
    second = _prepared_raid_apply(
        blocks=(
            RaidClanBlock(
                clan_tag="#AAA111",
                clan_name="Alpha",
                rows=(second_row,),
            ),
        ),
    )

    sheets, blocks, _states = await _apply_raid_twice(first=first, second=second)

    second_updates = sheets.batch_value_updates[1]
    assert [update.range_a1 for update in second_updates] == ["A1:H5", "A1:H3"]
    assert second_updates[0].values == [["" for _ in range(8)] for _ in range(5)]
    assert second_updates[0].values[3:] == [["" for _ in range(8)] for _ in range(2)]
    assert len(second_updates[1].values) == 3
    assert _format_reset_ranges(sheets, batch_index=1) == [
        _grid_range(
            sheet_id=444,
            start_row=0,
            end_row=5,
            start_column=0,
            end_column=8,
        ),
    ]
    assert _pink_ranges(sheets, batch_index=1) == []
    assert blocks.replace_calls[-1]["blocks"] == (
        make_sheet_block(
            sheet_name="Рейды",
            sheet_id=444,
            block_key=f"{RAID_BLOCK_PREFIX}#AAA111",
            start_cell="A1",
            rows_count=3,
            columns_count=8,
        ),
    )
    _assert_no_rotation_operations(sheets)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_field", "message"),
    (
        ("fail_values_update", "values write failed"),
        ("fail_spreadsheet_update", "format write failed"),
        ("fail_hide", "hide write failed"),
    ),
    ids=("values", "format", "hide"),
)
async def test_apply_sheet_write_failure_does_not_update_sqlite_or_block_metadata(
    failure_field: str,
    message: str,
) -> None:
    """Проверяет отсутствие ложного success-state при Sheet write failure."""

    error = RuntimeError(message)
    sheets = FakeSheetsClient(**{failure_field: error})  # type: ignore[arg-type]
    blocks = RecordingSheetBlockRepository()
    states = RecordingRaidPlayerStateRepository()

    with pytest.raises(RuntimeError, match=message) as caught:
        await _apply_raid(
            runtime=_runtime(active_raid_season=RAID_SEASON_KEY),
            prepared=_prepared_raid_apply(),
            sheets=sheets,
            blocks=blocks,
            states=states,
        )

    assert caught.value is error
    assert states.upserted_states == []
    assert blocks.replace_calls == []
    assert states.commit_calls == 0
    assert blocks.commit_calls == 0
    if failure_field == "fail_values_update":
        assert len(sheets.batch_value_updates) == 1
        assert sheets.spreadsheet_requests == []
        assert sheets.hidden_dimensions == []
    elif failure_field == "fail_spreadsheet_update":
        assert len(sheets.batch_value_updates) == 1
        assert len(sheets.spreadsheet_requests) == 1
        assert sheets.hidden_dimensions == []
    else:
        assert len(sheets.batch_value_updates) == 1
        assert len(sheets.spreadsheet_requests) == 1
        assert len(sheets.hidden_dimensions) == 1


@pytest.mark.asyncio
async def test_apply_keeps_saved_ended_raid_visible_between_api_events() -> None:
    """Покрывает отображение сохранённого ended state при пустом API-окне."""

    runtime = _runtime(active_raid_season=RAID_SEASON_KEY)
    prepared = await _prepare(
        runtime=runtime,
        clash=FakeRaidClash({"#AAA111": []}),
        saved_rows=(
            _saved_raid_state(
                season_key=RAID_SEASON_KEY,
                season_end_at=RAID_SEASON_END,
                clan_tag="#AAA111",
                player_tag="#PLAYER",
            ),
        ),
    )

    result, sheets, _blocks, states = await _apply_raid(
        runtime=runtime,
        prepared=prepared,
    )

    assert result.season_state == "ended"
    assert sheets.batch_value_updates[0][-1].values[0][1].endswith("| завершён")
    assert sheets.batch_value_updates[0][-1].values[2][4] == "1/6"
    assert states.upserted_states[0].season_state == "ended"


@pytest.mark.asyncio
async def test_apply_rejects_season_change_without_sheet_or_state_writes() -> None:
    """Фиксирует границу commit 4: смена сезона требует будущей rotation."""

    sheets = FakeSheetsClient()
    blocks = RecordingSheetBlockRepository()
    states = RecordingRaidPlayerStateRepository()

    with pytest.raises(RaidDataError, match="ротац"):
        await _apply_raid(
            runtime=_runtime(active_raid_season="2026-07-17T07:00:00+00:00"),
            prepared=_prepared_raid_apply(),
            sheets=sheets,
            blocks=blocks,
            states=states,
        )

    assert sheets.batch_value_updates == []
    assert sheets.spreadsheet_requests == []
    assert sheets.hidden_dimensions == []
    assert sheets.added_sheets == []
    assert blocks.replace_calls == []
    assert states.upserted_states == []
