"""Точка входа публичного Telegram-бота."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Final

import httpx

from config import ConfigError, load_config
from migrations import apply_migrations
from models import AppConfig
from setup_flow import SetupFlow, TelegramChatInfo
from storage import Database, StorageError
from sync_service import SyncChatInfo, SyncService
from telegram_access import TelegramAccessService
from telegram_client import (
    POLLING_TIMEOUT_SECONDS,
    JsonObject,
    TelegramApiError,
    TelegramClient,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# httpx на INFO пишет полные URL, включая Telegram token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

POLLING_ERROR_SLEEP_SECONDS: Final = 3


@dataclass(frozen=True, slots=True)
class TelegramCommand:
    """Команда Telegram из текстового сообщения.

    Attributes:
        name: Команда без mention, например `/start`.
        args: Текст после команды без пробелов по краям.
    """

    name: str
    args: str


class BotApp:
    """Приложение Telegram-бота.

    Args:
        config: Глобальная конфигурация приложения.
        telegram: Клиент Telegram Bot API.
        connection: SQLite-подключение.
        bot_username: Username бота без `@` или `None`.
    """

    def __init__(
        self,
        *,
        config: AppConfig,
        telegram: TelegramClient,
        connection: Any,
        bot_username: str | None,
    ) -> None:
        self._config = config
        self._telegram = telegram
        self._connection = connection
        self._bot_username = bot_username
        self._update_tasks: set[asyncio.Task[None]] = set()

    async def run_polling(self) -> None:
        """Запускает Telegram long polling."""

        logger.info("telegram polling started")
        offset: int | None = None

        while True:
            try:
                updates = await self._telegram.get_updates(offset)
            except TelegramApiError as exc:
                logger.warning("telegram polling failed: %s", exc)
                await asyncio.sleep(POLLING_ERROR_SLEEP_SECONDS)
                continue

            for update in updates:
                update_id = update.get("update_id")
                if not isinstance(update_id, int):
                    continue
                offset = update_id + 1
                self._track_update_task(asyncio.create_task(self._handle_update(update)))

    def _track_update_task(self, task: asyncio.Task[None]) -> None:
        """Запоминает фоновую задачу обработки update.

        Args:
            task: Задача обработки одного Telegram update.
        """

        self._update_tasks.add(task)
        task.add_done_callback(self._handle_update_task_done)

    def _handle_update_task_done(self, task: asyncio.Task[None]) -> None:
        """Логирует ошибку фоновой обработки update.

        Args:
            task: Завершённая задача обработки update.
        """

        self._update_tasks.discard(task)
        try:
            task.result()
        except TelegramApiError as exc:
            logger.warning("telegram update handling failed: %s", exc)
        except Exception:
            logger.exception("telegram update handling failed")

    async def _handle_update(self, update: JsonObject) -> None:
        """Обрабатывает один Telegram update.

        Args:
            update: Update-объект Telegram Bot API.
        """

        message = update.get("message")
        if isinstance(message, dict):
            await self._handle_message(message)
            return

        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            await self._handle_callback_query(callback_query)

    async def _handle_message(self, message: JsonObject) -> None:
        """Обрабатывает обычное Telegram-сообщение.

        Args:
            message: Объект `message` из Telegram update.
        """

        chat = _extract_chat_info(message)
        user_id = _extract_user_id(message)
        if chat is None or user_id is None:
            return

        flow = self._setup_flow()
        is_private = chat.type == "private"
        raw_text = message.get("text")
        command = _extract_command(raw_text, self._bot_username)
        if command is None:
            if is_private and isinstance(raw_text, str):
                await flow.handle_private_text(
                    chat_id=chat.chat_id,
                    user_id=user_id,
                    text=raw_text,
                )
            return

        if command.name == "/start":
            if is_private:
                await flow.send_private_start(chat.chat_id)
            else:
                await flow.send_group_start(chat.chat_id)
            return

        if command.name == "/help":
            await flow.send_help(chat.chat_id, is_private=is_private)
            return

        if command.name == "/cancel":
            if is_private:
                await flow.cancel_private_setup(chat_id=chat.chat_id, user_id=user_id)
            else:
                await self._telegram.send_message(
                    chat_id=chat.chat_id,
                    text="Команда /cancel работает в личном чате с ботом.",
                )
            return

        if command.name == "/connect":
            await flow.connect_group(chat=chat, user_id=user_id, raw_token=command.args)
            return

        if command.name == "/settings":
            if is_private:
                await flow.send_private_settings(chat_id=chat.chat_id, user_id=user_id)
            else:
                await flow.send_group_settings_pointer(chat=chat, user_id=user_id)
            return

        if command.name == "/accept_transfer":
            await flow.accept_transfer(chat=chat, user_id=user_id, raw_token=command.args)
            return

        if command.name == "/sync":
            await self._sync_service().handle_sync_command(
                chat=SyncChatInfo(chat_id=chat.chat_id, type=chat.type),
                user_id=user_id,
            )
            return

        if command.name == "/status":
            await self._sync_service().handle_status_command(
                chat=SyncChatInfo(chat_id=chat.chat_id, type=chat.type),
            )
            return

    async def _handle_callback_query(self, callback_query: JsonObject) -> None:
        """Обрабатывает нажатие inline-кнопки.

        Args:
            callback_query: Объект `callback_query` из Telegram update.
        """

        callback_query_id = callback_query.get("id")
        user_id = _extract_user_id(callback_query)
        data = callback_query.get("data")
        message = callback_query.get("message")
        if (
            not isinstance(callback_query_id, str)
            or user_id is None
            or not isinstance(data, str)
            or not isinstance(message, dict)
        ):
            return

        chat = _extract_chat_info(message)
        message_id = message.get("message_id")
        if chat is None or not isinstance(message_id, int):
            return

        flow = self._setup_flow()
        await flow.handle_callback(
            callback_data=data,
            callback_query_id=callback_query_id,
            chat_id=chat.chat_id,
            message_id=message_id,
            user_id=user_id,
        )

    def _setup_flow(self) -> SetupFlow:
        """Создаёт setup-flow поверх текущего SQLite-подключения.

        Returns:
            Сервис Telegram setup-flow.
        """

        access = TelegramAccessService(
            telegram=self._telegram,
            connection=self._connection,
            admin_cache_ttl_seconds=self._config.admin_cache_ttl_seconds,
        )
        return SetupFlow(
            config=self._config,
            telegram=self._telegram,
            connection=self._connection,
            access=access,
            bot_username=self._bot_username,
        )

    def _sync_service(self) -> SyncService:
        """Создаёт sync-service поверх текущего SQLite-подключения."""

        return SyncService(
            config=self._config,
            telegram=self._telegram,
            connection=self._connection,
        )



def _extract_command(raw_text: object, bot_username: str | None) -> TelegramCommand | None:
    """Извлекает Telegram-команду из текста сообщения.

    Args:
        raw_text: Значение поля `message.text`.
        bot_username: Username текущего бота без `@` или `None`.

    Returns:
        Команда без mention бота или `None`.
    """

    if not isinstance(raw_text, str):
        return None

    stripped = raw_text.strip()
    if stripped == "":
        return None

    first_token, _, args = stripped.partition(" ")
    if not first_token.startswith("/"):
        return None

    command_name, _, mention = first_token.partition("@")
    if mention and bot_username is not None and mention.lower() != bot_username.lower():
        return None

    return TelegramCommand(name=command_name, args=args.strip())


def _extract_user_id(payload: JsonObject) -> int | None:
    """Извлекает Telegram user ID из message или callback_query.

    Args:
        payload: Объект Telegram с полем `from`.

    Returns:
        Telegram user ID или `None`.
    """

    sender = payload.get("from")
    if not isinstance(sender, dict):
        return None
    user_id = sender.get("id")
    return user_id if isinstance(user_id, int) else None


def _extract_chat_info(message: JsonObject) -> TelegramChatInfo | None:
    """Извлекает Telegram chat info из message.

    Args:
        message: Объект `message` Telegram Bot API.

    Returns:
        Данные чата или `None`.
    """

    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None

    chat_id = chat.get("id")
    chat_type = chat.get("type")
    if not isinstance(chat_id, int) or not isinstance(chat_type, str):
        return None

    title = chat.get("title")
    first_name = chat.get("first_name")
    username = chat.get("username")
    if isinstance(title, str) and title.strip():
        display_title = title.strip()
    elif isinstance(first_name, str) and first_name.strip():
        display_title = first_name.strip()
    elif isinstance(username, str) and username.strip():
        display_title = username.strip()
    else:
        display_title = str(chat_id)

    return TelegramChatInfo(chat_id=chat_id, title=display_title, type=chat_type)


async def async_main() -> int:
    """Запускает Telegram-бота.

    Returns:
        Код завершения процесса.
    """

    try:
        config = load_config()
    except ConfigError as exc:
        logger.error("bot startup failed: %s", exc)
        return 1

    database = Database(config.db_path)
    timeout = httpx.Timeout(POLLING_TIMEOUT_SECONDS + 10, connect=10.0)

    try:
        async with database.connect() as connection:
            await apply_migrations(connection)
            async with httpx.AsyncClient(timeout=timeout) as http_client:
                telegram = TelegramClient(config.telegram_bot_token, http_client)
                identity = await telegram.get_me()
                logger.info("bot started")
                app = BotApp(
                    config=config,
                    telegram=telegram,
                    connection=connection,
                    bot_username=identity.username,
                )
                await app.run_polling()
    except (StorageError, TelegramApiError) as exc:
        logger.error("bot startup failed: %s", exc)
        return 1

    return 0


def main() -> int:
    """Синхронная точка входа приложения.

    Returns:
        Код завершения процесса.
    """

    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        logger.info("bot stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
