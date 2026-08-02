"""SQLite-backed тесты sync-service error/write contracts."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import aiosqlite
import pytest
from fakes import (
    FakeTelegram,
    make_app_config,
    make_raid_sheet_sync_result,
    make_runtime_config,
    make_sheet_binding,
)

import clash_sheet_sync_bot.sync.service as sync_service
from clash_sheet_sync_bot.sheets.client import GoogleSheetsWriteError
from clash_sheet_sync_bot.sync.composition import PreparedCompositionSync
from clash_sheet_sync_bot.sync.cwl import CwlPreparedData
from clash_sheet_sync_bot.sync.raids import PreparedRaidSync, RaidDataError
from clash_sheet_sync_bot.sync.service import (
    PARTIAL_SHEET_WRITE_WARNING,
    UNEXPECTED_SYNC_ERROR_REASON,
    WRITE_PHASE_COMPOSITION_WRITTEN,
    WRITE_PHASE_CWL_WRITTEN,
    WRITE_PHASE_PREPARED,
    WRITE_PHASE_RAIDS_WRITTEN,
    SyncService,
    _sync_error_reason,
    _write_reconciled_bot_state,
)
from clash_sheet_sync_bot.telegram.client import TelegramApiError

NOW = "2026-07-09T12:00:00+00:00"


class _TelegramChatsStub:
    """Минимальный stub времени последнего `/sync`."""

    def __init__(self, last_sync_started_at: str) -> None:
        self.last_sync_started_at = last_sync_started_at
        self.calls = 0

    async def get_last_sync_started_at(self, chat_id: int) -> str:
        """Возвращает время последнего запуска и считает обращения."""

        self.calls += 1
        return self.last_sync_started_at


async def _insert_ready_chat(
    connection: aiosqlite.Connection,
    *,
    chat_id: int,
) -> None:
    """Создаёт минимальную готовую группу для RuntimeConfigRepository."""

    await connection.execute(
        """
        INSERT INTO telegram_chats(
            chat_id,
            title,
            type,
            status,
            created_by_user_id,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chat_id,
            "Test group",
            "supergroup",
            "ready",
            1001,
            NOW,
            NOW,
        ),
    )
    await connection.execute(
        """
        INSERT INTO sheet_bindings(
            chat_id,
            google_sheet_id,
            spreadsheet_url,
            composition_sheet_name,
            composition_sheet_id,
            active_cwl_sheet_name,
            active_cwl_sheet_id,
            active_cwl_season,
            bot_state_sheet_name,
            bot_state_sheet_id,
            timezone,
            is_active,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            chat_id,
            f"sheet-{abs(chat_id)}",
            f"https://docs.google.com/spreadsheets/d/sheet-{abs(chat_id)}/edit",
            "Состав",
            111,
            "CWL",
            222,
            "2026-07",
            "_bot_state",
            333,
            "Europe/Kyiv",
            NOW,
            NOW,
        ),
    )
    await connection.execute(
        """
        INSERT INTO tracked_clans(
            chat_id,
            clan_tag,
            clan_name,
            sort_order,
            is_active,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, 1, ?, ?)
        """,
        (
            chat_id,
            "#AAA111",
            "Alpha",
            10,
            NOW,
            NOW,
        ),
    )
    await connection.commit()


def _prepared_composition() -> PreparedCompositionSync:
    """Создаёт минимальный prepared composition result."""

    return PreparedCompositionSync(
        planned_states={},
        built_blocks=(),
        active_counts=(("Alpha", 0),),
        exited_count=0,
        diff_items=(),
        warnings=(),
    )


def _prepared_cwl() -> CwlPreparedData:
    """Создаёт минимальный prepared CWL result."""

    return CwlPreparedData(
        season=None,
        clan_blocks=(),
        rows=(),
        all_not_in_progress=True,
        not_in_progress_clans=(),
        warnings=(),
        diff_items=(),
    )


async def _successful_prepare_composition(**_: Any) -> PreparedCompositionSync:
    """Fake successful prepare_composition_sync."""

    return _prepared_composition()


async def _successful_prepare_cwl(**_: Any) -> CwlPreparedData:
    """Fake successful prepare_public_cwl_sync."""

    return _prepared_cwl()


async def _successful_apply_composition(**_: Any) -> None:
    """Fake successful apply_prepared_composition_sync."""


async def _successful_apply_cwl(**_: Any) -> None:
    """Fake successful apply_public_cwl_sync."""

    return None


def _prepared_raid() -> PreparedRaidSync:
    """Создаёт минимальный prepared raid result."""

    return PreparedRaidSync(
        selected_season=None,
        previous_active_season=None,
        empty_blocks=(),
    )


async def _successful_prepare_raid(**_: Any) -> PreparedRaidSync:
    """Fake successful prepare_public_raid_sync."""

    return _prepared_raid()


async def _successful_apply_raid(**_: Any) -> Any:
    """Fake successful apply_public_raid_sync."""

    return make_raid_sheet_sync_result(
        season_key=None,
        season_state=None,
        rows_count=0,
        attacks_complete_count=0,
        attacks_below_target_count=0,
    )


async def _successful_write_bot_state(**_: Any) -> None:
    """Fake successful final `_bot_state` write."""


async def _failing_apply_composition(**_: Any) -> None:
    """Fake Google Sheets failure после начала записи состава."""

    raise GoogleSheetsWriteError("Google write failed")


async def _unexpected_prepare_composition(**_: Any) -> PreparedCompositionSync:
    """Fake unexpected failure до начала записи Google Sheets."""

    raise RuntimeError("raw secret traceback detail")


def _patch_successful_sync_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подменяет внешний HTTP/Google/CoC pipeline безопасными fake-функциями."""

    monkeypatch.setattr(sync_service, "GoogleAccessTokenProvider", lambda _: object())
    monkeypatch.setattr(sync_service, "prepare_composition_sync", _successful_prepare_composition)
    monkeypatch.setattr(sync_service, "prepare_public_cwl_sync", _successful_prepare_cwl)
    monkeypatch.setattr(sync_service, "prepare_public_raid_sync", _successful_prepare_raid)
    monkeypatch.setattr(
        sync_service, "apply_prepared_composition_sync", _successful_apply_composition
    )
    monkeypatch.setattr(sync_service, "apply_public_cwl_sync", _successful_apply_cwl)
    monkeypatch.setattr(sync_service, "apply_public_raid_sync", _successful_apply_raid)
    monkeypatch.setattr(sync_service, "_write_reconciled_bot_state", _successful_write_bot_state)


async def _last_sync_run(connection: aiosqlite.Connection) -> aiosqlite.Row:
    """Читает последний sync_run."""

    cursor = await connection.execute(
        """
        SELECT status, error_stage, error_message, report_json
        FROM sync_runs
        ORDER BY id DESC
        LIMIT 1
        """
    )
    row = await cursor.fetchone()
    assert row is not None
    return row


async def _chat_sync_status(connection: aiosqlite.Connection, chat_id: int) -> aiosqlite.Row:
    """Читает sync status из telegram_chats."""

    cursor = await connection.execute(
        """
        SELECT last_sync_status, last_sync_error
        FROM telegram_chats
        WHERE chat_id = ?
        """,
        (chat_id,),
    )
    row = await cursor.fetchone()
    assert row is not None
    return row


def test_sync_error_reason_before_sheet_write_has_no_partial_warning() -> None:
    """Проверяет, что ошибка до записи Sheets не получает partial warning."""

    reason = _sync_error_reason("CoC API failed", WRITE_PHASE_PREPARED)

    assert reason == "CoC API failed"
    assert PARTIAL_SHEET_WRITE_WARNING not in reason


def test_sync_error_reason_after_sheet_write_adds_partial_warning() -> None:
    """Проверяет partial warning после начала записи Sheets."""

    reason = _sync_error_reason("Google write failed", WRITE_PHASE_COMPOSITION_WRITTEN)

    assert "Google write failed" in reason
    assert PARTIAL_SHEET_WRITE_WARNING in reason


@pytest.mark.parametrize(
    "write_phase",
    (WRITE_PHASE_COMPOSITION_WRITTEN, WRITE_PHASE_CWL_WRITTEN, WRITE_PHASE_RAIDS_WRITTEN),
)
def test_all_write_phases_add_partial_warning(write_phase: str) -> None:
    """Проверяет partial warning для каждой Sheets write phase."""

    assert PARTIAL_SHEET_WRITE_WARNING in _sync_error_reason("write failed", write_phase)


@pytest.mark.asyncio
async def test_write_reconciled_bot_state_uses_final_cwl_and_raid_binding() -> None:
    """Проверяет финальное `_bot_state` mirror после всех apply."""

    class RecordingSheets:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def write_values(self, **kwargs: Any) -> None:
            self.calls.append(kwargs)

    runtime = make_runtime_config(
        sheet_binding=make_sheet_binding(
            active_cwl_sheet_id=223,
            active_cwl_season="2026-08",
            active_raid_sheet_id=445,
            active_raid_season="2026-07-31T07:00:00+00:00",
        )
    )
    sheets = RecordingSheets()

    await _write_reconciled_bot_state(
        runtime_config=runtime,
        sheets_client=sheets,  # type: ignore[arg-type]
    )

    assert len(sheets.calls) == 1
    call = sheets.calls[0]
    state = dict(call["values"])
    assert call["sheet_name"] == "_bot_state"
    assert call["range_a1"] == f"A1:B{len(call['values'])}"
    assert state["active_cwl_sheet_id"] == 223
    assert state["active_cwl_season"] == "2026-08"
    assert state["active_raid_sheet_id"] == 445
    assert state["active_raid_season"] == "2026-07-31T07:00:00+00:00"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dev_mode", "expected_retry_after"),
    (
        (False, 60),
        (True, 0),
    ),
)
async def test_dev_mode_controls_sync_cooldown(
    monkeypatch: pytest.MonkeyPatch,
    dev_mode: bool,
    expected_retry_after: int,
) -> None:
    """Проверяет, что DEV_MODE отключает только cooldown `/sync`."""

    chat_id = -1500
    monkeypatch.setattr(sync_service, "_utc_now", lambda: datetime.fromisoformat(NOW))

    service = SyncService(
        config=make_app_config(dev_mode=dev_mode),
        telegram=FakeTelegram(),  # type: ignore[arg-type]
        connection=object(),
    )
    telegram_chats = _TelegramChatsStub(NOW)
    service._telegram_chats = telegram_chats  # type: ignore[assignment]

    retry_after = await service._rate_limit_retry_after(chat_id)

    assert retry_after == expected_retry_after
    assert telegram_chats.calls == int(not dev_mode)


