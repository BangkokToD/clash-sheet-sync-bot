"""Flow/integration tests пользовательской команды `/cwl_forecast`."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from clash_sheet_sync_bot.cwl_forecast import league_group_fingerprint, parse_league_group
from clash_sheet_sync_bot.cwl_forecast.forecast_flow import FORECAST_CHAT_LOCKS, CwlForecastFlow
from clash_sheet_sync_bot.repositories import (
    CwlForecastRepository,
    CwlForecastRound,
    CwlForecastScheduleKey,
)
from clash_sheet_sync_bot.telegram.client import TelegramApiError, TelegramBadRequestError
from clash_sheet_sync_bot.telegram.emoji_catalog import load_telegram_emoji_catalog
from tests.fakes import FakeTelegram, make_app_config

NOW = "2026-08-05T12:00:00+00:00"


def _member(tag: str, th: int, position: int | None = None) -> dict[str, object]:
    result: dict[str, object] = {"tag": tag, "name": tag, "townHallLevel": th}
    if position is not None:
        result["mapPosition"] = position
    return result


def _clan(tag: str, name: str, level: int) -> dict[str, object]:
    return {
        "tag": tag,
        "name": name,
        "clanLevel": level,
        "members": [_member(f"#P{tag[1:]}1", 18), _member(f"#P{tag[1:]}2", 17)],
    }


def _group() -> dict[str, object]:
    return {
        "state": "inWar",
        "season": "2026-08",
        "clans": [
            _clan("#AAA111", "Alpha", 20),
            _clan("#BBB222", "Beta", 19),
            _clan("#CCC333", "Gamma", 18),
            _clan("#DDD444", "Delta", 17),
        ],
        "rounds": [
            {"warTags": ["#WAR1", "#WAR2"]},
            {"warTags": ["#0", "#0"]},
            {"warTags": ["#0", "#0"]},
        ],
    }


def _war(
    home: str,
    away: str,
    *,
    state: str,
    start: str = "20260805T120000.000Z",
) -> dict[str, object]:
    return {
        "state": state,
        "teamSize": 2,
        "startTime": start,
        "clan": {
            "tag": home,
            "name": home,
            "members": [_member(f"#M{home[1:]}2", 17, 2), _member(f"#M{home[1:]}1", 18, 1)],
        },
        "opponent": {
            "tag": away,
            "name": away,
            "members": [_member(f"#M{away[1:]}2", 15, 2), _member(f"#M{away[1:]}1", 16, 1)],
        },
    }


@dataclass(slots=True)
class FakeForecastClash:
    group: dict[str, object] = field(default_factory=_group)
    group_errors: dict[str, Exception] = field(default_factory=dict)
    block_started: asyncio.Event | None = None
    unblock: asyncio.Event | None = None
    group_calls: list[str] = field(default_factory=list)
    war_calls: list[str] = field(default_factory=list)

    async def get_current_war_league_group(self, clan_tag: str) -> dict[str, Any]:
        self.group_calls.append(clan_tag)
        error = self.group_errors.get(clan_tag)
        if error is not None:
            raise error
        if self.block_started is not None and self.unblock is not None:
            self.block_started.set()
            await self.unblock.wait()
        return deepcopy(self.group)

    async def get_cwl_war(self, war_tag: str) -> dict[str, Any]:
        self.war_calls.append(war_tag)
        wars = {
            "#WAR1": _war("#AAA111", "#BBB222", state="inWar"),
            "#WAR2": _war("#CCC333", "#DDD444", state="warEnded"),
        }
        return deepcopy(wars[war_tag])


async def _insert_connected(
    connection: aiosqlite.Connection,
    *,
    chat_id: int = -1001,
    clans: tuple[tuple[str, str], ...] = (("#AAA111", "Alpha-old"),),
) -> None:
    await connection.execute(
        """INSERT INTO telegram_chats(
            chat_id, title, type, status, created_at, updated_at
        ) VALUES (?, 'Group', 'supergroup', 'ready', ?, ?)""",
        (chat_id, NOW, NOW),
    )
    for index, (tag, name) in enumerate(clans, start=1):
        await connection.execute(
            """INSERT INTO tracked_clans(
                chat_id, clan_tag, clan_name, sort_order, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (chat_id, tag, name, index * 10, NOW, NOW),
        )
    await connection.commit()


