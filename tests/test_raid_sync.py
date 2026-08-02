"""Contract-тесты Raid API, parser и доменной формулы."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiosqlite
import httpx
import pytest

from clash_sheet_sync_bot.coc.client import ClashApiUnavailableError, ClashClient
from clash_sheet_sync_bot.repositories import (
    RaidDataError,
    RaidPlayerState,
    RaidPlayerStateRepository,
    RaidSheetArchive,
    RaidSheetArchiveRepository,
    RuntimeConfigRepository,
    SheetBindingRepository,
    SheetBlockRepository,
)
from clash_sheet_sync_bot.sheets.client import (
    GoogleSheetsWriteError,
    SheetMetadata,
    SheetsClient,
)
from clash_sheet_sync_bot.sheets.column_profiles import default_columns
from clash_sheet_sync_bot.sync.composition import PlannedPlayerState
from clash_sheet_sync_bot.sync.raids import (
    CAPITAL_PEAK_DISTRICT_ID,
    RAID_ACTIVE_SHEET_NAME,
    RAID_ARCHIVE_SHEET_PREFIX,
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
    RecordingRaidSheetArchiveRepository,
    RecordingSheetBindingRepository,
    RecordingSheetBlockRepository,
)

JsonObject = dict[str, Any]
FIXTURE_DIR = Path(__file__).parent / "fixtures"
TEST_NOW = "2026-07-29T00:00:00+00:00"


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
    destruction_percent: int | None = None,
) -> JsonObject:
    """Создаёт district object минимального raid-контракта."""

    final_destruction = (
        destruction_percent
        if destruction_percent is not None
        else (attacks[0]["destructionPercent"] if attacks else 0)
    )
    return {
        "id": district_id,
        "name": name,
        "destructionPercent": final_destruction,
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
        ("destructionPercent", None),
        ("destructionPercent", True),
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
        "destructionPercent": 0,
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


def test_parse_raid_season_converts_reverse_cumulative_damage_to_player_contributions() -> None:
    """Проверяет delta накопленного урона и привязку к игрокам."""

    payload = _season(
        members=[
            _member("#LAST", attacks=1),
            _member("#MIDDLE", attacks=1),
            _member("#FIRST", attacks=1),
        ],
        districts=[
            _district(
                CAPITAL_PEAK_DISTRICT_ID,
                [
                    _attack("#LAST", 100),
                    _attack("#MIDDLE", 90),
                    _attack("#FIRST", 33),
                ],
                name="Capital Peak",
            ),
        ],
    )

    parsed = parse_raid_season(payload, clan_tag="#CLAN")

    assert [attack.player_tag for attack in parsed.attacks] == [
        "#LAST",
        "#MIDDLE",
        "#FIRST",
    ]
    assert [attack.destruction_delta_percent for attack in parsed.attacks] == [10, 57, 33]
    assert sum(attack.destruction_delta_percent for attack in parsed.attacks) == 100


def test_parse_raid_season_rejects_non_monotonic_reverse_cumulative_damage() -> None:
    """Проверяет отказ при невозрастающей API-хронологии."""

    payload = _season(
        members=[_member("#PLAYER", attacks=3)],
        districts=[
            _district(
                123,
                [
                    _attack("#PLAYER", 100),
                    _attack("#PLAYER", 60),
                    _attack("#PLAYER", 70),
                ],
            ),
        ],
    )

    with pytest.raises(RaidContractError, match="немонотон"):
        parse_raid_season(payload, clan_tag="#CLAN")


def test_parse_raid_season_rejects_district_final_destruction_mismatch() -> None:
    """Проверяет сверку cumulative-атак с итогом района."""

    payload = _season(
        members=[_member("#PLAYER", attacks=2)],
        districts=[
            _district(
                123,
                [_attack("#PLAYER", 90), _attack("#PLAYER", 40)],
                destruction_percent=100,
            ),
        ],
    )

    with pytest.raises(RaidContractError, match="итоговым destructionPercent"):
        parse_raid_season(payload, clan_tag="#CLAN")


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


def test_normal_district_cumulative_100_and_33_give_two_normal_points() -> None:
    """Проверяет формулу обычного района без float drift."""

    rows = _aggregate(
        _season(
            members=[_member("#PLAYER", attacks=2)],
            districts=[_district(123, [_attack("#PLAYER", 100), _attack("#PLAYER", 33)])],
        ),
    )

    assert rows[0].weighted_damage_units == 200
    assert rows[0].normal_points == Decimal("2")
    assert isinstance(rows[0].normal_points, Decimal)


def test_capital_peak_cumulative_damage_gives_three_normal_points() -> None:
    """Проверяет формулу Capital Peak по подтверждённому ID."""

    rows = _aggregate(
        _season(
            members=[_member("#PLAYER", attacks=3)],
            districts=[
                _district(
                    CAPITAL_PEAK_DISTRICT_ID,
                    [
                        _attack("#PLAYER", 100),
                        _attack("#PLAYER", 75),
                        _attack("#PLAYER", 40),
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

    rows = _aggregate(
        _season(
            members=[_member("#PLAYER", attacks=6)],
            districts=[
                _district(123, [_attack("#PLAYER", 100), _attack("#PLAYER", 50)]),
                _district(124, [_attack("#PLAYER", 100), _attack("#PLAYER", 50)]),
                _district(125, [_attack("#PLAYER", 100), _attack("#PLAYER", 50)]),
            ],
        ),
    )

    assert rows[0].weighted_damage_units == 600
    assert rows[0].coefficient == Decimal("1")


def test_five_normative_attacks_display_as_point_83() -> None:
    """Проверяет отображаемое округление коэффициента пяти атак."""

    rows = _aggregate(
        _season(
            members=[_member("#PLAYER", attacks=5)],
            districts=[
                _district(123, [_attack("#PLAYER", 100), _attack("#PLAYER", 50)]),
                _district(124, [_attack("#PLAYER", 100), _attack("#PLAYER", 50)]),
                _district(125, [_attack("#PLAYER", 50)]),
            ],
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
                [_attack("#TWO", 100), _attack("#ONE", 50)],
                name="Capital Peak",
            ),
        ],
    )

    rows = _aggregate(payload)

    assert [row.player_tag for row in rows] == ["#ONE", "#TWO"]
    assert rows[0].weighted_damage_units == 250
    assert rows[1].weighted_damage_units == 250


def test_normal_and_capital_three_attack_districts_give_five_points_and_point_83() -> None:
    """Проверяет полные обычный район и Capital Peak за шесть атак."""

    cumulative_attacks = [
        _attack("#PLAYER", 100),
        _attack("#PLAYER", 90),
        _attack("#PLAYER", 33),
    ]
    rows = _aggregate(
        _season(
            members=[_member("#PLAYER", attacks=6)],
            districts=[
                _district(123, cumulative_attacks),
                _district(
                    CAPITAL_PEAK_DISTRICT_ID,
                    cumulative_attacks,
                    name="Capital Peak",
                ),
            ],
        ),
    )

    assert rows[0].weighted_damage_units == 500
    assert rows[0].normal_points == Decimal("5")
    assert f"{rows[0].coefficient:.2f}" == "0.83"


def test_coefficient_may_exceed_one_and_zero_attack_has_zero_points() -> None:
    """Проверяет сильные атаки и участника с нулевым вкладом."""

    payload = _season(
        members=[
            _member("#STRONG", attacks=6),
            _member("#ZERO", attacks=1),
        ],
        districts=[
            _district(
                CAPITAL_PEAK_DISTRICT_ID,
                [_attack("#STRONG", 100)],
                name="Capital Peak",
            ),
            _district(123, [_attack("#STRONG", 100)]),
            _district(124, [_attack("#STRONG", 100)]),
            _district(125, [_attack("#STRONG", 0)]),
            _district(126, [_attack("#STRONG", 0)]),
            _district(127, [_attack("#STRONG", 0)]),
            _district(
                128,
                [_attack("#ZERO", 0)],
            ),
        ],
    )

    strong, zero = _aggregate(payload)

    assert strong.weighted_damage_units == 700
    assert strong.coefficient == Decimal(7) / Decimal(6)
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
    assert [attack.player_tag for attack in parsed.attacks[:3]] == [
        "#000000088",
        "#000000088",
        "#000000088",
    ]
    assert [attack.destruction_delta_percent for attack in parsed.attacks[:3]] == [
        10,
        57,
        33,
    ]


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
            _district(123, [_attack("#ONE", 50)]),
            _district(124, [_attack("#TWO", 50)]),
            _district(125, [_attack("#LEFT", 25)]),
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
    season_key: str = RAID_SEASON_KEY,
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
        row_key=f"raid_row:{season_key}|{clan_tag}|{player_tag}",
        season_key=season_key,
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
    season_key: str = RAID_SEASON_KEY,
    end_time: str = RAID_SEASON_END,
    state: str = "ended",
    blocks: tuple[RaidClanBlock, ...] | None = None,
    previous_active_season: PreparedRaidSeason | None = None,
) -> PreparedRaidSync:
    """Создаёт результат preparation для изолированного apply."""

    return PreparedRaidSync(
        selected_season=PreparedRaidSeason(
            season_key=season_key,
            start_time=season_key,
            end_time=end_time,
            state=state,  # type: ignore[arg-type]
            blocks=blocks
            or (
                RaidClanBlock(
                    clan_tag="#AAA111",
                    clan_name="Alpha",
                    rows=(_planned_raid_row(season_key=season_key),),
                ),
            ),
        ),
        previous_active_season=previous_active_season,
    )


async def _apply_raid(
    *,
    runtime: Any,
    prepared: PreparedRaidSync,
    sheets: FakeSheetsClient | None = None,
    blocks: RecordingSheetBlockRepository | None = None,
    states: RecordingRaidPlayerStateRepository | None = None,
    archives: RecordingRaidSheetArchiveRepository | None = None,
    bindings: RecordingSheetBindingRepository | None = None,
    config: Any | None = None,
    sync_run_id: int = 77,
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
    archives = archives or RecordingRaidSheetArchiveRepository()
    bindings = bindings or RecordingSheetBindingRepository()
    result = await apply_public_raid_sync(
        runtime_config=runtime,
        sheets_client=sheets,  # type: ignore[arg-type]
        raid_player_state_repository=states,  # type: ignore[arg-type]
        raid_sheet_archive_repository=archives,  # type: ignore[arg-type]
        sheet_block_repository=blocks,  # type: ignore[arg-type]
        sheet_binding_repository=bindings,  # type: ignore[arg-type]
        config=config or make_app_config(),
        prepared=prepared,
        sync_run_id=sync_run_id,
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
async def test_apply_raid_uses_composition_and_cwl_green_palette() -> None:
    """Проверяет общую зелёную палитру managed raid block."""

    rows = (
        _planned_raid_row(player_tag="#P1", rank=1, attacks=6),
        _planned_raid_row(player_tag="#P2", rank=2, attacks=6),
    )
    result, sheets, _blocks, _states = await _apply_raid(
        runtime=_runtime(active_raid_season=RAID_SEASON_KEY),
        prepared=_prepared_raid_apply(
            state="ongoing",
            blocks=(RaidClanBlock(clan_tag="#AAA111", clan_name="Alpha", rows=rows),),
        ),
    )

    full_format_fields = (
        "userEnteredFormat(backgroundColorStyle,textFormat,verticalAlignment,wrapStrategy)"
    )
    full_format_requests = [
        request["repeatCell"]
        for request in sheets.spreadsheet_requests[0]
        if request.get("repeatCell", {}).get("fields") == full_format_fields
    ]
    assert full_format_requests == [
        {
            "range": _grid_range(
                sheet_id=result.sheet_id,
                start_row=0,
                end_row=4,
                start_column=0,
                end_column=8,
            ),
            "cell": {
                "userEnteredFormat": {
                    "backgroundColorStyle": {
                        "rgbColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                    },
                    "textFormat": {
                        "foregroundColorStyle": {
                            "rgbColor": {"red": 0.0, "green": 0.0, "blue": 0.0},
                        },
                        "bold": False,
                    },
                    "verticalAlignment": "MIDDLE",
                    "wrapStrategy": "WRAP",
                },
            },
            "fields": full_format_fields,
        },
        {
            "range": _grid_range(
                sheet_id=result.sheet_id,
                start_row=0,
                end_row=1,
                start_column=0,
                end_column=8,
            ),
            "cell": {
                "userEnteredFormat": {
                    "backgroundColorStyle": {
                        "rgbColor": {"red": 0.12, "green": 0.32, "blue": 0.24},
                    },
                    "textFormat": {
                        "foregroundColorStyle": {
                            "rgbColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                        },
                        "bold": True,
                    },
                    "verticalAlignment": "MIDDLE",
                    "wrapStrategy": "WRAP",
                },
            },
            "fields": full_format_fields,
        },
        {
            "range": _grid_range(
                sheet_id=result.sheet_id,
                start_row=1,
                end_row=2,
                start_column=0,
                end_column=8,
            ),
            "cell": {
                "userEnteredFormat": {
                    "backgroundColorStyle": {
                        "rgbColor": {"red": 0.18, "green": 0.42, "blue": 0.31},
                    },
                    "textFormat": {
                        "foregroundColorStyle": {
                            "rgbColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                        },
                        "bold": True,
                    },
                    "verticalAlignment": "MIDDLE",
                    "wrapStrategy": "WRAP",
                },
            },
            "fields": full_format_fields,
        },
    ]

    band_requests = [
        request["repeatCell"]
        for request in sheets.spreadsheet_requests[0]
        if request.get("repeatCell", {}).get("fields") == "userEnteredFormat.backgroundColorStyle"
    ]
    assert band_requests == [
        {
            "range": _grid_range(
                sheet_id=result.sheet_id,
                start_row=3,
                end_row=4,
                start_column=0,
                end_column=8,
            ),
            "cell": {
                "userEnteredFormat": {
                    "backgroundColorStyle": {
                        "rgbColor": {"red": 0.95, "green": 0.97, "blue": 0.96},
                    },
                },
            },
            "fields": "userEnteredFormat.backgroundColorStyle",
        },
    ]


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
            "Рейды",
            444,
        ),
        (
            "Мои рейды",
            777,
            (
                SheetMetadata(sheet_id=444, title="Рейды", index=0),
                SheetMetadata(sheet_id=777, title="Мои рейды", index=1),
            ),
            "Мои рейды",
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
async def test_apply_resolves_active_raid_sheet_with_canonical_recovery_priority(
    binding_title: str,
    binding_id: int,
    metadata_sheets: tuple[SheetMetadata, ...],
    expected_title: str,
    expected_id: int,
) -> None:
    """Проверяет canonical recovery, затем binding ID/title fallback."""

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
    assert result.showing_saved_season is True
    assert sheets.batch_value_updates[0][-1].values[0][1].endswith("| завершён")
    assert sheets.batch_value_updates[0][-1].values[2][4] == "1/6"
    assert states.upserted_states[0].season_state == "ended"


@pytest.mark.asyncio
async def test_apply_rejects_season_change_without_previous_state_before_writes() -> None:
    """Проверяет обязательное old active state для безопасной rotation."""

    sheets = FakeSheetsClient()
    blocks = RecordingSheetBlockRepository()
    states = RecordingRaidPlayerStateRepository()

    with pytest.raises(RaidDataError, match="old active"):
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


OLD_RAID_SEASON_KEY = "2026-07-17T07:00:00+00:00"
OLD_RAID_SEASON_END = "2026-07-20T07:00:00+00:00"


def _prepared_raid_rotation() -> PreparedRaidSync:
    """Создаёт preparation смены old active на новый выбранный сезон."""

    previous = PreparedRaidSeason(
        season_key=OLD_RAID_SEASON_KEY,
        start_time=OLD_RAID_SEASON_KEY,
        end_time=OLD_RAID_SEASON_END,
        state="ended",
        blocks=(
            RaidClanBlock(
                clan_tag="#AAA111",
                clan_name="Alpha",
                rows=(
                    _planned_raid_row(
                        season_key=OLD_RAID_SEASON_KEY,
                        player_tag="#OLD",
                        player_name="Old",
                        attacks=5,
                    ),
                ),
            ),
        ),
    )
    return _prepared_raid_apply(previous_active_season=previous)


def _prepared_raid_rotation_layout(
    *,
    previous_rows: tuple[RaidPlannedRow, ...] = (),
    previous_message: str | None = None,
) -> PreparedRaidSync:
    """Создаёт смену сезона с управляемым финальным layout архива."""

    previous = PreparedRaidSeason(
        season_key=OLD_RAID_SEASON_KEY,
        start_time=OLD_RAID_SEASON_KEY,
        end_time=OLD_RAID_SEASON_END,
        state="ended",
        blocks=(
            RaidClanBlock(
                clan_tag="#AAA111",
                clan_name="Alpha",
                rows=previous_rows,
                message=previous_message,
            ),
        ),
    )
    return _prepared_raid_apply(previous_active_season=previous)


def _raid_archive(
    number: int,
    *,
    chat_id: int = -1001,
    sheet_id: int | None = None,
    sheet_name: str | None = None,
) -> RaidSheetArchive:
    """Создаёт ordered archive registry item."""

    day = number + 1
    season_key = f"2026-06-{day:02d}T07:00:00+00:00"
    return RaidSheetArchive(
        chat_id=chat_id,
        season_key=season_key,
        season_start_at=season_key,
        sheet_name=sheet_name or f"Рейды 2026-06-{day:02d}",
        sheet_id=sheet_id if sheet_id is not None else 500 + number,
        archived_at=f"2026-07-{day:02d}T00:00:00+00:00",
    )


def _rotation_metadata(
    *archives: RaidSheetArchive,
) -> tuple[SheetMetadata, ...]:
    """Создаёт metadata active, CWL и зарегистрированных архивов."""

    return (
        SheetMetadata(sheet_id=444, title=RAID_ACTIVE_SHEET_NAME, index=0),
        SheetMetadata(sheet_id=222, title="CWL", index=1),
        *(
            SheetMetadata(
                sheet_id=archive.sheet_id,
                title=archive.sheet_name,
                index=index + 2,
            )
            for index, archive in enumerate(archives)
        ),
    )


@pytest.mark.asyncio
async def test_apply_first_season_updates_binding_without_archive_or_staging() -> None:
    """Проверяет первый season поверх message-only active без архива."""

    sheets = FakeSheetsClient(metadata_sheets=_rotation_metadata())
    archives = RecordingRaidSheetArchiveRepository()
    bindings = RecordingSheetBindingRepository()

    result, sheets, _blocks, _states = await _apply_raid(
        runtime=_runtime(active_raid_season=None),
        prepared=_prepared_raid_apply(),
        sheets=sheets,
        archives=archives,
        bindings=bindings,
    )

    assert result.archived_previous_season is False
    assert result.season_start_at == RAID_SEASON_KEY
    assert result.season_end_at == RAID_SEASON_END
    assert result.attacks_complete_count == 0
    assert result.attacks_below_target_count == 1
    assert sheets.added_sheets == []
    assert archives.upserted_archives == []
    assert bindings.update_calls[-1]["active_raid_season"] == RAID_SEASON_KEY
    assert bindings.update_calls[-1]["active_raid_sheet_id"] == 444


@pytest.mark.asyncio
async def test_apply_rotation_prepares_staging_then_uses_one_atomic_rename_move_request() -> None:
    """Проверяет полный staging до единого atomic rename/move batch."""

    sheets = FakeSheetsClient(metadata_sheets=_rotation_metadata())
    old_block = make_sheet_block(
        sheet_name=RAID_ACTIVE_SHEET_NAME,
        sheet_id=444,
        block_key=f"{RAID_BLOCK_PREFIX}#AAA111",
        start_cell="A1",
        rows_count=3,
        columns_count=8,
    )
    blocks = RecordingSheetBlockRepository(blocks=(old_block,))
    archives = RecordingRaidSheetArchiveRepository()
    bindings = RecordingSheetBindingRepository()
    states = RecordingRaidPlayerStateRepository()

    result, sheets, blocks, states = await _apply_raid(
        runtime=_runtime(active_raid_season=OLD_RAID_SEASON_KEY),
        prepared=_prepared_raid_rotation(),
        sheets=sheets,
        blocks=blocks,
        states=states,
        archives=archives,
        bindings=bindings,
        sync_run_id=77,
    )

    staging_title = "Рейды - staging - 77"
    assert sheets.added_sheets == [staging_title]
    assert sheets.operation_log[:8] == [
        "add_sheet",
        "values",
        "format",
        "hide",
        "values",
        "format",
        "hide",
        "atomic_rotation",
    ]
    atomic_batches = [
        batch
        for batch in sheets.spreadsheet_requests
        if any(
            request.get("updateSheetProperties", {}).get("fields") == "index" for request in batch
        )
    ]
    assert atomic_batches == [
        [
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": 444,
                        "title": "Рейды 2026-07-17",
                    },
                    "fields": "title",
                },
            },
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": 445,
                        "title": RAID_ACTIVE_SHEET_NAME,
                    },
                    "fields": "title",
                },
            },
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": 445, "index": 1},
                    "fields": "index",
                },
            },
        ],
    ]
    assert result.archived_previous_season is True
    assert result.archive_sheet_name == "Рейды 2026-07-17"
    assert archives.upserted_archives == [
        RaidSheetArchive(
            chat_id=-1001,
            season_key=OLD_RAID_SEASON_KEY,
            season_start_at=OLD_RAID_SEASON_KEY,
            sheet_name="Рейды 2026-07-17",
            sheet_id=444,
            archived_at=archives.upserted_archives[0].archived_at,
        ),
    ]
    assert blocks.rebind_calls == []
    assert blocks.replace_calls[1]["blocks"] == (
        make_sheet_block(
            sheet_name="Рейды 2026-07-17",
            sheet_id=444,
            block_key=f"{RAID_BLOCK_PREFIX}#AAA111",
            start_cell="A1",
            rows_count=3,
            columns_count=8,
        ),
    )
    assert blocks.replace_calls[-1]["blocks"] == (
        make_sheet_block(
            sheet_name=RAID_ACTIVE_SHEET_NAME,
            sheet_id=445,
            block_key=f"{RAID_BLOCK_PREFIX}#AAA111",
            start_cell="A1",
            rows_count=3,
            columns_count=8,
        ),
    )
    assert bindings.update_calls[-1]["active_raid_sheet_id"] == 445
    assert bindings.update_calls[-1]["active_raid_season"] == RAID_SEASON_KEY
    assert {state.season_key for state in states.upserted_states} == {
        OLD_RAID_SEASON_KEY,
        RAID_SEASON_KEY,
    }
    assert states.commit_calls == 0
    assert blocks.commit_calls == 0
    assert archives.commit_calls == 0
    assert bindings.commit_calls == 0


@pytest.mark.asyncio
async def test_apply_rotation_uses_unique_archive_suffix() -> None:
    """Проверяет безопасный suffix при конфликтующих пользовательских titles."""

    sheets = FakeSheetsClient(
        metadata_sheets=(
            *_rotation_metadata(),
            SheetMetadata(sheet_id=600, title="Рейды 2026-07-17", index=2),
            SheetMetadata(sheet_id=601, title="Рейды 2026-07-17 - 2", index=3),
        ),
    )

    result, sheets, _blocks, _states = await _apply_raid(
        runtime=_runtime(active_raid_season=OLD_RAID_SEASON_KEY),
        prepared=_prepared_raid_rotation(),
        sheets=sheets,
    )

    assert result.archive_sheet_name == "Рейды 2026-07-17 - 3"
    assert sheets.deleted_sheet_ids == []


@pytest.mark.asyncio
async def test_apply_rotation_uses_unique_sync_run_staging_title() -> None:
    """Проверяет безопасный suffix leftover staging того же sync_run_id."""

    sheets = FakeSheetsClient(
        metadata_sheets=(
            *_rotation_metadata(),
            SheetMetadata(
                sheet_id=700,
                title="Рейды - staging - 77",
                index=2,
            ),
        ),
    )

    await _apply_raid(
        runtime=_runtime(active_raid_season=OLD_RAID_SEASON_KEY),
        prepared=_prepared_raid_rotation(),
        sheets=sheets,
        sync_run_id=77,
    )

    assert sheets.added_sheets == ["Рейды - staging - 77 - 2"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "previous_rows",
        "previous_message",
        "old_block_key",
        "old_rows_count",
        "expected_block_key",
        "expected_rows_count",
    ),
    (
        (
            (
                _planned_raid_row(
                    season_key=OLD_RAID_SEASON_KEY,
                    player_tag="#OLD1",
                    rank=1,
                ),
                _planned_raid_row(
                    season_key=OLD_RAID_SEASON_KEY,
                    player_tag="#OLD2",
                    rank=2,
                ),
            ),
            None,
            f"{RAID_BLOCK_PREFIX}#AAA111",
            3,
            f"{RAID_BLOCK_PREFIX}#AAA111",
            4,
        ),
        (
            (
                _planned_raid_row(
                    season_key=OLD_RAID_SEASON_KEY,
                    player_tag="#OLD1",
                ),
            ),
            None,
            f"{RAID_BLOCK_PREFIX}#AAA111",
            6,
            f"{RAID_BLOCK_PREFIX}#AAA111",
            3,
        ),
        (
            (),
            "Нет данных завершённого сезона",
            f"{RAID_BLOCK_PREFIX}#AAA111",
            5,
            f"{RAID_MESSAGE_BLOCK_PREFIX}#AAA111",
            2,
        ),
        (
            (
                _planned_raid_row(
                    season_key=OLD_RAID_SEASON_KEY,
                    player_tag="#OLD1",
                ),
            ),
            None,
            f"{RAID_MESSAGE_BLOCK_PREFIX}#AAA111",
            2,
            f"{RAID_BLOCK_PREFIX}#AAA111",
            3,
        ),
    ),
    ids=("players-grow", "players-shrink", "rows-to-message", "message-to-rows"),
)
async def test_rotation_registers_finalized_archive_block_layout(
    previous_rows: tuple[RaidPlannedRow, ...],
    previous_message: str | None,
    old_block_key: str,
    old_rows_count: int,
    expected_block_key: str,
    expected_rows_count: int,
) -> None:
    """Проверяет archive metadata по фактически финализированной матрице."""

    stale = make_sheet_block(
        sheet_name=RAID_ACTIVE_SHEET_NAME,
        sheet_id=444,
        block_key=old_block_key,
        start_cell="A1",
        rows_count=old_rows_count,
        columns_count=8,
    )
    unrelated = make_sheet_block(
        sheet_name=RAID_ACTIVE_SHEET_NAME,
        sheet_id=444,
        block_key="user:keep",
        start_cell="K20",
        rows_count=7,
        columns_count=3,
    )
    blocks = RecordingSheetBlockRepository(blocks=(stale, unrelated))

    result, _sheets, blocks, _states = await _apply_raid(
        runtime=_runtime(active_raid_season=OLD_RAID_SEASON_KEY),
        prepared=_prepared_raid_rotation_layout(
            previous_rows=previous_rows,
            previous_message=previous_message,
        ),
        sheets=FakeSheetsClient(metadata_sheets=_rotation_metadata()),
        blocks=blocks,
    )

    archive_blocks = tuple(
        block
        for block in blocks.blocks
        if block.sheet_id == 444
        and (
            block.block_key.startswith(RAID_BLOCK_PREFIX)
            or block.block_key.startswith(RAID_MESSAGE_BLOCK_PREFIX)
        )
    )
    assert archive_blocks == (
        make_sheet_block(
            sheet_name=result.archive_sheet_name or "",
            sheet_id=444,
            block_key=expected_block_key,
            start_cell="A1",
            rows_count=expected_rows_count,
            columns_count=8,
        ),
    )
    assert unrelated in blocks.blocks


@pytest.mark.asyncio
async def test_rotation_keeps_four_registered_archives_without_pruning() -> None:
    """Проверяет лимит четыре после добавления нового архива."""

    existing = tuple(_raid_archive(index) for index in range(3))
    sheets = FakeSheetsClient(metadata_sheets=_rotation_metadata(*existing))
    archives = RecordingRaidSheetArchiveRepository(archives=existing)

    await _apply_raid(
        runtime=_runtime(active_raid_season=OLD_RAID_SEASON_KEY),
        prepared=_prepared_raid_rotation(),
        sheets=sheets,
        archives=archives,
    )

    assert len(archives.archives) == 4
    assert sheets.deleted_sheet_ids == []


@pytest.mark.asyncio
async def test_rotation_fifth_archive_deletes_only_oldest_registered_sheet() -> None:
    """Проверяет pruning пятого архива по каноническому порядку."""

    existing = tuple(_raid_archive(index) for index in range(4))
    sheets = FakeSheetsClient(
        metadata_sheets=(
            *_rotation_metadata(*existing),
            SheetMetadata(sheet_id=900, title="Рейды 2026-06-01 user copy", index=8),
        ),
    )
    archives = RecordingRaidSheetArchiveRepository(archives=existing)
    preserved_state = _saved_raid_state(
        season_key=existing[0].season_key,
        season_end_at="2026-06-04T07:00:00+00:00",
        clan_tag="#AAA111",
        player_tag="#PRESERVED",
    )
    states = RecordingRaidPlayerStateRepository(upserted_states=[preserved_state])
    blocks = RecordingSheetBlockRepository(
        blocks=(
            make_sheet_block(
                sheet_name=existing[0].sheet_name,
                sheet_id=existing[0].sheet_id,
                block_key=f"{RAID_BLOCK_PREFIX}#OLD",
                start_cell="A1",
            ),
        ),
    )

    _result, sheets, blocks, states = await _apply_raid(
        runtime=_runtime(active_raid_season=OLD_RAID_SEASON_KEY),
        prepared=_prepared_raid_rotation(),
        sheets=sheets,
        blocks=blocks,
        states=states,
        archives=archives,
    )

    assert sheets.deleted_sheet_ids == [existing[0].sheet_id]
    assert 900 not in sheets.deleted_sheet_ids
    assert archives.deleted_seasons == [(-1001, existing[0].season_key)]
    assert blocks.delete_calls[-1]["sheet_id"] == existing[0].sheet_id
    assert preserved_state in states.upserted_states


@pytest.mark.asyncio
async def test_apply_prunes_multiple_excess_archives_to_configured_limit() -> None:
    """Проверяет последовательное удаление нескольких oldest registry items."""

    existing = tuple(_raid_archive(index) for index in range(7))
    sheets = FakeSheetsClient(metadata_sheets=_rotation_metadata(*existing))
    archives = RecordingRaidSheetArchiveRepository(archives=existing)

    await _apply_raid(
        runtime=_runtime(active_raid_season=RAID_SEASON_KEY),
        prepared=_prepared_raid_apply(),
        sheets=sheets,
        archives=archives,
    )

    assert sheets.deleted_sheet_ids == [500, 501, 502]
    assert len(archives.archives) == 4


@pytest.mark.asyncio
async def test_pruning_never_deletes_active_staging_or_title_only_match() -> None:
    """Проверяет запрет delete active/staging и разрешения registry по title."""

    unsafe = (
        _raid_archive(0, sheet_id=444, sheet_name=RAID_ACTIVE_SHEET_NAME),
        _raid_archive(1, sheet_id=700, sheet_name="Рейды - staging - orphan"),
        _raid_archive(2, sheet_id=999, sheet_name="Рейды 2026-06-03"),
        _raid_archive(3),
        _raid_archive(4),
        _raid_archive(5),
    )
    sheets = FakeSheetsClient(
        metadata_sheets=(
            *_rotation_metadata(),
            SheetMetadata(sheet_id=700, title="Рейды - staging - orphan", index=2),
            SheetMetadata(sheet_id=777, title="Рейды 2026-06-03", index=3),
            SheetMetadata(sheet_id=503, title=unsafe[3].sheet_name, index=4),
            SheetMetadata(sheet_id=504, title=unsafe[4].sheet_name, index=5),
            SheetMetadata(sheet_id=505, title=unsafe[5].sheet_name, index=6),
        ),
    )
    archives = RecordingRaidSheetArchiveRepository(archives=unsafe)

    result, sheets, _blocks, _states = await _apply_raid(
        runtime=_runtime(active_raid_season=RAID_SEASON_KEY),
        prepared=_prepared_raid_apply(),
        sheets=sheets,
        archives=archives,
    )

    assert sheets.deleted_sheet_ids == []
    assert 444 not in sheets.deleted_sheet_ids
    assert 700 not in sheets.deleted_sheet_ids
    assert 777 not in sheets.deleted_sheet_ids
    assert any("cleanup" in warning.casefold() for warning in result.warnings)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("unsafe_sheet_id", "unsafe_title"),
    (
        (444, RAID_ACTIVE_SHEET_NAME),
        (700, "Рейды - staging - orphan"),
    ),
    ids=("active", "staging"),
)
async def test_pruning_rejects_active_and_staging_registry_entries(
    unsafe_sheet_id: int,
    unsafe_title: str,
) -> None:
    """Проверяет каждый запрещённый physical ID как oldest registry item."""

    unsafe = _raid_archive(0, sheet_id=unsafe_sheet_id, sheet_name=unsafe_title)
    safe = tuple(_raid_archive(index) for index in range(1, 5))
    metadata = list(_rotation_metadata(*safe))
    if unsafe_sheet_id != 444:
        metadata.append(SheetMetadata(sheet_id=unsafe_sheet_id, title=unsafe_title, index=8))
    sheets = FakeSheetsClient(metadata_sheets=tuple(metadata))
    archives = RecordingRaidSheetArchiveRepository(archives=(unsafe, *safe))

    result, sheets, _blocks, _states = await _apply_raid(
        runtime=_runtime(active_raid_season=RAID_SEASON_KEY),
        prepared=_prepared_raid_apply(),
        sheets=sheets,
        archives=archives,
    )

    assert sheets.deleted_sheet_ids == []
    if unsafe_sheet_id == 444:
        assert result.warnings == ()
    else:
        assert any("cleanup" in warning.casefold() for warning in result.warnings)


@pytest.mark.asyncio
async def test_pruning_does_not_resolve_missing_registry_sheet_id_by_title() -> None:
    """Проверяет запрет удаления похожего листа при stale registry sheet ID."""

    missing = _raid_archive(0, sheet_id=999, sheet_name="Рейды 2026-06-01")
    safe = tuple(_raid_archive(index) for index in range(1, 5))
    sheets = FakeSheetsClient(
        metadata_sheets=(
            *_rotation_metadata(*safe),
            SheetMetadata(sheet_id=777, title=missing.sheet_name, index=8),
        ),
    )
    archives = RecordingRaidSheetArchiveRepository(archives=(missing, *safe))

    result, sheets, _blocks, _states = await _apply_raid(
        runtime=_runtime(active_raid_season=RAID_SEASON_KEY),
        prepared=_prepared_raid_apply(),
        sheets=sheets,
        archives=archives,
    )

    assert sheets.deleted_sheet_ids == []
    assert 777 not in sheets.deleted_sheet_ids
    assert archives.deleted_seasons == []
    assert any("sheet_id=999" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_pruning_failure_returns_cleanup_warning_and_retries_next_apply() -> None:
    """Проверяет successful rotation, cleanup warning и повторный pruning."""

    existing = tuple(_raid_archive(index) for index in range(4))
    delete_error = GoogleSheetsWriteError("delete failed")
    sheets = FakeSheetsClient(
        metadata_sheets=_rotation_metadata(*existing),
        fail_delete_sheet=delete_error,
    )
    archives = RecordingRaidSheetArchiveRepository(archives=existing)
    bindings = RecordingSheetBindingRepository()

    first, sheets, blocks, states = await _apply_raid(
        runtime=_runtime(active_raid_season=OLD_RAID_SEASON_KEY),
        prepared=_prepared_raid_rotation(),
        sheets=sheets,
        archives=archives,
        bindings=bindings,
    )

    assert first.season_key == RAID_SEASON_KEY
    assert first.archived_previous_season is True
    assert any("cleanup" in warning.casefold() for warning in first.warnings)
    assert all("частично обновлена" not in warning for warning in first.warnings)
    assert len(archives.archives) == 5
    assert any(archive.sheet_id == 444 for archive in archives.archives)
    assert bindings.update_calls

    sheets.fail_delete_sheet = None
    second, sheets, _blocks, _states = await _apply_raid(
        runtime=_runtime(
            active_raid_season=RAID_SEASON_KEY,
            active_raid_sheet_id=first.sheet_id,
        ),
        prepared=_prepared_raid_apply(),
        sheets=sheets,
        blocks=blocks,
        states=states,
        archives=archives,
        bindings=bindings,
    )

    assert second.season_key == RAID_SEASON_KEY
    assert sheets.deleted_sheet_ids == [500, 500]
    assert len(archives.archives) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    (
        RuntimeError("runtime delete failure"),
        TypeError("type delete failure"),
        AssertionError("assert delete failure"),
        RaidDataError("contract delete failure"),
    ),
    ids=("runtime", "type", "assertion", "raid-data"),
)
async def test_pruning_propagates_unexpected_delete_exceptions(error: Exception) -> None:
    """Проверяет, что programming/contract errors не становятся cleanup warning."""

    existing = tuple(_raid_archive(index) for index in range(5))
    sheets = FakeSheetsClient(
        metadata_sheets=_rotation_metadata(*existing),
        fail_delete_sheet=error,
    )
    archives = RecordingRaidSheetArchiveRepository(archives=existing)

    with pytest.raises(type(error)) as caught:
        await _apply_raid(
            runtime=_runtime(active_raid_season=RAID_SEASON_KEY),
            prepared=_prepared_raid_apply(),
            sheets=sheets,
            archives=archives,
        )

    assert caught.value is error
    assert archives.deleted_seasons == []
    assert len(archives.archives) == 5


@pytest.mark.asyncio
async def test_pruning_rejects_duplicate_physical_registry_sheet_id() -> None:
    """Проверяет domain error при неоднозначном physical archive identity."""

    existing = tuple(_raid_archive(index) for index in range(5))
    duplicate = existing[0]
    sheets = FakeSheetsClient(
        metadata_sheets=(
            *_rotation_metadata(*existing),
            SheetMetadata(
                sheet_id=duplicate.sheet_id,
                title=f"{duplicate.sheet_name} duplicate",
                index=20,
            ),
        ),
    )
    archives = RecordingRaidSheetArchiveRepository(archives=existing)
    blocks = RecordingSheetBlockRepository()

    with pytest.raises(RaidDataError, match=r"(?i)неоднознач"):
        await _apply_raid(
            runtime=_runtime(active_raid_season=RAID_SEASON_KEY),
            prepared=_prepared_raid_apply(),
            sheets=sheets,
            archives=archives,
            blocks=blocks,
        )

    assert sheets.deleted_sheet_ids == []
    assert archives.deleted_seasons == []
    assert blocks.delete_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_field",
    (
        "fail_add_sheet",
        "fail_values_update",
        "fail_spreadsheet_update",
        "fail_hide",
        "fail_atomic_rotation",
    ),
)
async def test_rotation_failure_before_atomic_success_does_not_update_sqlite_metadata(
    failure_field: str,
) -> None:
    """Проверяет отсутствие binding/registry reconciliation до atomic success."""

    error = RuntimeError(f"{failure_field} failed")
    sheets = FakeSheetsClient(
        metadata_sheets=_rotation_metadata(),
        **{failure_field: error},  # type: ignore[arg-type]
    )
    blocks = RecordingSheetBlockRepository()
    archives = RecordingRaidSheetArchiveRepository()
    bindings = RecordingSheetBindingRepository()
    states = RecordingRaidPlayerStateRepository()

    with pytest.raises(RuntimeError) as caught:
        await _apply_raid(
            runtime=_runtime(active_raid_season=OLD_RAID_SEASON_KEY),
            prepared=_prepared_raid_rotation(),
            sheets=sheets,
            blocks=blocks,
            states=states,
            archives=archives,
            bindings=bindings,
        )

    assert caught.value is error
    assert bindings.update_calls == []
    assert archives.upserted_archives == []
    assert blocks.rebind_calls == []
    assert blocks.replace_calls == []
    assert states.upserted_states == []
    if failure_field != "fail_atomic_rotation":
        assert all(
            update.sheet_name.startswith("Рейды - staging - ")
            for batch in sheets.batch_value_updates
            for update in batch
        )
        assert all(
            request.get("repeatCell", {}).get("range", {}).get("sheetId") != 444
            for batch in sheets.spreadsheet_requests
            for request in batch
        )
        assert all(item["sheet_id"] != 444 for item in sheets.hidden_dimensions)
    else:
        canonical = [
            sheet for sheet in sheets.metadata_sheets if sheet.title == RAID_ACTIVE_SHEET_NAME
        ]
        assert [(sheet.sheet_id, sheet.title) for sheet in canonical] == [
            (444, RAID_ACTIVE_SHEET_NAME),
        ]


@pytest.mark.asyncio
async def test_retry_after_post_rename_binding_failure_reconciles_without_second_archive() -> None:
    """Проверяет recovery после atomic rename до SQLite binding."""

    sheets = FakeSheetsClient(metadata_sheets=_rotation_metadata())
    binding_error = RuntimeError("binding failed")
    bindings = RecordingSheetBindingRepository(fail_update=binding_error)
    archives = RecordingRaidSheetArchiveRepository()
    blocks = RecordingSheetBlockRepository()

    with pytest.raises(RuntimeError, match="binding failed"):
        await _apply_raid(
            runtime=_runtime(active_raid_season=OLD_RAID_SEASON_KEY),
            prepared=_prepared_raid_rotation(),
            sheets=sheets,
            archives=archives,
            bindings=bindings,
            blocks=blocks,
        )

    assert any(
        sheet.title == RAID_ACTIVE_SHEET_NAME and sheet.sheet_id != 444
        for sheet in sheets.metadata_sheets
    )
    assert bindings.update_calls == []
    assert archives.upserted_archives[-1].sheet_id == 444
    assert blocks.replace_calls[-1]["blocks"][0].sheet_id != 444
    added_after_failure = tuple(sheets.added_sheets)
    atomic_count_after_failure = sheets.operation_log.count("atomic_rotation")
    bindings.fail_update = None
    result, sheets, _blocks, _states = await _apply_raid(
        runtime=_runtime(active_raid_season=OLD_RAID_SEASON_KEY),
        prepared=_prepared_raid_rotation(),
        sheets=sheets,
        archives=archives,
        bindings=bindings,
        blocks=blocks,
    )

    assert result.archived_previous_season is True
    assert tuple(sheets.added_sheets) == added_after_failure
    assert sheets.operation_log.count("atomic_rotation") == atomic_count_after_failure
    assert archives.upserted_archives[-1].sheet_id == 444
    assert bindings.update_calls[-1]["active_raid_sheet_id"] != 444


class _FailingRepositoryProxy:
    """Делегирует repository, падая на выбранном вызове метода."""

    def __init__(
        self,
        target: object,
        *,
        method_name: str,
        call_number: int = 1,
        after_call: bool = False,
    ) -> None:
        self._target = target
        self._method_name = method_name
        self._call_number = call_number
        self._after_call = after_call
        self._calls = 0
        self.error = RuntimeError(f"{method_name} recovery checkpoint failed")

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._target, name)
        if name != self._method_name:
            return attribute

        async def controlled(*args: Any, **kwargs: Any) -> Any:
            self._calls += 1
            should_fail = self._calls == self._call_number
            if should_fail and not self._after_call:
                raise self.error
            result = await attribute(*args, **kwargs)
            if should_fail:
                raise self.error
            return result

        return controlled


class _OrderedRepositoryProxy:
    """Записывает порядок вызовов одного repository метода."""

    def __init__(
        self,
        target: object,
        *,
        method_name: str,
        event_name: str,
        events: list[str],
    ) -> None:
        self._target = target
        self._method_name = method_name
        self._event_name = event_name
        self._events = events

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._target, name)
        if name != self._method_name:
            return attribute

        async def recorded(*args: Any, **kwargs: Any) -> Any:
            self._events.append(self._event_name)
            return await attribute(*args, **kwargs)

        return recorded


@pytest.mark.asyncio
async def test_rotation_publishes_binding_after_registry_and_all_blocks() -> None:
    """Проверяет binding как последний основной reconciliation marker."""

    events: list[str] = []
    archives = RecordingRaidSheetArchiveRepository()
    blocks = RecordingSheetBlockRepository(
        blocks=(
            make_sheet_block(
                sheet_name=RAID_ACTIVE_SHEET_NAME,
                sheet_id=444,
                block_key=f"{RAID_BLOCK_PREFIX}#AAA111",
                start_cell="A1",
                rows_count=7,
                columns_count=8,
            ),
        ),
    )
    bindings = RecordingSheetBindingRepository()

    await apply_public_raid_sync(
        runtime_config=_runtime(active_raid_season=OLD_RAID_SEASON_KEY),
        sheets_client=FakeSheetsClient(metadata_sheets=_rotation_metadata()),  # type: ignore[arg-type]
        raid_player_state_repository=RecordingRaidPlayerStateRepository(),  # type: ignore[arg-type]
        raid_sheet_archive_repository=_OrderedRepositoryProxy(
            archives,
            method_name="upsert",
            event_name="archive-registry",
            events=events,
        ),  # type: ignore[arg-type]
        sheet_block_repository=_OrderedRepositoryProxy(
            blocks,
            method_name="replace_blocks_by_prefixes",
            event_name="blocks",
            events=events,
        ),  # type: ignore[arg-type]
        sheet_binding_repository=_OrderedRepositoryProxy(
            bindings,
            method_name="update_active_raid_binding",
            event_name="binding",
            events=events,
        ),  # type: ignore[arg-type]
        config=make_app_config(),
        prepared=_prepared_raid_rotation(),
        sync_run_id=77,
    )

    assert events == [
        "archive-registry",
        "blocks",
        "blocks",
        "blocks",
        "binding",
    ]


async def _seed_rotation_recovery_database(
    connection: aiosqlite.Connection,
) -> tuple[RaidSheetArchive, ...]:
    """Создаёт committed baseline для проверки настоящего SQLite rollback."""

    await connection.execute(
        """
        INSERT INTO telegram_chats(
            chat_id, title, type, status, created_by_user_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (-1001, "Raid recovery", "supergroup", "ready", 1001, TEST_NOW, TEST_NOW),
    )
    await SheetBindingRepository(connection).upsert_active_binding(
        chat_id=-1001,
        google_sheet_id="sheet-id",
        spreadsheet_url="https://docs.google.com/spreadsheets/d/sheet-id/edit",
        composition_sheet_name="Состав",
        composition_sheet_id=111,
        active_cwl_sheet_name="CWL",
        active_cwl_sheet_id=222,
        active_cwl_season="2026-07",
        active_raid_sheet_name=RAID_ACTIVE_SHEET_NAME,
        active_raid_sheet_id=444,
        active_raid_season=OLD_RAID_SEASON_KEY,
        bot_state_sheet_name="_bot_state",
        bot_state_sheet_id=333,
        timezone="Europe/Kyiv",
        now=TEST_NOW,
    )
    old_block = make_sheet_block(
        sheet_name=RAID_ACTIVE_SHEET_NAME,
        sheet_id=444,
        block_key=f"{RAID_BLOCK_PREFIX}#AAA111",
        start_cell="A1",
        rows_count=9,
        columns_count=8,
    )
    await SheetBlockRepository(connection).upsert_block(
        block=old_block,
        updated_at=TEST_NOW,
    )
    await RaidPlayerStateRepository(connection).upsert(
        _saved_raid_state(
            season_key=OLD_RAID_SEASON_KEY,
            season_end_at=OLD_RAID_SEASON_END,
            clan_tag="#AAA111",
            player_tag="#OLD",
            user_values={"raid_note": "old-user-value"},
        ),
    )
    existing = tuple(_raid_archive(index) for index in range(4))
    archive_repository = RaidSheetArchiveRepository(connection)
    for archive in existing:
        await archive_repository.upsert(archive)
    await connection.commit()
    return existing


