"""Строгий разбор и доменный расчёт рейдовых сезонов."""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Literal

from clash_sheet_sync_bot.models import AppConfig, TrackedClan, normalize_tag
from clash_sheet_sync_bot.repositories.raid_state import RaidPlayerState

CAPITAL_PEAK_DISTRICT_ID: Final = 70_000_000
CAPITAL_PEAK_NAME: Final = "Capital Peak"

RaidSeasonState = Literal["ongoing", "ended"]
RaidDistrictKind = Literal["normal", "capital"]
JsonObject = dict[str, Any]


class RaidDataError(RuntimeError):
    """Базовая ошибка обязательных рейдовых данных."""


class RaidContractError(RaidDataError):
    """Строгая ошибка контракта завершённого сезона."""


class RaidRetryableDataError(RaidDataError):
    """Временная ошибка ongoing-сезона, для которой допустим повтор sync."""


@dataclass(frozen=True, slots=True)
class RaidMember:
    """Участник рейдового сезона из CoC API."""

    player_tag: str
    player_name: str
    attacks: int
    attack_limit: int
    bonus_attack_limit: int
    capital_resources_looted: int


@dataclass(frozen=True, slots=True)
class RaidAttack:
    """Одна проверенная атака рейдового сезона."""

    player_tag: str
    district_kind: RaidDistrictKind
    destruction_percent: int


@dataclass(frozen=True, slots=True)
class ParsedRaidSeason:
    """Проверенный сезон до доменной агрегации."""

    clan_tag: str
    state: RaidSeasonState
    start_time: str
    end_time: str
    members: tuple[RaidMember, ...]
    attacks: tuple[RaidAttack, ...]


@dataclass(frozen=True, slots=True)
class RaidTechnicalValues:
    """Агрегированные технические значения одного участника."""

    player_tag: str
    player_name: str
    attacks: int
    attack_limit: int
    bonus_attack_limit: int
    capital_resources_looted: int
    weighted_damage_units: int
    normal_points: Decimal
    coefficient: Decimal


@dataclass(frozen=True, slots=True)
class RaidPlannedRow:
    """Полностью подготовленная строка рейдового листа."""

    row_key: str
    season_key: str
    clan_tag: str
    rank: int
    technical_values: RaidTechnicalValues
    user_values: dict[str, str]


@dataclass(frozen=True, slots=True)
class RaidClanBlock:
    """Подготовленный клановый data- или message-block."""

    clan_tag: str
    clan_name: str
    rows: tuple[RaidPlannedRow, ...] = ()
    message: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedRaidSync:
    """Результат raid preparation без каких-либо write-операций."""

    season_key: str | None
    season_state: RaidSeasonState | None
    blocks: tuple[RaidClanBlock, ...]
    warnings: tuple[str, ...] = ()
    diff: tuple[str, ...] = ()


