import asyncio
import sqlite3
from typing import Any

import pytest
from aiohttp_tiny_mcp.client.base import BaseClient
from test_server import call, resource

from backlog_mcp import server
from backlog_mcp.backlog import Backlog
from backlog_mcp.models import RELATIONS, Relation, Status, relation
from backlog_mcp.store import Store


@pytest.mark.parametrize("kind", list(Relation))
def test_relation_pair(kind: Relation) -> None:
    assert relation(kind) == relation(kind.value.upper()) == (kind, RELATIONS[kind])
    assert relation(RELATIONS[kind])[1] == kind
    with pytest.raises(KeyError):
        relation("unknown")


async def tasks(backlog: Backlog, count: int = 3) -> list[int]:
    project, _ = await backlog.current()
    return [
        (await backlog.store.add_task(project_id=project.id, title=f"Task {number}")).id
        for number in range(count)
    ]


@pytest.mark.parametrize("kind", list(Relation))
async def test_pairs_are_idempotent_from_either_side(backlog: Backlog, kind: Relation) -> None:
    a, b = await tasks(backlog, 2)
    store = backlog.store
    inverse = RELATIONS[kind]
    assert await store.link_task(a, kind, b, reason="Explanation")
    saved = await store.task(a)
    assert not await store.link_task(b, inverse, a)
    assert (await store.task(a)).updated_at == saved.updated_at
    assert len(await store.rows("SELECT * FROM task_links")) == 2
    assert [(x.task, x.relation, x.reason) for x in await store.task_links(a)] == [
        (b, kind, "Explanation")
    ]
    assert [(x.task, x.relation) for x in await store.task_links(b)] == [(a, inverse)]
    assert await store.link_task(b, inverse, a, reason="Revised")
    assert (await store.task_links(a))[0].reason == "Revised"
    assert await store.link_task(a, kind, b, reason="")
    assert (await store.task_links(b))[0].reason == ""
    assert await store.unlink_task(b, inverse, a)
    assert not await store.unlink_task(a, kind, b)
    assert await store.task_links(a) == await store.task_links(b) == []


@pytest.mark.parametrize("kind", [Relation.DEPENDS_ON, Relation.PART_OF])
async def test_cycles_and_self_links(backlog: Backlog, kind: Relation) -> None:
    a, b, c = await tasks(backlog)
    store = backlog.store
    await store.link_task(a, kind, b)
    await store.link_task(b, kind, c)
    with pytest.raises(ValueError, match="cycle"):
        await store.link_task(c, kind, a)
    with pytest.raises(ValueError, match="cycle"):
        await store.link_task(a, RELATIONS[kind], c)
    with pytest.raises(ValueError, match="itself"):
        await store.link_task(a, Relation.RELATED_TO, a)
    # Different relation families do not form a dependency cycle together.
    assert await store.link_task(c, Relation.CAUSED_BY, a)
    assert await store.link_task(a, Relation.CAUSED_BY, c)
    assert await store.link_task(a, Relation.RELATED_TO, b)
    await store.unlink_task(a, Relation.RELATED_TO, b)
    assert (await store.task_links(a))[0].task in (b, c)


