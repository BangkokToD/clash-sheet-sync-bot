"""Общие pytest fixtures для SQLite-тестов."""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

import aiosqlite
import pytest_asyncio

from migrations import apply_migrations
from storage import Database


@pytest_asyncio.fixture
async def migrated_connection(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    """Открывает временную SQLite DB и применяет актуальные migrations."""

    database = Database(tmp_path / "bot.db")
    async with database.connect() as connection:
        await apply_migrations(connection)
        yield connection