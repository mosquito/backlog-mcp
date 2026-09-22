import asyncio
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from backlog_mcp import server
from backlog_mcp.backlog import Backlog
from backlog_mcp.transaction import Transaction


async def test_batch_commits_once_and_rolls_back_on_failure(backlog: Backlog) -> None:
    project, _ = await backlog.current()
    connection = await backlog.open()
    observer = Backlog(backlog.database.path, principal=backlog.principal)
    other = await observer.open()
    try:
        async with backlog.transaction:
            task = await server.add_task(
                connection, backlog.actor, project_id=project.id, title="Atomic batch"
            )
            await server.add_comment(connection, backlog.actor, task.id, "Same transaction")
            assert await server.read_task(other, observer.actor, task.id) is None
        assert (await server.read_task(other, observer.actor, task.id)).title == "Atomic batch"
        assert len(await server.read_comments(other, observer.actor, task_id=task.id)) == 1
        with pytest.raises(ValueError):
            async with backlog.transaction:
                await server.update_task(connection, backlog.actor, task.id, title="Must roll back")
                await server.add_comment(connection, backlog.actor, task.id, "Must roll back too")
                raise ValueError("abort batch")
        assert (await server.read_task(other, observer.actor, task.id)).title == "Atomic batch"
        assert len(await server.read_comments(other, observer.actor, task_id=task.id)) == 1
    finally:
        await observer.close()


async def test_concurrent_writes_cannot_commit_another_coroutine(backlog: Backlog) -> None:
    project, _ = await backlog.current()
    connection = await backlog.open()

    async def writer(number: int) -> None:
        async with backlog.transaction:
            task = await server.add_task(
                connection, backlog.actor, project_id=project.id, title=f"Writer {number}"
            )
            await asyncio.sleep(0)
            await server.add_comment(connection, backlog.actor, task.id, f"Comment {number}")
            if number % 2:
                raise ValueError("abort odd writer")

    results = await asyncio.gather(*(writer(i) for i in range(40)), return_exceptions=True)
    assert sum(isinstance(result, ValueError) for result in results) == 20
    tasks = await server.read_tasks(connection, backlog.actor, project_id=project.id)
    assert {task.title for task in tasks} == {f"Writer {i}" for i in range(0, 40, 2)}
    comments = await server.read_comments(connection, backlog.actor, project_id=project.id)
    assert {comment.body for comment in comments} == {f"Comment {i}" for i in range(0, 40, 2)}
    assert not connection.in_transaction


async def test_cancelled_writer_rolls_back_and_unlocks(backlog: Backlog) -> None:
    project, _ = await backlog.current()
    connection = await backlog.open()
    started = asyncio.Event()

    async def writer(title: str, wait: bool = False) -> None:
        async with backlog.transaction:
            await server.add_task(connection, backlog.actor, project_id=project.id, title=title)
            if wait:
                started.set()
                await asyncio.Event().wait()

    pending = asyncio.create_task(writer("Cancelled", True))
    await started.wait()
    successor = asyncio.create_task(writer("Survivor"))
    await asyncio.sleep(0)
    assert not successor.done()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    async with asyncio.timeout(5):
        await successor
    assert [task.title for task in await server.read_tasks(connection, backlog.actor)] == [
        "Survivor"
    ]


@pytest.mark.parametrize("stage", ["begin", "commit", "rollback"])
async def test_failure_releases_lock(stage: str) -> None:
    connection = AsyncMock()
    transaction = Transaction(connection, asyncio.Lock())
    error = RuntimeError(stage)
    connection.execute.side_effect = error if stage == "begin" else None
    if stage != "begin":
        getattr(connection, stage).side_effect = error
    with pytest.raises(RuntimeError, match=stage):
        async with transaction:
            if stage == "rollback":
                raise ValueError("body failed")
    assert not transaction.lock.locked()
    if stage == "commit":
        connection.rollback.assert_awaited_once()


async def test_views_share_connection_and_transaction(backlog: Backlog) -> None:
    connection = await backlog.open()
    view = backlog.view(principal="other", computer="remote")
    assert await view.open() is connection
    assert view.transaction is backlog.transaction
    assert await server.active_connection(None) is connection
    assert await server.active_transaction(None) is backlog.transaction


async def test_cancelled_begin_rolls_back_before_unlocking(
    backlog: Backlog, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = await backlog.open()
    original = connection.execute
    started = asyncio.Event()

    async def begin(sql: str) -> aiosqlite.Cursor:
        cursor = await original(sql)
        try:
            started.set()
            await asyncio.Event().wait()
        except BaseException:
            await cursor.close()
            raise
        return cursor

    monkeypatch.setattr(connection, "execute", begin)
    pending = asyncio.create_task(backlog.transaction.__aenter__())
    await started.wait()
    assert connection.in_transaction
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not connection.in_transaction
    assert not backlog.transaction.lock.locked()
    monkeypatch.setattr(connection, "execute", original)
    async with backlog.transaction:
        assert connection.in_transaction
