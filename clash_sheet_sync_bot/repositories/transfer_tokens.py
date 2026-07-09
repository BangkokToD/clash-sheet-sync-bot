"""Repository transfer-токенов."""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite

from .base import as_int, as_optional_str, as_str, fetch_one


@dataclass(frozen=True, slots=True)
class TransferToken:
    """Одноразовый токен переноса таблицы на другой чат.

    Attributes:
        token: Секретная часть команды `/accept_transfer`.
        source_chat_id: ID исходного Telegram-чата.
        created_by_user_id: Telegram user ID создателя токена.
        expires_at: ISO-дата истечения токена.
        used_at: ISO-дата использования или `None`.
        created_at: ISO-дата создания.
    """

    token: str
    source_chat_id: int
    created_by_user_id: int
    expires_at: str
    used_at: str | None
    created_at: str


class TransferTokenRepository:
    """Repository токенов переноса таблицы."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def create_transfer_token(
        self,
        *,
        token: str,
        source_chat_id: int,
        created_by_user_id: int,
        expires_at: str,
        created_at: str,
    ) -> None:
        """Создаёт одноразовый transfer token."""

        await self._connection.execute(
            """
            INSERT INTO transfer_tokens(
                token, source_chat_id, created_by_user_id, expires_at, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (token, source_chat_id, created_by_user_id, expires_at, created_at),
        )

    async def get_transfer_token(self, token: str) -> TransferToken | None:
        """Читает transfer token по секретному значению."""

        row = await fetch_one(
            self._connection,
            """
            SELECT token, source_chat_id, created_by_user_id, expires_at, used_at, created_at
            FROM transfer_tokens
            WHERE token = ?
            """,
            (token,),
        )
        if row is None:
            return None
        return TransferToken(
            token=as_str(row["token"], "token"),
            source_chat_id=as_int(row["source_chat_id"], "source_chat_id"),
            created_by_user_id=as_int(row["created_by_user_id"], "created_by_user_id"),
            expires_at=as_str(row["expires_at"], "expires_at"),
            used_at=as_optional_str(row["used_at"], "used_at"),
            created_at=as_str(row["created_at"], "created_at"),
        )

    async def mark_transfer_token_used(self, *, token: str, used_at: str) -> bool:
        """Помечает transfer token использованным."""

        cursor = await self._connection.execute(
            """
            UPDATE transfer_tokens
            SET used_at = ?
            WHERE token = ? AND used_at IS NULL
            """,
            (used_at, token),
        )
        return cursor.rowcount == 1
