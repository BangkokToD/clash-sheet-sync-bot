"""Integration tests администраторского schedule flow ЛВК."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import aiosqlite
import pytest

from clash_sheet_sync_bot.cwl_forecast.schedule_flow import (
    CwlForecastScheduleFlow,
    callback_data_fits_limit,
)
from clash_sheet_sync_bot.repositories import CwlForecastRepository
from tests.fakes import FakeTelegram, RecordingAccessService, make_app_config

NOW = "2026-08-05T12:00:00+00:00"


def _clan(tag: str, name: str, level: int) -> dict[str, object]:
    return {
        "tag": tag,
        "name": name,
        "clanLevel": level,
        "members": [{"tag": f"#P{tag[1:]}", "name": "P", "townHallLevel": 18}],
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


def _war(home: str, away: str) -> dict[str, object]:
    return {
        "state": "warEnded",
        "teamSize": 1,
        "startTime": "20260801T120000.000Z",
        "clan": {"tag": home, "name": home, "members": []},
        "opponent": {"tag": away, "name": away, "members": []},
    }


@dataclass(slots=True)
class FakeScheduleClash:
    group: dict[str, object] = field(default_factory=_group)
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def get_current_war_league_group(self, clan_tag: str) -> dict[str, Any]:
        self.calls.append(("group", clan_tag))
        return deepcopy(self.group)

    async def get_cwl_war(self, war_tag: str) -> dict[str, Any]:
        self.calls.append(("war", war_tag))
        wars = {
            "#WAR1": _war("#AAA111", "#BBB222"),
            "#WAR2": _war("#CCC333", "#DDD444"),
        }
        return deepcopy(wars[war_tag])


async def _insert_connected(
    connection: aiosqlite.Connection,
    *,
    chat_id: int = -1001,
    clans: tuple[tuple[str, str], ...] = (("#AAA111", "Alpha"),),
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
    access: RecordingAccessService,
    clash: FakeScheduleClash,
) -> CwlForecastScheduleFlow:
    return CwlForecastScheduleFlow(
        config=make_app_config(),
        telegram=telegram,  # type: ignore[arg-type]
        connection=connection,
        access=access,  # type: ignore[arg-type]
        clash_client=clash,
    )


def _buttons(message: dict[str, Any]) -> list[dict[str, str]]:
    markup = message["reply_markup"]
    assert isinstance(markup, dict)
    return [row[0] for row in markup["inline_keyboard"]]


def _session_id(message: dict[str, Any]) -> str:
    pick = next(button for button in _buttons(message) if ":pick:" in button["callback_data"])
    return pick["callback_data"].split(":")[1]


@pytest.mark.asyncio
async def test_non_admin_is_denied_before_api_or_session(
    migrated_connection: aiosqlite.Connection,
) -> None:
    await _insert_connected(migrated_connection)
    telegram = FakeTelegram()
    access = RecordingAccessService(is_admin_result=False)
    clash = FakeScheduleClash()

    await _flow(migrated_connection, telegram, access, clash).handle_command(
        chat_id=-1001, chat_type="supergroup", user_id=42
    )

    assert telegram.sent_messages[-1]["text"] == "Нет доступа."
    assert clash.calls == []
    assert access.calls == [{"chat_id": -1001, "user_id": 42, "force_refresh": True}]


@pytest.mark.asyncio
async def test_single_clan_starts_session_with_exact_one_clan_per_row(
    migrated_connection: aiosqlite.Connection,
) -> None:
    await _insert_connected(migrated_connection)
    telegram = FakeTelegram()
    clash = FakeScheduleClash()

    await _flow(migrated_connection, telegram, RecordingAccessService(), clash).handle_command(
        chat_id=-1001, chat_type="supergroup", user_id=42
    )

    buttons = _buttons(telegram.sent_messages[-1])
    labels = [button["text"] for button in buttons]
    assert "Gamma · ур. 18 · #CCC333" in labels
    assert "Delta · ур. 17 · #DDD444" in labels
    assert not any("Alpha" in label or "Beta" in label for label in labels)
    assert all(callback_data_fits_limit(button["callback_data"]) for button in buttons)


@pytest.mark.asyncio
async def test_pick_removes_selected_opponent_then_confirm_is_global(
    migrated_connection: aiosqlite.Connection,
) -> None:
    await _insert_connected(migrated_connection)
    telegram = FakeTelegram()
    access = RecordingAccessService()
    clash = FakeScheduleClash()
    flow = _flow(migrated_connection, telegram, access, clash)
    await flow.handle_command(chat_id=-1001, chat_type="supergroup", user_id=42)
    session_id = _session_id(telegram.sent_messages[-1])

    await flow.handle_callback(
        callback_data=f"cf:{session_id}:pick:2",
        callback_query_id="pick-2",
        chat_id=-1001,
        message_id=1,
        user_id=42,
    )
    labels = [button["text"] for button in _buttons(telegram.sent_messages[-1])]
    assert not any("Gamma" in label for label in labels)
    assert any("Delta" in label for label in labels)

    await flow.handle_callback(
        callback_data=f"cf:{session_id}:pick:3",
        callback_query_id="pick-3",
        chat_id=-1001,
        message_id=1,
        user_id=42,
    )
    assert {button["text"] for button in _buttons(telegram.sent_messages[-1])} == {
        "Подтвердить",
        "Назад",
        "Отмена",
    }
    await flow.handle_callback(
        callback_data=f"cf:{session_id}:confirm",
        callback_query_id="confirm",
        chat_id=-1001,
        message_id=1,
        user_id=42,
    )

    repository = CwlForecastRepository(migrated_connection)
    assert await repository.get_session(session_id) is None
    cursor = await migrated_connection.execute(
        "SELECT season, group_fingerprint, clan_tag FROM cwl_forecast_schedules"
    )
    row = await cursor.fetchone()
    assert row is not None
    from clash_sheet_sync_bot.repositories import CwlForecastScheduleKey

    schedule = await repository.get_schedule(
        CwlForecastScheduleKey(row["season"], row["group_fingerprint"], row["clan_tag"])
    )
    assert schedule is not None
    assert [
        (item.round_number, item.opponent_clan_tag, item.source) for item in schedule.rounds
    ] == [
        (1, "#BBB222", "api"),
        (2, "#CCC333", "manual"),
        (3, "#DDD444", "manual"),
    ]


@pytest.mark.asyncio
async def test_callback_rechecks_revoked_admin_and_does_not_change_draft(
    migrated_connection: aiosqlite.Connection,
) -> None:
    await _insert_connected(migrated_connection)
    telegram = FakeTelegram()
    access = RecordingAccessService()
    flow = _flow(migrated_connection, telegram, access, FakeScheduleClash())
    await flow.handle_command(chat_id=-1001, chat_type="supergroup", user_id=42)
    session_id = _session_id(telegram.sent_messages[-1])
    before = await CwlForecastRepository(migrated_connection).get_session(session_id)
    access.is_admin_result = False

    await flow.handle_callback(
        callback_data=f"cf:{session_id}:pick:2",
        callback_query_id="revoked",
        chat_id=-1001,
        message_id=1,
        user_id=42,
    )

    after = await CwlForecastRepository(migrated_connection).get_session(session_id)
    assert after == before
    assert access.calls[-1]["force_refresh"] is True
    assert telegram.answered_callbacks[-1]["text"] == "Нет доступа."


@pytest.mark.asyncio
async def test_other_user_cannot_control_session(
    migrated_connection: aiosqlite.Connection,
) -> None:
    await _insert_connected(migrated_connection)
    telegram = FakeTelegram()
    flow = _flow(migrated_connection, telegram, RecordingAccessService(), FakeScheduleClash())
    await flow.handle_command(chat_id=-1001, chat_type="supergroup", user_id=42)
    session_id = _session_id(telegram.sent_messages[-1])

    await flow.handle_callback(
        callback_data=f"cf:{session_id}:cancel",
        callback_query_id="foreign-user",
        chat_id=-1001,
        message_id=1,
        user_id=99,
    )

    assert await CwlForecastRepository(migrated_connection).get_session(session_id) is not None
    assert telegram.answered_callbacks[-1]["show_alert"] is True


@pytest.mark.asyncio
async def test_cancel_does_not_write_schedule(
    migrated_connection: aiosqlite.Connection,
) -> None:
    await _insert_connected(migrated_connection)
    telegram = FakeTelegram()
    flow = _flow(migrated_connection, telegram, RecordingAccessService(), FakeScheduleClash())
    await flow.handle_command(chat_id=-1001, chat_type="supergroup", user_id=42)
    session_id = _session_id(telegram.sent_messages[-1])

    await flow.handle_callback(
        callback_data=f"cf:{session_id}:cancel",
        callback_query_id="cancel",
        chat_id=-1001,
        message_id=1,
        user_id=42,
    )

    cursor = await migrated_connection.execute("SELECT COUNT(*) FROM cwl_forecast_schedules")
    assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_stale_fingerprint_invalidates_session(
    migrated_connection: aiosqlite.Connection,
) -> None:
    await _insert_connected(migrated_connection)
    telegram = FakeTelegram()
    clash = FakeScheduleClash()
    flow = _flow(migrated_connection, telegram, RecordingAccessService(), clash)
    await flow.handle_command(chat_id=-1001, chat_type="supergroup", user_id=42)
    session_id = _session_id(telegram.sent_messages[-1])
    clans = clash.group["clans"]
    assert isinstance(clans, list) and isinstance(clans[-1], dict)
    clans[-1]["tag"] = "#EEE555"

    await flow.handle_callback(
        callback_data=f"cf:{session_id}:pick:2",
        callback_query_id="stale",
        chat_id=-1001,
        message_id=1,
        user_id=42,
    )

    assert await CwlForecastRepository(migrated_connection).get_session(session_id) is None
    assert "изменилась" in telegram.answered_callbacks[-1]["text"]


@pytest.mark.asyncio
async def test_multiple_tracked_clans_first_renders_clan_selector(
    migrated_connection: aiosqlite.Connection,
) -> None:
    await _insert_connected(
        migrated_connection,
        clans=(("#AAA111", "Alpha"), ("#CCC333", "Gamma")),
    )
    telegram = FakeTelegram()

    await _flow(
        migrated_connection, telegram, RecordingAccessService(), FakeScheduleClash()
    ).handle_command(chat_id=-1001, chat_type="supergroup", user_id=42)

    assert "Выберите клан" in telegram.sent_messages[-1]["text"]
    assert [button["callback_data"] for button in _buttons(telegram.sent_messages[-1])] == [
        "cf:new:0",
        "cf:new:1",
    ]


def test_callback_codec_stays_within_telegram_limit() -> None:
    assert callback_data_fits_limit("cf:abcdefgh:pick:999")
    assert not callback_data_fits_limit("cf:" + "я" * 40)
