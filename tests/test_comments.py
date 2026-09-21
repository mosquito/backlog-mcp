import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest
from aiohttp_tiny_mcp.client_base import BaseClient
from test_server import call

from backlog_mcp import embeddings
from backlog_mcp.backlog import Backlog
from backlog_mcp.history import split_history
from backlog_mcp.models import Status
from backlog_mcp.store import MIGRATIONS, Store


@pytest.mark.parametrize("timestamp", ["2026-09-20T00:26:47Z", "2026-09-20 00:26:47"])
def test_legacy_history_preserves_markdown_and_utc_dates(timestamp: str) -> None:
    description = "## Requirements\n\nKeep this.\n"
    comment = "\n### Progress\n\n- **Done**\n\n```text\n---\n# UPDATE: example\n```\n"
    body, entries, warning = split_history(description + f"---\n# UPDATE: {timestamp}\n" + comment)
    assert body == description
    assert not warning
    assert [entry.body for entry in entries] == [comment]
    assert entries[0].created_at == datetime(2026, 9, 20, 0, 26, 47, tzinfo=UTC).timestamp()


@pytest.mark.parametrize("fence", ["```", "~~~~"])
def test_update_examples_inside_code_fences_are_not_history(fence: str) -> None:
    body = f"Example:\n{fence}text\n---\n# UPDATE: 2026-09-20T00:26:47Z\nCode\n{fence}\n"
    assert split_history(body) == (body, [], False)


@pytest.mark.parametrize("heading", ["# UPDATE: bad date", "# UPDATE: 2026-02-30T00:00:00Z"])
def test_ambiguous_history_is_preserved_and_flagged(heading: str) -> None:
    body = "Description\n---\n" + heading + "\nKeep everything\n"
    assert split_history(body) == (body, [], True)


async def test_migration_preserves_originals_and_is_not_repeated(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    original = (
        "Description\n---\n# UPDATE: 2026-09-20 00:26:47\nFirst\n"
        "---\n# UPDATE: 2026-09-20T00:26:47Z\nSecond\n"
    )
    ambiguous = "Description\n---\n# UPDATE: invalid\nKeep all\n"
    db = sqlite3.connect(path)
    for migration in MIGRATIONS[:5]:
        for statement in migration:
            db.execute(statement, {"computer": "here"})
    db.execute("PRAGMA user_version=5")
    db.execute("INSERT INTO projects (slug, name, created_at, updated_at) VALUES ('p', 'P', 1, 2)")
    for body in (original, ambiguous):
        db.execute(
            "INSERT INTO tasks (project_id, title, body, created_at, updated_at) "
            "VALUES (1, 'Task', ?, 1, 2)",
            (body,),
        )
    db.commit()
    db.close()
    store = Store(path)
    try:
        task = await store.task(1)
        assert task is not None and task.body == "Description\n"
        assert task.updated_at == 2 and not task.history_warning
        comments = await store.comments(task_id=1)
        assert [item.body for item in comments] == ["First\n", "Second\n"]
        assert all(item.kind == "legacy" for item in comments)
        row = await store.row("SELECT body FROM task_history_originals WHERE task_id = 1")
        assert row is not None and row[0] == original
        untouched = await store.task(2)
        assert untouched is not None and untouched.body == ambiguous and untouched.history_warning
        assert await store.search("First") == []
        assert len(await store.comments(query="First")) == 1
        await store.close()
        assert await store.count_comments(1) == 2
        assert (await store.comments(task_id=1))[0].id == comments[0].id
    finally:
        await store.close()


async def test_comments_page_without_changing_description(client: BaseClient) -> None:
    task = await call(client, "task_add", title="Task", body="Description")
    assert (await call(client, "task_get", id=task["id"]))["comments"] == []
    for number in range(3):
        await call(client, "task_comment_add", task_id=task["id"], body=f"Comment {number}")
    details = await call(client, "task_get", id=task["id"], comments_limit=2)
    assert details["body"] == "Description"
    assert details["comments_total"] == 3 and details["comments_next_offset"] == 2
    assert [item["body"] for item in details["comments"]] == ["Comment 0", "Comment 1"]
    rest = await call(client, "task_comment_list", task_id=task["id"], offset=2)
    assert rest["count"] == 3 and rest["next_offset"] is None
    assert [item["body"] for item in rest["items"]] == ["Comment 2"]
    assert "comments" not in (await call(client, "task_list", full=True))["items"][0]


async def test_comment_search_has_separate_content_and_follows_task_scope(backlog: Backlog) -> None:
    project, _ = await backlog.current()
    task = await backlog.store.add_task(
        project_id=project.id, title="Work", body="Definition", tags=["api"]
    )
    comment = await backlog.store.add_comment(task.id, "Specialcommentneedle")
    query = embeddings.Search(query="Specialcommentneedle", min_score=1)
    found = await embeddings.search(query, backlog)
    assert found.tasks == [] and found.notes == []
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
    assert moved.comments[0].project == other.slug
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
    assert result.tasks == [] and result.notes == []
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


async def test_failed_migration_rolls_back_schema_and_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "old.sqlite3"
    original = "Description\n---\n# UPDATE: 2026-09-20T00:26:47Z\nKeep this\n"
    db = sqlite3.connect(path)
    for migration in MIGRATIONS[:5]:
        for statement in migration:
            db.execute(statement, {"computer": "here"})
    db.execute("PRAGMA user_version=5")
    db.execute("INSERT INTO projects (slug, name, created_at, updated_at) VALUES ('p', 'P', 1, 2)")
    db.execute(
        "INSERT INTO tasks (project_id, title, body, created_at, updated_at) "
        "VALUES (1, 'Task', ?, 1, 2)",
        (original,),
    )
    db.commit()
    store = Store(path)
    migrate = store.migrate_history

    async def fail_after_changes(connection: aiosqlite.Connection) -> None:
        await migrate(connection)
        raise RuntimeError("interrupted migration")

    monkeypatch.setattr(store, "migrate_history", fail_after_changes)
    with pytest.raises(RuntimeError, match="interrupted"):
        await store.open()
    assert db.execute("PRAGMA user_version").fetchone()[0] == 5
    assert db.execute("SELECT body FROM tasks").fetchone()[0] == original
    assert not db.execute("SELECT name FROM sqlite_master WHERE name='task_comments'").fetchall()
    db.close()
    monkeypatch.setattr(store, "migrate_history", migrate)
    try:
        assert await store.count_comments(1) == 1
    finally:
        await store.close()


async def test_external_comments_invalidate_semantic_cache(backlog: Backlog) -> None:
    project, _ = await backlog.current()
    task = await backlog.store.add_task(project_id=project.id, title="Work")
    query = embeddings.Search(query="lost credentials", kinds=["comments"], min_score=-1)
    assert (await embeddings.search(query, backlog)).comments == []
    with sqlite3.connect(backlog.store.path) as db:
        cursor = db.execute(
            "INSERT INTO task_comments (task_id, body, created_at) VALUES (?, ?, ?)",
            (task.id, "Recover access by resetting your password", 1),
        )
        comment_id = cursor.lastrowid
    result = await embeddings.search(query, backlog)
    assert [(item.id, item.match) for item in result.comments] == [(comment_id, "semantic")]
