"""Доменные модели и общие типы проекта."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

TelegramChatStatus = Literal[
    "not_configured",
    "waiting_for_sheet",
    "waiting_for_access",
    "waiting_for_clans",
    "ready",
    "disabled",
]
TelegramChatType = Literal["private", "group", "supergroup", "channel"]
TelegramMemberStatus = Literal[
    "creator",
    "administrator",
    "member",
    "restricted",
    "left",
    "kicked",
]
TableType = Literal["composition_active", "composition_exited", "cwl"]
ColumnKind = Literal["system", "user", "service"]
ColumnValueType = Literal["string", "integer", "datetime"]
CompositionPlayerStatus = Literal["active", "exited", "untracked"]
SyncRunStatus = Literal["success", "error", "rate_limited", "skipped"]
SyncResultStatus = Literal["success", "error"]


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Глобальная конфигурация приложения из переменных окружения.

    Runtime-настройки Telegram-групп, Google-таблиц, кланов и колонок не входят
    в этот объект. Они хранятся в SQLite и собираются через repository-слой.

    Attributes:
        telegram_bot_token: Токен Telegram Bot API.
        coc_api_token: Токен Clash of Clans API.
        google_service_account_file: Путь к JSON-файлу service account.
        google_service_account_email: Ожидаемый email service account или `None`.
        db_path: Путь к SQLite-файлу.
        default_timezone: IANA-таймзона для новых привязок.
        max_clans_per_chat: Максимум активных кланов на Telegram-группу.
        sync_cooldown_seconds: Cooldown `/sync` для одного чата.
        max_concurrent_syncs: Глобальный лимит одновременных sync.
        cwl_war_concurrency_limit: Лимит конкурентных запросов CWL wars.
        admin_cache_ttl_seconds: TTL кэша Telegram-админов для обычных меню.
        setup_token_ttl_seconds: TTL токена подключения группы.
        transfer_token_ttl_seconds: TTL токена переноса таблицы.
        report_max_items: Максимум элементов diff-отчёта.
    """

    telegram_bot_token: str
    coc_api_token: str
    google_service_account_file: Path
    google_service_account_email: str | None
    db_path: Path
    default_timezone: str
    max_clans_per_chat: int
    sync_cooldown_seconds: int
    max_concurrent_syncs: int
    cwl_war_concurrency_limit: int
    admin_cache_ttl_seconds: int
    setup_token_ttl_seconds: int
    transfer_token_ttl_seconds: int
    report_max_items: int


@dataclass(frozen=True, slots=True)
class ClanConfig:
    """Конфигурация отслеживаемого клана в runtime-настройках чата.

    Имя оставлено совместимым со старым кодом. Новые слои должны получать эти
    объекты из SQLite, а не из `.env`.

    Attributes:
        tag: Нормализованный тег клана Clash of Clans.
        name: Человекочитаемое название клана для отчётов и листов.
    """

    tag: str
    name: str


@dataclass(frozen=True, slots=True)
class TrackedClan:
    """Активный отслеживаемый клан конкретного Telegram-чата.

    Attributes:
        chat_id: ID Telegram-чата.
        clan_tag: Нормализованный тег клана.
        clan_name: Название клана из CoC API.
        sort_order: Порядок вывода в таблицах и отчётах.
    """

    chat_id: int
    clan_tag: str
    clan_name: str
    sort_order: int

    def to_clan_config(self) -> ClanConfig:
        """Преобразует runtime-запись клана в совместимую модель.

        Returns:
            Короткая модель клана для доменных sync-модулей.
        """

        return ClanConfig(tag=self.clan_tag, name=self.clan_name)


@dataclass(frozen=True, slots=True)
class ColumnProfile:
    """Колонка видимого профиля таблицы.

    Attributes:
        chat_id: ID Telegram-чата.
        table_type: Тип таблицы.
        column_key: Стабильный внутренний ключ колонки.
        title: Видимый заголовок в Google Sheets.
        visible: Нужно ли выводить колонку в Google Sheets.
        kind: Тип происхождения колонки: system, user или service.
        value_type: Тип значения для валидации и будущего UI.
        sort_order: Порядок колонки внутри профиля.
        is_active: Активна ли колонка во внутренней модели.
    """

    chat_id: int
    table_type: TableType
    column_key: str
    title: str
    visible: bool
    kind: ColumnKind
    value_type: ColumnValueType
    sort_order: int
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class SheetBinding:
    """Активная привязка Telegram-чата к Google-таблице.

    Attributes:
        chat_id: ID Telegram-чата.
        google_sheet_id: ID Google Spreadsheet.
        spreadsheet_url: Исходная или нормализованная ссылка на Spreadsheet.
        composition_sheet_name: Название листа состава.
        composition_sheet_id: Числовой ID листа состава или `None`.
        active_cwl_sheet_name: Название активного CWL-листа.
        active_cwl_sheet_id: Числовой ID активного CWL-листа или `None`.
        active_cwl_season: Текущий CWL-сезон или `None`.
        bot_state_sheet_name: Название служебного листа.
        bot_state_sheet_id: Числовой ID служебного листа или `None`.
        timezone: IANA-таймзона чата.
    """

    chat_id: int
    google_sheet_id: str
    spreadsheet_url: str
    composition_sheet_name: str
    composition_sheet_id: int | None
    active_cwl_sheet_name: str
    active_cwl_sheet_id: int | None
    active_cwl_season: str | None
    bot_state_sheet_name: str
    bot_state_sheet_id: int | None
    timezone: str