async def test_failed_second_insert_rolls_back_pair_and_timestamps(backlog: Backlog) -> None:
    a, b = await tasks(backlog, 2)
    store = backlog.store
    before = await store.task(a)
    connection = await store.open()
    await connection.execute(
        "CREATE TRIGGER fail_reverse BEFORE INSERT ON task_links "
        "WHEN new.relation = 'blocks' BEGIN SELECT RAISE(ABORT, 'reverse failed'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="reverse failed"):
        await store.link_task(a, Relation.DEPENDS_ON, b)
    assert await store.rows("SELECT * FROM task_links") == []
    assert (await store.task(a)).updated_at == before.updated_at


async def test_concurrent_connections_cannot_create_cycle(backlog: Backlog) -> None:
    a, b = await tasks(backlog, 2)
    other = Store(backlog.store.path, principal=backlog.store.principal, computer="second")
    await other.open()
    try:
        results = await asyncio.gather(
            backlog.store.link_task(a, Relation.DEPENDS_ON, b),
            other.link_task(b, Relation.DEPENDS_ON, a),
            return_exceptions=True,
        )
        assert results.count(True) == 1
        assert sum(isinstance(result, ValueError) for result in results) == 1
        assert len(await backlog.store.rows("SELECT * FROM task_links")) == 2
    finally:
        await other.close()


async def test_access_and_task_lifecycle(backlog: Backlog) -> None:
    a, b = await tasks(backlog, 2)
    store = backlog.store
    project, _ = await backlog.current()
    other = await backlog.create(backlog.cwd.parent / "other")
    await store.update_task(b, project_id=other.id)
    await store.register("reader", "reader")
    reader = store.view(principal="reader")
    await store.share(project.id, "reader", "editor")
    await store.link_task(a, Relation.DEPENDS_ON, b)
    assert await reader.task_links(a) == []
    with pytest.raises(LookupError):
        await reader.link_task(a, Relation.CAUSED_BY, b)
    await store.share(other.id, "reader", "reader")
    assert (await reader.task_links(a))[0].task == b
    with pytest.raises(PermissionError):
        await reader.unlink_task(a, Relation.DEPENDS_ON, b)
    await store.share(other.id, "reader", "editor")
    assert await reader.link_task(a, Relation.CAUSED_BY, b)
    await store.update_task(b, title="Renamed", status=Status.DONE)
    assert (await store.task_links(a))[0].title == "Renamed"
    await store.delete_task(b)
    assert all(link.status == Status.DELETED for link in await store.task_links(a))
    await store.delete_project(other.id)
    assert await store.task_links(a) == []
    with pytest.raises(LookupError):
        await store.link_task(a, Relation.RELATED_TO, 99999)


async def test_missing_inverse_and_conflicting_reasons(backlog: Backlog) -> None:
    a, b = await tasks(backlog, 2)
    store = backlog.store
    await store.link_task(a, Relation.DEPENDS_ON, b, reason="Keep this")
    connection = await store.open()
    await connection.execute("DELETE FROM task_links WHERE task_id = ?", (b,))
    await connection.commit()
    assert await store.link_task(b, Relation.BLOCKS, a)
    assert (await store.task_links(b))[0].reason == "Keep this"
    await connection.execute("UPDATE task_links SET reason = 'Conflict' WHERE task_id = ?", (b,))
    await connection.commit()
    with pytest.raises(ValueError, match="conflicting"):
        await store.link_task(a, Relation.DEPENDS_ON, b)
    assert await store.unlink_task(a, Relation.DEPENDS_ON, b)


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
    await server.task_link(args, backlog)
    assert events == [a, b]
    events.clear()
    await server.task_link(args, backlog)
    assert events == []
    before = await backlog.store.task(a)
    await server.task_update(server.UpdateTask(id=b, title="New provider"), backlog)
    assert events == [b] and invalidated == [f"backlog://tasks/{a}"]
    assert (await backlog.store.task(a)).updated_at == before.updated_at


async def test_existing_schema_is_registered_without_changing_links(backlog: Backlog) -> None:
    a, b = await tasks(backlog, 2)
    await backlog.store.link_task(a, Relation.PART_OF, b, reason="Keep this")
    path = backlog.store.path
    await backlog.store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE migrations")
    assert (await backlog.store.task(a)).title == "Task 0"
    assert (await backlog.store.task_links(b))[0].reason == "Keep this"
    assert len(await backlog.store.rows("SELECT * FROM migrations")) == 1
    await backlog.store.close()
    assert (await backlog.store.task_links(b))[0].relation == Relation.CONTAINS


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
    store = backlog.store
    await store.register("reader", "Reader")
    await store.share(project.id, "reader", "reader")
    reader = store.view(principal="reader")
    await store.link_task(a, Relation.DEPENDS_ON, b)
    assert len(await reader.task_links(a)) == 1
    seen: list[tuple[Any, list[str], Any]] = []

    async def stale(uris: list[str], event: Any) -> None:
        seen.append((namespace.get(), uris, event.model_dump()))

    monkeypatch.setattr(server, "stale", stale)
    await server.task_move(server.MoveTask(id=b, project=other.full_name), backlog)
    assert await reader.task_links(a) == []
    await server.task_update(server.UpdateTask(id=b, title="Private target"), backlog)
    reader_events = [(uris, event) for identity, uris, event in seen if identity == "reader"]
    assert reader_events
    assert all(uris == [f"backlog://tasks/{a}"] for uris, _ in reader_events)
    assert all(event["id"] == a and event["title"] == "Task 0" for _, event in reader_events)
