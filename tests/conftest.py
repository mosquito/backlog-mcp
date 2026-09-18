from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp_tiny_mcp.client_base import BaseClient
from aiohttp_tiny_mcp.hub import MemoryHub
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.testing import connect

from backlog_mcp.backlog import Backlog
from backlog_mcp.server import ACTIVE, registry
from backlog_mcp.state import State
from backlog_mcp.store import Store


@pytest.fixture
async def backlog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Backlog]:
    # The global registry's notification condition must belong to this test's event loop.
    monkeypatch.setattr(registry, "hub", MemoryHub())
    root = tmp_path / "workspace"
    root.mkdir()
    # Keep checkout discovery inside the temporary directory.
    (root / ".git").mkdir()
    instance = Backlog(
        Store(tmp_path / "backlog.sqlite3"), State(tmp_path / "state.json"), cwd=root
    )
    token = ACTIVE.set(instance)
    try:
        yield instance
    finally:
        ACTIVE.reset(token)
        await instance.store.close()


@pytest.fixture(params=AdapterSet.default().versions)
async def client(backlog: Backlog, request: pytest.FixtureRequest) -> AsyncIterator[BaseClient]:
    """Exercise the application through each supported MCP protocol revision."""
    async with connect(registry, adapter=request.param) as connected:
        yield connected
