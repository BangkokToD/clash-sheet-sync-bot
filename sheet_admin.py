"""Администрирование привязанной Google-таблицы."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

from sheets_client import (
    CellValue,
    GoogleSheetsError,
    SheetMetadata,
    SheetsClient,
)

SPREADSHEET_URL_RE: Final = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")
SPREADSHEET_ID_RE: Final = re.compile(r"^[a-zA-Z0-9-_]+$")

DEFAULT_COMPOSITION_SHEET_NAME: Final = "Состав"
DEFAULT_CWL_SHEET_NAME: Final = "CWL"
DEFAULT_BOT_STATE_SHEET_NAME: Final = "_bot_state"
MANAGED_BY_VALUE: Final = "clash-sheet-sync-bot"
BOT_STATE_SCHEMA_VERSION: Final = "1"


class SheetAdminError(RuntimeError):
    """Ошибка администрирования Google-таблицы."""


@dataclass(frozen=True, slots=True)
class SheetSetupResult:
    """Результат подготовки Google-таблицы к работе бота.

    Attributes:
        spreadsheet_id: ID Google Spreadsheet.
        spreadsheet_url: Нормализованная ссылка на Spreadsheet.
        composition_sheet_name: Название листа состава.
        composition_sheet_id: Числовой ID листа состава.
        active_cwl_sheet_name: Название активного CWL-листа.
        active_cwl_sheet_id: Числовой ID активного CWL-листа.
        active_cwl_season: Текущий CWL-сезон или `None`.
        bot_state_sheet_name: Название служебного листа.
        bot_state_sheet_id: Числовой ID служебного листа.
    """

    spreadsheet_id: str
    spreadsheet_url: str
    composition_sheet_name: str
    composition_sheet_id: int
    active_cwl_sheet_name: str
    active_cwl_sheet_id: int
    active_cwl_season: str | None
    bot_state_sheet_name: str
    bot_state_sheet_id: int


class SheetAdminService:
    """Сервис подготовки Google Sheets для новой привязки.

    Args:
        sheets_client: Низкоуровневый клиент Google Sheets API.
        spreadsheet_id: ID Google Spreadsheet.
        service_account_email: Email service account из credentials.json.
        expected_service_account_email: Email из `.env` или `None`.
    """

    def __init__(
        self,
        *,
        sheets_client: SheetsClient,
        spreadsheet_id: str,
        service_account_email: str,
        expected_service_account_email: str | None,
    ) -> None:
        self._sheets_client = sheets_client
        self._spreadsheet_id = spreadsheet_id
        self._service_account_email = service_account_email
        self._expected_service_account_email = expected_service_account_email

    async def initialize_new_binding(self, *, chat_id: int, timezone: str) -> SheetSetupResult:
        """Проверяет доступ и создаёт обязательные листы новой привязки.

        Метод намеренно выполняет реальные ensure-операции вместо создания
        временного тестового листа, потому что бот не должен удалять листы.

        Args:
            chat_id: ID Telegram-группы.
            timezone: IANA-таймзона новой привязки.

        Returns:
            Результат подготовки обязательных листов.

        Raises:
            SheetAdminError: Если service account email не совпадает с `.env`.
            GoogleSheetsError: Если Google Sheets API недоступен или прав недостаточно.
        """

        self._validate_service_account_email()
        metadata = await self._sheets_client.get_spreadsheet_metadata()
        composition_sheet = await self._ensure_sheet(
            known_sheets=metadata.sheets,
            title=DEFAULT_COMPOSITION_SHEET_NAME,
        )
        metadata = await self._sheets_client.get_spreadsheet_metadata()
        cwl_sheet = await self._ensure_sheet(
            known_sheets=metadata.sheets,
            title=DEFAULT_CWL_SHEET_NAME,
        )
        metadata = await self._sheets_client.get_spreadsheet_metadata()
        bot_state_sheet = await self._ensure_sheet(
            known_sheets=metadata.sheets,
            title=DEFAULT_BOT_STATE_SHEET_NAME,
        )

        await self._write_bot_state(
            chat_id=chat_id,
            composition_sheet=composition_sheet,
            cwl_sheet=cwl_sheet,
            bot_state_sheet=bot_state_sheet,
            timezone=timezone,
        )
        await self._sheets_client.hide_sheet(bot_state_sheet.sheet_id, hidden=True)

        return SheetSetupResult(
            spreadsheet_id=self._spreadsheet_id,
            spreadsheet_url=spreadsheet_url(self._spreadsheet_id),
            composition_sheet_name=composition_sheet.title,
            composition_sheet_id=composition_sheet.sheet_id,
            active_cwl_sheet_name=cwl_sheet.title,
            active_cwl_sheet_id=cwl_sheet.sheet_id,
            active_cwl_season=None,
            bot_state_sheet_name=bot_state_sheet.title,
            bot_state_sheet_id=bot_state_sheet.sheet_id,
        )

    async def hide_bot_key_column(self, *, sheet_id: int, column_index: int) -> None:
        """Скрывает физическую колонку служебного ключа строки.

        Args:
            sheet_id: Числовой ID листа.
            column_index: Zero-based индекс служебной колонки.
        """

        await self._sheets_client.hide_dimension(
            sheet_id=sheet_id,
            dimension="COLUMNS",
            start_index=column_index,
            end_index=column_index + 1,
            hidden=True,
        )

    async def _ensure_sheet(
        self,
        *,
        known_sheets: tuple[SheetMetadata, ...],
        title: str,
    ) -> SheetMetadata:
        """Находит лист по названию или создаёт его.

        Args:
            known_sheets: Уже прочитанные metadata листов.
            title: Требуемое название листа.

        Returns:
            Metadata найденного или созданного листа.
        """

        for sheet in known_sheets:
            if sheet.title == title:
                return sheet
        return await self._sheets_client.add_sheet(title)

    async def _write_bot_state(
        self,
        *,
        chat_id: int,
        composition_sheet: SheetMetadata,
        cwl_sheet: SheetMetadata,
        bot_state_sheet: SheetMetadata,
        timezone: str,
    ) -> None:
        """Записывает служебный лист `_bot_state`.

        Args:
            chat_id: ID Telegram-группы.
            composition_sheet: Metadata листа состава.
            cwl_sheet: Metadata активного CWL-листа.
            bot_state_sheet: Metadata листа `_bot_state`.
            timezone: IANA-таймзона привязки.
        """

        values: list[list[CellValue]] = [
            ["managed_by", MANAGED_BY_VALUE],
            ["schema_version", BOT_STATE_SCHEMA_VERSION],
            ["chat_id", chat_id],
            ["google_sheet_id", self._spreadsheet_id],
            ["composition_sheet_name", composition_sheet.title],
            ["composition_sheet_id", composition_sheet.sheet_id],
            ["active_cwl_sheet_name", cwl_sheet.title],
            ["active_cwl_sheet_id", cwl_sheet.sheet_id],
            ["active_cwl_season", ""],
            ["bot_state_sheet_name", bot_state_sheet.title],
            ["bot_state_sheet_id", bot_state_sheet.sheet_id],
            ["timezone", timezone],
            ["updated_at", _utc_now_iso()],
        ]
        await self._sheets_client.write_values(
            sheet_name=bot_state_sheet.title,
            range_a1=f"A1:B{len(values)}",
            values=values,
        )

    def _validate_service_account_email(self) -> None:
        """Проверяет совпадение client_email с `.env`, если оно задано.

        Raises:
            SheetAdminError: Если email не совпадает.
        """

        if self._expected_service_account_email is None:
            return
        if self._expected_service_account_email != self._service_account_email:
            raise SheetAdminError(
                "GOOGLE_SERVICE_ACCOUNT_EMAIL не совпадает с client_email credentials.json.",
            )


def extract_spreadsheet_id(value: str) -> str:
    """Извлекает Google spreadsheet ID из ссылки или чистого ID.

    Args:
        value: Полная ссылка Google Sheets или чистый spreadsheet ID.

    Returns:
        ID Google Spreadsheet.

    Raises:
        SheetAdminError: Если ID не найден.
    """

    stripped = value.strip()
    match = SPREADSHEET_URL_RE.search(stripped)
    if match is not None:
        return match.group(1)

    if SPREADSHEET_ID_RE.fullmatch(stripped) is not None:
        return stripped

    raise SheetAdminError(
        "Не удалось найти ID Google-таблицы. Отправьте обычную ссылку на Google Sheets.",
    )


def spreadsheet_url(spreadsheet_id: str) -> str:
    """Создаёт нормализованную ссылку на Google Spreadsheet.

    Args:
        spreadsheet_id: ID Google Spreadsheet.

    Returns:
        URL таблицы.
    """

    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"


def _utc_now_iso() -> str:
    """Возвращает текущую UTC-дату в ISO-формате.

    Returns:
        ISO-дата с timezone offset.
    """

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
