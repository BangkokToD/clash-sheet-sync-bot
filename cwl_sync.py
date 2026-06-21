"""Синхронизация листа CWL Clash of Clans."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from coc_client import ClashApiUnavailableError, ClashClient, ClashCwlNotInProgressError
from composition_sync import CompositionDataError, parse_old_composition
from config import AppConfig
from models import ClanConfig, normalize_tag
from sheets_client import CellValue, SheetValues, SheetsClient

logger = logging.getLogger(__name__)

CWL_HEADERS: Final = (
    "Раунд",
    "Тег",
    "Ник",
    "Юзернейм",
    "ТХ",
    "ТХ соперника",
    "Звезды",
    "Процент разрушений",
    "Идея1",
    "Реализация1",
    "Ожидания1",
    "Комментарий1",
    "Идея2",
    "Реализация2",
    "Ожидания2",
    "Комментарий2",
    "Итоговая оценка",
)
CWL_HEADER_ALIASES: Final = {
    "ТХ": {"ТХ", "ТХ - номер"},
    "ТХ соперника": {"ТХ соперника", "ТХ соперника - номер"},
    "Ожидания2": {"Ожидания2", "Ожидания3"},
}
CWL_USER_COLUMNS_START: Final = 8
CWL_WAR_CONCURRENCY_LIMIT: Final = 5
NO_ATTACK_MARKER: Final = "NO_ATTACK"
ATTACK_MARKER_PREFIX: Final = "ATTACK"
CWL_ELIGIBLE_WAR_STATES: Final = {"warEnded", "inWar"}

A1_CELL_RE: Final = re.compile(r"^\$?([A-Za-z]+)\$?([1-9][0-9]*)$")
A1_RANGE_RE: Final = re.compile(
    r"^\$?([A-Za-z]+)\$?([1-9][0-9]*):\$?([A-Za-z]+)\$?([1-9][0-9]*)$",
)
DEFENDER_POSITION_RE: Final = re.compile(r"^\s*(\d+)\s+")

GREEN_RGB: Final = {"red": 0.18, "green": 0.42, "blue": 0.31}
DARK_GREEN_RGB: Final = {"red": 0.12, "green": 0.32, "blue": 0.24}
WHITE_RGB: Final = {"red": 1.0, "green": 1.0, "blue": 1.0}
BLACK_RGB: Final = {"red": 0.0, "green": 0.0, "blue": 0.0}
LIGHT_BAND_RGB: Final = {"red": 0.95, "green": 0.97, "blue": 0.96}
BORDER_RGB: Final = {"red": 0.70, "green": 0.76, "blue": 0.73}

JsonObject = dict[str, Any]


class CwlSyncError(RuntimeError):
    """Базовая ошибка синхронизации CWL."""


class CwlDataError(CwlSyncError):
    """Ошибка данных CWL, при которой лист нельзя менять."""


@dataclass(frozen=True, slots=True)
class CwlUserFields:
    """Пользовательские поля строки CWL.

    Attributes:
        idea1: Оценка идеи первой атаки.
        realization1: Оценка реализации первой атаки.
        expectations1: Оценка ожиданий первой атаки.
        comment1: Комментарий первой атаки.
        idea2: Оценка идеи второй атаки.
        realization2: Оценка реализации второй атаки.
        expectations2: Оценка ожиданий второй атаки.
        comment2: Комментарий второй атаки.
        final_score: Итоговая оценка.
    """

    idea1: str = ""
    realization1: str = ""
    expectations1: str = ""
    comment1: str = ""
    idea2: str = ""
    realization2: str = ""
    expectations2: str = ""
    comment2: str = ""
    final_score: str = ""

    def to_values(self) -> list[CellValue]:
        """Преобразует пользовательские поля в список значений.

        Returns:
            Значения пользовательских колонок CWL.
        """

        return [
            self.idea1,
            self.realization1,
            self.expectations1,
            self.comment1,
            self.idea2,
            self.realization2,
            self.expectations2,
            self.comment2,
            self.final_score,
        ]


@dataclass(slots=True)
class CwlRow:
    """Строка новой CWL-таблицы.

    Attributes:
        round_number: Номер раунда.
        attacker_tag: Тег атакующего.
        attacker_name: Ник атакующего.
        username: Username с листа состава.
        attacker_position: Номер атакующего на карте.
        attacker_town_hall: TH атакующего.
        defender_position: Номер цели на карте соперника.
        defender_town_hall: TH цели.
        stars: Количество звёзд или пустая строка.
        destruction_percentage: Процент разрушений или пустая строка.
        sync_key: Стабильный ключ строки.
        no_attack_key: Ключ строки без атаки для fallback-переноса.
        user_fields: Пользовательские поля.
        sort_key: Ключ сортировки строки.
    """

    round_number: int
    attacker_tag: str
    attacker_name: str
    username: str
    attacker_position: int
    attacker_town_hall: int
    defender_position: int | None
    defender_town_hall: int | None
    stars: int | str
    destruction_percentage: int | str
    sync_key: str
    no_attack_key: str
    user_fields: CwlUserFields
    sort_key: tuple[int, int, int]

    @property
    def is_attack(self) -> bool:
        """Проверяет, является ли строка атакой.

        Returns:
            `True`, если у строки есть цель атаки.
        """

        return self.defender_position is not None

    def to_values(self) -> list[CellValue]:
        """Преобразует строку CWL в значения Google Sheets.

        Returns:
            Строка значений CWL.
        """

        defender_value = ""
        if self.defender_position is not None and self.defender_town_hall is not None:
            defender_value = format_town_hall(self.defender_town_hall)

        return [
            self.round_number,
            self.attacker_tag,
            self.attacker_name,
            self.username,
            format_town_hall(self.attacker_town_hall),
            defender_value,
            self.stars,
            self.destruction_percentage,
            *self.user_fields.to_values(),
        ]


@dataclass(frozen=True, slots=True)
class CwlClanBlock:
    """Блок CWL для одного клана.

    Attributes:
        clan: Конфигурация клана.
        rows: Строки CWL.
        message: Служебная строка вместо таблицы.
        rounds_count: Количество включённых раундов.
    """

    clan: ClanConfig
    rows: tuple[CwlRow, ...]
    message: str | None = None
    rounds_count: int = 0


@dataclass(frozen=True, slots=True)
class CwlClanReport:
    """Отчёт по CWL одного клана.

    Attributes:
        clan_name: Название клана.
        rounds_count: Количество раундов.
        rows_count: Количество строк.
        attacks_count: Количество сыгранных атак.
        no_attack_count: Количество строк без атаки.
    """

    clan_name: str
    rounds_count: int
    rows_count: int
    attacks_count: int
    no_attack_count: int


@dataclass(frozen=True, slots=True)
class CwlSyncResult:
    """Результат синхронизации CWL.

    Attributes:
        all_not_in_progress: CWL не проводится у всех кланов.
        reports: Отчёты по участвующим кланам.
        not_in_progress_clans: Кланы, у которых CWL не проводится.
    """

    all_not_in_progress: bool
    reports: tuple[CwlClanReport, ...] = ()
    not_in_progress_clans: tuple[ClanConfig, ...] = ()

    def to_telegram_message(self) -> str:
        """Формирует Telegram-отчёт CWL.

        Returns:
            Текст отчёта.
        """

        if self.all_not_in_progress:
            return "CWL не проводится."

        lines = ["CWL обновлена."]

        for report in self.reports:
            lines.extend(
                [
                    "",
                    f"{report.clan_name}:",
                    f"Раундов: {report.rounds_count}",
                    f"Строк: {report.rows_count}",
                    f"Атак сыграно: {report.attacks_count}",
                    f"Без атаки: {report.no_attack_count}",
                ],
            )

        if self.not_in_progress_clans:
            lines.extend(["", "Не проводится:"])
            lines.extend(
                f"- {clan.name} | {clan.tag}"
                for clan in self.not_in_progress_clans
            )

        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class CwlTableSpec:
    """Описание форматируемого блока CWL.

    Attributes:
        name: Название блока.
        start_cell: Левая верхняя ячейка блока.
        rows_count: Количество строк блока.
        columns: Колонки таблицы.
        has_table: Есть ли табличный заголовок и строки данных.
    """

    name: str
    start_cell: str
    rows_count: int
    columns: tuple[str, ...]
    has_table: bool


@dataclass(frozen=True, slots=True)
class OldCwlTableHeader:
    """Заголовок старой CWL-таблицы.

    Attributes:
        row_index: Индекс строки заголовка.
        column_index: Индекс первой колонки.
        clan_tag: Тег семейного клана.
    """

    row_index: int
    column_index: int
    clan_tag: str | None


async def run_cwl_sync(
    config: AppConfig,
    clash_client: ClashClient,
    sheets_client: SheetsClient,
) -> CwlSyncResult:
    """Выполняет полную синхронизацию листа `CWL`.

    Args:
        config: Конфигурация приложения.
        clash_client: Клиент Clash of Clans API.
        sheets_client: Клиент Google Sheets API.

    Returns:
        Результат синхронизации CWL.

    Raises:
        CwlDataError: Если данные CWL противоречат ТЗ.
        ClashApiUnavailableError: Если Clash API недоступен.
        GoogleSheetsError: Если Google Sheets недоступен.
    """

    league_groups = await _load_league_groups(config, clash_client)
    participating_groups = {
        clan_tag: group
        for clan_tag, group in league_groups.items()
        if group is not None
    }

    not_in_progress_clans = tuple(
        clan for clan in config.clans if league_groups.get(clan.tag) is None
    )
    if not participating_groups:
        return CwlSyncResult(all_not_in_progress=True)

    season = _resolve_cwl_season(participating_groups)
    war_tags = _collect_unique_war_tags(participating_groups.values())
    wars_by_tag = await _load_cwl_wars(clash_client, war_tags)

    old_cwl_values = await sheets_client.read_values(
        config.cwl_sheet_name,
        config.cwl_managed_range,
    )
    composition_values = await sheets_client.read_values(
        config.composition_sheet_name,
        config.composition_managed_range,
    )

    old_user_fields = parse_old_cwl_user_fields(
        old_cwl_values,
        clans=config.clans,
    )
    usernames_by_tag = _read_composition_usernames(composition_values, config)

    blocks = _build_cwl_blocks(
        config=config,
        season=season,
        league_groups=league_groups,
        wars_by_tag=wars_by_tag,
        usernames_by_tag=usernames_by_tag,
    )
    _apply_old_user_fields(blocks, old_user_fields)

    matrix, table_specs = _build_cwl_matrix(config, season, blocks)
    sheet_metadata = await sheets_client.get_sheet_metadata(config.cwl_sheet_name)

    await sheets_client.rewrite_managed_range(
        sheet_name=config.cwl_sheet_name,
        managed_range_a1=config.cwl_managed_range,
        updates=[
            SheetValues(
                sheet_name=config.cwl_sheet_name,
                range_a1=_range_from_start_cell(
                    config.cwl_start_cell,
                    rows_count=len(matrix),
                    columns_count=len(CWL_HEADERS),
                ),
                values=matrix,
            ),
        ],
    )
    await _format_cwl_tables(
        sheets_client=sheets_client,
        sheet_id=sheet_metadata.sheet_id,
        managed_range_a1=config.cwl_managed_range,
        table_specs=table_specs,
    )

    reports = tuple(
        _build_report(block)
        for block in blocks
        if block.message is None
    )

    return CwlSyncResult(
        all_not_in_progress=False,
        reports=reports,
        not_in_progress_clans=not_in_progress_clans,
    )


async def _load_league_groups(
    config: AppConfig,
    clash_client: ClashClient,
) -> dict[str, JsonObject | None]:
    """Загружает CWL leaguegroup для всех кланов.

    Args:
        config: Конфигурация приложения.
        clash_client: Клиент Clash of Clans API.

    Returns:
        Leaguegroup по тегу клана. `None` означает `CWL не проводится`.
    """

    groups: dict[str, JsonObject | None] = {}

    for clan in config.clans:
        try:
            groups[clan.tag] = await clash_client.get_current_war_league_group(clan.tag)
        except ClashCwlNotInProgressError:
            groups[clan.tag] = None

    return groups


def _resolve_cwl_season(groups: dict[str, JsonObject]) -> str:
    """Определяет единый сезон CWL.

    Args:
        groups: Участвующие leaguegroup по тегу клана.

    Returns:
        Сезон CWL.

    Raises:
        CwlDataError: Если участвующие кланы вернули разные сезоны.
    """

    seasons = {_require_str(group, "season", "leaguegroup") for group in groups.values()}
    if len(seasons) != 1:
        raise CwlDataError("участвующие кланы вернули разные сезоны CWL.")
    return next(iter(seasons))


def _collect_unique_war_tags(groups: Sequence[JsonObject]) -> list[str]:
    """Собирает уникальные CWL warTag.

    Args:
        groups: Leaguegroup участвующих кланов.

    Returns:
        Уникальные warTag без `#0` в порядке первого появления.
    """

    seen: set[str] = set()
    war_tags: list[str] = []

    for group in groups:
        for round_payload in _require_list(group, "rounds", "leaguegroup"):
            if not isinstance(round_payload, dict):
                raise ClashApiUnavailableError("leaguegroup содержит некорректный round.")

            raw_tags = _require_list(round_payload, "warTags", "leaguegroup round")
            for raw_tag in raw_tags:
                if not isinstance(raw_tag, str):
                    raise ClashApiUnavailableError("leaguegroup содержит некорректный warTag.")
                tag = normalize_tag(raw_tag)
                if tag == "#0" or tag in seen:
                    continue
                seen.add(tag)
                war_tags.append(tag)

    return war_tags


async def _load_cwl_wars(
    clash_client: ClashClient,
    war_tags: Sequence[str],
) -> dict[str, JsonObject]:
    """Загружает CWL wars с ограничением конкурентности.

    Args:
        clash_client: Клиент Clash of Clans API.
        war_tags: Уникальные warTag.

    Returns:
        Войны по warTag.
    """

    semaphore = asyncio.Semaphore(CWL_WAR_CONCURRENCY_LIMIT)

    async def load_one(war_tag: str) -> tuple[str, JsonObject]:
        """Загружает одну CWL-войну.

        Args:
            war_tag: Тег войны.

        Returns:
            Пара `(warTag, war)`.
        """

        async with semaphore:
            return war_tag, await clash_client.get_cwl_war(war_tag)

    loaded = await asyncio.gather(*(load_one(war_tag) for war_tag in war_tags))
    return dict(loaded)


def _read_composition_usernames(
    values: Sequence[Sequence[CellValue]],
    config: AppConfig,
) -> dict[str, str]:
    """Читает username игроков с листа состава.

    Args:
        values: Значения листа `Состав`.
        config: Конфигурация приложения.

    Returns:
        Username по player tag.

    Raises:
        CwlDataError: Если лист состава содержит дубли или повреждённые теги.
    """

    try:
        composition = parse_old_composition(values, clans=config.clans)
    except CompositionDataError as exc:
        raise CwlDataError(str(exc)) from exc

    return {
        tag: player.user_fields.username
        for tag, player in composition.players_by_tag.items()
    }


def _build_cwl_blocks(
    *,
    config: AppConfig,
    season: str,
    league_groups: dict[str, JsonObject | None],
    wars_by_tag: dict[str, JsonObject],
    usernames_by_tag: dict[str, str],
) -> list[CwlClanBlock]:
    """Строит CWL-блоки по кланам.

    Args:
        config: Конфигурация приложения.
        season: Сезон CWL.
        league_groups: Leaguegroup по тегу клана.
        wars_by_tag: Загруженные CWL wars.
        usernames_by_tag: Username по player tag.

    Returns:
        Блоки CWL в порядке кланов из `.env`.
    """

    blocks: list[CwlClanBlock] = []

    for clan in config.clans:
        group = league_groups.get(clan.tag)
        if group is None:
            blocks.append(CwlClanBlock(clan=clan, rows=(), message="CWL не проводится"))
            continue

        rows, rounds_count = _build_clan_rows(
            clan=clan,
            group=group,
            season=season,
            wars_by_tag=wars_by_tag,
            usernames_by_tag=usernames_by_tag,
        )

        if not rows:
            blocks.append(
                CwlClanBlock(
                    clan=clan,
                    rows=(),
                    message="Нет завершённых или текущих раундов",
                    rounds_count=0,
                ),
            )
            continue

        blocks.append(
            CwlClanBlock(
                clan=clan,
                rows=tuple(sorted(rows, key=lambda row: row.sort_key)),
                rounds_count=rounds_count,
            ),
        )

    return blocks


def _build_clan_rows(
    *,
    clan: ClanConfig,
    group: JsonObject,
    season: str,
    wars_by_tag: dict[str, JsonObject],
    usernames_by_tag: dict[str, str],
) -> tuple[list[CwlRow], int]:
    """Строит строки CWL одного клана.

    Args:
        clan: Конфигурация клана.
        group: Leaguegroup клана.
        season: Сезон CWL.
        wars_by_tag: Загруженные CWL wars.
        usernames_by_tag: Username по player tag.

    Returns:
        Строки CWL и количество включённых раундов.
    """

    rows: list[CwlRow] = []
    included_rounds: set[int] = set()

    rounds = _require_list(group, "rounds", "leaguegroup")
    for round_number, round_payload in enumerate(rounds, start=1):
        if not isinstance(round_payload, dict):
            raise ClashApiUnavailableError("leaguegroup содержит некорректный round.")

        raw_war_tags = _require_list(round_payload, "warTags", "leaguegroup round")
        for raw_tag in raw_war_tags:
            if not isinstance(raw_tag, str):
                raise ClashApiUnavailableError("leaguegroup содержит некорректный warTag.")

            war_tag = normalize_tag(raw_tag)
            if war_tag == "#0":
                continue

            war = wars_by_tag.get(war_tag)
            if war is None:
                continue

            state = _require_str(war, "state", f"war {war_tag}")
            if state not in CWL_ELIGIBLE_WAR_STATES:
                continue

            side = _extract_war_side(war, clan.tag)
            if side is None:
                continue

            included_rounds.add(round_number)
            rows.extend(
                _build_war_rows(
                    season=season,
                    clan_tag=clan.tag,
                    round_number=round_number,
                    our_side=side[0],
                    opponent_side=side[1],
                    usernames_by_tag=usernames_by_tag,
                ),
            )

    return rows, len(included_rounds)


def _extract_war_side(
    war: JsonObject,
    clan_tag: str,
) -> tuple[JsonObject, JsonObject] | None:
    """Определяет сторону семейного клана в войне.

    Args:
        war: CWL war из API.
        clan_tag: Тег семейного клана.

    Returns:
        Пара `(наш клан, соперник)` или `None`, если клан не участвует в войне.
    """

    clan_side = _require_dict(war, "clan", "cwl war")
    opponent_side = _require_dict(war, "opponent", "cwl war")

    api_clan_tag = normalize_tag(_require_str(clan_side, "tag", "war clan"))
    api_opponent_tag = normalize_tag(_require_str(opponent_side, "tag", "war opponent"))

    if api_clan_tag == clan_tag:
        return clan_side, opponent_side
    if api_opponent_tag == clan_tag:
        return opponent_side, clan_side
    return None


def _build_war_rows(
    *,
    season: str,
    clan_tag: str,
    round_number: int,
    our_side: JsonObject,
    opponent_side: JsonObject,
    usernames_by_tag: dict[str, str],
) -> list[CwlRow]:
    """Строит строки одной войны CWL.

    Args:
        season: Сезон CWL.
        clan_tag: Тег семейного клана.
        round_number: Номер раунда.
        our_side: JSON нашей стороны войны.
        opponent_side: JSON соперника.
        usernames_by_tag: Username по player tag.

    Returns:
        Строки CWL для одной войны.
    """

    opponent_members = _read_war_members(opponent_side, "opponent")
    opponent_by_tag = {member["tag"]: member for member in opponent_members}
    our_members = sorted(
        _read_war_members(our_side, "clan"),
        key=lambda member: (member["map_position"], member["tag"]),
    )

    rows: list[CwlRow] = []
    for member in our_members:
        attacks = member["attacks"]
        no_attack_key = make_cwl_key(
            season,
            clan_tag,
            round_number,
            member["tag"],
            NO_ATTACK_MARKER,
        )

        if not attacks:
            rows.append(
                CwlRow(
                    round_number=round_number,
                    attacker_tag=member["tag"],
                    attacker_name=member["name"],
                    username=usernames_by_tag.get(member["tag"], ""),
                    attacker_position=member["map_position"],
                    attacker_town_hall=member["town_hall"],
                    defender_position=None,
                    defender_town_hall=None,
                    stars="",
                    destruction_percentage="",
                    sync_key=no_attack_key,
                    no_attack_key=no_attack_key,
                    user_fields=CwlUserFields(),
                    sort_key=(round_number, member["map_position"], 0),
                ),
            )
            continue

        for attack_index, attack in enumerate(attacks, start=1):
            defender_tag = normalize_tag(_require_str(attack, "defenderTag", "attack"))
            defender = opponent_by_tag.get(defender_tag)
            if defender is None:
                raise ClashApiUnavailableError(
                    f"CWL war attack содержит неизвестный defenderTag {defender_tag}.",
                )

            defender_position = defender["map_position"]
            sync_key = make_cwl_key(
                season,
                clan_tag,
                round_number,
                member["tag"],
                f"{ATTACK_MARKER_PREFIX}_{attack_index}",
            )

            rows.append(
                CwlRow(
                    round_number=round_number,
                    attacker_tag=member["tag"],
                    attacker_name=member["name"],
                    username=usernames_by_tag.get(member["tag"], ""),
                    attacker_position=member["map_position"],
                    attacker_town_hall=member["town_hall"],
                    defender_position=defender_position,
                    defender_town_hall=defender["town_hall"],
                    stars=_require_int(attack, "stars", "attack"),
                    destruction_percentage=_require_int(
                        attack,
                        "destructionPercentage",
                        "attack",
                    ),
                    sync_key=sync_key,
                    no_attack_key=no_attack_key,
                    user_fields=CwlUserFields(),
                    sort_key=(round_number, member["map_position"], attack_index),
                ),
            )

    return rows


def _read_war_members(side: JsonObject, context: str) -> list[JsonObject]:
    """Читает участников стороны войны.

    Args:
        side: JSON стороны CWL war.
        context: Контекст для ошибок.

    Returns:
        Нормализованные участники войны.
    """

    raw_members = _require_list(side, "members", context)
    members: list[JsonObject] = []

    for index, raw_member in enumerate(raw_members, start=1):
        if not isinstance(raw_member, dict):
            raise ClashApiUnavailableError(f"{context}: member #{index} должен быть объектом.")

        tag = normalize_tag(_require_str(raw_member, "tag", f"{context} member #{index}"))
        raw_attacks = raw_member.get("attacks", [])
        if raw_attacks is None:
            raw_attacks = []
        if not isinstance(raw_attacks, list):
            raise ClashApiUnavailableError(f"{context} member {tag}: attacks должен быть списком.")

        attacks: list[JsonObject] = []
        for attack_index, raw_attack in enumerate(raw_attacks, start=1):
            if not isinstance(raw_attack, dict):
                raise ClashApiUnavailableError(
                    f"{context} member {tag}: attack #{attack_index} должен быть объектом.",
                )
            attacks.append(raw_attack)

        members.append(
            {
                "tag": tag,
                "name": _require_str(raw_member, "name", f"{context} member {tag}"),
                "town_hall": _require_int(
                    raw_member,
                    "townhallLevel",
                    f"{context} member {tag}",
                ),
                "map_position": _require_int(
                    raw_member,
                    "mapPosition",
                    f"{context} member {tag}",
                ),
                "attacks": attacks,
            },
        )

    return members


def parse_old_cwl_user_fields(
    values: Sequence[Sequence[CellValue]],
    *,
    clans: tuple[ClanConfig, ClanConfig, ClanConfig],
) -> dict[str, CwlUserFields]:
    """Читает пользовательские поля старого листа CWL.

    Args:
        values: Значения старого листа CWL.
        clans: Три семейных клана.

    Returns:
        Пользовательские поля по стабильному CWL-ключу.
    """

    rows = [_normalize_row(row) for row in values]
    season = _find_old_cwl_season(rows)
    if season is None:
        return {}

    headers = _find_old_cwl_headers(rows, clans)
    user_fields_by_key: dict[str, CwlUserFields] = {}
    attack_occurrences: dict[tuple[str, int, str], int] = {}

    for header in headers:
        next_header_row = _find_next_cwl_header_row(headers, header)
        for row in rows[header.row_index + 1 : next_header_row]:
            sync_keys = _parse_old_cwl_row_keys(
                row,
                header,
                season,
                attack_occurrences=attack_occurrences,
            )
            if not sync_keys:
                continue

            user_fields = _parse_old_cwl_user_fields(row, header)
            for sync_key in sync_keys:
                if sync_key in user_fields_by_key:
                    logger.warning("duplicate old CWL sync key ignored: %s", sync_key)
                    continue
                user_fields_by_key[sync_key] = user_fields

    return user_fields_by_key


def _find_old_cwl_season(rows: Sequence[list[str]]) -> str | None:
    """Ищет сезон на старом CWL-листе.

    Args:
        rows: Строки старого листа.

    Returns:
        Сезон или `None`.
    """

    for row in rows:
        for cell in row:
            normalized = cell.strip()
            if normalized.startswith("CWL season:"):
                return normalized.split(":", maxsplit=1)[1].strip()
    return None


def _find_old_cwl_headers(
    rows: Sequence[list[str]],
    clans: tuple[ClanConfig, ClanConfig, ClanConfig],
) -> list[OldCwlTableHeader]:
    """Ищет заголовки старых CWL-таблиц.

    Args:
        rows: Строки старого листа.
        clans: Три семейных клана.

    Returns:
        Заголовки CWL-таблиц.
    """

    headers: list[OldCwlTableHeader] = []

    for row_index, row in enumerate(rows):
        for column_index in range(len(row)):
            if not _matches_cwl_header(row, column_index):
                continue
            headers.append(
                OldCwlTableHeader(
                    row_index=row_index,
                    column_index=column_index,
                    clan_tag=_find_clan_tag_above(rows, row_index, column_index),
                ),
            )

    return _assign_cwl_header_clan_tags(headers, clans)


def _matches_cwl_header(row: Sequence[str], column_index: int) -> bool:
    """Проверяет заголовок CWL с поддержкой алиаса `Ожидания3`.

    Args:
        row: Строка листа.
        column_index: Индекс первой колонки.

    Returns:
        `True`, если найден заголовок CWL.
    """

    if column_index + len(CWL_HEADERS) > len(row):
        return False

    for offset, expected_header in enumerate(CWL_HEADERS):
        actual = _cell_at(row, column_index + offset).strip()
        if actual not in _allowed_cwl_header_values(expected_header):
            return False

    return True


def _allowed_cwl_header_values(expected_header: str) -> set[str]:
    """Возвращает допустимые названия старого и нового заголовка CWL.

    Args:
        expected_header: Новое название заголовка.

    Returns:
        Множество допустимых названий.
    """

    return CWL_HEADER_ALIASES.get(expected_header, {expected_header})


def _assign_cwl_header_clan_tags(
    headers: Sequence[OldCwlTableHeader],
    clans: tuple[ClanConfig, ClanConfig, ClanConfig],
) -> list[OldCwlTableHeader]:
    """Назначает теги кланов старым CWL-таблицам.

    Args:
        headers: Найденные заголовки.
        clans: Три семейных клана.

    Returns:
        Заголовки с тегами кланов.
    """

    sorted_headers = sorted(headers, key=lambda header: (header.column_index, header.row_index))
    fallback_tags = {
        id(header): clans[index].tag
        for index, header in enumerate(sorted_headers)
        if index < len(clans)
    }

    assigned: list[OldCwlTableHeader] = []
    for header in sorted_headers:
        assigned.append(
            OldCwlTableHeader(
                row_index=header.row_index,
                column_index=header.column_index,
                clan_tag=header.clan_tag or fallback_tags.get(id(header)),
            ),
        )
    return assigned


def _find_next_cwl_header_row(
    headers: Sequence[OldCwlTableHeader],
    current: OldCwlTableHeader,
) -> int:
    """Ищет начало следующей CWL-таблицы.

    Args:
        headers: Все заголовки.
        current: Текущий заголовок.

    Returns:
        Индекс следующего заголовка или большое число.
    """

    next_rows = [
        header.row_index
        for header in headers
        if header.column_index == current.column_index
        and header.row_index > current.row_index
    ]
    return min(next_rows, default=10**9)


def _parse_old_cwl_row_keys(
    row: Sequence[str],
    header: OldCwlTableHeader,
    season: str,
    *,
    attack_occurrences: dict[tuple[str, int, str], int],
) -> tuple[str, ...]:
    """Вычисляет возможные ключи старой CWL-строки.

    Args:
        row: Строка старого листа.
        header: Заголовок таблицы.
        season: Сезон старого листа.
        attack_occurrences: Счётчик атак по `(clan_tag, round, attacker_tag)`.

    Returns:
        Кортеж CWL sync key. Пустой кортеж означает, что строка не является CWL-строкой.
    """

    if header.clan_tag is None:
        return ()

    round_raw = _cell_at(row, header.column_index).strip()
    attacker_raw = _cell_at(row, header.column_index + 1).strip()
    if not round_raw.isdigit() or attacker_raw == "":
        return ()

    try:
        attacker_tag = normalize_tag(attacker_raw)
    except ValueError:
        return ()

    round_number = int(round_raw)
    defender_raw = _cell_at(row, header.column_index + 5).strip()
    stars_raw = _cell_at(row, header.column_index + 6).strip()
    destruction_raw = _cell_at(row, header.column_index + 7).strip()

    if defender_raw == "" and stars_raw == "" and destruction_raw == "":
        return (
            make_cwl_key(
                season,
                header.clan_tag,
                round_number,
                attacker_tag,
                NO_ATTACK_MARKER,
            ),
        )

    occurrence_key = (header.clan_tag, round_number, attacker_tag)
    attack_index = attack_occurrences.get(occurrence_key, 0) + 1
    attack_occurrences[occurrence_key] = attack_index

    keys = [
        make_cwl_key(
            season,
            header.clan_tag,
            round_number,
            attacker_tag,
            f"{ATTACK_MARKER_PREFIX}_{attack_index}",
        ),
    ]

    defender_position = _parse_defender_position(defender_raw)
    if defender_position is not None:
        keys.append(
            make_cwl_key(
                season,
                header.clan_tag,
                round_number,
                attacker_tag,
                f"DEF_POS_{defender_position}",
            ),
        )

    return tuple(keys)


def _parse_old_cwl_user_fields(
    row: Sequence[str],
    header: OldCwlTableHeader,
) -> CwlUserFields:
    """Читает пользовательские поля старой CWL-строки.

    Args:
        row: Строка старого листа.
        header: Заголовок таблицы.

    Returns:
        Пользовательские поля.
    """

    base = header.column_index + CWL_USER_COLUMNS_START
    return CwlUserFields(
        idea1=_cell_at(row, base),
        realization1=_cell_at(row, base + 1),
        expectations1=_cell_at(row, base + 2),
        comment1=_cell_at(row, base + 3),
        idea2=_cell_at(row, base + 4),
        realization2=_cell_at(row, base + 5),
        expectations2=_cell_at(row, base + 6),
        comment2=_cell_at(row, base + 7),
        final_score=_cell_at(row, base + 8),
    )


def _apply_old_user_fields(
    blocks: Sequence[CwlClanBlock],
    old_user_fields: dict[str, CwlUserFields],
) -> None:
    """Переносит пользовательские поля в новые строки CWL.

    Args:
        blocks: Новые блоки CWL.
        old_user_fields: Пользовательские поля по старым ключам.
    """

    used_exact_keys: set[str] = set()
    used_no_attack_fallback_keys: set[str] = set()

    for block in blocks:
        for row in block.rows:
            if row.sync_key in old_user_fields and row.sync_key not in used_exact_keys:
                row.user_fields = old_user_fields[row.sync_key]
                used_exact_keys.add(row.sync_key)
                continue

            if (
                row.is_attack
                and row.no_attack_key in old_user_fields
                and row.no_attack_key not in used_no_attack_fallback_keys
            ):
                row.user_fields = old_user_fields[row.no_attack_key]
                used_no_attack_fallback_keys.add(row.no_attack_key)
                continue

            if row.sync_key in used_exact_keys:
                logger.warning("duplicate new CWL sync key without user fields: %s", row.sync_key)


def _build_cwl_matrix(
    config: AppConfig,
    season: str,
    blocks: Sequence[CwlClanBlock],
) -> tuple[list[list[CellValue]], list[CwlTableSpec]]:
    """Строит матрицу нового листа CWL.

    Args:
        config: Конфигурация приложения.
        season: Сезон CWL.
        blocks: Блоки CWL.

    Returns:
        Матрица значений и спецификации форматирования.
    """

    matrix: list[list[CellValue]] = [
        _title_row(f"CWL season: {season}", len(CWL_HEADERS)),
        _empty_row(len(CWL_HEADERS)),
    ]
    table_specs: list[CwlTableSpec] = []
    row_offset = 2

    for block_index, block in enumerate(blocks):
        if block_index > 0:
            matrix.append(_empty_row(len(CWL_HEADERS)))
            row_offset += 1

        start_cell = _offset_cell(config.cwl_start_cell, row_offset=row_offset, column_offset=0)
        block_rows: list[list[CellValue]] = [_title_row(block.clan.name, len(CWL_HEADERS))]

        has_table = block.message is None
        if block.message is not None:
            block_rows.append([block.message, *("" for _ in range(len(CWL_HEADERS) - 1))])
        else:
            block_rows.append(list(CWL_HEADERS))
            block_rows.extend(row.to_values() for row in block.rows)

        matrix.extend(block_rows)
        table_specs.append(
            CwlTableSpec(
                name=block.clan.name,
                start_cell=start_cell,
                rows_count=len(block_rows),
                columns=CWL_HEADERS,
                has_table=has_table,
            ),
        )
        row_offset += len(block_rows)

    return matrix, table_specs


async def _format_cwl_tables(
    *,
    sheets_client: SheetsClient,
    sheet_id: int,
    managed_range_a1: str,
    table_specs: Sequence[CwlTableSpec],
) -> None:
    """Форматирует блоки CWL обычными Google Sheets requests.

    Args:
        sheets_client: Клиент Google Sheets API.
        sheet_id: Числовой ID листа Google Sheets.
        managed_range_a1: Управляемая область CWL.
        table_specs: Спецификации блоков.
    """

    requests = _build_cwl_format_requests(
        sheet_id=sheet_id,
        managed_range_a1=managed_range_a1,
        table_specs=table_specs,
    )
    await sheets_client.batch_update_spreadsheet(requests)


def _build_cwl_format_requests(
    *,
    sheet_id: int,
    managed_range_a1: str,
    table_specs: Sequence[CwlTableSpec],
) -> list[dict[str, object]]:
    """Строит requests оформления CWL.

    Args:
        sheet_id: Числовой ID листа.
        managed_range_a1: Управляемая область CWL.
        table_specs: Спецификации блоков.

    Returns:
        Requests для `spreadsheets.batchUpdate`.
    """

    requests: list[dict[str, object]] = [
        _repeat_cell_request(
            _grid_range_from_a1(sheet_id=sheet_id, range_a1=managed_range_a1),
            _base_cell_format(),
            "userEnteredFormat(backgroundColorStyle,textFormat,wrapStrategy)",
        ),
    ]

    for table_spec in table_specs:
        requests.extend(_build_cwl_block_format_requests(sheet_id, table_spec))

    return requests


def _build_cwl_block_format_requests(
    sheet_id: int,
    table_spec: CwlTableSpec,
) -> list[dict[str, object]]:
    """Строит оформление одного CWL-блока.

    Args:
        sheet_id: Числовой ID листа.
        table_spec: Спецификация блока.

    Returns:
        Requests оформления.
    """

    title_range = _grid_range_for_block_row(sheet_id, table_spec, row_offset=0)
    block_range = _grid_range_from_start_cell(
        sheet_id=sheet_id,
        start_cell=table_spec.start_cell,
        rows_count=table_spec.rows_count,
        columns_count=len(table_spec.columns),
    )

    requests: list[dict[str, object]] = [
        _repeat_cell_request(
            title_range,
            _title_cell_format(),
            "userEnteredFormat(backgroundColorStyle,textFormat,wrapStrategy)",
        ),
        _update_borders_request(block_range),
    ]

    if not table_spec.has_table:
        requests.append(
            _repeat_cell_request(
                _grid_range_for_block_row(sheet_id, table_spec, row_offset=1),
                _message_cell_format(),
                "userEnteredFormat(backgroundColorStyle,textFormat,wrapStrategy)",
            ),
        )
        return requests

    requests.append(
        _repeat_cell_request(
            _grid_range_for_block_row(sheet_id, table_spec, row_offset=1),
            _header_cell_format(),
            "userEnteredFormat(backgroundColorStyle,textFormat,wrapStrategy)",
        ),
    )

    data_rows_count = max(table_spec.rows_count - 2, 0)
    if data_rows_count > 0:
        data_range = _grid_range_from_start_cell(
            sheet_id=sheet_id,
            start_cell=_offset_cell(table_spec.start_cell, row_offset=2, column_offset=0),
            rows_count=data_rows_count,
            columns_count=len(table_spec.columns),
        )
        requests.append(
            _repeat_cell_request(
                data_range,
                _data_cell_format(),
                "userEnteredFormat(backgroundColorStyle,textFormat,wrapStrategy)",
            ),
        )
        requests.extend(
            _build_alternating_row_requests(
                sheet_id=sheet_id,
                table_spec=table_spec,
                data_rows_count=data_rows_count,
            ),
        )

    return requests


def _build_alternating_row_requests(
    *,
    sheet_id: int,
    table_spec: CwlTableSpec,
    data_rows_count: int,
) -> list[dict[str, object]]:
    """Строит чередование строк данных.

    Args:
        sheet_id: Числовой ID листа.
        table_spec: Спецификация блока.
        data_rows_count: Количество строк данных.

    Returns:
        Requests для нечётных строк.
    """

    requests: list[dict[str, object]] = []
    for data_row_offset in range(data_rows_count):
        if data_row_offset % 2 == 0:
            continue

        requests.append(
            _repeat_cell_request(
                _grid_range_for_block_row(
                    sheet_id=sheet_id,
                    table_spec=table_spec,
                    row_offset=2 + data_row_offset,
                ),
                {"userEnteredFormat": {"backgroundColorStyle": {"rgbColor": LIGHT_BAND_RGB}}},
                "userEnteredFormat.backgroundColorStyle",
            ),
        )

    return requests


def _build_report(block: CwlClanBlock) -> CwlClanReport:
    """Строит отчёт по блоку клана.

    Args:
        block: CWL-блок.

    Returns:
        Отчёт клана.
    """

    attacks_count = sum(1 for row in block.rows if row.is_attack)
    no_attack_count = sum(1 for row in block.rows if not row.is_attack)
    return CwlClanReport(
        clan_name=block.clan.name,
        rounds_count=block.rounds_count,
        rows_count=len(block.rows),
        attacks_count=attacks_count,
        no_attack_count=no_attack_count,
    )


def make_cwl_key(
    season: str,
    clan_tag: str,
    round_number: int,
    attacker_tag: str,
    marker: str,
) -> str:
    """Создаёт стабильный ключ CWL-строки.

    Args:
        season: Сезон CWL.
        clan_tag: Тег семейного клана.
        round_number: Номер раунда.
        attacker_tag: Тег атакующего.
        marker: `NO_ATTACK`, `ATTACK_N` или старый совместимый `DEF_POS_N`.

    Returns:
        Стабильный CWL-ключ.
    """

    return "|".join(
        [
            season,
            normalize_tag(clan_tag),
            str(round_number),
            normalize_tag(attacker_tag),
            marker,
        ],
    )


def format_town_hall(town_hall: int) -> str:
    """Форматирует ратушу без номера на карте.

    Args:
        town_hall: Уровень ратуши.

    Returns:
        Значение вида `TH18`.
    """

    return f"TH{town_hall}"


def _parse_defender_position(value: str) -> int | None:
    """Парсит позицию цели из старого формата `1 — TH18`.

    Args:
        value: Значение старой ячейки.

    Returns:
        Номер цели или `None`.
    """

    match = DEFENDER_POSITION_RE.search(value)
    if match is None:
        return None
    return int(match.group(1))


def _find_clan_tag_above(
    rows: Sequence[list[str]],
    header_row_index: int,
    column_index: int,
) -> str | None:
    """Ищет тег клана над старой таблицей.

    Args:
        rows: Строки листа.
        header_row_index: Индекс строки заголовка.
        column_index: Индекс первой колонки.

    Returns:
        Тег клана или `None`.
    """

    for row_index in range(header_row_index - 1, -1, -1):
        row = rows[row_index]
        segment = row[column_index : column_index + len(CWL_HEADERS)]
        if not any(cell.strip() for cell in segment):
            continue

        for cell in segment:
            try:
                return normalize_tag(cell.split("|")[-1].strip())
            except ValueError:
                continue
        return None

    return None


def _range_from_start_cell(start_cell: str, rows_count: int, columns_count: int) -> str:
    """Строит закрытый A1-диапазон по стартовой ячейке и размеру.

    Args:
        start_cell: Стартовая ячейка.
        rows_count: Количество строк.
        columns_count: Количество колонок.

    Returns:
        A1-диапазон.
    """

    start_column_number, start_row = _parse_start_cell(start_cell)
    end_column = _number_to_column(start_column_number + columns_count - 1)
    end_row = start_row + rows_count - 1
    return f"{_number_to_column(start_column_number)}{start_row}:{end_column}{end_row}"


def _grid_range_from_start_cell(
    *,
    sheet_id: int,
    start_cell: str,
    rows_count: int,
    columns_count: int,
) -> dict[str, int]:
    """Строит GridRange по стартовой ячейке и размеру.

    Args:
        sheet_id: Числовой ID листа.
        start_cell: Стартовая ячейка.
        rows_count: Количество строк.
        columns_count: Количество колонок.

    Returns:
        GridRange.
    """

    start_column_number, start_row = _parse_start_cell(start_cell)
    return {
        "sheetId": sheet_id,
        "startRowIndex": start_row - 1,
        "endRowIndex": start_row - 1 + rows_count,
        "startColumnIndex": start_column_number - 1,
        "endColumnIndex": start_column_number - 1 + columns_count,
    }


def _grid_range_from_a1(*, sheet_id: int, range_a1: str) -> dict[str, int]:
    """Строит GridRange из закрытого A1-диапазона.

    Args:
        sheet_id: Числовой ID листа.
        range_a1: Закрытый A1-диапазон.

    Returns:
        GridRange.

    Raises:
        CwlDataError: Если диапазон некорректен.
    """

    match = A1_RANGE_RE.fullmatch(range_a1.strip())
    if match is None:
        raise CwlDataError(f"Некорректный A1-диапазон: {range_a1}.")

    start_column, start_row_raw, end_column, end_row_raw = match.groups()
    start_column_number = _column_to_number(start_column)
    end_column_number = _column_to_number(end_column)
    start_row = int(start_row_raw)
    end_row = int(end_row_raw)

    if end_column_number < start_column_number or end_row < start_row:
        raise CwlDataError(f"A1-диапазон задан в обратном порядке: {range_a1}.")

    return {
        "sheetId": sheet_id,
        "startRowIndex": start_row - 1,
        "endRowIndex": end_row,
        "startColumnIndex": start_column_number - 1,
        "endColumnIndex": end_column_number,
    }


def _grid_range_for_block_row(
    sheet_id: int,
    table_spec: CwlTableSpec,
    *,
    row_offset: int,
) -> dict[str, int]:
    """Строит GridRange строки блока.

    Args:
        sheet_id: Числовой ID листа.
        table_spec: Спецификация блока.
        row_offset: Смещение строки.

    Returns:
        GridRange.
    """

    return _grid_range_from_start_cell(
        sheet_id=sheet_id,
        start_cell=_offset_cell(table_spec.start_cell, row_offset=row_offset, column_offset=0),
        rows_count=1,
        columns_count=len(table_spec.columns),
    )


def _grid_range_for_block_column(
    *,
    sheet_id: int,
    table_spec: CwlTableSpec,
    column_index: int,
    row_offset: int,
    rows_count: int,
) -> dict[str, int]:
    """Строит GridRange колонки блока.

    Args:
        sheet_id: Числовой ID листа.
        table_spec: Спецификация блока.
        column_index: Индекс колонки.
        row_offset: Смещение строки.
        rows_count: Количество строк.

    Returns:
        GridRange.
    """

    return _grid_range_from_start_cell(
        sheet_id=sheet_id,
        start_cell=_offset_cell(
            table_spec.start_cell,
            row_offset=row_offset,
            column_offset=column_index,
        ),
        rows_count=rows_count,
        columns_count=1,
    )


def _offset_cell(start_cell: str, *, row_offset: int, column_offset: int) -> str:
    """Сдвигает A1-ячейку.

    Args:
        start_cell: Исходная ячейка.
        row_offset: Сдвиг строк.
        column_offset: Сдвиг колонок.

    Returns:
        Новая A1-ячейка.
    """

    start_column_number, start_row = _parse_start_cell(start_cell)
    return f"{_number_to_column(start_column_number + column_offset)}{start_row + row_offset}"


def _parse_start_cell(start_cell: str) -> tuple[int, int]:
    """Парсит стартовую A1-ячейку.

    Args:
        start_cell: A1-ячейка.

    Returns:
        Номер колонки и строки.

    Raises:
        CwlDataError: Если ячейка некорректна.
    """

    match = A1_CELL_RE.fullmatch(start_cell.strip())
    if match is None:
        raise CwlDataError(f"Некорректная стартовая ячейка: {start_cell}.")

    start_column, start_row_raw = match.groups()
    return _column_to_number(start_column), int(start_row_raw)


def _repeat_cell_request(
    grid_range: dict[str, int],
    cell: dict[str, object],
    fields: str,
) -> dict[str, object]:
    """Создаёт repeatCell request.

    Args:
        grid_range: GridRange.
        cell: Формат ячейки.
        fields: Field mask.

    Returns:
        Request.
    """

    return {"repeatCell": {"range": grid_range, "cell": cell, "fields": fields}}


def _update_borders_request(grid_range: dict[str, int]) -> dict[str, object]:
    """Создаёт request границ.

    Args:
        grid_range: GridRange.

    Returns:
        Request.
    """

    border = {
        "style": "SOLID",
        "width": 1,
        "colorStyle": {"rgbColor": BORDER_RGB},
    }
    return {
        "updateBorders": {
            "range": grid_range,
            "top": border,
            "bottom": border,
            "left": border,
            "right": border,
            "innerHorizontal": border,
            "innerVertical": border,
        },
    }


def _base_cell_format() -> dict[str, object]:
    """Возвращает базовый формат managed range.

    Returns:
        Формат ячейки.
    """

    return {
        "userEnteredFormat": {
            "backgroundColorStyle": {"rgbColor": WHITE_RGB},
            "textFormat": {
                "foregroundColorStyle": {"rgbColor": BLACK_RGB},
                "bold": False,
            },
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "WRAP",
        },
    }


def _title_cell_format() -> dict[str, object]:
    """Возвращает формат строки названия.

    Returns:
        Формат ячейки.
    """

    return {
        "userEnteredFormat": {
            "backgroundColorStyle": {"rgbColor": DARK_GREEN_RGB},
            "textFormat": {
                "foregroundColorStyle": {"rgbColor": WHITE_RGB},
                "bold": True,
            },
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "WRAP",
        },
    }


def _header_cell_format() -> dict[str, object]:
    """Возвращает формат заголовков.

    Returns:
        Формат ячейки.
    """

    return {
        "userEnteredFormat": {
            "backgroundColorStyle": {"rgbColor": GREEN_RGB},
            "textFormat": {
                "foregroundColorStyle": {"rgbColor": WHITE_RGB},
                "bold": True,
            },
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "WRAP",
        },
    }


def _message_cell_format() -> dict[str, object]:
    """Возвращает формат служебной строки.

    Returns:
        Формат ячейки.
    """

    return {
        "userEnteredFormat": {
            "backgroundColorStyle": {"rgbColor": LIGHT_BAND_RGB},
            "textFormat": {
                "foregroundColorStyle": {"rgbColor": BLACK_RGB},
                "bold": False,
            },
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "WRAP",
        },
    }


def _data_cell_format() -> dict[str, object]:
    """Возвращает формат строк данных.

    Returns:
        Формат ячейки.
    """

    return {
        "userEnteredFormat": {
            "backgroundColorStyle": {"rgbColor": WHITE_RGB},
            "textFormat": {
                "foregroundColorStyle": {"rgbColor": BLACK_RGB},
                "bold": False,
            },
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "WRAP",
        },
    }


def _title_row(title: str, width: int) -> list[CellValue]:
    """Создаёт строку названия.

    Args:
        title: Название блока.
        width: Ширина строки.

    Returns:
        Строка значений.
    """

    return [title, *("" for _ in range(width - 1))]


def _empty_row(width: int) -> list[CellValue]:
    """Создаёт пустую строку.

    Args:
        width: Ширина строки.

    Returns:
        Пустая строка.
    """

    return ["" for _ in range(width)]


def _normalize_row(row: Sequence[CellValue]) -> list[str]:
    """Преобразует строку Google Sheets в строки.

    Args:
        row: Строка значений.

    Returns:
        Строковые значения.
    """

    return [_cell_to_str(cell) for cell in row]


def _cell_to_str(value: CellValue) -> str:
    """Преобразует значение ячейки в строку.

    Args:
        value: Значение ячейки.

    Returns:
        Строковое представление.
    """

    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _cell_at(row: Sequence[str], index: int) -> str:
    """Безопасно читает ячейку.

    Args:
        row: Строка.
        index: Индекс.

    Returns:
        Значение или пустая строка.
    """

    if index < 0 or index >= len(row):
        return ""
    return row[index]


def _require_str(data: JsonObject, key: str, context: str) -> str:
    """Читает обязательную строку.

    Args:
        data: JSON-объект.
        key: Имя поля.
        context: Контекст ошибки.

    Returns:
        Строка.

    Raises:
        ClashApiUnavailableError: Если поле некорректно.
    """

    value = data.get(key)
    if not isinstance(value, str):
        raise ClashApiUnavailableError(f"{context}: поле {key} должно быть строкой.")
    return value


def _require_int(data: JsonObject, key: str, context: str) -> int:
    """Читает обязательное целое число.

    Args:
        data: JSON-объект.
        key: Имя поля.
        context: Контекст ошибки.

    Returns:
        Целое число.

    Raises:
        ClashApiUnavailableError: Если поле некорректно.
    """

    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ClashApiUnavailableError(f"{context}: поле {key} должно быть числом.")
    return value


def _require_list(data: JsonObject, key: str, context: str) -> list[Any]:
    """Читает обязательный список.

    Args:
        data: JSON-объект.
        key: Имя поля.
        context: Контекст ошибки.

    Returns:
        Список.

    Raises:
        ClashApiUnavailableError: Если поле некорректно.
    """

    value = data.get(key)
    if not isinstance(value, list):
        raise ClashApiUnavailableError(f"{context}: поле {key} должно быть списком.")
    return value


def _require_dict(data: JsonObject, key: str, context: str) -> JsonObject:
    """Читает обязательный объект.

    Args:
        data: JSON-объект.
        key: Имя поля.
        context: Контекст ошибки.

    Returns:
        JSON-объект.

    Raises:
        ClashApiUnavailableError: Если поле некорректно.
    """

    value = data.get(key)
    if not isinstance(value, dict):
        raise ClashApiUnavailableError(f"{context}: поле {key} должно быть объектом.")
    return value


def _column_to_number(column: str) -> int:
    """Преобразует имя колонки в номер.

    Args:
        column: Имя колонки.

    Returns:
        Номер колонки с 1.
    """

    number = 0
    for char in column.upper():
        number = number * 26 + ord(char) - ord("A") + 1
    return number


def _number_to_column(number: int) -> str:
    """Преобразует номер колонки в имя.

    Args:
        number: Номер колонки с 1.

    Returns:
        Имя колонки.
    """

    chars: list[str] = []
    while number > 0:
        number, remainder = divmod(number - 1, 26)
        chars.append(chr(ord("A") + remainder))
    return "".join(reversed(chars))