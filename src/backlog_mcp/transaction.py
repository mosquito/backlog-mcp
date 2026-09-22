"""A transaction and the lock shared by writers on its connection."""

import asyncio
from dataclasses import dataclass
from types import TracebackType
from typing import Self

import aiosqlite


@dataclass
class Transaction:
    connection: aiosqlite.Connection
    lock: asyncio.Lock

    async def __aenter__(self) -> Self:
        await self.lock.acquire()
        try:
            # Transaction control is an inline SQL exception; other queries belong in queries.sql.
            cursor = await self.connection.execute("BEGIN IMMEDIATE")
            await cursor.close()
        except BaseException:
            try:
                # Cancellation can arrive after SQLite queued BEGIN on its worker thread.
                await self.connection.rollback()
            finally:
                self.lock.release()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if exc_type is None:
                try:
                    await self.connection.commit()
                except BaseException:
                    await self.connection.rollback()
                    raise
            else:
                await self.connection.rollback()
        finally:
            self.lock.release()
