"""Repository-слой для runtime SQLite-хранилища."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

import aiosqlite

from column_profiles import all_default_columns, default_columns
from models import (
    ColumnKind,
    ColumnProfile,
    ColumnValueType,
    CompositionPlayerStatus,
    RuntimeChatConfig,
    SetupToken,
    SheetBinding,
    SheetBlock,
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


@dataclass(frozen=True, slots=True)
class PendingSheetLinkSetup:
    """Ожидаемый ввод ссылки на таблицу в личном чате.

    Attributes:
        chat_id: ID настраиваемой Telegram-группы.
        title: Название Telegram-группы.
        setup_state: Текущее состояние setup-flow.
    """

    chat_id: int
    title: str
    setup_state: str


@dataclass(frozen=True, slots=True)
class CompositionPlayerState:
    """Состояние игрока состава из SQLite.

    Attributes:
        player_tag: Нормализованный тег игрока.
        status: Статус игрока: active, exited или untracked.
        clan_tag: Тег активного клана или `None`.
        town_hall: Уровень ратуши или `None`.
        nickname: Никнейм игрока или `None`.
        exited_at: ISO-дата выхода или `None`.
        user_values: Пользовательские значения по `column_key`.
        last_seen_at: ISO-дата последнего наблюдения в CoC API или `None`.
    """

    player_tag: str
    status: CompositionPlayerStatus
    clan_tag: str | None
    town_hall: int | None
    nickname: str | None
    exited_at: str | None
    user_values: dict[str, str]
    last_seen_at: str | None


@dataclass(frozen=True, slots=True)
class CwlRowState:
    """Состояние строки CWL из SQLite.

    Attributes:
        season: CWL-сезон.
        row_key: Стабильный ключ строки.
        clan_tag: Тег отслеживаемого клана.
        round_number: Номер раунда.
        attacker_tag: Тег атакующего или `None`.
        marker: Маркер строки.
        technical_values: Технические значения строки.
        user_values: Пользовательские значения по `column_key`.
        row_hash: Hash технических значений или `None`.
    """

    season: str
    row_key: str
    clan_tag: str
    round_number: int | None
    attacker_tag: str | None
    marker: str
    technical_values: dict[str, object]
    user_values: dict[str, str]
    row_hash: str | None


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


@dataclass(frozen=True, slots=True)
class SyncStatusSummary:
    """Сводка последнего sync для `/status`.

    Attributes:
        chat_id: ID Telegram-чата.
        status: Статус настройки чата.
        last_sync_started_at: Дата последнего принятого `/sync` или `None`.
        last_sync_finished_at: Дата завершения последнего `/sync` или `None`.
        last_sync_status: Статус последнего `/sync` или `None`.
        last_sync_error: Ошибка последнего `/sync` или `None`.
        active_clans_count: Количество active clans.
        active_cwl_season: Активный CWL-сезон или `None`.
        spreadsheet_url: Ссылка на таблицу или `None`.
    """

    chat_id: int
    status: TelegramChatStatus
    last_sync_started_at: str | None
    last_sync_finished_at: str | None
    last_sync_status: str | None
    last_sync_error: str | None
    active_clans_count: int
    active_cwl_season: str | None
    spreadsheet_url: str | None


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
            composition_sheet_id=as_optional_int(
                row["composition_sheet_id"], "composition_sheet_id"
            ),
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


class ClanSettingsRepository:
    """Repository настроек отслеживаемых кланов."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def count_active_clans(self, chat_id: int) -> int:
        """Считает активные кланы чата.

        Args:
            chat_id: ID Telegram-чата.

        Returns:
            Количество активных кланов.
        """

        row = await fetch_one(
            self._connection,
            "SELECT COUNT(*) AS cnt FROM tracked_clans WHERE chat_id = ? AND is_active = 1",
            (chat_id,),
        )
        return 0 if row is None else as_int(row["cnt"], "cnt")

    async def list_active_clans(self, chat_id: int) -> tuple[TrackedClan, ...]:
        """Читает активные кланы чата.

        Args:
            chat_id: ID Telegram-чата.

        Returns:
            Активные кланы.
        """

        return await RuntimeConfigRepository(self._connection).list_active_clans(chat_id)

    async def is_active_clan(self, *, chat_id: int, clan_tag: str) -> bool:
        """Проверяет, активен ли клан в чате."""

        row = await fetch_one(
            self._connection,
            """
            SELECT 1
            FROM tracked_clans
            WHERE chat_id = ? AND clan_tag = ? AND is_active = 1
            LIMIT 1
            """,
            (chat_id, clan_tag),
        )
        return row is not None

    async def upsert_or_reactivate_clan(
        self,
        *,
        chat_id: int,
        clan_tag: str,
        clan_name: str,
        now: str,
    ) -> None:
        """Создаёт или реактивирует отслеживаемый клан."""

        next_order = await self._next_sort_order(chat_id)
        await self._connection.execute(
            """
            INSERT INTO tracked_clans(
                chat_id,
                clan_tag,
                clan_name,
                sort_order,
                is_active,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(chat_id, clan_tag) DO UPDATE SET
                clan_name = excluded.clan_name,
                sort_order = excluded.sort_order,
                is_active = 1,
                updated_at = excluded.updated_at
            """,
            (chat_id, clan_tag, clan_name, next_order, now, now),
        )

    async def soft_delete_clan(self, *, chat_id: int, clan_tag: str, now: str) -> bool:
        """Мягко удаляет клан из отслеживания."""

        cursor = await self._connection.execute(
            """
            UPDATE tracked_clans
            SET is_active = 0, updated_at = ?
            WHERE chat_id = ? AND clan_tag = ? AND is_active = 1
            """,
            (now, chat_id, clan_tag),
        )
        return cursor.rowcount == 1

    async def mark_players_untracked(self, *, chat_id: int, clan_tag: str, now: str) -> None:
        """Помечает игроков удалённого клана как `untracked`."""

        await self._connection.execute(
            """
            UPDATE composition_player_state
            SET status = 'untracked', updated_at = ?
            WHERE chat_id = ? AND clan_tag = ? AND status = 'active'
            """,
            (now, chat_id, clan_tag),
        )

    async def move_clan(self, *, chat_id: int, clan_tag: str, direction: str, now: str) -> bool:
        """Меняет порядок активного клана."""

        clans = list(await self.list_active_clans(chat_id))
        current_index = next(
            (index for index, clan in enumerate(clans) if clan.clan_tag == clan_tag),
            None,
        )
        if current_index is None:
            return False
        target_index = current_index - 1 if direction == "up" else current_index + 1
        if target_index < 0 or target_index >= len(clans):
            return False
        current = clans[current_index]
        target = clans[target_index]
        await self._connection.execute(
            """
            UPDATE tracked_clans
            SET sort_order = ?, updated_at = ?
            WHERE chat_id = ? AND clan_tag = ?
            """,
            (target.sort_order, now, chat_id, current.clan_tag),
        )
        await self._connection.execute(
            """
            UPDATE tracked_clans
            SET sort_order = ?, updated_at = ?
            WHERE chat_id = ? AND clan_tag = ?
            """,
            (current.sort_order, now, chat_id, target.clan_tag),
        )
        return True

    async def _next_sort_order(self, chat_id: int) -> int:
        """Вычисляет следующий sort_order для клана."""

        row = await fetch_one(
            self._connection,
            """
            SELECT COALESCE(MAX(sort_order), 0) AS max_order
            FROM tracked_clans
            WHERE chat_id = ?
            """,
            (chat_id,),
        )
        return 10 if row is None else as_int(row["max_order"], "max_order") + 10


