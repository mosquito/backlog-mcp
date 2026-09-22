import asyncio
import sqlite3

import pytest
from aiohttp_tiny_mcp.client.base import BaseClient
from helpers import mutate
from test_server import call

from backlog_mcp import backlog as scope
from backlog_mcp import embeddings
from backlog_mcp import server as operations
from backlog_mcp.backlog import Backlog
from backlog_mcp.models import Status


async def test_all_comments_are_returned_without_changing_description(client: BaseClient) -> None:
    task = await call(client, "task_add", title="Task", body="Description")
    assert (await call(client, "task_get", id=task["id"]))["comments"] == []
    expected = [f"Comment {number}" for number in range(51)]
    for body in expected:
        await call(client, "task_comment_add", task_id=task["id"], body=body)
    details = await call(client, "task_get", id=task["id"])
    assert details["body"] == "Description"
    assert [item["body"] for item in details["comments"]] == expected
    history = await call(client, "task_comment_list", task_id=task["id"])
    assert history["count"] == len(expected)
    assert history["items"] == details["comments"]
    assert "comments" not in (await call(client, "task_list", full=True))["items"][0]


async def test_comment_search_has_separate_content_and_follows_task_scope(backlog: Backlog) -> None:
    project, _ = await backlog.current()
    task = await mutate(
        operations.add_task(
            await backlog.open(),
            backlog.actor,
            project_id=project.id,
            title="Work",
            body="Definition",
            tags=["api"],
        ),
        backlog.transaction,
    )
    comment = await mutate(
        operations.add_comment(
            await backlog.open(), backlog.actor, task.id, "Specialcommentneedle"
        ),
        backlog.transaction,
    )
    query = embeddings.Search(query="Specialcommentneedle", min_score=1)
    found = await embeddings.search(
        query, backlog, connection=await backlog.open(), transaction=backlog.transaction
    )
    assert found.tasks == [] and found.memory == []
    assert [(item.id, item.task_id, item.match) for item in found.comments] == [
        (comment.id, task.id, "fts")
    ]
    assert (
        await embeddings.search(
            query.model_copy(update={"kinds": ["tasks"]}),
            backlog,
            connection=await backlog.open(),
            transaction=backlog.transaction,
        )
    ).count == 0
    assert (
        await embeddings.search(
            query.model_copy(update={"tag": "other"}),
            backlog,
            connection=await backlog.open(),
            transaction=backlog.transaction,
        )
    ).count == 0
    assert (
        await embeddings.search(
            query.model_copy(update={"status": [Status.DONE]}),
            backlog,
            connection=await backlog.open(),
            transaction=backlog.transaction,
        )
    ).count == 0
    other = await backlog.create(backlog.cwd.parent / "other")
    await mutate(
        operations.update_task(await backlog.open(), backlog.actor, task.id, project_id=other.id),
        backlog.transaction,
    )
    assert (
        await embeddings.search(
            query, backlog, connection=await backlog.open(), transaction=backlog.transaction
        )
    ).count == 0
    moved = await embeddings.search(
        query.model_copy(update={"project": other.slug}),
        backlog,
        connection=await backlog.open(),
        transaction=backlog.transaction,
    )
    assert moved.comments[0].project == other.full_name
    await mutate(
        operations.delete_task(await backlog.open(), backlog.actor, task.id), backlog.transaction
    )
    assert (
        await embeddings.search(
            query.model_copy(update={"project": other.slug}),
            backlog,
            connection=await backlog.open(),
            transaction=backlog.transaction,
        )
    ).count == 0


async def test_comments_have_independent_semantic_vectors(backlog: Backlog) -> None:
    project, _ = await backlog.current()
    task = await mutate(
        operations.add_task(
            await backlog.open(), backlog.actor, project_id=project.id, title="Unrelated work"
        ),
        backlog.transaction,
    )
    comment = await mutate(
        operations.add_comment(
            await backlog.open(),
            backlog.actor,
            task.id,
            "Reset forgotten passwords with an email link",
        ),
        backlog.transaction,
    )
    result = await embeddings.search(
        embeddings.Search(query="recover login credentials", kinds=["comments"], min_score=0),
        backlog,
        connection=await backlog.open(),
        transaction=backlog.transaction,
    )
    assert result.tasks == [] and result.memory == []
    assert result.comments[0].id == comment.id
    assert result.comments[0].match == "semantic" and result.comments[0].score is not None
    later = await mutate(
        operations.add_comment(await backlog.open(), backlog.actor, task.id, "uniquenewcomment"),
        backlog.transaction,
    )
    refreshed = await embeddings.search(
        embeddings.Search(query="uniquenewcomment", kinds=["comments"], min_score=1),
        backlog,
        connection=await backlog.open(),
        transaction=backlog.transaction,
    )
    assert refreshed.comments[0].id == later.id


async def test_concurrent_comments_and_status_keep_description(backlog: Backlog) -> None:
    project, _ = await backlog.current()
    task = await mutate(
        operations.add_task(
            await backlog.open(),
            backlog.actor,
            project_id=project.id,
            title="Work",
            body="Definition",
        ),
        backlog.transaction,
    )
    await asyncio.gather(
        mutate(
            operations.add_comment(await backlog.open(), backlog.actor, task.id, "First"),
            backlog.transaction,
        ),
        mutate(
            operations.add_comment(await backlog.open(), backlog.actor, task.id, "Second"),
            backlog.transaction,
        ),
        mutate(
            operations.update_task(
                await backlog.open(), backlog.actor, task.id, status=Status.DOING
            ),
            backlog.transaction,
        ),
    )
    assert await operations.count_comments(await backlog.open(), backlog.actor, task.id) == 3
    saved = await operations.read_task(await backlog.open(), backlog.actor, task.id)
    assert saved is not None and saved.body == "Definition" and saved.status == Status.DOING
    with pytest.raises(ValueError, match="empty"):
        await mutate(
            operations.add_comment(await backlog.open(), backlog.actor, task.id, " \n"),
            backlog.transaction,
        )
    with pytest.raises(LookupError, match="no task"):
        await mutate(
            operations.add_comment(await backlog.open(), backlog.actor, 999, "Missing"),
            backlog.transaction,
        )
    await mutate(
        scope.delete_project(await backlog.open(), backlog.actor, project.id), backlog.transaction
    )
    assert await operations.read_comment(await backlog.open(), backlog.actor, 1) is None
    assert await operations.read_comments(await backlog.open(), backlog.actor, query="First") == []


async def test_external_comments_invalidate_semantic_cache(backlog: Backlog) -> None:
    project, _ = await backlog.current()
    task = await mutate(
        operations.add_task(
            await backlog.open(), backlog.actor, project_id=project.id, title="Work"
        ),
        backlog.transaction,
    )
    query = embeddings.Search(query="lost credentials", kinds=["comments"], min_score=-1)
    assert (
        await embeddings.search(
            query, backlog, connection=await backlog.open(), transaction=backlog.transaction
        )
    ).comments == []
    with sqlite3.connect(backlog.database.path) as db:
        cursor = db.execute(
            "INSERT INTO task_comments (task_id, body, created_at, principal, computer) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                task.id,
                "Recover access by resetting your password",
                1,
                backlog.principal,
                "external",
            ),
        )
        comment_id = cursor.lastrowid
    await embeddings.indexers[backlog.database].refresh()
    result = await embeddings.search(
        query, backlog, connection=await backlog.open(), transaction=backlog.transaction
    )
    assert [(item.id, item.match) for item in result.comments] == [(comment_id, "semantic")]