@dataclass(frozen=True, slots=True)
class RuntimeChatConfig:
    """Runtime-настройки чата, необходимые для синхронизации.

    Attributes:
        chat_id: ID Telegram-чата.
        status: Статус настройки чата.
        sheet_binding: Активная привязка Google Sheets.
        active_clans: Активные отслеживаемые кланы в порядке вывода.
        column_profiles: Профили колонок всех управляемых таблиц.
        timezone: IANA-таймзона чата.
    """

    chat_id: int
    status: TelegramChatStatus
    sheet_binding: SheetBinding
    active_clans: tuple[TrackedClan, ...]
    column_profiles: tuple[ColumnProfile, ...]
    timezone: str


@dataclass(frozen=True, slots=True)
class ChatSyncConfig:
    """Сокращённый sync-конфиг для доменных модулей.

    Attributes:
        chat_id: ID Telegram-чата.
        google_sheet_id: ID Google Spreadsheet.
        spreadsheet_url: Ссылка на Google Spreadsheet.
        composition_sheet_name: Название листа состава.
        active_cwl_sheet_name: Название активного CWL-листа.
        active_cwl_sheet_id: Числовой ID активного CWL-листа или `None`.
        active_cwl_season: Текущий CWL-сезон или `None`.
        active_clans: Активные кланы в порядке вывода.
        column_profiles: Профили колонок.
        timezone: IANA-таймзона чата.
    """

    chat_id: int
    google_sheet_id: str
    spreadsheet_url: str
    composition_sheet_name: str
    active_cwl_sheet_name: str
    active_cwl_sheet_id: int | None
    active_cwl_season: str | None
    active_clans: tuple[TrackedClan, ...]
    column_profiles: tuple[ColumnProfile, ...]
    timezone: str

    @classmethod
    def from_runtime_config(cls, config: RuntimeChatConfig) -> ChatSyncConfig:
        """Создаёт sync-конфиг из полного runtime-конфига чата.

        Args:
            config: Полные runtime-настройки чата.

        Returns:
            Сокращённый конфиг для sync orchestration.
        """

        binding = config.sheet_binding
        return cls(
            chat_id=config.chat_id,
            google_sheet_id=binding.google_sheet_id,
            spreadsheet_url=binding.spreadsheet_url,
            composition_sheet_name=binding.composition_sheet_name,
            active_cwl_sheet_name=binding.active_cwl_sheet_name,
            active_cwl_sheet_id=binding.active_cwl_sheet_id,
            active_cwl_season=binding.active_cwl_season,
            active_clans=config.active_clans,
            column_profiles=config.column_profiles,
            timezone=config.timezone,
        )


@dataclass(frozen=True, slots=True)
class SetupToken:
    """Одноразовый токен подключения Telegram-группы.

    Attributes:
        token: Секретная часть команды `/connect`.
        created_by_user_id: Telegram user ID администратора, создавшего токен.
        expires_at: ISO-дата истечения токена.
        used_chat_id: ID чата, где токен использован, или `None`.
        used_at: ISO-дата использования или `None`.
        created_at: ISO-дата создания.
    """

    token: str
    created_by_user_id: int
    expires_at: str
    used_chat_id: int | None
    used_at: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class SyncDiffItem:
    """Один элемент технического diff-отчёта.

    Attributes:
        section: Раздел отчёта: composition или cwl.
        kind: Тип изменения внутри раздела.
        message: Уже подготовленный человекочитаемый текст изменения.
    """

    section: Literal["composition", "cwl"]
    kind: str
    message: str


