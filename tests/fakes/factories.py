"""Фабрики тестовых моделей."""

from __future__ import annotations

from pathlib import Path

from clash_sheet_sync_bot.models import (
    AppConfig,
    ColumnProfile,
    RuntimeChatConfig,
    SheetBinding,
    SheetBlock,
    TrackedClan,
)
from clash_sheet_sync_bot.repositories import CompositionPlayerState


def make_tracked_clan(
    *,
    chat_id: int = -1001,
    tag: str = "#AAA111",
    name: str = "Alpha",
    sort_order: int = 10,
) -> TrackedClan:
    """Создаёт tracked clan для тестов."""

    return TrackedClan(
        chat_id=chat_id,
        clan_tag=tag,
        clan_name=name,
        sort_order=sort_order,
    )


def make_sheet_binding(
    *,
    chat_id: int = -1001,
    google_sheet_id: str = "sheet-id",
    spreadsheet_url: str = "https://docs.google.com/spreadsheets/d/sheet-id/edit",
    composition_sheet_name: str = "Состав",
    composition_sheet_id: int | None = 111,
    active_cwl_sheet_name: str = "CWL",
    active_cwl_sheet_id: int | None = 222,
    active_cwl_season: str | None = "2026-07",
    bot_state_sheet_name: str = "_bot_state",
    bot_state_sheet_id: int | None = 333,
    timezone: str = "Europe/Kyiv",
) -> SheetBinding:
    """Создаёт sheet binding для runtime config tests."""

    return SheetBinding(
        chat_id=chat_id,
        google_sheet_id=google_sheet_id,
        spreadsheet_url=spreadsheet_url,
        composition_sheet_name=composition_sheet_name,
        composition_sheet_id=composition_sheet_id,
        active_cwl_sheet_name=active_cwl_sheet_name,
        active_cwl_sheet_id=active_cwl_sheet_id,
        active_cwl_season=active_cwl_season,
        bot_state_sheet_name=bot_state_sheet_name,
        bot_state_sheet_id=bot_state_sheet_id,
        timezone=timezone,
    )


def make_app_config(
    *,
    telegram_bot_token: str = "telegram-token",
    coc_api_token: str = "coc-token",
    google_service_account_file: str | Path = "credentials.json",
    google_service_account_email: str | None = None,
    db_path: str | Path = "bot.db",
) -> AppConfig:
    """Создаёт AppConfig для setup/sync service tests."""

    return AppConfig(
        telegram_bot_token=telegram_bot_token,
        coc_api_token=coc_api_token,
        google_service_account_file=Path(google_service_account_file),
        google_service_account_email=google_service_account_email,
        db_path=Path(db_path),
        default_timezone="Europe/Kyiv",
        max_clans_per_chat=20,
        sync_cooldown_seconds=60,
        max_concurrent_syncs=3,
        cwl_war_concurrency_limit=5,
        admin_cache_ttl_seconds=300,
        setup_token_ttl_seconds=900,
        transfer_token_ttl_seconds=900,
        report_max_items=50,
    )


def make_column_profile(
    *,
    chat_id: int = -1001,
    table_type: str = "composition_active",
    column_key: str,
    title: str,
    visible: bool,
    kind: str,
    value_type: str,
    sort_order: int,
    is_active: bool = True,
) -> ColumnProfile:
    """Создаёт column profile для тестов."""

    return ColumnProfile(
        chat_id=chat_id,
        table_type=table_type,  # type: ignore[arg-type]
        column_key=column_key,
        title=title,
        visible=visible,
        kind=kind,  # type: ignore[arg-type]
        value_type=value_type,  # type: ignore[arg-type]
        sort_order=sort_order,
        is_active=is_active,
    )


