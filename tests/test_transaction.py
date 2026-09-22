import asyncio
from pathlib import Path

import pytest

from backlog_mcp.backlog import Backlog
from backlog_mcp.store import Store
from backlog_mcp.transaction import transaction


async def test_nested_writes_commit_only_at_outer_exit(backlog: Backlog) -> None:
    project, _ = await backlog.current()
    store = backlog.store
    connection = await store.open()
    observer = Store(store.path, principal=store.principal)
    await observer.open()
    try:
        async with transaction(connection):
            task = await store.add_task(project_id=project.id, title="Atomic batch")
            await store.add_comment(task.id, "Same transaction")
            assert await observer.task(task.id) is None
        assert (await observer.task(task.id)).title == "Atomic batch"
        assert len(await observer.comments(task_id=task.id)) == 1
        with pytest.raises(ValueError):
            async with transaction(connection):
                await store.update_task(task.id, title="Must roll back")
                await store.add_comment(task.id, "Must roll back too")
                raise ValueError("abort batch")
        assert (await observer.task(task.id)).title == "Atomic batch"
        assert len(await observer.comments(task_id=task.id)) == 1
    finally:
        await observer.close()


async def test_concurrent_writes_cannot_commit_another_coroutine(backlog: Backlog) -> None:
    project, _ = await backlog.current()
    store = backlog.store
    connection = await store.open()

    async def writer(number: int) -> None:
        async with transaction(connection):
            task = await store.add_task(project_id=project.id, title=f"Writer {number}")
            await asyncio.sleep(0)
            await store.add_comment(task.id, f"Comment {number}")
            if number % 2:
                raise ValueError("abort odd writer")

    results = await asyncio.gather(*(writer(i) for i in range(40)), return_exceptions=True)
    assert sum(isinstance(result, ValueError) for result in results) == 20
    tasks = await store.tasks(project_id=project.id)
    assert {task.title for task in tasks} == {f"Writer {i}" for i in range(0, 40, 2)}
    comments = await store.comments(project_id=project.id)
    assert {comment.body for comment in comments} == {f"Comment {i}" for i in range(0, 40, 2)}
    assert not connection.in_transaction


async def test_cancelled_writer_rolls_back_and_unlocks(backlog: Backlog) -> None:
    project, _ = await backlog.current()
    store = backlog.store
    connection = await store.open()
    started = asyncio.Event()

    async def writer() -> None:
        async with transaction(connection):
            await store.add_task(project_id=project.id, title="Cancelled")
            started.set()
            await asyncio.Event().wait()

    pending = asyncio.create_task(writer())
    await started.wait()
    successor = asyncio.create_task(store.add_task(project_id=project.id, title="Survivor"))
    await asyncio.sleep(0)
    assert not successor.done()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    async with asyncio.timeout(5):
        await successor
    assert [task.title for task in await store.tasks(project_id=project.id)] == ["Survivor"]


async def test_caught_nested_failure_aborts_outer_transaction(backlog: Backlog) -> None:
    project, _ = await backlog.current()
    connection = await backlog.store.open()
    with pytest.raises(RuntimeError, match="nested transaction failed"):
        async with transaction(connection):
            await backlog.store.add_task(project_id=project.id, title="Must roll back")
            with pytest.raises(ValueError):
                async with transaction(connection):
                    raise ValueError("nested failure")
    assert await backlog.store.tasks(project_id=project.id) == []


async def test_writer_lock_is_shared_between_databases(tmp_path: Path) -> None:
    first, second = Store(tmp_path / "first.sqlite3"), Store(tmp_path / "second.sqlite3")
    try:
        a, b = await asyncio.gather(first.open(), second.open())
        entered, release, other_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def owner() -> None:
            async with transaction(a):
                entered.set()
                await release.wait()

        async def competitor() -> None:
            await entered.wait()
            async with transaction(b):
                other_entered.set()

        tasks = [asyncio.create_task(owner()), asyncio.create_task(competitor())]
        await entered.wait()
        await asyncio.sleep(0)
        assert not other_entered.is_set()
        release.set()
        async with asyncio.timeout(5):
            await asyncio.gather(*tasks)
        assert other_entered.is_set()
    finally:
        await asyncio.gather(first.close(), second.close())
