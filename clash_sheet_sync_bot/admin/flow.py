"""Role-gated support setup and bot-wide broadcast scenarios."""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Final

import aiosqlite

from clash_sheet_sync_bot.admin.broadcasts import (
    audience_pages,
    delivery_error,
    delivery_report_pages,
)
from clash_sheet_sync_bot.admin.keyboards import (
    CALLBACK_ADMIN_MENU,
    CALLBACK_BROADCAST_CANCEL_PREFIX,
    CALLBACK_BROADCAST_CONFIRM_PREFIX,
    CALLBACK_BROADCAST_RECIPIENTS,
    CALLBACK_BROADCAST_REPORT,
    CALLBACK_BROADCAST_START,
    CALLBACK_SUPPORT_CONNECT,
    admin_menu_keyboard,
    broadcast_confirmation_keyboard,
)
from clash_sheet_sync_bot.common.time import format_dt, utc_now
from clash_sheet_sync_bot.models import AppConfig
from clash_sheet_sync_bot.repositories import BotUserRepository, SuperadminRepository
from clash_sheet_sync_bot.repositories.superadmin import BroadcastTarget
from clash_sheet_sync_bot.setup.flow import TelegramChatInfo
from clash_sheet_sync_bot.setup.keyboards import CALLBACK_PRIVATE_START, main_private_keyboard
from clash_sheet_sync_bot.storage import transaction
from clash_sheet_sync_bot.telegram.access import TelegramAccessService
from clash_sheet_sync_bot.telegram.client import (
    JsonObject,
    TelegramApiError,
    TelegramClient,
    TelegramMessageEntity,
    TelegramMessageNotModifiedError,
)
from clash_sheet_sync_bot.telegram.text import (
    TelegramEntityError,
    decode_custom_emoji_entities,
    encode_custom_emoji_entities,
)

PENDING_BROADCAST_TEXT: Final = "awaiting_broadcast_text"
BROADCAST_TEXT_MAX_LENGTH: Final = 4096
BROADCAST_BATCH_SIZE: Final = 20
BROADCAST_BATCH_PAUSE_SECONDS: Final = 1.0
logger = logging.getLogger(__name__)


