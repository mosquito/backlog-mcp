"""The onboarding board reads an explicit project without creating or selecting one."""

from aiohttp_tiny_mcp.client.base import BaseClient
from aiohttp_tiny_mcp.protocol.models import CallToolResult
from test_server import call

from backlog_mcp.auth import user_identity
from backlog_mcp.backlog import Backlog


async def test_board_missing_or_unknown_project_creates_nothing(
    client: BaseClient, backlog: Backlog
) -> None:
    for args in ({}, {"project": ""}, {"project": "missing"}, {"project": "./missing"}):
        result = await client.call_tool("project_board", args)
        assert isinstance(result, CallToolResult) and result.is_error
    assert await backlog.store.projects() == []


async def test_empty_board_context(client: BaseClient) -> None:
    project = await call(
        client,
        "project_save",
        root=".",
        name="Example",
        description="Project purpose",
        preferred_language="ru",
    )
    board = await call(client, "project_board", project=f"#{project['id']}")
    assert board["project"] == {
        "id": project["id"],
        "full_name": project["full_name"],
        "name": "Example",
        "description": "Project purpose",
        "preferred_language": "ru",
        "role": "owner",
        "archived": False,
        "roots": project["roots"],
    }
    assert board["tasks"] == {"total": 0, "open": 0, "claimed": 0, "unclaimed": 0, "blocked": 0}
    assert board["memory"] == 0
    assert board["unclaimed"] == []


async def test_board_top_ten_and_counts(client: BaseClient) -> None:
    project = await call(client, "project_save", root=".")
    await call(client, "task_add", title="Low", priority="low")
    normal = [await call(client, "task_add", title=f"Normal {i}") for i in range(11)]
    high = await call(client, "task_add", title="High", priority="high", status="doing")
    urgent = await call(client, "task_add", title="Urgent", priority="urgent", status="blocked")
    claimed = await call(client, "task_add", title="Already claimed", priority="urgent")
    closed = await call(client, "task_add", title="Closed", status="done", priority="urgent")
    await call(client, "task_add", title="Cancelled", status="cancelled")
    await call(client, "task_add", title="Deleted", status="deleted")
    for task in (claimed, closed):
        await call(
            client, "task_claim", task=task["id"], expected=None, assigned="local@here:agent"
        )
    other = await call(client, "project_save", root="../other")
    await call(client, "task_add", project=other["full_name"], title="Elsewhere", priority="urgent")
    board = await call(client, "project_board", project=project["full_name"])
    assert board["tasks"] == {
        "total": 18,
        "open": 15,
        "claimed": 1,
        "unclaimed": 14,
        "blocked": 1,
    }
    assert board["unclaimed"] == [
        {key: task[key] for key in ("id", "title", "status", "priority")}
        for task in (urgent, high, *normal[:8])
    ]
    # A board read is not a reservation: the winner disappears from subsequent reads.
    await call(client, "task_claim", task=urgent["id"], expected=None, assigned="local@here:winner")
    conflict = await client.call_tool(
        "task_claim", {"task": urgent["id"], "expected": None, "assigned": "local@here:loser"}
    )
    assert isinstance(conflict, CallToolResult) and conflict.is_error
    updated = await call(client, "project_board", project=project["full_name"])
    assert updated["tasks"]["claimed"] == 2
    assert updated["tasks"]["unclaimed"] == 13
    assert updated["unclaimed"][0]["id"] == high["id"]


async def test_board_memory_and_membership(client: BaseClient, backlog: Backlog) -> None:
    project = await call(client, "project_save", root=".")
    await call(client, "memory_save", title="Local", body="Local agreement")
    other = await call(client, "project_save", root="../other")
    await call(client, "memory_save", project=other["full_name"], title="Shared", shared=True)
    await call(client, "memory_save", project=other["full_name"], title="Other private")
    await backlog.store.register(user_identity("alice"), "Alice")
    alice = backlog.store.view(principal=user_identity("alice"))
    private = await alice.add_project(slug="secret", name="Secret", root="", computer="here")
    await alice.add_note(project_id=private.id, title="Secret shared", body="Hidden", shared=True)
    for reference in (private.full_name, f"#{private.id}"):
        result = await client.call_tool("project_board", {"project": reference})
        assert isinstance(result, CallToolResult) and result.is_error
    board = await call(client, "project_board", project=project["full_name"])
    assert board["memory"] == 2
    await alice.share(private.id, backlog.store.principal, "reader")
    board = await call(client, "project_board", project=private.full_name)
    assert board["project"]["role"] == "reader"
    assert board["memory"] == 2  # Secret shared plus the accessible shared note.
