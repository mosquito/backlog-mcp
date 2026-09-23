import asyncio
import json
from typing import Any

import pytest
from aiohttp_tiny_mcp.client.base import BaseClient
from aiohttp_tiny_mcp.protocol.models import CallToolResult, TextContent, TextResourceContents
from aiohttp_tiny_mcp.testing import connect
from helpers import mutate

from backlog_mcp import backlog as scope
from backlog_mcp import server as operations
from backlog_mcp.backlog import Backlog
from backlog_mcp.models import Status
from backlog_mcp.server import ACTIVE, registry


def check_public_identity(value: Any) -> None:
    if isinstance(value, dict):
        assert not {"computer", "computers", "this_computer"} & value.keys()
        if "principal" in value:
            assert isinstance(value["principal"], str) and "@" in value["principal"]
        for field in ("created_by", "updated_by"):
            if value.get(field) is not None:
                assert isinstance(value[field], str) and "@" in value[field]
        for item in value.values():
            check_public_identity(item)
    elif isinstance(value, list):
        for item in value:
            check_public_identity(item)


async def call(client: BaseClient, tool_name: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool_name, arguments)
    assert isinstance(result, CallToolResult), result
    assert not result.is_error, result
    # Older revisions return JSON text instead of structuredContent.
    if result.structured_content is not None:
        value = result.structured_content
    else:
        assert isinstance(result.content[0], TextContent)
        value = json.loads(result.content[0].text)
    assert isinstance(value, dict)
    check_public_identity(value)
    return value


async def resource(client: BaseClient, uri: str) -> Any:
    result = await client.read_resource(uri)
    assert len(result.contents) == 1
    content = result.contents[0]
    assert isinstance(content, TextResourceContents)
    assert content.mime_type == "application/json"
    return json.loads(content.text)


async def test_catalogue(client: BaseClient) -> None:
    tools = {tool.name: tool for tool in await client.list_tools()}
    assert set(tools) == {
        "whoami",
        "project_board",
        "project_share",
        "project_members",
        "project_list",
        "project_save",
        "project_delete",
        "task_add",
        "task_comment_add",
        "task_comment_list",
        "task_list",
        "task_get",
        "task_link",
        "task_claim",
        "task_assigned",
        "task_unlink",
        "task_update",
        "task_move",
        "task_delete",
        "memory_save",
        "memory_append",
        "memory_get",
        "memory_list",
        "memory_delete",
        "search",
        "stats",
    }
    assert "title" in tools["task_add"].input_schema["required"]
    assert "backlog" not in tools["task_add"].input_schema["properties"]
    assert tools["project_board"].input_schema["required"] == ["project"]
    assert {prompt.name for prompt in await client.list_prompts()} == {"standup", "groom"}
    assert {item.uri for item in await client.list_resources()} >= {
        "backlog://project",
        "backlog://projects",
        "backlog://tasks",
        "backlog://memory",
    }


async def test_board_does_not_change_directory_scope(client: BaseClient, backlog: Backlog) -> None:
    local = await call(client, "project_save", root=".")
    current = await call(client, "project_board", project=local["full_name"])
    assert current["project"]["roots"] == [f"local@here:{backlog.cwd}"]
    assert not {"database", "state", "origin", "version", "commit"} & current.keys()
    other = await call(client, "project_save", root="../other", name="Other Project")
    board = await call(client, "project_board", project=other["full_name"])
    assert board["project"]["id"] == other["id"]
    task = await call(client, "task_add", title="Here")
    assert task["project"] == local["full_name"]


async def test_board_requires_explicit_project_even_with_connection_scope(
    client: BaseClient, backlog: Backlog
) -> None:
    local = await call(client, "project_save", root=".")
    other = await call(client, "project_save", root="../other")
    backlog.named = other["slug"]
    result = await client.call_tool("project_board", {})
    assert isinstance(result, CallToolResult) and result.is_error
    board = await call(client, "project_board", project=local["full_name"])
    assert board["project"]["id"] == local["id"]
    task = await call(client, "task_add", title="Fixed")
    assert task["project"] == other["full_name"]


async def test_project_update_archive_and_counts(client: BaseClient) -> None:
    project = await call(client, "project_save", root=".", name="Example")
    repeated = await call(client, "project_save", root=".")
    assert repeated["id"] == project["id"]
    await call(client, "task_add", title="Open")
    await call(client, "task_add", title="Closed", status="done")
    listed = await call(client, "project_list")
    assert listed["count"] == 1
    assert listed["items"][0]["open_tasks"] == 1
    assert listed["items"][0]["total_tasks"] == 2

    updated = await call(
        client,
        "project_save",
        project=project["slug"],
        name="Renamed",
        description="A description",
        archived=True,
    )
    assert updated["name"] == "Renamed"
    assert updated["description"] == "A description"
    assert updated["slug"] == project["slug"]
    assert (await call(client, "project_list"))["count"] == 0
    assert (await call(client, "project_list", archived=True))["items"][0]["archived"]
    await call(client, "project_save", project=project["slug"], archived=False)
    assert (await call(client, "project_list"))["count"] == 1


