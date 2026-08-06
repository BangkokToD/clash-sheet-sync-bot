"""Оркестрация прогноза одного клана поверх Clash API и schedule repository."""

from __future__ import annotations

import asyncio
from collections.abc import MutableMapping
from typing import Protocol

from clash_sheet_sync_bot.coc.client import ClashCwlNotInProgressError, JsonObject
from clash_sheet_sync_bot.models import TrackedClan
from clash_sheet_sync_bot.repositories import CwlForecastRepository, CwlForecastScheduleKey
from clash_sheet_sync_bot.telegram.emoji_catalog import TelegramEmojiCatalog

from .domain import (
    CwlScheduleError,
    actual_roster,
    build_predicted_roster,
    extract_known_opponents,
    league_group_fingerprint,
    parse_cwl_war,
    parse_league_group,
    select_current_war,
    validate_schedule,
)
from .formatting import build_forecast_message
from .models import (
    CreatedWar,
    CwlWar,
    ForecastColumn,
    ForecastInactive,
    ForecastReady,
    ForecastScheduleRequired,
    LeagueGroup,
)


class ForecastClashClient(Protocol):
    """Минимальный Clash API contract forecast service."""

    async def get_current_war_league_group(self, clan_tag: str) -> JsonObject: ...

    async def get_cwl_war(self, war_tag: str) -> JsonObject: ...


ForecastResult = ForecastReady | ForecastInactive | ForecastScheduleRequired


class CwlForecastService:
    """Формирует один forecast с общим на запуск war cache."""

    def __init__(
        self,
        *,
        clash: ForecastClashClient,
        repository: CwlForecastRepository,
        catalog: TelegramEmojiCatalog,
        war_cache: MutableMapping[str, CwlWar],
        war_concurrency_limit: int,
    ) -> None:
        self._clash = clash
        self._repository = repository
        self._catalog = catalog
        self._war_cache = war_cache
        self._war_concurrency_limit = war_concurrency_limit

    async def forecast_clan(self, tracked: TrackedClan) -> ForecastResult:
        """Загружает authoritative API data и строит результат одного клана."""

        try:
            group = parse_league_group(
                await self._clash.get_current_war_league_group(tracked.clan_tag)
            )
        except ClashCwlNotInProgressError:
            return ForecastInactive(tracked.clan_name)
        group_clan = group.clan(tracked.clan_tag)
        created = await self._load_created_wars(group)
        selected = select_current_war(own_clan_tag=tracked.clan_tag, created_wars=created)
        if selected is None:
            return ForecastInactive(group_clan.name)
        known = extract_known_opponents(
            group=group,
            own_clan_tag=tracked.clan_tag,
            created_wars=created,
        )
        key = CwlForecastScheduleKey(
            season=group.season,
            group_fingerprint=league_group_fingerprint(group),
            clan_tag=tracked.clan_tag,
        )
        schedule = await self._repository.get_schedule(key)
        saved = (
            {item.round_number: item.opponent_clan_tag for item in schedule.rounds}
            if schedule is not None
            else None
        )
        try:
            future = validate_schedule(
                group=group,
                own_clan_tag=tracked.clan_tag,
                selected_round=selected.round_number,
                known_opponents=known,
                saved_opponents=saved,
            )
        except CwlScheduleError as exc:
            return ForecastScheduleRequired(group_clan.name, str(exc))
        team_size = selected.war.team_size
        columns = tuple(
            ForecastColumn(
                round_number=round_number,
                roster=build_predicted_roster(group.clan(opponent_tag), team_size),
            )
            for round_number, opponent_tag in future.items()
        )
        return ForecastReady(
            clan_name=group_clan.name,
            message=build_forecast_message(
                clan_name=group_clan.name,
                clan_tag=group_clan.tag,
                own_roster=actual_roster(selected.own_clan, team_size),
                current_opponent_roster=actual_roster(selected.opponent_clan, team_size),
                future_columns=columns,
                catalog=self._catalog,
            ),
        )

    async def _load_created_wars(self, group: LeagueGroup) -> tuple[CreatedWar, ...]:
        """Загружает реальные warTags с лимитом и общим cache запуска."""

        tags = [
            (round_.number, tag)
            for round_ in group.rounds
            for tag in round_.war_tags
            if tag != "#0"
        ]
        semaphore = asyncio.Semaphore(self._war_concurrency_limit)

        async def load(round_number: int, war_tag: str) -> CreatedWar:
            war = self._war_cache.get(war_tag)
            if war is None:
                async with semaphore:
                    war = self._war_cache.get(war_tag)
                    if war is None:
                        war = parse_cwl_war(await self._clash.get_cwl_war(war_tag))
                        self._war_cache[war_tag] = war
            return CreatedWar(round_number, war_tag, war)

        return tuple(await asyncio.gather(*(load(number, tag) for number, tag in tags)))
