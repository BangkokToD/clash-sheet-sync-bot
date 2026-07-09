"""Общие pytest fixtures для SQLite-тестов."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest_asyncio

from clash_sheet_sync_bot.migrations import apply_migrations
from clash_sheet_sync_bot.storage import Database


@pytest_asyncio.fixture
async def migrated_connection(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    """Открывает временную SQLite DB и применяет актуальные migrations."""

    database = Database(tmp_path / "bot.db")
    async with database.connect() as connection:
        await apply_migrations(connection)
        yield connection
