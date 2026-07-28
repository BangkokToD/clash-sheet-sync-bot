"""Contract-тесты Raid API, parser и доменной формулы."""

from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from clash_sheet_sync_bot.coc.client import ClashApiUnavailableError, ClashClient
from clash_sheet_sync_bot.sync.raids import (
    CAPITAL_PEAK_DISTRICT_ID,
    RaidContractError,
    RaidRetryableDataError,
    RaidTechnicalValues,
    aggregate_raid_season,
    classify_raid_district,
    parse_raid_season,
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
) -> JsonObject:
    """Создаёт season object минимального raid-контракта."""

    return {
        "state": state,
        "startTime": "20260724T070000.000Z",
        "endTime": "20260727T070000.000Z",
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
