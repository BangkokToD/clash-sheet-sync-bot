"""Публичный API repository-слоя.

Модуль сохраняет старый контракт `from repositories import ...`,
хотя реализация разнесена по focused modules.
"""

from __future__ import annotations

from .admins import AdminChatRepository, KnownAdminChat
from .base import (
    RepositoryError,
    as_bool_int,
    as_chat_status,
    as_column_kind,
    as_column_value_type,
    as_composition_player_status,
    as_int,
    as_json_dict,
    as_optional_int,
    as_optional_str,
    as_str,
    as_table_type,
    as_user_values,
    fetch_all,
    fetch_one,
)
from .bindings import RuntimeConfigRepository, SheetBindingRepository
from .chats import (
    ChatLifecycleRepository,
    PendingSheetLinkSetup,
    SyncStatusSummary,
    TelegramChatRepository,
)
from .clans import ClanSettingsRepository
from .columns import ColumnProfileRepository
from .composition_state import CompositionPlayerState, CompositionPlayerStateRepository
from .cwl_state import CwlRowState, CwlRowStateRepository
from .setup_tokens import SetupTokenRepository
from .sheet_blocks import SheetBlockRepository
from .sync_runs import SyncRunRepository
from .transfer_tokens import TransferToken, TransferTokenRepository

__all__ = [
    "AdminChatRepository",
    "ChatLifecycleRepository",
    "ClanSettingsRepository",
    "ColumnProfileRepository",
    "CompositionPlayerState",
    "CompositionPlayerStateRepository",
    "CwlRowState",
    "CwlRowStateRepository",
    "KnownAdminChat",
    "PendingSheetLinkSetup",
    "RepositoryError",
    "RuntimeConfigRepository",
    "SetupTokenRepository",
    "SheetBindingRepository",
    "SheetBlockRepository",
    "SyncRunRepository",
    "SyncStatusSummary",
    "TelegramChatRepository",
    "TransferToken",
    "TransferTokenRepository",
    "as_bool_int",
    "as_chat_status",
    "as_column_kind",
    "as_column_value_type",
    "as_composition_player_status",
    "as_int",
    "as_json_dict",
    "as_optional_int",
    "as_optional_str",
    "as_str",
    "as_table_type",
    "as_user_values",
    "fetch_all",
    "fetch_one",
]