def _prepared_rotation_with_user_values() -> PreparedRaidSync:
    """Создаёт оба сезона с user-values для recovery-инварианта."""

    previous = PreparedRaidSeason(
        season_key=OLD_RAID_SEASON_KEY,
        start_time=OLD_RAID_SEASON_KEY,
        end_time=OLD_RAID_SEASON_END,
        state="ended",
        blocks=(
            RaidClanBlock(
                clan_tag="#AAA111",
                clan_name="Alpha",
                rows=(
                    _planned_raid_row(
                        season_key=OLD_RAID_SEASON_KEY,
                        player_tag="#OLD",
                        user_values={"raid_note": "old-user-value"},
                    ),
                ),
            ),
        ),
    )
    return _prepared_raid_apply(
        blocks=(
            RaidClanBlock(
                clan_tag="#AAA111",
                clan_name="Alpha",
                rows=(
                    _planned_raid_row(
                        player_tag="#NEW",
                        user_values={"raid_note": "new-user-value"},
                    ),
                ),
            ),
        ),
        previous_active_season=previous,
    )


async def _assert_rotation_rollback_state(
    connection: aiosqlite.Connection,
    *,
    canonical_sheet_id: int,
) -> None:
    """Проверяет полный SQLite baseline после rollback post-rename ошибки."""

    binding = await RuntimeConfigRepository(connection).get_active_sheet_binding(-1001)
    assert binding is not None
    assert binding.active_raid_sheet_id == 444
    assert binding.active_raid_season == OLD_RAID_SEASON_KEY
    assert (
        await RaidSheetArchiveRepository(connection).get_by_season(
            chat_id=-1001,
            season_key=OLD_RAID_SEASON_KEY,
        )
        is None
    )
    blocks = await SheetBlockRepository(connection).list_blocks(-1001)
    assert blocks == (
        make_sheet_block(
            sheet_name=RAID_ACTIVE_SHEET_NAME,
            sheet_id=444,
            block_key=f"{RAID_BLOCK_PREFIX}#AAA111",
            start_cell="A1",
            rows_count=9,
            columns_count=8,
        ),
    )
    assert all(block.sheet_id != canonical_sheet_id for block in blocks)
    old_states = await RaidPlayerStateRepository(connection).list_for_season(
        chat_id=-1001,
        season_key=OLD_RAID_SEASON_KEY,
    )
    new_states = await RaidPlayerStateRepository(connection).list_for_season(
        chat_id=-1001,
        season_key=RAID_SEASON_KEY,
    )
    assert len(old_states) == 1
    assert old_states[0].user_values == {"raid_note": "old-user-value"}
    assert new_states == ()


