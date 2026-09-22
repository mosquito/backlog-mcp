"""Connection lifetime and schema initialization."""

import asyncio
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite

from .migrate import migrate
from .names import owner_name
from .queries import QUERIES
from .transaction import Transaction

# Connection settings are an inline SQL exception; other queries belong in queries.sql.
PRAGMAS = {
    "busy_timeout": 5000,
    "foreign_keys": "ON",
    "journal_mode": "WAL",
}


@dataclass(eq=False)
class Database:
    path: Path
    connection: aiosqlite.Connection | None = field(default=None, init=False)
    transaction: Transaction | None = field(default=None, init=False)
    opening: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    @property
    def in_memory(self) -> bool:
        return str(self.path) == ":memory:"

    async def open(self, principal: str) -> aiosqlite.Connection:
        async with self.opening:
            if self.connection is not None:
                return self.connection
            if not self.in_memory:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = await aiosqlite.connect(str(self.path))
            connection.row_factory = aiosqlite.Row
            transaction = Transaction(connection, asyncio.Lock())
            try:
                for name, value in PRAGMAS.items():
                    # WAL can return BUSY immediately during concurrent startup.
                    for attempt in range(5):
                        try:
                            await connection.execute_fetchall(f"PRAGMA {name} = {value}")
                            break
                        except sqlite3.OperationalError as error:
                            if (
                                name != "journal_mode"
                                or error.sqlite_errorcode != sqlite3.SQLITE_BUSY
                                or attempt == 4
                            ):
                                raise
                            await asyncio.sleep(0.05)
                await migrate(connection, transaction=transaction)
                async with transaction:
                    await QUERIES.register_principal(
                        connection,
                        identity=principal,
                        handle=owner_name(principal),
                        name=owner_name(principal),
                    )
            except BaseException:
                await connection.close()
                raise
            self.connection = connection
            self.transaction = transaction
            return connection

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()
            self.connection = None
            self.transaction = None
