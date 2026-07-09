"""Unit-тесты Telegram report builder."""

from __future__ import annotations

import pytest

from composition_sync import CompositionSyncResult
from cwl_sync import CwlSheetSyncResult
from report_builder import (
    MAX_TELEGRAM_MESSAGE_LENGTH,
    build_error_report,
    build_status_report,
    build_success_report,
)
from repositories import SyncStatusSummary


def _composition_result(*, warnings: tuple[str, ...] = ()) -> CompositionSyncResult:
    """Создаёт минимальный результат sync состава."""

    return CompositionSyncResult(
        active_counts=(("Alpha", 2),),
        exited_count=1,
        diff_items=(),
        warnings=warnings,
    )


def _status_summary(last_sync_status: str | None) -> SyncStatusSummary:
    """Создаёт минимальную status summary."""

    return SyncStatusSummary(
        chat_id=-1001,
        status="ready",
        last_sync_started_at="2026-07-09T11:59:00+00:00",
        last_sync_finished_at="2026-07-09T12:00:00+00:00",
        last_sync_status=last_sync_status,
        last_sync_error="<ошибка & причина>",
        active_clans_count=2,
        active_cwl_season="<2026-07>",
        spreadsheet_url="https://example.com/sheet?a=1&b=2",
    )


def test_build_error_report_escapes_reason_and_table_url() -> None:
    """Проверяет HTML escaping в error report."""

    payload = build_error_report(
        reason="<broken & unsafe>",
        spreadsheet_url="https://example.com/sheet?a=1&b=2",
    )

    assert payload.parse_mode == "HTML"
    assert payload.disable_web_page_preview is True
    assert "Причина: &lt;broken &amp; unsafe&gt;" in payload.text
    assert '<a href="https://example.com/sheet?a=1&amp;b=2">Таблица</a>' in payload.text


@pytest.mark.parametrize(
    ("raw_status", "display_status"),
    (
        ("success", "успешно"),
        ("error", "ошибка"),
        ("rate_limited", "rate limit"),
        ("skipped", "пропущено"),
        (None, "-"),
    ),
)
def test_build_status_report_displays_sync_status(
    raw_status: str | None,
    display_status: str,
) -> None:
    """Проверяет человекочитаемые статусы /status."""

    payload = build_status_report(_status_summary(raw_status))

    assert f"Статус: {display_status}" in payload.text


def test_build_status_report_escapes_fields() -> None:
    """Проверяет HTML escaping в /status."""

    payload = build_status_report(_status_summary("error"))

    assert "Ошибка: &lt;ошибка &amp; причина&gt;" in payload.text
    assert "CWL-сезон: &lt;2026-07&gt;" in payload.text
    assert '<a href="https://example.com/sheet?a=1&amp;b=2">Таблица</a>' in payload.text


def test_build_status_report_for_missing_summary() -> None:
    """Проверяет /status для неизвестной группы."""

    payload = build_status_report(None)

    assert payload.text == "Группа не настроена."


def test_build_success_report_baseline() -> None:
    """Проверяет отчёт первичной синхронизации."""

    payload = build_success_report(
        composition_result=_composition_result(),
        cwl_result=None,
        spreadsheet_url="https://example.com/sheet",
        report_max_items=50,
        is_baseline=True,
    )

    assert "Первичная синхронизация завершена." in payload.text
    assert '<a href="https://example.com/sheet">Таблица</a>' in payload.text


def test_build_success_report_without_changes() -> None:
    """Проверяет краткий success report без изменений."""

    payload = build_success_report(
        composition_result=_composition_result(),
        cwl_result=None,
        spreadsheet_url="https://example.com/sheet",
        report_max_items=50,
        is_baseline=False,
    )

    assert "Обновление завершено." in payload.text
    assert "Состав:" in payload.text
    assert "Всего игроков: 3." in payload.text
    assert "Активных: 2. Вышедших: 1." in payload.text
    assert "Изменений нет." in payload.text
    assert '<a href="https://example.com/sheet">Таблица</a>' in payload.text


def test_build_success_report_includes_import_warning_summary() -> None:
    """Проверяет предупреждения импорта в success report."""

    payload = build_success_report(
        composition_result=_composition_result(warnings=("warning 1", "warning 2")),
        cwl_result=None,
        spreadsheet_url="https://example.com/sheet",
        report_max_items=50,
        is_baseline=False,
    )

    assert "Предупреждения импорта: 2." in payload.text
    assert "Если число повторяется после следующего /sync" in payload.text


def test_build_success_report_truncates_long_report() -> None:
    """Проверяет ограничение длины Telegram-отчёта."""

    cwl_result = CwlSheetSyncResult(
        season="2026-07-" + ("x" * 5000),
        rows_count=1,
        blocks_count=1,
        all_not_in_progress=False,
    )

    payload = build_success_report(
        composition_result=_composition_result(),
        cwl_result=cwl_result,
        spreadsheet_url="https://example.com/sheet",
        report_max_items=50,
        is_baseline=False,
    )

    assert len(payload.text) <= MAX_TELEGRAM_MESSAGE_LENGTH
    assert "Отчёт сокращён. Полный результат смотри в таблице." in payload.text
    assert payload.text.endswith('<a href="https://example.com/sheet">Таблица</a>')
    