async def prepare_public_raid_sync(
    *,
    clans: Sequence[TrackedClan],
    clash_client: Any,
    config: AppConfig,
    saved_rows: Sequence[RaidPlayerState] = (),
    imported_user_values: Mapping[str, Mapping[str, str]] | None = None,
    composition_user_values: Mapping[str, Mapping[str, str]] | None = None,
) -> PreparedRaidSync:
    """Подготавливает общий raid season всех активных кланов до Sheets write."""

    semaphore = asyncio.Semaphore(config.raid_api_concurrency_limit)

    async def load(clan: TrackedClan) -> tuple[str, list[JsonObject]]:
        async with semaphore:
            items = await clash_client.get_capital_raid_seasons(
                clan.clan_tag,
                limit=config.raid_season_fetch_limit,
            )
        return normalize_tag(clan.clan_tag), items

    windows = dict(await asyncio.gather(*(load(clan) for clan in clans)))
    ongoing_keys = {
        _season_key(_required_non_empty_str(item, "startTime", "raid season"))
        for items in windows.values()
        for item in items
        if item.get("state") == "ongoing"
    }
    if len(ongoing_keys) > 1:
        raise RaidContractError("Активные кланы вернули разные ongoing raid seasons.")
    api_keys = {
        _season_key(_required_non_empty_str(item, "startTime", "raid season"))
        for items in windows.values()
        for item in items
        if item.get("state") in {"ongoing", "ended"}
    }
    selected_key = next(iter(ongoing_keys), max(api_keys, default=None))
    if selected_key is None and saved_rows:
        selected_key = max(row.season_key for row in saved_rows)

    imported = imported_user_values or {}
    composition = composition_user_values or {}
    saved_by_key = {row.row_key: row for row in saved_rows}
    blocks: list[RaidClanBlock] = []
    warnings: list[str] = []
    selected_state: RaidSeasonState | None = None

    for clan in clans:
        clan_tag = normalize_tag(clan.clan_tag)
        raw = next(
            (
                item
                for item in windows.get(clan_tag, [])
                if _season_key(_required_non_empty_str(item, "startTime", "raid season"))
                == selected_key
            ),
            None,
        )
        technical_rows: tuple[RaidTechnicalValues, ...] = ()
        if raw is not None:
            parsed = parse_raid_season(raw, clan_tag=clan_tag)
            selected_state = parsed.state
            technical_rows = aggregate_raid_season(
                parsed,
                attacks_target=config.raid_attacks_target,
                normal_district_attack_norm=config.raid_normal_district_attack_norm,
                capital_district_attack_norm=config.raid_capital_district_attack_norm,
            )
        elif selected_key is not None:
            stored = [
                row
                for row in saved_rows
                if row.season_key == selected_key and normalize_tag(row.clan_tag) == clan_tag
            ]
            technical_rows = tuple(_technical_from_state(row) for row in stored)
            if stored:
                selected_state = _stored_state(stored[0].season_state)
                warnings.append(f"{clan_tag}: использовано сохранённое состояние рейдов.")

        if not technical_rows:
            blocks.append(
                RaidClanBlock(
                    clan_tag=clan_tag,
                    clan_name=clan.clan_name,
                    message="Нет данных рейдового уикенда",
                )
            )
            continue
        planned = [
            _planned_row(
                values,
                season_key=selected_key or "",
                clan_tag=clan_tag,
                imported=imported,
                saved_by_key=saved_by_key,
                composition=composition,
            )
            for values in technical_rows
        ]
        planned.sort(
            key=lambda row: (
                -row.technical_values.coefficient,
                -row.technical_values.attacks,
                row.technical_values.player_name.casefold(),
                row.technical_values.player_tag,
            )
        )
        ranked = tuple(
            RaidPlannedRow(
                row_key=row.row_key,
                season_key=row.season_key,
                clan_tag=row.clan_tag,
                rank=index,
                technical_values=row.technical_values,
                user_values=row.user_values,
            )
            for index, row in enumerate(planned, start=1)
        )
        blocks.append(RaidClanBlock(clan_tag, clan.clan_name, ranked))

    diff = tuple(
        f"{block.clan_tag}: {len(block.rows)} участников" for block in blocks if block.rows
    )
    return PreparedRaidSync(selected_key, selected_state, tuple(blocks), tuple(warnings), diff)


def make_raid_row_key(season_key: str, clan_tag: str, player_tag: str) -> str:
    """Строит стабильный ключ raid row."""

    return f"raid_row:{season_key}|{normalize_tag(clan_tag)}|{normalize_tag(player_tag)}"


def classify_raid_district(
    district_id: int,
    district_name: str | None,
) -> RaidDistrictKind:
    """Классифицирует район по стабильному API ID.

    Args:
        district_id: Числовой идентификатор района.
        district_name: Необязательное имя района из API.

    Returns:
        `capital` для подтверждённого Capital Peak ID, иначе `normal`.

    Raises:
        RaidContractError: Если ID имеет неверный тип или имя Capital Peak
            противоречит подтверждённому ID.
    """

    if not _is_int(district_id):
        raise RaidContractError("district: поле id должно быть целым числом.")
    if (
        district_name is not None
        and district_name.strip().casefold() == CAPITAL_PEAK_NAME.casefold()
        and district_id != CAPITAL_PEAK_DISTRICT_ID
    ):
        raise RaidContractError(
            "district Capital Peak содержит неподтверждённый id "
            f"{district_id}; ожидается {CAPITAL_PEAK_DISTRICT_ID}.",
        )
    if district_id == CAPITAL_PEAK_DISTRICT_ID:
        return "capital"
    return "normal"


