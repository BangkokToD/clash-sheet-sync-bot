"""Сценарии подключения группы и навигации настроек."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Final

import aiosqlite
import httpx

from models import AppConfig, SetupToken
from repositories import (
    AdminChatRepository,
    RuntimeConfigRepository,
    SheetBindingRepository,
    SetupTokenRepository,
    TelegramChatRepository,
)
from sheet_admin import SheetAdminError, SheetAdminService, extract_spreadsheet_id
from storage import transaction
from telegram_access import TelegramAccessService
from telegram_client import (
    JsonObject,
    TelegramApiError,
    TelegramClient,
    TelegramMessageNotModifiedError,
)
from sheets_client import (
    GoogleAccessTokenProvider,
    GoogleSheetsAuthError,
    SheetsClient,
)

CALLBACK_CONNECT_GROUP: Final = "setup:create_token"
CALLBACK_MY_GROUPS: Final = "setup:my_groups"
CALLBACK_HELP: Final = "setup:help"
CALLBACK_SETTINGS_PREFIX: Final = "settings:open:"
CALLBACK_SETTINGS_SECTION_PREFIX: Final = "settings:section:"
CALLBACK_BIND_SHEET_PREFIX: Final = "sheet:bind:"
CALLBACK_CHECK_SHEET_PREFIX: Final = "sheet:check:"
AWAITING_SHEET_LINK_STATE_PREFIX: Final = "awaiting_sheet_link:"
AWAITING_SHEET_ACCESS_STATE_PREFIX: Final = "awaiting_sheet_access:"
SETTINGS_SECTIONS: Final = {
    "table": "Таблица",
    "clans": "Кланы",
    "composition_columns": "Колонки состава",
    "exited_columns": "Колонки вышедших",
    "cwl_columns": "Колонки CWL",
    "transfer": "Перенос таблицы",
    "delete": "Удалить настройки группы",
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
                "Бот добавлен в группу. Администратор может подключить её "
                "через личный чат с ботом."
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

    async def cancel_private_setup(self, chat_id: int) -> None:
        """Сбрасывает текущую setup-сессию пользователя.

        В коммите 2 отдельная per-user session ещё не хранится. Метод оставлен как
        стабильная точка расширения для следующих шагов настройки.

        Args:
            chat_id: ID личного чата.
        """

        await self._telegram.send_message(chat_id=chat_id, text="Текущая настройка сброшена.")
    async def handle_private_text(self, *, chat_id: int, user_id: int, text: str) -> None:
        """Обрабатывает текст в личке, если бот ждёт ссылку на таблицу.

        Args:
            chat_id: ID личного чата.
            user_id: Telegram user ID.
            text: Текст сообщения.
        """

        pending = await self._telegram_chats.find_pending_sheet_link_setup(
            user_id=user_id,
            state_prefix=AWAITING_SHEET_LINK_STATE_PREFIX,
        )
        if pending is None:
            return

        try:
            spreadsheet_id = extract_spreadsheet_id(text)
        except SheetAdminError as exc:
            await self._telegram.send_message(chat_id=chat_id, text=str(exc))
            return

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
            return

        now = _format_dt(_utc_now())
        async with transaction(self._connection):
            await self._telegram_chats.set_setup_state(
                chat_id=pending.chat_id,
                setup_state=_sheet_access_state(user_id, spreadsheet_id),
                now=now,
            )

        await self._send_sheet_access_instruction(chat_id=chat_id, group_chat_id=pending.chat_id)


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

        group_text = (
            "Группа подключена. Настройка продолжится в личном чате с администратором."
        )
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
                    "У вас пока нет подключённых групп. "
                    "Нажмите «Подключить группу», чтобы начать."
                ),
                reply_markup=main_private_keyboard(),
            )
            return

        buttons = [
            KnownGroupButton(chat_id=item.chat_id, title=item.title)
            for item in known_chats
        ]
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
        if callback_data == CALLBACK_MY_GROUPS:
            await self._telegram.answer_callback_query(callback_query_id, "Принято.")
            await self.send_private_settings(chat_id=chat_id, user_id=user_id)
            return
        if callback_data == CALLBACK_HELP:
            await self._telegram.answer_callback_query(callback_query_id, "Принято.")
            await self.send_help(chat_id=chat_id, is_private=True)
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
        if callback_data.startswith(CALLBACK_CHECK_SHEET_PREFIX):
            await self._check_sheet_access(
                callback_data=callback_data,
                callback_query_id=callback_query_id,
                chat_id=chat_id,
                message_id=message_id,
                user_id=user_id,
            )
            return

        await self._telegram.answer_callback_query(
            callback_query_id,
            "Неизвестная кнопка.",
            show_alert=True,
        )

    async def send_sync_boundary_message(self, *, chat: TelegramChatInfo) -> None:
        """Отправляет временный ответ на `/sync` до реализации sync pipeline.

        Args:
            chat: Данные Telegram-чата.
        """

        if chat.type == "private":
            await self._telegram.send_message(
                chat_id=chat.chat_id,
                text="Команда /sync работает в подключённой группе.",
            )
            return

        status = await self._runtime.get_chat_status(chat.chat_id)
        if status != "ready":
            await self._telegram.send_message(
                chat_id=chat.chat_id,
                text=(
                    "Группа не настроена. Администратор может подключить её "
                    "через личный чат с ботом."
                ),
            )
            return

        await self._telegram.send_message(
            chat_id=chat.chat_id,
            text="Команда /sync будет подключена в следующем этапе разработки.",
        )

    async def send_status_boundary_message(self, *, chat: TelegramChatInfo) -> None:
        """Отправляет временный ответ на `/status` до реализации sync reports.

        Args:
            chat: Данные Telegram-чата.
        """

        if chat.type == "private":
            await self._telegram.send_message(
                chat_id=chat.chat_id,
                text="Команда /status работает в подключённой группе.",
            )
            return

        status = await self._runtime.get_chat_status(chat.chat_id)
        if status is None or status == "disabled":
            await self._telegram.send_message(
                chat_id=chat.chat_id,
                text=(
                    "Группа не настроена. Администратор может подключить её "
                    "через личный чат с ботом."
                ),
            )
            return

        await self._telegram.send_message(
            chat_id=chat.chat_id,
            text="Статус обновлений будет доступен после подключения /sync pipeline.",
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
        title = SETTINGS_SECTIONS.get(section_key, "Раздел")
        text = f"Раздел «{title}» будет реализован в следующем шаге разработки."
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

        await _edit_or_send_message(
            telegram=self._telegram,
            chat_id=chat_id,
            message_id=message_id,
            text="Раздел «Таблица». Вы можете привязать новую Google-таблицу.",
            reply_markup=sheet_section_keyboard(group_chat_id),
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

        if not await self._has_group_settings_access(group_chat_id=group_chat_id, user_id=user_id):
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

        if not await self._has_group_settings_access(group_chat_id=group_chat_id, user_id=user_id):
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
        ],
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

    Returns:
        Telegram inline keyboard.
    """

    return {
        "inline_keyboard": [
            [
                {
                    "text": title,
                    "callback_data": (
                        f"{CALLBACK_SETTINGS_SECTION_PREFIX}"
                        f"{group_chat_id}:{key}"
                    ),
                },
            ]
            for key, title in SETTINGS_SECTIONS.items()
        ],
    }



def sheet_section_keyboard(group_chat_id: int) -> JsonObject:
    """Создаёт клавиатуру раздела таблицы.

    Args:
        group_chat_id: ID Telegram-группы.

    Returns:
        Telegram inline keyboard.
    """

    return {
        "inline_keyboard": [
            [
                {
                    "text": "Привязать таблицу",
                    "callback_data": f"{CALLBACK_BIND_SHEET_PREFIX}{group_chat_id}",
                },
            ],
            [
                {
                    "text": "Назад",
                    "callback_data": f"{CALLBACK_SETTINGS_PREFIX}{group_chat_id}",
                },
            ],
        ],
    }


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
        ],
    }

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
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= _utc_now():
        return "Токен подключения истёк. Создайте новый токен в личном чате."
    return None


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
        await telegram.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)


def _utc_now() -> datetime:
    """Возвращает текущую UTC-дату.

    Returns:
        Timezone-aware UTC datetime.
    """

    return datetime.now(timezone.utc).replace(microsecond=0)


def _format_dt(value: datetime) -> str:
    """Форматирует дату для SQLite.

    Args:
        value: Дата и время.

    Returns:
        ISO-строка с timezone offset.
    """

    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


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
