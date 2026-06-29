"""Repository-слой для runtime SQLite-хранилища."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import aiosqlite

from models import (
    ColumnKind,
    ColumnProfile,
    ColumnValueType,
    RuntimeChatConfig,
    SetupToken,
    SheetBinding,
    SyncRunStatus,
    TableType,
    TelegramChatStatus,
    TrackedClan,
)


class RepositoryError(RuntimeError):
    """Ошибка repository-слоя."""


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


class RuntimeConfigRepository:
    """Repository для чтения runtime-настроек чата.

    Args:
        connection: Открытое SQLite-подключение.
    """

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def get_chat_status(self, chat_id: int) -> TelegramChatStatus | None:
        """Читает статус Telegram-чата.

        Args:
            chat_id: ID Telegram-чата.

        Returns:
            Статус чата или `None`, если чат неизвестен.
        """

        row = await fetch_one(
            self._connection,
            "SELECT status FROM telegram_chats WHERE chat_id = ?",
            (chat_id,),
        )
        if row is None:
            return None
        return as_chat_status(row["status"])

    async def get_runtime_chat_config(self, chat_id: int) -> RuntimeChatConfig | None:
        """Собирает runtime-конфиг чата.

        Args:
            chat_id: ID Telegram-чата.

        Returns:
            Runtime-конфиг или `None`, если чат/активная таблица не найдены.
        """

        chat_row = await fetch_one(
            self._connection,
            "SELECT chat_id, status FROM telegram_chats WHERE chat_id = ?",
            (chat_id,),
        )
        if chat_row is None:
            return None

        binding = await self.get_active_sheet_binding(chat_id)
        if binding is None:
            return None

        return RuntimeChatConfig(
            chat_id=chat_id,
            status=as_chat_status(chat_row["status"]),
            sheet_binding=binding,
            active_clans=await self.list_active_clans(chat_id),
            column_profiles=await self.list_column_profiles(chat_id),
            timezone=binding.timezone,
        )

    async def get_active_sheet_binding(self, chat_id: int) -> SheetBinding | None:
        """Читает активную привязку Google Sheets для чата.

        Args:
            chat_id: ID Telegram-чата.

        Returns:
            Привязка таблицы или `None`.
        """

        row = await fetch_one(
            self._connection,
            """
            SELECT
                chat_id,
                google_sheet_id,
                spreadsheet_url,
                composition_sheet_name,
                composition_sheet_id,
                active_cwl_sheet_name,
                active_cwl_sheet_id,
                active_cwl_season,
                bot_state_sheet_name,
                bot_state_sheet_id,
                timezone
            FROM sheet_bindings
            WHERE chat_id = ? AND is_active = 1
            """,
            (chat_id,),
        )
        if row is None:
            return None
        return SheetBinding(
            chat_id=as_int(row["chat_id"], "chat_id"),
            google_sheet_id=as_str(row["google_sheet_id"], "google_sheet_id"),
            spreadsheet_url=as_str(row["spreadsheet_url"], "spreadsheet_url"),
            composition_sheet_name=as_str(row["composition_sheet_name"], "composition_sheet_name"),
            composition_sheet_id=as_optional_int(row["composition_sheet_id"], "composition_sheet_id"),
            active_cwl_sheet_name=as_str(row["active_cwl_sheet_name"], "active_cwl_sheet_name"),
            active_cwl_sheet_id=as_optional_int(row["active_cwl_sheet_id"], "active_cwl_sheet_id"),
            active_cwl_season=as_optional_str(row["active_cwl_season"], "active_cwl_season"),
            bot_state_sheet_name=as_str(row["bot_state_sheet_name"], "bot_state_sheet_name"),
            bot_state_sheet_id=as_optional_int(row["bot_state_sheet_id"], "bot_state_sheet_id"),
            timezone=as_str(row["timezone"], "timezone"),
        )

    async def list_active_clans(self, chat_id: int) -> tuple[TrackedClan, ...]:
        """Читает активные кланы чата в порядке вывода.

        Args:
            chat_id: ID Telegram-чата.

        Returns:
            Кортеж активных кланов.
        """

        rows = await fetch_all(
            self._connection,
            """
            SELECT chat_id, clan_tag, clan_name, sort_order
            FROM tracked_clans
            WHERE chat_id = ? AND is_active = 1
            ORDER BY sort_order ASC, clan_tag ASC
            """,
            (chat_id,),
        )
        return tuple(
            TrackedClan(
                chat_id=as_int(row["chat_id"], "chat_id"),
                clan_tag=as_str(row["clan_tag"], "clan_tag"),
                clan_name=as_str(row["clan_name"], "clan_name"),
                sort_order=as_int(row["sort_order"], "sort_order"),
            )
            for row in rows
        )

    async def list_column_profiles(self, chat_id: int) -> tuple[ColumnProfile, ...]:
        """Читает активные профили колонок чата.

        Args:
            chat_id: ID Telegram-чата.

        Returns:
            Кортеж колонок всех профилей.
        """

        rows = await fetch_all(
            self._connection,
            """
            SELECT
                chat_id,
                table_type,
                column_key,
                title,
                visible,
                is_active,
                sort_order,
                kind,
                value_type
            FROM column_profiles
            WHERE chat_id = ? AND is_active = 1
            ORDER BY table_type ASC, sort_order ASC, column_key ASC
            """,
            (chat_id,),
        )
        return tuple(_row_to_column_profile(row) for row in rows)

    async def is_google_sheet_bound_elsewhere(
        self,
        google_sheet_id: str,
        *,
        current_chat_id: int | None = None,
    ) -> bool:
        """Проверяет, занята ли Google-таблица другим активным чатом.

        Args:
            google_sheet_id: ID Google Spreadsheet.
            current_chat_id: ID текущего чата, который нужно исключить из проверки.

        Returns:
            `True`, если таблица уже активно привязана к другому чату.
        """

        sql = """
            SELECT 1
            FROM sheet_bindings
            WHERE google_sheet_id = ? AND is_active = 1
        """
        parameters: tuple[object, ...] = (google_sheet_id,)
        if current_chat_id is not None:
            sql += " AND chat_id != ?"
            parameters = (google_sheet_id, current_chat_id)
        row = await fetch_one(self._connection, f"{sql} LIMIT 1", parameters)
        return row is not None


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
        """Создаёт setup-токен.

        Args:
            token: Секретная часть команды `/connect`.
            created_by_user_id: Telegram user ID создателя.
            expires_at: ISO-дата истечения.
            created_at: ISO-дата создания.
        """

        await self._connection.execute(
            """
            INSERT INTO setup_tokens(token, created_by_user_id, expires_at, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (token, created_by_user_id, expires_at, created_at),
        )

    async def get_setup_token(self, token: str) -> SetupToken | None:
        """Читает setup-токен по секретному значению.

        Args:
            token: Секретная часть команды `/connect`.

        Returns:
            Токен или `None`.
        """

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
        """Помечает setup-токен использованным.

        Args:
            token: Секретная часть команды `/connect`.
            used_chat_id: ID группы, где токен применён.
            used_at: ISO-дата использования.

        Returns:
            `True`, если токен был помечен использованным именно этим вызовом.
        """

        cursor = await self._connection.execute(
            """
            UPDATE setup_tokens
            SET used_chat_id = ?, used_at = ?
            WHERE token = ? AND used_at IS NULL
            """,
            (used_chat_id, used_at, token),
        )
        return cursor.rowcount == 1


