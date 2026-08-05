"""Immutable модели домена прогноза ЛВК."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from clash_sheet_sync_bot.telegram.client import TelegramMessageEntity


@dataclass(frozen=True, slots=True)
class LeagueMember:
    """Зарегистрированный участник League Group."""

    tag: str
    name: str
    town_hall_level: int


@dataclass(frozen=True, slots=True)
class LeagueClan:
    """Клан League Group с актуальными API metadata."""

    tag: str
    name: str
    clan_level: int
    members: tuple[LeagueMember, ...]


@dataclass(frozen=True, slots=True)
class LeagueRound:
    """Раунд League Group и его warTags, включая `#0`."""

    number: int
    war_tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LeagueGroup:
    """Строго распарсенная текущая League Group."""

    season: str
    state: str
    clans: tuple[LeagueClan, ...]
    rounds: tuple[LeagueRound, ...]

    def clan(self, clan_tag: str) -> LeagueClan:
        """Возвращает клан по нормализованному тегу."""

        for clan in self.clans:
            if clan.tag == clan_tag:
                return clan
        raise KeyError(clan_tag)


@dataclass(frozen=True, slots=True)
class WarMember:
    """Фактический участник CWL war."""

    tag: str
    name: str
    town_hall_level: int
    map_position: int


@dataclass(frozen=True, slots=True)
class WarClan:
    """Одна фактическая сторона CWL war."""

    tag: str
    name: str
    members: tuple[WarMember, ...]


@dataclass(frozen=True, slots=True)
class CwlWar:
    """Строго распарсенная CWL war."""

    state: str
    team_size: int
    start_time: datetime
    clan: WarClan
    opponent: WarClan


@dataclass(frozen=True, slots=True)
class CreatedWar:
    """Созданная API-война с round identity."""

    round_number: int
    war_tag: str
    war: CwlWar


@dataclass(frozen=True, slots=True)
class SelectedWar:
    """Выбранная current/preparation война и стороны нашего клана."""

    round_number: int
    war_tag: str
    war: CwlWar
    own_clan: WarClan
    opponent_clan: WarClan


@dataclass(frozen=True, slots=True)
class ForecastColumn:
    """Одна колонка будущего раунда."""

    round_number: int
    roster: tuple[int | None, ...]


@dataclass(frozen=True, slots=True)
class ForecastMessage:
    """Custom emoji сообщение и согласованный plain fallback."""

    text: str
    entities: tuple[TelegramMessageEntity, ...]
    fallback_text: str


@dataclass(frozen=True, slots=True)
class ForecastReady:
    """Готовое к Telegram delivery сообщение одного клана."""

    clan_name: str
    message: ForecastMessage


@dataclass(frozen=True, slots=True)
class ForecastInactive:
    """У клана нет current/preparation CWL war."""

    clan_name: str


@dataclass(frozen=True, slots=True)
class ForecastScheduleRequired:
    """Прогноз заблокирован отсутствующим или конфликтным schedule."""

    clan_name: str
    reason: str