async def test_project_language_create_update_omit_and_clear(client: BaseClient) -> None:
    default = await call(client, "project_save", root=".")
    assert default["preferred_language"] is None
    project = await call(client, "project_save", root="../other", preferred_language="ru")
    assert project["preferred_language"] == "ru"
    slug = project["slug"]
    renamed = await call(client, "project_save", project=slug, name="Renamed")
    assert renamed["preferred_language"] == "ru"
    rebound = await call(client, "project_save", project=slug, root="")
    assert rebound["preferred_language"] == "ru"

    updated = await call(client, "project_save", project=slug, preferred_language="en")
    assert updated["preferred_language"] == "en"
    listed = await call(client, "project_list")
    assert (
        next(item for item in listed["items"] if item["slug"] == slug)["preferred_language"] == "en"
    )
    assert (await call(client, "project_board", project=default["full_name"]))["project"][
        "preferred_language"
    ] is None
    saved = await call(client, "project_save", project=default["slug"], preferred_language="ru")
    assert saved["preferred_language"] == "ru"
    assert (await call(client, "project_board", project=default["full_name"]))["project"][
        "preferred_language"
    ] == "ru"
    assert (await resource(client, "backlog://project"))["project"]["preferred_language"] == "ru"

    cleared = await call(client, "project_save", project=slug, preferred_language=None)
    assert cleared["preferred_language"] is None
    kept = await call(client, "project_save", project=slug, description="Changed")
    assert kept["preferred_language"] is None


@pytest.mark.parametrize("closed_status", ["done", "cancelled", "deleted"])
async def test_task_close_and_reopen(client: BaseClient, closed_status: str) -> None:
    task = await call(client, "task_add", title="Implement feature", body="Details", tags=["api"])
    assert task["status"] == "todo"
    assert task["priority"] == "normal"
    assert task["closed_at"] is None
    closed = await call(client, "task_update", id=task["id"], status=closed_status)
    assert type(closed["closed_at"]) is int
    assert closed["body"] == "Details"
    history = await call(client, "task_comment_list", task_id=task["id"])
    assert history["items"][0]["body"] == f"Status changed: todo → {closed_status}"
    assert closed["tags"] == ["api"]
    assert (await call(client, "task_list"))["count"] == 0
    assert (await call(client, "task_list", status=[closed_status]))["items"][0]["id"] == task["id"]

    reopened = await call(
        client, "task_update", id=task["id"], status="doing", body="", tags=[], priority="urgent"
    )
    assert reopened["closed_at"] is None
    assert reopened["body"] == ""
    history = await call(client, "task_comment_list", task_id=task["id"])
    assert history["items"][-1]["body"] == f"Status changed: {closed_status} → doing"
    assert reopened["tags"] == []
    assert reopened["priority"] == "urgent"
    details = await call(client, "task_get", id=task["id"])
    assert all(details[key] == value for key, value in reopened.items())
    assert len(details["comments"]) == 2


async def test_task_filters_pagination_and_global_scope(client: BaseClient) -> None:
    low = await call(client, "task_add", title="Low", priority="low")
    high = await call(client, "task_add", title="High", priority="high", tags=["api"])
    urgent = await call(client, "task_add", title="Urgent", priority="urgent", tags=["api"])
    await call(client, "task_add", title="Done", status="done")
    other = await call(client, "project_save", root="../other")
    remote = await call(client, "task_add", title="Remote", project=other["slug"])

    listed = await call(client, "task_list", limit=1, offset=1)
    assert listed["count"] == 3
    assert [item["id"] for item in listed["items"]] == [high["id"]]
    tagged = await call(client, "task_list", tag="api")
    assert [item["id"] for item in tagged["items"]] == [urgent["id"], high["id"]]
    filtered = await call(client, "task_list", priority="low")
    assert [item["id"] for item in filtered["items"]] == [low["id"]]
    assert (await call(client, "task_list", all_projects=True))["count"] == 4
    scoped = await call(client, "task_list", project="../other/nested")
    assert [item["id"] for item in scoped["items"]] == [remote["id"]]


async def test_task_list_is_compact_unless_full_requested(client: BaseClient) -> None:
    task = await call(
        client,
        "task_add",
        title="Document lists",
        body=(
            "## Problem\n\nThis first paragraph has enough words to be shortened cleanly at its "
            "boundary without cutting a word in half, while retaining a useful description of "
            "the actual task for an agent reading a compact list.\n\nSecond paragraph."
        ),
    )

    compact = await call(client, "task_list")
    item = compact["items"][0]
    assert "body" not in item
    assert item["snippet"].startswith("This first paragraph")
    assert item["snippet"].endswith("…")
    assert len(item["snippet"]) <= 150
    assert "Second paragraph" not in item["snippet"]

    full = await call(client, "task_list", full=True)
    assert full["items"][0]["body"] == task["body"]


async def test_move_preserves_task_and_changes_project_counts(client: BaseClient) -> None:
    task = await call(client, "task_add", title="Move me", body="Keep this", tags=["api"])
    other = await call(client, "project_save", root="../other")
    moved = await call(client, "task_move", id=task["id"], project=other["slug"])
    assert moved["project"] == other["full_name"]
    for field in ("id", "title", "status", "priority", "tags", "created_at"):
        assert moved[field] == task[field]
    assert (await call(client, "task_list"))["count"] == 0
    assert (await call(client, "task_list", project=other["slug"]))["count"] == 1
    assert (await call(client, "task_get", id=task["id"]))["project"] == other["full_name"]
    assert moved["body"] == "Keep this"
    history = await call(client, "task_comment_list", task_id=task["id"])
    assert history["items"][0]["body"] == f"Moved: {task['project']} → {other['full_name']}"