async def _assert_recovered_rotation_state(
    connection: aiosqlite.Connection,
    *,
    canonical_sheet_id: int,
) -> RaidSheetArchive:
    """Проверяет единый полный post-retry SQLite recovery contract."""

    binding = await RuntimeConfigRepository(connection).get_active_sheet_binding(-1001)
    assert binding is not None
    assert binding.active_raid_sheet_name == RAID_ACTIVE_SHEET_NAME
    assert binding.active_raid_sheet_id == canonical_sheet_id
    assert binding.active_raid_season == RAID_SEASON_KEY
    old_archive = await RaidSheetArchiveRepository(connection).get_by_season(
        chat_id=-1001,
        season_key=OLD_RAID_SEASON_KEY,
    )
    assert old_archive is not None
    assert old_archive.sheet_id == 444
    raid_blocks = tuple(
        block
        for block in await SheetBlockRepository(connection).list_blocks(-1001)
        if block.block_key.startswith(RAID_BLOCK_PREFIX)
        or block.block_key.startswith(RAID_MESSAGE_BLOCK_PREFIX)
    )
    assert set(raid_blocks) == {
        make_sheet_block(
            sheet_name=old_archive.sheet_name,
            sheet_id=444,
            block_key=f"{RAID_BLOCK_PREFIX}#AAA111",
            start_cell="A1",
            rows_count=3,
            columns_count=8,
        ),
        make_sheet_block(
            sheet_name=RAID_ACTIVE_SHEET_NAME,
            sheet_id=canonical_sheet_id,
            block_key=f"{RAID_BLOCK_PREFIX}#AAA111",
            start_cell="A1",
            rows_count=3,
            columns_count=8,
        ),
    }
    old_states = await RaidPlayerStateRepository(connection).list_for_season(
        chat_id=-1001,
        season_key=OLD_RAID_SEASON_KEY,
    )
    new_states = await RaidPlayerStateRepository(connection).list_for_season(
        chat_id=-1001,
        season_key=RAID_SEASON_KEY,
    )
    assert len(old_states) == 1
    assert len(new_states) == 1
    assert old_states[0].user_values == {"raid_note": "old-user-value"}
    assert new_states[0].user_values == {"raid_note": "new-user-value"}
    foreign_key_violations = await (await connection.execute("PRAGMA foreign_key_check")).fetchall()
    assert foreign_key_violations == []
    return old_archive


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("checkpoint", "repository_name", "method_name", "call_number", "after_call"),
    (
        ("before-archive-upsert", "archives", "upsert", 1, False),
        ("after-archive-upsert", "archives", "upsert", 1, True),
        ("archive-blocks", "blocks", "replace_blocks_by_prefixes", 2, True),
        ("active-blocks", "blocks", "replace_blocks_by_prefixes", 3, True),
        ("active-binding", "bindings", "update_active_raid_binding", 1, True),
    ),
)
async def test_rotation_recovers_each_sqlite_reconciliation_checkpoint_after_rollback(
    migrated_connection: aiosqlite.Connection,
    checkpoint: str,
    repository_name: str,
    method_name: str,
    call_number: int,
    after_call: bool,
) -> None:
    """Проверяет recovery после physical rename и настоящего SQLite rollback."""

    existing = await _seed_rotation_recovery_database(migrated_connection)
    sheets = FakeSheetsClient(metadata_sheets=_rotation_metadata(*existing))
    binding = await RuntimeConfigRepository(migrated_connection).get_active_sheet_binding(-1001)
    assert binding is not None
    runtime = replace(
        _runtime(active_raid_season=OLD_RAID_SEASON_KEY),
        sheet_binding=binding,
    )
    repositories: dict[str, object] = {
        "states": RaidPlayerStateRepository(migrated_connection),
        "archives": RaidSheetArchiveRepository(migrated_connection),
        "blocks": SheetBlockRepository(migrated_connection),
        "bindings": SheetBindingRepository(migrated_connection),
    }
    failing = _FailingRepositoryProxy(
        repositories[repository_name],
        method_name=method_name,
        call_number=call_number,
        after_call=after_call,
    )
    repositories[repository_name] = failing

    with pytest.raises(RuntimeError, match="recovery checkpoint failed"):
        await apply_public_raid_sync(
            runtime_config=runtime,
            sheets_client=sheets,  # type: ignore[arg-type]
            raid_player_state_repository=repositories["states"],  # type: ignore[arg-type]
            raid_sheet_archive_repository=repositories["archives"],  # type: ignore[arg-type]
            sheet_block_repository=repositories["blocks"],  # type: ignore[arg-type]
            sheet_binding_repository=repositories["bindings"],  # type: ignore[arg-type]
            config=make_app_config(),
            prepared=_prepared_rotation_with_user_values(),
            sync_run_id=77,
        )
    await migrated_connection.rollback()

    canonical = tuple(
        sheet for sheet in sheets.metadata_sheets if sheet.title == RAID_ACTIVE_SHEET_NAME
    )
    assert len(canonical) == 1
    canonical_sheet_id = canonical[0].sheet_id
    assert canonical_sheet_id != 444
    await _assert_rotation_rollback_state(
        migrated_connection,
        canonical_sheet_id=canonical_sheet_id,
    )
    rolled_back = await RuntimeConfigRepository(migrated_connection).get_active_sheet_binding(-1001)
    assert rolled_back is not None
    assert checkpoint

    retry_runtime = replace(runtime, sheet_binding=rolled_back)
    result = await apply_public_raid_sync(
        runtime_config=retry_runtime,
        sheets_client=sheets,  # type: ignore[arg-type]
        raid_player_state_repository=RaidPlayerStateRepository(migrated_connection),
        raid_sheet_archive_repository=RaidSheetArchiveRepository(migrated_connection),
        sheet_block_repository=SheetBlockRepository(migrated_connection),
        sheet_binding_repository=SheetBindingRepository(migrated_connection),
        config=make_app_config(),
        prepared=_prepared_rotation_with_user_values(),
        sync_run_id=78,
    )

    assert result.sheet_id == canonical_sheet_id
    assert sheets.operation_log.count("atomic_rotation") == 1
    assert len(sheets.added_sheets) == 1
    await _assert_recovered_rotation_state(
        migrated_connection,
        canonical_sheet_id=canonical_sheet_id,
    )


