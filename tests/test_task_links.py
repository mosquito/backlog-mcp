import asyncio
import sqlite3
from typing import Any

import pytest
from aiohttp_tiny_mcp.client.base import BaseClient
from helpers import mutate, rows
from test_server import call, resource

from backlog_mcp import backlog as scope
from backlog_mcp import server
from backlog_mcp import server as operations
from backlog_mcp.backlog import Backlog
from backlog_mcp.models import RELATIONS, Relation, Status, relation


@pytest.mark.parametrize("kind", list(Relation))
def test_relation_pair(kind: Relation) -> None:
    assert relation(kind) == relation(kind.value.upper()) == (kind, RELATIONS[kind])
    assert relation(RELATIONS[kind])[1] == kind
    with pytest.raises(KeyError):
        relation("unknown")


async def tasks(backlog: Backlog, count: int = 3) -> list[int]:
    project, _ = await backlog.current()
    return [
        (
            await mutate(
                operations.add_task(
                    await backlog.open(),
                    backlog.actor,
                    project_id=project.id,
                    title=f"Task {number}",
                ),
                backlog.transaction,
            )
        ).id
        for number in range(count)
    ]


@pytest.mark.parametrize("kind", list(Relation))
async def test_pairs_are_idempotent_from_either_side(backlog: Backlog, kind: Relation) -> None:
    a, b = await tasks(backlog, 2)
    store = backlog
    inverse = RELATIONS[kind]
    assert await mutate(
        operations.link_task(await store.open(), store.actor, a, kind, b, reason="Explanation"),
        store.transaction,
    )
    saved = await operations.read_task(await store.open(), store.actor, a)
    assert not await mutate(
        operations.link_task(await store.open(), store.actor, b, inverse, a), store.transaction
    )
    assert (
        await operations.read_task(await store.open(), store.actor, a)
    ).updated_at == saved.updated_at
    assert len(await rows(await store.open(), "SELECT * FROM task_links")) == 2
    assert [
        (x.task, x.relation, x.reason)
        for x in await operations.task_links(await store.open(), store.actor, a)
    ] == [(b, kind, "Explanation")]
    assert [
        (x.task, x.relation)
        for x in await operations.task_links(await store.open(), store.actor, b)
    ] == [(a, inverse)]
    assert await mutate(
        operations.link_task(await store.open(), store.actor, b, inverse, a, reason="Revised"),
        store.transaction,
    )
    assert (await operations.task_links(await store.open(), store.actor, a))[0].reason == "Revised"
    assert await mutate(
        operations.link_task(await store.open(), store.actor, a, kind, b, reason=""),
        store.transaction,
    )
    assert (await operations.task_links(await store.open(), store.actor, b))[0].reason == ""
    assert await mutate(
        operations.unlink_task(await store.open(), store.actor, b, inverse, a), store.transaction
    )
    assert not await mutate(
        operations.unlink_task(await store.open(), store.actor, a, kind, b), store.transaction
    )
    assert (
        await operations.task_links(await store.open(), store.actor, a)
        == await operations.task_links(await store.open(), store.actor, b)
        == []
    )


@pytest.mark.parametrize("kind", [Relation.DEPENDS_ON, Relation.PART_OF])
async def test_cycles_and_self_links(backlog: Backlog, kind: Relation) -> None:
    a, b, c = await tasks(backlog)
    store = backlog
    await mutate(
        operations.link_task(await store.open(), store.actor, a, kind, b), store.transaction
    )
    await mutate(
        operations.link_task(await store.open(), store.actor, b, kind, c), store.transaction
    )
    with pytest.raises(ValueError, match="cycle"):
        await mutate(
            operations.link_task(await store.open(), store.actor, c, kind, a), store.transaction
        )
    with pytest.raises(ValueError, match="cycle"):
        await mutate(
            operations.link_task(await store.open(), store.actor, a, RELATIONS[kind], c),
            store.transaction,
        )
    with pytest.raises(ValueError, match="itself"):
        await mutate(
            operations.link_task(await store.open(), store.actor, a, Relation.RELATED_TO, a),
            store.transaction,
        )
    # Different relation families do not form a dependency cycle together.
    assert await mutate(
        operations.link_task(await store.open(), store.actor, c, Relation.CAUSED_BY, a),
        store.transaction,
    )
    assert await mutate(
        operations.link_task(await store.open(), store.actor, a, Relation.CAUSED_BY, c),
        store.transaction,
    )
    assert await mutate(
        operations.link_task(await store.open(), store.actor, a, Relation.RELATED_TO, b),
        store.transaction,
    )
    await mutate(
        operations.unlink_task(await store.open(), store.actor, a, Relation.RELATED_TO, b),
        store.transaction,
    )
    assert (await operations.task_links(await store.open(), store.actor, a))[0].task in (b, c)