async def test_search_prefixes_closed_tasks_and_updated_index(client: BaseClient) -> None:
    task = await call(
        client,
        "task_add",
        title="Authentication service",
        body="Token refresh",
        tags=["security"],
        status="done",
    )
    other = await call(client, "project_save", root="../other")
    await call(client, "task_add", title="Authentication service", project=other["slug"])
    found = await call(client, "search", query="auth serv")
    assert [item["id"] for item in found["tasks"]] == [task["id"]]
    assert (await call(client, "search", query="token secur"))["count"] == 1
    assert (await call(client, "search", query="auth", status=["todo"]))["count"] == 0
    assert (await call(client, "search", query="auth", all_projects=True))["count"] == 2
    assert (await call(client, "search", query='" OR *'))["count"] == 0

    await call(client, "task_update", id=task["id"], title="Login", body="", tags=[])
    assert (await call(client, "search", query="auth", min_score=1))["count"] == 0
    assert (await call(client, "search", query="login"))["count"] == 1


async def test_task_body_updates_replace_description(client: BaseClient) -> None:
    task = await call(client, "task_add", title="Track progress", body="Initial scope")
    comment = await call(
        client, "task_comment_add", task_id=task["id"], body="Implemented step one"
    )
    updated = await call(client, "task_update", id=task["id"], body="Revised scope")
    assert updated["body"] == "Revised scope"
    details = await call(client, "task_get", id=task["id"])
    assert details["comments"] == [comment]
    assert (await call(client, "task_update", id=task["id"], body=""))["body"] == ""
    assert (await call(client, "task_get", id=task["id"]))["comments"] == [comment]


async def test_task_status_changes_create_separate_history(client: BaseClient) -> None:
    task = await call(client, "task_add", title="Track status")
    updated = await call(client, "task_update", id=task["id"], status="doing")
    assert updated["body"] == ""
    await call(client, "task_update", id=task["id"], status="doing")
    history = await call(client, "task_comment_list", task_id=task["id"])
    assert history["count"] == 1
    assert history["items"][0]["body"] == "Status changed: todo → doing"
    assert history["items"][0]["kind"] == "status"


async def test_statistics(client: BaseClient) -> None:
    empty = await call(client, "stats")
    assert (empty["total"], empty["open"], empty["closed"], empty["memory"]) == (0, 0, 0, 0)
    await call(client, "task_add", title="Open", priority="urgent", tags=["api"])
    for status in ("doing", "blocked", "done", "cancelled"):
        await call(client, "task_add", title=status, status=status, tags=["docs"])
    await call(client, "memory_save", title="Local preference")
    other = await call(client, "project_save", root="../other")
    await call(client, "task_add", title="Elsewhere", status="blocked", project=other["slug"])
    await call(client, "memory_save", title="Shared agreement", project=other["slug"], shared=True)
    await call(client, "memory_save", title="Private agreement", project=other["slug"])
    stats = await call(client, "stats")
    assert (stats["total"], stats["open"], stats["closed"], stats["memory"]) == (5, 3, 2, 2)
    assert stats["by_status"] == {"todo": 1, "doing": 1, "blocked": 1, "done": 1, "cancelled": 1}
    assert stats["by_priority"] == {"urgent": 1, "normal": 4}
    assert set(stats["tags"]) == {"api", "docs"}
    global_stats = await call(client, "stats", all_projects=True)
    assert global_stats["project"] is None
    assert (
        global_stats["total"],
        global_stats["open"],
        global_stats["closed"],
        global_stats["memory"],
    ) == (6, 4, 2, 3)
    assert global_stats["by_status"]["blocked"] == 2
    scoped = await call(client, "stats", project=other["slug"])
    assert (scoped["total"], scoped["open"], scoped["closed"], scoped["memory"]) == (1, 1, 0, 2)


async def test_resources(client: BaseClient) -> None:
    task = await call(client, "task_add", title="Visible")
    await call(client, "task_add", title="Closed", status="done")
    assert await resource(client, f"backlog://tasks/{task['id']}") == await call(
        client, "task_get", id=task["id"]
    )
    assert [item["id"] for item in await resource(client, "backlog://tasks")] == [task["id"]]
    current = await resource(client, "backlog://project")
    assert current["project"]["full_name"] == task["project"]
    assert (await resource(client, "backlog://projects"))[0]["total_tasks"] == 2
    assert "invalid task id" in (await resource(client, "backlog://tasks/invalid"))["error"]
    assert "no task #999" in (await resource(client, "backlog://tasks/999"))["error"]