class SuperadminFlow:
    """Handles privileged operations and role-aware private menus."""

    def __init__(
        self,
        *,
        config: AppConfig,
        telegram: TelegramClient,
        connection: aiosqlite.Connection,
        access: TelegramAccessService,
    ) -> None:
        self._config = config
        self._telegram = telegram
        self._connection = connection
        self._access = access
        self._users = BotUserRepository(connection)
        self._admin = SuperadminRepository(connection)

    async def observe_private_user(
        self,
        *,
        user_id: int,
        private_chat_id: int,
        display_name: str | None = None,
        username: str | None = None,
    ) -> None:
        """Adds a private user to the future broadcast audience."""

        now = format_dt(utc_now())
        async with transaction(self._connection):
            await self._users.observe_private_user(
                user_id=user_id,
                private_chat_id=private_chat_id,
                now=now,
                display_name=display_name,
                username=username,
            )

    async def send_private_start(self, *, chat_id: int, user_id: int) -> None:
        """Sends a role-aware main menu."""

        await self._telegram.send_message(
            chat_id=chat_id,
            text="Выберите действие.",
            reply_markup=await self._main_keyboard(user_id),
        )

    async def handle_private_text(
        self,
        *,
        chat_id: int,
        user_id: int,
        text: str,
        entities: tuple[TelegramMessageEntity, ...] = (),
    ) -> bool:
        """Consumes text only when the superadmin is composing a broadcast."""

        if not self._is_superadmin(user_id, chat_id):
            return False
        if await self._users.get_pending_action(user_id) != PENDING_BROADCAST_TEXT:
            return False

        broadcast_text = text
        if not broadcast_text.strip():
            await self._telegram.send_message(
                chat_id=chat_id, text="Сообщение не может быть пустым."
            )
            return True
        if len(broadcast_text) > BROADCAST_TEXT_MAX_LENGTH:
            await self._telegram.send_message(
                chat_id=chat_id,
                text=f"Сообщение длиннее лимита Telegram ({BROADCAST_TEXT_MAX_LENGTH} символов).",
            )
            return True

        targets = await self._broadcast_targets()
        now = format_dt(utc_now())
        async with transaction(self._connection):
            broadcast_id = await self._admin.create_broadcast(
                created_by_user_id=user_id,
                text=broadcast_text,
                entities_json=encode_custom_emoji_entities(entities, text=broadcast_text),
                created_at=now,
            )
            await self._admin.snapshot_broadcast_targets(broadcast_id, targets)
            await self._users.set_pending_action(user_id=user_id, action=None, now=now)

        for page in audience_pages(targets, preview=True):
            await self._telegram.send_message(chat_id=chat_id, text=page)
        await self._telegram.send_message(
            chat_id=chat_id,
            text=broadcast_text,
            reply_markup=broadcast_confirmation_keyboard(broadcast_id),
            entities=entities or None,
        )
        return True

    async def cancel_pending_action(self, *, chat_id: int, user_id: int) -> bool:
        """Cancels the superadmin input state, if one is active."""

        if not self._is_superadmin(user_id, chat_id):
            return False
        if await self._users.get_pending_action(user_id) is None:
            return False
        now = format_dt(utc_now())
        async with transaction(self._connection):
            await self._users.set_pending_action(user_id=user_id, action=None, now=now)
        await self._telegram.send_message(chat_id=chat_id, text="Админское действие отменено.")
        return True

    async def connect_support_group(
        self,
        *,
        chat: TelegramChatInfo,
        user_id: int,
        raw_token: str | None,
    ) -> None:
        """Connects or replaces the support group using a one-time token."""

        if chat.type not in {"group", "supergroup"}:
            await self._telegram.send_message(
                chat_id=chat.chat_id,
                text="Команда /connect_support работает только в Telegram-группе.",
            )
            return
        if user_id != self._config.superadmin_user_id:
            await self._telegram.send_message(chat_id=chat.chat_id, text="Нет доступа.")
            return

        token_value = (raw_token or "").strip()
        token = await self._admin.get_support_token(token_value)
        token_error = _validate_support_token(token, token_value, user_id)
        if token_error is not None:
            await self._telegram.send_message(chat_id=chat.chat_id, text=token_error)
            return

        admin_result = await self._access.is_admin(
            chat_id=chat.chat_id,
            user_id=user_id,
            force_refresh=True,
        )
        if not admin_result.is_admin:
            await self._telegram.send_message(
                chat_id=chat.chat_id,
                text="Подключить техподдержку может только администратор этой группы.",
            )
            return

        if chat.username is not None:
            support_url = f"https://t.me/{chat.username}"
        else:
            try:
                invite = await self._telegram.create_chat_invite_link(
                    chat.chat_id,
                    name="Техподдержка бота",
                )
            except TelegramApiError:
                await self._telegram.send_message(
                    chat_id=chat.chat_id,
                    text=(
                        "Не удалось создать ссылку-приглашение. "
                        "Назначьте бота администратором с правом приглашать пользователей и повторите."
                    ),
                )
                return
            support_url = invite.invite_link

        now = format_dt(utc_now())
        async with transaction(self._connection):
            consumed = await self._admin.consume_support_token(
                token=token_value,
                used_chat_id=chat.chat_id,
                used_at=now,
            )
            if consumed:
                await self._admin.set_support_group(
                    chat_id=chat.chat_id,
                    title=chat.title,
                    url=support_url,
                    updated_by_user_id=user_id,
                    updated_at=now,
                )

        if not consumed:
            await self._telegram.send_message(
                chat_id=chat.chat_id,
                text="Токен уже использован. Создайте новый в админском меню.",
            )
            return

        await self._telegram.send_message(
            chat_id=chat.chat_id,
            text="Эта группа подключена как группа техподдержки.",
        )
        with suppress(TelegramApiError):
            await self._telegram.send_message(
                chat_id=user_id,
                text=f"Техподдержка подключена: {chat.title}.",
                reply_markup=admin_menu_keyboard(),
            )

    async def handle_callback(
        self,
        *,
        callback_data: str,
        callback_query_id: str,
        chat_id: int,
        message_id: int,
        user_id: int,
    ) -> bool:
        """Handles admin callbacks and the dynamic main-menu back button."""

        if callback_data == CALLBACK_PRIVATE_START:
            await self._telegram.answer_callback_query(callback_query_id, "Принято.")
            await _edit_or_ignore_not_modified(
                telegram=self._telegram,
                chat_id=chat_id,
                message_id=message_id,
                text="Выберите действие.",
                reply_markup=await self._main_keyboard(user_id),
            )
            return True
        if not callback_data.startswith("admin:"):
            return False
        if not self._is_superadmin(user_id, chat_id):
            await self._telegram.answer_callback_query(
                callback_query_id,
                "Нет доступа.",
                show_alert=True,
            )
            return True

        if callback_data == CALLBACK_ADMIN_MENU:
            await self._telegram.answer_callback_query(callback_query_id, "Принято.")
            await self._show_admin_menu(chat_id=chat_id, message_id=message_id)
            return True
        if callback_data == CALLBACK_SUPPORT_CONNECT:
            await self._telegram.answer_callback_query(callback_query_id, "Токен создан.")
            await self._create_support_token(chat_id=chat_id, user_id=user_id)
            return True
        if callback_data == CALLBACK_BROADCAST_START:
            await self._telegram.answer_callback_query(callback_query_id, "Принято.")
            await self._start_broadcast(chat_id=chat_id, user_id=user_id)
            return True
        if callback_data == CALLBACK_BROADCAST_RECIPIENTS:
            await self._telegram.answer_callback_query(callback_query_id, "Получаю список.")
            await self._send_admin_pages(chat_id, audience_pages(await self._broadcast_targets()))
            return True
        if callback_data == CALLBACK_BROADCAST_REPORT:
            await self._telegram.answer_callback_query(callback_query_id, "Принято.")
            report = await self._admin.get_latest_broadcast_report(user_id)
            if report is None:
                await self._telegram.send_message(
                    chat_id=chat_id,
                    text="Рассылок ещё не было.",
                    reply_markup=admin_menu_keyboard(),
                )
            else:
                await self._show_broadcast_report(chat_id=chat_id, report=report)
            return True
        if callback_data.startswith(CALLBACK_BROADCAST_CONFIRM_PREFIX):
            await self._telegram.answer_callback_query(callback_query_id, "Принято.")
            broadcast_id = _positive_callback_id(
                callback_data,
                CALLBACK_BROADCAST_CONFIRM_PREFIX,
            )
            if broadcast_id is None:
                await self._telegram.send_message(chat_id=chat_id, text="Некорректная рассылка.")
            else:
                await self._send_broadcast(
                    chat_id=chat_id,
                    user_id=user_id,
                    broadcast_id=broadcast_id,
                )
            return True
        if callback_data.startswith(CALLBACK_BROADCAST_CANCEL_PREFIX):
            await self._telegram.answer_callback_query(callback_query_id, "Отменено.")
            broadcast_id = _positive_callback_id(
                callback_data,
                CALLBACK_BROADCAST_CANCEL_PREFIX,
            )
            if broadcast_id is not None:
                async with transaction(self._connection):
                    await self._admin.cancel_broadcast(
                        broadcast_id=broadcast_id,
                        created_by_user_id=user_id,
                    )
            await self._show_admin_menu(chat_id=chat_id, message_id=message_id)
            return True

        await self._telegram.answer_callback_query(
            callback_query_id,
            "Неизвестная админская кнопка.",
            show_alert=True,
        )
        return True

    async def _main_keyboard(self, user_id: int) -> JsonObject:
        support = await self._admin.get_support_group()
        return main_private_keyboard(
            support_url=support.url if support is not None else None,
            is_superadmin=user_id == self._config.superadmin_user_id,
        )

    async def _show_admin_menu(self, *, chat_id: int, message_id: int) -> None:
        support = await self._admin.get_support_group()
        support_text = support.title if support is not None else "не подключена"
        await _edit_or_ignore_not_modified(
            telegram=self._telegram,
            chat_id=chat_id,
            message_id=message_id,
            text=f"Администрирование.\nТехподдержка: {support_text}.",
            reply_markup=admin_menu_keyboard(),
        )

    async def _create_support_token(self, *, chat_id: int, user_id: int) -> None:
        now = utc_now()
        token = secrets.token_urlsafe(18)
        async with transaction(self._connection):
            await self._admin.create_support_token(
                token=token,
                created_by_user_id=user_id,
                expires_at=format_dt(now + timedelta(seconds=self._config.setup_token_ttl_seconds)),
                created_at=format_dt(now),
            )
        command = f"/connect_support {token}"
        await self._telegram.send_message(
            chat_id=chat_id,
            text=(
                "1. Добавьте бота в группу техподдержки.\n"
                "2. Для закрытой группы назначьте бота администратором с правом приглашать.\n"
                "3. Отправьте в группе команду:\n\n"
                f"<code>{escape(command)}</code>"
            ),
            parse_mode="HTML",
        )

    async def _start_broadcast(self, *, chat_id: int, user_id: int) -> None:
        now = format_dt(utc_now())
        async with transaction(self._connection):
            await self._users.set_pending_action(
                user_id=user_id,
                action=PENDING_BROADCAST_TEXT,
                now=now,
            )
        await self._telegram.send_message(
            chat_id=chat_id,
            text=(
                "Отправьте текст рассылки одним сообщением. "
                "Перед отправкой бот покажет предпросмотр и запросит подтверждение. "
                "Для отмены используйте /cancel."
            ),
        )

    async def _send_broadcast(
        self,
        *,
        chat_id: int,
        user_id: int,
        broadcast_id: int,
    ) -> None:
        broadcast = await self._admin.get_broadcast(broadcast_id)
        if (
            broadcast is None
            or broadcast.created_by_user_id != user_id
            or broadcast.status != "draft"
        ):
            await self._telegram.send_message(
                chat_id=chat_id,
                text="Рассылка уже обработана или недоступна.",
            )
            return

        try:
            entities = decode_custom_emoji_entities(
                broadcast.entities_json,
                text=broadcast.text,
            )
        except TelegramEntityError:
            await self._telegram.send_message(
                chat_id=chat_id,
                text="Черновик рассылки повреждён. Создайте рассылку заново.",
            )
            return

        # Старые черновики не содержат snapshot аудитории. Новые всегда
        # отправляются только адресатам, показанным в предпросмотре.
        legacy_targets = (
            await self._broadcast_targets() if broadcast.audience_snapshot_at is None else ()
        )
        started_at = format_dt(utc_now())
        async with transaction(self._connection):
            claimed = await self._admin.claim_broadcast(
                broadcast_id=broadcast_id,
                created_by_user_id=user_id,
                started_at=started_at,
            )
            if claimed and broadcast.audience_snapshot_at is None:
                await self._admin.snapshot_broadcast_targets(broadcast_id, legacy_targets)
        if not claimed:
            await self._telegram.send_message(chat_id=chat_id, text="Рассылка уже запущена.")
            return

        deliveries = await self._admin.list_broadcast_deliveries(broadcast_id)
        targets = tuple(d.target for d in deliveries)
        delivered_count = 0
        failed_count = 0
        for index, target in enumerate(targets):
            attempted_at = format_dt(utc_now())
            async with transaction(self._connection):
                await self._admin.record_broadcast_delivery(
                    broadcast_id=broadcast_id,
                    chat_id=target.chat_id,
                    status="sending",
                    attempted_at=attempted_at,
                )
            error_message = None
            telegram_message_id = None
            try:
                telegram_message_id = await self._telegram.send_message(
                    chat_id=target.chat_id,
                    text=broadcast.text,
                    entities=entities or None,
                )
            except TelegramApiError as error:
                error_message = delivery_error(error, self._config.telegram_bot_token)
                logger.warning(
                    "broadcast delivery failed, broadcast=%s chat=%s: %s",
                    broadcast_id,
                    target.chat_id,
                    error_message,
                )
                failed_count += 1
            else:
                delivered_count += 1
            async with transaction(self._connection):
                await self._admin.record_broadcast_delivery(
                    broadcast_id=broadcast_id,
                    chat_id=target.chat_id,
                    status="failed" if error_message is not None else "delivered",
                    attempted_at=attempted_at,
                    error_message=error_message,
                    telegram_message_id=telegram_message_id,
                )
            if (index + 1) % BROADCAST_BATCH_SIZE == 0 and index + 1 < len(targets):
                await asyncio.sleep(BROADCAST_BATCH_PAUSE_SECONDS)

        finished_at = format_dt(utc_now())
        async with transaction(self._connection):
            await self._admin.finish_broadcast(
                broadcast_id=broadcast_id,
                finished_at=finished_at,
                user_targets_count=sum(t.target_type == "private" for t in targets),
                group_targets_count=sum(t.target_type == "group" for t in targets),
                delivered_count=delivered_count,
                failed_count=failed_count,
            )

        report = await self._admin.get_broadcast_report(broadcast_id, user_id)
        # Недоступность личного чата администратора не отменяет сохранённые
        # результаты. Их можно повторно открыть через «Последняя рассылка».
        try:
            assert report is not None
            await self._show_broadcast_report(chat_id=chat_id, report=report)
        except TelegramApiError as error:
            logger.warning(
                "broadcast report delivery failed, broadcast=%s: %s",
                broadcast_id,
                delivery_error(error, self._config.telegram_bot_token),
            )

    async def _broadcast_targets(self) -> tuple[BroadcastTarget, ...]:
        targets = await self._admin.list_broadcast_targets()
        resolved = []
        private_identities = []
        for target in targets:
            try:
                identity = await self._telegram.get_chat(target.chat_id)
            except TelegramApiError:
                resolved.append(target)
            else:
                resolved.append(
                    replace(target, display_name=identity.display_name, username=identity.username)
                )
                if target.target_type == "private":
                    private_identities.append(identity)
        if private_identities:
            async with transaction(self._connection):
                for identity in private_identities:
                    await self._users.cache_chat_identity(
                        chat_id=identity.id,
                        display_name=identity.display_name,
                        username=identity.username,
                    )
        return tuple(resolved)

    async def _show_broadcast_report(self, *, chat_id: int, report: aiosqlite.Row) -> None:
        pages = delivery_report_pages(
            broadcast_id=report["id"],
            status=report["status"],
            deliveries=await self._admin.list_broadcast_deliveries(report["id"]),
            delivered_count=report["delivered_count"],
            failed_count=report["failed_count"],
            has_audience_snapshot=report["audience_snapshot_at"] is not None,
        )
        await self._send_admin_pages(chat_id, pages)

    async def _send_admin_pages(self, chat_id: int, pages: tuple[str, ...]) -> None:
        for index, page in enumerate(pages):
            await self._telegram.send_message(
                chat_id=chat_id,
                text=page,
                reply_markup=admin_menu_keyboard() if index == len(pages) - 1 else None,
            )

    def _is_superadmin(self, user_id: int, chat_id: int) -> bool:
        return user_id == self._config.superadmin_user_id and chat_id == user_id


def _validate_support_token(token, raw_token: str, user_id: int) -> str | None:
    if raw_token == "" or token is None or token.token != raw_token:
        return "Токен техподдержки не найден. Создайте новый в админском меню."
    if token.created_by_user_id != user_id:
        return "Этот токен создан другим пользователем."
    if token.used_at is not None:
        return "Токен техподдержки уже использован."
    try:
        expires_at = datetime.fromisoformat(token.expires_at)
    except ValueError:
        return "Токен техподдержки повреждён. Создайте новый."
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= utc_now():
        return "Токен техподдержки истёк. Создайте новый."
    return None


def _positive_callback_id(callback_data: str, prefix: str) -> int | None:
    raw_value = callback_data.removeprefix(prefix)
    try:
        value = int(raw_value)
    except ValueError:
        return None
    return value if value > 0 else None


async def _edit_or_ignore_not_modified(
    *,
    telegram: TelegramClient,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: JsonObject,
) -> None:
    with suppress(TelegramMessageNotModifiedError):
        await telegram.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
        )
