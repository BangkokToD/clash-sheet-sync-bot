"""SQLite-backed тесты sync-service error/write contracts."""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite
import pytest

import sync_service
from composition_sync import PreparedCompositionSync
from cwl_sync import CwlPreparedData
from fakes import FakeTelegram, make_app_config
from sheets_client import GoogleSheetsWriteError
from sync_service import (
    PARTIAL_SHEET_WRITE_WARNING,
    UNEXPECTED_SYNC_ERROR_REASON,
    WRITE_PHASE_COMPOSITION_WRITTEN,
    WRITE_PHASE_PREPARED,
    SyncService,
    _sync_error_reason,
)
from telegram_client import TelegramApiError

NOW = "2026-07-09T12:00:00+00:00"


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
    monkeypatch.setattr(sync_service, "apply_prepared_composition_sync", _successful_apply_composition)
    monkeypatch.setattr(sync_service, "apply_public_cwl_sync", _successful_apply_cwl)


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