@pytest.mark.asyncio
async def test_run_sync_records_error_stage_and_partial_warning_after_composition_write(
    migrated_connection: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверяет error_stage и partial warning при ошибке записи состава."""

    chat_id = -1501
    telegram = FakeTelegram()
    await _insert_ready_chat(migrated_connection, chat_id=chat_id)

    _patch_successful_sync_pipeline(monkeypatch)
    monkeypatch.setattr(sync_service, "apply_prepared_composition_sync", _failing_apply_composition)

    service = SyncService(
        config=make_app_config(),
        telegram=telegram,  # type: ignore[arg-type]
        connection=migrated_connection,
    )

    await service._run_sync(runtime_chat_id=chat_id, user_id=1001)

    sync_run = await _last_sync_run(migrated_connection)
    chat_status = await _chat_sync_status(migrated_connection, chat_id)

    assert sync_run["status"] == "error"
    assert sync_run["error_stage"] == WRITE_PHASE_COMPOSITION_WRITTEN
    assert "Google write failed" in sync_run["error_message"]
    assert PARTIAL_SHEET_WRITE_WARNING in sync_run["error_message"]

    assert chat_status["last_sync_status"] == "error"
    assert PARTIAL_SHEET_WRITE_WARNING in chat_status["last_sync_error"]

    assert telegram.sent_messages
    assert PARTIAL_SHEET_WRITE_WARNING in telegram.sent_messages[-1]["text"]


@pytest.mark.asyncio
async def test_run_sync_hides_raw_unexpected_exception_from_user_and_logs_exception(
    migrated_connection: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Проверяет safe reason для unexpected exception и logger.exception."""

    chat_id = -1502
    telegram = FakeTelegram()
    await _insert_ready_chat(migrated_connection, chat_id=chat_id)

    _patch_successful_sync_pipeline(monkeypatch)
    monkeypatch.setattr(sync_service, "prepare_composition_sync", _unexpected_prepare_composition)

    service = SyncService(
        config=make_app_config(),
        telegram=telegram,  # type: ignore[arg-type]
        connection=migrated_connection,
    )

    with caplog.at_level(logging.ERROR, logger="sync_service"):
        await service._run_sync(runtime_chat_id=chat_id, user_id=1001)

    sync_run = await _last_sync_run(migrated_connection)

    assert sync_run["status"] == "error"
    assert sync_run["error_stage"] == WRITE_PHASE_PREPARED
    assert UNEXPECTED_SYNC_ERROR_REASON in sync_run["error_message"]
    assert "raw secret traceback detail" not in sync_run["error_message"]
    assert PARTIAL_SHEET_WRITE_WARNING not in sync_run["error_message"]

    assert telegram.sent_messages
    assert UNEXPECTED_SYNC_ERROR_REASON in telegram.sent_messages[-1]["text"]
    assert "raw secret traceback detail" not in telegram.sent_messages[-1]["text"]

    assert "unexpected sync failure" in caplog.text


@pytest.mark.asyncio
async def test_telegram_delivery_failure_after_success_keeps_success_status(
    migrated_connection: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Проверяет, что Telegram failure после success не откатывает успешный sync."""

    chat_id = -1503
    telegram = FakeTelegram(send_error=TelegramApiError("send failed"))
    await _insert_ready_chat(migrated_connection, chat_id=chat_id)

    _patch_successful_sync_pipeline(monkeypatch)

    service = SyncService(
        config=make_app_config(),
        telegram=telegram,  # type: ignore[arg-type]
        connection=migrated_connection,
    )

    with caplog.at_level(logging.WARNING, logger="sync_service"):
        await service._run_sync(runtime_chat_id=chat_id, user_id=1001)

    sync_run = await _last_sync_run(migrated_connection)
    chat_status = await _chat_sync_status(migrated_connection, chat_id)

    assert sync_run["status"] == "success"
    assert sync_run["error_stage"] is None
    assert sync_run["error_message"] is None

    assert chat_status["last_sync_status"] == "success"
    assert chat_status["last_sync_error"] is None

    assert "sync finished, but telegram report delivery failed" in caplog.text


@pytest.mark.asyncio
async def test_success_report_uses_configured_support_chat_link(
    migrated_connection: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверяет чтение актуальной support URL из singleton-настройки."""

    chat_id = -1504
    telegram = FakeTelegram()
    await _insert_ready_chat(migrated_connection, chat_id=chat_id)
    await migrated_connection.execute(
        """
        UPDATE bot_settings
        SET support_chat_id = ?, support_chat_title = ?, support_url = ?,
            updated_by_user_id = ?, updated_at = ?
        WHERE singleton_id = 1
        """,
        (-9001, "Support", "https://t.me/+support?a=1&b=2", 1001, NOW),
    )
    await migrated_connection.commit()
    _patch_successful_sync_pipeline(monkeypatch)

    await SyncService(
        config=make_app_config(),
        telegram=telegram,  # type: ignore[arg-type]
        connection=migrated_connection,
    )._run_sync(runtime_chat_id=chat_id, user_id=1001)

    assert telegram.sent_messages
    assert (
        '<a href="https://docs.google.com/spreadsheets/d/sheet-1504/edit">Таблица</a>'
        in telegram.sent_messages[-1]["text"]
    )
    assert (
        '<a href="https://t.me/+support?a=1&amp;b=2">Чат Леши</a>'
        in telegram.sent_messages[-1]["text"]
    )


@pytest.mark.asyncio
async def test_run_sync_prepares_all_domains_before_any_write(
    migrated_connection: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверяет порядок composition/CWL/raid prepare и apply."""

    chat_id = -1510
    await _insert_ready_chat(migrated_connection, chat_id=chat_id)
    calls: list[str] = []
    composition_state = object()
    _patch_successful_sync_pipeline(monkeypatch)
    original_commit = migrated_connection.commit

    async def commit() -> None:
        calls.append("commit")
        await original_commit()

    monkeypatch.setattr(migrated_connection, "commit", commit)

    async def prepare_composition(**_: Any) -> PreparedCompositionSync:
        calls.append("prepare composition")
        prepared = _prepared_composition()
        return PreparedCompositionSync(
            planned_states={"#PLAYER": composition_state},  # type: ignore[dict-item]
            built_blocks=prepared.built_blocks,
            active_counts=prepared.active_counts,
            exited_count=prepared.exited_count,
            diff_items=prepared.diff_items,
            warnings=prepared.warnings,
        )

    async def prepare_cwl(**kwargs: Any) -> CwlPreparedData:
        calls.append("prepare cwl")
        assert kwargs["composition_player_states"] == (composition_state,)
        return _prepared_cwl()

    async def prepare_raid(**kwargs: Any) -> PreparedRaidSync:
        calls.append("prepare raids")
        assert kwargs["composition_player_states"] == (composition_state,)
        return _prepared_raid()

    async def apply_composition(**_: Any) -> None:
        calls.append("apply composition")

    async def apply_cwl(**_: Any) -> None:
        calls.append("apply cwl")

    async def apply_raid(**_: Any) -> Any:
        calls.append("apply raids")
        return await _successful_apply_raid()

    async def write_bot_state(**_: Any) -> None:
        calls.append("write bot state")

    monkeypatch.setattr(sync_service, "prepare_composition_sync", prepare_composition)
    monkeypatch.setattr(sync_service, "prepare_public_cwl_sync", prepare_cwl)
    monkeypatch.setattr(sync_service, "prepare_public_raid_sync", prepare_raid)
    monkeypatch.setattr(sync_service, "apply_prepared_composition_sync", apply_composition)
    monkeypatch.setattr(sync_service, "apply_public_cwl_sync", apply_cwl)
    monkeypatch.setattr(sync_service, "apply_public_raid_sync", apply_raid)
    monkeypatch.setattr(sync_service, "_write_reconciled_bot_state", write_bot_state)

    await SyncService(
        config=make_app_config(),
        telegram=FakeTelegram(),  # type: ignore[arg-type]
        connection=migrated_connection,
    )._run_sync(runtime_chat_id=chat_id, user_id=1001)

    assert calls == [
        "commit",
        "prepare composition",
        "prepare cwl",
        "prepare raids",
        "apply composition",
        "apply cwl",
        "apply raids",
        "write bot state",
        "commit",
    ]


@pytest.mark.asyncio
async def test_raid_preparation_error_happens_before_all_sheet_writes(
    migrated_connection: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверяет domain error raid preparation до любого apply."""

    chat_id = -1511
    await _insert_ready_chat(migrated_connection, chat_id=chat_id)
    _patch_successful_sync_pipeline(monkeypatch)
    apply_calls: list[str] = []

    async def failing_prepare(**_: Any) -> PreparedRaidSync:
        raise RaidDataError("raid contract broken")

    async def forbidden_apply(**_: Any) -> None:
        apply_calls.append("write")

    monkeypatch.setattr(sync_service, "prepare_public_raid_sync", failing_prepare)
    monkeypatch.setattr(sync_service, "apply_prepared_composition_sync", forbidden_apply)
    monkeypatch.setattr(sync_service, "apply_public_cwl_sync", forbidden_apply)
    monkeypatch.setattr(sync_service, "apply_public_raid_sync", forbidden_apply)
    monkeypatch.setattr(sync_service, "_write_reconciled_bot_state", forbidden_apply)

    await SyncService(
        config=make_app_config(),
        telegram=FakeTelegram(),  # type: ignore[arg-type]
        connection=migrated_connection,
    )._run_sync(runtime_chat_id=chat_id, user_id=1001)

    sync_run = await _last_sync_run(migrated_connection)
    assert apply_calls == []
    assert sync_run["status"] == "error"
    assert sync_run["error_stage"] == WRITE_PHASE_PREPARED
    assert sync_run["error_message"] == "raid contract broken"
    assert PARTIAL_SHEET_WRITE_WARNING not in sync_run["error_message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failing_function", "expected_phase"),
    (
        ("apply_public_cwl_sync", WRITE_PHASE_CWL_WRITTEN),
        ("apply_public_raid_sync", WRITE_PHASE_RAIDS_WRITTEN),
    ),
)
async def test_run_sync_records_partial_warning_for_later_write_failures(
    migrated_connection: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
    failing_function: str,
    expected_phase: str,
) -> None:
    """Проверяет CWL/raid write stage и общий partial warning."""

    chat_id = -1512 if failing_function == "apply_public_cwl_sync" else -1513
    await _insert_ready_chat(migrated_connection, chat_id=chat_id)
    _patch_successful_sync_pipeline(monkeypatch)

    async def fail(**_: Any) -> None:
        raise GoogleSheetsWriteError("later write failed")

    monkeypatch.setattr(sync_service, failing_function, fail)

    await SyncService(
        config=make_app_config(),
        telegram=FakeTelegram(),  # type: ignore[arg-type]
        connection=migrated_connection,
    )._run_sync(runtime_chat_id=chat_id, user_id=1001)

    sync_run = await _last_sync_run(migrated_connection)
    assert sync_run["status"] == "error"
    assert sync_run["error_stage"] == expected_phase
    assert PARTIAL_SHEET_WRITE_WARNING in sync_run["error_message"]


@pytest.mark.asyncio
async def test_recoverable_raid_cleanup_warning_commits_success_without_partial_text(
    migrated_connection: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверяет successful cleanup warning без generic partial warning."""

    chat_id = -1514
    telegram = FakeTelegram()
    await _insert_ready_chat(migrated_connection, chat_id=chat_id)
    _patch_successful_sync_pipeline(monkeypatch)
    cleanup_warning = "Raid cleanup не завершён: deleteSheet failed."

    async def apply_raid(**_: Any) -> Any:
        return make_raid_sheet_sync_result(warnings=(cleanup_warning,))

    monkeypatch.setattr(sync_service, "apply_public_raid_sync", apply_raid)

    await SyncService(
        config=make_app_config(),
        telegram=telegram,  # type: ignore[arg-type]
        connection=migrated_connection,
    )._run_sync(runtime_chat_id=chat_id, user_id=1001)

    sync_run = await _last_sync_run(migrated_connection)
    assert sync_run["status"] == "success"
    assert cleanup_warning in sync_run["report_json"]
    assert PARTIAL_SHEET_WRITE_WARNING not in sync_run["report_json"]


@pytest.mark.asyncio
async def test_final_bot_state_write_failure_is_raid_partial_write_error(
    migrated_connection: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверяет phase и rollback при ошибке финального `_bot_state`."""

    chat_id = -1515
    await _insert_ready_chat(migrated_connection, chat_id=chat_id)
    _patch_successful_sync_pipeline(monkeypatch)

    async def fail(**_: Any) -> None:
        raise GoogleSheetsWriteError("bot state write failed")

    monkeypatch.setattr(sync_service, "_write_reconciled_bot_state", fail)

    await SyncService(
        config=make_app_config(),
        telegram=FakeTelegram(),  # type: ignore[arg-type]
        connection=migrated_connection,
    )._run_sync(runtime_chat_id=chat_id, user_id=1001)

    sync_run = await _last_sync_run(migrated_connection)
    assert sync_run["status"] == "error"
    assert sync_run["error_stage"] == WRITE_PHASE_RAIDS_WRITTEN
    assert "bot state write failed" in sync_run["error_message"]
    assert PARTIAL_SHEET_WRITE_WARNING in sync_run["error_message"]


@pytest.mark.asyncio
async def test_run_sync_loads_bound_and_latest_raid_snapshots_for_active_clans(
    migrated_connection: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверяет SQLite fallback scope для bound и latest raid seasons."""

    chat_id = -1516
    await _insert_ready_chat(migrated_connection, chat_id=chat_id)
    await migrated_connection.execute(
        "UPDATE sheet_bindings SET active_raid_season = ? WHERE chat_id = ?",
        ("2026-07-17T07:00:00+00:00", chat_id),
    )
    await migrated_connection.commit()
    _patch_successful_sync_pipeline(monkeypatch)
    repository_instances: list[Any] = []
    captured_saved_rows: list[Any] = []

    class RecordingRaidRepository:
        def __init__(self, connection: Any) -> None:
            assert connection is migrated_connection
            self.list_calls: list[dict[str, Any]] = []
            repository_instances.append(self)

        async def get_latest_season_key(self, **kwargs: Any) -> str:
            assert kwargs == {"chat_id": chat_id, "clan_tags": ("#AAA111",)}
            return "2026-07-24T07:00:00+00:00"

        async def list_for_season(self, **kwargs: Any) -> tuple[str, ...]:
            self.list_calls.append(kwargs)
            return (kwargs["season_key"],)

    async def prepare_raid(**kwargs: Any) -> PreparedRaidSync:
        captured_saved_rows.extend(kwargs["saved_rows"])
        return _prepared_raid()

    monkeypatch.setattr(sync_service, "RaidPlayerStateRepository", RecordingRaidRepository)
    monkeypatch.setattr(sync_service, "prepare_public_raid_sync", prepare_raid)

    await SyncService(
        config=make_app_config(),
        telegram=FakeTelegram(),  # type: ignore[arg-type]
        connection=migrated_connection,
    )._run_sync(runtime_chat_id=chat_id, user_id=1001)

    assert captured_saved_rows == [
        "2026-07-17T07:00:00+00:00",
        "2026-07-24T07:00:00+00:00",
    ]
    assert [call["season_key"] for call in repository_instances[0].list_calls] == [
        "2026-07-17T07:00:00+00:00",
        "2026-07-24T07:00:00+00:00",
    ]
    assert all(
        call["chat_id"] == chat_id and call["clan_tags"] == ("#AAA111",)
        for call in repository_instances[0].list_calls
    )


@pytest.mark.asyncio
async def test_status_summary_includes_active_raid_season(
    migrated_connection: aiosqlite.Connection,
) -> None:
    """Проверяет raid binding в repository summary для `/status`."""

    chat_id = -1517
    await _insert_ready_chat(migrated_connection, chat_id=chat_id)
    await migrated_connection.execute(
        "UPDATE sheet_bindings SET active_raid_season = ? WHERE chat_id = ?",
        ("2026-07-24T07:00:00+00:00", chat_id),
    )
    await migrated_connection.commit()

    summary = await SyncService(
        config=make_app_config(),
        telegram=FakeTelegram(),  # type: ignore[arg-type]
        connection=migrated_connection,
    )._telegram_chats.get_sync_status_summary(chat_id)

    assert summary is not None
    assert summary.active_raid_season == "2026-07-24T07:00:00+00:00"