@pytest.mark.asyncio
async def test_rotation_recovers_failure_after_binding_before_pruning_rollback(
    migrated_connection: aiosqlite.Connection,
) -> None:
    """Проверяет последний binding marker и retry pruning после rollback."""

    existing = await _seed_rotation_recovery_database(migrated_connection)
    error = RuntimeError("after binding before pruning failed")
    sheets = FakeSheetsClient(
        metadata_sheets=_rotation_metadata(*existing),
        fail_metadata_on_call=3,
        fail_metadata_error=error,
    )
    binding = await RuntimeConfigRepository(migrated_connection).get_active_sheet_binding(-1001)
    assert binding is not None
    runtime = replace(_runtime(active_raid_season=OLD_RAID_SEASON_KEY), sheet_binding=binding)

    with pytest.raises(RuntimeError) as caught:
        await apply_public_raid_sync(
            runtime_config=runtime,
            sheets_client=sheets,  # type: ignore[arg-type]
            raid_player_state_repository=RaidPlayerStateRepository(migrated_connection),
            raid_sheet_archive_repository=RaidSheetArchiveRepository(migrated_connection),
            sheet_block_repository=SheetBlockRepository(migrated_connection),
            sheet_binding_repository=SheetBindingRepository(migrated_connection),
            config=make_app_config(),
            prepared=_prepared_rotation_with_user_values(),
            sync_run_id=77,
        )
    assert caught.value is error
    await migrated_connection.rollback()

    canonical_sheet_id = next(
        sheet.sheet_id for sheet in sheets.metadata_sheets if sheet.title == RAID_ACTIVE_SHEET_NAME
    )
    assert any(
        sheet.sheet_id == 444 and sheet.title.startswith(RAID_ARCHIVE_SHEET_PREFIX)
        for sheet in sheets.metadata_sheets
    )
    await _assert_rotation_rollback_state(
        migrated_connection,
        canonical_sheet_id=canonical_sheet_id,
    )
    rolled_back = await RuntimeConfigRepository(migrated_connection).get_active_sheet_binding(-1001)
    assert rolled_back is not None
    sheets.fail_metadata_on_call = None
    sheets.fail_metadata_error = None

    result = await apply_public_raid_sync(
        runtime_config=replace(runtime, sheet_binding=rolled_back),
        sheets_client=sheets,  # type: ignore[arg-type]
        raid_player_state_repository=RaidPlayerStateRepository(migrated_connection),
        raid_sheet_archive_repository=RaidSheetArchiveRepository(migrated_connection),
        sheet_block_repository=SheetBlockRepository(migrated_connection),
        sheet_binding_repository=SheetBindingRepository(migrated_connection),
        config=make_app_config(),
        prepared=_prepared_rotation_with_user_values(),
        sync_run_id=78,
    )

    assert result.sheet_id == canonical_sheet_id
    assert sheets.operation_log.count("atomic_rotation") == 1
    assert len(sheets.added_sheets) == 1
    await _assert_recovered_rotation_state(
        migrated_connection,
        canonical_sheet_id=canonical_sheet_id,
    )
    archive_repository = RaidSheetArchiveRepository(migrated_connection)
    assert len(await archive_repository.list_ordered(-1001)) == 4
    assert (
        await archive_repository.get_by_season(
            chat_id=-1001,
            season_key=existing[0].season_key,
        )
        is None
    )
    assert sheets.deleted_sheet_ids == [existing[0].sheet_id]


