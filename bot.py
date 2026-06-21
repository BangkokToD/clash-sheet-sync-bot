"""Точка входа Telegram-бота.

Модуль реализует Telegram long polling без тяжёлых фреймворков.
Синхронизация состава подключена через отдельный доменный модуль.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime
from typing import Any, Final
from zoneinfo import ZoneInfo

import httpx

from coc_client import ClashApiUnavailableError, ClashClient
from composition_sync import CompositionDataError, run_composition_sync
from cwl_sync import CwlDataError, run_cwl_sync
from config import ConfigError, load_config
from models import AppConfig, SyncSettings
from settings_store import SettingsStore, SettingsStoreError
from sheets_client import (
    GoogleAccessTokenProvider,
    GoogleSheetsAuthError,
    GoogleSheetsReadError,
    GoogleSheetsWriteError,
    SheetsClient,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# httpx на INFO пишет полные URL, включая Telegram token и Google Sheet ID.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

CALLBACK_UPDATE_COMPOSITION: Final = "update_composition"
CALLBACK_UPDATE_CWL: Final = "update_cwl"
POLLING_TIMEOUT_SECONDS: Final = 35
POLLING_ERROR_SLEEP_SECONDS: Final = 3

JsonObject = dict[str, Any]


class TelegramApiError(RuntimeError):
    """Ошибка Telegram Bot API без вывода секретов в лог."""


class TelegramMessageNotModifiedError(TelegramApiError):
    """Telegram отказался редактировать сообщение без изменений."""


class TelegramClient:
    """Минимальный клиент Telegram Bot API.

    Args:
        token: Токен Telegram Bot API.
        client: Асинхронный HTTP-клиент.
    """

    def __init__(self, token: str, client: httpx.AsyncClient) -> None:
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._client = client

    async def get_updates(self, offset: int | None) -> list[JsonObject]:
        """Получает новые Telegram updates через long polling.

        Args:
            offset: ID следующего update или `None` для первого запроса.

        Returns:
            Список update-объектов Telegram.
        """

        payload: JsonObject = {
            "timeout": POLLING_TIMEOUT_SECONDS,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset

        result = await self._request("getUpdates", payload)
        if not isinstance(result, list):
            raise TelegramApiError("Telegram getUpdates вернул некорректный result.")
        return [item for item in result if isinstance(item, dict)]

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: JsonObject | None = None,
    ) -> None:
        """Отправляет сообщение в Telegram.

        Args:
            chat_id: ID чата.
            text: Текст сообщения.
            reply_markup: Inline-клавиатура или другое Telegram markup.
        """

        payload: JsonObject = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self._request("sendMessage", payload)

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: JsonObject | None = None,
    ) -> None:
        """Редактирует сообщение Telegram.

        Args:
            chat_id: ID чата.
            message_id: ID сообщения.
            text: Новый текст сообщения.
            reply_markup: Inline-клавиатура или другое Telegram markup.
        """

        payload: JsonObject = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self._request("editMessageText", payload)

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        """Отвечает на callback query, чтобы убрать Telegram spinner.

        Args:
            callback_query_id: ID callback query.
            text: Короткий текст ответа.
            show_alert: Нужно ли показать текст в модальном окне Telegram.
        """

        payload: JsonObject = {"callback_query_id": callback_query_id}
        if text is not None:
            payload["text"] = text
        if show_alert:
            payload["show_alert"] = show_alert
        await self._request("answerCallbackQuery", payload)

    async def _request(self, method: str, payload: JsonObject) -> Any:
        """Выполняет Telegram API-запрос.

        Args:
            method: Название метода Telegram Bot API.
            payload: JSON-тело запроса.

        Returns:
            Поле `result` из ответа Telegram.

        Raises:
            TelegramApiError: Если Telegram вернул ошибку или битый JSON.
        """

        try:
            response = await self._client.post(f"{self._base_url}/{method}", json=payload)
        except httpx.HTTPError as exc:
            raise TelegramApiError("Telegram API временно недоступен.") from exc

        try:
            data = response.json()
        except ValueError as exc:
            if response.status_code >= 400:
                raise TelegramApiError(f"Telegram API HTTP {response.status_code}.") from exc
            raise TelegramApiError("Telegram API вернул битый JSON.") from exc

        if not isinstance(data, dict):
            raise TelegramApiError("Telegram API вернул некорректный JSON.")

        if response.status_code >= 400:
            description = data.get("description")
            if not isinstance(description, str):
                description = "неизвестная ошибка"
            if response.status_code == 400 and "message is not modified" in description.lower():
                raise TelegramMessageNotModifiedError("Telegram message is not modified.")
            raise TelegramApiError(f"Telegram API HTTP {response.status_code}: {description}.")

        if data.get("ok") is not True:
            description = data.get("description")
            if not isinstance(description, str):
                description = "неизвестная ошибка"
            raise TelegramApiError(f"Telegram API error: {description}.")
        return data.get("result")


class BotApp:
    """Приложение Telegram-бота.

    Args:
        config: Конфигурация приложения.
        telegram: Клиент Telegram Bot API.
        settings_store: Хранилище статусов ручных запусков.
        clash_client: Клиент Clash of Clans API.
        sheets_client: Клиент Google Sheets API.
    """

    def __init__(
        self,
        config: AppConfig,
        telegram: TelegramClient,
        settings_store: SettingsStore,
        clash_client: ClashClient,
        sheets_client: SheetsClient,
    ) -> None:
        self._config = config
        self._telegram = telegram
        self._settings_store = settings_store
        self._clash_client = clash_client
        self._sheets_client = sheets_client
        self._operation_lock = asyncio.Lock()
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

        chat_id = _extract_chat_id(message)
        user_id = _extract_user_id(message)
        if chat_id is None or user_id is None:
            return

        if not self._is_user_allowed(user_id):
            await self._telegram.send_message(chat_id, "Нет доступа.")
            return

        command = _extract_command(message.get("text"))
        if command == "/start":
            await self._telegram.send_message(chat_id, "Выберите действие.", _main_keyboard())
            return
        if command == "/status":
            await self._send_status(chat_id)
            return

    async def _handle_callback_query(self, callback_query: JsonObject) -> None:
        """Обрабатывает нажатие inline-кнопки.

        Args:
            callback_query: Объект `callback_query` из Telegram update.
        """

        callback_query_id = callback_query.get("id")
        user_id = _extract_user_id(callback_query)
        if not isinstance(callback_query_id, str) or user_id is None:
            return

        if not self._is_user_allowed(user_id):
            await self._telegram.answer_callback_query(
                callback_query_id,
                "Нет доступа.",
                show_alert=True,
            )
            return

        data = callback_query.get("data")
        if data not in {CALLBACK_UPDATE_COMPOSITION, CALLBACK_UPDATE_CWL}:
            await self._telegram.answer_callback_query(
                callback_query_id,
                "Неизвестная кнопка.",
                show_alert=True,
            )
            return

        if self._operation_lock.locked():
            await self._telegram.answer_callback_query(
                callback_query_id,
                "Операция уже выполняется.",
                show_alert=True,
            )
            return

        await self._operation_lock.acquire()
        try:
            await self._telegram.answer_callback_query(callback_query_id, "Принято.")
            if data == CALLBACK_UPDATE_COMPOSITION:
                await self._run_composition_sync(callback_query)
            else:
                await self._run_cwl_sync(callback_query)
        finally:
            self._operation_lock.release()

    async def _send_status(self, chat_id: int) -> None:
        """Отправляет статус последних ручных запусков.

        Args:
            chat_id: ID чата Telegram.
        """

        try:
            settings = self._settings_store.load()
        except SettingsStoreError:
            logger.error("settings read failed")
            await self._telegram.send_message(chat_id, "Не удалось прочитать статус.")
            return

        await self._telegram.send_message(chat_id, _format_status(settings))

    async def _edit_or_send_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: JsonObject | None = None,
    ) -> None:
        """Редактирует сообщение или отправляет новое при невозможности редактирования.

        Telegram возвращает `message is not modified`, если новый текст и клавиатура
        совпадают со старым сообщением. Для пользователя это выглядит как отсутствие
        реакции на кнопку, поэтому в этом случае отправляем отдельное сообщение.

        Args:
            chat_id: ID чата Telegram.
            message_id: ID сообщения для редактирования.
            text: Новый текст сообщения.
            reply_markup: Inline-клавиатура.
        """

        try:
            await self._telegram.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )
        except TelegramMessageNotModifiedError:
            await self._telegram.send_message(chat_id, text, reply_markup)
        except TelegramApiError as exc:
            logger.warning("telegram edit failed, sending new message: %s", exc)
            await self._telegram.send_message(chat_id, text, reply_markup)

    async def _run_composition_sync(self, callback_query: JsonObject) -> None:
        """Запускает реальное обновление листа состава.

        Args:
            callback_query: Объект `callback_query` из Telegram update.
        """

        message = callback_query.get("message")
        if not isinstance(message, dict):
            return

        chat_id = _extract_chat_id(message)
        message_id = message.get("message_id")
        if chat_id is None or not isinstance(message_id, int):
            return

        now_dt = datetime.now(ZoneInfo(self._config.timezone)).replace(microsecond=0)
        now = now_dt.isoformat()

        try:
            settings = self._settings_store.load()
        except SettingsStoreError:
            logger.error("settings read failed")
            await self._edit_or_send_message(
                chat_id=chat_id,
                message_id=message_id,
                text="Не удалось прочитать статус синхронизации.\n\nВыберите действие.",
                reply_markup=_main_keyboard(),
            )
            return

        logger.info("composition sync started")
        status_error: str | None = None

        try:
            result = await run_composition_sync(
                config=self._config,
                clash_client=self._clash_client,
                sheets_client=self._sheets_client,
                detected_at=now_dt,
            )
        except ClashApiUnavailableError as exc:
            logger.info("api unavailable: %s", exc)
            logger.info("composition sync failed: api unavailable")
            result_text = "API недоступно."
            status_error = result_text
        except CompositionDataError as exc:
            logger.info("composition sync failed: %s", exc)
            result_text = f"Обновление состава отменено.\n\nПричина: {exc}"
            status_error = str(exc)
        except (GoogleSheetsAuthError, GoogleSheetsReadError) as exc:
            logger.error("google sheets read failed: %s", exc)
            logger.info("composition sync failed: google sheets read failed")
            result_text = "Не удалось прочитать Google Sheets."
            status_error = result_text
        except GoogleSheetsWriteError as exc:
            logger.error("google sheets write failed: %s", exc)
            logger.info("composition sync failed: google sheets write failed")
            result_text = "Не удалось записать Google Sheets."
            status_error = result_text
        else:
            logger.info("composition sync finished")
            result_text = result.to_telegram_message()

        if status_error is None:
            settings = replace(
                settings,
                last_composition_sync_at=now,
                last_composition_sync_status="success",
                last_composition_sync_error=None,
            )
        else:
            settings = replace(
                settings,
                last_composition_sync_at=now,
                last_composition_sync_status="error",
                last_composition_sync_error=status_error,
            )

        try:
            self._settings_store.save(settings)
        except SettingsStoreError:
            logger.error("settings write failed")
            result_text = f"{result_text}\n\nСтатус синхронизации не записан."

        await self._edit_or_send_message(
            chat_id=chat_id,
            message_id=message_id,
            text=f"{result_text}\n\nВыберите действие.",
            reply_markup=_main_keyboard(),
        )

    async def _run_cwl_sync(self, callback_query: JsonObject) -> None:
        """Запускает реальное обновление листа CWL.

        Args:
            callback_query: Объект `callback_query` из Telegram update.
        """

        message = callback_query.get("message")
        if not isinstance(message, dict):
            return

        chat_id = _extract_chat_id(message)
        message_id = message.get("message_id")
        if chat_id is None or not isinstance(message_id, int):
            return

        now_dt = datetime.now(ZoneInfo(self._config.timezone)).replace(microsecond=0)
        now = now_dt.isoformat()

        try:
            settings = self._settings_store.load()
        except SettingsStoreError:
            logger.error("settings read failed")
            await self._edit_or_send_message(
                chat_id=chat_id,
                message_id=message_id,
                text="Не удалось прочитать статус синхронизации.\n\nВыберите действие.",
                reply_markup=_main_keyboard(),
            )
            return

        logger.info("cwl sync started")
        status_error: str | None = None

        try:
            result = await run_cwl_sync(
                config=self._config,
                clash_client=self._clash_client,
                sheets_client=self._sheets_client,
            )
        except ClashApiUnavailableError as exc:
            logger.info("api unavailable: %s", exc)
            logger.info("cwl sync failed: api unavailable")
            result_text = "API недоступно."
            status_error = result_text
        except CwlDataError as exc:
            logger.info("cwl sync failed: %s", exc)
            result_text = f"Обновление CWL отменено.\n\nПричина: {exc}"
            status_error = str(exc)
        except (GoogleSheetsAuthError, GoogleSheetsReadError) as exc:
            logger.error("google sheets read failed: %s", exc)
            logger.info("cwl sync failed: google sheets read failed")
            result_text = "Не удалось прочитать Google Sheets."
            status_error = result_text
        except GoogleSheetsWriteError as exc:
            logger.error("google sheets write failed: %s", exc)
            logger.info("cwl sync failed: google sheets write failed")
            result_text = "Не удалось записать Google Sheets."
            status_error = result_text
        else:
            logger.info("cwl sync finished")
            result_text = result.to_telegram_message()

        if status_error is None:
            settings = replace(
                settings,
                last_cwl_sync_at=now,
                last_cwl_sync_status="success",
                last_cwl_sync_error=None,
            )
        else:
            settings = replace(
                settings,
                last_cwl_sync_at=now,
                last_cwl_sync_status="error",
                last_cwl_sync_error=status_error,
            )

        try:
            self._settings_store.save(settings)
        except SettingsStoreError:
            logger.error("settings write failed")
            result_text = f"{result_text}\n\nСтатус синхронизации не записан."

        await self._edit_or_send_message(
            chat_id=chat_id,
            message_id=message_id,
            text=f"{result_text}\n\nВыберите действие.",
            reply_markup=_main_keyboard(),
        )

    async def _run_not_implemented_sync(
        self,
        callback_query: JsonObject,
        callback_data: str,
    ) -> None:
        """Фиксирует нажатие кнопки до появления реальной синхронизации.

        Args:
            callback_query: Объект `callback_query` из Telegram update.
            callback_data: Тип запрошенной операции.
        """

        message = callback_query.get("message")
        if not isinstance(message, dict):
            return

        chat_id = _extract_chat_id(message)
        message_id = message.get("message_id")
        if chat_id is None or not isinstance(message_id, int):
            return

        now = datetime.now(ZoneInfo(self._config.timezone)).replace(microsecond=0).isoformat()
        try:
            settings = self._settings_store.load()
        except SettingsStoreError:
            logger.error("settings read failed")
            await self._edit_or_send_message(
                chat_id=chat_id,
                message_id=message_id,
                text="Не удалось прочитать статус синхронизации.\n\nВыберите действие.",
                reply_markup=_main_keyboard(),
            )
            return

        if callback_data == CALLBACK_UPDATE_COMPOSITION:
            logger.info("composition sync started")
            error_text = "Синхронизация состава ещё не реализована."
            settings = replace(
                settings,
                last_composition_sync_at=now,
                last_composition_sync_status="error",
                last_composition_sync_error=error_text,
            )
            result_text = (
                "Обновление состава недоступно.\n\n"
                f"Причина: {error_text}\n"
                f"Время запуска: {now}"
            )
            logger.info("composition sync failed: not implemented")
        else:
            logger.info("cwl sync started")
            error_text = "Синхронизация CWL ещё не реализована."
            settings = replace(
                settings,
                last_cwl_sync_at=now,
                last_cwl_sync_status="error",
                last_cwl_sync_error=error_text,
            )
            result_text = (
                "Обновление CWL недоступно.\n\n"
                f"Причина: {error_text}\n"
                f"Время запуска: {now}"
            )
            logger.info("cwl sync failed: not implemented")

        try:
            self._settings_store.save(settings)
        except SettingsStoreError:
            logger.error("settings write failed")
            result_text = (
                "Не удалось записать статус синхронизации.\n"
                f"Время запуска: {now}"
            )

        await self._edit_or_send_message(
            chat_id=chat_id,
            message_id=message_id,
            text=f"{result_text}\n\nВыберите действие.",
            reply_markup=_main_keyboard(),
        )

    def _is_user_allowed(self, user_id: int) -> bool:
        """Проверяет доступ пользователя к боту.

        Args:
            user_id: Telegram user ID.

        Returns:
            `True`, если пользователь разрешён через `.env`.
        """

        return user_id in self._config.telegram_allowed_user_ids


def _main_keyboard() -> JsonObject:
    """Создаёт главное меню inline-кнопок.

    Returns:
        Telegram reply markup с двумя кнопками управления.
    """

    return {
        "inline_keyboard": [
            [{"text": "Обновить состав", "callback_data": CALLBACK_UPDATE_COMPOSITION}],
            [{"text": "Обновить CWL", "callback_data": CALLBACK_UPDATE_CWL}],
        ],
    }


def _format_status(settings: SyncSettings) -> str:
    """Форматирует статус последних ручных запусков.

    Args:
        settings: Состояние `sync_settings.json`.

    Returns:
        Текст команды `/status`.
    """

    return "\n".join(
        [
            "Последнее обновление состава: "
            + _format_datetime(settings.last_composition_sync_at),
            "Статус состава: " + _format_sync_status(settings.last_composition_sync_status),
            "Ошибка состава: " + _format_error(settings.last_composition_sync_error),
            "",
            "Последнее обновление CWL: " + _format_datetime(settings.last_cwl_sync_at),
            "Статус CWL: " + _format_sync_status(settings.last_cwl_sync_status),
            "Ошибка CWL: " + _format_error(settings.last_cwl_sync_error),
        ],
    )


def _format_datetime(value: str | None) -> str:
    """Форматирует дату запуска для Telegram.

    Args:
        value: ISO-дата или `None`.

    Returns:
        Дата или текст для отсутствующего запуска.
    """

    return value or "ещё не запускалось"


def _format_sync_status(value: str | None) -> str:
    """Форматирует статус запуска для Telegram.

    Args:
        value: Технический статус из `sync_settings.json`.

    Returns:
        Человекочитаемый статус.
    """

    if value == "success":
        return "успешно"
    if value == "error":
        return "ошибка"
    return "-"


def _format_error(value: str | None) -> str:
    """Форматирует ошибку запуска для Telegram.

    Args:
        value: Текст ошибки или `None`.

    Returns:
        Текст ошибки или `-`.
    """

    return value or "-"


def _extract_command(raw_text: object) -> str | None:
    """Извлекает Telegram-команду из текста сообщения.

    Args:
        raw_text: Значение поля `message.text`.

    Returns:
        Команда без mention бота или `None`.
    """

    if not isinstance(raw_text, str):
        return None
    first_token = raw_text.strip().split(maxsplit=1)[0] if raw_text.strip() else ""
    if not first_token.startswith("/"):
        return None
    return first_token.split("@", maxsplit=1)[0]


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


def _extract_chat_id(message: JsonObject) -> int | None:
    """Извлекает Telegram chat ID из message.

    Args:
        message: Объект `message` Telegram Bot API.

    Returns:
        Telegram chat ID или `None`.
    """

    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None
    chat_id = chat.get("id")
    return chat_id if isinstance(chat_id, int) else None


async def async_main() -> int:
    """Запускает Telegram-бота.

    Returns:
        Код завершения процесса.
    """

    try:
        config = load_config()
        settings_store = SettingsStore(config.sync_settings_file)
        settings_store.load()
        token_provider = GoogleAccessTokenProvider(config.google_service_account_file)
    except (ConfigError, SettingsStoreError, GoogleSheetsAuthError) as exc:
        logger.error("bot startup failed: %s", exc)
        return 1

    logger.info("bot started")

    timeout = httpx.Timeout(POLLING_TIMEOUT_SECONDS + 10, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as http_client:
        telegram = TelegramClient(config.telegram_bot_token, http_client)
        clash_client = ClashClient(config.coc_api_token, http_client)
        sheets_client = SheetsClient(config.google_sheet_id, token_provider, http_client)
        app = BotApp(
            config=config,
            telegram=telegram,
            settings_store=settings_store,
            clash_client=clash_client,
            sheets_client=sheets_client,
        )
        await app.run_polling()
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
