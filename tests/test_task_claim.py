import asyncio
import sqlite3
from typing import Any

import pytest
from aiohttp_tiny_mcp.client.base import BaseClient
from helpers import mutate
from pydantic import ValidationError
from test_server import call, resource

from backlog_mcp import backlog as scope
from backlog_mcp import server
from backlog_mcp import server as operations
from backlog_mcp.backlog import Backlog
from backlog_mcp.models import Status
from backlog_mcp.names import principal_label
from backlog_mcp.records import AssignmentConflict


def assignment(store: Backlog, marker: str = "agent-k7Q2m") -> str:
    return f"{principal_label(store.principal, store.computer)}:{marker}"


async def new_task(backlog: Backlog) -> int:
    project, _ = await backlog.current()
    return (
        await mutate(
            operations.add_task(
                await backlog.open(), backlog.actor, project_id=project.id, title="Claim me"
            ),
            backlog.transaction,
        )
    ).id


async def test_claim_transfer_release_and_strict_conflict(backlog: Backlog) -> None:
    task = await new_task(backlog)
    store = backlog
    first, second = assignment(store), assignment(store, "lucky")
    original = await operations.read_task(await store.open(), store.actor, task)
    saved, changed = await mutate(
        operations.claim_task(await store.open(), store.actor, task, expected=None, assigned=first),
        store.transaction,
    )
    assert changed and saved.assigned == first and saved.status == Status.TODO
    assert saved.updated_at >= original.updated_at
    comments = await operations.read_comments(await store.open(), store.actor, task_id=task)
    assert len(comments) == 1
    assert comments[0].body == f"Assignment changed: null → {first}"
    assert comments[0].principal == store.principal
    assert comments[0].computer == store.computer
    for target in (first, second, None):
        with pytest.raises(AssignmentConflict, match="current") as error:
            await mutate(
                operations.claim_task(
                    await store.open(), store.actor, task, expected=None, assigned=target
                ),
                store.transaction,
            )
        assert error.value.assigned == first
    same, changed = await mutate(
        operations.claim_task(
            await store.open(), store.actor, task, expected=first, assigned=first
        ),
        store.transaction,
    )
    assert not changed and same.updated_at == saved.updated_at
    assert len(await operations.read_comments(await store.open(), store.actor, task_id=task)) == 1
    assert (
        await mutate(
            operations.claim_task(
                await store.open(), store.actor, task, expected=first, assigned=second
            ),
            store.transaction,
        )
    )[0].assigned == second
    assert (
        await mutate(
            operations.claim_task(
                await store.open(), store.actor, task, expected=second, assigned=None
            ),
            store.transaction,
        )
    )[0].assigned is None
    empty = await operations.read_task(await store.open(), store.actor, task)
    assert not (
        await mutate(
            operations.claim_task(
                await store.open(), store.actor, task, expected=None, assigned=None
            ),
            store.transaction,
        )
    )[1]
    assert (
        await operations.read_task(await store.open(), store.actor, task)
    ).updated_at == empty.updated_at
    assert len(await operations.read_comments(await store.open(), store.actor, task_id=task)) == 3
    with pytest.raises(AssignmentConflict) as error:
        await mutate(
            operations.claim_task(
                await store.open(), store.actor, task, expected=second, assigned=None
            ),
            store.transaction,
        )
    assert error.value.assigned is None


@pytest.mark.parametrize("field", ["expected", "assigned"])
@pytest.mark.parametrize(
    "value",
    [
        "",
        "alice",
        "alice@pc",
        "alice@pc:",
        "@pc:a",
        "alice@:a",
        "alice@pc:a:b",
        "alice@pc:has space",
        "alice@pc:slash/",
        "alice@pc:a\n",
        "alice@pc:é",
        "alice@pc:" + "x" * 65,
        "alice@@pc:a",
        "alice@pc%ZZ:a",
    ],
)
async def test_invalid_format_at_model_and_storage(
    backlog: Backlog, field: str, value: str
) -> None:
    values = {"expected": None, "assigned": None, field: value}
    with pytest.raises(ValidationError):
        server.ClaimTask(task=1, **values)
    task = await new_task(backlog)
    with pytest.raises(ValueError):
        await mutate(
            operations.claim_task(await backlog.open(), backlog.actor, task, **values),
            backlog.transaction,
        )
    assert (await operations.read_task(await backlog.open(), backlog.actor, task)).assigned is None
    assert await operations.read_comments(await backlog.open(), backlog.actor, task_id=task) == []


@pytest.mark.parametrize("missing", ["expected", "assigned"])
def test_nullable_arguments_are_required(missing: str) -> None:
    values = {"task": 1, "expected": None, "assigned": None}
    del values[missing]
    with pytest.raises(ValidationError):
        server.ClaimTask.model_validate(values)


