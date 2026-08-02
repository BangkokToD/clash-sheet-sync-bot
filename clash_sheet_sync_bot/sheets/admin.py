"""Администрирование привязанной Google-таблицы."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from clash_sheet_sync_bot.common.time import utc_now_iso as _utc_now_iso
from clash_sheet_sync_bot.models import SheetBinding, SheetBlock
from clash_sheet_sync_bot.repositories import RaidSheetArchive
from clash_sheet_sync_bot.sheets.client import (
    CellValue,
    GoogleSheetsError,
    GoogleSheetsWriteError,
    SheetMetadata,
    SheetsClient,
    SpreadsheetMetadata,
    range_from_start_cell,
)
from clash_sheet_sync_bot.sheets.ranges import parse_a1_cell as _parse_a1_cell

SPREADSHEET_URL_RE: Final = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")
SPREADSHEET_ID_RE: Final = re.compile(r"^[a-zA-Z0-9-_]+$")

DEFAULT_COMPOSITION_SHEET_NAME: Final = "Состав"
DEFAULT_CWL_SHEET_NAME: Final = "CWL"
DEFAULT_RAID_SHEET_NAME: Final = "Рейды"
DEFAULT_BOT_STATE_SHEET_NAME: Final = "_bot_state"
RAID_STAGING_SHEET_PREFIX: Final = "Рейды - staging - "
RAID_BLOCK_PREFIX: Final = "raid:"
RAID_MESSAGE_BLOCK_PREFIX: Final = "raid_message:"
MANAGED_BY_VALUE: Final = "clash-sheet-sync-bot"
BOT_STATE_SCHEMA_VERSION: Final = "2"

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
class RaidArchiveCleanup:
    """Registry-запись, которую можно удалить после подтверждённого cleanup.

    Attributes:
        season_key: Стабильный ключ рейдового сезона.
        sheet_id: Физический ID отсутствующего или удалённого архива.
    """

    season_key: str
    sheet_id: int


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
        active_raid_sheet_name: Название активного рейдового листа.
        active_raid_sheet_id: Числовой ID активного рейдового листа.
        active_raid_season: Текущий рейдовый сезон или `None`.
        bot_state_sheet_name: Название служебного листа.
        bot_state_sheet_id: Числовой ID служебного листа.
        raid_archive_cleanup: Registry-записи для удаления в SQLite-транзакции.
        cleanup_warnings: Предупреждения незавершённого безопасного cleanup.
    """

    spreadsheet_id: str
    spreadsheet_url: str
    composition_sheet_name: str
    composition_sheet_id: int
    active_cwl_sheet_name: str
    active_cwl_sheet_id: int
    active_cwl_season: str | None
    active_raid_sheet_name: str
    active_raid_sheet_id: int
    active_raid_season: str | None
    bot_state_sheet_name: str
    bot_state_sheet_id: int
    raid_archive_cleanup: tuple[RaidArchiveCleanup, ...] = ()
    cleanup_warnings: tuple[str, ...] = ()


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
        raid_sheet = await self._ensure_sheet(
            known_sheets=metadata.sheets,
            title=DEFAULT_RAID_SHEET_NAME,
        )
        metadata = await self._sheets_client.get_spreadsheet_metadata()
        bot_state_sheet = await self._ensure_sheet(
            known_sheets=metadata.sheets,
            title=DEFAULT_BOT_STATE_SHEET_NAME,
        )
        await self._move_raid_before_cwl(raid_sheet=raid_sheet, cwl_sheet=cwl_sheet)

        await self._write_bot_state(
            chat_id=chat_id,
            composition_sheet=composition_sheet,
            cwl_sheet=cwl_sheet,
            active_cwl_season=None,
            raid_sheet=raid_sheet,
            active_raid_season=None,
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
            active_raid_sheet_name=raid_sheet.title,
            active_raid_sheet_id=raid_sheet.sheet_id,
            active_raid_season=None,
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
        raid_archives: Sequence[RaidSheetArchive] = (),
        raid_archive_limit: int = 4,
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
        cwl_sheet = resolve_active_cwl_sheet_metadata(
            metadata,
            configured_title=binding.active_cwl_sheet_name,
            active_sheet_id=binding.active_cwl_sheet_id,
            error_cls=SheetAdminError,
        )
        if cwl_sheet is None:
            issues.append(TableDiagnosticIssue("error", "Активный лист CWL отсутствует.", True))
        else:
            issues.append(
                TableDiagnosticIssue("ok", f"Активный лист CWL найден: {cwl_sheet.title}.")
            )

        raid_sheet = _resolve_active_raid_sheet(metadata.sheets, binding=binding)
        if binding.active_raid_sheet_id is None:
            issues.append(
                TableDiagnosticIssue("error", "active_raid_sheet_id отсутствует в SQLite.", True)
            )
        if raid_sheet is None:
            requires_apply_recovery = _requires_raid_apply_recovery(
                known_sheets=metadata.sheets,
                binding=binding,
                canonical_raid_sheet=None,
            )
            message = "Активный лист Рейды отсутствует."
            if requires_apply_recovery:
                message += " Bound raid-лист требует recovery через /sync."
            issues.append(
                TableDiagnosticIssue(
                    "error",
                    message,
                    not requires_apply_recovery,
                ),
            )
        else:
            issues.append(
                TableDiagnosticIssue("ok", f"Active raid лист найден: {raid_sheet.title}.")
            )
            if (
                binding.active_raid_sheet_id != raid_sheet.sheet_id
                or binding.active_raid_sheet_name != raid_sheet.title
            ):
                requires_apply_recovery = _requires_raid_apply_recovery(
                    known_sheets=metadata.sheets,
                    binding=binding,
                    canonical_raid_sheet=raid_sheet,
                )
                suffix = " Требуется raid recovery через /sync." if requires_apply_recovery else ""
                issues.append(
                    TableDiagnosticIssue(
                        "error",
                        "Raid binding не соответствует физическому canonical листу Рейды." + suffix,
                        not requires_apply_recovery,
                    ),
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
            issues.extend(_diagnose_raid_bot_state(bot_state=bot_state, binding=binding))
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

        issues.extend(
            await self._diagnose_bot_key_blocks(
                blocks,
                known_sheets=metadata.sheets,
            )
        )
        issues.extend(
            _diagnose_raid_archives(
                raid_archives=raid_archives,
                known_sheets=metadata.sheets,
                protected_sheet_ids=frozenset(
                    sheet_id
                    for sheet_id in (
                        None if composition_sheet is None else composition_sheet.sheet_id,
                        None if cwl_sheet is None else cwl_sheet.sheet_id,
                        None if raid_sheet is None else raid_sheet.sheet_id,
                        None if bot_state_sheet is None else bot_state_sheet.sheet_id,
                    )
                    if sheet_id is not None
                ),
                archive_limit=raid_archive_limit,
            )
        )
        staging_sheets = tuple(
            sheet.title
            for sheet in metadata.sheets
            if sheet.title.startswith("CWL - staging - ")
            or sheet.title.startswith(RAID_STAGING_SHEET_PREFIX)
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
        raid_archives: Sequence[RaidSheetArchive] = (),
        raid_archive_limit: int = 4,
    ) -> SheetSetupResult:
        """Восстанавливает обязательные листы, `_bot_state` и скрытые ключи."""

        self._validate_service_account_email()
        metadata = await self._sheets_client.get_spreadsheet_metadata()
        preflight_bot_state_sheet = _sheet_by_id(
            metadata.sheets,
            binding.bot_state_sheet_id,
        )
        if preflight_bot_state_sheet is None:
            preflight_bot_state_sheet = _sheet_by_title(
                metadata.sheets,
                binding.bot_state_sheet_name,
            )
        if preflight_bot_state_sheet is not None:
            bot_state = await self._read_bot_state(preflight_bot_state_sheet.title)
            _validate_bot_state_spreadsheet_id(
                bot_state=bot_state,
                expected_spreadsheet_id=binding.google_sheet_id,
            )

        preflight_raid_sheet = _resolve_active_raid_sheet(metadata.sheets, binding=binding)
        if _requires_raid_apply_recovery(
            known_sheets=metadata.sheets,
            binding=binding,
            canonical_raid_sheet=preflight_raid_sheet,
        ):
            raise SheetAdminError(
                "Обнаружена незавершённая raid rotation; "
                "auto-fix запрещён до raid recovery через /sync.",
            )

        composition_sheet = _sheet_by_id(metadata.sheets, binding.composition_sheet_id)
        if composition_sheet is None:
            composition_sheet = _sheet_by_title(metadata.sheets, binding.composition_sheet_name)
        if composition_sheet is None:
            composition_sheet = await self._sheets_client.add_sheet(binding.composition_sheet_name)

        metadata = await self._sheets_client.get_spreadsheet_metadata()
        cwl_sheet = resolve_active_cwl_sheet_metadata(
            metadata,
            configured_title=binding.active_cwl_sheet_name,
            active_sheet_id=binding.active_cwl_sheet_id,
            error_cls=SheetAdminError,
        )
        if cwl_sheet is None:
            cwl_sheet = await self._sheets_client.add_sheet(DEFAULT_CWL_SHEET_NAME)

        metadata = await self._sheets_client.get_spreadsheet_metadata()
        raid_sheet = _resolve_active_raid_sheet(metadata.sheets, binding=binding)
        if raid_sheet is None:
            raid_sheet = await self._sheets_client.add_sheet(DEFAULT_RAID_SHEET_NAME)

        metadata = await self._sheets_client.get_spreadsheet_metadata()
        bot_state_sheet = _sheet_by_id(metadata.sheets, binding.bot_state_sheet_id)
        if bot_state_sheet is None:
            bot_state_sheet = _sheet_by_title(metadata.sheets, binding.bot_state_sheet_name)
        if bot_state_sheet is None:
            bot_state_sheet = await self._sheets_client.add_sheet(binding.bot_state_sheet_name)
        bot_state = await self._read_bot_state(bot_state_sheet.title)
        _validate_bot_state_spreadsheet_id(
            bot_state=bot_state,
            expected_spreadsheet_id=binding.google_sheet_id,
        )

        await self._move_raid_before_cwl(raid_sheet=raid_sheet, cwl_sheet=cwl_sheet)

        await self._write_bot_state(
            chat_id=binding.chat_id,
            composition_sheet=composition_sheet,
            cwl_sheet=cwl_sheet,
            active_cwl_season=binding.active_cwl_season,
            raid_sheet=raid_sheet,
            active_raid_season=binding.active_raid_season,
            bot_state_sheet=bot_state_sheet,
            timezone=binding.timezone,
        )
        await self._sheets_client.hide_sheet(bot_state_sheet.sheet_id, hidden=True)

        metadata = await self._sheets_client.get_spreadsheet_metadata()
        await self._hide_bot_key_columns_for_blocks(
            blocks,
            known_sheets=metadata.sheets,
        )
        raid_archive_cleanup, cleanup_warnings = await self._cleanup_raid_archives(
            raid_archives=raid_archives,
            known_sheets=metadata.sheets,
            protected_sheet_ids=frozenset(
                {
                    composition_sheet.sheet_id,
                    cwl_sheet.sheet_id,
                    raid_sheet.sheet_id,
                    bot_state_sheet.sheet_id,
                },
            ),
            archive_limit=raid_archive_limit,
        )
        return SheetSetupResult(
            spreadsheet_id=self._spreadsheet_id,
            spreadsheet_url=spreadsheet_url(self._spreadsheet_id),
            composition_sheet_name=composition_sheet.title,
            composition_sheet_id=composition_sheet.sheet_id,
            active_cwl_sheet_name=cwl_sheet.title,
            active_cwl_sheet_id=cwl_sheet.sheet_id,
            active_cwl_season=binding.active_cwl_season,
            active_raid_sheet_name=raid_sheet.title,
            active_raid_sheet_id=raid_sheet.sheet_id,
            active_raid_season=binding.active_raid_season,
            bot_state_sheet_name=bot_state_sheet.title,
            bot_state_sheet_id=bot_state_sheet.sheet_id,
            raid_archive_cleanup=raid_archive_cleanup,
            cleanup_warnings=cleanup_warnings,
        )

    async def _move_raid_before_cwl(
        self,
        *,
        raid_sheet: SheetMetadata,
        cwl_sheet: SheetMetadata,
    ) -> None:
        """Размещает active raid непосредственно перед физическим CWL.

        Args:
            raid_sheet: Metadata обязательного active raid-листа.
            cwl_sheet: Metadata обязательного active CWL-листа.

        Raises:
            SheetAdminError: Если физические индексы обязательных листов неизвестны.
        """

        metadata = await self._sheets_client.get_spreadsheet_metadata()
        current_raid = _unique_sheet_by_id(metadata.sheets, raid_sheet.sheet_id)
        current_cwl = _unique_sheet_by_id(metadata.sheets, cwl_sheet.sheet_id)
        if current_raid is None or current_cwl is None or current_cwl.index is None:
            raise SheetAdminError("Не удалось определить позицию обязательных листов Рейды/CWL.")
        if current_raid.index is not None and current_raid.index + 1 == current_cwl.index:
            return
        await self._sheets_client.move_sheet(current_raid.sheet_id, current_cwl.index)

    async def _cleanup_raid_archives(
        self,
        *,
        raid_archives: Sequence[RaidSheetArchive],
        known_sheets: Sequence[SheetMetadata],
        protected_sheet_ids: frozenset[int],
        archive_limit: int,
    ) -> tuple[tuple[RaidArchiveCleanup, ...], tuple[str, ...]]:
        """Планирует stale cleanup и безопасно повторяет raid pruning.

        Args:
            raid_archives: Registry архивов в каноническом oldest-first порядке.
            known_sheets: Актуальные metadata физических листов.
            protected_sheet_ids: Физические IDs обязательных active/service-листов.
            archive_limit: Максимум зарегистрированных физических архивов.

        Returns:
            Cleanup registry-записей и recoverable предупреждения.

        Raises:
            SheetAdminError: Если physical sheet ID неоднозначен или registry небезопасен.
        """

        if isinstance(archive_limit, bool) or archive_limit < 1:
            raise SheetAdminError("Raid archive limit должен быть положительным целым числом.")

        cleanup: list[RaidArchiveCleanup] = []
        warnings: list[str] = []
        physical_archives: list[tuple[RaidSheetArchive, SheetMetadata]] = []
        for archive in raid_archives:
            matches = tuple(sheet for sheet in known_sheets if sheet.sheet_id == archive.sheet_id)
            if len(matches) > 1:
                raise SheetAdminError(
                    "Неоднозначный raid archive metadata для "
                    f"registry sheet_id={archive.sheet_id}.",
                )
            if not matches:
                cleanup.append(RaidArchiveCleanup(archive.season_key, archive.sheet_id))
                continue
            physical = matches[0]
            if physical.sheet_id in protected_sheet_ids or physical.title.startswith(
                RAID_STAGING_SHEET_PREFIX,
            ):
                warnings.append(
                    "Raid cleanup пропущен: registry указывает на protected/staging "
                    f"sheet_id={physical.sheet_id}.",
                )
                physical_archives.append((archive, physical))
                continue
            physical_archives.append((archive, physical))

        excess = len(physical_archives) - archive_limit
        for archive, physical in physical_archives:
            if excess <= 0:
                break
            if physical.sheet_id in protected_sheet_ids or physical.title.startswith(
                RAID_STAGING_SHEET_PREFIX,
            ):
                break
            try:
                await self._sheets_client.delete_sheet(physical.sheet_id)
            except GoogleSheetsWriteError:
                warnings.append(
                    "Raid cleanup не завершён: не удалось удалить зарегистрированный "
                    f"архив {archive.sheet_name!r}; pruning будет повторён.",
                )
                break
            cleanup.append(RaidArchiveCleanup(archive.season_key, archive.sheet_id))
            excess -= 1
        return tuple(cleanup), tuple(warnings)

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
        *,
        known_sheets: Sequence[SheetMetadata],
    ) -> tuple[TableDiagnosticIssue, ...]:
        """Проверяет наличие `__bot_key` в управляемых табличных блоках."""

        table_blocks = [
            block
            for block in blocks
            if not block.block_key.startswith("cwl_message:")
            and not block.block_key.startswith(RAID_MESSAGE_BLOCK_PREFIX)
        ]
        if not table_blocks:
            return (
                TableDiagnosticIssue(
                    "warning",
                    "Управляемые блоки ещё не создавались. Запустите /sync.",
                    False,
                ),
            )

        issues: list[TableDiagnosticIssue] = []
        valid_bot_key_found = False
        for block in table_blocks:
            physical_sheet_id: int
            if block.block_key.startswith(RAID_BLOCK_PREFIX):
                physical_matches = tuple(
                    sheet for sheet in known_sheets if sheet.sheet_id == block.sheet_id
                )
                if len(physical_matches) != 1 or physical_matches[0].title != block.sheet_name:
                    issues.append(
                        TableDiagnosticIssue(
                            "error",
                            f"Raid block {block.block_key} имеет stale physical sheet metadata.",
                            True,
                        ),
                    )
                    continue
                physical_sheet_id = physical_matches[0].sheet_id
            else:
                try:
                    physical_sheet_id = _required_block_sheet_id(block, known_sheets)
                except SheetAdminError:
                    issues.append(
                        TableDiagnosticIssue(
                            "warning",
                            f"Лист managed block {block.block_key} не разрешён однозначно.",
                            True,
                        ),
                    )
                    continue
            if block.rows_count < 2 or block.columns_count < 1:
                issues.append(
                    TableDiagnosticIssue(
                        "error",
                        f"Блок {block.block_key} слишком мал для заголовка __bot_key.",
                        False,
                    ),
                )
                continue
            try:
                values = await self._sheets_client.read_values(
                    block.sheet_name,
                    range_from_start_cell(
                        start_cell=block.start_cell,
                        rows_count=2,
                        columns_count=1,
                    ),
                )
            except GoogleSheetsError:
                issues.append(
                    TableDiagnosticIssue(
                        "warning",
                        f"Блок {block.block_key} ссылается на недоступный лист {block.sheet_name}.",
                        True,
                    ),
                )
                continue
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
            else:
                valid_bot_key_found = True
                column_number, _ = _parse_a1_cell(block.start_cell, error_cls=SheetAdminError)
                try:
                    is_hidden = await self._sheets_client.is_column_hidden(
                        sheet_id=physical_sheet_id,
                        sheet_name=block.sheet_name,
                        column_index=column_number - 1,
                    )
                except GoogleSheetsError:
                    issues.append(
                        TableDiagnosticIssue(
                            "warning",
                            f"Не удалось проверить скрытие __bot_key блока {block.block_key}.",
                            True,
                        ),
                    )
                else:
                    if not is_hidden:
                        issues.append(
                            TableDiagnosticIssue(
                                "error",
                                f"__bot_key блока {block.block_key} не скрыт.",
                                True,
                            ),
                        )
        if valid_bot_key_found:
            issues.append(TableDiagnosticIssue("ok", "__bot_key найден в управляемых блоках."))
        return tuple(issues)

    async def _hide_bot_key_columns_for_blocks(
        self,
        blocks: Sequence[SheetBlock],
        *,
        known_sheets: Sequence[SheetMetadata],
    ) -> None:
        """Скрывает первые физические колонки, игнорируя stale sheet_id блоков."""

        sheets_by_id = {sheet.sheet_id: sheet for sheet in known_sheets}
        sheets_by_title = {sheet.title: sheet for sheet in known_sheets}

        hidden: set[tuple[int, int]] = set()
        for block in blocks:
            sheet = None
            if block.sheet_id is not None:
                sheet = sheets_by_id.get(block.sheet_id)
            is_raid_block = block.block_key.startswith(
                (RAID_BLOCK_PREFIX, RAID_MESSAGE_BLOCK_PREFIX),
            )
            if sheet is None and not is_raid_block:
                sheet = sheets_by_title.get(block.sheet_name)
            if is_raid_block and sheet is not None and sheet.title != block.sheet_name:
                sheet = None
            if sheet is None:
                continue

            column_number, _ = _parse_a1_cell(block.start_cell, error_cls=SheetAdminError)
            column_index = column_number - 1
            key = (sheet.sheet_id, column_index)
            if key in hidden:
                continue
            hidden.add(key)
            await self.hide_bot_key_column(sheet_id=sheet.sheet_id, column_index=column_index)

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

        matches = tuple(sheet for sheet in known_sheets if sheet.title == title)
        if len(matches) > 1:
            raise SheetAdminError(f"Найдено несколько обязательных листов с названием {title!r}.")
        if matches:
            return matches[0]
        return await self._sheets_client.add_sheet(title)

    async def _write_bot_state(
        self,
        *,
        chat_id: int,
        composition_sheet: SheetMetadata,
        cwl_sheet: SheetMetadata,
        active_cwl_season: str | None,
        raid_sheet: SheetMetadata,
        active_raid_season: str | None,
        bot_state_sheet: SheetMetadata,
        timezone: str,
    ) -> None:
        """Записывает служебный лист `_bot_state`.

        Args:
            chat_id: ID Telegram-группы.
            composition_sheet: Metadata листа состава.
            cwl_sheet: Metadata активного CWL-листа.
            active_cwl_season: Текущий CWL-сезон или `None`.
            raid_sheet: Metadata активного рейдового листа.
            active_raid_season: Текущий raid season или `None`.
            bot_state_sheet: Metadata листа `_bot_state`.
            timezone: IANA-таймзона привязки.
        """

        values = build_bot_state_values(
            chat_id=chat_id,
            google_sheet_id=self._spreadsheet_id,
            composition_sheet_name=composition_sheet.title,
            composition_sheet_id=composition_sheet.sheet_id,
            active_cwl_sheet_name=cwl_sheet.title,
            active_cwl_sheet_id=cwl_sheet.sheet_id,
            active_cwl_season=active_cwl_season,
            active_raid_sheet_name=raid_sheet.title,
            active_raid_sheet_id=raid_sheet.sheet_id,
            active_raid_season=active_raid_season,
            bot_state_sheet_name=bot_state_sheet.title,
            bot_state_sheet_id=bot_state_sheet.sheet_id,
            timezone=timezone,
        )
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


def build_bot_state_values(
    *,
    chat_id: int,
    google_sheet_id: str,
    composition_sheet_name: str,
    composition_sheet_id: int | None,
    active_cwl_sheet_name: str,
    active_cwl_sheet_id: int | None,
    active_cwl_season: str | None,
    active_raid_sheet_name: str,
    active_raid_sheet_id: int | None,
    active_raid_season: str | None,
    bot_state_sheet_name: str,
    bot_state_sheet_id: int | None,
    timezone: str,
) -> list[list[CellValue]]:
    """Строит единое служебное зеркало binding для setup и sync.

    Args:
        chat_id: ID Telegram-группы.
        google_sheet_id: ID Google Spreadsheet.
        composition_sheet_name: Название листа состава.
        composition_sheet_id: Физический ID листа состава.
        active_cwl_sheet_name: Название active CWL.
        active_cwl_sheet_id: Физический ID active CWL.
        active_cwl_season: Активный CWL-сезон или `None`.
        active_raid_sheet_name: Название active raid.
        active_raid_sheet_id: Физический ID active raid.
        active_raid_season: Активный raid season или `None`.
        bot_state_sheet_name: Название служебного листа.
        bot_state_sheet_id: Физический ID служебного листа.
        timezone: IANA-таймзона binding.

    Returns:
        Матрица `_bot_state` актуальной schema version.
    """

    return [
        ["managed_by", MANAGED_BY_VALUE],
        ["schema_version", BOT_STATE_SCHEMA_VERSION],
        ["chat_id", chat_id],
        ["google_sheet_id", google_sheet_id],
        ["composition_sheet_name", composition_sheet_name],
        ["composition_sheet_id", "" if composition_sheet_id is None else composition_sheet_id],
        ["active_cwl_sheet_name", active_cwl_sheet_name],
        ["active_cwl_sheet_id", "" if active_cwl_sheet_id is None else active_cwl_sheet_id],
        ["active_cwl_season", active_cwl_season or ""],
        ["active_raid_sheet_name", active_raid_sheet_name],
        ["active_raid_sheet_id", "" if active_raid_sheet_id is None else active_raid_sheet_id],
        ["active_raid_season", active_raid_season or ""],
        ["bot_state_sheet_name", bot_state_sheet_name],
        ["bot_state_sheet_id", "" if bot_state_sheet_id is None else bot_state_sheet_id],
        ["timezone", timezone],
        ["updated_at", _utc_now_iso()],
    ]


def resolve_active_cwl_sheet_metadata(
    metadata: SpreadsheetMetadata,
    *,
    configured_title: str,
    active_sheet_id: int | None,
    error_cls: type[Exception],
) -> SheetMetadata | None:
    """Выбирает metadata физического активного CWL-листа без Sheets-операций.

    Args:
        metadata: Актуальная metadata таблицы с физическими листами.
        configured_title: Точное имя активного CWL-листа из binding.
        active_sheet_id: Физический ID активного CWL-листа из binding.
        error_cls: Доменный тип ошибки для неоднозначного безопасного active CWL.

    Returns:
        Metadata выбранного активного CWL-листа или ``None``, если безопасный
        active CWL отсутствует и вызывающий контекст должен обработать это.

    Raises:
        error_cls: Если metadata безопасного active CWL неоднозначна. При его
            отсутствии обязательный физический лист отклоняется тем же доменным
            типом ошибки в вызывающем контексте.
    """

    canonical_matches = tuple(
        sheet for sheet in metadata.sheets if sheet.title == DEFAULT_CWL_SHEET_NAME
    )
    canonical = _unique_cwl_sheet_match(
        canonical_matches,
        criterion=f"canonical title={DEFAULT_CWL_SHEET_NAME!r}",
        error_cls=error_cls,
    )
    if canonical is not None:
        return canonical

    if active_sheet_id is not None:
        id_matches = tuple(sheet for sheet in metadata.sheets if sheet.sheet_id == active_sheet_id)
        sheet_by_id = _unique_cwl_sheet_match(
            id_matches,
            criterion=f"sheet_id={active_sheet_id}",
            error_cls=error_cls,
        )
        if (
            sheet_by_id is not None
            and sheet_by_id.title == configured_title
            and _is_allowed_active_cwl_title(sheet_by_id.title)
        ):
            return sheet_by_id

    title_matches = tuple(sheet for sheet in metadata.sheets if sheet.title == configured_title)
    sheet_by_title = _unique_cwl_sheet_match(
        title_matches,
        criterion=f"binding title={configured_title!r}",
        error_cls=error_cls,
    )
    if sheet_by_title is not None and _is_allowed_active_cwl_title(
        sheet_by_title.title,
    ):
        return sheet_by_title
    return None


def _unique_cwl_sheet_match(
    matches: Sequence[SheetMetadata],
    *,
    criterion: str,
    error_cls: type[Exception],
) -> SheetMetadata | None:
    """Возвращает единственный exact CWL match или отклоняет ambiguity."""

    if len(matches) > 1:
        raise error_cls(
            f"Неоднозначный active CWL для {criterion}: найдено {len(matches)} листа.",
        )
    return matches[0] if matches else None


def _is_allowed_active_cwl_title(title: str) -> bool:
    """Запрещает service-looking CWL staging/archive как active fallback."""

    return title == DEFAULT_CWL_SHEET_NAME or not title.startswith(DEFAULT_CWL_SHEET_NAME)


def _diagnose_raid_bot_state(
    *,
    bot_state: dict[str, str],
    binding: SheetBinding,
) -> tuple[TableDiagnosticIssue, ...]:
    """Проверяет raid fields и schema version служебного зеркала."""

    required_fields = {
        "active_raid_sheet_name",
        "active_raid_sheet_id",
        "active_raid_season",
    }
    if bot_state.get("schema_version") != BOT_STATE_SCHEMA_VERSION or not required_fields.issubset(
        bot_state,
    ):
        return (
            TableDiagnosticIssue(
                "warning",
                "Legacy _bot_state не содержит актуальные raid fields.",
                True,
            ),
        )

    expected = {
        "active_raid_sheet_name": binding.active_raid_sheet_name,
        "active_raid_sheet_id": ""
        if binding.active_raid_sheet_id is None
        else str(binding.active_raid_sheet_id),
        "active_raid_season": binding.active_raid_season or "",
    }
    mismatches = tuple(key for key, value in expected.items() if bot_state.get(key) != value)
    if mismatches:
        return (
            TableDiagnosticIssue(
                "error",
                "Raid binding fields в SQLite и _bot_state не совпадают: "
                + ", ".join(mismatches)
                + ".",
                True,
            ),
        )
    return (TableDiagnosticIssue("ok", "Raid binding fields в _bot_state совпадают."),)


def _diagnose_raid_archives(
    *,
    raid_archives: Sequence[RaidSheetArchive],
    known_sheets: Sequence[SheetMetadata],
    protected_sheet_ids: frozenset[int],
    archive_limit: int,
) -> tuple[TableDiagnosticIssue, ...]:
    """Проверяет registry архивов по физическим sheet IDs."""

    issues: list[TableDiagnosticIssue] = []
    for archive in raid_archives:
        matches = tuple(sheet for sheet in known_sheets if sheet.sheet_id == archive.sheet_id)
        if not matches:
            issues.append(
                TableDiagnosticIssue(
                    "warning",
                    f"Stale raid archive registry: sheet_id={archive.sheet_id} отсутствует.",
                    True,
                ),
            )
        elif len(matches) > 1:
            issues.append(
                TableDiagnosticIssue(
                    "error",
                    f"Raid archive sheet_id={archive.sheet_id} неоднозначен в metadata.",
                    False,
                ),
            )
        elif matches[0].sheet_id in protected_sheet_ids or matches[0].title.startswith(
            RAID_STAGING_SHEET_PREFIX,
        ):
            issues.append(
                TableDiagnosticIssue(
                    "error",
                    "Raid archive registry указывает на protected/staging "
                    f"sheet_id={archive.sheet_id}.",
                    False,
                ),
            )

    registered_count = sum(archive.sheet_id not in protected_sheet_ids for archive in raid_archives)
    if registered_count > archive_limit:
        issues.append(
            TableDiagnosticIssue(
                "warning",
                f"Raid retention overflow: зарегистрировано {registered_count}, лимит {archive_limit}.",
                True,
            ),
        )
    if not issues and raid_archives:
        issues.append(TableDiagnosticIssue("ok", "Raid archive registry согласован."))
    return tuple(issues)


def _resolve_active_raid_sheet(
    sheets: Sequence[SheetMetadata],
    *,
    binding: SheetBinding,
) -> SheetMetadata | None:
    """Разрешает только единственный физический canonical active raid."""

    canonical = tuple(sheet for sheet in sheets if sheet.title == DEFAULT_RAID_SHEET_NAME)
    if len(canonical) > 1:
        raise SheetAdminError("Найдено несколько canonical active-листов Рейды.")
    if canonical:
        return canonical[0]

    bound = tuple(sheet for sheet in sheets if sheet.sheet_id == binding.active_raid_sheet_id)
    if len(bound) > 1:
        raise SheetAdminError("active_raid_sheet_id неоднозначен в Sheets metadata.")
    if (
        len(bound) == 1
        and binding.active_raid_sheet_name == DEFAULT_RAID_SHEET_NAME
        and bound[0].title == DEFAULT_RAID_SHEET_NAME
    ):
        return bound[0]
    return None


def _requires_raid_apply_recovery(
    *,
    known_sheets: Sequence[SheetMetadata],
    binding: SheetBinding,
    canonical_raid_sheet: SheetMetadata | None,
) -> bool:
    """Проверяет, должен ли stale raid binding восстанавливать только raid apply."""

    if binding.active_raid_season is None or binding.active_raid_sheet_id is None:
        return False
    bound_matches = tuple(
        sheet for sheet in known_sheets if sheet.sheet_id == binding.active_raid_sheet_id
    )
    if len(bound_matches) > 1:
        raise SheetAdminError("active_raid_sheet_id неоднозначен в Sheets metadata.")
    if not bound_matches or bound_matches[0].title.startswith(RAID_STAGING_SHEET_PREFIX):
        return False
    return (
        canonical_raid_sheet is None or bound_matches[0].sheet_id != canonical_raid_sheet.sheet_id
    )


def _validate_bot_state_spreadsheet_id(
    *,
    bot_state: dict[str, str],
    expected_spreadsheet_id: str,
) -> None:
    """Останавливает auto-fix при foreign ownership marker `_bot_state`."""

    mirrored_spreadsheet_id = bot_state.get("google_sheet_id")
    if mirrored_spreadsheet_id not in {None, "", expected_spreadsheet_id}:
        raise SheetAdminError(
            "google_sheet_id в SQLite и _bot_state не совпадает; auto-fix запрещён.",
        )


def _unique_sheet_by_id(
    sheets: Sequence[SheetMetadata],
    sheet_id: int,
) -> SheetMetadata | None:
    """Возвращает единственный physical sheet ID или отклоняет ambiguity."""

    matches = tuple(sheet for sheet in sheets if sheet.sheet_id == sheet_id)
    if len(matches) > 1:
        raise SheetAdminError(f"Физический sheet_id={sheet_id} неоднозначен.")
    return matches[0] if matches else None


def _required_block_sheet_id(
    block: SheetBlock,
    known_sheets: Sequence[SheetMetadata],
) -> int:
    """Разрешает physical ID non-raid блока по действующему legacy fallback."""

    if block.sheet_id is not None:
        sheet = _unique_sheet_by_id(known_sheets, block.sheet_id)
        if sheet is not None:
            return sheet.sheet_id
    matches = tuple(sheet for sheet in known_sheets if sheet.title == block.sheet_name)
    if len(matches) != 1:
        raise SheetAdminError(f"Лист managed block {block.block_key} не разрешён однозначно.")
    return matches[0].sheet_id


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