class TelegramChatRepository:
    """Repository Telegram-чатов и связей с администраторами.

    Args:
        connection: Открытое SQLite-подключение.
    """

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def upsert_connected_chat(
        self,
        *,
        chat_id: int,
        title: str,
        chat_type: str,
        created_by_user_id: int,
        now: str,
    ) -> None:
        """Создаёт или обновляет подключённый Telegram-чат.

        Args:
            chat_id: ID Telegram-чата.
            title: Название группы.
            chat_type: Тип Telegram-чата.
            created_by_user_id: Telegram user ID администратора.
            now: ISO-дата операции.
        """

        await self._connection.execute(
            """
            INSERT INTO telegram_chats(
                chat_id,
                title,
                type,
                status,
                created_by_user_id,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, 'waiting_for_sheet', ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                type = excluded.type,
                status = CASE
                    WHEN telegram_chats.status IN ('disabled', 'not_configured')
                    THEN 'waiting_for_sheet'
                    ELSE telegram_chats.status
                END,
                created_by_user_id = COALESCE(
                    telegram_chats.created_by_user_id,
                    excluded.created_by_user_id
                ),
                updated_at = excluded.updated_at
            """,
            (chat_id, title, chat_type, created_by_user_id, now, now),
        )

    async def upsert_known_chat(
        self,
        *,
        chat_id: int,
        title: str,
        chat_type: str,
        now: str,
    ) -> None:
        """Создаёт или обновляет известный, но ещё не настроенный Telegram-чат.

        Метод используется для `/settings` в группе до `/connect`, чтобы связь
        `chat_admin_links` не нарушала внешний ключ на `telegram_chats`.
        Статус уже существующего чата не меняется.

        Args:
            chat_id: ID Telegram-чата.
            title: Название группы.
            chat_type: Тип Telegram-чата.
            now: ISO-дата операции.
        """

        await self._connection.execute(
            """
            INSERT INTO telegram_chats(
                chat_id,
                title,
                type,
                status,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, 'not_configured', ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                type = excluded.type,
                updated_at = excluded.updated_at
            """,
            (chat_id, title, chat_type, now, now),
        )

    async def upsert_admin_link(
        self,
        *,
        chat_id: int,
        user_id: int,
        linked_at: str,
        last_admin_check_at: str | None = None,
    ) -> None:
        """Создаёт или реактивирует связь пользователя с группой.

        Args:
            chat_id: ID Telegram-чата.
            user_id: Telegram user ID.
            linked_at: ISO-дата создания связи.
            last_admin_check_at: ISO-дата последней положительной проверки админа.
        """

        await self._connection.execute(
            """
            INSERT INTO chat_admin_links(chat_id, user_id, is_active, linked_at, last_admin_check_at)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                is_active = 1,
                last_admin_check_at = COALESCE(
                    excluded.last_admin_check_at,
                    chat_admin_links.last_admin_check_at
                )
            """,
            (chat_id, user_id, linked_at, last_admin_check_at),
        )

    async def update_admin_check_at(self, *, chat_id: int, user_id: int, checked_at: str) -> None:
        """Обновляет дату положительной проверки Telegram-админа.

        Args:
            chat_id: ID Telegram-чата.
            user_id: Telegram user ID.
            checked_at: ISO-дата проверки.
        """

        await self._connection.execute(
            """
            UPDATE chat_admin_links
            SET last_admin_check_at = ?
            WHERE chat_id = ? AND user_id = ? AND is_active = 1
            """,
            (checked_at, chat_id, user_id),
        )

    async def has_active_admin_link(self, *, chat_id: int, user_id: int) -> bool:
        """Проверяет, есть ли активная связь пользователя с группой.

        Args:
            chat_id: ID Telegram-чата.
            user_id: Telegram user ID.

        Returns:
            `True`, если пользователь связан с группой через setup-flow.
        """

        row = await fetch_one(
            self._connection,
            """
            SELECT 1
            FROM chat_admin_links
            WHERE chat_id = ? AND user_id = ? AND is_active = 1
            LIMIT 1
            """,
            (chat_id, user_id),
        )
        return row is not None

    async def get_admin_check_at(self, *, chat_id: int, user_id: int) -> str | None:
        """Читает дату последней положительной проверки Telegram-админа.

        Args:
            chat_id: ID Telegram-чата.
            user_id: Telegram user ID.

        Returns:
            ISO-дата или `None`.
        """

        row = await fetch_one(
            self._connection,
            """
            SELECT last_admin_check_at
            FROM chat_admin_links
            WHERE chat_id = ? AND user_id = ? AND is_active = 1
            """,
            (chat_id, user_id),
        )
        if row is None:
            return None
        return as_optional_str(row["last_admin_check_at"], "last_admin_check_at")


