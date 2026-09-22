"""Serialize application writes and keep commit ownership with the outer transaction."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from weakref import WeakKeyDictionary

import aiosqlite

from .queries import QUERIES

# One shared writer lock per event loop. Locks cannot be reused across asyncio.run calls.
LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakKeyDictionary()


@dataclass
class Transaction:
    connection: aiosqlite.Connection
    owner: asyncio.Task[object] | None
    failed: bool = False


ACTIVE: ContextVar[Transaction | None] = ContextVar("ACTIVE", default=None)


@asynccontextmanager
async def transaction(connection: aiosqlite.Connection) -> AsyncIterator[None]:
    """Commit once, or roll back on failure, while holding the shared writer lock.

    Nested writes in the same task reuse the transaction. Child tasks cannot inherit ownership.
    A caught nested failure still aborts the outer transaction.
    """
    owner = asyncio.current_task()
    active = ACTIVE.get()
    if active is not None and active.owner is owner:
        if active.connection is not connection:
            raise RuntimeError("cannot nest transactions on different connections")
        try:
            yield
        except BaseException:
            active.failed = True
            raise
        return
    loop = asyncio.get_running_loop()
    lock = LOCKS.setdefault(loop, asyncio.Lock())
    async with lock:
        if connection.in_transaction:
            raise RuntimeError("connection already has an unmanaged transaction")
        active = Transaction(connection, owner)
        token = ACTIVE.set(active)
        try:
            await QUERIES.begin_transaction(connection)
            yield
            if active.failed:
                raise RuntimeError("nested transaction failed")
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
        finally:
            ACTIVE.reset(token)
