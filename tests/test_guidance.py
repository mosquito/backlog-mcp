import json
import re
from typing import Any

import pytest
from aiohttp.test_utils import TestServer
from aiohttp_tiny_mcp import Client
from aiohttp_tiny_mcp.client.base import BaseClient
from aiohttp_tiny_mcp.protocol.models import TextResourceContents
from aiohttp_tiny_mcp.protocol.selection import AdapterSet

from backlog_mcp import guidance
from backlog_mcp.httpd import application


@pytest.mark.parametrize("topic", [None, *guidance.TOPICS])
async def test_guide_resource_has_packaged_markdown(client: BaseClient, topic: str | None) -> None:
    uri = "backlog://guide" + (f"/{topic}" if topic else "")
    assert uri in {item.uri for item in await client.list_resources()}
    resource = await client.read_resource(uri)
    assert isinstance(resource.contents[0], TextResourceContents)
    assert resource.contents[0].mime_type == "text/markdown"
    assert resource.contents[0].text == guidance.read(topic)
    assert not re.search(r"note_\w+|current project", resource.contents[0].text)


@pytest.mark.parametrize("topic", ["unknown", "../server.py", ""])
def test_unknown_guide_topic_lists_choices(topic: str) -> None:
    with pytest.raises(ValueError, match="Unknown guide topic") as error:
        guidance.read(topic)
    assert all(name in str(error.value) for name in guidance.TOPICS)


@pytest.mark.parametrize("version", AdapterSet.default().versions)
async def test_http_resource_tools_read_guide(backlog, version):
    adapter = AdapterSet.default().by_version[version]
    async with (
        TestServer(application(backlog, console=False)) as server,
        Client(str(server.make_url("/mcp?resources=tool")), adapter) as client,
    ):
        await client.initialize()
        names = {tool.name for tool in await client.list_tools()}
        assert {"mcp_resources_list", "mcp_resources_read"} <= names
        assert "guide" not in names
        listed = await client.call_tool("mcp_resources_list", {"query": "backlog://guide"})
        assert not listed.is_error
        entries = json.loads(listed.content[0].text)["items"]
        assert {entry["uri"] for entry in entries} == {
            "backlog://guide",
            *(f"backlog://guide/{topic}" for topic in guidance.TOPICS),
        }
        for topic in (None, *guidance.TOPICS):
            uri = "backlog://guide" + (f"/{topic}" if topic else "")
            result = await client.call_tool("mcp_resources_read", {"uri": uri})
            assert not result.is_error
            contents = json.loads(result.content[0].text)["contents"][0]
            assert contents == {
                "uri": uri,
                "mimeType": "text/markdown",
                "text": guidance.read(topic),
            }


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
        assert "backlog://guide/" in tool.description, name
        assert "guide(" not in tool.description, name
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
