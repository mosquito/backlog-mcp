import asyncio
import json
import re
from typing import Any

import pytest
from aiohttp_tiny_mcp.client_base import BaseClient
from aiohttp_tiny_mcp.models import CallToolResult, TextContent, TextResourceContents
from aiohttp_tiny_mcp.testing import connect

from backlog_mcp.backlog import Backlog
from backlog_mcp.models import Status
from backlog_mcp.server import registry


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
        "project_current",
        "project_list",
        "project_save",
        "project_delete",
        "task_add",
        "task_list",
        "task_get",
        "task_update",
        "task_move",
        "task_delete",
        "note_save",
        "note_append",
        "note_get",
        "note_list",
        "note_delete",
        "search",
        "stats",
    }
    assert "title" in tools["task_add"].input_schema["required"]
    assert "backlog" not in tools["task_add"].input_schema["properties"]
    assert {prompt.name for prompt in await client.list_prompts()} == {"standup", "groom"}
    assert {item.uri for item in await client.list_resources()} >= {
        "backlog://project",
        "backlog://projects",
        "backlog://tasks",
        "backlog://notes",
    }


async def test_scope_follows_the_directory(client: BaseClient, backlog: Backlog) -> None:
    current = await call(client, "project_current")
    assert current["origin"] == "created"
    assert current["project"]["roots"] == [{"computer": "here", "root": str(backlog.cwd)}]
    assert current["database"] == str(backlog.store.path)
    assert current["state"] == str(backlog.state.path)
    assert (await call(client, "project_current"))["origin"] == "directory"

    other = await call(client, "project_save", root="../other", name="Other Project")
    #: Another registered project changes nothing: the directory still decides.
    assert (await call(client, "project_current"))["project"]["id"] == current["project"]["id"]
    task = await call(client, "task_add", title="Elsewhere", project=other["slug"])
    assert task["project"] == other["slug"]
    task = await call(client, "task_add", title="Here")
    assert task["project"] == current["project"]["slug"]


async def test_the_fixed_project_beats_the_directory(client: BaseClient, backlog: Backlog) -> None:
    current = await call(client, "project_current")
    other = await call(client, "project_save", root="../other")
    backlog.named = other["slug"]
    fixed = await call(client, "project_current")
    assert fixed["origin"] == "argument"
    assert fixed["project"]["id"] == other["id"]
    assert fixed["project"]["id"] != current["project"]["id"]


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


@pytest.mark.parametrize("closed_status", ["done", "cancelled"])
async def test_task_close_and_reopen(client: BaseClient, closed_status: str) -> None:
    task = await call(client, "task_add", title="Implement feature", body="Details", tags=["api"])
    assert task["status"] == "todo"
    assert task["priority"] == "normal"
    assert task["closed_at"] is None
    closed = await call(client, "task_update", id=task["id"], status=closed_status)
    assert closed["closed_at"] is not None
    assert re.fullmatch(
        rf"Details\n---\n# UPDATE: \d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}:\d{{2}}:\d{{2}}Z\n"
        rf"Status changed: todo → {closed_status}\n",
        closed["body"],
    )
    assert closed["tags"] == ["api"]
    assert (await call(client, "task_list"))["count"] == 0
    assert (await call(client, "task_list", status=[closed_status]))["items"][0]["id"] == task["id"]

    reopened = await call(
        client, "task_update", id=task["id"], status="doing", body="", tags=[], priority="urgent"
    )
    assert reopened["closed_at"] is None
    assert re.fullmatch(
        rf"Details\n---\n# UPDATE: \d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}:\d{{2}}:\d{{2}}Z\n"
        rf"Status changed: todo → {closed_status}\n---\n"
        r"# UPDATE: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\n"
        rf"Status changed: {closed_status} → doing\n",
        reopened["body"],
    )
    assert reopened["tags"] == []
    assert reopened["priority"] == "urgent"
    assert await call(client, "task_get", id=task["id"]) == reopened


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
    assert moved["project"] == other["slug"]
    for field in ("id", "title", "status", "priority", "tags", "created_at"):
        assert moved[field] == task[field]
    assert (await call(client, "task_list"))["count"] == 0
    assert (await call(client, "task_list", project=other["slug"]))["count"] == 1
    assert (await call(client, "task_get", id=task["id"]))["project"] == other["slug"]
    assert re.search(
        rf"# UPDATE: \d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}:\d{{2}}:\d{{2}}Z\n"
        rf"Moved: {re.escape(task['project'])} → {re.escape(other['slug'])}\n$",
        moved["body"],
    )


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