def make_composition_column_profiles(chat_id: int = -1001) -> tuple[ColumnProfile, ...]:
    """Создаёт стандартные профили колонок active/exited состава."""

    return (
        make_column_profile(
            chat_id=chat_id,
            column_key="bot_key",
            title="__bot_key",
            visible=False,
            kind="service",
            value_type="string",
            sort_order=0,
        ),
        make_column_profile(
            chat_id=chat_id,
            column_key="number",
            title="№",
            visible=True,
            kind="system",
            value_type="integer",
            sort_order=10,
        ),
        make_column_profile(
            chat_id=chat_id,
            column_key="tag",
            title="Тег",
            visible=True,
            kind="system",
            value_type="string",
            sort_order=20,
        ),
        make_column_profile(
            chat_id=chat_id,
            column_key="town_hall",
            title="Ратуша",
            visible=True,
            kind="system",
            value_type="integer",
            sort_order=30,
        ),
        make_column_profile(
            chat_id=chat_id,
            column_key="nickname",
            title="Никнейм",
            visible=True,
            kind="system",
            value_type="string",
            sort_order=40,
        ),
        make_column_profile(
            chat_id=chat_id,
            column_key="note",
            title="Заметка",
            visible=True,
            kind="user",
            value_type="string",
            sort_order=45,
        ),
        make_column_profile(
            chat_id=chat_id,
            table_type="composition_exited",
            column_key="bot_key",
            title="__bot_key",
            visible=False,
            kind="service",
            value_type="string",
            sort_order=0,
        ),
        make_column_profile(
            chat_id=chat_id,
            table_type="composition_exited",
            column_key="number",
            title="№",
            visible=True,
            kind="system",
            value_type="integer",
            sort_order=10,
        ),
        make_column_profile(
            chat_id=chat_id,
            table_type="composition_exited",
            column_key="tag",
            title="Тег",
            visible=True,
            kind="system",
            value_type="string",
            sort_order=20,
        ),
        make_column_profile(
            chat_id=chat_id,
            table_type="composition_exited",
            column_key="town_hall",
            title="Ратуша",
            visible=True,
            kind="system",
            value_type="integer",
            sort_order=30,
        ),
        make_column_profile(
            chat_id=chat_id,
            table_type="composition_exited",
            column_key="nickname",
            title="Никнейм",
            visible=True,
            kind="system",
            value_type="string",
            sort_order=40,
        ),
        make_column_profile(
            chat_id=chat_id,
            table_type="composition_exited",
            column_key="note",
            title="Заметка",
            visible=True,
            kind="user",
            value_type="string",
            sort_order=45,
        ),
        make_column_profile(
            chat_id=chat_id,
            table_type="composition_exited",
            column_key="exited_at",
            title="Дата выхода",
            visible=True,
            kind="system",
            value_type="datetime",
            sort_order=50,
        ),
    )


def make_runtime_config(
    *,
    chat_id: int = -1001,
    active_clans: tuple[TrackedClan, ...] | None = None,
    column_profiles: tuple[ColumnProfile, ...] | None = None,
) -> RuntimeChatConfig:
    """Создаёт RuntimeChatConfig для composition tests."""

    clans = active_clans
    if clans is None:
        clans = (make_tracked_clan(chat_id=chat_id),)

    return RuntimeChatConfig(
        chat_id=chat_id,
        status="ready",
        sheet_binding=make_sheet_binding(chat_id=chat_id),
        active_clans=clans,
        column_profiles=column_profiles or make_composition_column_profiles(chat_id),
        timezone="Europe/Kyiv",
    )


def make_composition_state(
    *,
    player_tag: str,
    status: str,
    clan_tag: str | None,
    town_hall: int | None = 15,
    nickname: str | None = "Player",
    exited_at: str | None = None,
    user_values: dict[str, str] | None = None,
    last_seen_at: str | None = "2026-07-01T00:00:00+00:00",
) -> CompositionPlayerState:
    """Создаёт CompositionPlayerState для planning tests."""

    return CompositionPlayerState(
        player_tag=player_tag,
        status=status,  # type: ignore[arg-type]
        clan_tag=clan_tag,
        town_hall=town_hall,
        nickname=nickname,
        exited_at=exited_at,
        user_values=user_values or {},
        last_seen_at=last_seen_at,
    )


def make_sheet_block(
    *,
    chat_id: int = -1001,
    sheet_name: str = "Состав",
    sheet_id: int | None = 111,
    block_key: str,
    start_cell: str,
    rows_count: int = 3,
    columns_count: int = 4,
) -> SheetBlock:
    """Создаёт SheetBlock для repository/apply tests."""

    return SheetBlock(
        chat_id=chat_id,
        sheet_name=sheet_name,
        sheet_id=sheet_id,
        block_key=block_key,
        start_cell=start_cell,
        rows_count=rows_count,
        columns_count=columns_count,
    )