def parse_raid_season(data: JsonObject, *, clan_tag: str) -> ParsedRaidSeason:
    """Строго разбирает один используемый raid season object.

    Районы с `attackCount = 0` реальный API может вернуть без поля `attacks`;
    они не содержат вклада и пропускаются. Для района с атаками список
    `attacks` обязателен.

    Args:
        data: JSON-объект одного выбранного сезона.
        clan_tag: Тег клана, которому принадлежит ответ.

    Returns:
        Проверенный сезон с нормализованными тегами и плоским списком атак.

    Raises:
        RaidContractError: Если обязательный контракт нарушен.
        RaidRetryableDataError: Если ongoing-сезон временно не согласовал
            счётчики участников с attack log.
    """

    if not isinstance(data, dict):
        raise RaidContractError("raid season должен быть объектом.")
    normalized_clan_tag = _normalize_api_tag(clan_tag, "raid season clan")
    state = _season_state(data)
    start_time = _required_non_empty_str(data, "startTime", "raid season")
    end_time = _required_non_empty_str(data, "endTime", "raid season")
    members = _parse_members(_required_list(data, "members", "raid season"))
    attacks = _parse_attack_log(_required_list(data, "attackLog", "raid season"))
    _validate_attack_counters(state=state, members=members, attacks=attacks)
    return ParsedRaidSeason(
        clan_tag=normalized_clan_tag,
        state=state,
        start_time=start_time,
        end_time=end_time,
        members=members,
        attacks=attacks,
    )


def aggregate_raid_season(
    season: ParsedRaidSeason,
    *,
    attacks_target: int,
    normal_district_attack_norm: int,
    capital_district_attack_norm: int,
) -> tuple[RaidTechnicalValues, ...]:
    """Агрегирует атаки сезона до технических значений участников.

    Args:
        season: Проверенный рейдовый сезон.
        attacks_target: Знаменатель коэффициента.
        normal_district_attack_norm: Множитель обычного района.
        capital_district_attack_norm: Множитель Capital Peak.

    Returns:
        Строки участников в исходном порядке `members`.

    Raises:
        ValueError: Если норма или целевое число атак не положительны.
    """

    _validate_positive_int(attacks_target, "attacks_target")
    _validate_positive_int(normal_district_attack_norm, "normal_district_attack_norm")
    _validate_positive_int(capital_district_attack_norm, "capital_district_attack_norm")

    weighted_units: defaultdict[str, int] = defaultdict(int)
    for attack in season.attacks:
        multiplier = (
            capital_district_attack_norm
            if attack.district_kind == "capital"
            else normal_district_attack_norm
        )
        weighted_units[attack.player_tag] += attack.destruction_percent * multiplier

    coefficient_denominator = 100 * attacks_target
    return tuple(
        RaidTechnicalValues(
            player_tag=member.player_tag,
            player_name=member.player_name,
            attacks=member.attacks,
            attack_limit=member.attack_limit,
            bonus_attack_limit=member.bonus_attack_limit,
            capital_resources_looted=member.capital_resources_looted,
            weighted_damage_units=weighted_units[member.player_tag],
            normal_points=Decimal(weighted_units[member.player_tag]) / Decimal(100),
            coefficient=Decimal(weighted_units[member.player_tag])
            / Decimal(coefficient_denominator),
        )
        for member in season.members
    )


def _season_key(value: str) -> str:
    parsed = datetime.strptime(value, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=UTC)
    return parsed.isoformat()