@dataclass(frozen=True, slots=True)
class SyncDiff:
    """Diff одного запуска `/sync`.

    Attributes:
        items: Технические изменения, видимые в Telegram-отчёте.
        warnings: Предупреждения, не являющиеся sync-изменениями.
    """

    items: tuple[SyncDiffItem, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def has_changes(self) -> bool:
        """Проверяет наличие технических изменений.

        Returns:
            `True`, если в diff есть хотя бы один элемент.
        """

        return bool(self.items)


@dataclass(slots=True)
class SyncSettings:
    """Legacy-состояние последних ручных синхронизаций.

    Модель временно оставлена для совместимости старого `settings_store.py` до
    удаления JSON-хранилища в следующих коммитах. Новая runtime-архитектура
    должна использовать SQLite-таблицы `sync_runs` и `telegram_chats`.

    Attributes:
        last_composition_sync_at: ISO-дата последнего запуска состава.
        last_composition_sync_status: Статус последнего запуска состава.
        last_composition_sync_error: Текст последней ошибки состава.
        last_cwl_sync_at: ISO-дата последнего запуска CWL.
        last_cwl_sync_status: Статус последнего запуска CWL.
        last_cwl_sync_error: Текст последней ошибки CWL.
    """

    last_composition_sync_at: str | None = None
    last_composition_sync_status: SyncResultStatus | None = None
    last_composition_sync_error: str | None = None
    last_cwl_sync_at: str | None = None
    last_cwl_sync_status: SyncResultStatus | None = None
    last_cwl_sync_error: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> SyncSettings:
        """Создаёт legacy-настройки из JSON-словаря.

        Args:
            data: Словарь, прочитанный из `sync_settings.json`.

        Returns:
            Нормализованный объект настроек синхронизации.
        """

        return cls(
            last_composition_sync_at=_read_optional_str(data, "last_composition_sync_at"),
            last_composition_sync_status=_read_optional_status(
                data,
                "last_composition_sync_status",
            ),
            last_composition_sync_error=_read_optional_str(
                data,
                "last_composition_sync_error",
            ),
            last_cwl_sync_at=_read_optional_str(data, "last_cwl_sync_at"),
            last_cwl_sync_status=_read_optional_status(data, "last_cwl_sync_status"),
            last_cwl_sync_error=_read_optional_str(data, "last_cwl_sync_error"),
        )

    def to_dict(self) -> dict[str, str | None]:
        """Преобразует legacy-настройки в JSON-совместимый словарь.

        Returns:
            Словарь с полями `sync_settings.json`.
        """

        return {
            "last_composition_sync_at": self.last_composition_sync_at,
            "last_composition_sync_status": self.last_composition_sync_status,
            "last_composition_sync_error": self.last_composition_sync_error,
            "last_cwl_sync_at": self.last_cwl_sync_at,
            "last_cwl_sync_status": self.last_cwl_sync_status,
            "last_cwl_sync_error": self.last_cwl_sync_error,
        }


@dataclass(frozen=True, slots=True)
class SheetBlock:
    """Последний записанный ботом прямоугольник на листе.

    Attributes:
        chat_id: ID Telegram-чата.
        sheet_name: Название листа.
        sheet_id: Числовой ID листа или `None`.
        block_key: Стабильный ключ блока.
        start_cell: Левая верхняя ячейка блока.
        rows_count: Количество строк блока.
        columns_count: Количество колонок блока.
    """

    chat_id: int
    sheet_name: str
    sheet_id: int | None
    block_key: str
    start_cell: str
    rows_count: int
    columns_count: int


@dataclass(frozen=True, slots=True)
class SyncRun:
    """Запись истории запуска `/sync`.

    Attributes:
        id: ID записи `sync_runs`.
        chat_id: ID Telegram-чата.
        started_by_user_id: Telegram user ID инициатора.
        status: Статус запуска.
        started_at: ISO-дата принятия команды.
        finished_at: ISO-дата завершения или `None`.
        error_stage: Этап ошибки или `None`.
        error_clan_tag: Тег клана для ошибки или `None`.
        error_war_tag: Тег CWL-войны для ошибки или `None`.
        error_message: Текст ошибки или `None`.
        report_json: JSON-отчёт или `None`.
    """

    id: int
    chat_id: int
    started_by_user_id: int
    status: SyncRunStatus
    started_at: str
    finished_at: str | None = None
    error_stage: str | None = None
    error_clan_tag: str | None = None
    error_war_tag: str | None = None
    error_message: str | None = None
    report_json: str | None = None


def normalize_tag(value: str) -> str:
    """Нормализует тег Clash of Clans.

    Args:
        value: Исходный тег из `.env`, API или Google Sheets.

    Returns:
        Тег без пробелов по краям и в верхнем регистре.

    Raises:
        ValueError: Если тег пустой или не начинается с `#`.
    """

    normalized = value.strip().upper()
    if not normalized:
        raise ValueError("Тег не может быть пустым.")
    if not normalized.startswith("#"):
        raise ValueError(f"Тег должен начинаться с '#': {normalized}")
    return normalized


def _read_optional_str(data: dict[str, object], key: str) -> str | None:
    """Читает необязательную строку из JSON-словаря.

    Args:
        data: Словарь с данными.
        key: Имя поля.

    Returns:
        Строка или `None`, если поле пустое/отсутствует.
    """

    value = data.get(key)
    return value if isinstance(value, str) else None


def _read_optional_status(data: dict[str, object], key: str) -> SyncResultStatus | None:
    """Читает legacy-статус синхронизации из JSON-словаря.

    Args:
        data: Словарь с данными.
        key: Имя поля.

    Returns:
        `success`, `error` или `None`.
    """

    value = data.get(key)
    if value == "success" or value == "error":
        return value
    return None