async def test_principal_validation_permissions_and_takeover(backlog: Backlog) -> None:
    task = await new_task(backlog)
    store = backlog
    project, _ = await backlog.current()
    first = assignment(store)
    await mutate(
        operations.claim_task(await store.open(), store.actor, task, expected=None, assigned=first),
        store.transaction,
    )
    for target in ("other@here:agent", f"{store.principal}@elsewhere:agent"):
        with pytest.raises(PermissionError, match="current principal"):
            await mutate(
                operations.claim_task(
                    await store.open(), store.actor, task, expected=first, assigned=target
                ),
                store.transaction,
            )
    await mutate(
        scope.register(await store.open(), store.actor, "other", "Other"), store.transaction
    )
    other = store.view(principal="other", computer="another")
    with pytest.raises(LookupError):
        await mutate(
            operations.claim_task(
                await other.open(), other.actor, task, expected=first, assigned=assignment(other)
            ),
            other.transaction,
        )
    await mutate(
        scope.share(await store.open(), store.actor, project.id, "other", "reader"),
        store.transaction,
    )
    with pytest.raises(PermissionError, match="editor"):
        await mutate(
            operations.claim_task(
                await other.open(), other.actor, task, expected=first, assigned=None
            ),
            other.transaction,
        )
    await mutate(
        scope.share(await store.open(), store.actor, project.id, "other", "editor"),
        store.transaction,
    )
    second = assignment(other)
    assert (
        await mutate(
            operations.claim_task(
                await other.open(), other.actor, task, expected=first, assigned=second
            ),
            other.transaction,
        )
    )[0].assigned == second
    # Any editor may release a known assignment; knowing it alone is not authorization.
    assert (
        await mutate(
            operations.claim_task(
                await store.open(), store.actor, task, expected=second, assigned=None
            ),
            store.transaction,
        )
    )[0].assigned is None
    with pytest.raises(LookupError):
        await mutate(
            operations.claim_task(
                await store.open(), store.actor, 999999, expected=None, assigned=first
            ),
            store.transaction,
        )


async def test_escaped_principal_label(backlog: Backlog) -> None:
    task = await new_task(backlog)
    store = backlog
    project, _ = await backlog.current()
    await mutate(
        scope.register(await store.open(), store.actor, "name@example.org", "Name"),
        store.transaction,
    )
    await mutate(
        scope.share(await store.open(), store.actor, project.id, "name@example.org", "editor"),
        store.transaction,
    )
    from backlog_mcp.models import Actor

    actor = Actor(principal="name@example.org", computer="host:dev")
    target = principal_label(actor.principal, actor.computer) + ":A_9-"
    assert target == "name%40example.org@host%3Adev:A_9-"
    assert server.ClaimTask(task=task, expected=None, assigned=target).assigned == target
    assert (
        await mutate(
            operations.claim_task(await store.open(), actor, task, expected=None, assigned=target),
            store.transaction,
        )
    )[0].assigned == target


@pytest.mark.parametrize("different_computers", [False, True])
async def test_five_connections_have_one_winner(
    backlog: Backlog, different_computers: bool
) -> None:
    task = await new_task(backlog)
    stores = [
        Backlog(
            backlog.database.path,
            principal=backlog.principal,
            computer=f"pc-{i}" if different_computers else "here",
        )
        for i in range(5)
    ]
    try:
        await asyncio.gather(*(store.open() for store in stores))
        results = await asyncio.gather(
            *[
                mutate(
                    operations.claim_task(
                        await store.open(),
                        store.actor,
                        task,
                        expected=None,
                        assigned=assignment(store, f"agent-{i}"),
                    ),
                    store.transaction,
                )
                for i, store in enumerate(stores)
            ],
            return_exceptions=True,
        )
        winners = [result for result in results if isinstance(result, tuple)]
        assert len(winners) == 1 and winners[0][1]
        conflicts = [result for result in results if isinstance(result, AssignmentConflict)]
        assert len(conflicts) == 4
        saved = await operations.read_task(await backlog.open(), backlog.actor, task)
        assert saved.assigned == winners[0][0].assigned
        assert all(error.assigned == saved.assigned for error in conflicts)
        assert (
            len(await operations.read_comments(await backlog.open(), backlog.actor, task_id=task))
            == 1
        )
    finally:
        await asyncio.gather(*(store.close() for store in stores))


