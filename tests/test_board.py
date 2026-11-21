"""The onboarding board reads an explicit project without creating or selecting one."""

from aiohttp_tiny_mcp.client.base import BaseClient
from aiohttp_tiny_mcp.protocol.models import CallToolResult
from helpers import mutate
from test_server import call

from backlog_mcp import backlog as scope
from backlog_mcp import server as operations
from backlog_mcp.auth import user_identity
from backlog_mcp.backlog import Backlog


async def test_board_missing_or_unknown_project_creates_nothing(
    client: BaseClient, backlog: Backlog
) -> None:
    for args in ({}, {"project": ""}, {"project": "missing"}, {"project": "./missing"}):
        result = await client.call_tool("project_board", args)
        assert isinstance(result, CallToolResult) and result.is_error
    assert await scope.read_projects(await backlog.open(), backlog.actor) == []


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
    assert board["tasks"] == {
        "total": 0,
        "open": 0,
        "claimed": 0,
        "unclaimed": 0,
        "blocked": 0,
        "done": 0,
        "cancelled": 0,
        "deleted": 0,
    }
    assert board["memory"] == 0
    assert board["ready"] == board["claimed"] == {"count": 0, "items": []}


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
        "done": 1,
        "cancelled": 1,
        "deleted": 1,
    }
    assert board["ready"]["count"] == 13
    assert board["claimed"] == {
        "count": 1,
        "items": [{"id": claimed["id"], "title": claimed["title"]}],
    }
    assert board["ready"]["items"] == [
        {key: task[key] for key in ("id", "title")} for task in (high, *normal[:9])
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
    assert updated["ready"]["items"][0]["id"] == high["id"]


async def test_board_memory_and_membership(client: BaseClient, backlog: Backlog) -> None:
    project = await call(client, "project_save", root=".")
    await call(client, "memory_save", title="Local", body="Local agreement")
    other = await call(client, "project_save", root="../other")
    await call(client, "memory_save", project=other["full_name"], title="Shared", shared=True)
    await call(client, "memory_save", project=other["full_name"], title="Other private")
    await mutate(
        scope.register(await backlog.open(), backlog.actor, user_identity("alice"), "Alice"),
        backlog.transaction,
    )
    alice = backlog.view(principal=user_identity("alice"))
    private = await mutate(
        scope.add_project(
            await alice.open(), alice.actor, slug="secret", name="Secret", root="", computer="here"
        ),
        alice.transaction,
    )
    await mutate(
        operations.add_note(
            await alice.open(),
            alice.actor,
            project_id=private.id,
            title="Secret shared",
            body="Hidden",
            shared=True,
        ),
        alice.transaction,
    )
    for reference in (private.full_name, f"#{private.id}"):
        result = await client.call_tool("project_board", {"project": reference})
        assert isinstance(result, CallToolResult) and result.is_error
    board = await call(client, "project_board", project=project["full_name"])
    assert board["memory"] == 1
    await mutate(
        scope.share(await alice.open(), alice.actor, private.id, backlog.principal, "reader"),
        alice.transaction,
    )
    board = await call(client, "project_board", project=private.full_name)
    assert board["project"]["role"] == "reader"
    assert board["memory"] == 1


async def test_ready_dependencies_close_reopen_and_blocked_union(client: BaseClient) -> None:
    project = await call(client, "project_save", root=".")
    work = await call(client, "task_add", title="Wait for both", priority="urgent")
    manual = await call(client, "task_add", title="Manual block", status="blocked")
    other = await call(client, "project_save", root="../other")
    deps = [
        await call(client, "task_add", project=other["full_name"], title=f"Dependency {i}")
        for i in range(2)
    ]
    for task in (work, manual):
        for dep in deps:
            await call(
                client, "task_link", task=task["id"], relation="depends_on", target=dep["id"]
            )
    board = await call(client, "project_board", project=project["full_name"])
    assert board["ready"] == {"count": 0, "items": []}
    assert board["tasks"]["blocked"] == 2  # Count tasks, not edges or reasons.
    await call(client, "task_update", id=deps[0]["id"], status="done")
    assert (await call(client, "project_board", project=project["full_name"]))["ready"][
        "count"
    ] == 0
    for status in ("done", "cancelled", "deleted"):
        await call(client, "task_update", id=deps[1]["id"], status=status)
        board = await call(client, "project_board", project=project["full_name"])
        assert board["ready"] == {"count": 1, "items": [{"id": work["id"], "title": work["title"]}]}
        assert board["tasks"]["blocked"] == 1
    await call(client, "task_update", id=deps[0]["id"], status="todo")
    assert (await call(client, "project_board", project=project["full_name"]))["ready"][
        "count"
    ] == 0
    for dep in deps:
        await call(client, "task_unlink", task=work["id"], relation="depends_on", target=dep["id"])
    # Other link types do not prevent readiness.
    await call(client, "task_link", task=work["id"], relation="related_to", target=deps[0]["id"])
    assert (await call(client, "project_board", project=project["full_name"]))["ready"][
        "count"
    ] == 1


async def test_claimed_scopes_and_top_ten(client: BaseClient, backlog: Backlog) -> None:
    project = await call(client, "project_save", root=".")
    mine = []
    for i in range(12):
        task = await call(client, "task_add", title=f"Agent {i}")
        await call(
            client, "task_claim", task=task["id"], expected=None, assigned=f"local@here:m{i}"
        )
        mine.append(task)
    away = backlog.view(computer="here-extra")
    remote = await call(client, "task_add", title="Another computer", priority="urgent")
    await mutate(
        operations.claim_task(
            await away.open(),
            away.actor,
            remote["id"],
            expected=None,
            assigned="local@here-extra:agent",
        ),
        away.transaction,
    )
    await mutate(
        scope.register(await backlog.open(), backlog.actor, user_identity("alice"), "Alice"),
        backlog.transaction,
    )
    alice = backlog.view(principal=user_identity("alice"))
    await mutate(
        scope.share(await backlog.open(), backlog.actor, project["id"], alice.principal, "editor"),
        backlog.transaction,
    )
    foreign = await call(client, "task_add", title="Another principal", priority="urgent")
    await mutate(
        operations.claim_task(
            await alice.open(),
            alice.actor,
            foreign["id"],
            expected=None,
            assigned="alice@here:agent",
        ),
        alice.transaction,
    )
    await call(client, "task_update", id=mine[-1]["id"], status="blocked", priority="urgent")
    other = await call(client, "project_save", root="../other")
    elsewhere = await call(client, "task_add", project=other["full_name"], title="Other project")
    await call(
        client, "task_claim", task=elsewhere["id"], expected=None, assigned="local@here:other"
    )
    board = await call(client, "project_board", project=project["full_name"])
    assert board["claimed"] == {
        "count": 12,
        "items": [{"id": t["id"], "title": t["title"]} for t in (mine[-1], *mine[:9])],
    }
    assert board["tasks"]["claimed"] == 14
    assert board["tasks"]["blocked"] == 1
    assert board["ready"]["count"] == 0
    await call(client, "task_update", id=mine[0]["id"], status="done")
    assert (await call(client, "project_board", project=project["full_name"]))["claimed"][
        "count"
    ] == 11


async def test_hidden_dependency_prevents_readiness(client: BaseClient, backlog: Backlog) -> None:
    from backlog_mcp.models import Relation, Status

    project = await call(client, "project_save", root=".")
    work = await call(client, "task_add", title="Visible work")
    await mutate(
        scope.register(await backlog.open(), backlog.actor, user_identity("alice"), "Alice"),
        backlog.transaction,
    )
    alice = backlog.view(principal=user_identity("alice"))
    await mutate(
        scope.share(await backlog.open(), backlog.actor, project["id"], alice.principal, "editor"),
        backlog.transaction,
    )
    hidden = await mutate(
        scope.add_project(
            await alice.open(), alice.actor, slug="hidden", name="Hidden", root="", computer="here"
        ),
        alice.transaction,
    )
    dependency = await mutate(
        operations.add_task(
            await alice.open(),
            alice.actor,
            project_id=hidden.id,
            title="Hidden dependency",
            status=Status.DONE,
        ),
        alice.transaction,
    )
    await mutate(
        operations.link_task(
            await alice.open(), alice.actor, work["id"], Relation.DEPENDS_ON, dependency.id
        ),
        alice.transaction,
    )
    board = await call(client, "project_board", project=project["full_name"])
    assert board["ready"] == {"count": 0, "items": []}
    assert board["tasks"]["blocked"] == 1
    await mutate(
        scope.share(await alice.open(), alice.actor, hidden.id, backlog.principal, "reader"),
        alice.transaction,
    )
    board = await call(client, "project_board", project=project["full_name"])
    assert board["ready"]["count"] == 1
    assert board["tasks"]["blocked"] == 0
