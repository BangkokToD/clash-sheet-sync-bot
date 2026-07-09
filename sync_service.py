"""Оркестрация `/sync`, rate limit, locks и Telegram-отчёты."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import httpx

from coc_client import ClashApiUnavailableError, ClashClient
from composition_sync import (
    CompositionDataError,
    CompositionSyncResult,
    apply_prepared_composition_sync,
    prepare_composition_sync,
)
from config import AppConfig
from cwl_sync import (
    CwlDataError,
    apply_public_cwl_sync,
    prepare_public_cwl_sync,
)
from report_builder import build_error_report, build_status_report, build_success_report
from repositories import (
    CompositionPlayerStateRepository,
    CwlRowStateRepository,
    RuntimeConfigRepository,
    SheetBindingRepository,
    SheetBlockRepository,
    SyncRunRepository,
    TelegramChatRepository,
)
from sheets_client import GoogleAccessTokenProvider, GoogleSheetsError, SheetsClient
from telegram_client import TelegramApiError, TelegramClient

CHAT_SYNC_LOCKS: dict[int, asyncio.Lock] = {}
SHEET_SYNC_LOCKS: dict[str, asyncio.Lock] = {}
GLOBAL_SEMAPHORE: asyncio.Semaphore | None = None
GLOBAL_SEMAPHORE_LIMIT: int | None = None
SYNC_HTTP_TIMEOUT_SECONDS: Final = 90.0
WRITE_PHASE_PREPARED: Final = "prepared"
WRITE_PHASE_COMPOSITION_WRITTEN: Final = "composition_written"
WRITE_PHASE_CWL_WRITTEN: Final = "cwl_written"
WRITE_PHASE_SQLITE_COMMITTED: Final = "sqlite_committed"
PARTIAL_SHEET_WRITE_WARNING: Final = (
    "Таблица могла быть частично обновлена. Запустите диагностику и повторите /sync."
)
UNEXPECTED_SYNC_ERROR_REASON: Final = (
    "Непредвиденная ошибка во время обновления. Подробности записаны в лог."
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SyncChatInfo:
    """Минимальная информация о Telegram-чате для `/sync`.

    Attributes:
        chat_id: ID Telegram-чата.
        type: Тип Telegram-чата.
    """

    chat_id: int
    type: str


class SyncService:
    """Оркестратор команд `/sync` и `/status`.

    Args:
        config: Глобальная конфигурация приложения.
        telegram: Telegram Bot API клиент.
        connection: SQLite-подключение.
    """

    def __init__(self, *, config: AppConfig, telegram: TelegramClient, connection: Any) -> None:
        self._config = config
        self._telegram = telegram
        self._connection = connection
        self._runtime = RuntimeConfigRepository(connection)
        self._telegram_chats = TelegramChatRepository(connection)
        self._sync_runs = SyncRunRepository(connection)

    async def handle_sync_command(self, *, chat: SyncChatInfo, user_id: int) -> None:
        """Обрабатывает команду `/sync` из Telegram."""

        if chat.type == "private":
            await self._telegram.send_message(
                chat_id=chat.chat_id,
                text="Команда /sync работает в подключённой группе.",
            )
            return

        runtime = await self._runtime.get_runtime_chat_config(chat.chat_id)
        if runtime is None or runtime.status != "ready":
            await self._telegram.send_message(
                chat_id=chat.chat_id,
                text="Группа не настроена. Администратор может подключить её через личный чат с ботом.",
            )
            return
        if not runtime.active_clans:
            await self._telegram.send_message(
                chat_id=chat.chat_id, text="Добавьте хотя бы один клан в настройках."
            )
            return

        retry_after = await self._rate_limit_retry_after(chat.chat_id)
        if retry_after > 0:
            await self._mark_rate_limited(chat_id=chat.chat_id, user_id=user_id)
            await self._telegram.send_message(
                chat_id=chat.chat_id,
                text=f"Обновление недавно запускалось. Повторить можно через {retry_after} сек.",
            )
            return

        chat_lock = _lock_for_key(CHAT_SYNC_LOCKS, chat.chat_id)
        if chat_lock.locked():
            await self._telegram.send_message(
                chat_id=chat.chat_id, text="Обновление уже выполняется."
            )
            return

        async with chat_lock:
            runtime = await self._runtime.get_runtime_chat_config(chat.chat_id)
            if runtime is None or runtime.status != "ready":
                await self._telegram.send_message(
                    chat_id=chat.chat_id,
                    text="Группа не настроена. Администратор может подключить её через личный чат с ботом.",
                )
                return

            sheet_lock = _lock_for_key(SHEET_SYNC_LOCKS, runtime.sheet_binding.google_sheet_id)
            if sheet_lock.locked():
                await self._telegram.send_message(
                    chat_id=chat.chat_id, text="Обновление уже выполняется."
                )
                return

            async with sheet_lock:
                semaphore = _global_semaphore(self._config.max_concurrent_syncs)
                async with semaphore:
                    await self._run_sync(runtime_chat_id=chat.chat_id, user_id=user_id)

    async def handle_status_command(self, *, chat: SyncChatInfo) -> None:
        """Обрабатывает команду `/status`."""

        if chat.type == "private":
            await self._telegram.send_message(
                chat_id=chat.chat_id,
                text="Команда /status работает в подключённой группе.",
            )
            return

        summary = await self._telegram_chats.get_sync_status_summary(chat.chat_id)
        if summary is None or summary.status == "disabled":
            await self._telegram.send_message(
                chat_id=chat.chat_id,
                text="Группа не настроена. Администратор может подключить её через личный чат с ботом.",
            )
            return

        payload = build_status_report(summary)
        await self._telegram.send_message(
            chat_id=chat.chat_id,
            text=payload.text,
            parse_mode=payload.parse_mode,
            disable_web_page_preview=payload.disable_web_page_preview,
        )

    async def _run_sync(self, *, runtime_chat_id: int, user_id: int) -> None:
        """Выполняет staged `/sync` после прохождения проверок и locks."""

        started_at_dt = _utc_now()
        started_at = _format_dt(started_at_dt)
        await self._telegram_chats.mark_sync_started(chat_id=runtime_chat_id, started_at=started_at)
        sync_run_id = await self._sync_runs.create_sync_run(
            chat_id=runtime_chat_id,
            started_by_user_id=user_id,
            status="skipped",
            started_at=started_at,
        )
        await self._connection.commit()

        write_phase = WRITE_PHASE_PREPARED
        runtime = await self._runtime.get_runtime_chat_config(runtime_chat_id)
        if runtime is None:
            await self._finish_error(
                chat_id=runtime_chat_id,
                sync_run_id=sync_run_id,
                error_stage=write_phase,
                reason="RuntimeChatConfig не найден.",
                spreadsheet_url=None,
            )
            return

        spreadsheet_url = runtime.sheet_binding.spreadsheet_url
        is_baseline = not await self._sync_runs.has_successful_sync(runtime_chat_id)

        try:
            token_provider = GoogleAccessTokenProvider(self._config.google_service_account_file)
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(SYNC_HTTP_TIMEOUT_SECONDS, connect=10.0),
            ) as http_client:
                clash_client = ClashClient(self._config.coc_api_token, http_client)
                sheets_client = SheetsClient(
                    runtime.sheet_binding.google_sheet_id, token_provider, http_client
                )

                composition_repository = CompositionPlayerStateRepository(self._connection)
                cwl_repository = CwlRowStateRepository(self._connection)
                sheet_block_repository = SheetBlockRepository(self._connection)
                sheet_binding_repository = SheetBindingRepository(self._connection)

                prepared_composition = await prepare_composition_sync(
                    runtime_config=runtime,
                    clash_client=clash_client,
                    sheets_client=sheets_client,
                    composition_repository=composition_repository,
                    sheet_block_repository=sheet_block_repository,
                    detected_at=started_at_dt,
                )
                prepared_cwl = await prepare_public_cwl_sync(
                    runtime_config=runtime,
                    clash_client=clash_client,
                    sheets_client=sheets_client,
                    cwl_repository=cwl_repository,
                    sheet_block_repository=sheet_block_repository,
                    cwl_war_concurrency_limit=self._config.cwl_war_concurrency_limit,
                )

                write_phase = WRITE_PHASE_COMPOSITION_WRITTEN
                await apply_prepared_composition_sync(
                    runtime_config=runtime,
                    sheets_client=sheets_client,
                    composition_repository=composition_repository,
                    sheet_block_repository=sheet_block_repository,
                    detected_at=started_at_dt,
                    prepared=prepared_composition,
                )

                write_phase = WRITE_PHASE_CWL_WRITTEN
                cwl_result = await apply_public_cwl_sync(
                    runtime_config=runtime,
                    sheets_client=sheets_client,
                    cwl_repository=cwl_repository,
                    sheet_block_repository=sheet_block_repository,
                    sheet_binding_repository=sheet_binding_repository,
                    sync_run_id=sync_run_id,
                    prepared=prepared_cwl,
                )

            composition_result = CompositionSyncResult(
                active_counts=prepared_composition.active_counts,
                exited_count=prepared_composition.exited_count,
                diff_items=prepared_composition.diff_items,
                warnings=prepared_composition.warnings,
            )
            report = build_success_report(
                composition_result=composition_result,
                cwl_result=cwl_result,
                spreadsheet_url=spreadsheet_url,
                report_max_items=self._config.report_max_items,
                is_baseline=is_baseline,
            )
            finished_at = _format_dt(_utc_now())
            await self._sync_runs.finish_sync_run(
                sync_run_id=sync_run_id,
                status="success",
                finished_at=finished_at,
                report_json=json.dumps({"telegram_report": report.text}, ensure_ascii=False),
            )
            await self._telegram_chats.mark_sync_finished(
                chat_id=runtime_chat_id,
                finished_at=finished_at,
                status="success",
                error=None,
            )
            await self._connection.commit()
            write_phase = WRITE_PHASE_SQLITE_COMMITTED
            try:
                await self._telegram.send_message(
                    chat_id=runtime_chat_id,
                    text=report.text,
                    parse_mode=report.parse_mode,
                    disable_web_page_preview=report.disable_web_page_preview,
                )
            except TelegramApiError as exc:
                logger.warning("sync finished, but telegram report delivery failed: %s", exc)
        except (
            ClashApiUnavailableError,
            GoogleSheetsError,
            CompositionDataError,
            CwlDataError,
        ) as exc:
            await self._connection.rollback()
            reason = _sync_error_reason(str(exc), write_phase)
            await self._finish_error(
                chat_id=runtime_chat_id,
                sync_run_id=sync_run_id,
                error_stage=write_phase,
                reason=reason,
                spreadsheet_url=spreadsheet_url,
            )
        except Exception:
            await self._connection.rollback()
            logger.exception("unexpected sync failure")
            reason = _sync_error_reason(UNEXPECTED_SYNC_ERROR_REASON, write_phase)
            await self._finish_error(
                chat_id=runtime_chat_id,
                sync_run_id=sync_run_id,
                error_stage=write_phase,
                reason=reason,
                spreadsheet_url=spreadsheet_url,
            )

    async def _finish_error(
        self,
        *,
        chat_id: int,
        sync_run_id: int,
        error_stage: str | None,
        reason: str,
        spreadsheet_url: str | None,
    ) -> None:
        """Сохраняет ошибку sync и отправляет Telegram-отчёт."""

        finished_at = _format_dt(_utc_now())
        report = build_error_report(reason=reason, spreadsheet_url=spreadsheet_url)
        await self._sync_runs.finish_sync_run(
            sync_run_id=sync_run_id,
            status="error",
            finished_at=finished_at,
            error_stage=error_stage,
            error_message=reason,
            report_json=json.dumps({"telegram_report": report.text}, ensure_ascii=False),
        )
        await self._telegram_chats.mark_sync_finished(
            chat_id=chat_id,
            finished_at=finished_at,
            status="error",
            error=reason,
        )
        await self._connection.commit()
        await self._telegram.send_message(
            chat_id=chat_id,
            text=report.text,
            parse_mode=report.parse_mode,
            disable_web_page_preview=report.disable_web_page_preview,
        )

    async def _mark_rate_limited(self, *, chat_id: int, user_id: int) -> None:
        """Пишет skipped/rate_limited sync run для истории."""

        now = _format_dt(_utc_now())
        sync_run_id = await self._sync_runs.create_sync_run(
            chat_id=chat_id,
            started_by_user_id=user_id,
            status="rate_limited",
            started_at=now,
        )
        await self._sync_runs.finish_sync_run(
            sync_run_id=sync_run_id,
            status="rate_limited",
            finished_at=now,
        )
        await self._connection.commit()

    async def _rate_limit_retry_after(self, chat_id: int) -> int:
        """Считает остаток cooldown для чата."""

        last_started_at = await self._telegram_chats.get_last_sync_started_at(chat_id)
        if last_started_at is None or self._config.sync_cooldown_seconds <= 0:
            return 0
        try:
            last_started = datetime.fromisoformat(last_started_at)
        except ValueError:
            return 0
        if last_started.tzinfo is None:
            last_started = last_started.replace(tzinfo=UTC)
        elapsed = (_utc_now() - last_started.astimezone(UTC)).total_seconds()
        remaining = self._config.sync_cooldown_seconds - int(elapsed)
        return max(remaining, 0)


def _lock_for_key(storage: dict[Any, asyncio.Lock], key: Any) -> asyncio.Lock:
    """Возвращает lock для ключа."""

    lock = storage.get(key)
    if lock is None:
        lock = asyncio.Lock()
        storage[key] = lock
    return lock


def _global_semaphore(limit: int) -> asyncio.Semaphore:
    """Возвращает process-local global semaphore."""

    global GLOBAL_SEMAPHORE, GLOBAL_SEMAPHORE_LIMIT
    if GLOBAL_SEMAPHORE is None or limit != GLOBAL_SEMAPHORE_LIMIT:
        GLOBAL_SEMAPHORE = asyncio.Semaphore(limit)
        GLOBAL_SEMAPHORE_LIMIT = limit
    return GLOBAL_SEMAPHORE


def _sync_error_reason(reason: str, write_phase: str) -> str:
    """Дополняет ошибку предупреждением о возможной частичной записи."""

    normalized_reason = reason.strip() or "Ошибка во время обновления."
    if not _has_sheet_write_started(write_phase):
        return normalized_reason
    if PARTIAL_SHEET_WRITE_WARNING in normalized_reason:
        return normalized_reason
    return f"{normalized_reason}\n\n{PARTIAL_SHEET_WRITE_WARNING}"


def _has_sheet_write_started(write_phase: str) -> bool:
    """Проверяет, могла ли Google-таблица уже измениться."""

    return write_phase in {WRITE_PHASE_COMPOSITION_WRITTEN, WRITE_PHASE_CWL_WRITTEN}


def _utc_now() -> datetime:
    """Возвращает текущую UTC-дату."""

    return datetime.now(UTC).replace(microsecond=0)


def _format_dt(value: datetime) -> str:
    """Форматирует дату для SQLite."""

    return value.astimezone(UTC).replace(microsecond=0).isoformat()
