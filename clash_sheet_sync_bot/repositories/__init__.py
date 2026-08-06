"""Public API for focused SQLite repositories.

Repository classes and helpers are re-exported here so application code can
import them from `clash_sheet_sync_bot.repositories`.
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
from .cwl_forecast import (
    CwlForecastRepository,
    CwlForecastRound,
    CwlForecastSchedule,
    CwlForecastScheduleKey,
    CwlForecastSession,
    CwlForecastSessionConflictError,
)
from .cwl_state import CwlRowState, CwlRowStateRepository
from .raid_state import (
    RaidDataError,
    RaidPlayerState,
    RaidPlayerStateRepository,
    RaidSheetArchive,
    RaidSheetArchiveRepository,
    decode_raid_technical_values,
    encode_raid_technical_values,
)
from .setup_tokens import SetupTokenRepository
from .sheet_blocks import SheetBlockRepository
from .superadmin import (
    BotUserRepository,
    Broadcast,
    SuperadminRepository,
    SupportGroup,
    SupportSetupToken,
)
from .sync_runs import SyncRunRepository
from .transfer_tokens import TransferToken, TransferTokenRepository

__all__ = [
    "AdminChatRepository",
    "BotUserRepository",
    "Broadcast",
    "ChatLifecycleRepository",
    "ClanSettingsRepository",
    "ColumnProfileRepository",
    "CompositionPlayerState",
    "CompositionPlayerStateRepository",
    "CwlForecastRepository",
    "CwlForecastRound",
    "CwlForecastSchedule",
    "CwlForecastScheduleKey",
    "CwlForecastSession",
    "CwlForecastSessionConflictError",
    "CwlRowState",
    "CwlRowStateRepository",
    "KnownAdminChat",
    "PendingSheetLinkSetup",
    "RaidDataError",
    "RaidPlayerState",
    "RaidPlayerStateRepository",
    "RaidSheetArchive",
    "RaidSheetArchiveRepository",
    "RepositoryError",
    "RuntimeConfigRepository",
    "SetupTokenRepository",
    "SheetBindingRepository",
    "SheetBlockRepository",
    "SuperadminRepository",
    "SupportGroup",
    "SupportSetupToken",
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
    "decode_raid_technical_values",
    "encode_raid_technical_values",
    "fetch_all",
    "fetch_one",
]