def _planned_row(
    values: RaidTechnicalValues,
    *,
    season_key: str,
    clan_tag: str,
    imported: Mapping[str, Mapping[str, str]],
    saved_by_key: Mapping[str, RaidPlayerState],
    composition: Mapping[str, Mapping[str, str]],
) -> RaidPlannedRow:
    row_key = make_raid_row_key(season_key, clan_tag, values.player_tag)
    if row_key in imported:
        user_values = dict(imported[row_key])
    elif row_key in saved_by_key:
        user_values = dict(saved_by_key[row_key].user_values)
    else:
        user_values = {}
    for key, value in composition.get(values.player_tag, {}).items():
        if user_values.get(key, "").strip() == "" and value.strip() != "":
            user_values[key] = value
    return RaidPlannedRow(row_key, season_key, clan_tag, 0, values, user_values)


def _technical_from_state(state: RaidPlayerState) -> RaidTechnicalValues:
    data = state.technical_values
    return RaidTechnicalValues(
        player_tag=state.player_tag,
        player_name=str(data["player_name"]),
        attacks=int(data["attacks"]),
        attack_limit=int(data["attack_limit"]),
        bonus_attack_limit=int(data["bonus_attack_limit"]),
        capital_resources_looted=int(data["capital_resources_looted"]),
        weighted_damage_units=int(data["weighted_damage_units"]),
        normal_points=Decimal(str(data["normal_points"])),
        coefficient=Decimal(str(data["coefficient"])),
    )


def _stored_state(value: str) -> RaidSeasonState:
    if value not in {"ongoing", "ended"}:
        raise RaidContractError(f"Некорректный сохранённый raid state: {value}.")
    return value


def _parse_members(raw_members: list[Any]) -> tuple[RaidMember, ...]:
    """Разбирает и проверяет список участников сезона."""

    members: list[RaidMember] = []
    seen_tags: set[str] = set()
    for index, raw_member in enumerate(raw_members, start=1):
        context = f"raid member #{index}"
        member = _required_dict_item(raw_member, context)
        player_tag = _normalize_api_tag(
            _required_non_empty_str(member, "tag", context),
            context,
        )
        if player_tag in seen_tags:
            raise RaidContractError(f"{context}: повторяющийся tag {player_tag}.")
        seen_tags.add(player_tag)
        members.append(
            RaidMember(
                player_tag=player_tag,
                player_name=_required_non_empty_str(member, "name", context),
                attacks=_required_non_negative_int(member, "attacks", context),
                attack_limit=_required_non_negative_int(member, "attackLimit", context),
                bonus_attack_limit=_required_non_negative_int(
                    member,
                    "bonusAttackLimit",
                    context,
                ),
                capital_resources_looted=_required_non_negative_int(
                    member,
                    "capitalResourcesLooted",
                    context,
                ),
            ),
        )
    return tuple(members)


def _parse_attack_log(raw_logs: list[Any]) -> tuple[RaidAttack, ...]:
    """Разбирает все фактически выполненные атаки сезона."""

    attacks: list[RaidAttack] = []
    for log_index, raw_log in enumerate(raw_logs, start=1):
        log_context = f"raid attack log #{log_index}"
        log = _required_dict_item(raw_log, log_context)
        raw_districts = _required_list(log, "districts", log_context)
        for district_index, raw_district in enumerate(raw_districts, start=1):
            district_context = f"{log_context} district #{district_index}"
            district = _required_dict_item(raw_district, district_context)
            district_id = _required_int(district, "id", district_context)
            district_name = _optional_str(district, "name", district_context)
            district_kind = classify_raid_district(district_id, district_name)
            raw_attacks = _district_attacks(district, district_context)
            for attack_index, raw_attack in enumerate(raw_attacks, start=1):
                attack_context = f"{district_context} attack #{attack_index}"
                attack = _required_dict_item(raw_attack, attack_context)
                attacker = _required_dict(attack, "attacker", attack_context)
                player_tag = _normalize_api_tag(
                    _required_non_empty_str(attacker, "tag", f"{attack_context} attacker"),
                    f"{attack_context} attacker",
                )
                destruction_percent = _required_int(
                    attack,
                    "destructionPercent",
                    attack_context,
                )
                if not 0 <= destruction_percent <= 100:
                    raise RaidContractError(
                        f"{attack_context}: destructionPercent должен быть в диапазоне 0..100.",
                    )
                attacks.append(
                    RaidAttack(
                        player_tag=player_tag,
                        district_kind=district_kind,
                        destruction_percent=destruction_percent,
                    ),
                )
    return tuple(attacks)