def _flow(
    connection: aiosqlite.Connection,
    telegram: FakeTelegram,
    clash: FakeForecastClash,
    *,
    dev_mode: bool = False,
) -> CwlForecastFlow:
    return CwlForecastFlow(
        config=make_app_config(dev_mode=dev_mode),
        telegram=telegram,  # type: ignore[arg-type]
        connection=connection,
        catalog=load_telegram_emoji_catalog(Path("resources/telegram_emoji_catalog.json")),
        clash_client=clash,
    )


async def _save_schedule(connection: aiosqlite.Connection) -> None:
    group = parse_league_group(_group())
    await CwlForecastRepository(connection).replace_schedule(
        key=CwlForecastScheduleKey(group.season, league_group_fingerprint(group), "#AAA111"),
        rounds=(
            CwlForecastRound(1, "#BBB222", "api"),
            CwlForecastRound(2, "#CCC333", "manual"),
            CwlForecastRound(3, "#DDD444", "manual"),
        ),
        created_by_user_id=42,
        source_chat_id=-1001,
        now=datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
    )


@pytest.fixture(autouse=True)
def _clear_locks() -> None:
    FORECAST_CHAT_LOCKS.clear()


@pytest.mark.asyncio
async def test_disconnected_group_does_not_call_clash(
    migrated_connection: aiosqlite.Connection,
) -> None:
    telegram = FakeTelegram()
    clash = FakeForecastClash()

    await _flow(migrated_connection, telegram, clash).handle_command(
        chat_id=-999, chat_type="supergroup"
    )

    assert "подключённой группе" in telegram.sent_messages[-1]["text"]
    assert clash.group_calls == []


@pytest.mark.asyncio
async def test_missing_schedule_sends_exact_instruction_and_persists_cooldown(
    migrated_connection: aiosqlite.Connection,
) -> None:
    await _insert_connected(migrated_connection)
    telegram = FakeTelegram()
    clash = FakeForecastClash()
    flow = _flow(migrated_connection, telegram, clash)

    await flow.handle_command(chat_id=-1001, chat_type="supergroup")

    assert telegram.sent_messages[-1]["text"] == (
        "Для клана Alpha не заполнено расписание будущих раундов ЛВК. "
        "Администратор группы должен выполнить команду /cwl_forecast_schedule."
    )
    assert await CwlForecastRepository(migrated_connection).get_last_started_at(-1001) is not None
    calls = len(clash.group_calls)
    await _flow(migrated_connection, telegram, clash).handle_command(
        chat_id=-1001, chat_type="supergroup"
    )
    assert "недавно запускался" in telegram.sent_messages[-1]["text"]
    assert len(clash.group_calls) == calls


@pytest.mark.asyncio
async def test_ready_forecast_uses_api_name_custom_entities_and_one_message(
    migrated_connection: aiosqlite.Connection,
) -> None:
    await _insert_connected(migrated_connection)
    await _save_schedule(migrated_connection)
    telegram = FakeTelegram()

    await _flow(migrated_connection, telegram, FakeForecastClash()).handle_command(
        chat_id=-1001, chat_type="supergroup"
    )

    assert len(telegram.sent_messages) == 1
    message = telegram.sent_messages[0]
    assert message["text"].startswith("Alpha | #AAA111\n")
    assert message["parse_mode"] is None
    assert message["entities"]