async def test_prompts_use_current_project(client: BaseClient) -> None:
    await call(client, "task_add", title="Ship release", status="done")
    await call(client, "task_add", title="Fix blocker", status="blocked", priority="high")
    other = await call(client, "project_save", root="../other")
    await call(client, "task_add", title="Other work", project=other["slug"])
    standup = await client.get_prompt("standup", {})
    assert isinstance(standup.messages[0].content, TextContent)
    text = standup.messages[0].content.text
    assert "done: Ship release" in text
    assert "blocked (high): Fix blocker" in text
    assert "Other work" not in text
    groom = await client.get_prompt("groom", {})
    assert isinstance(groom.messages[0].content, TextContent)
    text = groom.messages[0].content.text
    assert "Fix blocker" in text
    assert "Ship release" not in text
    assert "Other work" not in text


@pytest.mark.parametrize(
    ("argument", "prefix", "expected"),
    [("status", "do", ["doing", "done"]), ("priority", "h", ["high"]), ("unknown", "", [])],
)
async def test_enum_completions(
    client: BaseClient, argument: str, prefix: str, expected: list[str]
) -> None:
    result = await client.complete(
        {"type": "ref/prompt", "name": "groom"}, {"name": argument, "value": prefix}
    )
    assert result.completion.values == expected


async def test_scoped_completions(client: BaseClient) -> None:
    task = await call(client, "task_add", title="Local", tags=["api"])
    other = await call(client, "project_save", root="../other", name="Other")
    await call(client, "task_add", title="Remote", tags=["away"], project=other["slug"])
    for argument, prefix, expected in [("tag", "a", ["api"]), ("project", "ot", ["local/other"])]:
        result = await client.complete(
            {"type": "ref/prompt", "name": "groom"}, {"name": argument, "value": prefix}
        )
        assert result.completion.values == expected
    result = await client.complete(
        {"type": "ref/resource", "uri": "backlog://tasks/{id}"}, {"name": "id", "value": ""}
    )
    assert result.completion.values == [str(task["id"])]
    #: The note template names its argument `id` as well; the URI says which ids are wanted.
    saved = await call(client, "memory_save", title="dev vm")
    result = await client.complete(
        {"type": "ref/resource", "uri": "backlog://memory/{id}"}, {"name": "id", "value": ""}
    )
    assert result.completion.values == [str(saved["id"])]


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        ("task_get", {"id": 999}, "no task #999"),
        ("task_comment_add", {"task_id": 999, "body": "Missing"}, "no task #999"),
        ("task_comment_list", {"task_id": 999}, "no task #999"),
        ("task_update", {"id": 999, "title": "Missing"}, "no task #999"),
        ("task_delete", {"id": 999}, "no task #999"),
        ("memory_get", {"id": 999}, "no note #999"),
        ("memory_save", {"id": 999, "body": "Missing"}, "no note #999"),
        ("memory_append", {"id": 999, "text": "Missing"}, "no note #999"),
        ("memory_delete", {"id": 999}, "no note #999"),
        ("task_add", {"title": "Wrong project", "project": "missing"}, "no project"),
    ],
)
async def test_missing_objects_return_errors(
    client: BaseClient, name: str, arguments: dict[str, Any], message: str
) -> None:
    result = await client.call_tool(name, arguments)
    assert isinstance(result, CallToolResult)
    assert result.is_error
    assert isinstance(result.content[0], TextContent)
    assert message in result.content[0].text
    assert (await call(client, "project_list"))["count"] == 0


@pytest.mark.parametrize("arguments", [{"limit": 0}, {"limit": 501}, {"offset": -1}])
async def test_invalid_pagination_is_rejected(
    client: BaseClient, arguments: dict[str, int]
) -> None:
    result = await client.call_tool("task_list", arguments)
    assert isinstance(result, CallToolResult)
    assert result.is_error
    assert isinstance(result.content[0], TextContent)
    assert next(iter(arguments)) in result.content[0].text


@pytest.mark.parametrize("adapter", ["2025-11-25", "2026-07-28"])
@pytest.mark.parametrize("answer", [{"ok": True}, {"ok": False}, None])
@pytest.mark.parametrize("cross_project", [False, True])
async def test_delete_asks_only_across_projects(
    backlog: Backlog, adapter: str, answer: dict[str, bool] | None, cross_project: bool
) -> None:
    async with (
        asyncio.timeout(5),
        connect(registry, adapter=adapter, answers={"Delete note": answer}) as client,
    ):
        project = await call(client, "project_save", root="../other" if cross_project else ".")
        saved = await call(client, "memory_save", title="Delete candidate", project=project["slug"])
        deleted = await call(client, "memory_delete", id=saved["id"])
        expected = not cross_project or answer == {"ok": True}
        assert deleted["deleted"] is expected
        assert deleted["title"] == "Delete candidate"
        assert (
            await operations.read_note(await backlog.open(), backlog.actor, saved["id"]) is None
        ) is expected
        if expected:
            assert (await call(client, "search", query="candidate", all_projects=True))[
                "count"
            ] == 0


async def test_delete_without_elicitation(client: BaseClient, backlog: Backlog) -> None:
    saved = await call(client, "task_add", title="Delete me")
    result = await call(client, "task_delete", id=saved["id"])
    assert result["deleted"] is True
    task = await operations.read_task(await backlog.open(), backlog.actor, saved["id"])
    assert task is not None
    assert task.status == Status.DELETED