def _district_attacks(district: JsonObject, context: str) -> list[Any]:
    """Читает атаки района с учётом реального zero-attack представления API."""

    raw_attacks = district.get("attacks")
    if isinstance(raw_attacks, list):
        return raw_attacks
    if raw_attacks is not None:
        raise RaidContractError(f"{context}: поле attacks должно быть списком.")

    attack_count = district.get("attackCount")
    if _is_int(attack_count) and attack_count == 0:
        return []
    raise RaidContractError(f"{context}: отсутствует обязательное поле attacks.")


def _validate_attack_counters(
    *,
    state: RaidSeasonState,
    members: tuple[RaidMember, ...],
    attacks: tuple[RaidAttack, ...],
) -> None:
    """Сверяет member counters с разобранными атаками."""

    expected = Counter({member.player_tag: member.attacks for member in members})
    actual = Counter(attack.player_tag for attack in attacks)
    if expected == actual:
        return

    message = (
        "Количество атак в members не совпадает с raid attack log: "
        f"ожидалось {sum(expected.values())}, разобрано {sum(actual.values())}."
    )
    if state == "ongoing":
        raise RaidRetryableDataError(f"{message} Повторите /sync позже.")
    raise RaidContractError(message)


def _season_state(data: JsonObject) -> RaidSeasonState:
    """Читает поддерживаемое состояние сезона."""

    state = _required_non_empty_str(data, "state", "raid season")
    if state == "ongoing":
        return "ongoing"
    if state == "ended":
        return "ended"
    raise RaidContractError(f"raid season: неподдерживаемое состояние {state!r}.")


def _required_non_empty_str(data: JsonObject, key: str, context: str) -> str:
    """Читает обязательную непустую строку."""

    value = data.get(key)
    if not isinstance(value, str) or value.strip() == "":
        raise RaidContractError(f"{context}: поле {key} должно быть непустой строкой.")
    return value


def _optional_str(data: JsonObject, key: str, context: str) -> str | None:
    """Читает необязательную строку."""

    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RaidContractError(f"{context}: поле {key} должно быть строкой.")
    return value


def _required_int(data: JsonObject, key: str, context: str) -> int:
    """Читает обязательное целое число, исключая bool."""

    value = data.get(key)
    if not _is_int(value):
        raise RaidContractError(f"{context}: поле {key} должно быть целым числом.")
    return value


def _required_non_negative_int(data: JsonObject, key: str, context: str) -> int:
    """Читает обязательное неотрицательное целое число."""

    value = _required_int(data, key, context)
    if value < 0:
        raise RaidContractError(f"{context}: поле {key} не может быть отрицательным.")
    return value


def _required_list(data: JsonObject, key: str, context: str) -> list[Any]:
    """Читает обязательный список."""

    value = data.get(key)
    if not isinstance(value, list):
        raise RaidContractError(f"{context}: поле {key} должно быть списком.")
    return value


def _required_dict(data: JsonObject, key: str, context: str) -> JsonObject:
    """Читает обязательный вложенный объект."""

    value = data.get(key)
    if not isinstance(value, dict):
        raise RaidContractError(f"{context}: поле {key} должно быть объектом.")
    return value


def _required_dict_item(value: Any, context: str) -> JsonObject:
    """Проверяет объект внутри внешнего списка."""

    if not isinstance(value, dict):
        raise RaidContractError(f"{context} должен быть объектом.")
    return value


def _normalize_api_tag(value: str, context: str) -> str:
    """Нормализует тег или преобразует ошибку в доменную."""

    try:
        return normalize_tag(value)
    except ValueError as exc:
        raise RaidContractError(f"{context}: некорректный tag.") from exc


def _validate_positive_int(value: int, name: str) -> None:
    """Проверяет положительный целочисленный аргумент формулы."""

    if not _is_int(value) or value <= 0:
        raise ValueError(f"{name} должен быть положительным целым числом.")


def _is_int(value: object) -> bool:
    """Проверяет строгий int без принятия bool."""

    return isinstance(value, int) and not isinstance(value, bool)