@pytest.mark.asyncio
async def test_canonical_active_recovers_stale_binding_pointing_to_archive() -> None:
    """Проверяет canonical resolver и reconciliation partial rotation."""

    sheets = FakeSheetsClient(
        metadata_sheets=(
            SheetMetadata(sheet_id=444, title="Рейды 2026-07-17", index=0),
            SheetMetadata(sheet_id=445, title=RAID_ACTIVE_SHEET_NAME, index=1),
            SheetMetadata(sheet_id=222, title="CWL", index=2),
        ),
    )
    archives = RecordingRaidSheetArchiveRepository()
    bindings = RecordingSheetBindingRepository()

    result, sheets, _blocks, _states = await _apply_raid(
        runtime=_runtime(active_raid_season=OLD_RAID_SEASON_KEY),
        prepared=_prepared_raid_rotation(),
        sheets=sheets,
        archives=archives,
        bindings=bindings,
    )

    assert result.sheet_id == 445
    assert sheets.added_sheets == []
    assert sheets.operation_log.count("atomic_rotation") == 0
    assert archives.upserted_archives[-1].sheet_id == 444
    assert bindings.update_calls[-1]["active_raid_sheet_id"] == 445


@pytest.mark.asyncio
async def test_rotation_positions_active_before_physical_canonical_cwl() -> None:
    """Проверяет, что stale CWL archive binding не побеждает canonical CWL."""

    runtime = replace(
        _runtime(active_raid_season=OLD_RAID_SEASON_KEY),
        sheet_binding=make_sheet_binding(
            active_raid_season=OLD_RAID_SEASON_KEY,
            active_cwl_sheet_name="CWL 2026-07",
            active_cwl_sheet_id=222,
        ),
    )
    sheets = FakeSheetsClient(
        metadata_sheets=(
            SheetMetadata(sheet_id=444, title=RAID_ACTIVE_SHEET_NAME, index=0),
            SheetMetadata(sheet_id=222, title="CWL 2026-07", index=1),
            SheetMetadata(sheet_id=333, title="CWL", index=4),
        ),
    )

    await _apply_raid(
        runtime=runtime,
        prepared=_prepared_raid_rotation(),
        sheets=sheets,
    )

    atomic = next(
        batch
        for batch in sheets.spreadsheet_requests
        if any(item.get("updateSheetProperties", {}).get("fields") == "index" for item in batch)
    )
    move = next(
        item["updateSheetProperties"]
        for item in atomic
        if item.get("updateSheetProperties", {}).get("fields") == "index"
    )
    assert move["properties"]["index"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bound_title", "bound_index"),
    (
        ("CWL - staging - 77", 3),
        ("CWL 2026-07", 5),
        ("CWL copy", 6),
    ),
    ids=("staging", "archive", "similar-user-title"),
)
async def test_rotation_rejects_nonactive_bound_cwl_before_staging(
    bound_title: str,
    bound_index: int,
) -> None:
    """Проверяет запрет CWL staging/archive при отсутствии canonical."""

    runtime = replace(
        _runtime(active_raid_season=OLD_RAID_SEASON_KEY),
        sheet_binding=make_sheet_binding(
            active_raid_season=OLD_RAID_SEASON_KEY,
            active_cwl_sheet_name=bound_title,
            active_cwl_sheet_id=222,
        ),
    )
    sheets = FakeSheetsClient(
        metadata_sheets=(
            SheetMetadata(sheet_id=444, title=RAID_ACTIVE_SHEET_NAME, index=0),
            SheetMetadata(sheet_id=222, title=bound_title, index=bound_index),
        ),
    )

    with pytest.raises(RaidDataError, match="active CWL"):
        await _apply_raid(
            runtime=runtime,
            prepared=_prepared_raid_rotation(),
            sheets=sheets,
        )

    assert sheets.added_sheets == []
    assert sheets.batch_value_updates == []
    assert sheets.spreadsheet_requests == []
    assert sheets.hidden_dimensions == []
    assert sheets.operation_log == []


