"""Формирование HTML-отчётов Telegram для `/sync` и `/status`."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Final

from clash_sheet_sync_bot.repositories import SyncStatusSummary
from clash_sheet_sync_bot.sync.composition import CompositionSyncResult
from clash_sheet_sync_bot.sync.cwl import CwlSheetSyncResult
from clash_sheet_sync_bot.sync.raids import RaidSheetSyncResult

TABLE_LINK_TEXT: Final = "Таблица"
DEVELOPER_NAME: Final = "BangkokToD"
DEVELOPER_URL: Final = "https://t.me/BangkokToD"
SUPPORT_LINK_TEXT: Final = "Чат Леши"


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
    support_url: str | None = None,
) -> SyncReportPayload:
    """Строит отчёт успешного `/sync`.

    Args:
        composition_result: Результат синхронизации состава.
        cwl_result: Результат CWL или `None`, если CWL не запускалась.
        raid_result: Результат raid sync или `None`, если raids не запускались.
        spreadsheet_url: Ссылка на привязанную Google Spreadsheet.
        support_url: Ссылка на настроенный чат техподдержки или `None`.

    Returns:
        HTML-отчёт для Telegram.
    """

    sections = ["Состав"]
    if cwl_result is not None:
        sections.append("CWL")
    if raid_result is not None:
        sections.append("Рейды")

    lines = [f"Обновлены {_human_list(sections)} для кланов:"]
    lines.extend(f"• {escape(clan_name)}" for clan_name, _ in composition_result.active_counts)
    lines.extend(
        [
            "",
            f'Разработчик: <a href="{DEVELOPER_URL}">{DEVELOPER_NAME}</a>',
            _table_link(spreadsheet_url),
        ]
    )
    if support_url is not None:
        lines.append(f'<a href="{escape(support_url, quote=True)}">{SUPPORT_LINK_TEXT}</a>')
    return SyncReportPayload(text="\n".join(lines))


def _human_list(items: list[str]) -> str:
    """Соединяет названия разделов через запятые и союз `и`."""

    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} и {items[-1]}"


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
