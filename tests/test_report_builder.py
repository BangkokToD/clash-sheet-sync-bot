"""Unit-тесты Telegram report builder."""

from __future__ import annotations

import pytest
from fakes import make_raid_sheet_sync_result

from clash_sheet_sync_bot.repositories import SyncStatusSummary
from clash_sheet_sync_bot.sync.composition import CompositionSyncResult
from clash_sheet_sync_bot.sync.cwl import CwlSheetSyncResult
from clash_sheet_sync_bot.sync.reports import (
    build_error_report,
    build_status_report,
    build_success_report,
)


def _composition_result(
    *,
    active_counts: tuple[tuple[str, int], ...] = (("Alpha", 2),),
    warnings: tuple[str, ...] = (),
) -> CompositionSyncResult:
    """Создаёт минимальный результат sync состава."""

    return CompositionSyncResult(
        active_counts=active_counts,
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


def test_build_error_report_escapes_reason_and_uses_table_button() -> None:
    """Проверяет HTML escaping и кнопку таблицы в error report."""

    payload = build_error_report(
        reason="<broken & unsafe>",
        spreadsheet_url="https://example.com/sheet?a=1&b=2",
    )

    assert payload.parse_mode == "HTML"
    assert payload.disable_web_page_preview is True
    assert "Причина: &lt;broken &amp; unsafe&gt;" in payload.text
    assert "Таблица" not in payload.text
    assert payload.reply_markup == {
        "inline_keyboard": [
            [{"text": "Таблица", "url": "https://example.com/sheet?a=1&b=2"}],
        ],
    }


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


def test_build_success_report_has_only_sections_clans_and_developer() -> None:
    """Проверяет точный компактный формат успешного отчёта."""

    payload = build_success_report(
        composition_result=_composition_result(
            active_counts=(("Alpha & Co", 2), ("Beta", 1)),
        ),
        cwl_result=CwlSheetSyncResult(
            season="2026-07",
            rows_count=15,
            blocks_count=2,
            all_not_in_progress=False,
        ),
        raid_result=make_raid_sheet_sync_result(),
        spreadsheet_url="https://example.com/sheet?a=1&b=2",
        support_url="https://t.me/+support?a=1&b=2",
    )

    assert payload.text == (
        "Обновлены Состав, CWL и Рейды для кланов:\n"
        "• Alpha &amp; Co\n"
        "• Beta\n\n"
        "Разработчик: BangkokToD\n"
        '<a href="https://t.me/+support?a=1&amp;b=2">Чат Леши</a>'
    )
    assert payload.reply_markup == {
        "inline_keyboard": [
            [{"text": "Таблица", "url": "https://example.com/sheet?a=1&b=2"}],
        ],
    }


def test_build_success_report_lists_only_updated_sections() -> None:
    """Проверяет отчёт, когда optional-разделы не запускались."""

    payload = build_success_report(
        composition_result=_composition_result(),
        cwl_result=None,
        raid_result=None,
        spreadsheet_url="https://example.com/sheet",
    )

    assert payload.text == ("Обновлены Состав для кланов:\n• Alpha\n\nРазработчик: BangkokToD")
    assert payload.reply_markup == {
        "inline_keyboard": [
            [{"text": "Таблица", "url": "https://example.com/sheet"}],
        ],
    }
    assert "Чат Леши" not in payload.text


def test_build_success_report_omits_counts_seasons_and_warnings() -> None:
    """Проверяет отсутствие прежней подробной информации."""

    payload = build_success_report(
        composition_result=_composition_result(warnings=("composition warning",)),
        cwl_result=CwlSheetSyncResult(
            season="2026-07",
            rows_count=15,
            blocks_count=1,
            all_not_in_progress=True,
            showing_previous_season=True,
            warnings=("cwl warning",),
        ),
        raid_result=make_raid_sheet_sync_result(warnings=("raid warning",)),
        spreadsheet_url="https://example.com/sheet",
    )

    assert "Всего игроков" not in payload.text
    assert "Сезон" not in payload.text
    assert "warning" not in payload.text
    assert "Таблица" not in payload.text
    assert payload.text.endswith("Разработчик: BangkokToD")
