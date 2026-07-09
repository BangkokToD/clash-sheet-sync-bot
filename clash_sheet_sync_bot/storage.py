"""SQLite-подключение, PRAGMA и транзакции."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite


class StorageError(RuntimeError):
    """Ошибка SQLite-хранилища."""


class Database:
    """Фабрика SQLite-подключений проекта.

    Args:
        path: Путь к SQLite-файлу.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        """Открывает подключение к SQLite с обязательными PRAGMA.

        Yields:
            Активное подключение `aiosqlite.Connection`.

        Raises:
            StorageError: Если подключение не удалось открыть или настроить.
        """

        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = await aiosqlite.connect(self.path)
        except OSError as exc:
            raise StorageError(f"Не удалось открыть SQLite DB: {self.path}.") from exc

        try:
            connection.row_factory = aiosqlite.Row
            await configure_connection(connection)
            yield connection
        finally:
            await connection.close()


async def configure_connection(connection: aiosqlite.Connection) -> None:
    """Настраивает SQLite-подключение для runtime-хранилища.

    Args:
        connection: Открытое SQLite-подключение.
    """

    await connection.execute("PRAGMA journal_mode = WAL")
    await connection.execute("PRAGMA foreign_keys = ON")
    await connection.execute("PRAGMA busy_timeout = 5000")


@asynccontextmanager
async def transaction(connection: aiosqlite.Connection) -> AsyncIterator[aiosqlite.Connection]:
    """Выполняет блок кода внутри SQLite-транзакции.

    Args:
        connection: Открытое SQLite-подключение.

    Yields:
        То же подключение внутри транзакции.

    Raises:
        Exception: Любая ошибка вызывающего кода после rollback.
    """

    await connection.execute("BEGIN")
    try:
        yield connection
    except Exception:
        await connection.rollback()
        raise
    else:
        await connection.commit()
