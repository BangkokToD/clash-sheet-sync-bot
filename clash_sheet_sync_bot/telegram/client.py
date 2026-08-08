"""Минимальный клиент Telegram Bot API через `httpx`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import httpx

POLLING_TIMEOUT_SECONDS: Final = 35

JsonObject = dict[str, Any]


class TelegramApiError(RuntimeError):
    """Ошибка Telegram Bot API без вывода секретов в лог."""


class TelegramBadRequestError(TelegramApiError):
    """Telegram Bot API ответил HTTP 400."""


class TelegramMessageNotModifiedError(TelegramBadRequestError):
    """Telegram отказался редактировать сообщение без изменений."""


class TelegramChatMigratedError(TelegramBadRequestError):
    """Telegram сообщил, что basic group преобразована в supergroup."""

    def __init__(self, message: str, *, new_chat_id: int) -> None:
        super().__init__(message)
        self.new_chat_id = new_chat_id


@dataclass(frozen=True, slots=True)
class TelegramBotIdentity:
    """Короткая информация о Telegram-боте.

    Attributes:
        id: Telegram user ID бота.
        username: Username бота без `@` или `None`.
    """

    id: int
    username: str | None


@dataclass(frozen=True, slots=True)
class TelegramChatMember:
    """Результат `getChatMember`.

    Attributes:
        status: Статус пользователя в чате.
    """

    status: str


@dataclass(frozen=True, slots=True)
class TelegramInviteLink:
    """Invite link created by the bot for a private group."""

    invite_link: str


@dataclass(frozen=True, slots=True)
class TelegramMessageEntity:
    """Entity Telegram Bot API с offsets в UTF-16 code units."""

    type: str
    offset: int
    length: int
    custom_emoji_id: str | None = None

    def to_payload(self) -> JsonObject:
        """Преобразует entity в JSON payload Telegram."""

        payload: JsonObject = {"type": self.type, "offset": self.offset, "length": self.length}
        if self.custom_emoji_id is not None:
            payload["custom_emoji_id"] = self.custom_emoji_id
        return payload


class TelegramClient:
    """Минимальный клиент Telegram Bot API.

    Args:
        token: Токен Telegram Bot API.
        client: Асинхронный HTTP-клиент.
    """

    def __init__(self, token: str, client: httpx.AsyncClient) -> None:
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._client = client

    async def get_me(self) -> TelegramBotIdentity:
        """Получает identity текущего бота.

        Returns:
            ID и username бота.
        """

        result = await self._request("getMe", {})
        if not isinstance(result, dict):
            raise TelegramApiError("Telegram getMe вернул некорректный result.")

        bot_id = result.get("id")
        username = result.get("username")
        if not isinstance(bot_id, int):
            raise TelegramApiError("Telegram getMe не вернул id бота.")
        if username is not None and not isinstance(username, str):
            raise TelegramApiError("Telegram getMe вернул некорректный username.")
        return TelegramBotIdentity(id=bot_id, username=username)

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
        *,
        parse_mode: str | None = None,
        disable_web_page_preview: bool | None = None,
        entities: tuple[TelegramMessageEntity, ...] | None = None,
    ) -> int | None:
        """Отправляет сообщение в Telegram.

        Args:
            chat_id: ID чата.
            text: Текст сообщения.
            reply_markup: Inline-клавиатура или другое Telegram markup.
            parse_mode: Режим разметки Telegram, например `HTML`.
            disable_web_page_preview: Нужно ли отключить preview ссылок.
        """

        payload = _message_payload(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
            entities=entities,
        )
        result = await self._request("sendMessage", payload)
        if isinstance(result, dict):
            message_id = result.get("message_id")
            if isinstance(message_id, int):
                return message_id
        return None

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: JsonObject | None = None,
        *,
        parse_mode: str | None = None,
        disable_web_page_preview: bool | None = None,
        entities: tuple[TelegramMessageEntity, ...] | None = None,
    ) -> None:
        """Редактирует сообщение Telegram.

        Args:
            chat_id: ID чата.
            message_id: ID сообщения.
            text: Новый текст сообщения.
            reply_markup: Inline-клавиатура или другое Telegram markup.
            parse_mode: Режим разметки Telegram, например `HTML`.
            disable_web_page_preview: Нужно ли отключить preview ссылок.
        """

        payload = _message_payload(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
            entities=entities,
        )
        payload["message_id"] = message_id
        await self._request("editMessageText", payload)

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        *,
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

    async def get_chat_member(self, chat_id: int, user_id: int) -> TelegramChatMember:
        """Получает статус пользователя в Telegram-чате.

        Args:
            chat_id: ID группы или супергруппы.
            user_id: Telegram user ID.

        Returns:
            Статус пользователя в чате.
        """

        result = await self._request(
            "getChatMember",
            {"chat_id": chat_id, "user_id": user_id},
        )
        if not isinstance(result, dict):
            raise TelegramApiError("Telegram getChatMember вернул некорректный result.")
        status = result.get("status")
        if not isinstance(status, str):
            raise TelegramApiError("Telegram getChatMember не вернул status.")
        return TelegramChatMember(status=status)

    async def create_chat_invite_link(self, chat_id: int, *, name: str) -> TelegramInviteLink:
        """Creates a non-expiring invite link for a support group."""

        result = await self._request(
            "createChatInviteLink",
            {"chat_id": chat_id, "name": name},
        )
        if not isinstance(result, dict):
            raise TelegramApiError("Telegram createChatInviteLink вернул некорректный result.")
        invite_link = result.get("invite_link")
        if not isinstance(invite_link, str) or not invite_link.startswith("https://"):
            raise TelegramApiError("Telegram createChatInviteLink не вернул ссылку.")
        return TelegramInviteLink(invite_link=invite_link)

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
            if response.status_code == 400:
                parameters = data.get("parameters")
                if isinstance(parameters, dict):
                    new_chat_id = parameters.get("migrate_to_chat_id")
                    if isinstance(new_chat_id, int) and not isinstance(new_chat_id, bool):
                        raise TelegramChatMigratedError(
                            "Telegram group was upgraded to a supergroup.",
                            new_chat_id=new_chat_id,
                        )
                raise TelegramBadRequestError(f"Telegram API HTTP 400: {description}.")
            raise TelegramApiError(f"Telegram API HTTP {response.status_code}: {description}.")

        if data.get("ok") is not True:
            description = data.get("description")
            if not isinstance(description, str):
                description = "неизвестная ошибка"
            raise TelegramApiError(f"Telegram API error: {description}.")
        return data.get("result")


def _message_payload(
    *,
    chat_id: int,
    text: str,
    reply_markup: JsonObject | None,
    parse_mode: str | None,
    disable_web_page_preview: bool | None,
    entities: tuple[TelegramMessageEntity, ...] | None,
) -> JsonObject:
    """Собирает payload для отправки или редактирования сообщения.

    Args:
        chat_id: ID чата.
        text: Текст сообщения.
        reply_markup: Inline-клавиатура или `None`.
        parse_mode: Режим разметки Telegram.
        disable_web_page_preview: Нужно ли отключить preview ссылок.

    Returns:
        JSON-совместимый payload Telegram Bot API.
    """

    if parse_mode is not None and entities is not None:
        raise ValueError("parse_mode и entities нельзя использовать одновременно.")
    payload: JsonObject = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    if disable_web_page_preview is not None:
        payload["disable_web_page_preview"] = disable_web_page_preview
    if entities is not None:
        payload["entities"] = [entity.to_payload() for entity in entities]
    return payload
