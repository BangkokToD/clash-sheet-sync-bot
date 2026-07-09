"""Проверка прав Telegram-администратора."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite

from clash_sheet_sync_bot.common.time import utc_now_iso as _utc_now_iso
from clash_sheet_sync_bot.repositories import TelegramChatRepository
from clash_sheet_sync_bot.telegram.client import TelegramApiError, TelegramClient

ADMIN_STATUSES = frozenset({"creator", "administrator"})


@dataclass(frozen=True, slots=True)
class AdminCheckResult:
    """Результат проверки Telegram-админа.

    Attributes:
        is_admin: Является ли пользователь администратором группы.
        from_cache: Использован ли положительный кэш.
    """

    is_admin: bool
    from_cache: bool = False


class TelegramAccessService:
    """Сервис проверки Telegram-админов.

    Args:
        telegram: Клиент Telegram Bot API.
        connection: SQLite-подключение.
        admin_cache_ttl_seconds: TTL положительного кэша для обычных меню.
    """

    def __init__(
        self,
        *,
        telegram: TelegramClient,
        connection: aiosqlite.Connection,
        admin_cache_ttl_seconds: int,
    ) -> None:
        self._telegram = telegram
        self._connection = connection
        self._repository = TelegramChatRepository(connection)
        self._admin_cache_ttl_seconds = admin_cache_ttl_seconds

    async def is_admin(
        self,
        *,
        chat_id: int,
        user_id: int,
        force_refresh: bool = False,
    ) -> AdminCheckResult:
        """Проверяет, является ли пользователь админом Telegram-группы.

        Для чувствительных действий нужно вызывать с `force_refresh=True`.
        Для обычного открытия меню допустим положительный кэш из
        `chat_admin_links.last_admin_check_at`.

        Args:
            chat_id: ID Telegram-группы.
            user_id: Telegram user ID.
            force_refresh: Игнорировать ли положительный кэш.

        Returns:
            Результат проверки.
        """

        if not force_refresh and await self._has_fresh_positive_cache(chat_id, user_id):
            return AdminCheckResult(is_admin=True, from_cache=True)

        try:
            member = await self._telegram.get_chat_member(chat_id=chat_id, user_id=user_id)
        except TelegramApiError:
            return AdminCheckResult(is_admin=False, from_cache=False)

        is_admin = member.status in ADMIN_STATUSES
        if is_admin:
            await self._repository.update_admin_check_at(
                chat_id=chat_id,
                user_id=user_id,
                checked_at=_utc_now_iso(),
            )
            await self._connection.commit()
        return AdminCheckResult(is_admin=is_admin, from_cache=False)

    async def _has_fresh_positive_cache(self, chat_id: int, user_id: int) -> bool:
        """Проверяет положительный кэш Telegram-админа.

        Args:
            chat_id: ID Telegram-группы.
            user_id: Telegram user ID.

        Returns:
            `True`, если пользователь недавно проверялся как админ.
        """

        if self._admin_cache_ttl_seconds <= 0:
            return False

        raw_checked_at = await self._repository.get_admin_check_at(
            chat_id=chat_id,
            user_id=user_id,
        )
        if raw_checked_at is None:
            return False

        try:
            checked_at = datetime.fromisoformat(raw_checked_at)
        except ValueError:
            return False

        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=UTC)
        age_seconds = (datetime.now(UTC) - checked_at).total_seconds()
        return 0 <= age_seconds <= self._admin_cache_ttl_seconds
