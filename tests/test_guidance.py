import re
from typing import Any

import pytest
from aiohttp_tiny_mcp.client.base import BaseClient
from aiohttp_tiny_mcp.protocol.models import CallToolResult, TextContent, TextResourceContents

from backlog_mcp import guidance


@pytest.mark.parametrize("topic", [None, *guidance.TOPICS])
async def test_guide_tool_and_resource_share_markdown(
    client: BaseClient, topic: str | None
) -> None:
    uri = "backlog://guide" + (f"/{topic}" if topic else "")
    assert uri in {item.uri for item in await client.list_resources()}
    result = await client.call_tool("guide", {} if topic is None else {"topic": topic})
    assert isinstance(result, CallToolResult) and not result.is_error
    assert isinstance(result.content[0], TextContent)
    resource = await client.read_resource(uri)
    assert isinstance(resource.contents[0], TextResourceContents)
    assert resource.contents[0].mime_type == "text/markdown"
    assert result.content[0].text == resource.contents[0].text == guidance.read(topic)
    assert not re.search(r"note_\w+|current project", result.content[0].text)


@pytest.mark.parametrize("topic", ["unknown", "../server.py", ""])
async def test_unknown_guide_topic_lists_choices(client: BaseClient, topic: str) -> None:
    result = await client.call_tool("guide", {"topic": topic})
    assert isinstance(result, CallToolResult) and result.is_error
    assert isinstance(result.content[0], TextContent)
    assert "Unknown guide topic" in result.content[0].text
    assert all(name in result.content[0].text for name in guidance.TOPICS)


async def test_tool_descriptions_have_one_entry_point(client: BaseClient) -> None:
    def texts(value: Any) -> list[str]:
        if isinstance(value, dict):
            return [
                text
                for key, item in value.items()
                for text in (
                    [item]
                    if key in {"title", "description"} and isinstance(item, str)
                    else texts(item)
                )
            ]
        if isinstance(value, list):
            return [text for item in value for text in texts(item)]
        return []

    tools = {tool.name: tool for tool in await client.list_tools()}
    for name, tool in tools.items():
        for text in texts(tool.model_dump()):
            assert not re.search(r"\bnotes?\b|current project", text, re.I), (name, text)
            assert "ALWAYS" not in text and "Primary discovery" not in text, (name, text)
            if name != "project_board":
                assert "start here" not in text.lower(), (name, text)
    for name, title in {
        "memory_save": "SaveMemory",
        "memory_get": "ReadMemory",
        "memory_list": "ListMemory",
        "memory_append": "AppendMemory",
        "memory_delete": "MemoryId",
    }.items():
        assert tools[name].input_schema["title"] == title
    assert "Start here" in tools["project_board"].description
    assert "memory_save" in tools["memory_append"].description