async def test_notes_save_replace_rename_and_append(client: BaseClient) -> None:
    saved = await call(
        client, "memory_save", title="dev vm", body="host: vm-3", tags=["Infra"], create_only=True
    )
    assert saved["tags"] == ["infra"]
    assert saved["shared"] is False
    refused = await client.call_tool(
        "memory_save",
        {
            "title": "dev vm",
            "body": "Do not overwrite",
            "create_only": True,
        },
    )
    assert refused.is_error
    assert (await call(client, "memory_get", id=saved["id"]))["body"] == "host: vm-3"
    refused_id = await client.call_tool(
        "memory_save",
        {
            "id": saved["id"],
            "body": "Do not overwrite",
            "create_only": True,
        },
    )
    assert refused_id.is_error
    assert (
        saved["project"]
        == (await call(client, "project_board", project=saved["project"]))["project"]["full_name"]
    )

    #: The same title in the same project is the same note; the body is replaced, not added to.
    replaced = await call(client, "memory_save", title="dev vm", body="host: vm-4")
    assert replaced["id"] == saved["id"]
    assert replaced["body"] == "host: vm-4"
    assert (await call(client, "memory_list"))["count"] == 1

    renamed = await call(client, "memory_save", id=saved["id"], title="dev host", shared=True)
    assert (renamed["title"], renamed["body"], renamed["shared"]) == (
        "dev host",
        "host: vm-4",
        True,
    )

    appended = await call(client, "memory_append", id=saved["id"], text="port 2222")
    assert appended["body"] == "host: vm-4\n\nport 2222"
    by_title = await call(client, "memory_append", title="dev host", text="user: deploy")
    assert by_title["body"].endswith("port 2222\n\nuser: deploy")

    assert await call(client, "memory_get", id=saved["id"]) == by_title
    assert await call(client, "memory_get", title="dev host") == by_title
    assert (await call(client, "stats"))["memory"] == 1


async def test_search_covers_tasks_and_notes_together(client: BaseClient) -> None:
    task = await call(client, "task_add", title="Move the dev vm", status="done")
    saved = await call(client, "memory_save", title="dev vm", body="host: vm-3.internal")
    await call(client, "memory_save", title="staging", body="https://staging.example")

    found = await call(client, "search", query="vm", limit=1)
    assert found["count"] == 2
    assert [item["id"] for item in found["tasks"]] == [task["id"]]
    assert [item["id"] for item in found["memory"]] == [saved["id"]]
    #: The excerpt marks the hit, and the id is what a task body links to as `note #id`.
    assert "[vm]" in found["memory"][0]["snippet"]
    assert (await call(client, "search", query="vm", status=["todo"]))["tasks"] == []
    assert (await call(client, "search", query="staging"))["memory"][0]["title"] == "staging"
    assert (await call(client, "search", query="nothing here", min_score=1))["count"] == 0

    await call(client, "memory_append", id=saved["id"], text="port 2222")
    assert (await call(client, "search", query="port"))["memory"][0]["id"] == saved["id"]


async def test_shared_notes_cross_projects(client: BaseClient) -> None:
    other = await call(client, "project_save", root="../other", name="Other")
    theirs = await call(
        client, "memory_save", title="staging", body="theirs", shared=True, project=other["slug"]
    )
    await call(client, "memory_save", title="private", body="theirs only", project=other["slug"])

    listed = await call(client, "memory_list")
    assert [item["id"] for item in listed["items"]] == [theirs["id"]]
    assert (await call(client, "memory_get", title="staging"))["project"] == other["full_name"]
    assert (await call(client, "search", query="theirs"))["count"] == 1

    #: Saving by title from here creates a local note; the shared note of the other project stays.
    mine = await call(client, "memory_save", title="staging", body="mine")
    assert mine["id"] != theirs["id"]
    assert (await call(client, "memory_get", title="staging"))["id"] == mine["id"]
    assert (await call(client, "memory_list"))["count"] == 2
    assert (await call(client, "memory_list", shared=True))["count"] == 1
    assert (await call(client, "memory_list", shared=False))["count"] == 1
    assert (await call(client, "memory_list", all_projects=True))["count"] == 3
    assert (await call(client, "search", query="theirs", all_projects=True, min_score=1))[
        "count"
    ] == 2

    result = await client.call_tool("memory_get", {"title": "nowhere"})
    assert isinstance(result, CallToolResult) and result.is_error
    assert isinstance(result.content[0], TextContent)
    assert "no note 'nowhere'" in result.content[0].text
    result = await client.call_tool("memory_save", {"body": "no title"})
    assert isinstance(result, CallToolResult) and result.is_error


async def test_note_list_is_compact_unless_full_requested(client: BaseClient) -> None:
    note = await call(
        client,
        "memory_save",
        title="List note",
        body=(
            "## Context\n\nThis note starts with enough prose to demonstrate a readable compact "
            "snippet that ends cleanly between words instead of cutting a fragment in half for "
            "the agent consuming it.\n\nIgnored second paragraph."
        ),
    )

    compact = await call(client, "memory_list")
    item = compact["items"][0]
    assert "body" not in item
    assert item["snippet"].startswith("This note starts")
    assert item["snippet"].endswith("…")
    assert len(item["snippet"]) <= 150
    assert "Ignored second paragraph" not in item["snippet"]

    full = await call(client, "memory_list", full=True)
    assert full["items"][0]["body"] == note["body"]


