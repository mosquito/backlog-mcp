"""The first tool-list response contains the complete catalogue on both transports."""

import asyncio
import json
import sys

import pytest
from aiohttp.test_utils import TestServer
from aiohttp_tiny_mcp import Client, StdioClient
from aiohttp_tiny_mcp.protocol.core import Operation
from aiohttp_tiny_mcp.protocol.models import ListParams
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

from backlog_mcp.client import stop
from backlog_mcp.httpd import application
from backlog_mcp.server import registry
from backlog_mcp.version import VERSION


def check_catalogue(result):
    names = {tool["name"] for tool in result["tools"]}
    assert names == set(registry.tools)
    assert {"whoami", "task_update", "task_list"} <= names
    assert "nextCursor" not in result
    # Older revisions omit output schemas, so their catalogues are smaller.
    assert len(json.dumps(result, ensure_ascii=False).encode()) < 8 * 1024 * 1024


@pytest.mark.parametrize("version", AdapterSet.default().versions)
async def test_http_catalogue_is_complete(backlog, version):
    adapter = AdapterSet.default().by_version[version]
    async with (
        TestServer(application(backlog, console=False)) as server,
        Client(str(server.make_url("/mcp")), adapter) as client,
    ):
        initialized = await client.initialize()
        info = (
            initialized.get("serverInfo")
            or initialized["_meta"]["io.modelcontextprotocol/serverInfo"]
        )
        assert info == {"name": "backlog", "version": VERSION}
        check_catalogue(await client.request(Operation.LIST_TOOLS, ListParams()))


@pytest.mark.parametrize("version", AdapterSet.default().versions)
async def test_stdio_catalogue_is_complete(version):
    adapter = AdapterSet.default().by_version[version]
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import asyncio; from backlog_mcp.server import registry; "
        "from aiohttp_tiny_mcp import run_stdio; asyncio.run(run_stdio(registry))",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        limit=8 * 1024 * 1024,
    )
    assert process.stdout is not None and process.stdin is not None
    try:
        client = StdioClient(process.stdout, process.stdin, adapter)
        initialized = await client.initialize()
        info = (
            initialized.get("serverInfo")
            or initialized["_meta"]["io.modelcontextprotocol/serverInfo"]
        )
        assert info == {"name": "backlog", "version": VERSION}
        check_catalogue(await client.request(Operation.LIST_TOOLS, ListParams()))
    finally:
        await stop(process)
