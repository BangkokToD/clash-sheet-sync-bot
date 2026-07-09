"""Repository для связей пользователя с известными группами."""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite

from clash_sheet_sync_bot.models import TelegramChatStatus

from .base import as_chat_status, as_int, as_str, fetch_all


@dataclass(frozen=True, slots=True)
class KnownAdminChat:
    """Группа, известная пользователю через setup-flow.

    Attributes:
        chat_id: ID Telegram-чата.
        title: Название чата.
        type: Тип Telegram-чата.
        status: Статус настройки чата.
        linked_at: Дата создания связи с админом.
    """

    chat_id: int
    title: str
    type: str
    status: TelegramChatStatus
    linked_at: str


class AdminChatRepository:
    """Repository для связей пользователя с известными группами.

    Args:
        connection: Открытое SQLite-подключение.
    """

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def list_known_chats(self, user_id: int) -> tuple[KnownAdminChat, ...]:
        """Возвращает группы, известные пользователю через setup-flow."""

        rows = await fetch_all(
            self._connection,
            """
            SELECT c.chat_id, c.title, c.type, c.status, l.linked_at
            FROM chat_admin_links AS l
            JOIN telegram_chats AS c ON c.chat_id = l.chat_id
            WHERE l.user_id = ? AND l.is_active = 1
            ORDER BY l.linked_at DESC
            """,
            (user_id,),
        )
        return tuple(
            KnownAdminChat(
                chat_id=as_int(row["chat_id"], "chat_id"),
                title=as_str(row["title"], "title"),
                type=as_str(row["type"], "type"),
                status=as_chat_status(row["status"]),
                linked_at=as_str(row["linked_at"], "linked_at"),
            )
            for row in rows
        )