async def test_note_resources(client: BaseClient) -> None:
    saved = await call(client, "memory_save", title="dev vm", body="host: vm-3")
    assert await resource(client, f"backlog://memory/{saved['id']}") == saved
    assert [item["id"] for item in await resource(client, "backlog://memory")] == [saved["id"]]
    assert "invalid note id" in (await resource(client, "backlog://memory/x"))["error"]
    assert "no note #999" in (await resource(client, "backlog://memory/999"))["error"]


@pytest.mark.parametrize("adapter", ["2025-11-25", "2026-07-28"])
@pytest.mark.parametrize("answer", [{"ok": True}, {"ok": False}, None])
@pytest.mark.parametrize("cross_project", [False, True])
async def test_note_delete_asks_only_across_projects(
    backlog: Backlog, adapter: str, answer: dict[str, bool] | None, cross_project: bool
) -> None:
    async with (
        asyncio.timeout(5),
        connect(registry, adapter=adapter, answers={"Delete note": answer}) as client,
    ):
        project = await call(client, "project_save", root="../other" if cross_project else ".")
        saved = await call(client, "memory_save", title="Delete candidate", project=project["slug"])
        deleted = await call(client, "memory_delete", id=saved["id"])
        expected = not cross_project or answer == {"ok": True}
        assert deleted["deleted"] is expected
        assert deleted["title"] == "Delete candidate"
        assert (
            await operations.read_note(await backlog.open(), backlog.actor, saved["id"]) is None
        ) is expected
        if expected:
            assert (await call(client, "search", query="candidate", all_projects=True))[
                "count"
            ] == 0


@pytest.mark.parametrize("adapter", ["2025-11-25", "2026-07-28"])
@pytest.mark.parametrize("answer", [{"ok": True}, {"ok": False}, None])
async def test_project_delete_asks_the_user(
    backlog: Backlog, adapter: str, answer: dict[str, bool] | None
) -> None:
    async with (
        asyncio.timeout(5),
        connect(registry, adapter=adapter, answers={"Delete project": answer}) as client,
    ):
        await call(client, "project_save", root=".")
        other = await call(client, "project_save", root="../other")
        task = await call(client, "task_add", title="Doomed", project=other["slug"])
        result = await call(client, "project_delete", project=other["slug"])
        expected = answer == {"ok": True}
        assert result["deleted"] is expected
        assert (result["slug"], result["tasks"], result["memory"]) == (other["slug"], 1, 0)
        assert result["detail"] == ("" if expected else "cancelled")
        assert (
            await operations.read_task(await backlog.open(), backlog.actor, task["id"]) is None
        ) is expected
        assert (await call(client, "project_list"))["count"] == (1 if expected else 2)


async def test_project_delete_refuses_a_client_that_cannot_ask(backlog: Backlog) -> None:
    async with connect(registry, adapter="2025-11-25") as client:
        await call(client, "project_save", root=".")
        other = await call(client, "project_save", root="../other")
        await call(client, "task_add", title="Doomed", project=other["slug"])
        result = await client.call_tool("project_delete", {"project": other["slug"]})
        assert isinstance(result, CallToolResult) and result.is_error
        assert "cannot ask" in str(result.content)
        assert (await call(client, "project_list"))["count"] == 2


async def test_project_delete_takes_the_whole_board(backlog: Backlog) -> None:
    async with (
        asyncio.timeout(5),
        connect(registry, adapter="2025-11-25", answers={"Delete project": {"ok": True}}) as client,
    ):
        await call(client, "project_save", root=".")
        other = await call(client, "project_save", root="../other")
        task = await call(client, "task_add", title="Doomed", project=other["slug"])
        shared = await call(
            client, "memory_save", title="Kept in mind", project=other["slug"], shared=True
        )
        private = await call(client, "memory_save", title="Private", project=other["slug"])
        assert (await call(client, "project_list"))["count"] == 2

        deleted = await call(client, "project_delete", project=other["slug"])
        assert deleted["deleted"] is True
        assert (deleted["slug"], deleted["tasks"], deleted["memory"]) == (other["slug"], 1, 2)
        assert (await call(client, "project_list"))["count"] == 1
        assert await operations.read_task(await backlog.open(), backlog.actor, task["id"]) is None
        assert await operations.read_note(await backlog.open(), backlog.actor, shared["id"]) is None
        assert (
            await operations.read_note(await backlog.open(), backlog.actor, private["id"]) is None
        )
        assert (await call(client, "search", query="Doomed", all_projects=True))["count"] == 0
        #: The directory still decides.
        assert (await backlog.scope())[1].value == "directory"
        #: The directory is free again: registering it makes a new project.
        again = await call(client, "project_save", root="../other")
        assert again["id"] != other["id"]


async def test_project_delete_keeps_the_fixed_project(backlog: Backlog) -> None:
    project = await backlog.save(
        root=".", connection=await backlog.open(), transaction=backlog.transaction
    )
    fixed = backlog.view(project=project.slug)
    with pytest.raises(ValueError, match="fixed for this client"):
        await fixed.delete(project, await fixed.open(), fixed.transaction)
    assert await scope.read_project(await backlog.open(), backlog.actor, project.id) is not None


