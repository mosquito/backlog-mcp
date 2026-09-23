"""Protocol state on the application connection and transaction lock."""

from collections.abc import Mapping
from typing import Any

import aiosqlite
from aiohttp_tiny_mcp.storage.hub import Cursor, Event
from aiohttp_tiny_mcp.storage.sessions import SessionRecord
from aiohttp_tiny_mcp.storage.sqlite import SCHEMA, SqliteHub, SqliteSessionStore, SqliteStorage

from .backlog import Backlog


class ProtocolStorage(SqliteStorage):
    """Borrow the connection; only Backlog owns its lifetime."""

    def __init__(self, backlog: Backlog) -> None:
        super().__init__(backlog.database.path)
        self.backlog = backlog

    async def open(self) -> aiosqlite.Connection:
        async with self.opening:
            connection = await self.backlog.open()
            if self.connection is not connection:
                async with self.backlog.transaction:
                    # The dependency owns this schema. Do not use executescript:
                    # it would commit the transaction before running the schema.
                    for statement in SCHEMA.split(";"):
                        if statement.strip():
                            async with connection.execute(statement):
                                pass
                self.connection = connection
            return connection

    async def close(self) -> None:
        async with self.opening:
            self.connection = None

    async def sweep(self) -> None:
        await self.open()
        async with self.backlog.transaction:
            await super().sweep()


class ProtocolSessions(SqliteSessionStore):
    storage: ProtocolStorage

    async def create(self, session_id: str, data: Mapping[str, Any], *, ttl_seconds: int) -> bool:
        await self.storage.open()
        async with self.storage.backlog.transaction:
            return await super().create(session_id, data, ttl_seconds=ttl_seconds)

    async def get(self, session_id: str) -> SessionRecord | None:
        await self.storage.open()
        async with self.storage.backlog.transaction.lock:
            return await super().get(session_id)

    async def save(
        self,
        session_id: str,
        data: Mapping[str, Any],
        *,
        expected_version: int,
        ttl_seconds: int,
    ) -> bool:
        await self.storage.open()
        async with self.storage.backlog.transaction:
            return await super().save(
                session_id, data, expected_version=expected_version, ttl_seconds=ttl_seconds
            )

    async def touch(self, session_id: str, *, ttl_seconds: int) -> bool:
        await self.storage.open()
        async with self.storage.backlog.transaction:
            return await super().touch(session_id, ttl_seconds=ttl_seconds)

    async def delete(self, session_id: str) -> None:
        await self.storage.open()
        async with self.storage.backlog.transaction:
            await super().delete(session_id)


class ProtocolHub(SqliteHub):
    storage: ProtocolStorage

    async def publish(self, topic: str, message: Mapping[str, Any]) -> Cursor:
        await self.storage.open()
        async with self.storage.backlog.transaction:
            return await super().publish(topic, message)

    async def position(self, topic: str) -> Cursor:
        await self.storage.open()
        async with self.storage.backlog.transaction.lock:
            return await super().position(topic)

    async def after(self, topic: str, cursor: Cursor) -> list[Event]:
        await self.storage.open()
        async with self.storage.backlog.transaction.lock:
            return await super().after(topic, cursor)

    async def delete(self, topic: str) -> None:
        await self.storage.open()
        async with self.storage.backlog.transaction:
            await super().delete(topic)
