import asyncio
import sqlite3

import pytest
from aiohttp_tiny_mcp.client.base import BaseClient
from test_server import call

from backlog_mcp import embeddings
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
    task = await backlog.store.add_task(
        project_id=project.id, title="Work", body="Definition", tags=["api"]
    )
    comment = await backlog.store.add_comment(task.id, "Specialcommentneedle")
    query = embeddings.Search(query="Specialcommentneedle", min_score=1)
    found = await embeddings.search(query, backlog)
    assert found.tasks == [] and found.memory == []
    assert [(item.id, item.task_id, item.match) for item in found.comments] == [
        (comment.id, task.id, "fts")
    ]
    assert (
        await embeddings.search(query.model_copy(update={"kinds": ["tasks"]}), backlog)
    ).count == 0
    assert (await embeddings.search(query.model_copy(update={"tag": "other"}), backlog)).count == 0
    assert (
        await embeddings.search(query.model_copy(update={"status": [Status.DONE]}), backlog)
    ).count == 0
    other = await backlog.create(backlog.cwd.parent / "other")
    await backlog.store.update_task(task.id, project_id=other.id)
    assert (await embeddings.search(query, backlog)).count == 0
    moved = await embeddings.search(query.model_copy(update={"project": other.slug}), backlog)
    assert moved.comments[0].project == other.full_name
    await backlog.store.delete_task(task.id)
    assert (
        await embeddings.search(query.model_copy(update={"project": other.slug}), backlog)
    ).count == 0


async def test_comments_have_independent_semantic_vectors(backlog: Backlog) -> None:
    project, _ = await backlog.current()
    task = await backlog.store.add_task(project_id=project.id, title="Unrelated work")
    comment = await backlog.store.add_comment(
        task.id, "Reset forgotten passwords with an email link"
    )
    result = await embeddings.search(
        embeddings.Search(query="recover login credentials", kinds=["comments"], min_score=0),
        backlog,
    )
    assert result.tasks == [] and result.memory == []
    assert result.comments[0].id == comment.id
    assert result.comments[0].match == "semantic" and result.comments[0].score is not None
    later = await backlog.store.add_comment(task.id, "uniquenewcomment")
    refreshed = await embeddings.search(
        embeddings.Search(query="uniquenewcomment", kinds=["comments"], min_score=1), backlog
    )
    assert refreshed.comments[0].id == later.id


async def test_concurrent_comments_and_status_keep_description(backlog: Backlog) -> None:
    project, _ = await backlog.current()
    task = await backlog.store.add_task(project_id=project.id, title="Work", body="Definition")
    await asyncio.gather(
        backlog.store.add_comment(task.id, "First"),
        backlog.store.add_comment(task.id, "Second"),
        backlog.store.update_task(task.id, status=Status.DOING),
    )
    assert await backlog.store.count_comments(task.id) == 3
    saved = await backlog.store.task(task.id)
    assert saved is not None and saved.body == "Definition" and saved.status == Status.DOING
    with pytest.raises(ValueError, match="empty"):
        await backlog.store.add_comment(task.id, " \n")
    with pytest.raises(LookupError, match="no task"):
        await backlog.store.add_comment(999, "Missing")
    await backlog.store.delete_project(project.id)
    assert await backlog.store.comment(1) is None
    assert await backlog.store.comments(query="First") == []


async def test_external_comments_invalidate_semantic_cache(backlog: Backlog) -> None:
    project, _ = await backlog.current()
    task = await backlog.store.add_task(project_id=project.id, title="Work")
    query = embeddings.Search(query="lost credentials", kinds=["comments"], min_score=-1)
    assert (await embeddings.search(query, backlog)).comments == []
    with sqlite3.connect(backlog.store.path) as db:
        cursor = db.execute(
            "INSERT INTO task_comments (task_id, body, created_at, principal, computer) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                task.id,
                "Recover access by resetting your password",
                1,
                backlog.store.principal,
                "external",
            ),
        )
        comment_id = cursor.lastrowid
    result = await embeddings.search(query, backlog)
    assert [(item.id, item.match) for item in result.comments] == [(comment_id, "semantic")]