async def test_failed_second_insert_rolls_back_pair_and_timestamps(backlog: Backlog) -> None:
    a, b = await tasks(backlog, 2)
    store = backlog
    before = await operations.read_task(await store.open(), store.actor, a)
    connection = await store.open()
    await connection.execute(
        "CREATE TRIGGER fail_reverse BEFORE INSERT ON task_links "
        "WHEN new.relation = 'blocks' BEGIN SELECT RAISE(ABORT, 'reverse failed'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="reverse failed"):
        await mutate(
            operations.link_task(await store.open(), store.actor, a, Relation.DEPENDS_ON, b),
            store.transaction,
        )
    assert await rows(await store.open(), "SELECT * FROM task_links") == []
    assert (
        await operations.read_task(await store.open(), store.actor, a)
    ).updated_at == before.updated_at


async def test_concurrent_connections_cannot_create_cycle(backlog: Backlog) -> None:
    a, b = await tasks(backlog, 2)
    other = Backlog(backlog.database.path, principal=backlog.principal, computer="second")
    await other.open()
    try:
        results = await asyncio.gather(
            mutate(
                operations.link_task(
                    await backlog.open(), backlog.actor, a, Relation.DEPENDS_ON, b
                ),
                backlog.transaction,
            ),
            mutate(
                operations.link_task(await other.open(), other.actor, b, Relation.DEPENDS_ON, a),
                other.transaction,
            ),
            return_exceptions=True,
        )
        assert results.count(True) == 1
        assert sum(isinstance(result, ValueError) for result in results) == 1
        assert len(await rows(await backlog.open(), "SELECT * FROM task_links")) == 2
    finally:
        await other.close()


async def test_access_and_task_lifecycle(backlog: Backlog) -> None:
    a, b = await tasks(backlog, 2)
    store = backlog
    project, _ = await backlog.current()
    other = await backlog.create(backlog.cwd.parent / "other")
    await mutate(
        operations.update_task(await store.open(), store.actor, b, project_id=other.id),
        store.transaction,
    )
    await mutate(
        scope.register(await store.open(), store.actor, "reader", "reader"), store.transaction
    )
    reader = store.view(principal="reader")
    await mutate(
        scope.share(await store.open(), store.actor, project.id, "reader", "editor"),
        store.transaction,
    )
    await mutate(
        operations.link_task(await store.open(), store.actor, a, Relation.DEPENDS_ON, b),
        store.transaction,
    )
    assert await operations.task_links(await reader.open(), reader.actor, a) == []
    with pytest.raises(LookupError):
        await mutate(
            operations.link_task(await reader.open(), reader.actor, a, Relation.CAUSED_BY, b),
            reader.transaction,
        )
    await mutate(
        scope.share(await store.open(), store.actor, other.id, "reader", "reader"),
        store.transaction,
    )
    assert (await operations.task_links(await reader.open(), reader.actor, a))[0].task == b
    with pytest.raises(PermissionError):
        await mutate(
            operations.unlink_task(await reader.open(), reader.actor, a, Relation.DEPENDS_ON, b),
            reader.transaction,
        )
    await mutate(
        scope.share(await store.open(), store.actor, other.id, "reader", "editor"),
        store.transaction,
    )
    assert await mutate(
        operations.link_task(await reader.open(), reader.actor, a, Relation.CAUSED_BY, b),
        reader.transaction,
    )
    await mutate(
        operations.update_task(
            await store.open(), store.actor, b, title="Renamed", status=Status.DONE
        ),
        store.transaction,
    )
    assert (await operations.task_links(await store.open(), store.actor, a))[0].title == "Renamed"
    await mutate(operations.delete_task(await store.open(), store.actor, b), store.transaction)
    assert all(
        link.status == Status.DELETED
        for link in await operations.task_links(await store.open(), store.actor, a)
    )
    await mutate(scope.delete_project(await store.open(), store.actor, other.id), store.transaction)
    assert await operations.task_links(await store.open(), store.actor, a) == []
    with pytest.raises(LookupError):
        await mutate(
            operations.link_task(await store.open(), store.actor, a, Relation.RELATED_TO, 99999),
            store.transaction,
        )


async def test_missing_inverse_and_conflicting_reasons(backlog: Backlog) -> None:
    a, b = await tasks(backlog, 2)
    store = backlog
    await mutate(
        operations.link_task(
            await store.open(), store.actor, a, Relation.DEPENDS_ON, b, reason="Keep this"
        ),
        store.transaction,
    )
    connection = await store.open()
    await connection.execute("DELETE FROM task_links WHERE task_id = ?", (b,))
    await connection.commit()
    assert await mutate(
        operations.link_task(await store.open(), store.actor, b, Relation.BLOCKS, a),
        store.transaction,
    )
    assert (await operations.task_links(await store.open(), store.actor, b))[
        0
    ].reason == "Keep this"
    await connection.execute("UPDATE task_links SET reason = 'Conflict' WHERE task_id = ?", (b,))
    await connection.commit()
    with pytest.raises(ValueError, match="conflicting"):
        await mutate(
            operations.link_task(await store.open(), store.actor, a, Relation.DEPENDS_ON, b),
            store.transaction,
        )
    assert await mutate(
        operations.unlink_task(await store.open(), store.actor, a, Relation.DEPENDS_ON, b),
        store.transaction,
    )


async def test_mcp_tools_and_resource(client: BaseClient) -> None:
    a = (await call(client, "task_add", title="Consumer"))["id"]
    b = (await call(client, "task_add", title="Provider"))["id"]
    assert (await call(client, "task_get", id=a))["links"] == []
    assert (await call(client, "task_link", task=a, relation="depends_on", target=b))["changed"]
    details = await call(client, "task_get", id=a)
    assert details["links"][0]["relation"] == "depends_on"
    assert (await resource(client, f"backlog://tasks/{b}"))["links"][0]["relation"] == "blocks"
    assert "links" not in (await call(client, "task_list", full=True))["items"][0]
    assert (
        await client.call_tool("task_link", {"task": a, "relation": "unknown", "target": b})
    ).is_error
    tools = {tool.name: tool for tool in await client.list_tools()}
    assert "task_links" not in tools
    assert "depends_on" in str(tools["task_link"].input_schema)
    assert (await call(client, "task_unlink", task=b, relation="blocks", target=a))["changed"]
    assert (await call(client, "task_get", id=a))["links"] == []


async def test_notifications_and_neighbor_invalidation(
    backlog: Backlog, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = await tasks(backlog, 2)
    events: list[int] = []
    invalidated: list[str] = []

    async def changed(event: Any, **kwargs: Any) -> None:
        events.append(event.id)

    async def stale(uris: list[str], event: Any) -> None:
        invalidated.extend(uris)

    monkeypatch.setattr(server, "changed", changed)
    monkeypatch.setattr(server, "stale", stale)
    args = server.LinkTask(task=a, relation=Relation.DEPENDS_ON, target=b)
    await server.task_link(
        args, backlog, connection=await backlog.open(), transaction=backlog.transaction
    )
    assert events == [a, b]
    events.clear()
    await server.task_link(
        args, backlog, connection=await backlog.open(), transaction=backlog.transaction
    )
    assert events == []
    before = await operations.read_task(await backlog.open(), backlog.actor, a)
    await server.task_update(
        server.UpdateTask(id=b, title="New provider"),
        backlog,
        connection=await backlog.open(),
        transaction=backlog.transaction,
    )
    assert events == [b] and invalidated == [f"backlog://tasks/{a}"]
    assert (
        await operations.read_task(await backlog.open(), backlog.actor, a)
    ).updated_at == before.updated_at


async def test_existing_schema_is_registered_without_changing_links(backlog: Backlog) -> None:
    a, b = await tasks(backlog, 2)
    await mutate(
        operations.link_task(
            await backlog.open(), backlog.actor, a, Relation.PART_OF, b, reason="Keep this"
        ),
        backlog.transaction,
    )
    path = backlog.database.path
    await backlog.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE migrations")
        connection.execute("DROP TABLE task_edit_claims")
        connection.execute("DROP INDEX tasks_by_assigned")
        connection.execute("ALTER TABLE tasks DROP COLUMN assigned")
    assert (await operations.read_task(await backlog.open(), backlog.actor, a)).title == "Task 0"
    assert (await operations.task_links(await backlog.open(), backlog.actor, b))[
        0
    ].reason == "Keep this"
    assert len(await rows(await backlog.open(), "SELECT * FROM migrations")) == 4
    await backlog.close()
    assert (await operations.task_links(await backlog.open(), backlog.actor, b))[
        0
    ].relation == Relation.CONTAINS


async def test_resource_subscribers_receive_both_directions(backlog: Backlog) -> None:
    from aiohttp_tiny_mcp.testing import connect
    from test_events import collect, summary

    async with (
        connect(server.registry, adapter="2026-07-28") as writer,
        connect(server.registry, adapter="2026-07-28") as listener,
    ):
        a = (await call(writer, "task_add", title="Consumer"))["id"]
        b = (await call(writer, "task_add", title="Provider"))["id"]
        frames: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        collecting = collect(
            listener.listen(resources=[f"backlog://tasks/{a}", f"backlog://tasks/{b}"]), frames
        )
        try:
            async with asyncio.timeout(5):
                while not listener.accepted:
                    await asyncio.sleep(0.01)
            await call(writer, "task_link", task=a, relation="depends_on", target=b)
            assert {summary(await asyncio.wait_for(frames.get(), 5))[3] for _ in range(2)} == {a, b}
            await call(writer, "task_update", id=b, status="done")
            assert {summary(await asyncio.wait_for(frames.get(), 5))[3] for _ in range(2)} == {a, b}
            await call(writer, "task_unlink", task=b, relation="blocks", target=a)
            assert {summary(await asyncio.wait_for(frames.get(), 5))[3] for _ in range(2)} == {a, b}
        finally:
            collecting.cancel()
            await asyncio.gather(collecting, return_exceptions=True)


async def test_neighbor_notifications_do_not_reveal_private_targets(
    backlog: Backlog, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiohttp_tiny_mcp.storage.namespaces import namespace

    a, b = await tasks(backlog, 2)
    project, _ = await backlog.current()
    other = await backlog.create(backlog.cwd.parent / "other")
    store = backlog
    await mutate(
        scope.register(await store.open(), store.actor, "reader", "Reader"), store.transaction
    )
    await mutate(
        scope.share(await store.open(), store.actor, project.id, "reader", "reader"),
        store.transaction,
    )
    reader = store.view(principal="reader")
    await mutate(
        operations.link_task(await store.open(), store.actor, a, Relation.DEPENDS_ON, b),
        store.transaction,
    )
    assert len(await operations.task_links(await reader.open(), reader.actor, a)) == 1
    seen: list[tuple[Any, list[str], Any]] = []

    async def stale(uris: list[str], event: Any) -> None:
        seen.append((namespace.get(), uris, event.model_dump()))

    monkeypatch.setattr(server, "stale", stale)
    await server.task_move(
        server.MoveTask(id=b, project=other.full_name),
        backlog,
        connection=await backlog.open(),
        transaction=backlog.transaction,
    )
    assert await operations.task_links(await reader.open(), reader.actor, a) == []
    await server.task_update(
        server.UpdateTask(id=b, title="Private target"),
        backlog,
        connection=await backlog.open(),
        transaction=backlog.transaction,
    )
    reader_events = [(uris, event) for identity, uris, event in seen if identity == "reader"]
    assert reader_events
    assert all(uris == [f"backlog://tasks/{a}"] for uris, _ in reader_events)
    assert all(event["id"] == a and event["title"] == "Task 0" for _, event in reader_events)
