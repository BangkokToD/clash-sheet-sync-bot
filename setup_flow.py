"""Сценарии подключения группы и навигации настроек."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Final

import aiosqlite
import httpx

from coc_client import (
    ClashApiUnavailableError,
    ClashClanNotFoundError,
    ClashClient,
)
from column_profiles import new_user_column_key, normalize_column_title, table_title
from models import AppConfig, SetupToken, TableType, normalize_tag
from repositories import (
    AdminChatRepository,
    ChatLifecycleRepository,
    ClanSettingsRepository,
    ColumnProfileRepository,
    RuntimeConfigRepository,
    SetupTokenRepository,
    SheetBindingRepository,
    SheetBlockRepository,
    TelegramChatRepository,
    TransferTokenRepository,
)
from sheet_admin import (
    SheetAdminError,
    SheetAdminService,
    TableDiagnosticIssue,
    TableDiagnosticResult,
    extract_spreadsheet_id,
)
from sheets_client import (
    GoogleAccessTokenProvider,
    GoogleSheetsAuthError,
    SheetsClient,
)
from storage import transaction
from telegram_access import TelegramAccessService
from telegram_client import (
    JsonObject,
    TelegramApiError,
    TelegramClient,
    TelegramMessageNotModifiedError,
)

CALLBACK_CONNECT_GROUP: Final = "setup:create_token"
CALLBACK_PRIVATE_START: Final = "setup:start"
CALLBACK_MY_GROUPS: Final = "setup:my_groups"
CALLBACK_HELP: Final = "setup:help"
CALLBACK_SETTINGS_PREFIX: Final = "settings:open:"
CALLBACK_SETTINGS_SECTION_PREFIX: Final = "settings:section:"
CALLBACK_BIND_SHEET_PREFIX: Final = "sheet:bind:"
CALLBACK_CHANGE_SHEET_PREFIX: Final = "sheet:change:"
CALLBACK_UNLINK_SHEET_PREFIX: Final = "sheet:unlink:"
CALLBACK_CONFIRM_UNLINK_SHEET_PREFIX: Final = "sheet:unlink_confirm:"
CALLBACK_DIAGNOSE_SHEET_PREFIX: Final = "sheet:diagnose:"
CALLBACK_FIX_SHEET_PREFIX: Final = "sheet:fix:"
CALLBACK_CREATE_TRANSFER_PREFIX: Final = "transfer:create:"
CALLBACK_CHECK_SHEET_PREFIX: Final = "sheet:check:"
CALLBACK_CLAN_ADD_PREFIX: Final = "clans:add:"
CALLBACK_CLAN_CONFIRM_PREFIX: Final = "clans:confirm:"
CALLBACK_CLAN_REMOVE_PREFIX: Final = "clans:remove:"
CALLBACK_CLAN_MOVE_UP_PREFIX: Final = "clans:up:"
CALLBACK_CLAN_MOVE_DOWN_PREFIX: Final = "clans:down:"
CALLBACK_COLUMN_ADD_PREFIX: Final = "columns:add:"
CALLBACK_COLUMN_TOGGLE_PREFIX: Final = "columns:toggle:"
CALLBACK_COLUMN_RENAME_PREFIX: Final = "columns:rename:"
CALLBACK_COLUMN_DELETE_PREFIX: Final = "columns:delete:"
CALLBACK_COLUMN_MOVE_UP_PREFIX: Final = "columns:up:"
CALLBACK_COLUMN_MOVE_DOWN_PREFIX: Final = "columns:down:"
CALLBACK_COLUMN_RESTORE_PREFIX: Final = "columns:restore:"
AWAITING_SHEET_LINK_STATE_PREFIX: Final = "awaiting_sheet_link:"
AWAITING_SHEET_ACCESS_STATE_PREFIX: Final = "awaiting_sheet_access:"
AWAITING_CLAN_TAG_STATE_PREFIX: Final = "awaiting_clan_tag:"
AWAITING_USER_COLUMN_TITLE_STATE_PREFIX: Final = "awaiting_user_column_title:"
AWAITING_COLUMN_RENAME_STATE_PREFIX: Final = "awaiting_column_rename:"
COLUMN_SECTION_TABLE_TYPES: Final[dict[str, TableType]] = {
    "composition_active_columns": "composition_active",
    "composition_exited_columns": "composition_exited",
    "cwl_columns": "cwl",
}
SETTINGS_SECTIONS: Final = {
    "table": "Таблица",
    "clans": "Кланы",
    "composition_active_columns": "Колонки активного состава",
    "composition_exited_columns": "Колонки вышедших",
    "cwl_columns": "Колонки CWL",
}


@dataclass(frozen=True, slots=True)
class TelegramChatInfo:
    """Короткая информация о Telegram-чате из update.

    Attributes:
        chat_id: ID Telegram-чата.
        title: Название чата или fallback.
        type: Тип Telegram-чата.
    """

    chat_id: int
    title: str
    type: str


class SetupFlow:
    """Сценарии публичного setup-flow.

    Args:
        config: Глобальная конфигурация приложения.
        telegram: Клиент Telegram Bot API.
        connection: SQLite-подключение.
        access: Сервис проверки Telegram-админов.
        bot_username: Username бота без `@` или `None`.
    """

    def __init__(
        self,
        *,
        config: AppConfig,
        telegram: TelegramClient,
        connection: aiosqlite.Connection,
        access: TelegramAccessService,
        bot_username: str | None,
    ) -> None:
        self._config = config
        self._telegram = telegram
        self._connection = connection
        self._access = access
        self._bot_username = bot_username
        self._setup_tokens = SetupTokenRepository(connection)
        self._telegram_chats = TelegramChatRepository(connection)
        self._admin_chats = AdminChatRepository(connection)
        self._sheet_bindings = SheetBindingRepository(connection)
        self._blocks = SheetBlockRepository(connection)
        self._transfers = TransferTokenRepository(connection)
        self._lifecycle = ChatLifecycleRepository(connection)
        self._clans = ClanSettingsRepository(connection)
        self._columns = ColumnProfileRepository(connection)
        self._runtime = RuntimeConfigRepository(connection)

    async def send_private_start(self, chat_id: int) -> None:
        """Отправляет главное меню личного чата.

        Args:
            chat_id: ID личного чата.
        """

        await self._telegram.send_message(
            chat_id=chat_id,
            text="Выберите действие.",
            reply_markup=main_private_keyboard(),
        )

    async def send_group_start(self, chat_id: int) -> None:
        """Отправляет короткую инструкцию в группе.

        Args:
            chat_id: ID группы.
        """

        await self._telegram.send_message(
            chat_id=chat_id,
            text=(
                "Бот добавлен в группу. Администратор может подключить её через личный чат с ботом."
            ),
        )

    async def send_help(self, chat_id: int, *, is_private: bool) -> None:
        """Отправляет краткую справку.

        Args:
            chat_id: ID Telegram-чата.
            is_private: Вызвана ли справка в личке.
        """

        if is_private:
            text = (
                "Порядок подключения:\n"
                "1. Нажмите «Подключить группу».\n"
                "2. Добавьте бота в Telegram-группу.\n"
                "3. Отправьте в группе команду /connect <token>.\n"
                "4. Вернитесь в личный чат и откройте /settings."
            )
        else:
            text = (
                "Команда /sync будет работать после настройки группы. "
                "Администратор подключает группу через личный чат с ботом."
            )
        await self._telegram.send_message(chat_id=chat_id, text=text)

    async def cancel_private_setup(self, *, chat_id: int, user_id: int) -> None:
        """Сбрасывает текущую setup-сессию пользователя.

        Args:
            chat_id: ID личного чата.
            user_id: Telegram user ID пользователя, отправившего `/cancel`.
        """

        now = _format_dt(_utc_now())
        async with transaction(self._connection):
            cleared_count = await self._telegram_chats.clear_setup_states_for_user(
                user_id=user_id,
                now=now,
            )

        text = "Текущая настройка сброшена." if cleared_count > 0 else "Активной настройки нет."
        await self._telegram.send_message(chat_id=chat_id, text=text)

    async def handle_private_text(self, *, chat_id: int, user_id: int, text: str) -> None:
        """Обрабатывает текст в личке для текущего setup_state.

        Args:
            chat_id: ID личного чата.
            user_id: Telegram user ID.
            text: Текст сообщения.
        """

        if await self._handle_pending_sheet_link_text(chat_id=chat_id, user_id=user_id, text=text):
            return
        if await self._handle_pending_clan_tag_text(chat_id=chat_id, user_id=user_id, text=text):
            return
        if await self._handle_pending_user_column_title_text(
            chat_id=chat_id,
            user_id=user_id,
            text=text,
        ):
            return
        await self._handle_pending_column_rename_text(chat_id=chat_id, user_id=user_id, text=text)

    async def _handle_pending_sheet_link_text(
        self, *, chat_id: int, user_id: int, text: str
    ) -> bool:
        """Обрабатывает ссылку на таблицу, если она ожидается.

        Args:
            chat_id: ID личного чата.
            user_id: Telegram user ID.
            text: Текст сообщения.

        Returns:
            `True`, если сообщение относилось к этому состоянию.
        """

        pending = await self._telegram_chats.find_pending_sheet_link_setup(
            user_id=user_id,
            state_prefix=AWAITING_SHEET_LINK_STATE_PREFIX,
        )
        if pending is None:
            return False

        try:
            spreadsheet_id = extract_spreadsheet_id(text)
        except SheetAdminError as exc:
            await self._telegram.send_message(chat_id=chat_id, text=str(exc))
            return True

        if await self._runtime.is_google_sheet_bound_elsewhere(
            spreadsheet_id,
            current_chat_id=pending.chat_id,
        ):
            await self._telegram.send_message(
                chat_id=chat_id,
                text=(
                    "Эта таблица уже привязана к другой группе.\n"
                    "Для переноса используйте сценарий переноса таблицы."
                ),
            )
            return True

        now = _format_dt(_utc_now())
        async with transaction(self._connection):
            await self._telegram_chats.set_setup_state(
                chat_id=pending.chat_id,
                setup_state=_sheet_access_state(user_id, spreadsheet_id),
                now=now,
            )

        await self._send_sheet_access_instruction(chat_id=chat_id, group_chat_id=pending.chat_id)
        return True

    async def _handle_pending_clan_tag_text(self, *, chat_id: int, user_id: int, text: str) -> bool:
        """Обрабатывает тег клана, если бот ждёт добавление клана.

        Args:
            chat_id: ID личного чата.
            user_id: Telegram user ID.
            text: Текст сообщения.

        Returns:
            `True`, если сообщение относилось к этому состоянию.
        """

        pending = await self._telegram_chats.find_pending_sheet_link_setup(
            user_id=user_id,
            state_prefix=AWAITING_CLAN_TAG_STATE_PREFIX,
        )
        if pending is None:
            return False

        try:
            clan_tag = normalize_tag(text)
            clan = await self._lookup_clan(clan_tag)
        except ValueError as exc:
            await self._telegram.send_message(chat_id=chat_id, text=str(exc))
            return True
        except ClashClanNotFoundError:
            await self._telegram.send_message(chat_id=chat_id, text="Клан не найден.")
            return True
        except ClashApiUnavailableError as exc:
            await self._telegram.send_message(chat_id=chat_id, text=f"CoC API недоступно: {exc}")
            return True

        await self._telegram.send_message(
            chat_id=chat_id,
            text=f"Клан найден: {clan.name} | {clan.tag}\nДобавить?",
            reply_markup=confirm_clan_keyboard(pending.chat_id, clan.tag),
        )
        return True

    async def _handle_pending_user_column_title_text(
        self,
        *,
        chat_id: int,
        user_id: int,
        text: str,
    ) -> bool:
        """Создаёт user-колонку из ожидаемого текстового названия.

        Args:
            chat_id: ID личного чата.
            user_id: Telegram user ID.
            text: Текст сообщения.

        Returns:
            `True`, если сообщение относилось к этому состоянию.
        """

        pending = await self._telegram_chats.find_pending_sheet_link_setup(
            user_id=user_id,
            state_prefix=AWAITING_USER_COLUMN_TITLE_STATE_PREFIX,
        )
        if pending is None:
            return False

        table_type = _table_type_from_state(
            pending.setup_state,
            prefix=AWAITING_USER_COLUMN_TITLE_STATE_PREFIX,
            user_id=user_id,
        )
        if table_type is None:
            return False

        if not await self._has_sensitive_group_settings_access(
            group_chat_id=pending.chat_id,
            user_id=user_id,
        ):
            await self._telegram.send_message(chat_id=chat_id, text="Нет доступа.")
            return True

        try:
            title = normalize_column_title(text)
        except ValueError as exc:
            await self._telegram.send_message(chat_id=chat_id, text=str(exc))
            return True

        now = _format_dt(_utc_now())
        async with transaction(self._connection):
            await self._columns.ensure_default_profiles(chat_id=pending.chat_id, now=now)
            await self._columns.create_user_column(
                chat_id=pending.chat_id,
                table_type=table_type,
                column_key=new_user_column_key(),
                title=title,
                now=now,
            )
            await self._telegram_chats.set_setup_state(
                chat_id=pending.chat_id,
                setup_state=None,
                now=now,
            )

        await self._send_columns_section_message(
            group_chat_id=pending.chat_id,
            chat_id=chat_id,
            table_type=table_type,
            prefix=f"Колонка «{title}» создана.\n\n",
        )
        return True

    async def _handle_pending_column_rename_text(
        self, *, chat_id: int, user_id: int, text: str
    ) -> bool:
        """Переименовывает колонку из ожидаемого текстового названия.

        Args:
            chat_id: ID личного чата.
            user_id: Telegram user ID.
            text: Текст сообщения.

        Returns:
            `True`, если сообщение относилось к этому состоянию.
        """

        pending = await self._telegram_chats.find_pending_sheet_link_setup(
            user_id=user_id,
            state_prefix=AWAITING_COLUMN_RENAME_STATE_PREFIX,
        )
        if pending is None:
            return False

        parsed = _rename_state_payload(pending.setup_state, user_id)
        if parsed is None:
            return False
        table_type, column_key = parsed

        if not await self._has_sensitive_group_settings_access(
            group_chat_id=pending.chat_id,
            user_id=user_id,
        ):
            await self._telegram.send_message(chat_id=chat_id, text="Нет доступа.")
            return True

        try:
            title = normalize_column_title(text)
        except ValueError as exc:
            await self._telegram.send_message(chat_id=chat_id, text=str(exc))
            return True

        now = _format_dt(_utc_now())
        async with transaction(self._connection):
            updated = await self._columns.rename_column(
                chat_id=pending.chat_id,
                table_type=table_type,
                column_key=column_key,
                title=title,
                now=now,
            )
            await self._telegram_chats.set_setup_state(
                chat_id=pending.chat_id,
                setup_state=None,
                now=now,
            )

        if updated:
            await self._send_columns_section_message(
                group_chat_id=pending.chat_id,
                chat_id=chat_id,
                table_type=table_type,
                prefix="Колонка переименована.\n\n",
            )
        else:
            await self._telegram.send_message(
                chat_id=chat_id,
                text="Колонку нельзя переименовать.",
            )
        return True

    async def create_setup_token(self, *, chat_id: int, user_id: int) -> None:
        """Создаёт setup-токен и отправляет команду `/connect` админу.

        Args:
            chat_id: ID личного чата администратора.
            user_id: Telegram user ID администратора.
        """

        now = _utc_now()
        expires_at = now + timedelta(seconds=self._config.setup_token_ttl_seconds)
        token = secrets.token_urlsafe(18)
        async with transaction(self._connection):
            await self._setup_tokens.create_setup_token(
                token=token,
                created_by_user_id=user_id,
                expires_at=_format_dt(expires_at),
                created_at=_format_dt(now),
            )

        command = f"/connect {token}"
        text = (
            "1. Добавьте бота в нужную Telegram-группу.\n"
            "2. Убедитесь, что вы администратор этой группы.\n"
            "3. Отправьте в группе команду:\n\n"
            f"<code>{escape(command)}</code>\n\n"
            "После этого настройка продолжится здесь, в личном чате."
        )
        await self._telegram.send_message(chat_id=chat_id, text=text, parse_mode="HTML")

    async def connect_group(
        self,
        *,
        chat: TelegramChatInfo,
        user_id: int,
        raw_token: str | None,
    ) -> None:
        """Обрабатывает команду `/connect <token>` в группе.

        Args:
            chat: Данные Telegram-группы.
            user_id: Telegram user ID отправителя команды.
            raw_token: Токен из текста команды или `None`.
        """

        if chat.type not in {"group", "supergroup"}:
            await self._telegram.send_message(
                chat_id=chat.chat_id,
                text="Команда /connect работает только в Telegram-группе.",
            )
            return

        token = (raw_token or "").strip()
        if token == "":
            await self._telegram.send_message(
                chat_id=chat.chat_id,
                text="Используйте команду вида /connect <token>.",
            )
            return

        setup_token = await self._setup_tokens.get_setup_token(token)
        token_error = _validate_setup_token(setup_token, token, user_id)
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
                text="Подключить группу может только Telegram-администратор группы.",
            )
            return

        now = _format_dt(_utc_now())
        token_was_marked = False
        async with transaction(self._connection):
            token_was_marked = await self._setup_tokens.mark_setup_token_used(
                token=token,
                used_chat_id=chat.chat_id,
                used_at=now,
            )
            if not token_was_marked:
                return
            await self._telegram_chats.upsert_connected_chat(
                chat_id=chat.chat_id,
                title=chat.title,
                chat_type=chat.type,
                created_by_user_id=user_id,
                now=now,
            )
            await self._telegram_chats.upsert_admin_link(
                chat_id=chat.chat_id,
                user_id=user_id,
                linked_at=now,
                last_admin_check_at=now,
            )

        if not token_was_marked:
            await self._telegram.send_message(
                chat_id=chat.chat_id,
                text="Токен подключения уже использован. Создайте новый токен в личном чате.",
            )
            return

        group_text = "Группа подключена. Настройка продолжится в личном чате с администратором."
        try:
            await self._telegram.send_message(
                chat_id=user_id,
                text=(
                    f"Группа подключена: {chat.title}.\n\n"
                    "Откройте /settings, чтобы продолжить настройку."
                ),
                reply_markup=known_groups_keyboard(
                    [KnownGroupButton(chat_id=chat.chat_id, title=chat.title)],
                ),
            )
        except TelegramApiError:
            group_text += " Откройте личный чат с ботом и отправьте /settings."

        await self._telegram.send_message(chat_id=chat.chat_id, text=group_text)

    async def accept_transfer(
        self,
        *,
        chat: TelegramChatInfo,
        user_id: int,
        raw_token: str | None,
    ) -> None:
        """Принимает перенос таблицы в новой Telegram-группе."""

        if chat.type not in {"group", "supergroup"}:
            await self._telegram.send_message(
                chat_id=chat.chat_id,
                text="Команда /accept_transfer работает только в Telegram-группе.",
            )
            return

        token_value = (raw_token or "").strip()
        if token_value == "":
            await self._telegram.send_message(
                chat_id=chat.chat_id,
                text="Используйте команду вида /accept_transfer <token>.",
            )
            return

        transfer_token = await self._transfers.get_transfer_token(token_value)
        token_error = _validate_transfer_token(transfer_token, token_value)
        if token_error is not None:
            await self._telegram.send_message(chat_id=chat.chat_id, text=token_error)
            return

        assert transfer_token is not None
        target_admin = await self._access.is_admin(
            chat_id=chat.chat_id,
            user_id=user_id,
            force_refresh=True,
        )
        if not target_admin.is_admin:
            await self._telegram.send_message(
                chat_id=chat.chat_id,
                text="Принять перенос может только Telegram-администратор новой группы.",
            )
            return

        source_admin = await self._access.is_admin(
            chat_id=transfer_token.source_chat_id,
            user_id=user_id,
            force_refresh=True,
        )
        if not source_admin.is_admin:
            await self._telegram.send_message(
                chat_id=chat.chat_id,
                text="Пользователь должен быть администратором старой группы.",
            )
            return

        if await self._sheet_bindings.has_active_binding(chat.chat_id):
            await self._telegram.send_message(
                chat_id=chat.chat_id,
                text="У этой группы уже есть активная таблица.",
            )
            return

        now = _format_dt(_utc_now())
        async with transaction(self._connection):
            token_marked = await self._transfers.mark_transfer_token_used(
                token=token_value,
                used_at=now,
            )
            if not token_marked:
                await self._telegram.send_message(
                    chat_id=chat.chat_id,
                    text="Transfer token уже использован. Создайте новый токен.",
                )
                return
            await self._telegram_chats.upsert_known_chat(
                chat_id=chat.chat_id,
                title=chat.title,
                chat_type=chat.type,
                now=now,
            )
            await self._telegram_chats.upsert_admin_link(
                chat_id=chat.chat_id,
                user_id=user_id,
                linked_at=now,
                last_admin_check_at=now,
            )
            await self._sheet_bindings.transfer_binding_to_chat(
                source_chat_id=transfer_token.source_chat_id,
                target_chat_id=chat.chat_id,
                now=now,
            )
            await self._lifecycle.move_runtime_state(
                source_chat_id=transfer_token.source_chat_id,
                target_chat_id=chat.chat_id,
                now=now,
            )

        await self._telegram.send_message(
            chat_id=chat.chat_id,
            text="Таблица и настройки перенесены в эту группу. Можно запускать /sync.",
        )

    async def send_private_settings(self, chat_id: int, user_id: int) -> None:
        """Показывает список известных групп пользователя в личке.

        Args:
            chat_id: ID личного чата.
            user_id: Telegram user ID.
        """

        known_chats = await self._admin_chats.list_known_chats(user_id)
        if not known_chats:
            await self._telegram.send_message(
                chat_id=chat_id,
                text=(
                    "У вас пока нет подключённых групп. Нажмите «Подключить группу», чтобы начать."
                ),
                reply_markup=main_private_keyboard(),
            )
            return

        buttons = [KnownGroupButton(chat_id=item.chat_id, title=item.title) for item in known_chats]
        await self._telegram.send_message(
            chat_id=chat_id,
            text="Выберите группу для настройки.",
            reply_markup=known_groups_keyboard(buttons),
        )

    async def send_group_settings_pointer(self, *, chat: TelegramChatInfo, user_id: int) -> None:
        """Обрабатывает `/settings` в группе.

        Args:
            chat: Данные Telegram-группы.
            user_id: Telegram user ID отправителя команды.
        """

        admin_result = await self._access.is_admin(
            chat_id=chat.chat_id,
            user_id=user_id,
            force_refresh=False,
        )
        if not admin_result.is_admin:
            await self._telegram.send_message(
                chat_id=chat.chat_id,
                text="Команда /settings доступна только Telegram-администратору группы.",
            )
            return

        now = _format_dt(_utc_now())
        async with transaction(self._connection):
            await self._telegram_chats.upsert_known_chat(
                chat_id=chat.chat_id,
                title=chat.title,
                chat_type=chat.type,
                now=now,
            )
            await self._telegram_chats.upsert_admin_link(
                chat_id=chat.chat_id,
                user_id=user_id,
                linked_at=now,
                last_admin_check_at=now,
            )

        await self._telegram.send_message(
            chat_id=chat.chat_id,
            text="Откройте настройки в личном чате.",
            reply_markup=private_chat_keyboard(self._bot_username),
        )

    async def handle_callback(
        self,
        *,
        callback_data: str,
        callback_query_id: str,
        chat_id: int,
        message_id: int,
        user_id: int,
    ) -> None:
        """Обрабатывает callback query setup/settings меню.

        Args:
            callback_data: Значение callback data.
            callback_query_id: ID callback query.
            chat_id: ID чата сообщения с кнопкой.
            message_id: ID сообщения с кнопкой.
            user_id: Telegram user ID отправителя callback.
        """

        if callback_data == CALLBACK_CONNECT_GROUP:
            await self._telegram.answer_callback_query(callback_query_id, "Принято.")
            await self.create_setup_token(chat_id=chat_id, user_id=user_id)
            return
        if callback_data == CALLBACK_PRIVATE_START:
            await self._telegram.answer_callback_query(callback_query_id, "Принято.")
            await _edit_or_send_message(
                telegram=self._telegram,
                chat_id=chat_id,
                message_id=message_id,
                text="Выберите действие.",
                reply_markup=main_private_keyboard(),
            )
            return
        if callback_data == CALLBACK_MY_GROUPS:
            await self._telegram.answer_callback_query(callback_query_id, "Принято.")
            await self.send_private_settings(chat_id=chat_id, user_id=user_id)
            return
        if callback_data == CALLBACK_HELP:
            await self._telegram.answer_callback_query(callback_query_id, "Принято.")
            await self.send_help(chat_id=chat_id, is_private=True)
            return
        if callback_data == "noop":
            await self._telegram.answer_callback_query(callback_query_id)
            return
        if callback_data.startswith(CALLBACK_SETTINGS_PREFIX):
            await self._show_group_settings_menu(
                callback_data=callback_data,
                callback_query_id=callback_query_id,
                chat_id=chat_id,
                message_id=message_id,
                user_id=user_id,
            )
            return
        if callback_data.startswith(CALLBACK_SETTINGS_SECTION_PREFIX):
            await self._show_placeholder_section(
                callback_data=callback_data,
                callback_query_id=callback_query_id,
                chat_id=chat_id,
                message_id=message_id,
                user_id=user_id,
            )
            return
        if callback_data.startswith(CALLBACK_BIND_SHEET_PREFIX):
            await self._start_sheet_binding(
                callback_data=callback_data,
                callback_query_id=callback_query_id,
                chat_id=chat_id,
                user_id=user_id,
            )
            return
        if callback_data.startswith(CALLBACK_CHANGE_SHEET_PREFIX):
            await self._show_change_sheet_warning(
                callback_data, callback_query_id, chat_id, message_id, user_id
            )
            return
        if callback_data.startswith(CALLBACK_UNLINK_SHEET_PREFIX):
            await self._show_unlink_sheet_warning(
                callback_data, callback_query_id, chat_id, message_id, user_id
            )
            return
        if callback_data.startswith(CALLBACK_CONFIRM_UNLINK_SHEET_PREFIX):
            await self._confirm_unlink_sheet(
                callback_data, callback_query_id, chat_id, message_id, user_id
            )
            return
        if callback_data.startswith(CALLBACK_DIAGNOSE_SHEET_PREFIX):
            await self._diagnose_sheet(
                callback_data, callback_query_id, chat_id, message_id, user_id
            )
            return
        if callback_data.startswith(CALLBACK_FIX_SHEET_PREFIX):
            await self._fix_sheet(callback_data, callback_query_id, chat_id, message_id, user_id)
            return
        if callback_data.startswith(CALLBACK_CREATE_TRANSFER_PREFIX):
            await self._create_transfer_token(callback_data, callback_query_id, chat_id, user_id)
            return
        if callback_data.startswith(CALLBACK_CHECK_SHEET_PREFIX):
            await self._check_sheet_access(
                callback_data=callback_data,
                callback_query_id=callback_query_id,
                chat_id=chat_id,
                message_id=message_id,
                user_id=user_id,
            )
            return
        if callback_data.startswith(CALLBACK_CLAN_ADD_PREFIX):
            await self._start_clan_add(callback_data, callback_query_id, chat_id, user_id)
            return
        if callback_data.startswith(CALLBACK_CLAN_CONFIRM_PREFIX):
            await self._confirm_clan_add(
                callback_data, callback_query_id, chat_id, message_id, user_id
            )
            return
        if callback_data.startswith(CALLBACK_CLAN_REMOVE_PREFIX):
            await self._remove_clan(callback_data, callback_query_id, chat_id, message_id, user_id)
            return
        if callback_data.startswith(CALLBACK_CLAN_MOVE_UP_PREFIX):
            await self._move_clan(
                callback_data, callback_query_id, chat_id, message_id, user_id, "up"
            )
            return
        if callback_data.startswith(CALLBACK_CLAN_MOVE_DOWN_PREFIX):
            await self._move_clan(
                callback_data, callback_query_id, chat_id, message_id, user_id, "down"
            )
            return
        if callback_data.startswith(CALLBACK_COLUMN_ADD_PREFIX):
            await self._start_user_column_add(callback_data, callback_query_id, chat_id, user_id)
            return
        if callback_data.startswith(CALLBACK_COLUMN_TOGGLE_PREFIX):
            await self._toggle_column(
                callback_data, callback_query_id, chat_id, message_id, user_id
            )
            return
        if callback_data.startswith(CALLBACK_COLUMN_RENAME_PREFIX):
            await self._start_column_rename(callback_data, callback_query_id, chat_id, user_id)
            return
        if callback_data.startswith(CALLBACK_COLUMN_DELETE_PREFIX):
            await self._delete_user_column(
                callback_data, callback_query_id, chat_id, message_id, user_id
            )
            return
        if callback_data.startswith(CALLBACK_COLUMN_MOVE_UP_PREFIX):
            await self._move_column(
                callback_data, callback_query_id, chat_id, message_id, user_id, "up"
            )
            return
        if callback_data.startswith(CALLBACK_COLUMN_MOVE_DOWN_PREFIX):
            await self._move_column(
                callback_data, callback_query_id, chat_id, message_id, user_id, "down"
            )
            return
        if callback_data.startswith(CALLBACK_COLUMN_RESTORE_PREFIX):
            await self._restore_columns(
                callback_data, callback_query_id, chat_id, message_id, user_id
            )
            return

        await self._telegram.answer_callback_query(
            callback_query_id,
            "Неизвестная кнопка.",
            show_alert=True,
        )

    async def _show_group_settings_menu(
        self,
        *,
        callback_data: str,
        callback_query_id: str,
        chat_id: int,
        message_id: int,
        user_id: int,
    ) -> None:
        """Показывает skeleton-меню настроек конкретной группы.

        Args:
            callback_data: Callback data с ID группы.
            callback_query_id: ID callback query.
            chat_id: ID личного чата.
            message_id: ID сообщения для редактирования.
            user_id: Telegram user ID отправителя callback.
        """

        group_chat_id = await self._parse_callback_group_id(
            callback_data=callback_data,
            callback_query_id=callback_query_id,
            prefix=CALLBACK_SETTINGS_PREFIX,
        )
        if group_chat_id is None:
            return

        has_access = await self._telegram_chats.has_active_admin_link(
            chat_id=group_chat_id,
            user_id=user_id,
        )
        if not has_access:
            await self._telegram.answer_callback_query(
                callback_query_id,
                "Нет доступа к настройкам этой группы.",
                show_alert=True,
            )
            return

        await self._telegram.answer_callback_query(callback_query_id, "Принято.")
        text = "Меню настроек группы. Выберите раздел."
        await _edit_or_send_message(
            telegram=self._telegram,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=settings_menu_keyboard(group_chat_id),
        )

    async def _show_placeholder_section(
        self,
        *,
        callback_data: str,
        callback_query_id: str,
        chat_id: int,
        message_id: int,
        user_id: int,
    ) -> None:
        """Показывает временный placeholder раздела настроек.

        Args:
            callback_data: Callback data с ID группы и ключом раздела.
            callback_query_id: ID callback query.
            chat_id: ID личного чата.
            message_id: ID сообщения для редактирования.
            user_id: Telegram user ID отправителя callback.
        """

        payload = callback_data.removeprefix(CALLBACK_SETTINGS_SECTION_PREFIX)
        raw_chat_id, _, section_key = payload.partition(":")
        try:
            group_chat_id = int(raw_chat_id)
        except ValueError:
            await self._telegram.answer_callback_query(
                callback_query_id,
                "Некорректная группа.",
                show_alert=True,
            )
            return

        has_access = await self._telegram_chats.has_active_admin_link(
            chat_id=group_chat_id,
            user_id=user_id,
        )
        if not has_access:
            await self._telegram.answer_callback_query(
                callback_query_id,
                "Нет доступа к настройкам этой группы.",
                show_alert=True,
            )
            return

        await self._telegram.answer_callback_query(callback_query_id, "Принято.")
        if section_key == "table":
            await self._show_table_section(
                group_chat_id=group_chat_id,
                chat_id=chat_id,
                message_id=message_id,
            )
            return
        if section_key == "clans":
            await self._show_clans_section(
                group_chat_id=group_chat_id,
                chat_id=chat_id,
                message_id=message_id,
            )
            return
        if section_key in COLUMN_SECTION_TABLE_TYPES:
            await self._show_columns_section(
                group_chat_id=group_chat_id,
                table_type=COLUMN_SECTION_TABLE_TYPES[section_key],
                chat_id=chat_id,
                message_id=message_id,
            )
            return

        title = SETTINGS_SECTIONS.get(section_key, "Раздел")
        text = f"Раздел «{title}» недоступен."
        await _edit_or_send_message(
            telegram=self._telegram,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=settings_menu_keyboard(group_chat_id),
        )

    async def _parse_callback_group_id(
        self,
        *,
        callback_data: str,
        callback_query_id: str,
        prefix: str,
    ) -> int | None:
        """Парсит ID группы из callback data.

        Args:
            callback_data: Callback data с ID группы.
            callback_query_id: ID callback query.
            prefix: Ожидаемый prefix callback data.

        Returns:
            ID группы или `None`, если callback повреждён.
        """

        raw_chat_id = callback_data.removeprefix(prefix)
        try:
            return int(raw_chat_id)
        except ValueError:
            await self._telegram.answer_callback_query(
                callback_query_id,
                "Некорректная группа.",
                show_alert=True,
            )
            return None

    async def _show_table_section(
        self,
        *,
        group_chat_id: int,
        chat_id: int,
        message_id: int,
    ) -> None:
        """Показывает раздел привязки Google-таблицы.

        Args:
            group_chat_id: ID настраиваемой Telegram-группы.
            chat_id: ID личного чата.
            message_id: ID сообщения для редактирования.
        """

        binding = await self._runtime.get_active_sheet_binding(group_chat_id)
        try:
            token_provider = GoogleAccessTokenProvider(self._config.google_service_account_file)
            service_account_email = token_provider.client_email
        except GoogleSheetsAuthError:
            service_account_email = "credentials.json недоступен"

        if binding is None:
            text = (
                "Раздел «Таблица».\n\n"
                "Таблица ещё не привязана. Нажмите кнопку ниже и отправьте ссылку "
                "на существующую Google-таблицу.\n\n"
                f"Service account: {service_account_email}"
            )
            reply_markup = sheet_section_keyboard(group_chat_id, has_binding=False)
        else:
            text = (
                "Раздел «Таблица».\n\n"
                f"Текущая таблица:\n{binding.spreadsheet_url}\n\n"
                f"Spreadsheet ID: {binding.google_sheet_id}\n"
                f"Лист состава: {binding.composition_sheet_name}\n"
                f"Лист CWL: {binding.active_cwl_sheet_name}\n"
                f"Service account: {service_account_email}\n\n"
                "Доступны диагностика, смена таблицы, отвязка и перенос в другую группу."
            )
            reply_markup = sheet_section_keyboard(group_chat_id, has_binding=True)

        await _edit_or_send_message(
            telegram=self._telegram,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
        )

    async def _show_change_sheet_warning(
        self,
        callback_data: str,
        callback_query_id: str,
        chat_id: int,
        message_id: int,
        user_id: int,
    ) -> None:
        """Показывает предупреждение перед сменой таблицы."""

        group_chat_id = await self._parse_callback_group_id(
            callback_data=callback_data,
            callback_query_id=callback_query_id,
            prefix=CALLBACK_CHANGE_SHEET_PREFIX,
        )
        if group_chat_id is None:
            return
        if not await self._has_sensitive_group_settings_access(
            group_chat_id=group_chat_id, user_id=user_id
        ):
            await self._telegram.answer_callback_query(
                callback_query_id, "Нет доступа.", show_alert=True
            )
            return
        await self._telegram.answer_callback_query(callback_query_id, "Принято.")
        await _edit_or_send_message(
            telegram=self._telegram,
            chat_id=chat_id,
            message_id=message_id,
            text=(
                "Старая таблица больше не будет обновляться.\n"
                "Настройки кланов и колонок сохранятся.\n\n"
                "Продолжить?"
            ),
            reply_markup=confirm_change_sheet_keyboard(group_chat_id),
        )

    async def _show_unlink_sheet_warning(
        self,
        callback_data: str,
        callback_query_id: str,
        chat_id: int,
        message_id: int,
        user_id: int,
    ) -> None:
        """Показывает подтверждение отвязки таблицы."""

        group_chat_id = await self._parse_callback_group_id(
            callback_data=callback_data,
            callback_query_id=callback_query_id,
            prefix=CALLBACK_UNLINK_SHEET_PREFIX,
        )
        if group_chat_id is None:
            return
        if not await self._has_sensitive_group_settings_access(
            group_chat_id=group_chat_id, user_id=user_id
        ):
            await self._telegram.answer_callback_query(
                callback_query_id, "Нет доступа.", show_alert=True
            )
            return
        await self._telegram.answer_callback_query(callback_query_id, "Принято.")
        await _edit_or_send_message(
            telegram=self._telegram,
            chat_id=chat_id,
            message_id=message_id,
            text=(
                "Отвязать таблицу от группы?\n\n"
                "После отвязки /sync станет недоступен. Кланы, колонки и state останутся в SQLite. "
                "Google Sheets не будет изменён."
            ),
            reply_markup=confirm_unlink_sheet_keyboard(group_chat_id),
        )

    async def _confirm_unlink_sheet(
        self,
        callback_data: str,
        callback_query_id: str,
        chat_id: int,
        message_id: int,
        user_id: int,
    ) -> None:
        """Деактивирует текущую привязку таблицы."""

        group_chat_id = await self._parse_callback_group_id(
            callback_data=callback_data,
            callback_query_id=callback_query_id,
            prefix=CALLBACK_CONFIRM_UNLINK_SHEET_PREFIX,
        )
        if group_chat_id is None:
            return
        if not await self._has_sensitive_group_settings_access(
            group_chat_id=group_chat_id, user_id=user_id
        ):
            await self._telegram.answer_callback_query(
                callback_query_id, "Нет доступа.", show_alert=True
            )
            return
        now = _format_dt(_utc_now())
        async with transaction(self._connection):
            await self._sheet_bindings.deactivate_binding(chat_id=group_chat_id, now=now)
            await self._telegram_chats.set_setup_state(
                chat_id=group_chat_id, setup_state=None, now=now
            )
            await self._telegram_chats.set_status(
                chat_id=group_chat_id, status="waiting_for_sheet", now=now
            )
        await self._telegram.answer_callback_query(callback_query_id, "Таблица отвязана.")
        await _edit_or_send_message(
            telegram=self._telegram,
            chat_id=chat_id,
            message_id=message_id,
            text="Таблица отвязана. Кланы и профили колонок сохранены.",
            reply_markup=settings_menu_keyboard(group_chat_id),
        )

    async def _diagnose_sheet(
        self,
        callback_data: str,
        callback_query_id: str,
        chat_id: int,
        message_id: int,
        user_id: int,
    ) -> None:
        """Запускает диагностику привязанной таблицы."""

        group_chat_id = await self._parse_callback_group_id(
            callback_data=callback_data,
            callback_query_id=callback_query_id,
            prefix=CALLBACK_DIAGNOSE_SHEET_PREFIX,
        )
        if group_chat_id is None:
            return
        if not await self._has_sensitive_group_settings_access(
            group_chat_id=group_chat_id, user_id=user_id
        ):
            await self._telegram.answer_callback_query(
                callback_query_id, "Нет доступа.", show_alert=True
            )
            return
        binding = await self._runtime.get_active_sheet_binding(group_chat_id)
        if binding is None:
            await self._telegram.answer_callback_query(
                callback_query_id, "Таблица не привязана.", show_alert=True
            )
            return
        await self._telegram.answer_callback_query(callback_query_id, "Проверяю таблицу.")
        try:
            result = await self._run_table_diagnostics(binding=binding)
        except (GoogleSheetsAuthError, SheetAdminError) as exc:
            await self._telegram.send_message(chat_id=chat_id, text=str(exc))
            return
        if await self._runtime.is_google_sheet_bound_elsewhere(
            binding.google_sheet_id,
            current_chat_id=group_chat_id,
        ):
            result = TableDiagnosticResult(
                issues=(
                    *result.issues,
                    _diagnostic_issue(
                        "error", "Эта таблица привязана к другой active group.", False
                    ),
                ),
                staging_sheets=result.staging_sheets,
            )
        await _edit_or_send_message(
            telegram=self._telegram,
            chat_id=chat_id,
            message_id=message_id,
            text=_diagnostic_text(result),
            reply_markup=diagnostic_keyboard(group_chat_id, result.has_fixable_issues),
        )

    async def _fix_sheet(
        self,
        callback_data: str,
        callback_query_id: str,
        chat_id: int,
        message_id: int,
        user_id: int,
    ) -> None:
        """Выполняет auto-fix привязанной таблицы."""

        group_chat_id = await self._parse_callback_group_id(
            callback_data=callback_data,
            callback_query_id=callback_query_id,
            prefix=CALLBACK_FIX_SHEET_PREFIX,
        )
        if group_chat_id is None:
            return
        if not await self._has_sensitive_group_settings_access(
            group_chat_id=group_chat_id, user_id=user_id
        ):
            await self._telegram.answer_callback_query(
                callback_query_id, "Нет доступа.", show_alert=True
            )
            return
        binding = await self._runtime.get_active_sheet_binding(group_chat_id)
        if binding is None:
            await self._telegram.answer_callback_query(
                callback_query_id, "Таблица не привязана.", show_alert=True
            )
            return
        await self._telegram.answer_callback_query(callback_query_id, "Исправляю.")
        try:
            setup_result = await self._run_table_autofix(binding=binding)
        except (GoogleSheetsAuthError, SheetAdminError) as exc:
            await self._telegram.send_message(chat_id=chat_id, text=str(exc))
            return
        now = _format_dt(_utc_now())
        async with transaction(self._connection):
            await self._sheet_bindings.update_sheet_ids(
                chat_id=group_chat_id,
                composition_sheet_name=setup_result.composition_sheet_name,
                composition_sheet_id=setup_result.composition_sheet_id,
                active_cwl_sheet_name=setup_result.active_cwl_sheet_name,
                active_cwl_sheet_id=setup_result.active_cwl_sheet_id,
                active_cwl_season=setup_result.active_cwl_season,
                bot_state_sheet_name=setup_result.bot_state_sheet_name,
                bot_state_sheet_id=setup_result.bot_state_sheet_id,
                now=now,
            )
        await _edit_or_send_message(
            telegram=self._telegram,
            chat_id=chat_id,
            message_id=message_id,
            text="Auto-fix выполнен. Запустите диагностику повторно.",
            reply_markup=diagnostic_keyboard(group_chat_id, has_fixable_issues=False),
        )

    async def _create_transfer_token(
        self,
        callback_data: str,
        callback_query_id: str,
        chat_id: int,
        user_id: int,
    ) -> None:
        """Создаёт transfer token для переноса таблицы в другую группу."""

        group_chat_id = await self._parse_callback_group_id(
            callback_data=callback_data,
            callback_query_id=callback_query_id,
            prefix=CALLBACK_CREATE_TRANSFER_PREFIX,
        )
        if group_chat_id is None:
            return
        if not await self._has_sensitive_group_settings_access(
            group_chat_id=group_chat_id, user_id=user_id
        ):
            await self._telegram.answer_callback_query(
                callback_query_id, "Нет доступа.", show_alert=True
            )
            return
        if await self._runtime.get_active_sheet_binding(group_chat_id) is None:
            await self._telegram.answer_callback_query(
                callback_query_id, "Таблица не привязана.", show_alert=True
            )
            return
        now = _utc_now()
        expires_at = now + timedelta(seconds=self._config.transfer_token_ttl_seconds)
        token = secrets.token_urlsafe(18)
        async with transaction(self._connection):
            await self._transfers.create_transfer_token(
                token=token,
                source_chat_id=group_chat_id,
                created_by_user_id=user_id,
                expires_at=_format_dt(expires_at),
                created_at=_format_dt(now),
            )
        command = f"/accept_transfer {token}"
        await self._telegram.answer_callback_query(callback_query_id, "Токен создан.")
        await self._telegram.send_message(
            chat_id=chat_id,
            text=(
                "Добавьте бота в новую группу и отправьте там команду:\n\n"
                f"<code>{escape(command)}</code>\n\n"
                "Команду должен отправить Telegram-администратор старой и новой группы."
            ),
            parse_mode="HTML",
        )

    async def _start_sheet_binding(
        self,
        *,
        callback_data: str,
        callback_query_id: str,
        chat_id: int,
        user_id: int,
    ) -> None:
        """Переводит группу в ожидание ссылки на Google-таблицу.

        Args:
            callback_data: Callback data с ID группы.
            callback_query_id: ID callback query.
            chat_id: ID личного чата.
            user_id: Telegram user ID.
        """

        group_chat_id = await self._parse_callback_group_id(
            callback_data=callback_data,
            callback_query_id=callback_query_id,
            prefix=CALLBACK_BIND_SHEET_PREFIX,
        )
        if group_chat_id is None:
            return

        if not await self._has_sensitive_group_settings_access(
            group_chat_id=group_chat_id, user_id=user_id
        ):
            await self._telegram.answer_callback_query(
                callback_query_id,
                "Нет доступа к настройкам этой группы.",
                show_alert=True,
            )
            return

        await self._telegram.answer_callback_query(callback_query_id, "Принято.")
        now = _format_dt(_utc_now())
        async with transaction(self._connection):
            await self._telegram_chats.set_setup_state(
                chat_id=group_chat_id,
                setup_state=_sheet_link_state(user_id),
                now=now,
            )
        await self._telegram.send_message(
            chat_id=chat_id,
            text="Отправьте ссылку на Google-таблицу.",
        )

    async def _check_sheet_access(
        self,
        *,
        callback_data: str,
        callback_query_id: str,
        chat_id: int,
        message_id: int,
        user_id: int,
    ) -> None:
        """Проверяет доступ к таблице и сохраняет binding.

        Args:
            callback_data: Callback data с ID группы.
            callback_query_id: ID callback query.
            chat_id: ID личного чата.
            message_id: ID сообщения для редактирования.
            user_id: Telegram user ID.
        """

        group_chat_id = await self._parse_callback_group_id(
            callback_data=callback_data,
            callback_query_id=callback_query_id,
            prefix=CALLBACK_CHECK_SHEET_PREFIX,
        )
        if group_chat_id is None:
            return

        if not await self._has_sensitive_group_settings_access(
            group_chat_id=group_chat_id, user_id=user_id
        ):
            await self._telegram.answer_callback_query(
                callback_query_id,
                "Нет доступа к настройкам этой группы.",
                show_alert=True,
            )
            return

        setup_state = await self._telegram_chats.get_setup_state(group_chat_id)
        spreadsheet_id = _spreadsheet_id_from_access_state(setup_state, user_id)
        if spreadsheet_id is None:
            await self._telegram.answer_callback_query(
                callback_query_id,
                "Ссылка на таблицу не найдена. Отправьте ссылку заново.",
                show_alert=True,
            )
            return

        if await self._runtime.is_google_sheet_bound_elsewhere(
            spreadsheet_id,
            current_chat_id=group_chat_id,
        ):
            await self._telegram.answer_callback_query(
                callback_query_id,
                "Эта таблица уже привязана к другой группе.",
                show_alert=True,
            )
            return

        await self._telegram.answer_callback_query(callback_query_id, "Проверяю доступ.")
        try:
            setup_result = await self._initialize_sheet(group_chat_id, spreadsheet_id)
        except (GoogleSheetsAuthError, SheetAdminError) as exc:
            await self._telegram.send_message(chat_id=chat_id, text=str(exc))
            return

        now = _format_dt(_utc_now())
        async with transaction(self._connection):
            if await self._runtime.is_google_sheet_bound_elsewhere(
                spreadsheet_id,
                current_chat_id=group_chat_id,
            ):
                await self._telegram.send_message(
                    chat_id=chat_id,
                    text="Эта таблица уже привязана к другой группе.",
                )
                return
            await self._sheet_bindings.upsert_active_binding(
                chat_id=group_chat_id,
                google_sheet_id=setup_result.spreadsheet_id,
                spreadsheet_url=setup_result.spreadsheet_url,
                composition_sheet_name=setup_result.composition_sheet_name,
                composition_sheet_id=setup_result.composition_sheet_id,
                active_cwl_sheet_name=setup_result.active_cwl_sheet_name,
                active_cwl_sheet_id=setup_result.active_cwl_sheet_id,
                active_cwl_season=setup_result.active_cwl_season,
                bot_state_sheet_name=setup_result.bot_state_sheet_name,
                bot_state_sheet_id=setup_result.bot_state_sheet_id,
                timezone=self._config.default_timezone,
                now=now,
            )
            await self._columns.ensure_default_profiles(chat_id=group_chat_id, now=now)
            await self._telegram_chats.set_setup_state(
                chat_id=group_chat_id,
                setup_state=None,
                now=now,
            )
            await self._telegram_chats.set_status(
                chat_id=group_chat_id,
                status="waiting_for_clans",
                now=now,
            )

        await _edit_or_send_message(
            telegram=self._telegram,
            chat_id=chat_id,
            message_id=message_id,
            text=(
                "Таблица привязана.\n\n"
                "Следующий шаг: откройте раздел «Кланы» и добавьте хотя бы один клан."
            ),
            reply_markup=settings_menu_keyboard(group_chat_id),
        )

    async def _initialize_sheet(self, group_chat_id: int, spreadsheet_id: str):
        """Инициализирует Google Sheets для новой привязки.

        Args:
            group_chat_id: ID Telegram-группы.
            spreadsheet_id: ID Google Spreadsheet.

        Returns:
            Результат подготовки листов.
        """

        token_provider = GoogleAccessTokenProvider(self._config.google_service_account_file)
        timeout = httpx.Timeout(30.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as http_client:
            sheets_client = SheetsClient(spreadsheet_id, token_provider, http_client)
            admin = SheetAdminService(
                sheets_client=sheets_client,
                spreadsheet_id=spreadsheet_id,
                service_account_email=token_provider.client_email,
                expected_service_account_email=self._config.google_service_account_email,
            )
            return await admin.initialize_new_binding(
                chat_id=group_chat_id,
                timezone=self._config.default_timezone,
            )

    async def _send_sheet_access_instruction(self, *, chat_id: int, group_chat_id: int) -> None:
        """Показывает service account email и кнопку проверки доступа.

        Args:
            chat_id: ID личного чата.
            group_chat_id: ID настраиваемой Telegram-группы.
        """

        try:
            token_provider = GoogleAccessTokenProvider(self._config.google_service_account_file)
            service_account_email = token_provider.client_email
        except GoogleSheetsAuthError as exc:
            await self._telegram.send_message(chat_id=chat_id, text=str(exc))
            return

        if (
            self._config.google_service_account_email is not None
            and self._config.google_service_account_email != service_account_email
        ):
            await self._telegram.send_message(
                chat_id=chat_id,
                text="GOOGLE_SERVICE_ACCOUNT_EMAIL не совпадает с client_email credentials.json.",
            )
            return

        await self._telegram.send_message(
            chat_id=chat_id,
            text=(
                "Добавьте этот email в Google-таблицу с правами Редактор:\n\n"
                f"<code>{escape(service_account_email)}</code>\n\n"
                "После этого нажмите «Проверить доступ»."
            ),
            reply_markup=check_sheet_access_keyboard(group_chat_id),
            parse_mode="HTML",
        )

    async def _has_group_settings_access(self, *, group_chat_id: int, user_id: int) -> bool:
        """Проверяет доступ пользователя к настройкам группы.

        Args:
            group_chat_id: ID Telegram-группы.
            user_id: Telegram user ID.

        Returns:
            `True`, если пользователь связан с группой через setup-flow.
        """

        return await self._telegram_chats.has_active_admin_link(
            chat_id=group_chat_id,
            user_id=user_id,
        )

    async def _has_sensitive_group_settings_access(
        self, *, group_chat_id: int, user_id: int
    ) -> bool:
        """Проверяет доступ к чувствительным действиям без admin-cache."""

        if not await self._has_group_settings_access(group_chat_id=group_chat_id, user_id=user_id):
            return False
        admin_result = await self._access.is_admin(
            chat_id=group_chat_id,
            user_id=user_id,
            force_refresh=True,
        )
        return admin_result.is_admin

    async def _run_table_diagnostics(self, *, binding) -> TableDiagnosticResult:
        """Запускает низкоуровневую диагностику Google Sheets."""

        token_provider = GoogleAccessTokenProvider(self._config.google_service_account_file)
        timeout = httpx.Timeout(30.0, connect=10.0)
        blocks = await self._blocks.list_blocks(binding.chat_id)
        async with httpx.AsyncClient(timeout=timeout) as http_client:
            sheets_client = SheetsClient(binding.google_sheet_id, token_provider, http_client)
            admin = SheetAdminService(
                sheets_client=sheets_client,
                spreadsheet_id=binding.google_sheet_id,
                service_account_email=token_provider.client_email,
                expected_service_account_email=self._config.google_service_account_email,
            )
            return await admin.diagnose_binding(binding=binding, blocks=blocks)

    async def _run_table_autofix(self, *, binding):
        """Запускает auto-fix Google Sheets и возвращает новые sheet IDs."""

        token_provider = GoogleAccessTokenProvider(self._config.google_service_account_file)
        timeout = httpx.Timeout(30.0, connect=10.0)
        blocks = await self._blocks.list_blocks(binding.chat_id)
        async with httpx.AsyncClient(timeout=timeout) as http_client:
            sheets_client = SheetsClient(binding.google_sheet_id, token_provider, http_client)
            admin = SheetAdminService(
                sheets_client=sheets_client,
                spreadsheet_id=binding.google_sheet_id,
                service_account_email=token_provider.client_email,
                expected_service_account_email=self._config.google_service_account_email,
            )
            return await admin.auto_fix_binding(binding=binding, blocks=blocks)

    async def _lookup_clan(self, clan_tag: str):
        """Проверяет клан через Clash of Clans API.

        Args:
            clan_tag: Нормализованный тег клана.

        Returns:
            Краткая информация о клане.
        """

        timeout = httpx.Timeout(30.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as http_client:
            client = ClashClient(self._config.coc_api_token, http_client)
            return await client.get_clan(clan_tag)

    async def _show_clans_section(
        self, *, group_chat_id: int, chat_id: int, message_id: int
    ) -> None:
        """Показывает раздел управления кланами."""

        clans = await self._clans.list_active_clans(group_chat_id)
        if clans:
            lines = ["Раздел «Кланы».", ""]
            lines.extend(
                f"{index}. {clan.clan_name} | {clan.clan_tag}"
                for index, clan in enumerate(clans, start=1)
            )
        else:
            lines = ["Раздел «Кланы».", "", "Активных кланов пока нет."]
        await _edit_or_send_message(
            telegram=self._telegram,
            chat_id=chat_id,
            message_id=message_id,
            text="\n".join(lines),
            reply_markup=clans_section_keyboard(group_chat_id, list(clans)),
        )

    async def _start_clan_add(
        self,
        callback_data: str,
        callback_query_id: str,
        chat_id: int,
        user_id: int,
    ) -> None:
        """Переводит группу в ожидание тега клана."""

        group_chat_id = await self._parse_callback_group_id(
            callback_data=callback_data,
            callback_query_id=callback_query_id,
            prefix=CALLBACK_CLAN_ADD_PREFIX,
        )
        if group_chat_id is None:
            return
        if not await self._has_sensitive_group_settings_access(
            group_chat_id=group_chat_id, user_id=user_id
        ):
            await self._telegram.answer_callback_query(
                callback_query_id, "Нет доступа.", show_alert=True
            )
            return
        if await self._clans.count_active_clans(group_chat_id) >= self._config.max_clans_per_chat:
            await self._telegram.answer_callback_query(
                callback_query_id,
                f"В этой группе уже добавлено максимальное количество кланов: {self._config.max_clans_per_chat}.",
                show_alert=True,
            )
            return
        now = _format_dt(_utc_now())
        async with transaction(self._connection):
            await self._telegram_chats.set_setup_state(
                chat_id=group_chat_id,
                setup_state=_clan_tag_state(user_id),
                now=now,
            )
        await self._telegram.answer_callback_query(callback_query_id, "Принято.")
        await self._telegram.send_message(
            chat_id=chat_id, text="Отправьте тег клана, например #2RVJ0CUR9."
        )

    async def _confirm_clan_add(
        self,
        callback_data: str,
        callback_query_id: str,
        chat_id: int,
        message_id: int,
        user_id: int,
    ) -> None:
        """Добавляет подтверждённый клан в отслеживание."""

        parsed = _parse_clan_callback(callback_data, CALLBACK_CLAN_CONFIRM_PREFIX)
        if parsed is None:
            await self._telegram.answer_callback_query(
                callback_query_id, "Некорректный клан.", show_alert=True
            )
            return
        group_chat_id, clan_tag = parsed
        if not await self._has_sensitive_group_settings_access(
            group_chat_id=group_chat_id, user_id=user_id
        ):
            await self._telegram.answer_callback_query(
                callback_query_id, "Нет доступа.", show_alert=True
            )
            return
        is_already_active = await self._clans.is_active_clan(
            chat_id=group_chat_id,
            clan_tag=clan_tag,
        )
        if (
            not is_already_active
            and await self._clans.count_active_clans(group_chat_id)
            >= self._config.max_clans_per_chat
        ):
            await self._telegram.answer_callback_query(
                callback_query_id,
                f"В этой группе уже добавлено максимальное количество кланов: {self._config.max_clans_per_chat}.",
                show_alert=True,
            )
            return
        try:
            clan = await self._lookup_clan(clan_tag)
        except (ClashClanNotFoundError, ClashApiUnavailableError) as exc:
            await self._telegram.answer_callback_query(callback_query_id, str(exc), show_alert=True)
            return
        now = _format_dt(_utc_now())
        async with transaction(self._connection):
            await self._clans.upsert_or_reactivate_clan(
                chat_id=group_chat_id,
                clan_tag=clan.tag,
                clan_name=clan.name,
                now=now,
            )
            await self._telegram_chats.set_setup_state(
                chat_id=group_chat_id, setup_state=None, now=now
            )
            await self._refresh_ready_status(group_chat_id=group_chat_id, now=now)
        await self._telegram.answer_callback_query(callback_query_id, "Клан добавлен.")
        await self._show_clans_section(
            group_chat_id=group_chat_id, chat_id=chat_id, message_id=message_id
        )

    async def _remove_clan(
        self,
        callback_data: str,
        callback_query_id: str,
        chat_id: int,
        message_id: int,
        user_id: int,
    ) -> None:
        """Мягко удаляет клан из отслеживания."""

        parsed = _parse_clan_callback(callback_data, CALLBACK_CLAN_REMOVE_PREFIX)
        if parsed is None:
            await self._telegram.answer_callback_query(
                callback_query_id, "Некорректный клан.", show_alert=True
            )
            return
        group_chat_id, clan_tag = parsed
        if not await self._has_sensitive_group_settings_access(
            group_chat_id=group_chat_id, user_id=user_id
        ):
            await self._telegram.answer_callback_query(
                callback_query_id, "Нет доступа.", show_alert=True
            )
            return
        now = _format_dt(_utc_now())
        async with transaction(self._connection):
            await self._clans.soft_delete_clan(chat_id=group_chat_id, clan_tag=clan_tag, now=now)
            await self._clans.mark_players_untracked(
                chat_id=group_chat_id, clan_tag=clan_tag, now=now
            )
            await self._refresh_ready_status(group_chat_id=group_chat_id, now=now)
        await self._telegram.answer_callback_query(
            callback_query_id, "Клан удалён из отслеживания."
        )
        await self._show_clans_section(
            group_chat_id=group_chat_id, chat_id=chat_id, message_id=message_id
        )

    async def _move_clan(
        self,
        callback_data: str,
        callback_query_id: str,
        chat_id: int,
        message_id: int,
        user_id: int,
        direction: str,
    ) -> None:
        """Меняет порядок клана."""

        prefix = (
            CALLBACK_CLAN_MOVE_UP_PREFIX if direction == "up" else CALLBACK_CLAN_MOVE_DOWN_PREFIX
        )
        parsed = _parse_clan_callback(callback_data, prefix)
        if parsed is None:
            await self._telegram.answer_callback_query(
                callback_query_id, "Некорректный клан.", show_alert=True
            )
            return
        group_chat_id, clan_tag = parsed
        if not await self._has_sensitive_group_settings_access(
            group_chat_id=group_chat_id, user_id=user_id
        ):
            await self._telegram.answer_callback_query(
                callback_query_id, "Нет доступа.", show_alert=True
            )
            return
        now = _format_dt(_utc_now())
        async with transaction(self._connection):
            await self._clans.move_clan(
                chat_id=group_chat_id,
                clan_tag=clan_tag,
                direction=direction,
                now=now,
            )
        await self._telegram.answer_callback_query(callback_query_id, "Порядок обновлён.")
        await self._show_clans_section(
            group_chat_id=group_chat_id, chat_id=chat_id, message_id=message_id
        )

    async def _show_columns_section(
        self,
        *,
        group_chat_id: int,
        table_type: TableType,
        chat_id: int,
        message_id: int,
    ) -> None:
        """Показывает раздел управления колонками."""

        text, reply_markup = await self._build_columns_section_payload(
            group_chat_id=group_chat_id,
            table_type=table_type,
        )
        await _edit_or_send_message(
            telegram=self._telegram,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
        )

    async def _build_columns_section_payload(
        self,
        *,
        group_chat_id: int,
        table_type: TableType,
        prefix: str = "",
    ) -> tuple[str, JsonObject]:
        """Собирает текст и клавиатуру раздела управления колонками."""

        now = _format_dt(_utc_now())
        async with transaction(self._connection):
            await self._columns.ensure_default_profiles(chat_id=group_chat_id, now=now)
        columns = await self._columns.list_columns(chat_id=group_chat_id, table_type=table_type)
        editable_columns = [column for column in columns if column.kind != "service"]
        lines = []
        if prefix:
            lines.append(prefix.rstrip())
            lines.append("")
        lines.extend([f"Раздел «{table_title(table_type)}».", ""])
        lines.append("Служебная колонка __bot_key скрывается автоматически.")
        lines.append("")
        if editable_columns:
            for column in editable_columns:
                marker = "✅" if column.visible else "❌"
                lines.append(f"{marker} {column.title}")
        else:
            lines.append("Настраиваемых колонок пока нет.")
        return "\n".join(lines), columns_section_keyboard(group_chat_id, table_type, list(columns))

    async def _send_columns_section_message(
        self,
        *,
        group_chat_id: int,
        chat_id: int,
        table_type: TableType,
        prefix: str = "",
    ) -> None:
        """Отправляет новый экран управления колонками.

        Используется после text-input действий, когда нельзя редактировать
        старое inline-сообщение: у обычного сообщения нет `message_id` меню.
        """

        text, reply_markup = await self._build_columns_section_payload(
            group_chat_id=group_chat_id,
            table_type=table_type,
            prefix=prefix,
        )
        await self._telegram.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)

    async def _start_user_column_add(
        self,
        callback_data: str,
        callback_query_id: str,
        chat_id: int,
        user_id: int,
    ) -> None:
        """Запрашивает название новой user-колонки."""

        parsed = _parse_column_section_callback(callback_data, CALLBACK_COLUMN_ADD_PREFIX)
        if parsed is None:
            await self._telegram.answer_callback_query(
                callback_query_id, "Некорректный раздел.", show_alert=True
            )
            return
        group_chat_id, table_type = parsed
        if not await self._has_sensitive_group_settings_access(
            group_chat_id=group_chat_id, user_id=user_id
        ):
            await self._telegram.answer_callback_query(
                callback_query_id, "Нет доступа.", show_alert=True
            )
            return
        now = _format_dt(_utc_now())
        async with transaction(self._connection):
            await self._telegram_chats.set_setup_state(
                chat_id=group_chat_id,
                setup_state=_user_column_title_state(user_id, table_type),
                now=now,
            )
        await self._telegram.answer_callback_query(callback_query_id, "Принято.")
        await self._telegram.send_message(chat_id=chat_id, text="Отправьте название новой колонки.")

    async def _start_column_rename(
        self,
        callback_data: str,
        callback_query_id: str,
        chat_id: int,
        user_id: int,
    ) -> None:
        """Запрашивает новое название колонки."""

        parsed = _parse_column_callback(callback_data, CALLBACK_COLUMN_RENAME_PREFIX)
        if parsed is None:
            await self._telegram.answer_callback_query(
                callback_query_id, "Некорректная колонка.", show_alert=True
            )
            return
        group_chat_id, table_type, column_key = parsed
        if not await self._has_sensitive_group_settings_access(
            group_chat_id=group_chat_id, user_id=user_id
        ):
            await self._telegram.answer_callback_query(
                callback_query_id, "Нет доступа.", show_alert=True
            )
            return
        now = _format_dt(_utc_now())
        async with transaction(self._connection):
            await self._telegram_chats.set_setup_state(
                chat_id=group_chat_id,
                setup_state=_column_rename_state(user_id, table_type, column_key),
                now=now,
            )
        await self._telegram.answer_callback_query(callback_query_id, "Принято.")
        await self._telegram.send_message(chat_id=chat_id, text="Отправьте новое название колонки.")

    async def _toggle_column(
        self,
        callback_data: str,
        callback_query_id: str,
        chat_id: int,
        message_id: int,
        user_id: int,
    ) -> None:
        """Переключает видимость колонки."""

        parsed = _parse_column_callback(callback_data, CALLBACK_COLUMN_TOGGLE_PREFIX)
        if parsed is None:
            await self._telegram.answer_callback_query(
                callback_query_id, "Некорректная колонка.", show_alert=True
            )
            return
        group_chat_id, table_type, column_key = parsed
        if not await self._has_sensitive_group_settings_access(
            group_chat_id=group_chat_id, user_id=user_id
        ):
            await self._telegram.answer_callback_query(
                callback_query_id, "Нет доступа.", show_alert=True
            )
            return
        columns = await self._columns.list_columns(chat_id=group_chat_id, table_type=table_type)
        target = next((column for column in columns if column.column_key == column_key), None)
        if target is None or target.kind == "service":
            await self._telegram.answer_callback_query(
                callback_query_id, "Эту колонку нельзя скрыть.", show_alert=True
            )
            return
        now = _format_dt(_utc_now())
        async with transaction(self._connection):
            await self._columns.set_visibility(
                chat_id=group_chat_id,
                table_type=table_type,
                column_key=column_key,
                visible=not target.visible,
                now=now,
            )
        await self._telegram.answer_callback_query(callback_query_id, "Видимость обновлена.")
        await self._show_columns_section(
            group_chat_id=group_chat_id,
            table_type=table_type,
            chat_id=chat_id,
            message_id=message_id,
        )

    async def _delete_user_column(
        self,
        callback_data: str,
        callback_query_id: str,
        chat_id: int,
        message_id: int,
        user_id: int,
    ) -> None:
        """Мягко удаляет user-колонку."""

        parsed = _parse_column_callback(callback_data, CALLBACK_COLUMN_DELETE_PREFIX)
        if parsed is None:
            await self._telegram.answer_callback_query(
                callback_query_id, "Некорректная колонка.", show_alert=True
            )
            return
        group_chat_id, table_type, column_key = parsed
        if not await self._has_sensitive_group_settings_access(
            group_chat_id=group_chat_id, user_id=user_id
        ):
            await self._telegram.answer_callback_query(
                callback_query_id, "Нет доступа.", show_alert=True
            )
            return
        now = _format_dt(_utc_now())
        async with transaction(self._connection):
            deleted = await self._columns.soft_delete_user_column(
                chat_id=group_chat_id,
                table_type=table_type,
                column_key=column_key,
                now=now,
            )
        await self._telegram.answer_callback_query(
            callback_query_id,
            "Колонка удалена." if deleted else "Можно удалить только пользовательскую колонку.",
            show_alert=not deleted,
        )
        await self._show_columns_section(
            group_chat_id=group_chat_id,
            table_type=table_type,
            chat_id=chat_id,
            message_id=message_id,
        )

    async def _move_column(
        self,
        callback_data: str,
        callback_query_id: str,
        chat_id: int,
        message_id: int,
        user_id: int,
        direction: str,
    ) -> None:
        """Меняет порядок колонки."""

        prefix = (
            CALLBACK_COLUMN_MOVE_UP_PREFIX
            if direction == "up"
            else CALLBACK_COLUMN_MOVE_DOWN_PREFIX
        )
        parsed = _parse_column_callback(callback_data, prefix)
        if parsed is None:
            await self._telegram.answer_callback_query(
                callback_query_id, "Некорректная колонка.", show_alert=True
            )
            return
        group_chat_id, table_type, column_key = parsed
        if not await self._has_sensitive_group_settings_access(
            group_chat_id=group_chat_id, user_id=user_id
        ):
            await self._telegram.answer_callback_query(
                callback_query_id, "Нет доступ.", show_alert=True
            )
            return
        now = _format_dt(_utc_now())
        async with transaction(self._connection):
            await self._columns.move_column(
                chat_id=group_chat_id,
                table_type=table_type,
                column_key=column_key,
                direction=direction,
                now=now,
            )
        await self._telegram.answer_callback_query(callback_query_id, "Порядок обновлён.")
        await self._show_columns_section(
            group_chat_id=group_chat_id,
            table_type=table_type,
            chat_id=chat_id,
            message_id=message_id,
        )

    async def _restore_columns(
        self,
        callback_data: str,
        callback_query_id: str,
        chat_id: int,
        message_id: int,
        user_id: int,
    ) -> None:
        """Восстанавливает обязательные колонки профиля."""

        parsed = _parse_column_section_callback(callback_data, CALLBACK_COLUMN_RESTORE_PREFIX)
        if parsed is None:
            await self._telegram.answer_callback_query(
                callback_query_id, "Некорректный раздел.", show_alert=True
            )
            return
        group_chat_id, table_type = parsed
        if not await self._has_sensitive_group_settings_access(
            group_chat_id=group_chat_id, user_id=user_id
        ):
            await self._telegram.answer_callback_query(
                callback_query_id, "Нет доступа.", show_alert=True
            )
            return
        now = _format_dt(_utc_now())
        async with transaction(self._connection):
            await self._columns.restore_defaults(
                chat_id=group_chat_id, table_type=table_type, now=now
            )
        await self._telegram.answer_callback_query(
            callback_query_id, "Обязательные колонки восстановлены."
        )
        await self._show_columns_section(
            group_chat_id=group_chat_id,
            table_type=table_type,
            chat_id=chat_id,
            message_id=message_id,
        )

    async def _refresh_ready_status(self, *, group_chat_id: int, now: str) -> None:
        """Пересчитывает готовность группы после изменения кланов."""

        binding = await self._runtime.get_active_sheet_binding(group_chat_id)
        if binding is None:
            await self._telegram_chats.set_status(
                chat_id=group_chat_id, status="waiting_for_sheet", now=now
            )
            return
        active_clans = await self._clans.count_active_clans(group_chat_id)
        status = "ready" if active_clans > 0 else "waiting_for_clans"
        await self._telegram_chats.set_status(chat_id=group_chat_id, status=status, now=now)


@dataclass(frozen=True, slots=True)
class KnownGroupButton:
    """Данные кнопки известной группы.

    Attributes:
        chat_id: ID Telegram-группы.
        title: Название группы.
    """

    chat_id: int
    title: str


def main_private_keyboard() -> JsonObject:
    """Создаёт главное меню личного чата.

    Returns:
        Telegram inline keyboard.
    """

    return {
        "inline_keyboard": [
            [{"text": "Подключить группу", "callback_data": CALLBACK_CONNECT_GROUP}],
            [{"text": "Мои группы", "callback_data": CALLBACK_MY_GROUPS}],
            [{"text": "Помощь", "callback_data": CALLBACK_HELP}],
        ],
    }


def known_groups_keyboard(groups: list[KnownGroupButton]) -> JsonObject:
    """Создаёт клавиатуру известных групп.

    Args:
        groups: Группы пользователя.

    Returns:
        Telegram inline keyboard.
    """

    return {
        "inline_keyboard": [
            [
                {
                    "text": group.title,
                    "callback_data": f"{CALLBACK_SETTINGS_PREFIX}{group.chat_id}",
                },
            ]
            for group in groups
        ]
        + [[{"text": "Назад", "callback_data": CALLBACK_PRIVATE_START}]],
    }


def private_chat_keyboard(bot_username: str | None) -> JsonObject | None:
    """Создаёт кнопку перехода в личный чат с ботом.

    Args:
        bot_username: Username бота без `@` или `None`.

    Returns:
        Telegram inline keyboard или `None`, если username недоступен.
    """

    if bot_username is None:
        return None
    return {
        "inline_keyboard": [
            [{"text": "Открыть личный чат", "url": f"https://t.me/{bot_username}"}],
        ],
    }


def settings_menu_keyboard(group_chat_id: int) -> JsonObject:
    """Создаёт skeleton-меню настроек группы.

    Args:
        group_chat_id: ID Telegram-группы.
        action_text: Текст кнопки привязки или замены таблицы.

    Returns:
        Telegram inline keyboard.
    """

    return {
        "inline_keyboard": [
            [
                {
                    "text": title,
                    "callback_data": (f"{CALLBACK_SETTINGS_SECTION_PREFIX}{group_chat_id}:{key}"),
                },
            ]
            for key, title in SETTINGS_SECTIONS.items()
        ]
        + [[{"text": "Назад", "callback_data": CALLBACK_MY_GROUPS}]],
    }


def sheet_section_keyboard(group_chat_id: int, *, has_binding: bool) -> JsonObject:
    """Создаёт клавиатуру раздела таблицы."""

    keyboard: list[list[dict[str, str]]] = []
    if has_binding:
        keyboard.extend(
            [
                [
                    {
                        "text": "Проверить таблицу",
                        "callback_data": f"{CALLBACK_DIAGNOSE_SHEET_PREFIX}{group_chat_id}",
                    },
                ],
                [
                    {
                        "text": "Сменить таблицу",
                        "callback_data": f"{CALLBACK_CHANGE_SHEET_PREFIX}{group_chat_id}",
                    },
                ],
                [
                    {
                        "text": "Отвязать таблицу",
                        "callback_data": f"{CALLBACK_UNLINK_SHEET_PREFIX}{group_chat_id}",
                    },
                ],
                [
                    {
                        "text": "Перенести в другую группу",
                        "callback_data": f"{CALLBACK_CREATE_TRANSFER_PREFIX}{group_chat_id}",
                    },
                ],
            ],
        )
    else:
        keyboard.append(
            [
                {
                    "text": "Привязать таблицу",
                    "callback_data": f"{CALLBACK_BIND_SHEET_PREFIX}{group_chat_id}",
                },
            ],
        )
    keyboard.append(
        [
            {
                "text": "Назад",
                "callback_data": f"{CALLBACK_SETTINGS_PREFIX}{group_chat_id}",
            },
        ],
    )
    return {"inline_keyboard": keyboard}


def confirm_change_sheet_keyboard(group_chat_id: int) -> JsonObject:
    """Создаёт клавиатуру подтверждения смены таблицы."""

    return {
        "inline_keyboard": [
            [
                {
                    "text": "Продолжить",
                    "callback_data": f"{CALLBACK_BIND_SHEET_PREFIX}{group_chat_id}",
                }
            ],
            [
                {
                    "text": "Назад",
                    "callback_data": f"{CALLBACK_SETTINGS_SECTION_PREFIX}{group_chat_id}:table",
                }
            ],
        ],
    }


def confirm_unlink_sheet_keyboard(group_chat_id: int) -> JsonObject:
    """Создаёт клавиатуру подтверждения отвязки таблицы."""

    return {
        "inline_keyboard": [
            [
                {
                    "text": "Отвязать таблицу",
                    "callback_data": f"{CALLBACK_CONFIRM_UNLINK_SHEET_PREFIX}{group_chat_id}",
                }
            ],
            [
                {
                    "text": "Назад",
                    "callback_data": f"{CALLBACK_SETTINGS_SECTION_PREFIX}{group_chat_id}:table",
                }
            ],
        ],
    }


def diagnostic_keyboard(group_chat_id: int, has_fixable_issues: bool) -> JsonObject:
    """Создаёт клавиатуру результата диагностики."""

    keyboard: list[list[dict[str, str]]] = []
    if has_fixable_issues:
        keyboard.append(
            [{"text": "Исправить", "callback_data": f"{CALLBACK_FIX_SHEET_PREFIX}{group_chat_id}"}]
        )
    keyboard.append(
        [
            {
                "text": "Назад",
                "callback_data": f"{CALLBACK_SETTINGS_SECTION_PREFIX}{group_chat_id}:table",
            }
        ]
    )
    return {"inline_keyboard": keyboard}


def check_sheet_access_keyboard(group_chat_id: int) -> JsonObject:
    """Создаёт клавиатуру проверки доступа к таблице.

    Args:
        group_chat_id: ID Telegram-группы.

    Returns:
        Telegram inline keyboard.
    """

    return {
        "inline_keyboard": [
            [
                {
                    "text": "Проверить доступ",
                    "callback_data": f"{CALLBACK_CHECK_SHEET_PREFIX}{group_chat_id}",
                },
            ],
            [
                {
                    "text": "Назад",
                    "callback_data": f"{CALLBACK_SETTINGS_SECTION_PREFIX}{group_chat_id}:table",
                },
            ],
        ],
    }


def clans_section_keyboard(group_chat_id: int, clans: list) -> JsonObject:
    """Создаёт клавиатуру раздела кланов.

    Args:
        group_chat_id: ID Telegram-группы.
        clans: Активные кланы.

    Returns:
        Telegram inline keyboard.
    """

    keyboard: list[list[dict[str, str]]] = [
        [{"text": "Добавить клан", "callback_data": f"{CALLBACK_CLAN_ADD_PREFIX}{group_chat_id}"}],
    ]
    total = len(clans)
    for index, clan in enumerate(clans):
        tag_payload = _tag_payload(clan.clan_tag)
        up_callback = f"{CALLBACK_CLAN_MOVE_UP_PREFIX}{group_chat_id}:{tag_payload}"
        down_callback = f"{CALLBACK_CLAN_MOVE_DOWN_PREFIX}{group_chat_id}:{tag_payload}"
        keyboard.append(
            [
                {"text": clan.clan_name, "callback_data": "noop"},
                *_move_buttons(
                    index=index,
                    total=total,
                    up_callback=up_callback,
                    down_callback=down_callback,
                ),
                {
                    "text": "Удалить",
                    "callback_data": f"{CALLBACK_CLAN_REMOVE_PREFIX}{group_chat_id}:{tag_payload}",
                },
            ],
        )
    keyboard.append(
        [{"text": "Назад", "callback_data": f"{CALLBACK_SETTINGS_PREFIX}{group_chat_id}"}]
    )
    return {"inline_keyboard": keyboard}


def confirm_clan_keyboard(group_chat_id: int, clan_tag: str) -> JsonObject:
    """Создаёт клавиатуру подтверждения добавления клана.

    Args:
        group_chat_id: ID Telegram-группы.
        clan_tag: Нормализованный тег клана.

    Returns:
        Telegram inline keyboard.
    """

    return {
        "inline_keyboard": [
            [
                {
                    "text": "Добавить",
                    "callback_data": f"{CALLBACK_CLAN_CONFIRM_PREFIX}{group_chat_id}:{_tag_payload(clan_tag)}",
                },
            ],
            [
                {
                    "text": "Кланы",
                    "callback_data": f"{CALLBACK_SETTINGS_SECTION_PREFIX}{group_chat_id}:clans",
                }
            ],
        ],
    }


def columns_section_keyboard(
    group_chat_id: int, table_type: TableType, columns: list
) -> JsonObject:
    """Создаёт клавиатуру управления колонками.

    Args:
        group_chat_id: ID Telegram-группы.
        table_type: Тип таблицы.
        columns: Активные колонки профиля.

    Returns:
        Telegram inline keyboard.
    """

    keyboard: list[list[dict[str, str]]] = [
        [
            {
                "text": "Добавить пользовательскую колонку",
                "callback_data": f"{CALLBACK_COLUMN_ADD_PREFIX}{group_chat_id}:{table_type}",
            },
        ],
        [
            {
                "text": "Восстановить обязательные",
                "callback_data": f"{CALLBACK_COLUMN_RESTORE_PREFIX}{group_chat_id}:{table_type}",
            },
        ],
    ]
    editable_columns = [column for column in columns if column.kind != "service"]
    total = len(editable_columns)

    for index, column in enumerate(editable_columns):
        action_text = "Удалить" if column.kind == "user" else ("✅" if column.visible else "❌")
        action_callback = (
            f"{CALLBACK_COLUMN_DELETE_PREFIX}{group_chat_id}:{table_type}:{column.column_key}"
            if column.kind == "user"
            else f"{CALLBACK_COLUMN_TOGGLE_PREFIX}{group_chat_id}:{table_type}:{column.column_key}"
        )
        up_callback = (
            f"{CALLBACK_COLUMN_MOVE_UP_PREFIX}{group_chat_id}:{table_type}:{column.column_key}"
        )
        down_callback = (
            f"{CALLBACK_COLUMN_MOVE_DOWN_PREFIX}{group_chat_id}:{table_type}:{column.column_key}"
        )
        keyboard.append(
            [
                {"text": column.title, "callback_data": "noop"},
                *_move_buttons(
                    index=index,
                    total=total,
                    up_callback=up_callback,
                    down_callback=down_callback,
                ),
                {"text": action_text, "callback_data": action_callback},
            ],
        )
    keyboard.append(
        [{"text": "Назад", "callback_data": f"{CALLBACK_SETTINGS_PREFIX}{group_chat_id}"}]
    )
    return {"inline_keyboard": keyboard}


def _move_buttons(
    *,
    index: int,
    total: int,
    up_callback: str,
    down_callback: str,
) -> list[dict[str, str]]:
    """Создаёт кнопки изменения порядка для inline-списков."""

    if total <= 1:
        return [{"text": "·", "callback_data": "noop"}, {"text": "·", "callback_data": "noop"}]
    if index == 0:
        return [
            {"text": "·", "callback_data": "noop"},
            {"text": "↓", "callback_data": down_callback},
        ]
    if index == total - 1:
        return [{"text": "↑", "callback_data": up_callback}, {"text": "·", "callback_data": "noop"}]
    return [
        {"text": "↑", "callback_data": up_callback},
        {"text": "↓", "callback_data": down_callback},
    ]


def _tag_payload(clan_tag: str) -> str:
    """Преобразует тег клана в безопасный payload callback.

    Args:
        clan_tag: Нормализованный тег клана.

    Returns:
        Тег без ведущего `#`.
    """

    return clan_tag.removeprefix("#")


def _tag_from_payload(value: str) -> str:
    """Восстанавливает тег клана из callback payload.

    Args:
        value: Payload без ведущего `#`.

    Returns:
        Нормализованный тег клана.
    """

    return normalize_tag(f"#{value}")


def _parse_clan_callback(callback_data: str, prefix: str) -> tuple[int, str] | None:
    """Парсит callback клановой операции.

    Args:
        callback_data: Callback data.
        prefix: Prefix операции.

    Returns:
        Пара `(chat_id, clan_tag)` или `None`.
    """

    payload = callback_data.removeprefix(prefix)
    raw_chat_id, _, raw_tag = payload.partition(":")
    try:
        return int(raw_chat_id), _tag_from_payload(raw_tag)
    except ValueError:
        return None


def _parse_column_section_callback(callback_data: str, prefix: str) -> tuple[int, TableType] | None:
    """Парсит callback операции над разделом колонок.

    Args:
        callback_data: Callback data.
        prefix: Prefix операции.

    Returns:
        Пара `(chat_id, table_type)` или `None`.
    """

    payload = callback_data.removeprefix(prefix)
    raw_chat_id, _, raw_table_type = payload.partition(":")
    try:
        chat_id = int(raw_chat_id)
    except ValueError:
        return None
    if raw_table_type not in {"composition_active", "composition_exited", "cwl"}:
        return None
    return chat_id, raw_table_type  # type: ignore[return-value]


def _parse_column_callback(callback_data: str, prefix: str) -> tuple[int, TableType, str] | None:
    """Парсит callback операции над конкретной колонкой.

    Args:
        callback_data: Callback data.
        prefix: Prefix операции.

    Returns:
        Тройка `(chat_id, table_type, column_key)` или `None`.
    """

    payload = callback_data.removeprefix(prefix)
    raw_chat_id, _, rest = payload.partition(":")
    raw_table_type, _, column_key = rest.partition(":")
    try:
        chat_id = int(raw_chat_id)
    except ValueError:
        return None
    if (
        raw_table_type not in {"composition_active", "composition_exited", "cwl"}
        or column_key == ""
    ):
        return None
    return chat_id, raw_table_type, column_key  # type: ignore[return-value]


def _clan_tag_state(user_id: int) -> str:
    """Создаёт setup_state ожидания тега клана."""

    return f"{AWAITING_CLAN_TAG_STATE_PREFIX}{user_id}"


def _user_column_title_state(user_id: int, table_type: TableType) -> str:
    """Создаёт setup_state ожидания названия user-колонки."""

    return f"{AWAITING_USER_COLUMN_TITLE_STATE_PREFIX}{user_id}:{table_type}"


def _column_rename_state(user_id: int, table_type: TableType, column_key: str) -> str:
    """Создаёт setup_state ожидания нового названия колонки."""

    return f"{AWAITING_COLUMN_RENAME_STATE_PREFIX}{user_id}:{table_type}:{column_key}"


def _table_type_from_state(setup_state: str, *, prefix: str, user_id: int) -> TableType | None:
    """Извлекает table_type из setup_state.

    Args:
        setup_state: Текущее состояние группы.
        prefix: Prefix состояния.
        user_id: Telegram user ID.

    Returns:
        Тип таблицы или `None`.
    """

    expected_prefix = f"{prefix}{user_id}:"
    if not setup_state.startswith(expected_prefix):
        return None
    raw_table_type = setup_state.removeprefix(expected_prefix)
    if raw_table_type not in {"composition_active", "composition_exited", "cwl"}:
        return None
    return raw_table_type  # type: ignore[return-value]


def _rename_state_payload(setup_state: str, user_id: int) -> tuple[TableType, str] | None:
    """Извлекает данные переименования из setup_state.

    Args:
        setup_state: Текущее состояние группы.
        user_id: Telegram user ID.

    Returns:
        Пара `(table_type, column_key)` или `None`.
    """

    expected_prefix = f"{AWAITING_COLUMN_RENAME_STATE_PREFIX}{user_id}:"
    if not setup_state.startswith(expected_prefix):
        return None
    payload = setup_state.removeprefix(expected_prefix)
    raw_table_type, _, column_key = payload.partition(":")
    if (
        raw_table_type not in {"composition_active", "composition_exited", "cwl"}
        or column_key == ""
    ):
        return None
    return raw_table_type, column_key  # type: ignore[return-value]


def _validate_setup_token(
    setup_token: SetupToken | None,
    raw_token: str,
    user_id: int,
) -> str | None:
    """Проверяет setup-токен.

    Args:
        setup_token: Токен из БД или `None`.
        raw_token: Исходное значение из команды.
        user_id: Telegram user ID отправителя команды.

    Returns:
        Текст ошибки или `None`.
    """

    if setup_token is None or setup_token.token != raw_token:
        return "Токен подключения не найден. Создайте новый токен в личном чате."
    if setup_token.used_at is not None:
        return "Токен подключения уже использован. Создайте новый токен в личном чате."
    if setup_token.created_by_user_id != user_id:
        return "Этот токен создан другим пользователем. Создайте свой токен в личном чате."

    try:
        expires_at = datetime.fromisoformat(setup_token.expires_at)
    except ValueError:
        return "Токен подключения повреждён. Создайте новый токен в личном чате."
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= _utc_now():
        return "Токен подключения истёк. Создайте новый токен в личном чате."
    return None


def _validate_transfer_token(transfer_token, raw_token: str) -> str | None:
    """Проверяет transfer token."""

    if transfer_token is None or transfer_token.token != raw_token:
        return "Transfer token не найден. Создайте новый токен в настройках старой группы."
    if transfer_token.used_at is not None:
        return "Transfer token уже использован. Создайте новый токен."
    try:
        expires_at = datetime.fromisoformat(transfer_token.expires_at)
    except ValueError:
        return "Transfer token повреждён. Создайте новый токен."
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= _utc_now():
        return "Transfer token истёк. Создайте новый токен."
    return None


def _diagnostic_issue(level: str, message: str, fixable: bool) -> TableDiagnosticIssue:
    """Создаёт diagnostic issue."""

    return TableDiagnosticIssue(level=level, message=message, fixable=fixable)


def _diagnostic_text(result: TableDiagnosticResult) -> str:
    """Формирует текст результата диагностики таблицы."""

    lines = ["Диагностика таблицы.", ""]
    for issue in result.issues:
        if issue.level == "ok":
            marker = "✅"
        elif issue.level == "warning":
            marker = "⚠️"
        else:
            marker = "❌"
        suffix = " [можно исправить]" if issue.fixable else ""
        lines.append(f"{marker} {issue.message}{suffix}")
    if result.has_fixable_issues:
        lines.extend(["", "Есть исправимые проблемы. Нажмите «Исправить»."])
    return "\n".join(lines)


async def _edit_or_send_message(
    *,
    telegram: TelegramClient,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: JsonObject | None = None,
) -> None:
    """Редактирует сообщение или отправляет новое при невозможности редактирования.

    Args:
        telegram: Клиент Telegram Bot API.
        chat_id: ID Telegram-чата.
        message_id: ID сообщения.
        text: Новый текст сообщения.
        reply_markup: Inline-клавиатура.
    """

    try:
        await telegram.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
        )
    except TelegramMessageNotModifiedError:
        return


def _utc_now() -> datetime:
    """Возвращает текущую UTC-дату.

    Returns:
        Timezone-aware UTC datetime.
    """

    return datetime.now(UTC).replace(microsecond=0)


def _format_dt(value: datetime) -> str:
    """Форматирует дату для SQLite.

    Args:
        value: Дата и время.

    Returns:
        ISO-строка с timezone offset.
    """

    return value.astimezone(UTC).replace(microsecond=0).isoformat()


def _sheet_link_state(user_id: int) -> str:
    """Создаёт setup_state ожидания ссылки на таблицу.

    Args:
        user_id: Telegram user ID администратора.

    Returns:
        Значение setup_state.
    """

    return f"{AWAITING_SHEET_LINK_STATE_PREFIX}{user_id}"


def _sheet_access_state(user_id: int, spreadsheet_id: str) -> str:
    """Создаёт setup_state ожидания проверки доступа.

    Args:
        user_id: Telegram user ID администратора.
        spreadsheet_id: ID Google Spreadsheet.

    Returns:
        Значение setup_state.
    """

    return f"{AWAITING_SHEET_ACCESS_STATE_PREFIX}{user_id}:{spreadsheet_id}"


def _spreadsheet_id_from_access_state(setup_state: str | None, user_id: int) -> str | None:
    """Извлекает spreadsheet_id из setup_state проверки доступа.

    Args:
        setup_state: Текущее setup_state группы.
        user_id: Telegram user ID администратора.

    Returns:
        ID Google Spreadsheet или `None`.
    """

    expected_prefix = f"{AWAITING_SHEET_ACCESS_STATE_PREFIX}{user_id}:"
    if setup_state is None or not setup_state.startswith(expected_prefix):
        return None
    spreadsheet_id = setup_state.removeprefix(expected_prefix).strip()
    return spreadsheet_id or None
