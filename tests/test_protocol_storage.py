"""Protocol state shares the application's connection without sharing transactions."""

import asyncio
from unittest.mock import patch

import aiosqlite
import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer
from aiohttp_tiny_mcp import Client
from aiohttp_tiny_mcp.testing import pick

from backlog_mcp.backlog import Backlog
from backlog_mcp.httpd import STORAGE, application
from backlog_mcp.protocol_storage import ProtocolHub, ProtocolSessions, ProtocolStorage


async def test_http_opens_one_connection(backlog: Backlog) -> None:
    with patch.object(aiosqlite, "connect", wraps=aiosqlite.connect) as connect:
        app = application(backlog, console=False)
        async with (
            TestServer(app) as server,
            ClientSession() as session,
            Client(str(server.make_url("/mcp")), pick("2025-06-18"), session=session) as client,
        ):
            await client.initialize()
            await client.call_tool("task_add", {"title": "Single connection"})
            await client.call_tool("project_list", {})
            storage = app[STORAGE]
            assert await storage.open() is backlog.database.connection
            await storage.sweep()
            assert connect.call_count == 1
        assert backlog.database.connection is None


async def test_protocol_writes_wait_for_application_rollback(backlog: Backlog) -> None:
    storage = ProtocolStorage(backlog)
    sessions = ProtocolSessions(storage)
    hub = ProtocolHub(storage)
    connection = await storage.open()
    pending = []
    try:
        with pytest.raises(ValueError, match="rollback"):
            async with backlog.transaction:
                pending = [
                    asyncio.create_task(sessions.create("session", {}, ttl_seconds=3600)),
                    asyncio.create_task(hub.publish("topic", {"message": "committed"})),
                    asyncio.create_task(storage.sweep()),
                ]
                await asyncio.sleep(0.05)
                assert all(not task.done() for task in pending)
                raise ValueError("rollback")
        await asyncio.wait_for(asyncio.gather(*pending), timeout=5)
        assert not connection.in_transaction
        assert await sessions.get("session") is not None
        assert len(await hub.after("topic", "0")) == 1
        assert await sessions.touch("session", ttl_seconds=3600)
        assert await sessions.save("session", {"saved": True}, expected_version=1, ttl_seconds=3600)
        assert not connection.in_transaction
        await storage.close()
        assert await backlog.open() is connection
        assert await storage.open() is connection
        assert (await sessions.get("session")).data == {"saved": True}
        await sessions.delete("session")
        await hub.delete("topic")
        assert await sessions.get("session") is None
        assert await hub.after("topic", "0") == []
    finally:
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await storage.close()
