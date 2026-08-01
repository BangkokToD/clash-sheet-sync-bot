"""Unit-тесты Telegram report builder."""

from __future__ import annotations

import pytest
from fakes import make_raid_sheet_sync_result

from clash_sheet_sync_bot.repositories import SyncStatusSummary
from clash_sheet_sync_bot.sync.composition import CompositionSyncResult
from clash_sheet_sync_bot.sync.cwl import CwlSheetSyncResult
from clash_sheet_sync_bot.sync.reports import (
    MAX_TELEGRAM_MESSAGE_LENGTH,
    build_error_report,
    build_status_report,
    build_success_report,
)


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
        active_raid_season="<2026-07-24T07:00:00+00:00>",
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
    assert "Рейдовый сезон: &lt;2026-07-24T07:00:00+00:00&gt;" in payload.text
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
        raid_result=make_raid_sheet_sync_result(),
        spreadsheet_url="https://example.com/sheet",
        report_max_items=50,
        is_baseline=True,
    )

    assert "Первичная синхронизация завершена." in payload.text
    assert '<a href="https://example.com/sheet">Таблица</a>' in payload.text
    assert "Добавлено строк рейдов" not in payload.text
    assert "Рейды:" in payload.text
    assert "Изменения:" not in payload.text


def test_build_success_report_without_changes() -> None:
    """Проверяет краткий success report без изменений."""

    payload = build_success_report(
        composition_result=_composition_result(),
        cwl_result=None,
        raid_result=make_raid_sheet_sync_result(),
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
    assert "Рейды:" in payload.text
    assert "Сезон: 2026-07-24T07:00:00+00:00. Состояние: завершён." in payload.text
    assert "Период: 2026-07-24T07:00:00+00:00 — 2026-07-27T07:00:00+00:00." in payload.text
    assert "Клановых блоков: 2. Участников: 8." in payload.text
    assert "Выполнили 6/6: 5." in payload.text
    assert "Не выполнили 6/6: 3." in payload.text


def test_build_success_report_includes_import_warning_summary() -> None:
    """Проверяет предупреждения импорта в success report."""

    payload = build_success_report(
        composition_result=_composition_result(warnings=("warning 1", "warning 2")),
        cwl_result=None,
        raid_result=make_raid_sheet_sync_result(warnings=("warning 3",)),
        spreadsheet_url="https://example.com/sheet",
        report_max_items=50,
        is_baseline=False,
    )

    assert "Предупреждения импорта: 2." in payload.text
    assert "Предупреждения рейдов:" in payload.text
    assert "warning 3" in payload.text
    assert "Если число повторяется после следующего /sync" in payload.text


def test_build_success_report_identifies_saved_cwl_season() -> None:
    """Проверяет, что межсезонный CWL не называется текущим."""

    payload = build_success_report(
        composition_result=_composition_result(),
        cwl_result=CwlSheetSyncResult(
            season="2026-07",
            rows_count=15,
            blocks_count=1,
            all_not_in_progress=True,
            showing_previous_season=True,
        ),
        raid_result=make_raid_sheet_sync_result(),
        spreadsheet_url="https://example.com/sheet",
        report_max_items=50,
        is_baseline=False,
    )

    assert "CWL сейчас не проводится. Показан сохранённый сезон: 2026-07." in payload.text
    assert "Всего строк: 15." in payload.text


def test_build_success_report_explains_missing_cwl_history() -> None:
    """Проверяет сообщение для новой группы без сохранённого CWL."""

    payload = build_success_report(
        composition_result=_composition_result(),
        cwl_result=CwlSheetSyncResult(
            season=None,
            rows_count=0,
            blocks_count=1,
            all_not_in_progress=True,
        ),
        raid_result=make_raid_sheet_sync_result(),
        spreadsheet_url="https://example.com/sheet",
        report_max_items=50,
        is_baseline=False,
    )

    assert "CWL сейчас не проводится. Сохранённых данных за прошлый сезон нет." in payload.text


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
        raid_result=make_raid_sheet_sync_result(),
        spreadsheet_url="https://example.com/sheet",
        report_max_items=50,
        is_baseline=False,
    )

    assert len(payload.text) <= MAX_TELEGRAM_MESSAGE_LENGTH
    assert "Отчёт сокращён. Полный результат смотри в таблице." in payload.text
    assert payload.text.endswith('<a href="https://example.com/sheet">Таблица</a>')


def test_build_success_report_describes_ongoing_raids_without_below_target_count() -> None:
    """Проверяет raid-сводку ongoing без преждевременного `<6/6`."""

    payload = build_success_report(
        composition_result=_composition_result(),
        cwl_result=None,
        raid_result=make_raid_sheet_sync_result(
            season_state="ongoing",
            attacks_complete_count=2,
            attacks_below_target_count=6,
        ),
        spreadsheet_url="https://example.com/sheet",
        report_max_items=50,
        is_baseline=False,
    )

    assert "Состояние: проводится." in payload.text
    assert "Выполнили 6/6: 2." in payload.text
    assert "Не выполнили 6/6" not in payload.text


def test_build_success_report_uses_configured_raid_attacks_target() -> None:
    """Проверяет единый configured target в raid-отчёте."""

    payload = build_success_report(
        composition_result=_composition_result(),
        cwl_result=None,
        raid_result=make_raid_sheet_sync_result(attacks_target=7),
        spreadsheet_url="https://example.com/sheet",
        report_max_items=50,
        is_baseline=False,
    )

    assert "Выполнили 7/7: 5." in payload.text
    assert "Не выполнили 7/7: 3." in payload.text


def test_build_success_report_describes_saved_and_missing_raid_seasons() -> None:
    """Проверяет interseason и отсутствие raid history."""

    saved = build_success_report(
        composition_result=_composition_result(),
        cwl_result=None,
        raid_result=make_raid_sheet_sync_result(showing_saved_season=True),
        spreadsheet_url="https://example.com/sheet",
        report_max_items=50,
        is_baseline=False,
    )
    missing = build_success_report(
        composition_result=_composition_result(),
        cwl_result=None,
        raid_result=make_raid_sheet_sync_result(
            season_key=None,
            season_state=None,
            rows_count=0,
            attacks_complete_count=0,
            attacks_below_target_count=0,
        ),
        spreadsheet_url="https://example.com/sheet",
        report_max_items=50,
        is_baseline=False,
    )

    assert "Состояние: показан сохранённый." in saved.text
    assert ("Рейды сейчас не проводятся. Сохранённых данных за прошлый уикенд нет.") in missing.text


def test_build_success_report_describes_rotation_pruning_and_cleanup_warning() -> None:
    """Проверяет archive/pruning и special cleanup warning."""

    cleanup_warning = "Raid cleanup не завершён: deleteSheet failed."
    payload = build_success_report(
        composition_result=_composition_result(),
        cwl_result=None,
        raid_result=make_raid_sheet_sync_result(
            warnings=(cleanup_warning,),
            archived_previous_season=True,
            archive_sheet_name="Рейды 2026-07-17",
            pruned_archive_sheet_names=("Рейды 2026-06-19",),
        ),
        spreadsheet_url="https://example.com/sheet",
        report_max_items=50,
        is_baseline=False,
    )

    assert "Архивирован предыдущий сезон: Рейды 2026-07-17." in payload.text
    assert "Удалён старый архив: Рейды 2026-06-19." in payload.text
    assert cleanup_warning in payload.text
    assert "Таблица могла быть частично обновлена" not in payload.text