@pytest.mark.asyncio
async def test_rotation_rejects_ambiguous_exact_cwl_title_fallback_before_staging() -> None:
    """Проверяет ambiguity exact configured fallback без fuzzy ownership."""

    runtime = replace(
        _runtime(active_raid_season=OLD_RAID_SEASON_KEY),
        sheet_binding=make_sheet_binding(
            active_raid_season=OLD_RAID_SEASON_KEY,
            active_cwl_sheet_name="Лига",
            active_cwl_sheet_id=999,
        ),
    )
    sheets = FakeSheetsClient(
        metadata_sheets=(
            SheetMetadata(sheet_id=444, title=RAID_ACTIVE_SHEET_NAME, index=0),
            SheetMetadata(sheet_id=222, title="Лига", index=2),
            SheetMetadata(sheet_id=333, title="Лига", index=3),
        ),
    )

    with pytest.raises(RaidDataError, match=r"(?i)неоднознач"):
        await _apply_raid(
            runtime=runtime,
            prepared=_prepared_raid_rotation(),
            sheets=sheets,
        )

    assert sheets.added_sheets == []
    assert sheets.batch_value_updates == []
    assert sheets.spreadsheet_requests == []
    assert sheets.hidden_dimensions == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bound_title", "bound_id", "bound_index"),
    (
        ("CWL", 333, 4),
        ("Лига", 222, 3),
    ),
    ids=("canonical", "configured-bound-active"),
)
async def test_rotation_uses_exact_safe_cwl_target_index(
    bound_title: str,
    bound_id: int,
    bound_index: int,
) -> None:
    """Проверяет canonical и допустимый exact bound active без fuzzy matching."""

    runtime = replace(
        _runtime(active_raid_season=OLD_RAID_SEASON_KEY),
        sheet_binding=make_sheet_binding(
            active_raid_season=OLD_RAID_SEASON_KEY,
            active_cwl_sheet_name=bound_title,
            active_cwl_sheet_id=bound_id,
        ),
    )
    sheets = FakeSheetsClient(
        metadata_sheets=(
            SheetMetadata(sheet_id=444, title=RAID_ACTIVE_SHEET_NAME, index=0),
            SheetMetadata(sheet_id=bound_id, title=bound_title, index=bound_index),
        ),
    )

    await _apply_raid(
        runtime=runtime,
        prepared=_prepared_raid_rotation(),
        sheets=sheets,
    )

    atomic = next(
        batch
        for batch in sheets.spreadsheet_requests
        if any(item.get("updateSheetProperties", {}).get("fields") == "index" for item in batch)
    )
    move = next(
        item["updateSheetProperties"]
        for item in atomic
        if item.get("updateSheetProperties", {}).get("fields") == "index"
    )
    assert move["properties"] == {
        "sheetId": next(
            sheet.sheet_id
            for sheet in sheets.metadata_sheets
            if sheet.title == RAID_ACTIVE_SHEET_NAME
        ),
        "index": bound_index,
    }