async def test_note_delete_without_elicitation(client: BaseClient, backlog: Backlog) -> None:
    saved = await call(client, "memory_save", title="Delete me")
    result = await call(client, "memory_delete", id=saved["id"])
    assert result["deleted"] is True
    assert await operations.read_note(await backlog.open(), backlog.actor, saved["id"]) is None


async def test_search_combines_prefix_and_semantic_matches(client: BaseClient) -> None:
    lexical = await call(client, "task_add", title="Password rotation", body="Rotate password")
    semantic = await call(client, "task_add", title="Сбросить пароль")
    note = await call(client, "memory_save", title="Password policy", body="Change password")
    result = await call(client, "search", query="password")
    assert [item["id"] for item in result["tasks"]] == [lexical["id"], semantic["id"]]
    assert len({item["id"] for item in result["tasks"]}) == 2
    assert "[password]" in result["tasks"][0]["snippet"]
    assert result["tasks"][1]["score"] > 0
    assert result["tasks"][1]["snippet"] is None
    assert [item["id"] for item in result["memory"]] == [note["id"]]
    strict = await call(client, "search", query="passw", min_score=1)
    assert [item["id"] for item in strict["tasks"]] == [lexical["id"]]
    assert strict["tasks"][0]["score"] is None
    assert [item["id"] for item in strict["memory"]] == [note["id"]]
    limited = await call(client, "search", query="password", limit=1)
    assert [item["id"] for item in limited["tasks"]] == [lexical["id"]]
    assert [item["id"] for item in limited["memory"]] == [note["id"]]


@pytest.mark.parametrize("entity", ["task", "note"])
async def test_cross_project_delete_requires_capable_client(backlog: Backlog, entity: str) -> None:
    async with connect(registry, adapter="2024-11-05") as client:
        other = await call(client, "project_save", root="../other")
        tool = "task_add" if entity == "task" else "memory_save"
        args: dict[str, Any] = {"title": "Elsewhere", "project": other["slug"]}
        if entity == "note":
            args["shared"] = True
        saved = await call(client, tool, **args)
        if entity == "task":
            result = await call(client, "task_delete", id=saved["id"])
            assert result["deleted"] is True
            task = await operations.read_task(await backlog.open(), backlog.actor, saved["id"])
            assert task is not None
            assert task.status == Status.DELETED
        else:
            result = await client.call_tool("memory_delete", {"id": saved["id"]})
            assert isinstance(result, CallToolResult) and result.is_error
            assert "cannot ask" in str(result.content)
            assert (await call(client, "memory_get", id=saved["id"]))["title"] == "Elsewhere"


async def test_note_delete_with_no_project_chosen_needs_a_question(backlog: Backlog) -> None:
    """A connection without a project deletes by id only after the user confirms."""
    token = ACTIVE.set(backlog.view(computer="laptop"))
    try:
        async with connect(registry, adapter="2024-11-05") as client:
            result = await client.call_tool("project_board", {})
            assert isinstance(result, CallToolResult) and result.is_error
            app = await call(client, "project_save", root="/srv/app", name="App")
            saved = await call(client, "memory_save", title="Nowhere", project=app["slug"])
            refused = await client.call_tool("memory_delete", {"id": saved["id"]})
            assert isinstance(refused, CallToolResult) and refused.is_error
            assert "cannot ask" in str(refused.content)
            assert (
                await operations.read_note(await backlog.open(), backlog.actor, saved["id"])
                is not None
            )
    finally:
        ACTIVE.reset(token)


@pytest.mark.parametrize("answer", [{"ok": True}, {"ok": False}])
async def test_note_delete_with_no_project_chosen_asks(
    backlog: Backlog, answer: dict[str, bool]
) -> None:
    token = ACTIVE.set(backlog.view(computer="laptop"))
    try:
        async with connect(
            registry, adapter="2025-11-25", answers={"No project is chosen": answer}
        ) as client:
            app = await call(client, "project_save", root="/srv/app", name="App")
            saved = await call(client, "memory_save", title="Nowhere", project=app["slug"])
            deleted = await call(client, "memory_delete", id=saved["id"])
            assert deleted["deleted"] is answer["ok"]
            assert (
                await operations.read_note(await backlog.open(), backlog.actor, saved["id"]) is None
            ) is answer["ok"]
    finally:
        ACTIVE.reset(token)


async def test_note_bodies_are_limited_to_1024_characters(client: BaseClient) -> None:
    saved = await call(client, "memory_save", title="limit", body="x" * 1024)
    assert len(saved["body"]) == 1024
    for name, arguments in [
        ("memory_save", {"title": "limit", "body": "x" * 1025}),
        ("memory_append", {"id": saved["id"], "text": "y"}),
    ]:
        result = await client.call_tool(name, arguments)
        assert isinstance(result, CallToolResult) and result.is_error
        assert isinstance(result.content[0], TextContent)
        assert "1024" in result.content[0].text
    assert (await call(client, "memory_get", id=saved["id"]))["body"] == "x" * 1024


