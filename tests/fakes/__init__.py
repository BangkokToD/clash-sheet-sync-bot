"""Публичный API тестовых fake/factory helpers."""

from __future__ import annotations

from .factories import (
    make_app_config,
    make_column_profile,
    make_composition_column_profiles,
    make_composition_state,
    make_runtime_config,
    make_sheet_binding,
    make_sheet_block,
    make_tracked_clan,
)
from .sheets import FakeSheetsClient, RecordingCompositionRepository, RecordingSheetBlockRepository
from .telegram import FakeTelegram, RecordingAccessService

__all__ = [
    "FakeSheetsClient",
    "FakeTelegram",
    "RecordingAccessService",
    "RecordingCompositionRepository",
    "RecordingSheetBlockRepository",
    "make_app_config",
    "make_column_profile",
    "make_composition_column_profiles",
    "make_composition_state",
    "make_runtime_config",
    "make_sheet_binding",
    "make_sheet_block",
    "make_tracked_clan",
]
