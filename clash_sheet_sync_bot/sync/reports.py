"""Формирование HTML-отчётов Telegram для `/sync` и `/status`."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from html import escape
from typing import Final

from clash_sheet_sync_bot.repositories import SyncStatusSummary
from clash_sheet_sync_bot.sync.composition import CompositionDiffItem, CompositionSyncResult
from clash_sheet_sync_bot.sync.cwl import CwlDiffItem, CwlSheetSyncResult
from clash_sheet_sync_bot.sync.raids import RaidSheetSyncResult

TABLE_LINK_TEXT: Final = "Таблица"
MAX_TELEGRAM_MESSAGE_LENGTH: Final = 3500


@dataclass(frozen=True, slots=True)
class SyncReportPayload:
    """Готовый Telegram-отчёт.

    Attributes:
        text: HTML-текст сообщения.
        parse_mode: Режим разметки Telegram.
        disable_web_page_preview: Нужно ли отключить preview ссылки.
    """

    text: str
    parse_mode: str = "HTML"
    disable_web_page_preview: bool = True


def build_success_report(
    *,
    composition_result: CompositionSyncResult,
    cwl_result: CwlSheetSyncResult | None,
    raid_result: RaidSheetSyncResult | None,
    spreadsheet_url: str,
    report_max_items: int,
    is_baseline: bool,
) -> SyncReportPayload:
    """Строит отчёт успешного `/sync`.

    Args:
        composition_result: Результат синхронизации состава.
        cwl_result: Результат CWL или `None`, если CWL не запускалась.
        raid_result: Результат raid sync или `None`, если raids не запускались.
        spreadsheet_url: URL Google Spreadsheet.
        report_max_items: Лимит количества diff-строк.
        is_baseline: Является ли запуск первичной синхронизацией.

    Returns:
        HTML-отчёт для Telegram.
    """

    if is_baseline:
        lines = ["Первичная синхронизация завершена."]
        if raid_result is not None:
            lines.extend(["", *_raid_summary_lines(raid_result)])
            lines.extend(_raid_warning_lines(raid_result, report_max_items=report_max_items))
        lines.extend(["", _table_link(spreadsheet_url)])
        return SyncReportPayload(text=_fit_telegram_length("\n".join(lines), spreadsheet_url))

    lines = ["Обновление завершено.", ""]
    lines.extend(_composition_summary_lines(composition_result))
    if cwl_result is not None:
        lines.extend(["", *_cwl_summary_lines(cwl_result)])
    if raid_result is not None:
        lines.extend(["", *_raid_summary_lines(raid_result)])

    warnings = [*composition_result.warnings]
    if cwl_result is not None:
        warnings.extend(cwl_result.warnings)
    if warnings:
        lines.extend(
            [
                "",
                f"Предупреждения импорта: {len(warnings)}.",
                "Если число повторяется после следующего /sync, проверьте таблицу через диагностику.",
            ],
        )

    if raid_result is not None:
        lines.extend(_raid_warning_lines(raid_result, report_max_items=report_max_items))

    lines.extend(["", _table_link(spreadsheet_url)])
    return SyncReportPayload(text=_fit_telegram_length("\n".join(lines), spreadsheet_url))


def _composition_summary_lines(composition_result: CompositionSyncResult) -> list[str]:
    """Строит краткую сводку состава без перечисления игроков."""

    counts = _count_items_by_kind(composition_result.diff_items)
    active_total = sum(count for _, count in composition_result.active_counts)
    total_players = active_total + composition_result.exited_count
    lines = [
        "Состав:",
        f"Всего игроков: {total_players}.",
        f"Активных: {active_total}. Вышедших: {composition_result.exited_count}.",
    ]
    if not composition_result.diff_items:
        lines.append("Изменений нет.")
        return lines
    lines.append(
        "Изменения: "
        f"новых {counts.get('added', 0)}, "
        f"вышло {counts.get('exited', 0)}, "
        f"вернулось {counts.get('returned', 0)}, "
        f"переходов {counts.get('moved', 0)}, "
        f"обновлено {counts.get('updated', 0)}."
    )
    return lines


def _cwl_summary_lines(cwl_result: CwlSheetSyncResult) -> list[str]:
    """Строит краткую сводку CWL без перечисления атак."""

    lines = ["CWL:"]
    if cwl_result.all_not_in_progress:
        if cwl_result.showing_previous_season:
            season = escape(cwl_result.season or "-")
            lines.append(f"CWL сейчас не проводится. Показан сохранённый сезон: {season}.")
            lines.append(f"Всего строк: {cwl_result.rows_count}.")
        else:
            lines.append("CWL сейчас не проводится. Сохранённых данных за прошлый сезон нет.")
        return lines

    counts = _count_items_by_kind(cwl_result.diff_items)
    season = escape(cwl_result.season or "-")
    lines.append(f"Сезон: {season}.")
    lines.append(f"Всего строк: {cwl_result.rows_count}.")
    lines.append(
        f"Изменения: новых {counts.get('added', 0)}, обновлено {counts.get('updated', 0)}."
    )
    if cwl_result.archived_previous_season:
        lines.append("Сезон сменился, старый CWL архивирован.")
    if cwl_result.not_in_progress_clans:
        lines.append(f"Кланов без CWL: {len(cwl_result.not_in_progress_clans)}.")
    return lines


def _raid_summary_lines(raid_result: RaidSheetSyncResult) -> list[str]:
    """Строит краткую сводку raid weekend."""

    lines = ["Рейды:"]
    if raid_result.season_key is None:
        lines.append("Рейды сейчас не проводятся. Сохранённых данных за прошлый уикенд нет.")
        return lines

    state = (
        "показан сохранённый"
        if raid_result.showing_saved_season
        else {
            "ongoing": "проводится",
            "ended": "завершён",
        }.get(raid_result.season_state, "-")
    )
    lines.append(f"Сезон: {escape(raid_result.season_key)}. Состояние: {state}.")
    if raid_result.season_start_at is not None and raid_result.season_end_at is not None:
        lines.append(
            f"Период: {escape(raid_result.season_start_at)} — {escape(raid_result.season_end_at)}."
        )
    lines.append(
        f"Клановых блоков: {raid_result.blocks_count}. Участников: {raid_result.rows_count}."
    )
    target = raid_result.attacks_target
    lines.append(f"Выполнили {target}/{target}: {raid_result.attacks_complete_count}.")
    if raid_result.season_state == "ended":
        lines.append(f"Не выполнили {target}/{target}: {raid_result.attacks_below_target_count}.")
    if raid_result.archived_previous_season and raid_result.archive_sheet_name is not None:
        lines.append(f"Архивирован предыдущий сезон: {escape(raid_result.archive_sheet_name)}.")
    lines.extend(
        f"Удалён старый архив: {escape(sheet_name)}."
        for sheet_name in raid_result.pruned_archive_sheet_names
    )
    return lines


def _raid_warning_lines(
    raid_result: RaidSheetSyncResult,
    *,
    report_max_items: int,
) -> list[str]:
    """Строит ограниченный список raid warnings."""

    if not raid_result.warnings:
        return []
    visible_warnings = raid_result.warnings[:report_max_items]
    lines = ["", "Предупреждения рейдов:"]
    lines.extend(f"• {escape(warning)}" for warning in visible_warnings)
    omitted = len(raid_result.warnings) - len(visible_warnings)
    if omitted > 0:
        lines.append(f"…ещё {omitted}.")
    return lines


def _count_items_by_kind(
    items: tuple[CompositionDiffItem, ...] | tuple[CwlDiffItem, ...],
) -> Counter[str]:
    """Считает diff items по kind."""

    return Counter(item.kind for item in items)


def _fit_telegram_length(text: str, spreadsheet_url: str) -> str:
    """Жёстко ограничивает длину Telegram-отчёта."""

    if len(text) <= MAX_TELEGRAM_MESSAGE_LENGTH:
        return text
    suffix = "\n\nОтчёт сокращён. Полный результат смотри в таблице.\n" + _table_link(
        spreadsheet_url
    )
    ellipsis = "..."
    limit = MAX_TELEGRAM_MESSAGE_LENGTH - len(ellipsis) - len(suffix)
    return f"{text[: max(limit, 0)].rstrip()}{ellipsis}{suffix}"


def build_error_report(*, reason: str, spreadsheet_url: str | None = None) -> SyncReportPayload:
    """Строит отчёт ошибки `/sync`."""

    lines = ["Обновление отменено.", "", f"Причина: {escape(reason)}"]
    if spreadsheet_url is not None:
        lines.extend(["", _table_link(spreadsheet_url)])
    return SyncReportPayload(text="\n".join(lines))


def build_status_report(summary: SyncStatusSummary | None) -> SyncReportPayload:
    """Строит ответ `/status`."""

    if summary is None:
        return SyncReportPayload(text="Группа не настроена.")

    last_update = summary.last_sync_finished_at or "ещё не запускалось"
    status = _display_sync_status(summary.last_sync_status)
    error = summary.last_sync_error or "-"
    cwl_season = summary.active_cwl_season or "-"
    raid_season = summary.active_raid_season or "-"
    table = _table_link(summary.spreadsheet_url) if summary.spreadsheet_url else "-"
    lines = [
        f"Последнее обновление: {escape(last_update)}",
        f"Статус: {escape(status)}",
        f"Ошибка: {escape(error)}",
        f"Активных кланов: {summary.active_clans_count}",
        f"CWL-сезон: {escape(cwl_season)}",
        f"Рейдовый сезон: {escape(raid_season)}",
        f"Таблица: {table}",
    ]
    return SyncReportPayload(text="\n".join(lines))


def _display_sync_status(status: str | None) -> str:
    """Преобразует технический статус в короткий текст."""

    if status == "success":
        return "успешно"
    if status == "error":
        return "ошибка"
    if status == "rate_limited":
        return "rate limit"
    if status == "skipped":
        return "пропущено"
    return "-"


def _table_link(spreadsheet_url: str) -> str:
    """Строит HTML-ссылку на таблицу."""

    return f'<a href="{escape(spreadsheet_url, quote=True)}">{TABLE_LINK_TEXT}</a>'