async def test_history_failure_rolls_back_assignment(backlog: Backlog) -> None:
    task = await new_task(backlog)
    store = backlog
    before = await operations.read_task(await store.open(), store.actor, task)
    connection = await store.open()
    await connection.execute(
        "CREATE TRIGGER fail_claim BEFORE INSERT ON task_comments "
        "BEGIN SELECT RAISE(ABORT, 'history failed'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="history failed"):
        await mutate(
            operations.claim_task(
                await store.open(), store.actor, task, expected=None, assigned=assignment(store)
            ),
            store.transaction,
        )
    after = await operations.read_task(await store.open(), store.actor, task)
    assert after == before
    assert await operations.read_comments(await store.open(), store.actor, task_id=task) == []


async def test_status_move_and_reopen_preserve_assignment(backlog: Backlog) -> None:
    task = await new_task(backlog)
    store = backlog
    target = assignment(store)
    await mutate(
        operations.claim_task(
            await store.open(), store.actor, task, expected=None, assigned=target
        ),
        store.transaction,
    )
    other = await backlog.create(backlog.cwd.parent / "other")
    await mutate(
        operations.update_task(
            await store.open(), store.actor, task, status=Status.DONE, project_id=other.id
        ),
        store.transaction,
    )
    await store.close()
    assert (await operations.read_task(await store.open(), store.actor, task)).assigned == target
    await mutate(operations.delete_task(await store.open(), store.actor, task), store.transaction)
    assert (await operations.read_task(await store.open(), store.actor, task)).assigned == target
    await mutate(
        operations.update_task(await store.open(), store.actor, task, status=Status.TODO),
        store.transaction,
    )
    assert (await operations.read_task(await store.open(), store.actor, task)).assigned == target


async def test_mcp_schema_reads_and_errors(client: BaseClient) -> None:
    task = (await call(client, "task_add", title="Claimable"))["id"]
    principal = (await call(client, "whoami"))["principal"]
    target = f"{principal}:agent-test"
    tools = {tool.name: tool for tool in await client.list_tools()}
    schema = tools["task_claim"].input_schema
    assert set(schema["required"]) == {"task", "expected", "assigned"}
    for name in ("expected", "assigned"):
        field = schema["properties"][name]
        types = (
            {variant["type"] for variant in field["anyOf"]}
            if "anyOf" in field
            else set(field["type"])
        )
        assert types == {"string", "null"}
        assert (await client.call_tool("task_claim", {"task": task, name: None})).is_error
    assert (await call(client, "task_claim", task=task, expected=None, assigned=target))[
        "assigned"
    ] == target
    assert (await call(client, "task_get", id=task))["assigned"] == target
    for full in (False, True):
        assert (await call(client, "task_list", full=full))["items"][0]["assigned"] == target
    assert (await call(client, "search", query="Claimable", kinds=["tasks"]))["tasks"][0][
        "assigned"
    ] == target
    assert (await resource(client, f"backlog://tasks/{task}"))["assigned"] == target
    assert (await resource(client, "backlog://tasks"))[0]["assigned"] == target
    for values in (
        {"expected": None, "assigned": target},
        {"expected": target, "assigned": "other@pc:agent"},
        {"expected": target, "assigned": "bad"},
    ):
        assert (await client.call_tool("task_claim", {"task": task, **values})).is_error
    # task_update cannot bypass compare-and-swap, even if unknown fields are ignored.
    await call(client, "task_update", id=task, assigned=None)
    assert (await call(client, "task_get", id=task))["assigned"] == target
    assert (await call(client, "task_claim", task=task, expected=target, assigned=None))[
        "assigned"
    ] is None


async def test_notifications_only_follow_committed_changes(
    backlog: Backlog, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = await new_task(backlog)
    target = assignment(backlog)
    events = []

    async def changed(event: Any) -> None:
        assert (
            await operations.read_task(await backlog.open(), backlog.actor, task)
        ).assigned == target
        events.append(event)

    monkeypatch.setattr(server, "changed", changed)
    args = server.ClaimTask(task=task, expected=None, assigned=target)
    await server.task_claim(
        args, backlog, connection=await backlog.open(), transaction=backlog.transaction
    )
    assert len(events) == 1 and events[0].id == task
    with pytest.raises(AssignmentConflict):
        await server.task_claim(
            args, backlog, connection=await backlog.open(), transaction=backlog.transaction
        )
    await server.task_claim(
        server.ClaimTask(task=task, expected=target, assigned=target),
        backlog,
        connection=await backlog.open(),
        transaction=backlog.transaction,
    )
    assert len(events) == 1


async def test_assignment_prefixes_access_and_index(backlog: Backlog) -> None:
    from backlog_mcp.queries import QUERIES
    from backlog_mcp.records import assignment_pattern

    store = backlog
    project, _ = await backlog.current()
    ids = []
    for marker in ("agent_A", "agentXA", "Agent_A"):
        task = await new_task(backlog)
        ids.append(task)
        await mutate(
            operations.claim_task(
                await store.open(),
                store.actor,
                task,
                expected=None,
                assigned=assignment(store, marker),
            ),
            store.transaction,
        )
    assert [
        t.id
        for t in await operations.assigned_tasks(
            await store.open(), store.actor, assignment(store, "agent_")
        )
    ] == [ids[0]]
    assert [
        t.id
        for t in await operations.assigned_tasks(
            await store.open(), store.actor, assignment(store, "Agent_")
        )
    ] == [ids[2]]
    for prefix in ("local%", "local\\", "local@here:%"):
        assert await operations.assigned_tasks(await store.open(), store.actor, prefix) == []
    assert (
        await operations.count_assigned(await store.open(), store.actor, assignment(store, "agent"))
        == 2
    )
    assert (
        len(await operations.assigned_tasks(await store.open(), store.actor, "", limit=1, offset=1))
        == 1
    )
    await mutate(
        operations.update_task(await store.open(), store.actor, ids[0], status=Status.DELETED),
        store.transaction,
    )
    assert (
        await operations.count_assigned(
            await store.open(), store.actor, assignment(store, "agent_")
        )
        == 1
    )
    await mutate(
        scope.register(await store.open(), store.actor, "reader", "Reader"), store.transaction
    )
    reader = store.view(principal="reader")
    assert await operations.assigned_tasks(await reader.open(), reader.actor, "") == []
    assert await operations.count_assigned(await reader.open(), reader.actor, "") == 0
    await mutate(
        scope.share(await store.open(), store.actor, project.id, "reader", "reader"),
        store.transaction,
    )
    assert len(await operations.assigned_tasks(await reader.open(), reader.actor, "")) == 3
    assert await operations.count_assigned(await reader.open(), reader.actor, "") == 3
    # Check the actual aiosql queries, including literal wildcard escaping.
    connection = await store.open()
    for query, extra in (
        (QUERIES.assigned_tasks, {"limit": 50, "offset": 0}),
        (QUERIES.count_assigned, {}),
    ):
        prefix = assignment(store, "agent_")
        plan = await connection.execute_fetchall(
            "EXPLAIN QUERY PLAN " + query.sql,
            {
                "prefix": prefix,
                "pattern": assignment_pattern(prefix),
                "principal": store.principal,
                **extra,
            },
        )
        assert any("SEARCH t USING" in row[3] and "tasks_by_assigned" in row[3] for row in plan)


async def test_assigned_default_and_whoami_count(client: BaseClient, backlog: Backlog) -> None:
    store = backlog
    task = await new_task(backlog)
    principal = (await call(client, "whoami"))["principal"]
    target = f"{principal}:one"
    assert (await call(client, "whoami"))["assigned_tasks"] == 0
    await call(client, "task_claim", task=task, expected=None, assigned=target)
    # A similar computer name must not inflate the principal's count.
    other = store.view(computer=store.computer + "-other")
    second = await new_task(backlog)
    await mutate(
        operations.claim_task(
            await other.open(), other.actor, second, expected=None, assigned=assignment(other)
        ),
        other.transaction,
    )
    assert (await call(client, "whoami"))["assigned_tasks"] == 1
    for args in ({}, {"prefix": None}, {"prefix": principal + ":"}, {"prefix": target}):
        result = await call(client, "task_assigned", **args)
        assert result["count"] == 1 and result["items"][0]["id"] == task
    result = await call(client, "task_assigned", prefix="", limit=1)
    assert result["count"] == 2 and len(result["items"]) == 1
    assert (await call(client, "task_assigned", prefix=principal, limit=1, offset=1))["count"] == 2
    await call(client, "task_claim", task=task, expected=target, assigned=None)
    assert (await call(client, "whoami"))["assigned_tasks"] == 0


async def test_comments_report_current_assignment(client: BaseClient) -> None:
    task = (await call(client, "task_add", title="Comment ownership"))["id"]
    principal = (await call(client, "whoami"))["principal"]
    first, second = f"{principal}:first", f"{principal}:second"
    initial = await call(client, "task_comment_add", task_id=task, body="Ownership progress")
    assert initial["task_assigned"] is None
    await call(client, "task_claim", task=task, expected=None, assigned=first)
    progress = await call(client, "task_comment_add", task_id=task, body="Ownership progress two")
    assert progress["task_assigned"] == first
    await call(client, "task_claim", task=task, expected=first, assigned=second)
    for comments in (
        (await call(client, "task_get", id=task))["comments"],
        (await call(client, "task_comment_list", task_id=task))["items"],
        (await call(client, "search", query="Ownership progress", kinds=["comments"]))["comments"],
    ):
        assert comments and all(comment["task_assigned"] == second for comment in comments)
    await call(client, "task_claim", task=task, expected=second, assigned=None)
    assert all(
        comment["task_assigned"] is None
        for comment in (await call(client, "task_comment_list", task_id=task))["items"]
    )