async def test_whoami_does_not_create_a_project(client: BaseClient, backlog: Backlog) -> None:
    from backlog_mcp.records import LOCAL_PRINCIPAL

    result = await client.call_tool("whoami", {})
    assert not result.is_error
    assert result.structured_content == {
        "principal": f"{LOCAL_PRINCIPAL}@here",
        "handle": "local",
        "auth_type": "none",
        "assigned_tasks": 0,
    }
    assert await scope.read_projects(await backlog.open(), backlog.actor) == []


async def test_output_formats_principals_roots_and_seconds(
    client: BaseClient, backlog: Backlog
) -> None:
    task = await call(client, "task_add", title="Rounded timestamps")
    connection = await backlog.open()
    await connection.execute(
        "UPDATE tasks SET created_at=?, updated_at=? WHERE id=?",
        (1700000000.4, 1700000001.6, task["id"]),
    )
    await connection.commit()
    shown = await call(client, "task_get", id=task["id"])
    assert shown["created_at"] == 1700000000 and type(shown["created_at"]) is int
    assert shown["updated_at"] == 1700000002 and type(shown["updated_at"]) is int
    assert shown["created_by"] == "local@here"
    stored = await operations.read_task(await backlog.open(), backlog.actor, task["id"])
    assert stored and stored.created_at == 1700000000.4 and stored.updated_at == 1700000001.6
    assert await resource(client, f"backlog://tasks/{task['id']}") == shown
    project = (await call(client, "project_list"))["items"][0]
    assert project["roots"] == [f"local@here:{backlog.cwd}"]
    assert project["owner"] == "local@unknown"
    assert type(project["created_at"]) is int and type(project["updated_at"]) is int
    comment = await call(client, "task_comment_add", task_id=task["id"], body="Rounded")
    assert comment["principal"] == "local@here" and type(comment["created_at"]) is int
    memory = await call(client, "memory_save", title="A rule", body="A fact")
    assert type(memory["created_at"]) is int and type(memory["updated_at"]) is int
    await mutate(
        scope.register(await backlog.open(), backlog.actor, "alice@example.org", "Alice"),
        backlog.transaction,
    )
    shared = await call(client, "project_share", principal="alice%40example.org@laptop")
    assert any(item["principal"] == "alice%40example.org@unknown" for item in shared["items"])


async def test_initialize_includes_agent_workflow(client: BaseClient) -> None:
    from backlog_mcp.server import INSTRUCTIONS

    result = await client.initialize()
    assert result["instructions"] == INSTRUCTIONS
    for tool in ("project_board", "search", "task_comment_add", "memory_save"):
        assert tool in result["instructions"]


def test_attribution_strings_round_trip_and_schema() -> None:
    from backlog_mcp.models import Actor, Attributed

    actor = Actor(principal="alice@example.org", computer="laptop")
    item = Attributed(created_by=actor, updated_by=actor)
    encoded = item.model_dump(mode="json")
    assert encoded == {
        "created_by": "alice%40example.org@laptop",
        "updated_by": "alice%40example.org@laptop",
    }
    assert Attributed.model_validate(encoded) == item
    assert item.model_dump()["created_by"] == {
        "principal": "alice@example.org",
        "computer": "laptop",
    }
    assert Attributed().model_dump(mode="json") == {"created_by": None, "updated_by": None}
    schema = Attributed.model_json_schema(mode="serialization")
    for field in ("created_by", "updated_by"):
        assert schema["properties"][field]["anyOf"] == [{"type": "string"}, {"type": "null"}]


async def test_closed_timestamps_round_only_in_public_output(
    client: BaseClient, backlog: Backlog
) -> None:
    tasks = [
        await call(client, "task_add", title=f"Fractional closure {index}", status="done")
        for index in range(2)
    ]
    stamps = [1700000000.6, 1700000000.8]
    connection = await backlog.open()
    for task, stamp in zip(tasks, stamps, strict=True):
        await connection.execute(
            "UPDATE tasks SET closed_at = ?, updated_at = ? WHERE id = ?",
            (stamp, stamp, task["id"]),
        )
    await connection.commit()
    for full in (False, True):
        listed = await call(client, "task_list", status=["done"], order="recent", full=full)
        assert [item["id"] for item in listed["items"]] == [t["id"] for t in reversed(tasks)]
        for item in listed["items"]:
            assert type(item["closed_at"]) is int and item["closed_at"] == 1700000001
    found = await call(
        client, "search", query="Fractional closure", status=["done"], kinds=["tasks"]
    )
    assert len(found["tasks"]) == 2
    for item in found["tasks"]:
        assert type(item["closed_at"]) is int and item["closed_at"] == 1700000001
    for task, stamp in zip(tasks, stamps, strict=True):
        shown = await call(client, "task_get", id=task["id"])
        assert type(shown["closed_at"]) is int and shown["closed_at"] == round(stamp)
        assert (await resource(client, f"backlog://tasks/{task['id']}"))["closed_at"] == round(
            stamp
        )
        stored = await operations.read_task(connection, backlog.actor, task["id"])
        assert stored is not None and stored.closed_at == stamp and stored.updated_at == stamp
        reopened = await call(client, "task_update", id=task["id"], status="todo")
        assert reopened["closed_at"] is None
