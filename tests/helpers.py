"""Direct database assertions and transaction boundaries for application tests."""

from collections.abc import Awaitable, Sequence
from typing import Any

import aiosqlite

from backlog_mcp.transaction import Transaction


async def mutate[T](operation: Awaitable[T], transaction: Transaction) -> T:
    async with transaction:
        return await operation


async def rows(
    connection: aiosqlite.Connection, sql: str, parameters: Sequence[Any] = ()
) -> list[aiosqlite.Row]:
    async with connection.execute(sql, parameters) as cursor:
        return list(await cursor.fetchall())


async def row(
    connection: aiosqlite.Connection, sql: str, parameters: Sequence[Any] = ()
) -> aiosqlite.Row | None:
    found = await rows(connection, sql, parameters)
    return found[0] if found else None
