"""Администрирование привязанной Google-таблицы."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from clash_sheet_sync_bot.common.time import utc_now_iso as _utc_now_iso
from clash_sheet_sync_bot.models import SheetBinding, SheetBlock
from clash_sheet_sync_bot.sheets.client import (
    CellValue,
    GoogleSheetsError,
    SheetMetadata,
    SheetsClient,
    range_from_start_cell,
)
from clash_sheet_sync_bot.sheets.ranges import parse_a1_cell as _parse_a1_cell

SPREADSHEET_URL_RE: Final = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")
SPREADSHEET_ID_RE: Final = re.compile(r"^[a-zA-Z0-9-_]+$")

DEFAULT_COMPOSITION_SHEET_NAME: Final = "Состав"
DEFAULT_CWL_SHEET_NAME: Final = "CWL"
DEFAULT_BOT_STATE_SHEET_NAME: Final = "_bot_state"
MANAGED_BY_VALUE: Final = "clash-sheet-sync-bot"
BOT_STATE_SCHEMA_VERSION: Final = "1"

DIAGNOSTIC_WRITE_RANGE: Final = "A20:B20"


@dataclass(frozen=True, slots=True)
class TableDiagnosticIssue:
    """Одна проблема диагностики таблицы.

    Attributes:
        level: Уровень: ok, warning или error.
        message: Человекочитаемый текст.
        fixable: Может ли auto-fix исправить проблему.
    """

    level: str
    message: str
    fixable: bool = False


@dataclass(frozen=True, slots=True)
class TableDiagnosticResult:
    """Результат диагностики привязанной таблицы."""

    issues: tuple[TableDiagnosticIssue, ...]
    staging_sheets: tuple[str, ...]

    @property
    def has_errors(self) -> bool:
        """Проверяет наличие ошибок."""

        return any(issue.level == "error" for issue in self.issues)

    @property
    def has_fixable_issues(self) -> bool:
        """Проверяет наличие исправимых проблем."""

        return any(issue.fixable for issue in self.issues)


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
            active_cwl_season=None,
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

    async def diagnose_binding(
        self,
        *,
        binding: SheetBinding,
        blocks: Sequence[SheetBlock],
    ) -> TableDiagnosticResult:
        """Проверяет привязанную таблицу без изменения пользовательских листов.

        Диагностика может выполнять безопасные write/batchUpdate операции только
        на `_bot_state`, потому что это служебный лист бота.
        """

        issues: list[TableDiagnosticIssue] = []
        metadata = await self._sheets_client.get_spreadsheet_metadata()
        sheets_by_title = {sheet.title: sheet for sheet in metadata.sheets}
        composition_sheet = _sheet_by_id(metadata.sheets, binding.composition_sheet_id)
        if composition_sheet is None:
            composition_sheet = sheets_by_title.get(binding.composition_sheet_name)
        if composition_sheet is None:
            issues.append(TableDiagnosticIssue("error", "Лист Состав отсутствует.", True))
        else:
            issues.append(
                TableDiagnosticIssue("ok", f"Лист Состав найден: {composition_sheet.title}.")
            )

        if binding.active_cwl_sheet_id is None:
            issues.append(
                TableDiagnosticIssue("error", "active_cwl_sheet_id отсутствует в SQLite.", True)
            )
        cwl_sheet = _sheet_by_id(metadata.sheets, binding.active_cwl_sheet_id)
        if cwl_sheet is None:
            cwl_sheet = sheets_by_title.get(binding.active_cwl_sheet_name)
        if cwl_sheet is None:
            issues.append(TableDiagnosticIssue("error", "Активный лист CWL отсутствует.", True))
        else:
            issues.append(
                TableDiagnosticIssue("ok", f"Активный лист CWL найден: {cwl_sheet.title}.")
            )

        bot_state_sheet = _sheet_by_id(metadata.sheets, binding.bot_state_sheet_id)
        if bot_state_sheet is None:
            bot_state_sheet = sheets_by_title.get(binding.bot_state_sheet_name)
        if bot_state_sheet is None:
            issues.append(TableDiagnosticIssue("error", "Лист _bot_state отсутствует.", True))
        else:
            issues.append(TableDiagnosticIssue("ok", "Лист _bot_state найден."))
            bot_state = await self._read_bot_state(bot_state_sheet.title)
            state_sheet_id = bot_state.get("google_sheet_id")
            if state_sheet_id != binding.google_sheet_id:
                issues.append(
                    TableDiagnosticIssue(
                        "error",
                        "google_sheet_id в SQLite и _bot_state не совпадает.",
                        False,
                    ),
                )
            else:
                issues.append(TableDiagnosticIssue("ok", "google_sheet_id в _bot_state совпадает."))
            await self._sheets_client.write_values(
                sheet_name=bot_state_sheet.title,
                range_a1=DIAGNOSTIC_WRITE_RANGE,
                values=[
                    ["diagnostic_checked_at", _utc_now_iso()],
                ],
            )
            issues.append(TableDiagnosticIssue("ok", "values write доступен."))
            await self._sheets_client.hide_sheet(bot_state_sheet.sheet_id, hidden=True)
            issues.append(TableDiagnosticIssue("ok", "spreadsheets.batchUpdate доступен."))

        issues.extend(await self._diagnose_bot_key_blocks(blocks))
        staging_sheets = tuple(
            sheet.title for sheet in metadata.sheets if sheet.title.startswith("CWL - staging - ")
        )
        if staging_sheets:
            issues.append(
                TableDiagnosticIssue(
                    "warning",
                    "Найдены staging-листы после прошлых ошибок: "
                    + ", ".join(staging_sheets)
                    + ".",
                    False,
                ),
            )
        return TableDiagnosticResult(issues=tuple(issues), staging_sheets=staging_sheets)

    async def auto_fix_binding(
        self,
        *,
        binding: SheetBinding,
        blocks: Sequence[SheetBlock],
    ) -> SheetSetupResult:
        """Восстанавливает обязательные листы, `_bot_state` и скрытые ключи."""

        self._validate_service_account_email()
        metadata = await self._sheets_client.get_spreadsheet_metadata()
        composition_sheet = _sheet_by_id(metadata.sheets, binding.composition_sheet_id)
        if composition_sheet is None:
            composition_sheet = _sheet_by_title(metadata.sheets, binding.composition_sheet_name)
        if composition_sheet is None:
            composition_sheet = await self._sheets_client.add_sheet(binding.composition_sheet_name)

        metadata = await self._sheets_client.get_spreadsheet_metadata()
        cwl_sheet = _sheet_by_id(metadata.sheets, binding.active_cwl_sheet_id)
        if cwl_sheet is None:
            cwl_sheet = _sheet_by_title(metadata.sheets, binding.active_cwl_sheet_name)
        if cwl_sheet is None:
            cwl_sheet = await self._sheets_client.add_sheet(binding.active_cwl_sheet_name)

        metadata = await self._sheets_client.get_spreadsheet_metadata()
        bot_state_sheet = _sheet_by_id(metadata.sheets, binding.bot_state_sheet_id)
        if bot_state_sheet is None:
            bot_state_sheet = _sheet_by_title(metadata.sheets, binding.bot_state_sheet_name)
        if bot_state_sheet is None:
            bot_state_sheet = await self._sheets_client.add_sheet(binding.bot_state_sheet_name)

        await self._write_bot_state(
            chat_id=binding.chat_id,
            composition_sheet=composition_sheet,
            cwl_sheet=cwl_sheet,
            active_cwl_season=binding.active_cwl_season,
            bot_state_sheet=bot_state_sheet,
            timezone=binding.timezone,
        )
        await self._sheets_client.hide_sheet(bot_state_sheet.sheet_id, hidden=True)
        await self._hide_bot_key_columns_for_blocks(blocks)
        return SheetSetupResult(
            spreadsheet_id=self._spreadsheet_id,
            spreadsheet_url=spreadsheet_url(self._spreadsheet_id),
            composition_sheet_name=composition_sheet.title,
            composition_sheet_id=composition_sheet.sheet_id,
            active_cwl_sheet_name=cwl_sheet.title,
            active_cwl_sheet_id=cwl_sheet.sheet_id,
            active_cwl_season=binding.active_cwl_season,
            bot_state_sheet_name=bot_state_sheet.title,
            bot_state_sheet_id=bot_state_sheet.sheet_id,
        )

    async def _read_bot_state(self, sheet_name: str) -> dict[str, str]:
        """Читает `_bot_state` в словарь key -> value."""

        values = await self._sheets_client.read_values(sheet_name, "A1:B30")
        result: dict[str, str] = {}
        for row in values:
            if len(row) < 2:
                continue
            key = str(row[0]).strip()
            if key:
                result[key] = str(row[1]).strip()
        return result

    async def _diagnose_bot_key_blocks(
        self,
        blocks: Sequence[SheetBlock],
    ) -> tuple[TableDiagnosticIssue, ...]:
        """Проверяет наличие `__bot_key` в управляемых табличных блоках."""

        table_blocks = [block for block in blocks if not block.block_key.startswith("cwl_message:")]
        if not table_blocks:
            return (
                TableDiagnosticIssue(
                    "warning",
                    "Управляемые блоки ещё не создавались. Запустите /sync.",
                    False,
                ),
            )

        issues: list[TableDiagnosticIssue] = []
        for block in table_blocks:
            if block.rows_count < 2 or block.columns_count < 1:
                issues.append(
                    TableDiagnosticIssue(
                        "error",
                        f"Блок {block.block_key} слишком мал для заголовка __bot_key.",
                        False,
                    ),
                )
                continue
            values = await self._sheets_client.read_values(
                block.sheet_name,
                range_from_start_cell(
                    start_cell=block.start_cell,
                    rows_count=2,
                    columns_count=1,
                ),
            )
            header_value = ""
            if len(values) >= 2 and values[1]:
                header_value = str(values[1][0]).strip()
            if header_value != "__bot_key":
                issues.append(
                    TableDiagnosticIssue(
                        "error",
                        f"В блоке {block.block_key} не найден __bot_key в первой физической колонке.",
                        True,
                    ),
                )
        if not issues:
            issues.append(TableDiagnosticIssue("ok", "__bot_key найден в управляемых блоках."))
        return tuple(issues)

    async def _hide_bot_key_columns_for_blocks(self, blocks: Sequence[SheetBlock]) -> None:
        """Скрывает первые физические колонки всех известных managed-блоков."""

        hidden: set[tuple[int, int]] = set()
        for block in blocks:
            if block.sheet_id is None:
                try:
                    metadata = await self._sheets_client.get_sheet_metadata(block.sheet_name)
                except GoogleSheetsError:
                    continue
                sheet_id = metadata.sheet_id
            else:
                sheet_id = block.sheet_id
            column_number, _ = _parse_a1_cell(block.start_cell, error_cls=SheetAdminError)
            column_index = column_number - 1
            key = (sheet_id, column_index)
            if key in hidden:
                continue
            hidden.add(key)
            await self.hide_bot_key_column(sheet_id=sheet_id, column_index=column_index)

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
        active_cwl_season: str | None,
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
            ["active_cwl_season", active_cwl_season or ""],
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


def _sheet_by_id(sheets: Sequence[SheetMetadata], sheet_id: int | None) -> SheetMetadata | None:
    """Ищет лист по числовому ID."""

    if sheet_id is None:
        return None
    return next((sheet for sheet in sheets if sheet.sheet_id == sheet_id), None)


def _sheet_by_title(sheets: Sequence[SheetMetadata], title: str) -> SheetMetadata | None:
    """Ищет лист по названию."""

    return next((sheet for sheet in sheets if sheet.title == title), None)
