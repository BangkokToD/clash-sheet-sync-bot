"""Fake Telegram/access объекты для setup-flow и sync tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from clash_sheet_sync_bot.telegram.access import AdminCheckResult
from clash_sheet_sync_bot.telegram.client import TelegramMessageNotModifiedError


@dataclass(slots=True)
class FakeTelegram:
    """Fake Telegram client для setup-flow tests."""

    send_error: Exception | None = None
    raise_not_modified_on_edit: bool = False
    sent_messages: list[dict[str, Any]] = field(default_factory=list)
    edit_attempts: list[dict[str, Any]] = field(default_factory=list)
    edited_messages: list[dict[str, Any]] = field(default_factory=list)
    answered_callbacks: list[dict[str, Any]] = field(default_factory=list)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: Any | None = None,
        *,
        parse_mode: str | None = None,
        disable_web_page_preview: bool | None = None,
    ) -> int:
        """Запоминает отправленное сообщение."""

        message_id = len(self.sent_messages) + 1
        self.sent_messages.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
                "parse_mode": parse_mode,
                "disable_web_page_preview": disable_web_page_preview,
            },
        )
        if self.send_error is not None:
            raise self.send_error
        return message_id

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: Any | None = None,
        *,
        parse_mode: str | None = None,
        disable_web_page_preview: bool | None = None,
    ) -> None:
        """Запоминает попытку редактирования сообщения."""

        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "reply_markup": reply_markup,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }
        self.edit_attempts.append(payload)
        if self.raise_not_modified_on_edit:
            raise TelegramMessageNotModifiedError("Telegram message is not modified.")
        self.edited_messages.append(payload)
        for sent_message in self.sent_messages:
            if sent_message.get("message_id") == message_id:
                sent_message.update(payload)
                break

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        *,
        show_alert: bool = False,
    ) -> None:
        """Запоминает callback answer."""

        self.answered_callbacks.append(
            {
                "callback_query_id": callback_query_id,
                "text": text,
                "show_alert": show_alert,
            },
        )


@dataclass(slots=True)
class RecordingAccessService:
    """Fake access service с записью force_refresh."""

    is_admin_result: bool = True
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def is_admin(
        self,
        *,
        chat_id: int,
        user_id: int,
        force_refresh: bool = False,
    ) -> AdminCheckResult:
        """Запоминает admin-check и возвращает заданный результат."""

        self.calls.append(
            {
                "chat_id": chat_id,
                "user_id": user_id,
                "force_refresh": force_refresh,
            },
        )
        return AdminCheckResult(is_admin=self.is_admin_result, from_cache=False)
