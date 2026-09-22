"""Explicit 0.1 import preserves data and publishes only a complete new database."""

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from backlog_mcp.auth import basic_namespace
from backlog_mcp.store import Store

SCRIPT = Path(__file__).parents[1] / "scripts" / "migrate_db_from_0.1.py"
spec = importlib.util.spec_from_file_location("import_01", SCRIPT)
assert spec and spec.loader
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE projects(id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT, name TEXT,
                description TEXT, archived INTEGER, created_at REAL, updated_at REAL);
            CREATE TABLE project_roots(project_id INTEGER, computer TEXT, root TEXT);
            CREATE TABLE tasks(id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER,
                title TEXT, body TEXT, status TEXT, priority INTEGER, tags TEXT,
                created_at REAL, updated_at REAL, closed_at REAL);
            CREATE TABLE notes(id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER,
                title TEXT, body TEXT, tags TEXT, shared INTEGER, created_at REAL, updated_at REAL);
            CREATE TABLE task_comments(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER,
                body TEXT, kind TEXT, created_at REAL);
            INSERT INTO projects VALUES (7, 'infra', 'Infrastructure', 'Orchestration', 0, 10, 20);
            INSERT INTO project_roots VALUES (7, 'old-host', '/work');
            INSERT INTO tasks VALUES
                (12, 7, 'Needle', 'Original history', 'done', 1, 'ops', 11, 22, 22);
            INSERT INTO notes VALUES (8, 7, 'Convention', 'Memory needle', 'ops', 1, 12, 23);
            INSERT INTO task_comments VALUES (9, 12, 'Comment needle', 'comment', 21);
            UPDATE sqlite_sequence SET seq=99 WHERE name='tasks';
            PRAGMA user_version=5;
        """)
    return path


async def test_import_preserves_records_and_assigns_owner(source: Path, tmp_path: Path) -> None:
    original = source.read_bytes()
    target = tmp_path / "new.sqlite3"
    counts = migration.migrate(source, target, user="alice", computer="old-host")
    assert counts == {"projects": 1, "tasks": 1, "memory": 1, "comments": 1}
    assert source.read_bytes() == original
    store = Store(target)
    alice = store.view(principal=basic_namespace("alice"), computer="old-host")
    try:
        project = await alice.project(7)
        assert project and project.full_name == "alice/infra" and project.role == "owner"
        assert project.roots[0].root == "/work"
        task = await alice.task(12)
        assert task and task.body == "Original history" and task.updated_at == 22
        assert task.closed_at == 22 and task.created_at == 11
        assert task.created_by and task.created_by.principal == alice.principal
        assert [item.id for item in await alice.search("needle")] == [12]
        assert [item.id for item in await alice.search_notes("needle")] == [8]
        assert [item.id for item in await alice.comments(query="needle")] == [9]
        assert [item.id for item in await alice.search_projects("orchestration")] == [7]
        assert (await alice.add_task(project_id=7, title="Next")).id == 100
        bob = store.view(principal=basic_namespace("bob"), computer="old-host")
        assert await bob.projects() == []
        assert await bob.search("needle") == []
        assert await bob.search_notes("needle") == []
        assert await bob.comments(query="needle") == []
        assert await bob.search_projects("orchestration") == []
    finally:
        await store.close()


def test_import_refuses_existing_destination(source: Path, tmp_path: Path) -> None:
    target = tmp_path / "existing"
    target.write_bytes(b"keep")
    with pytest.raises(ValueError, match="already exists"):
        migration.migrate(source, target, user="alice")
    assert target.read_bytes() == b"keep"
    with pytest.raises(ValueError, match="already exists"):
        migration.migrate(source, source, user="alice")


def test_invalid_import_does_not_publish(source: Path, tmp_path: Path) -> None:
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE tasks SET project_id=999")
    target = tmp_path / "new.sqlite3"
    with pytest.raises(sqlite3.IntegrityError):
        migration.migrate(source, target, user="alice")
    assert not target.exists()
    assert not list(tmp_path.glob(".new.sqlite3.*"))


def test_import_accepts_early_project_roots(source: Path, tmp_path: Path) -> None:
    with sqlite3.connect(source) as connection:
        connection.executescript("""
            DROP TABLE project_roots;
            DROP TABLE task_comments;
            ALTER TABLE projects ADD COLUMN root TEXT DEFAULT '/legacy';
        """)
    target = tmp_path / "new.sqlite3"
    migration.migrate(source, target, user="alice", computer="legacy-host")
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT computer,root FROM project_roots").fetchone() == (
            "legacy-host",
            "/legacy",
        )
        assert connection.execute("SELECT body FROM tasks WHERE id=12").fetchone() == (
            "Original history",
        )


async def test_import_extracts_updates_and_preserves_existing_comment_ids(
    source: Path, tmp_path: Path
) -> None:
    body = (
        "Description\n\n```md\n---\n# UPDATE: 2026-01-01T00:00:00Z\nExample\n```\n"
        "---\n# UPDATE: 2026-09-20 02:10:42\nStatus changed: todo → doing\n\nFirst progress\n"
        "---\n# UPDATE: 2026-09-21T12:01:34Z\nSecond progress\n"
    )
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE tasks SET body=? WHERE id=12", (body,))
        connection.execute("UPDATE sqlite_sequence SET seq=40 WHERE name='task_comments'")
    original = source.read_bytes()
    target = tmp_path / "new.sqlite3"
    counts = migration.migrate(source, target, user="alice")
    assert counts["comments"] == 3
    assert source.read_bytes() == original
    store = Store(target)
    alice = store.view(principal=basic_namespace("alice"), computer="unknown")
    try:
        task = await alice.task(12)
        assert task and task.body.endswith("Example\n```\n") and task.updated_at == 22
        comments = await alice.comments(task_id=12)
        assert [item.id for item in comments] == [9, 41, 42]
        assert comments[1].body == "Status changed: todo → doing\n\nFirst progress\n"
        assert comments[2].body == "Second progress\n"
        assert comments[1].created_at == 1789870242
        assert comments[2].created_at == 1789992094
        assert await alice.search("Second progress") == []
        assert [item.id for item in await alice.comments(query="Second progress")] == [42]
    finally:
        await store.close()


@pytest.mark.parametrize(
    "body",
    [
        "Text\n---\n# UPDATE: invalid\nKeep me\n",
        "Text\n---\n# UPDATE: 2026-02-30T00:00:00Z\nKeep me\n",
        "Text\n# UPDATE: 2026-01-01T00:00:00Z\nNo separator\n",
        "Text\n---\n# UPDATE: 2026-01-01T00:00:00Z\nValid\n---\n# UPDATE: bad\nInvalid\n",
    ],
)
def test_ambiguous_history_stays_intact(body: str) -> None:
    assert migration.split_history(body) == (body, [], True)


@pytest.mark.parametrize("fence", ["```md", "~~~~"])
def test_history_parser_ignores_fenced_examples(fence: str) -> None:
    end = fence[:3] if fence.startswith("`") else fence
    body = f"Example\n{fence}\n---\n# UPDATE: 2026-01-01T00:00:00Z\nExample\n{end}\n"
    assert migration.split_history(body) == (body, [], False)


def test_equal_or_out_of_order_dates_keep_text_and_original_dates() -> None:
    body = (
        "---\n# UPDATE: 2026-01-02T00:00:00Z\nLater\n"
        "---\n# UPDATE: 2026-01-01T00:00:00Z\nEarlier\n"
        "---\n# UPDATE: 2026-01-01T00:00:00Z\nSame\n"
    )
    description, entries, ambiguous = migration.split_history(body)
    assert not ambiguous and description == ""
    assert [entry[0] for entry in entries] == ["Later\n", "Earlier\n", "Same\n"]
    assert entries[0][1] > entries[1][1] == entries[2][1]
