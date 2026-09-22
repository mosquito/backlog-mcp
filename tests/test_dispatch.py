"""Discovery and dispatch agree on tool names (task #85).

A tool that `tools/list` returns is callable under that exact name. Called with no arguments,
a tool fails on argument validation or returns a result; it never fails as an unknown tool.
"""

import pytest
from aiohttp_tiny_mcp.client.base import BaseClient, ClientError
from aiohttp_tiny_mcp.protocol.models import CallToolResult

UNKNOWN_TOOL = -32601


async def test_every_listed_tool_dispatches(client: BaseClient) -> None:
    listed = [tool.name for tool in await client.list_tools()]
    assert listed
    for name in listed:
        try:
            result = await client.call_tool(name, {})
        except ClientError as error:
            assert error.code != UNKNOWN_TOOL, f"{name}: {error}"
            assert "not found" not in str(error).lower(), f"{name}: {error}"
            continue
        if isinstance(result, CallToolResult) and result.is_error:
            assert "not found" not in str(result.content).lower(), f"{name}: {result}"


async def test_an_unlisted_tool_is_unknown(client: BaseClient) -> None:
    """The check above would catch a lookup failure: an unlisted name fails as unknown."""
    with pytest.raises(ClientError, match="unknown tool") as caught:
        await client.call_tool("backlog.project_current", {})
    assert caught.value.code == UNKNOWN_TOOL