async def test_task_body_updates_append_timestamped_history(client: BaseClient) -> None:
    task = await call(client, "task_add", title="Track progress", body="Initial scope")

    updated = await call(
        client, "task_update", id=task["id"], body="\nImplemented the first step\n"
    )

    assert re.fullmatch(
        r"Initial scope\n---\n# UPDATE: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\n"
        r"Implemented the first step\n",
        updated["body"],
    )


async def test_task_status_changes_append_timestamped_history(client: BaseClient) -> None:
    task = await call(client, "task_add", title="Track status")

    updated = await call(client, "task_update", id=task["id"], status="doing")

    assert re.fullmatch(
        r"---\n# UPDATE: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\nStatus changed: todo → doing\n",
        updated["body"],
    )


async def test_statistics(client: BaseClient) -> None:
    empty = await call(client, "stats")
    assert (empty["total"], empty["open"], empty["closed"], empty["notes"]) == (0, 0, 0, 0)
    await call(client, "task_add", title="Open", priority="urgent", tags=["api"])
    for status in ("doing", "blocked", "done", "cancelled"):
        await call(client, "task_add", title=status, status=status, tags=["docs"])
    await call(client, "note_save", title="Local preference")
    other = await call(client, "project_save", root="../other")
    await call(client, "task_add", title="Elsewhere", status="blocked", project=other["slug"])
    await call(client, "note_save", title="Shared agreement", project=other["slug"], shared=True)
    await call(client, "note_save", title="Private agreement", project=other["slug"])
    stats = await call(client, "stats")
    assert (stats["total"], stats["open"], stats["closed"], stats["notes"]) == (5, 3, 2, 2)
    assert stats["by_status"] == {"todo": 1, "doing": 1, "blocked": 1, "done": 1, "cancelled": 1}
    assert stats["by_priority"] == {"urgent": 1, "normal": 4}
    assert set(stats["tags"]) == {"api", "docs"}
    global_stats = await call(client, "stats", all_projects=True)
    assert global_stats["project"] is None
    assert (
        global_stats["total"],
        global_stats["open"],
        global_stats["closed"],
        global_stats["notes"],
    ) == (6, 4, 2, 3)
    assert global_stats["by_status"]["blocked"] == 2
    scoped = await call(client, "stats", project=other["slug"])
    assert (scoped["total"], scoped["open"], scoped["closed"], scoped["notes"]) == (1, 1, 0, 2)


async def test_resources(client: BaseClient) -> None:
    task = await call(client, "task_add", title="Visible")
    await call(client, "task_add", title="Closed", status="done")
    assert await resource(client, f"backlog://tasks/{task['id']}") == task
    assert [item["id"] for item in await resource(client, "backlog://tasks")] == [task["id"]]
    current = await resource(client, "backlog://project")
    assert current["project"]["slug"] == task["project"]
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
    for argument, prefix, expected in [("tag", "a", ["api"]), ("project", "ot", ["other"])]:
        result = await client.complete(
            {"type": "ref/prompt", "name": "groom"}, {"name": argument, "value": prefix}
        )
        assert result.completion.values == expected
    result = await client.complete(
        {"type": "ref/resource", "uri": "backlog://tasks/{id}"}, {"name": "id", "value": ""}
    )
    assert result.completion.values == [str(task["id"])]
    #: The note template names its argument `id` as well; the URI says which ids are wanted.
    saved = await call(client, "note_save", title="dev vm")
    result = await client.complete(
        {"type": "ref/resource", "uri": "backlog://notes/{id}"}, {"name": "id", "value": ""}
    )
    assert result.completion.values == [str(saved["id"])]


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        ("task_get", {"id": 999}, "no task #999"),
        ("task_update", {"id": 999, "title": "Missing"}, "no task #999"),
        ("task_delete", {"id": 999}, "no task #999"),
        ("note_get", {"id": 999}, "no note #999"),
        ("note_save", {"id": 999, "body": "Missing"}, "no note #999"),
        ("note_append", {"id": 999, "text": "Missing"}, "no note #999"),
        ("note_delete", {"id": 999}, "no note #999"),
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
        saved = await call(client, "note_save", title="Delete candidate", project=project["slug"])
        deleted = await call(client, "note_delete", id=saved["id"])
        expected = not cross_project or answer == {"ok": True}
        assert deleted["deleted"] is expected
        assert deleted["title"] == "Delete candidate"
        assert (await backlog.store.note(saved["id"]) is None) is expected
        if expected:
            assert (await call(client, "search", query="candidate", all_projects=True))[
                "count"
            ] == 0