@pytest.mark.asyncio
async def test_rotation_rejects_ambiguous_canonical_cwl_before_atomic_write() -> None:
    """Проверяет остановку неоднозначного canonical CWL до rename/move."""

    sheets = FakeSheetsClient(
        metadata_sheets=(
            SheetMetadata(sheet_id=444, title=RAID_ACTIVE_SHEET_NAME, index=0),
            SheetMetadata(sheet_id=222, title="CWL 2026-07", index=1),
            SheetMetadata(sheet_id=333, title="CWL", index=2),
            SheetMetadata(sheet_id=334, title="CWL", index=3),
        ),
    )

    with pytest.raises(RaidDataError, match=r"(?i)неоднознач"):
        await _apply_raid(
            runtime=_runtime(active_raid_season=OLD_RAID_SEASON_KEY),
            prepared=_prepared_raid_rotation(),
            sheets=sheets,
        )

    assert sheets.operation_log.count("atomic_rotation") == 0
    assert sheets.added_sheets == []
    assert sheets.batch_value_updates == []
    assert sheets.spreadsheet_requests == []
    assert sheets.hidden_dimensions == []


@pytest.mark.asyncio
async def test_staging_binding_is_never_resolved_as_active() -> None:
    """Проверяет, что leftover staging не становится active без canonical."""

    staging_title = "Рейды - staging - orphan"
    sheets = FakeSheetsClient(
        metadata_sheets=(
            SheetMetadata(sheet_id=444, title=staging_title, index=0),
            SheetMetadata(sheet_id=222, title="CWL", index=1),
        ),
    )
    bindings = RecordingSheetBindingRepository()

    result, sheets, _blocks, _states = await _apply_raid(
        runtime=_runtime(
            active_raid_season=RAID_SEASON_KEY,
            active_raid_sheet_name=staging_title,
            active_raid_sheet_id=444,
        ),
        prepared=_prepared_raid_apply(),
        sheets=sheets,
        bindings=bindings,
    )

    assert result.sheet_id != 444
    assert sheets.added_sheets == [RAID_ACTIVE_SHEET_NAME]
    assert any(
        sheet.sheet_id == 444 and sheet.title == staging_title for sheet in sheets.metadata_sheets
    )
    assert bindings.update_calls[-1]["active_raid_sheet_id"] == result.sheet_id