class AdminChatRepository:
    """Repository для связей пользователя с известными группами.

    Args:
        connection: Открытое SQLite-подключение.
    """

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def list_known_chats(self, user_id: int) -> tuple[KnownAdminChat, ...]:
        """Возвращает группы, известные пользователю через setup-flow.

        Args:
            user_id: Telegram user ID.

        Returns:
            Кортеж известных групп.
        """

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


class SyncRunRepository:
    """Repository для истории запусков `/sync`.

    Args:
        connection: Открытое SQLite-подключение.
    """

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def create_sync_run(
        self,
        *,
        chat_id: int,
        started_by_user_id: int,
        status: SyncRunStatus,
        started_at: str,
    ) -> int:
        """Создаёт запись запуска sync.

        Args:
            chat_id: ID Telegram-чата.
            started_by_user_id: Telegram user ID инициатора.
            status: Начальный статус запуска.
            started_at: ISO-дата принятия команды.

        Returns:
            ID созданной записи.
        """

        cursor = await self._connection.execute(
            """
            INSERT INTO sync_runs(chat_id, started_by_user_id, status, started_at)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, started_by_user_id, status, started_at),
        )
        if cursor.lastrowid is None:
            raise RepositoryError("SQLite не вернул id созданного sync_runs.")
        return cursor.lastrowid


async def fetch_one(
    connection: aiosqlite.Connection,
    sql: str,
    parameters: tuple[object, ...] = (),
) -> aiosqlite.Row | None:
    """Выполняет SELECT и возвращает одну строку.

    Args:
        connection: SQLite-подключение.
        sql: SQL-запрос.
        parameters: Параметры запроса.

    Returns:
        Строка результата или `None`.
    """

    cursor = await connection.execute(sql, parameters)
    return await cursor.fetchone()


async def fetch_all(
    connection: aiosqlite.Connection,
    sql: str,
    parameters: tuple[object, ...] = (),
) -> tuple[aiosqlite.Row, ...]:
    """Выполняет SELECT и возвращает все строки.

    Args:
        connection: SQLite-подключение.
        sql: SQL-запрос.
        parameters: Параметры запроса.

    Returns:
        Кортеж строк результата.
    """

    cursor = await connection.execute(sql, parameters)
    rows = await cursor.fetchall()
    return tuple(rows)


def _row_to_column_profile(row: aiosqlite.Row) -> ColumnProfile:
    """Преобразует SQLite-строку в `ColumnProfile`.

    Args:
        row: SQLite-строка.

    Returns:
        Доменная модель колонки.
    """

    return ColumnProfile(
        chat_id=as_int(row["chat_id"], "chat_id"),
        table_type=as_table_type(row["table_type"]),
        column_key=as_str(row["column_key"], "column_key"),
        title=as_str(row["title"], "title"),
        visible=as_bool_int(row["visible"], "visible"),
        is_active=as_bool_int(row["is_active"], "is_active"),
        sort_order=as_int(row["sort_order"], "sort_order"),
        kind=as_column_kind(row["kind"]),
        value_type=as_column_value_type(row["value_type"]),
    )


def as_str(value: Any, field_name: str) -> str:
    """Проверяет строковое значение из SQLite.

    Args:
        value: Значение SQLite.
        field_name: Имя поля для текста ошибки.

    Returns:
        Строка.
    """

    if not isinstance(value, str):
        raise RepositoryError(f"Поле {field_name} должно быть строкой.")
    return value


def as_optional_str(value: Any, field_name: str) -> str | None:
    """Проверяет nullable-строку из SQLite.

    Args:
        value: Значение SQLite.
        field_name: Имя поля для текста ошибки.

    Returns:
        Строка или `None`.
    """

    if value is None:
        return None
    return as_str(value, field_name)


def as_int(value: Any, field_name: str) -> int:
    """Проверяет целое значение из SQLite.

    Args:
        value: Значение SQLite.
        field_name: Имя поля для текста ошибки.

    Returns:
        Целое число.
    """

    if not isinstance(value, int) or isinstance(value, bool):
        raise RepositoryError(f"Поле {field_name} должно быть числом.")
    return value


def as_optional_int(value: Any, field_name: str) -> int | None:
    """Проверяет nullable-число из SQLite.

    Args:
        value: Значение SQLite.
        field_name: Имя поля для текста ошибки.

    Returns:
        Целое число или `None`.
    """

    if value is None:
        return None
    return as_int(value, field_name)


def as_bool_int(value: Any, field_name: str) -> bool:
    """Проверяет SQLite boolean, сохранённый как 0/1.

    Args:
        value: Значение SQLite.
        field_name: Имя поля для текста ошибки.

    Returns:
        Булево значение.
    """

    if value == 0:
        return False
    if value == 1:
        return True
    raise RepositoryError(f"Поле {field_name} должно быть 0 или 1.")


def as_chat_status(value: Any) -> TelegramChatStatus:
    """Проверяет статус Telegram-чата.

    Args:
        value: Значение SQLite.

    Returns:
        Типизированный статус чата.
    """

    raw = as_str(value, "status")
    allowed = {
        "not_configured",
        "waiting_for_sheet",
        "waiting_for_access",
        "waiting_for_clans",
        "ready",
        "disabled",
    }
    if raw not in allowed:
        raise RepositoryError(f"Некорректный status чата: {raw}.")
    return cast(TelegramChatStatus, raw)


def as_table_type(value: Any) -> TableType:
    """Проверяет тип таблицы профиля колонок.

    Args:
        value: Значение SQLite.

    Returns:
        Типизированный table_type.
    """

    raw = as_str(value, "table_type")
    if raw not in {"composition_active", "composition_exited", "cwl"}:
        raise RepositoryError(f"Некорректный table_type: {raw}.")
    return cast(TableType, raw)


def as_column_kind(value: Any) -> ColumnKind:
    """Проверяет kind профиля колонки.

    Args:
        value: Значение SQLite.

    Returns:
        Типизированный kind.
    """

    raw = as_str(value, "kind")
    if raw not in {"system", "user", "service"}:
        raise RepositoryError(f"Некорректный column kind: {raw}.")
    return cast(ColumnKind, raw)


def as_column_value_type(value: Any) -> ColumnValueType:
    """Проверяет value_type профиля колонки.

    Args:
        value: Значение SQLite.

    Returns:
        Типизированный value_type.
    """

    raw = as_str(value, "value_type")
    if raw not in {"string", "integer", "datetime"}:
        raise RepositoryError(f"Некорректный column value_type: {raw}.")
    return cast(ColumnValueType, raw)
