"""SQLite contracts for the superadmin menu and bot-wide announcements."""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite

from .base import as_int, as_optional_int, as_optional_str, as_str, fetch_all, fetch_one


@dataclass(frozen=True, slots=True)
class SupportSetupToken:
    """One-time token used to connect the support group."""

    token: str
    created_by_user_id: int
    expires_at: str
    used_chat_id: int | None
    used_at: str | None


@dataclass(frozen=True, slots=True)
class SupportGroup:
    """Configured Telegram support group."""

    chat_id: int
    title: str
    url: str


@dataclass(frozen=True, slots=True)
class Broadcast:
    """Persisted broadcast draft and delivery state."""

    id: int
    created_by_user_id: int
    text: str
    status: str


class BotUserRepository:
    """Registry of users who can receive private bot messages."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def observe_private_user(
        self,
        *,
        user_id: int,
        private_chat_id: int,
        now: str,
    ) -> None:
        """Registers or refreshes a user after private interaction."""

        await self._connection.execute(
            """
            INSERT INTO bot_users(
                user_id, private_chat_id, is_active, created_at, updated_at, last_seen_at
            )
            VALUES (?, ?, 1, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                private_chat_id = excluded.private_chat_id,
                is_active = 1,
                updated_at = excluded.updated_at,
                last_seen_at = excluded.last_seen_at
            """,
            (user_id, private_chat_id, now, now, now),
        )

    async def get_pending_action(self, user_id: int) -> str | None:
        """Returns the current private input state for a user."""

        row = await fetch_one(
            self._connection,
            "SELECT pending_action FROM bot_users WHERE user_id = ?",
            (user_id,),
        )
        if row is None:
            return None
        return as_optional_str(row["pending_action"], "pending_action")

    async def set_pending_action(self, *, user_id: int, action: str | None, now: str) -> None:
        """Sets or clears the current private input state."""

        await self._connection.execute(
            """
            UPDATE bot_users
            SET pending_action = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (action, now, user_id),
        )

    async def list_active_private_chat_ids(self) -> tuple[int, ...]:
        """Lists all currently known private broadcast targets."""

        rows = await fetch_all(
            self._connection,
            """
            SELECT private_chat_id
            FROM bot_users
            WHERE is_active = 1
            ORDER BY user_id
            """,
        )
        return tuple(as_int(row["private_chat_id"], "private_chat_id") for row in rows)


