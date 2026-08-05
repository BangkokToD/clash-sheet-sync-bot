"""Чистые правила parsing, выбора войны, roster и schedule ЛВК."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

from clash_sheet_sync_bot.models import normalize_tag

from .models import (
    CreatedWar,
    CwlWar,
    LeagueClan,
    LeagueGroup,
    LeagueMember,
    LeagueRound,
    SelectedWar,
    WarClan,
    WarMember,
)


class CwlForecastDataError(RuntimeError):
    """Обязательное внешнее поле отсутствует или противоречиво."""


class CwlScheduleError(RuntimeError):
    """Сохранённое расписание неполно или конфликтует с League Group."""


def parse_league_group(data: Mapping[str, object]) -> LeagueGroup:
    """Строго парсит League Group без молчаливого пропуска участников."""

    season = _string(data, "season", "league group")
    state = _string(data, "state", "league group")
    raw_clans = _list(data, "clans", "league group")
    raw_rounds = _list(data, "rounds", "league group")
    clans: list[LeagueClan] = []
    clan_tags: set[str] = set()
    for index, raw in enumerate(raw_clans, start=1):
        item = _mapping(raw, f"league group clan #{index}")
        tag = _tag(item, "tag", f"league group clan #{index}")
        if tag in clan_tags:
            raise CwlForecastDataError(f"League Group содержит дублирующийся clan tag {tag}.")
        clan_tags.add(tag)
        raw_members = _list(item, "members", f"league group clan {tag}")
        members: list[LeagueMember] = []
        member_tags: set[str] = set()
        for member_index, raw_member in enumerate(raw_members, start=1):
            member = _mapping(raw_member, f"league member #{member_index} клана {tag}")
            member_tag = _tag(member, "tag", f"league member #{member_index} клана {tag}")
            if member_tag in member_tags:
                raise CwlForecastDataError(f"Клан {tag} содержит дублирующийся player tag.")
            member_tags.add(member_tag)
            members.append(
                LeagueMember(
                    tag=member_tag,
                    name=_string(member, "name", f"league member {member_tag}"),
                    town_hall_level=_town_hall(member, f"league member {member_tag}"),
                )
            )
        clan_level = _integer(item, "clanLevel", f"league group clan {tag}")
        if clan_level <= 0:
            raise CwlForecastDataError(f"league group clan {tag}: clanLevel должен быть > 0.")
        clans.append(
            LeagueClan(
                tag=tag,
                name=_string(item, "name", f"league group clan {tag}"),
                clan_level=clan_level,
                members=tuple(members),
            )
        )
    rounds: list[LeagueRound] = []
    known_war_tags: set[str] = set()
    for number, raw in enumerate(raw_rounds, start=1):
        item = _mapping(raw, f"league round #{number}")
        tags = tuple(
            _war_tag(value, f"league round #{number}")
            for value in _list(item, "warTags", f"league round #{number}")
        )
        for tag in tags:
            if tag != "#0" and tag in known_war_tags:
                raise CwlForecastDataError(f"League Group содержит дублирующийся warTag {tag}.")
            if tag != "#0":
                known_war_tags.add(tag)
        rounds.append(LeagueRound(number=number, war_tags=tags))
    if not clans or not rounds:
        raise CwlForecastDataError("League Group должна содержать clans и rounds.")
    return LeagueGroup(season=season, state=state, clans=tuple(clans), rounds=tuple(rounds))


def parse_cwl_war(data: Mapping[str, object]) -> CwlWar:
    """Строго парсит CWL war, включая teamSize, time и обе стороны."""

    team_size = _integer(data, "teamSize", "cwl war")
    if team_size <= 0:
        raise CwlForecastDataError("cwl war: teamSize должен быть > 0.")
    return CwlWar(
        state=_string(data, "state", "cwl war"),
        team_size=team_size,
        start_time=_api_time(_string(data, "startTime", "cwl war")),
        clan=_war_clan(_mapping(data.get("clan"), "cwl war clan"), "cwl war clan"),
        opponent=_war_clan(_mapping(data.get("opponent"), "cwl war opponent"), "cwl war opponent"),
    )


def league_group_fingerprint(group: LeagueGroup) -> str:
    """Хеширует только отсортированные нормализованные clan tags."""

    canonical = "\n".join(sorted(clan.tag for clan in group.clans)).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def extract_known_opponents(
    *, group: LeagueGroup, own_clan_tag: str, created_wars: Iterable[CreatedWar]
) -> dict[int, str]:
    """Восстанавливает API-известного соперника нашего клана по раундам."""

    own_tag = _normalize(own_clan_tag, "own clan tag")
    group_tags = {clan.tag for clan in group.clans}
    if own_tag not in group_tags:
        raise CwlForecastDataError(f"Наш клан {own_tag} отсутствует в League Group.")
    result: dict[int, str] = {}
    for created in created_wars:
        if created.round_number < 1 or created.round_number > len(group.rounds):
            raise CwlForecastDataError("Созданная война ссылается на неизвестный раунд.")
        war_tag = _war_tag(created.war_tag, "created war")
        if war_tag == "#0" or war_tag not in group.rounds[created.round_number - 1].war_tags:
            raise CwlForecastDataError("Созданная война не совпадает с warTag указанного раунда.")
        side_tags = (created.war.clan.tag, created.war.opponent.tag)
        if side_tags.count(own_tag) == 0:
            continue
        if side_tags.count(own_tag) != 1:
            raise CwlForecastDataError(
                f"Война {created.war_tag} должна содержать наш клан ровно с одной стороны."
            )
        opponent = side_tags[1] if side_tags[0] == own_tag else side_tags[0]
        if opponent not in group_tags:
            raise CwlForecastDataError(f"Соперник {opponent} отсутствует в League Group.")
        previous = result.get(created.round_number)
        if previous is not None and previous != opponent:
            raise CwlForecastDataError("Раунд содержит противоречивые пары нашего клана.")
        result[created.round_number] = opponent
    return result


def select_current_war(
    *, own_clan_tag: str, created_wars: Iterable[CreatedWar]
) -> SelectedWar | None:
    """Выбирает `inWar`, иначе стабильную ближайшую `preparation`."""

    own_tag = _normalize(own_clan_tag, "own clan tag")
    candidates: list[SelectedWar] = []
    for created in created_wars:
        sides = (created.war.clan, created.war.opponent)
        matching = [side for side in sides if side.tag == own_tag]
        if not matching:
            continue
        if len(matching) != 1:
            raise CwlForecastDataError(
                f"Война {created.war_tag} должна содержать наш клан ровно с одной стороны."
            )
        own = matching[0]
        opponent = sides[1] if sides[0] is own else sides[0]
        if created.war.state in {"inWar", "preparation"}:
            candidates.append(
                SelectedWar(
                    round_number=created.round_number,
                    war_tag=created.war_tag,
                    war=created.war,
                    own_clan=own,
                    opponent_clan=opponent,
                )
            )
    in_war = [item for item in candidates if item.war.state == "inWar"]
    if in_war:
        return min(in_war, key=lambda item: (item.round_number, item.war_tag))
    preparations = [item for item in candidates if item.war.state == "preparation"]
    return min(
        preparations,
        key=lambda item: (item.war.start_time, item.round_number, item.war_tag),
        default=None,
    )


def actual_roster(clan: WarClan, team_size: int) -> tuple[int | None, ...]:
    """Сортирует фактический roster по mapPosition и дополняет до teamSize."""

    if team_size <= 0 or len(clan.members) != team_size:
        raise CwlForecastDataError("Фактический roster должен содержать ровно teamSize игроков.")
    return tuple(
        member.town_hall_level
        for member in sorted(clan.members, key=lambda item: item.map_position)
    )


def build_predicted_roster(clan: LeagueClan, team_size: int) -> tuple[int | None, ...]:
    """Выбирает сильнейших registered members по TH DESC/tag ASC."""

    if team_size <= 0:
        raise CwlForecastDataError("teamSize должен быть положительным.")
    members = sorted(clan.members, key=lambda member: (-member.town_hall_level, member.tag))
    values: list[int | None] = [member.town_hall_level for member in members[:team_size]]
    return tuple(values + [None] * (team_size - len(values)))


def validate_schedule(
    *,
    group: LeagueGroup,
    own_clan_tag: str,
    selected_round: int,
    known_opponents: Mapping[int, str],
    saved_opponents: Mapping[int, str] | None,
) -> dict[int, str]:
    """Проверяет полный schedule и возвращает соперников будущих раундов."""

    total_rounds = len(group.rounds)
    if selected_round < 1 or selected_round > total_rounds:
        raise CwlScheduleError("Выбран неизвестный раунд League Group.")
    if selected_round == total_rounds:
        return {}
    if saved_opponents is None:
        future_rounds = range(selected_round + 1, total_rounds + 1)
        if all(round_number in known_opponents for round_number in future_rounds):
            return {
                round_number: _schedule_tag(
                    known_opponents[round_number], f"known round {round_number}"
                )
                for round_number in future_rounds
            }
        raise CwlScheduleError("Расписание будущих раундов не заполнено.")
    expected_rounds = set(range(1, total_rounds + 1))
    if any(not isinstance(number, int) or isinstance(number, bool) for number in saved_opponents):
        raise CwlScheduleError("Номер раунда расписания должен быть целым числом.")
    if set(saved_opponents) != expected_rounds:
        raise CwlScheduleError("Количество или номера раундов расписания не совпадают с API.")
    own_tag = _normalize(own_clan_tag, "own clan tag")
    group_tags = {clan.tag for clan in group.clans}
    normalized: dict[int, str] = {}
    for round_number, raw_tag in saved_opponents.items():
        tag = _schedule_tag(raw_tag, f"schedule round {round_number}")
        if tag == own_tag:
            raise CwlScheduleError("Наш клан не может быть соперником в расписании.")
        if tag not in group_tags:
            raise CwlScheduleError(f"Соперник {tag} отсутствует в League Group.")
        normalized[round_number] = tag
    if len(set(normalized.values())) != len(normalized):
        raise CwlScheduleError("Соперники в расписании не должны повторяться.")
    for round_number, raw_known in known_opponents.items():
        if round_number not in expected_rounds:
            raise CwlScheduleError("API-известная пара ссылается на неизвестный раунд.")
        known = _schedule_tag(raw_known, f"known round {round_number}")
        if known not in group_tags or known == own_tag:
            raise CwlScheduleError("API-известный соперник не принадлежит League Group.")
        if normalized.get(round_number) != known:
            raise CwlScheduleError("Сохранённое расписание конфликтует с реальной CWL war.")
    return {
        round_number: normalized[round_number]
        for round_number in range(selected_round + 1, total_rounds + 1)
    }


def _war_clan(data: Mapping[str, object], context: str) -> WarClan:
    tag = _tag(data, "tag", context)
    raw_members = _list(data, "members", context)
    members: list[WarMember] = []
    positions: set[int] = set()
    tags: set[str] = set()
    for index, raw in enumerate(raw_members, start=1):
        item = _mapping(raw, f"{context} member #{index}")
        member_tag = _tag(item, "tag", f"{context} member #{index}")
        position = _integer(item, "mapPosition", f"{context} member {member_tag}")
        if position <= 0 or position in positions or member_tag in tags:
            raise CwlForecastDataError(f"{context}: mapPosition/player tag должен быть уникальным.")
        positions.add(position)
        tags.add(member_tag)
        members.append(
            WarMember(
                tag=member_tag,
                name=_string(item, "name", f"{context} member {member_tag}"),
                town_hall_level=_town_hall(item, f"{context} member {member_tag}"),
                map_position=position,
            )
        )
    return WarClan(tag=tag, name=_string(data, "name", context), members=tuple(members))


def _api_time(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CwlForecastDataError(f"Некорректное API-время: {value}.") from exc
    if parsed.tzinfo is None:
        raise CwlForecastDataError("API-время должно содержать timezone.")
    return parsed.astimezone(UTC)


def _mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CwlForecastDataError(f"{context} должен быть объектом.")
    return value


def _list(data: Mapping[str, object], key: str, context: str) -> list[object]:
    value = data.get(key)
    if not isinstance(value, list):
        raise CwlForecastDataError(f"{context}: поле {key} должно быть списком.")
    return value


def _string(data: Mapping[str, object], key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CwlForecastDataError(f"{context}: поле {key} должно быть непустой строкой.")
    return value


def _integer(data: Mapping[str, object], key: str, context: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise CwlForecastDataError(f"{context}: поле {key} должно быть целым числом.")
    return value


def _town_hall(data: Mapping[str, object], context: str) -> int:
    value = _integer(data, "townHallLevel", context)
    if not 1 <= value <= 18:
        raise CwlForecastDataError(f"{context}: townHallLevel должен быть от 1 до 18.")
    return value


def _tag(data: Mapping[str, object], key: str, context: str) -> str:
    return _normalize(_string(data, key, context), f"{context}.{key}")


def _war_tag(value: object, context: str) -> str:
    if value == "#0":
        return "#0"
    if not isinstance(value, str):
        raise CwlForecastDataError(f"{context}: warTag должен быть строкой.")
    return _normalize(value, f"{context}.warTag")


def _normalize(value: str, context: str) -> str:
    try:
        return normalize_tag(value)
    except ValueError as exc:
        raise CwlForecastDataError(f"{context}: некорректный tag.") from exc


def _schedule_tag(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise CwlScheduleError(f"{context}: tag должен быть строкой.")
    try:
        return normalize_tag(value)
    except ValueError as exc:
        raise CwlScheduleError(f"{context}: некорректный tag.") from exc