async def test_delete_without_elicitation(client: BaseClient, backlog: Backlog) -> None:
    saved = await call(client, "task_add", title="Delete me")
    result = await call(client, "task_delete", id=saved["id"])
    assert result["deleted"] is True
    task = await backlog.store.task(saved["id"])
    assert task is not None
    assert task.status == Status.DELETED


async def test_notes_save_replace_rename_and_append(client: BaseClient) -> None:
    saved = await call(client, "note_save", title="dev vm", body="host: vm-3", tags=["Infra"])
    assert saved["tags"] == ["infra"]
    assert saved["shared"] is False
    assert saved["project"] == (await call(client, "project_current"))["project"]["slug"]

    #: The same title in the same project is the same note; the body is replaced, not added to.
    replaced = await call(client, "note_save", title="dev vm", body="host: vm-4")
    assert replaced["id"] == saved["id"]
    assert replaced["body"] == "host: vm-4"
    assert (await call(client, "note_list"))["count"] == 1

    renamed = await call(client, "note_save", id=saved["id"], title="dev host", shared=True)
    assert (renamed["title"], renamed["body"], renamed["shared"]) == (
        "dev host",
        "host: vm-4",
        True,
    )

    appended = await call(client, "note_append", id=saved["id"], text="port 2222")
    assert appended["body"] == "host: vm-4\n\nport 2222"
    by_title = await call(client, "note_append", title="dev host", text="user: deploy")
    assert by_title["body"].endswith("port 2222\n\nuser: deploy")

    assert await call(client, "note_get", id=saved["id"]) == by_title
    assert await call(client, "note_get", title="dev host") == by_title
    assert (await call(client, "stats"))["notes"] == 1


async def test_search_covers_tasks_and_notes_together(client: BaseClient) -> None:
    task = await call(client, "task_add", title="Move the dev vm", status="done")
    saved = await call(client, "note_save", title="dev vm", body="host: vm-3.internal")
    await call(client, "note_save", title="staging", body="https://staging.example")

    found = await call(client, "search", query="vm", limit=1)
    assert found["count"] == 2
    assert [item["id"] for item in found["tasks"]] == [task["id"]]
    assert [item["id"] for item in found["notes"]] == [saved["id"]]
    #: The excerpt marks the hit, and the id is what a task body links to as `note #id`.
    assert "[vm]" in found["notes"][0]["snippet"]
    assert (await call(client, "search", query="vm", status=["todo"]))["tasks"] == []
    assert (await call(client, "search", query="staging"))["notes"][0]["title"] == "staging"
    assert (await call(client, "search", query="nothing here", min_score=1))["count"] == 0

    await call(client, "note_append", id=saved["id"], text="port 2222")
    assert (await call(client, "search", query="port"))["notes"][0]["id"] == saved["id"]