class SuperadminRepository:
    """Persistence for support-group setup and broadcasts."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def create_support_token(
        self,
        *,
        token: str,
        created_by_user_id: int,
        expires_at: str,
        created_at: str,
    ) -> None:
        """Creates a one-time support setup token."""

        await self._connection.execute(
            """
            INSERT INTO support_setup_tokens(
                token, created_by_user_id, expires_at, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (token, created_by_user_id, expires_at, created_at),
        )

    async def get_support_token(self, token: str) -> SupportSetupToken | None:
        """Reads a support setup token without consuming it."""

        row = await fetch_one(
            self._connection,
            """
            SELECT token, created_by_user_id, expires_at, used_chat_id, used_at
            FROM support_setup_tokens
            WHERE token = ?
            """,
            (token,),
        )
        if row is None:
            return None
        return SupportSetupToken(
            token=as_str(row["token"], "token"),
            created_by_user_id=as_int(row["created_by_user_id"], "created_by_user_id"),
            expires_at=as_str(row["expires_at"], "expires_at"),
            used_chat_id=as_optional_int(row["used_chat_id"], "used_chat_id"),
            used_at=as_optional_str(row["used_at"], "used_at"),
        )

    async def consume_support_token(
        self,
        *,
        token: str,
        used_chat_id: int,
        used_at: str,
    ) -> bool:
        """Consumes an unused support setup token exactly once."""

        cursor = await self._connection.execute(
            """
            UPDATE support_setup_tokens
            SET used_chat_id = ?, used_at = ?
            WHERE token = ? AND used_at IS NULL
            """,
            (used_chat_id, used_at, token),
        )
        return cursor.rowcount == 1

    async def set_support_group(
        self,
        *,
        chat_id: int,
        title: str,
        url: str,
        updated_by_user_id: int,
        updated_at: str,
    ) -> None:
        """Replaces the singleton support-group configuration."""

        await self._connection.execute(
            """
            UPDATE bot_settings
            SET support_chat_id = ?,
                support_chat_title = ?,
                support_url = ?,
                updated_by_user_id = ?,
                updated_at = ?
            WHERE singleton_id = 1
            """,
            (chat_id, title, url, updated_by_user_id, updated_at),
        )

    async def get_support_group(self) -> SupportGroup | None:
        """Returns the configured support group, if complete."""

        row = await fetch_one(
            self._connection,
            """
            SELECT support_chat_id, support_chat_title, support_url
            FROM bot_settings
            WHERE singleton_id = 1
            """,
        )
        if row is None:
            return None
        chat_id = as_optional_int(row["support_chat_id"], "support_chat_id")
        title = as_optional_str(row["support_chat_title"], "support_chat_title")
        url = as_optional_str(row["support_url"], "support_url")
        if chat_id is None or title is None or url is None:
            return None
        return SupportGroup(chat_id=chat_id, title=title, url=url)

    async def create_broadcast(
        self,
        *,
        created_by_user_id: int,
        text: str,
        created_at: str,
    ) -> int:
        """Persists a broadcast draft and returns its ID."""

        cursor = await self._connection.execute(
            """
            INSERT INTO broadcasts(created_by_user_id, text, status, created_at)
            VALUES (?, ?, 'draft', ?)
            """,
            (created_by_user_id, text, created_at),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return a broadcast id.")
        return int(cursor.lastrowid)

    async def get_broadcast(self, broadcast_id: int) -> Broadcast | None:
        """Reads a broadcast by ID."""

        row = await fetch_one(
            self._connection,
            """
            SELECT id, created_by_user_id, text, status
            FROM broadcasts
            WHERE id = ?
            """,
            (broadcast_id,),
        )
        if row is None:
            return None
        return Broadcast(
            id=as_int(row["id"], "id"),
            created_by_user_id=as_int(row["created_by_user_id"], "created_by_user_id"),
            text=as_str(row["text"], "text"),
            status=as_str(row["status"], "status"),
        )

    async def claim_broadcast(
        self,
        *,
        broadcast_id: int,
        created_by_user_id: int,
        started_at: str,
    ) -> bool:
        """Atomically moves a creator-owned draft to sending."""

        cursor = await self._connection.execute(
            """
            UPDATE broadcasts
            SET status = 'sending', started_at = ?
            WHERE id = ? AND created_by_user_id = ? AND status = 'draft'
            """,
            (started_at, broadcast_id, created_by_user_id),
        )
        return cursor.rowcount == 1

    async def cancel_broadcast(self, *, broadcast_id: int, created_by_user_id: int) -> bool:
        """Cancels a creator-owned draft."""

        cursor = await self._connection.execute(
            """
            UPDATE broadcasts
            SET status = 'cancelled'
            WHERE id = ? AND created_by_user_id = ? AND status = 'draft'
            """,
            (broadcast_id, created_by_user_id),
        )
        return cursor.rowcount == 1

    async def finish_broadcast(
        self,
        *,
        broadcast_id: int,
        finished_at: str,
        user_targets_count: int,
        group_targets_count: int,
        delivered_count: int,
        failed_count: int,
    ) -> None:
        """Stores the final delivery counters."""

        await self._connection.execute(
            """
            UPDATE broadcasts
            SET status = 'completed',
                finished_at = ?,
                user_targets_count = ?,
                group_targets_count = ?,
                delivered_count = ?,
                failed_count = ?
            WHERE id = ? AND status = 'sending'
            """,
            (
                finished_at,
                user_targets_count,
                group_targets_count,
                delivered_count,
                failed_count,
                broadcast_id,
            ),
        )

    async def list_active_group_chat_ids(self) -> tuple[int, ...]:
        """Lists configured groups that have not been disabled."""

        rows = await fetch_all(
            self._connection,
            """
            SELECT chat_id
            FROM telegram_chats
            WHERE type IN ('group', 'supergroup')
              AND status NOT IN ('not_configured', 'disabled')
            ORDER BY chat_id
            """,
        )
        return tuple(as_int(row["chat_id"], "chat_id") for row in rows)