@pytest.mark.asyncio
async def test_http_400_retries_plain_exactly_once(
    migrated_connection: aiosqlite.Connection,
) -> None:
    await _insert_connected(migrated_connection)
    await _save_schedule(migrated_connection)
    telegram = FakeTelegram(send_errors=[TelegramBadRequestError("400"), None])

    await _flow(migrated_connection, telegram, FakeForecastClash()).handle_command(
        chat_id=-1001, chat_type="supergroup"
    )

    assert len(telegram.sent_messages) == 2
    assert telegram.sent_messages[0]["entities"]
    assert telegram.sent_messages[1]["entities"] is None
    assert telegram.sent_messages[1]["text"].splitlines()[2] == "18|16|18|18"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "errors",
    (
        [TelegramApiError("network")],
        [TelegramBadRequestError("400"), TelegramApiError("fallback failed")],
    ),
)
async def test_delivery_error_never_starts_extra_retry(
    migrated_connection: aiosqlite.Connection,
    errors: list[Exception | None],
) -> None:
    await _insert_connected(migrated_connection)
    await _save_schedule(migrated_connection)
    telegram = FakeTelegram(send_errors=list(errors))

    await _flow(migrated_connection, telegram, FakeForecastClash()).handle_command(
        chat_id=-1001, chat_type="supergroup"
    )

    assert len(telegram.sent_messages) == len(errors)


@pytest.mark.asyncio
async def test_partial_api_error_sends_ready_before_one_summary(
    migrated_connection: aiosqlite.Connection,
) -> None:
    await _insert_connected(
        migrated_connection,
        clans=(("#AAA111", "Alpha-old"), ("#CCC333", "Gamma-old")),
    )
    await _save_schedule(migrated_connection)
    clash = FakeForecastClash(group_errors={"#CCC333": RuntimeError("boom")})
    telegram = FakeTelegram()

    await _flow(migrated_connection, telegram, clash).handle_command(
        chat_id=-1001, chat_type="supergroup"
    )

    assert telegram.sent_messages[0]["text"].startswith("Alpha | #AAA111")
    assert telegram.sent_messages[1]["text"] == (
        "Не удалось сформировать прогноз ЛВК для кланов: Gamma-old"
    )


@pytest.mark.asyncio
async def test_per_run_war_cache_loads_each_war_tag_once(
    migrated_connection: aiosqlite.Connection,
) -> None:
    await _insert_connected(
        migrated_connection,
        clans=(("#AAA111", "Alpha"), ("#CCC333", "Gamma")),
    )
    await _save_schedule(migrated_connection)
    clash = FakeForecastClash()

    await _flow(migrated_connection, FakeTelegram(), clash).handle_command(
        chat_id=-1001, chat_type="supergroup"
    )

    assert clash.war_calls.count("#WAR1") == 1
    assert clash.war_calls.count("#WAR2") == 1


@pytest.mark.asyncio
async def test_cooldown_is_written_before_api_and_singleflight_text_is_exact(
    migrated_connection: aiosqlite.Connection,
) -> None:
    await _insert_connected(migrated_connection)
    started = asyncio.Event()
    unblock = asyncio.Event()
    clash = FakeForecastClash(block_started=started, unblock=unblock)
    telegram = FakeTelegram()
    flow = _flow(migrated_connection, telegram, clash, dev_mode=True)
    first = asyncio.create_task(flow.handle_command(chat_id=-1001, chat_type="supergroup"))
    await started.wait()

    assert await CwlForecastRepository(migrated_connection).get_last_started_at(-1001) is not None
    await flow.handle_command(chat_id=-1001, chat_type="supergroup")
    assert telegram.sent_messages[-1]["text"] == "Прогноз ЛВК уже формируется"
    unblock.set()
    await first


@pytest.mark.asyncio
async def test_dev_mode_bypasses_time_cooldown_between_runs(
    migrated_connection: aiosqlite.Connection,
) -> None:
    await _insert_connected(migrated_connection)
    clash = FakeForecastClash()
    flow = _flow(migrated_connection, FakeTelegram(), clash, dev_mode=True)

    await flow.handle_command(chat_id=-1001, chat_type="supergroup")
    await flow.handle_command(chat_id=-1001, chat_type="supergroup")

    assert clash.group_calls == ["#AAA111", "#AAA111"]