async def test_shared_notes_cross_projects(client: BaseClient) -> None:
    other = await call(client, "project_save", root="../other", name="Other")
    theirs = await call(
        client, "note_save", title="staging", body="theirs", shared=True, project=other["slug"]
    )
    await call(client, "note_save", title="private", body="theirs only", project=other["slug"])

    listed = await call(client, "note_list")
    assert [item["id"] for item in listed["items"]] == [theirs["id"]]
    assert (await call(client, "note_get", title="staging"))["project"] == other["slug"]
    assert (await call(client, "search", query="theirs"))["count"] == 1

    #: Saving by title from here creates a local note; the shared note of the other project stays.
    mine = await call(client, "note_save", title="staging", body="mine")
    assert mine["id"] != theirs["id"]
    assert (await call(client, "note_get", title="staging"))["id"] == mine["id"]
    assert (await call(client, "note_list"))["count"] == 2
    assert (await call(client, "note_list", shared=True))["count"] == 1
    assert (await call(client, "note_list", shared=False))["count"] == 1
    assert (await call(client, "note_list", all_projects=True))["count"] == 3
    assert (await call(client, "search", query="theirs", all_projects=True, min_score=1))[
        "count"
    ] == 2

    result = await client.call_tool("note_get", {"title": "nowhere"})
    assert isinstance(result, CallToolResult) and result.is_error
    assert isinstance(result.content[0], TextContent)
    assert "no note 'nowhere'" in result.content[0].text
    result = await client.call_tool("note_save", {"body": "no title"})
    assert isinstance(result, CallToolResult) and result.is_error


async def test_note_list_is_compact_unless_full_requested(client: BaseClient) -> None:
    note = await call(
        client,
        "note_save",
        title="List note",
        body=(
            "## Context\n\nThis note starts with enough prose to demonstrate a readable compact "
            "snippet that ends cleanly between words instead of cutting a fragment in half for "
            "the agent consuming it.\n\nIgnored second paragraph."
        ),
    )

    compact = await call(client, "note_list")
    item = compact["items"][0]
    assert "body" not in item
    assert item["snippet"].startswith("This note starts")
    assert item["snippet"].endswith("…")
    assert len(item["snippet"]) <= 150
    assert "Ignored second paragraph" not in item["snippet"]

    full = await call(client, "note_list", full=True)
    assert full["items"][0]["body"] == note["body"]


async def test_note_resources(client: BaseClient) -> None:
    saved = await call(client, "note_save", title="dev vm", body="host: vm-3")
    assert await resource(client, f"backlog://notes/{saved['id']}") == saved
    assert [item["id"] for item in await resource(client, "backlog://notes")] == [saved["id"]]
    assert "invalid note id" in (await resource(client, "backlog://notes/x"))["error"]
    assert "no note #999" in (await resource(client, "backlog://notes/999"))["error"]


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
        saved = await call(client, "note_save", title="Delete candidate", project=project["slug"])
        deleted = await call(client, "note_delete", id=saved["id"])
        expected = not cross_project or answer == {"ok": True}
        assert deleted["deleted"] is expected
        assert deleted["title"] == "Delete candidate"
        assert (await backlog.store.note(saved["id"]) is None) is expected
        if expected:
            assert (await call(client, "search", query="candidate", all_projects=True))[
                "count"
            ] == 0


async def test_project_delete_reports_before_it_deletes(
    client: BaseClient, backlog: Backlog
) -> None:
    await call(client, "project_current")
    other = await call(client, "project_save", root="../other")
    task = await call(client, "task_add", title="Doomed", project=other["slug"])
    shared = await call(
        client, "note_save", title="Kept in mind", project=other["slug"], shared=True
    )
    private = await call(client, "note_save", title="Private", project=other["slug"])

    preview = await call(client, "project_delete", project=other["slug"])
    assert preview["deleted"] is False
    assert (preview["tasks"], preview["notes"], preview["shared_notes"]) == (1, 2, 1)
    assert "confirm=true" in preview["detail"]
    assert (await call(client, "project_list"))["count"] == 2

    deleted = await call(client, "project_delete", project=other["slug"], confirm=True)
    assert deleted["deleted"] is True
    assert (deleted["slug"], deleted["tasks"], deleted["notes"]) == (other["slug"], 1, 2)
    assert (await call(client, "project_list"))["count"] == 1
    assert await backlog.store.task(task["id"]) is None
    assert await backlog.store.note(shared["id"]) is None
    assert await backlog.store.note(private["id"]) is None
    assert (await call(client, "search", query="Doomed", all_projects=True))["count"] == 0
    #: The directory still decides.
    assert (await call(client, "project_current"))["origin"] == "directory"
    #: The directory is free again: registering it makes a new project.
    again = await call(client, "project_save", root="../other")
    assert again["id"] != other["id"]


