"""Repository setup-токенов."""

from __future__ import annotations

import aiosqlite

from models import SetupToken

from .base import as_int, as_optional_int, as_optional_str, as_str, fetch_one


class SetupTokenRepository:
    """Repository одноразовых setup-токенов.

    Args:
        connection: Открытое SQLite-подключение.
    """

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def create_setup_token(
        self,
        *,
        token: str,
        created_by_user_id: int,
        expires_at: str,
        created_at: str,
    ) -> None:
        """Создаёт setup-токен."""

        await self._connection.execute(
            """
            INSERT INTO setup_tokens(token, created_by_user_id, expires_at, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (token, created_by_user_id, expires_at, created_at),
        )

    async def get_setup_token(self, token: str) -> SetupToken | None:
        """Читает setup-токен по секретному значению."""

        row = await fetch_one(
            self._connection,
            """
            SELECT token, created_by_user_id, expires_at, used_chat_id, used_at, created_at
            FROM setup_tokens
            WHERE token = ?
            """,
            (token,),
        )
        if row is None:
            return None
        return SetupToken(
            token=as_str(row["token"], "token"),
            created_by_user_id=as_int(row["created_by_user_id"], "created_by_user_id"),
            expires_at=as_str(row["expires_at"], "expires_at"),
            used_chat_id=as_optional_int(row["used_chat_id"], "used_chat_id"),
            used_at=as_optional_str(row["used_at"], "used_at"),
            created_at=as_str(row["created_at"], "created_at"),
        )

    async def mark_setup_token_used(self, *, token: str, used_chat_id: int, used_at: str) -> bool:
        """Помечает setup-токен использованным."""

        cursor = await self._connection.execute(
            """
            UPDATE setup_tokens
            SET used_chat_id = ?, used_at = ?
            WHERE token = ? AND used_at IS NULL
            """,
            (used_chat_id, used_at, token),
        )
        return cursor.rowcount == 1
