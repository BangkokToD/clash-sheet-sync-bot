"""Telegram flow пользовательской команды `/cwl_forecast`."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC
from typing import Final

import aiosqlite
import httpx

from clash_sheet_sync_bot.coc.client import ClashClient
from clash_sheet_sync_bot.common.time import utc_now
from clash_sheet_sync_bot.models import AppConfig
from clash_sheet_sync_bot.repositories import (
    ClanSettingsRepository,
    CwlForecastRepository,
    TelegramChatRepository,
)
from clash_sheet_sync_bot.telegram.client import (
    TelegramApiError,
    TelegramBadRequestError,
    TelegramClient,
)
from clash_sheet_sync_bot.telegram.emoji_catalog import TelegramEmojiCatalog

from .models import ForecastInactive, ForecastReady, ForecastScheduleRequired
from .service import CwlForecastService, ForecastClashClient

FORECAST_CHAT_LOCKS: dict[int, asyncio.Lock] = {}
HTTP_TIMEOUT_SECONDS: Final = 60.0
logger = logging.getLogger(__name__)


class CwlForecastFlow:
    """Проверяет access/cooldown и доставляет ordered per-clan forecasts."""

    def __init__(
        self,
        *,
        config: AppConfig,
        telegram: TelegramClient,
        connection: aiosqlite.Connection,
        catalog: TelegramEmojiCatalog,
        clash_client: ForecastClashClient | None = None,
    ) -> None:
        self._config = config
        self._telegram = telegram
        self._connection = connection
        self._catalog = catalog
        self._clash_client = clash_client
        self._repository = CwlForecastRepository(connection)
        self._chats = TelegramChatRepository(connection)
        self._clans = ClanSettingsRepository(connection)

    async def handle_command(self, *, chat_id: int, chat_type: str) -> None:
        """Принимает forecast до API и удерживает process-local singleflight."""

        if chat_type not in {"group", "supergroup"} or not await self._chats.is_connected_group(
            chat_id
        ):
            await self._telegram.send_message(
                chat_id=chat_id, text="Команда /cwl_forecast работает в подключённой группе."
            )
            return
        clans = await self._clans.list_active_clans(chat_id)
        if not clans:
            return
        lock = _chat_lock(chat_id)
        if lock.locked():
            await self._telegram.send_message(chat_id=chat_id, text="Прогноз ЛВК уже формируется")
            return
        async with lock:
            retry_after = await self._retry_after(chat_id)
            if retry_after > 0:
                await self._telegram.send_message(
                    chat_id=chat_id,
                    text=(
                        f"Прогноз ЛВК недавно запускался. Повторить можно через {retry_after} сек."
                    ),
                )
                return
            await self._repository.set_last_started_at(chat_id=chat_id, started_at=utc_now())
            if self._clash_client is not None:
                await self._run(chat_id=chat_id, clash=self._clash_client)
                return
            timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS, connect=10.0)
            async with httpx.AsyncClient(timeout=timeout) as http_client:
                await self._run(
                    chat_id=chat_id,
                    clash=ClashClient(self._config.coc_api_token, http_client),
                )

    async def _run(self, *, chat_id: int, clash: ForecastClashClient) -> None:
        clans = await self._clans.list_active_clans(chat_id)
        service = CwlForecastService(
            clash=clash,
            repository=self._repository,
            catalog=self._catalog,
            war_cache={},
            war_concurrency_limit=self._config.cwl_war_concurrency_limit,
        )
        ready: list[ForecastReady] = []
        schedule_required: list[ForecastScheduleRequired] = []
        failures: list[str] = []
        for clan in clans:
            try:
                result = await service.forecast_clan(clan)
            except Exception:
                logger.exception("CWL forecast failed, chat=%s clan=%s", chat_id, clan.clan_tag)
                failures.append(clan.clan_name)
                continue
            if isinstance(result, ForecastReady):
                ready.append(result)
            elif isinstance(result, ForecastScheduleRequired):
                schedule_required.append(result)
            elif not isinstance(result, ForecastInactive):
                failures.append(clan.clan_name)

        for result in ready:
            try:
                await self._telegram.send_message(
                    chat_id=chat_id,
                    text=result.message.text,
                    entities=result.message.entities,
                )
            except TelegramBadRequestError:
                try:
                    await self._telegram.send_message(
                        chat_id=chat_id,
                        text=result.message.fallback_text,
                    )
                except TelegramApiError:
                    logger.warning(
                        "plain CWL forecast delivery failed, chat=%s clan=%s",
                        chat_id,
                        result.clan_name,
                    )
            except TelegramApiError:
                logger.warning(
                    "custom CWL forecast delivery failed, chat=%s clan=%s",
                    chat_id,
                    result.clan_name,
                )
        for result in schedule_required:
            if "не заполнено" in result.reason:
                text = (
                    f"Для клана {result.clan_name} не заполнено расписание будущих раундов ЛВК. "
                    "Администратор группы должен выполнить команду /cwl_forecast_schedule."
                )
            else:
                text = (
                    f"Для клана {result.clan_name} сохранённое расписание ЛВК конфликтует "
                    "с актуальными данными. Администратор группы должен снова выполнить "
                    "команду /cwl_forecast_schedule."
                )
            try:
                await self._telegram.send_message(chat_id=chat_id, text=text)
            except TelegramApiError:
                logger.warning("failed to deliver CWL schedule instruction, chat=%s", chat_id)
        if failures:
            unique_names = list(dict.fromkeys(failures))
            await self._telegram.send_message(
                chat_id=chat_id,
                text="Не удалось сформировать прогноз ЛВК для кланов: " + ", ".join(unique_names),
            )

    async def _retry_after(self, chat_id: int) -> int:
        if self._config.dev_mode:
            return 0
        last_started = await self._repository.get_last_started_at(chat_id)
        if last_started is None:
            return 0
        if last_started.tzinfo is None:
            last_started = last_started.replace(tzinfo=UTC)
        elapsed = (utc_now() - last_started.astimezone(UTC)).total_seconds()
        remaining = self._config.cwl_forecast_cooldown_seconds - elapsed
        return max(math.ceil(remaining), 0)


def _chat_lock(chat_id: int) -> asyncio.Lock:
    lock = FORECAST_CHAT_LOCKS.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        FORECAST_CHAT_LOCKS[chat_id] = lock
    return lock