@pytest.mark.asyncio
async def test_initial_message_only_active_is_not_archived() -> None:
    """Проверяет отсутствие staging/archive без известного active season."""

    prepared = PreparedRaidSync(
        selected_season=None,
        previous_active_season=None,
        empty_blocks=(
            RaidClanBlock(
                clan_tag="#AAA111",
                clan_name="Alpha",
                message="Нет данных рейдового уикенда за доступный период",
            ),
        ),
    )
    sheets = FakeSheetsClient(metadata_sheets=_rotation_metadata())
    archives = RecordingRaidSheetArchiveRepository()
    bindings = RecordingSheetBindingRepository()

    result, sheets, _blocks, _states = await _apply_raid(
        runtime=_runtime(active_raid_season=None),
        prepared=prepared,
        sheets=sheets,
        archives=archives,
        bindings=bindings,
    )

    assert result.season_key is None
    assert result.archived_previous_season is False
    assert sheets.added_sheets == []
    assert archives.upserted_archives == []
    assert bindings.update_calls == []


@pytest.mark.asyncio
async def test_sheets_client_delete_sheet_uses_only_numeric_sheet_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверяет точный deleteSheet request без title/prefix."""

    client = object.__new__(SheetsClient)
    calls: list[list[JsonObject]] = []

    async def capture(requests: list[JsonObject]) -> JsonObject:
        calls.append(requests)
        return {}

    monkeypatch.setattr(client, "batch_update_spreadsheet", capture)

    await client.delete_sheet(501)

    assert calls == [[{"deleteSheet": {"sheetId": 501}}]]
    with pytest.raises(GoogleSheetsWriteError):
        await client.delete_sheet(True)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("sheet_id", (True, "501", -1, None))
async def test_delete_sheet_contract_rejects_same_invalid_ids_in_client_and_fake(
    monkeypatch: pytest.MonkeyPatch,
    sheet_id: object,
) -> None:
    """Проверяет одинаково строгую validation production и Fake Sheets."""

    client = object.__new__(SheetsClient)

    async def capture(_requests: list[JsonObject]) -> JsonObject:
        raise AssertionError("invalid sheet_id must fail before request")

    monkeypatch.setattr(client, "batch_update_spreadsheet", capture)
    fake = FakeSheetsClient()

    with pytest.raises(GoogleSheetsWriteError):
        await client.delete_sheet(sheet_id)  # type: ignore[arg-type]
    with pytest.raises(GoogleSheetsWriteError):
        await fake.delete_sheet(sheet_id)  # type: ignore[arg-type]
    assert fake.deleted_sheet_ids == []


@pytest.mark.asyncio
@pytest.mark.parametrize("sheet_id", (0, 501))
async def test_delete_sheet_contract_accepts_same_valid_ids_in_client_and_fake(
    monkeypatch: pytest.MonkeyPatch,
    sheet_id: int,
) -> None:
    """Проверяет совпадающий допустимый домен physical sheet IDs."""

    client = object.__new__(SheetsClient)
    calls: list[list[JsonObject]] = []

    async def capture(requests: list[JsonObject]) -> JsonObject:
        calls.append(requests)
        return {}

    monkeypatch.setattr(client, "batch_update_spreadsheet", capture)
    fake = FakeSheetsClient(
        metadata_sheets=(SheetMetadata(sheet_id=sheet_id, title="Owned", index=0),),
    )

    await client.delete_sheet(sheet_id)
    await fake.delete_sheet(sheet_id)

    assert calls == [[{"deleteSheet": {"sheetId": sheet_id}}]]
    assert fake.deleted_sheet_ids == [sheet_id]