class ColumnProfileRepository:
    """Repository управления профилями колонок."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def ensure_default_profiles(self, *, chat_id: int, now: str) -> None:
        """Создаёт отсутствующие дефолтные колонки всех профилей."""

        for definition in all_default_columns():
            await self._connection.execute(
                """
                INSERT OR IGNORE INTO column_profiles(
                    chat_id,
                    table_type,
                    column_key,
                    title,
                    visible,
                    is_active,
                    sort_order,
                    kind,
                    value_type,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    definition.table_type,
                    definition.column_key,
                    definition.title,
                    int(definition.visible),
                    definition.sort_order,
                    definition.kind,
                    definition.value_type,
                    now,
                    now,
                ),
            )

    async def restore_defaults(self, *, chat_id: int, table_type: TableType, now: str) -> None:
        """Восстанавливает обязательные service/system колонки."""

        for definition in default_columns(table_type):
            await self._connection.execute(
                """
                INSERT INTO column_profiles(
                    chat_id,
                    table_type,
                    column_key,
                    title,
                    visible,
                    is_active,
                    sort_order,
                    kind,
                    value_type,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, table_type, column_key) DO UPDATE SET
                    title = excluded.title,
                    visible = excluded.visible,
                    is_active = 1,
                    sort_order = excluded.sort_order,
                    kind = excluded.kind,
                    value_type = excluded.value_type,
                    updated_at = excluded.updated_at
                """,
                (
                    chat_id,
                    definition.table_type,
                    definition.column_key,
                    definition.title,
                    int(definition.visible),
                    definition.sort_order,
                    definition.kind,
                    definition.value_type,
                    now,
                    now,
                ),
            )

    async def list_columns(
        self, *, chat_id: int, table_type: TableType
    ) -> tuple[ColumnProfile, ...]:
        """Читает активные колонки одного профиля."""

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
            WHERE chat_id = ? AND table_type = ? AND is_active = 1
            ORDER BY sort_order ASC, column_key ASC
            """,
            (chat_id, table_type),
        )
        return tuple(_row_to_column_profile(row) for row in rows)

    async def set_visibility(
        self,
        *,
        chat_id: int,
        table_type: TableType,
        column_key: str,
        visible: bool,
        now: str,
    ) -> bool:
        """Меняет видимость колонки, кроме service-колонок."""

        cursor = await self._connection.execute(
            """
            UPDATE column_profiles
            SET visible = ?, updated_at = ?
            WHERE chat_id = ?
              AND table_type = ?
              AND column_key = ?
              AND kind != 'service'
              AND is_active = 1
            """,
            (int(visible), now, chat_id, table_type, column_key),
        )
        return cursor.rowcount == 1

    async def rename_column(
        self,
        *,
        chat_id: int,
        table_type: TableType,
        column_key: str,
        title: str,
        now: str,
    ) -> bool:
        """Переименовывает колонку, кроме service-колонок."""

        cursor = await self._connection.execute(
            """
            UPDATE column_profiles
            SET title = ?, updated_at = ?
            WHERE chat_id = ?
              AND table_type = ?
              AND column_key = ?
              AND kind != 'service'
              AND is_active = 1
            """,
            (title, now, chat_id, table_type, column_key),
        )
        return cursor.rowcount == 1

    async def create_user_column(
        self,
        *,
        chat_id: int,
        table_type: TableType,
        column_key: str,
        title: str,
        now: str,
    ) -> None:
        """Создаёт пользовательскую колонку."""

        sort_order = await self._next_sort_order(chat_id=chat_id, table_type=table_type)
        await self._connection.execute(
            """
            INSERT INTO column_profiles(
                chat_id,
                table_type,
                column_key,
                title,
                visible,
                is_active,
                sort_order,
                kind,
                value_type,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, 1, 1, ?, 'user', 'string', ?, ?)
            """,
            (chat_id, table_type, column_key, title, sort_order, now, now),
        )

    async def soft_delete_user_column(
        self,
        *,
        chat_id: int,
        table_type: TableType,
        column_key: str,
        now: str,
    ) -> bool:
        """Мягко удаляет пользовательскую колонку."""

        cursor = await self._connection.execute(
            """
            UPDATE column_profiles
            SET visible = 0, is_active = 0, updated_at = ?
            WHERE chat_id = ?
              AND table_type = ?
              AND column_key = ?
              AND kind = 'user'
              AND is_active = 1
            """,
            (now, chat_id, table_type, column_key),
        )
        return cursor.rowcount == 1

    async def move_column(
        self,
        *,
        chat_id: int,
        table_type: TableType,
        column_key: str,
        direction: str,
        now: str,
    ) -> bool:
        """Меняет порядок колонки, кроме service-колонки."""

        columns = [
            column
            for column in await self.list_columns(chat_id=chat_id, table_type=table_type)
            if column.kind != "service"
        ]
        current_index = next(
            (index for index, column in enumerate(columns) if column.column_key == column_key),
            None,
        )
        if current_index is None:
            return False
        target_index = current_index - 1 if direction == "up" else current_index + 1
        if target_index < 0 or target_index >= len(columns):
            return False
        current = columns[current_index]
        target = columns[target_index]
        await self._connection.execute(
            """
            UPDATE column_profiles
            SET sort_order = ?, updated_at = ?
            WHERE chat_id = ? AND table_type = ? AND column_key = ?
            """,
            (target.sort_order, now, chat_id, table_type, current.column_key),
        )
        await self._connection.execute(
            """
            UPDATE column_profiles
            SET sort_order = ?, updated_at = ?
            WHERE chat_id = ? AND table_type = ? AND column_key = ?
            """,
            (current.sort_order, now, chat_id, table_type, target.column_key),
        )
        return True

    async def _next_sort_order(self, *, chat_id: int, table_type: TableType) -> int:
        """Вычисляет sort_order для новой user-колонки."""

        row = await fetch_one(
            self._connection,
            """
            SELECT COALESCE(MAX(sort_order), 0) AS max_order
            FROM column_profiles
            WHERE chat_id = ? AND table_type = ?
            """,
            (chat_id, table_type),
        )
        return 10 if row is None else as_int(row["max_order"], "max_order") + 10


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
        """Создаёт или обновляет подключённый Telegram-чат."""

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
        """Создаёт или обновляет известный, но ещё не настроенный Telegram-чат."""

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
        """Создаёт или реактивирует связь пользователя с группой."""

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
        """Обновляет дату положительной проверки Telegram-админа."""

        await self._connection.execute(
            """
            UPDATE chat_admin_links
            SET last_admin_check_at = ?
            WHERE chat_id = ? AND user_id = ? AND is_active = 1
            """,
            (checked_at, chat_id, user_id),
        )

    async def has_active_admin_link(self, *, chat_id: int, user_id: int) -> bool:
        """Проверяет, есть ли активная связь пользователя с группой."""

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

    async def get_setup_state(self, chat_id: int) -> str | None:
        """Читает setup_state Telegram-чата."""
        row = await fetch_one(
            self._connection,
            "SELECT setup_state FROM telegram_chats WHERE chat_id = ?",
            (chat_id,),
        )
        if row is None:
            return None
        return as_optional_str(row["setup_state"], "setup_state")

    async def set_setup_state(self, *, chat_id: int, setup_state: str | None, now: str) -> None:
        """Обновляет setup_state Telegram-чата."""

        await self._connection.execute(
            """
            UPDATE telegram_chats
            SET setup_state = ?, updated_at = ?
            WHERE chat_id = ?
            """,
            (setup_state, now, chat_id),
        )

    async def clear_setup_states_for_user(self, *, user_id: int, now: str) -> int:
        """Сбрасывает ожидающие setup_state, принадлежащие пользователю.

        Метод чистит только состояния групп, где у пользователя есть активная
        admin-связь. Это не даёт одному пользователю сбросить чужую настройку
        группы через личную команду `/cancel`.

        Args:
            user_id: Telegram user ID пользователя, отправившего `/cancel`.
            now: ISO-дата обновления.

        Returns:
            Количество Telegram-групп, где setup_state был сброшен.
        """

        setup_state_patterns = (
            f"awaiting_sheet_link:{user_id}",
            f"awaiting_sheet_access:{user_id}:*",
            f"awaiting_clan_tag:{user_id}",
            f"awaiting_user_column_title:{user_id}:*",
            f"awaiting_column_rename:{user_id}:*",
        )
        conditions = " OR ".join("telegram_chats.setup_state GLOB ?" for _ in setup_state_patterns)
        cursor = await self._connection.execute(
            f"""
            UPDATE telegram_chats
            SET setup_state = NULL,
                updated_at = ?
            WHERE setup_state IS NOT NULL
              AND ({conditions})
              AND EXISTS (
                  SELECT 1
                  FROM chat_admin_links
                  WHERE chat_admin_links.chat_id = telegram_chats.chat_id
                    AND chat_admin_links.user_id = ?
                    AND chat_admin_links.is_active = 1
              )
            """,
            (now, *setup_state_patterns, user_id),
        )
        return cursor.rowcount

    async def set_status(self, *, chat_id: int, status: TelegramChatStatus, now: str) -> None:
        """Обновляет статус Telegram-чата."""

        await self._connection.execute(
            """
            UPDATE telegram_chats
            SET status = ?, updated_at = ?
            WHERE chat_id = ?
            """,
            (status, now, chat_id),
        )

    async def disable_chat(self, *, chat_id: int, now: str) -> None:
        """Отключает Telegram-группу без удаления исторических данных."""

        await self._connection.execute(
            """
            UPDATE telegram_chats
            SET status = 'disabled', setup_state = NULL, updated_at = ?
            WHERE chat_id = ?
            """,
            (now, chat_id),
        )

    async def mark_sync_started(self, *, chat_id: int, started_at: str) -> None:
        """Фиксирует момент принятия `/sync` для rate limit."""

        await self._connection.execute(
            """
            UPDATE telegram_chats
            SET last_sync_started_at = ?, updated_at = ?
            WHERE chat_id = ?
            """,
            (started_at, started_at, chat_id),
        )

    async def mark_sync_finished(
        self,
        *,
        chat_id: int,
        finished_at: str,
        status: str,
        error: str | None,
    ) -> None:
        """Фиксирует результат последнего `/sync` для `/status`."""

        await self._connection.execute(
            """
            UPDATE telegram_chats
            SET last_sync_finished_at = ?,
                last_sync_status = ?,
                last_sync_error = ?,
                updated_at = ?
            WHERE chat_id = ?
            """,
            (finished_at, status, error, finished_at, chat_id),
        )

    async def get_last_sync_started_at(self, chat_id: int) -> str | None:
        """Читает дату последнего принятого `/sync`."""

        row = await fetch_one(
            self._connection,
            "SELECT last_sync_started_at FROM telegram_chats WHERE chat_id = ?",
            (chat_id,),
        )
        if row is None:
            return None
        return as_optional_str(row["last_sync_started_at"], "last_sync_started_at")

    async def get_sync_status_summary(self, chat_id: int) -> SyncStatusSummary | None:
        """Собирает данные для `/status`."""

        row = await fetch_one(
            self._connection,
            """
            SELECT
                c.chat_id,
                c.status,
                c.last_sync_started_at,
                c.last_sync_finished_at,
                c.last_sync_status,
                c.last_sync_error,
                b.spreadsheet_url,
                b.active_cwl_season,
                COUNT(tc.clan_tag) AS active_clans_count
            FROM telegram_chats AS c
            LEFT JOIN sheet_bindings AS b
                ON b.chat_id = c.chat_id AND b.is_active = 1
            LEFT JOIN tracked_clans AS tc
                ON tc.chat_id = c.chat_id AND tc.is_active = 1
            WHERE c.chat_id = ?
            GROUP BY c.chat_id
            """,
            (chat_id,),
        )
        if row is None:
            return None
        return SyncStatusSummary(
            chat_id=as_int(row["chat_id"], "chat_id"),
            status=as_chat_status(row["status"]),
            last_sync_started_at=as_optional_str(
                row["last_sync_started_at"], "last_sync_started_at"
            ),
            last_sync_finished_at=as_optional_str(
                row["last_sync_finished_at"], "last_sync_finished_at"
            ),
            last_sync_status=as_optional_str(row["last_sync_status"], "last_sync_status"),
            last_sync_error=as_optional_str(row["last_sync_error"], "last_sync_error"),
            active_clans_count=as_int(row["active_clans_count"], "active_clans_count"),
            active_cwl_season=as_optional_str(row["active_cwl_season"], "active_cwl_season"),
            spreadsheet_url=as_optional_str(row["spreadsheet_url"], "spreadsheet_url"),
        )

    async def find_pending_sheet_link_setup(
        self,
        *,
        user_id: int,
        state_prefix: str,
    ) -> PendingSheetLinkSetup | None:
        """Ищет группу, ожидающую ссылку на таблицу от пользователя."""

        row = await fetch_one(
            self._connection,
            """
            SELECT c.chat_id, c.title, c.setup_state
            FROM telegram_chats AS c
            JOIN chat_admin_links AS l ON l.chat_id = c.chat_id
            WHERE l.user_id = ?
              AND l.is_active = 1
              AND c.setup_state LIKE ?
            ORDER BY c.updated_at DESC
            LIMIT 1
            """,
            (user_id, f"{state_prefix}%"),
        )
        if row is None:
            return None
        return PendingSheetLinkSetup(
            chat_id=as_int(row["chat_id"], "chat_id"),
            title=as_str(row["title"], "title"),
            setup_state=as_str(row["setup_state"], "setup_state"),
        )

    async def get_admin_check_at(self, *, chat_id: int, user_id: int) -> str | None:
        """Читает дату последней положительной проверки Telegram-админа."""

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


class SheetBindingRepository:
    """Repository привязок Telegram-чата к Google Sheets.

    Args:
        connection: Открытое SQLite-подключение.
    """

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def upsert_active_binding(
        self,
        *,
        chat_id: int,
        google_sheet_id: str,
        spreadsheet_url: str,
        composition_sheet_name: str,
        composition_sheet_id: int,
        active_cwl_sheet_name: str,
        active_cwl_sheet_id: int,
        active_cwl_season: str | None,
        bot_state_sheet_name: str,
        bot_state_sheet_id: int,
        timezone: str,
        now: str,
    ) -> None:
        """Создаёт или обновляет активную привязку таблицы."""

        await self._connection.execute(
            """
            INSERT INTO sheet_bindings(
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
                timezone,
                is_active,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                google_sheet_id = excluded.google_sheet_id,
                spreadsheet_url = excluded.spreadsheet_url,
                composition_sheet_name = excluded.composition_sheet_name,
                composition_sheet_id = excluded.composition_sheet_id,
                active_cwl_sheet_name = excluded.active_cwl_sheet_name,
                active_cwl_sheet_id = excluded.active_cwl_sheet_id,
                active_cwl_season = excluded.active_cwl_season,
                bot_state_sheet_name = excluded.bot_state_sheet_name,
                bot_state_sheet_id = excluded.bot_state_sheet_id,
                timezone = excluded.timezone,
                is_active = 1,
                updated_at = excluded.updated_at
            """,
            (
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
                timezone,
                now,
                now,
            ),
        )

    async def update_active_cwl_binding(
        self,
        *,
        chat_id: int,
        active_cwl_sheet_name: str,
        active_cwl_sheet_id: int,
        active_cwl_season: str,
        now: str,
    ) -> None:
        """Обновляет активный CWL-лист binding после записи/архивации.

        Args:
            chat_id: ID Telegram-чата.
            active_cwl_sheet_name: Название активного CWL-листа.
            active_cwl_sheet_id: Числовой ID активного CWL-листа.
            active_cwl_season: Активный CWL-сезон.
            now: ISO-дата обновления.
        """

        await self._connection.execute(
            """
            UPDATE sheet_bindings
            SET active_cwl_sheet_name = ?,
                active_cwl_sheet_id = ?,
                active_cwl_season = ?,
                updated_at = ?
            WHERE chat_id = ? AND is_active = 1
            """,
            (active_cwl_sheet_name, active_cwl_sheet_id, active_cwl_season, now, chat_id),
        )

    async def update_sheet_ids(
        self,
        *,
        chat_id: int,
        composition_sheet_name: str,
        composition_sheet_id: int,
        active_cwl_sheet_name: str,
        active_cwl_sheet_id: int,
        active_cwl_season: str | None,
        bot_state_sheet_name: str,
        bot_state_sheet_id: int,
        now: str,
    ) -> None:
        """Обновляет sheet IDs после диагностики/auto-fix."""

        await self._connection.execute(
            """
            UPDATE sheet_bindings
            SET composition_sheet_name = ?,
                composition_sheet_id = ?,
                active_cwl_sheet_name = ?,
                active_cwl_sheet_id = ?,
                active_cwl_season = ?,
                bot_state_sheet_name = ?,
                bot_state_sheet_id = ?,
                updated_at = ?
            WHERE chat_id = ? AND is_active = 1
            """,
            (
                composition_sheet_name,
                composition_sheet_id,
                active_cwl_sheet_name,
                active_cwl_sheet_id,
                active_cwl_season,
                bot_state_sheet_name,
                bot_state_sheet_id,
                now,
                chat_id,
            ),
        )

    async def deactivate_binding(self, *, chat_id: int, now: str) -> None:
        """Деактивирует привязку таблицы без изменения Google Sheets."""

        await self._connection.execute(
            """
            UPDATE sheet_bindings
            SET is_active = 0, updated_at = ?
            WHERE chat_id = ? AND is_active = 1
            """,
            (now, chat_id),
        )

    async def has_active_binding(self, chat_id: int) -> bool:
        """Проверяет, есть ли у чата активная привязка таблицы."""

        row = await fetch_one(
            self._connection,
            "SELECT 1 FROM sheet_bindings WHERE chat_id = ? AND is_active = 1 LIMIT 1",
            (chat_id,),
        )
        return row is not None

    async def transfer_binding_to_chat(
        self,
        *,
        source_chat_id: int,
        target_chat_id: int,
        now: str,
    ) -> None:
        """Переносит активную привязку таблицы на другой Telegram-чат."""

        await self._connection.execute(
            "DELETE FROM sheet_bindings WHERE chat_id = ? AND is_active = 0",
            (target_chat_id,),
        )
        await self._connection.execute(
            """
            UPDATE sheet_bindings
            SET chat_id = ?, updated_at = ?
            WHERE chat_id = ? AND is_active = 1
            """,
            (target_chat_id, now, source_chat_id),
        )


class SheetBlockRepository:
    """Repository последних управляемых прямоугольников Google Sheets.

    Args:
        connection: Открытое SQLite-подключение.
    """

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def list_blocks(
        self, chat_id: int, sheet_name: str | None = None
    ) -> tuple[SheetBlock, ...]:
        """Читает последние записанные блоки чата."""

        sql = """
            SELECT chat_id, sheet_name, sheet_id, block_key, start_cell, rows_count, columns_count
            FROM sheet_blocks
            WHERE chat_id = ?
        """
        parameters: tuple[object, ...] = (chat_id,)
        if sheet_name is not None:
            sql += " AND sheet_name = ?"
            parameters = (chat_id, sheet_name)
        sql += " ORDER BY sheet_name ASC, block_key ASC"
        rows = await fetch_all(self._connection, sql, parameters)
        return tuple(
            SheetBlock(
                chat_id=as_int(row["chat_id"], "chat_id"),
                sheet_name=as_str(row["sheet_name"], "sheet_name"),
                sheet_id=as_optional_int(row["sheet_id"], "sheet_id"),
                block_key=as_str(row["block_key"], "block_key"),
                start_cell=as_str(row["start_cell"], "start_cell"),
                rows_count=as_int(row["rows_count"], "rows_count"),
                columns_count=as_int(row["columns_count"], "columns_count"),
            )
            for row in rows
        )

    async def upsert_block(self, *, block: SheetBlock, updated_at: str) -> None:
        """Создаёт или обновляет запись управляемого блока."""

        await self._connection.execute(
            """
            INSERT INTO sheet_blocks(
                chat_id,
                sheet_name,
                sheet_id,
                block_key,
                start_cell,
                rows_count,
                columns_count,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, sheet_name, block_key) DO UPDATE SET
                sheet_id = excluded.sheet_id,
                start_cell = excluded.start_cell,
                rows_count = excluded.rows_count,
                columns_count = excluded.columns_count,
                updated_at = excluded.updated_at
            """,
            (
                block.chat_id,
                block.sheet_name,
                block.sheet_id,
                block.block_key,
                block.start_cell,
                block.rows_count,
                block.columns_count,
                updated_at,
            ),
        )

    async def replace_blocks_by_prefixes(
        self,
        *,
        chat_id: int,
        sheet_name: str,
        block_key_prefixes: tuple[str, ...],
        blocks: tuple[SheetBlock, ...],
        updated_at: str,
    ) -> None:
        """Заменяет набор block records для листа по prefix-фильтру.

        Args:
            chat_id: ID Telegram-чата.
            sheet_name: Название листа.
            block_key_prefixes: Prefixes block_key, которые нужно заменить.
            blocks: Новый набор блоков.
            updated_at: ISO-дата обновления.
        """

        if block_key_prefixes:
            conditions = " OR ".join("block_key LIKE ?" for _ in block_key_prefixes)
            parameters: tuple[object, ...] = (
                chat_id,
                sheet_name,
                *(f"{prefix}%" for prefix in block_key_prefixes),
            )
            await self._connection.execute(
                f"""
                DELETE FROM sheet_blocks
                WHERE chat_id = ?
                  AND sheet_name = ?
                  AND ({conditions})
                """,
                parameters,
            )

        for block in blocks:
            await self.upsert_block(block=block, updated_at=updated_at)


class CompositionPlayerStateRepository:
    """Repository состояния игроков состава.

    Args:
        connection: Открытое SQLite-подключение.
    """

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def list_players(self, chat_id: int) -> tuple[CompositionPlayerState, ...]:
        """Читает все состояния игроков состава чата."""

        rows = await fetch_all(
            self._connection,
            """
            SELECT
                player_tag,
                clan_tag,
                status,
                town_hall,
                nickname,
                exited_at,
                user_values_json,
                last_seen_at
            FROM composition_player_state
            WHERE chat_id = ?
            """,
            (chat_id,),
        )
        return tuple(_row_to_composition_player_state(row) for row in rows)

    async def upsert_player_state(
        self,
        *,
        chat_id: int,
        player_tag: str,
        status: str,
        clan_tag: str | None,
        town_hall: int | None,
        nickname: str | None,
        exited_at: str | None,
        user_values: dict[str, str],
        last_seen_at: str | None,
        updated_at: str,
    ) -> None:
        """Создаёт или обновляет состояние игрока состава."""

        await self._connection.execute(
            """
            INSERT INTO composition_player_state(
                chat_id,
                player_tag,
                clan_tag,
                status,
                town_hall,
                nickname,
                exited_at,
                user_values_json,
                last_seen_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, player_tag) DO UPDATE SET
                clan_tag = excluded.clan_tag,
                status = excluded.status,
                town_hall = excluded.town_hall,
                nickname = excluded.nickname,
                exited_at = excluded.exited_at,
                user_values_json = excluded.user_values_json,
                last_seen_at = excluded.last_seen_at,
                updated_at = excluded.updated_at
            """,
            (
                chat_id,
                player_tag,
                clan_tag,
                status,
                town_hall,
                nickname,
                exited_at,
                json.dumps(user_values, ensure_ascii=False, sort_keys=True),
                last_seen_at,
                updated_at,
            ),
        )


class CwlRowStateRepository:
    """Repository состояния CWL-строк.

    Args:
        connection: Открытое SQLite-подключение.
    """

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def list_rows(self, *, chat_id: int, season: str) -> tuple[CwlRowState, ...]:
        """Читает CWL row state одного сезона.

        Args:
            chat_id: ID Telegram-чата.
            season: CWL-сезон.

        Returns:
            Строки CWL state.
        """

        rows = await fetch_all(
            self._connection,
            """
            SELECT
                season,
                row_key,
                clan_tag,
                round_number,
                attacker_tag,
                marker,
                technical_values_json,
                user_values_json,
                row_hash
            FROM cwl_row_state
            WHERE chat_id = ? AND season = ?
            """,
            (chat_id, season),
        )
        return tuple(_row_to_cwl_row_state(row) for row in rows)

    async def upsert_row_state(
        self,
        *,
        chat_id: int,
        season: str,
        row_key: str,
        clan_tag: str,
        round_number: int | None,
        attacker_tag: str | None,
        marker: str,
        technical_values: dict[str, object],
        user_values: dict[str, str],
        row_hash: str,
    ) -> None:
        """Создаёт или обновляет CWL row state.

        Args:
            chat_id: ID Telegram-чата.
            season: CWL-сезон.
            row_key: Стабильный ключ строки.
            clan_tag: Тег отслеживаемого клана.
            round_number: Номер раунда.
            attacker_tag: Тег атакующего или `None`.
            marker: Маркер строки.
            technical_values: Технические значения строки.
            user_values: Пользовательские значения.
            row_hash: Hash технических значений.
        """

        await self._connection.execute(
            """
            INSERT INTO cwl_row_state(
                chat_id,
                season,
                row_key,
                clan_tag,
                round_number,
                attacker_tag,
                marker,
                technical_values_json,
                user_values_json,
                row_hash,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            ON CONFLICT(chat_id, season, row_key) DO UPDATE SET
                clan_tag = excluded.clan_tag,
                round_number = excluded.round_number,
                attacker_tag = excluded.attacker_tag,
                marker = excluded.marker,
                technical_values_json = excluded.technical_values_json,
                user_values_json = excluded.user_values_json,
                row_hash = excluded.row_hash,
                updated_at = excluded.updated_at
            """,
            (
                chat_id,
                season,
                row_key,
                clan_tag,
                round_number,
                attacker_tag,
                marker,
                json.dumps(technical_values, ensure_ascii=False, sort_keys=True),
                json.dumps(user_values, ensure_ascii=False, sort_keys=True),
                row_hash,
            ),
        )


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


class ChatLifecycleRepository:
    """Repository массовых lifecycle-операций над настройками чата."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def move_runtime_state(
        self,
        *,
        source_chat_id: int,
        target_chat_id: int,
        now: str,
    ) -> None:
        """Переносит кланы, профили, state и blocks на новый chat_id."""

        for table_name in (
            "tracked_clans",
            "column_profiles",
            "composition_player_state",
            "cwl_row_state",
            "sheet_blocks",
        ):
            await self._connection.execute(
                f"DELETE FROM {table_name} WHERE chat_id = ?",
                (target_chat_id,),
            )
            await self._connection.execute(
                f"UPDATE {table_name} SET chat_id = ? WHERE chat_id = ?",
                (target_chat_id, source_chat_id),
            )

        await self._connection.execute(
            """
            UPDATE telegram_chats
            SET status = 'disabled', setup_state = NULL, updated_at = ?
            WHERE chat_id = ?
            """,
            (now, source_chat_id),
        )
        await self._connection.execute(
            """
            UPDATE telegram_chats
            SET status = 'ready', setup_state = NULL, updated_at = ?
            WHERE chat_id = ?
            """,
            (now, target_chat_id),
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
        """Создаёт запись запуска sync."""

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

    async def has_successful_sync(self, chat_id: int) -> bool:
        """Проверяет, был ли успешный sync у чата."""

        row = await fetch_one(
            self._connection,
            """
            SELECT 1
            FROM sync_runs
            WHERE chat_id = ? AND status = 'success'
            LIMIT 1
            """,
            (chat_id,),
        )
        return row is not None

    async def finish_sync_run(
        self,
        *,
        sync_run_id: int,
        status: SyncRunStatus,
        finished_at: str,
        error_stage: str | None = None,
        error_clan_tag: str | None = None,
        error_war_tag: str | None = None,
        error_message: str | None = None,
        report_json: str | None = None,
    ) -> None:
        """Завершает запись `sync_runs`."""

        await self._connection.execute(
            """
            UPDATE sync_runs
            SET status = ?,
                finished_at = ?,
                error_stage = ?,
                error_clan_tag = ?,
                error_war_tag = ?,
                error_message = ?,
                report_json = ?
            WHERE id = ?
            """,
            (
                status,
                finished_at,
                error_stage,
                error_clan_tag,
                error_war_tag,
                error_message,
                report_json,
                sync_run_id,
            ),
        )


async def fetch_one(
    connection: aiosqlite.Connection,
    sql: str,
    parameters: tuple[object, ...] = (),
) -> aiosqlite.Row | None:
    """Выполняет SELECT и возвращает одну строку."""

    cursor = await connection.execute(sql, parameters)
    return await cursor.fetchone()


async def fetch_all(
    connection: aiosqlite.Connection,
    sql: str,
    parameters: tuple[object, ...] = (),
) -> tuple[aiosqlite.Row, ...]:
    """Выполняет SELECT и возвращает все строки."""

    cursor = await connection.execute(sql, parameters)
    rows = await cursor.fetchall()
    return tuple(rows)


def _row_to_cwl_row_state(row: aiosqlite.Row) -> CwlRowState:
    """Преобразует SQLite-строку в `CwlRowState`."""

    return CwlRowState(
        season=as_str(row["season"], "season"),
        row_key=as_str(row["row_key"], "row_key"),
        clan_tag=as_str(row["clan_tag"], "clan_tag"),
        round_number=as_optional_int(row["round_number"], "round_number"),
        attacker_tag=as_optional_str(row["attacker_tag"], "attacker_tag"),
        marker=as_str(row["marker"], "marker"),
        technical_values=as_json_dict(row["technical_values_json"], "technical_values_json"),
        user_values=as_user_values(row["user_values_json"]),
        row_hash=as_optional_str(row["row_hash"], "row_hash"),
    )


def _row_to_column_profile(row: aiosqlite.Row) -> ColumnProfile:
    """Преобразует SQLite-строку в `ColumnProfile`."""

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


def _row_to_composition_player_state(row: aiosqlite.Row) -> CompositionPlayerState:
    """Преобразует SQLite-строку в `CompositionPlayerState`."""

    return CompositionPlayerState(
        player_tag=as_str(row["player_tag"], "player_tag"),
        status=as_composition_player_status(row["status"]),
        clan_tag=as_optional_str(row["clan_tag"], "clan_tag"),
        town_hall=as_optional_int(row["town_hall"], "town_hall"),
        nickname=as_optional_str(row["nickname"], "nickname"),
        exited_at=as_optional_str(row["exited_at"], "exited_at"),
        user_values=as_user_values(row["user_values_json"]),
        last_seen_at=as_optional_str(row["last_seen_at"], "last_seen_at"),
    )


def as_str(value: Any, field_name: str) -> str:
    """Проверяет строковое значение из SQLite."""

    if not isinstance(value, str):
        raise RepositoryError(f"Поле {field_name} должно быть строкой.")
    return value


def as_optional_str(value: Any, field_name: str) -> str | None:
    """Проверяет nullable-строку из SQLite."""

    if value is None:
        return None
    return as_str(value, field_name)


def as_int(value: Any, field_name: str) -> int:
    """Проверяет целое значение из SQLite."""

    if not isinstance(value, int) or isinstance(value, bool):
        raise RepositoryError(f"Поле {field_name} должно быть числом.")
    return value


def as_optional_int(value: Any, field_name: str) -> int | None:
    """Проверяет nullable-число из SQLite."""

    if value is None:
        return None
    return as_int(value, field_name)


def as_bool_int(value: Any, field_name: str) -> bool:
    """Проверяет SQLite boolean, сохранённый как 0/1."""

    if value == 0:
        return False
    if value == 1:
        return True
    raise RepositoryError(f"Поле {field_name} должно быть 0 или 1.")


def as_chat_status(value: Any) -> TelegramChatStatus:
    """Проверяет статус Telegram-чата."""

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
    """Проверяет тип таблицы профиля колонок."""

    raw = as_str(value, "table_type")
    if raw not in {"composition", "composition_active", "composition_exited", "cwl"}:
        raise RepositoryError(f"Некорректный table_type: {raw}.")
    return cast(TableType, raw)


def as_column_kind(value: Any) -> ColumnKind:
    """Проверяет kind профиля колонки."""

    raw = as_str(value, "kind")
    if raw not in {"system", "user", "service"}:
        raise RepositoryError(f"Некорректный column kind: {raw}.")
    return cast(ColumnKind, raw)


def as_column_value_type(value: Any) -> ColumnValueType:
    """Проверяет value_type профиля колонки."""

    raw = as_str(value, "value_type")
    if raw not in {"string", "integer", "datetime"}:
        raise RepositoryError(f"Некорректный column value_type: {raw}.")
    return cast(ColumnValueType, raw)


def as_composition_player_status(value: Any) -> CompositionPlayerStatus:
    """Проверяет статус игрока состава."""

    raw = as_str(value, "status")
    if raw not in {"active", "exited", "untracked"}:
        raise RepositoryError(f"Некорректный status игрока состава: {raw}.")
    return cast(CompositionPlayerStatus, raw)


def as_json_dict(value: Any, field_name: str) -> dict[str, object]:
    """Парсит JSON-объект из SQLite.

    Args:
        value: Значение SQLite.
        field_name: Имя поля для текста ошибки.

    Returns:
        JSON-словарь.
    """

    raw_json = as_str(value, field_name)
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise RepositoryError(f"{field_name} содержит битый JSON.") from exc
    if not isinstance(data, dict):
        raise RepositoryError(f"{field_name} должен быть JSON-объектом.")
    return dict(data)


def as_user_values(value: Any) -> dict[str, str]:
    """Парсит JSON пользовательских значений."""

    if value is None:
        return {}
    raw_json = as_str(value, "user_values_json")
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise RepositoryError("user_values_json содержит битый JSON.") from exc
    if not isinstance(data, dict):
        raise RepositoryError("user_values_json должен быть JSON-объектом.")
    result: dict[str, str] = {}
    for key, raw_value in data.items():
        if isinstance(key, str) and isinstance(raw_value, str):
            result[key] = raw_value
    return result