async def test_project_delete_keeps_the_fixed_project(backlog: Backlog) -> None:
    project = await backlog.save(root=".")
    fixed = backlog.view(project=project.slug)
    with pytest.raises(ValueError, match="fixed for this client"):
        await fixed.delete(project)
    assert await backlog.store.project(project.id) is not None


async def test_note_delete_without_elicitation(client: BaseClient, backlog: Backlog) -> None:
    saved = await call(client, "note_save", title="Delete me")
    result = await call(client, "note_delete", id=saved["id"])
    assert result["deleted"] is True
    assert await backlog.store.note(saved["id"]) is None


async def test_search_combines_prefix_and_semantic_matches(client: BaseClient) -> None:
    lexical = await call(client, "task_add", title="Password rotation", body="Rotate password")
    semantic = await call(client, "task_add", title="Сбросить пароль")
    note = await call(client, "note_save", title="Password policy", body="Change password")
    result = await call(client, "search", query="password")
    assert [item["id"] for item in result["tasks"]] == [lexical["id"], semantic["id"]]
    assert len({item["id"] for item in result["tasks"]}) == 2
    assert "[password]" in result["tasks"][0]["snippet"]
    assert result["tasks"][1]["score"] > 0
    assert result["tasks"][1]["snippet"] is None
    assert [item["id"] for item in result["notes"]] == [note["id"]]
    strict = await call(client, "search", query="passw", min_score=1)
    assert [item["id"] for item in strict["tasks"]] == [lexical["id"]]
    assert strict["tasks"][0]["score"] is None
    assert [item["id"] for item in strict["notes"]] == [note["id"]]
    limited = await call(client, "search", query="password", limit=1)
    assert [item["id"] for item in limited["tasks"]] == [lexical["id"]]
    assert [item["id"] for item in limited["notes"]] == [note["id"]]


@pytest.mark.parametrize("entity", ["task", "note"])
async def test_cross_project_delete_requires_capable_client(backlog: Backlog, entity: str) -> None:
    async with connect(registry, adapter="2024-11-05") as client:
        other = await call(client, "project_save", root="../other")
        tool = "task_add" if entity == "task" else "note_save"
        args: dict[str, Any] = {"title": "Elsewhere", "project": other["slug"]}
        if entity == "note":
            args["shared"] = True
        saved = await call(client, tool, **args)
        if entity == "task":
            result = await call(client, "task_delete", id=saved["id"])
            assert result["deleted"] is True
            task = await backlog.store.task(saved["id"])
            assert task is not None
            assert task.status == Status.DELETED
        else:
            result = await call(client, "note_delete", id=saved["id"])
            assert result["deleted"] is False
            assert "cannot confirm" in result["detail"]
            assert (await call(client, "note_get", id=saved["id"]))["title"] == "Elsewhere"
            confirmed = await call(client, "note_delete", id=saved["id"], confirm=True)
            assert confirmed["deleted"] is True


async def test_note_bodies_are_limited_to_1024_characters(client: BaseClient) -> None:
    saved = await call(client, "note_save", title="limit", body="x" * 1024)
    assert len(saved["body"]) == 1024
    for name, arguments in [
        ("note_save", {"title": "limit", "body": "x" * 1025}),
        ("note_append", {"id": saved["id"], "text": "y"}),
    ]:
        result = await client.call_tool(name, arguments)
        assert isinstance(result, CallToolResult) and result.is_error
        assert isinstance(result.content[0], TextContent)
        assert "1024" in result.content[0].text
    assert (await call(client, "note_get", id=saved["id"]))["body"] == "x" * 1024
