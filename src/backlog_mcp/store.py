"""SQLite storage: projects, tasks, notes, and the FTS5 index over them."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from .models import OPEN_STATUSES, PRIORITIES, RANKS, Priority, Status

#: Applied in order; the file's `user_version` says how many already ran.
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            root TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            archived INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )""",
        """CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'todo',
            priority INTEGER NOT NULL DEFAULT 2,
            tags TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            closed_at REAL
        )""",
        "CREATE INDEX tasks_by_project ON tasks (project_id, status, priority)",
        "CREATE INDEX tasks_by_update ON tasks (updated_at DESC)",
        """CREATE VIRTUAL TABLE tasks_fts USING fts5 (
            title, body, tags,
            content='tasks', content_rowid='id',
            tokenize="unicode61 remove_diacritics 2"
        )""",
        """CREATE TRIGGER tasks_fts_insert AFTER INSERT ON tasks BEGIN
            INSERT INTO tasks_fts (rowid, title, body, tags)
            VALUES (new.id, new.title, new.body, new.tags);
        END""",
        """CREATE TRIGGER tasks_fts_delete AFTER DELETE ON tasks BEGIN
            INSERT INTO tasks_fts (tasks_fts, rowid, title, body, tags)
            VALUES ('delete', old.id, old.title, old.body, old.tags);
        END""",
        """CREATE TRIGGER tasks_fts_update AFTER UPDATE ON tasks BEGIN
            INSERT INTO tasks_fts (tasks_fts, rowid, title, body, tags)
            VALUES ('delete', old.id, old.title, old.body, old.tags);
            INSERT INTO tasks_fts (rowid, title, body, tags)
            VALUES (new.id, new.title, new.body, new.tags);
        END""",
    ),
    (
        """CREATE TABLE notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '',
            shared INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )""",
        "CREATE UNIQUE INDEX notes_by_title ON notes (project_id, title)",
        "CREATE INDEX notes_by_update ON notes (updated_at DESC)",
        """CREATE VIRTUAL TABLE notes_fts USING fts5 (
            title, body, tags,
            content='notes', content_rowid='id',
            tokenize="unicode61 remove_diacritics 2"
        )""",
        """CREATE TRIGGER notes_fts_insert AFTER INSERT ON notes BEGIN
            INSERT INTO notes_fts (rowid, title, body, tags)
            VALUES (new.id, new.title, new.body, new.tags);
        END""",
        """CREATE TRIGGER notes_fts_delete AFTER DELETE ON notes BEGIN
            INSERT INTO notes_fts (notes_fts, rowid, title, body, tags)
            VALUES ('delete', old.id, old.title, old.body, old.tags);
        END""",
        """CREATE TRIGGER notes_fts_update AFTER UPDATE ON notes BEGIN
            INSERT INTO notes_fts (notes_fts, rowid, title, body, tags)
            VALUES ('delete', old.id, old.title, old.body, old.tags);
            INSERT INTO notes_fts (rowid, title, body, tags)
            VALUES (new.id, new.title, new.body, new.tags);
        END""",
    ),
)

OPEN_LIST = ", ".join(f"'{status.value}'" for status in OPEN_STATUSES)

COUNTS = (
    "(SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id) AS total_tasks, "
    f"(SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id "
    f"AND t.status IN ({OPEN_LIST})) AS open_tasks"
)

TASK_COLUMNS = "t.*, p.slug AS project"

NOTE_COLUMNS = "n.*, p.slug AS project"

#: A note is a short fact, not a page; the body stops here, in characters.
NOTE_LIMIT = 1024

ORDERS = {
    "backlog": "t.priority, t.created_at, t.id",
    "recent": "t.updated_at DESC, t.id DESC",
}


def clean_tags(tags: Iterable[str]) -> list[str]:
    """Tags are lower-case, unique and sorted, so listings and completion are stable."""
    return sorted({tag.strip().lower() for tag in tags if tag.strip()})


def join_tags(tags: Iterable[str]) -> str:
    return ",".join(clean_tags(tags))


def split_tags(text: str) -> list[str]:
    return [tag for tag in text.split(",") if tag]


def check_note(body: str) -> None:
    """Refuse a body longer than `NOTE_LIMIT`, before anything is written."""
    if len(body) > NOTE_LIMIT:
        raise ValueError(f"a note holds at most {NOTE_LIMIT} characters, this one has {len(body)}")


def match_query(text: str) -> str:
    """Turn user text into an FTS5 prefix query where every token must match.

    Quoting each token keeps FTS5 operators out of the query, so no input is a syntax error.
    """
    return " ".join(f'"{token}"*' for token in re.findall(r"\w+", text))


@dataclass(slots=True)
class Project:
    id: int
    slug: str
    name: str
    root: str
    description: str
    archived: bool
    created_at: float
    updated_at: float
    total_tasks: int = 0
    open_tasks: int = 0


@dataclass(slots=True)
class Task:
    id: int
    project_id: int
    project: str
    title: str
    body: str
    status: Status
    priority: Priority
    tags: list[str]
    created_at: float
    updated_at: float
    closed_at: float | None = None
    snippet: str | None = None


@dataclass(slots=True)
class Note:
    id: int
    project_id: int
    project: str
    title: str
    body: str
    tags: list[str]
    shared: bool
    created_at: float
    updated_at: float
    snippet: str | None = None


def to_project(row: aiosqlite.Row) -> Project:
    keys = row.keys()
    return Project(
        id=row["id"],
        slug=row["slug"],
        name=row["name"],
        root=row["root"],
        description=row["description"],
        archived=bool(row["archived"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        total_tasks=row["total_tasks"] if "total_tasks" in keys else 0,
        open_tasks=row["open_tasks"] if "open_tasks" in keys else 0,
    )


def to_task(row: aiosqlite.Row) -> Task:
    keys = row.keys()
    return Task(
        id=row["id"],
        project_id=row["project_id"],
        project=row["project"],
        title=row["title"],
        body=row["body"],
        status=Status(row["status"]),
        priority=PRIORITIES.get(row["priority"], Priority.NORMAL),
        tags=split_tags(row["tags"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        closed_at=row["closed_at"],
        snippet=row["snippet"] if "snippet" in keys else None,
    )


def to_note(row: aiosqlite.Row) -> Note:
    keys = row.keys()
    return Note(
        id=row["id"],
        project_id=row["project_id"],
        project=row["project"],
        title=row["title"],
        body=row["body"],
        tags=split_tags(row["tags"]),
        shared=bool(row["shared"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        snippet=row["snippet"] if "snippet" in keys else None,
    )


class Store:
    """One SQLite file holding every project and every task.

    The connection opens on first use, so a `Store` can be built before there is an event loop.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path) if path != ":memory:" else Path(":memory:")
        self.connection: aiosqlite.Connection | None = None
        self.opening = asyncio.Lock()

    @property
    def in_memory(self) -> bool:
        return str(self.path) == ":memory:"

    async def open(self) -> aiosqlite.Connection:
        async with self.opening:
            if self.connection is None:
                if not self.in_memory:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                connection = await aiosqlite.connect(str(self.path))
                connection.row_factory = aiosqlite.Row
                await connection.execute("PRAGMA journal_mode=WAL")
                await connection.execute("PRAGMA foreign_keys=ON")
                await connection.execute("PRAGMA busy_timeout=5000")
                await self.migrate(connection)
                self.connection = connection
            return self.connection

    async def migrate(self, connection: aiosqlite.Connection) -> None:
        """Run the migrations this file has not seen yet."""
        async with connection.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        done = int(row[0]) if row else 0
        for number, steps in enumerate(MIGRATIONS[done:], start=done + 1):
            for statement in steps:
                await connection.execute(statement)
            await connection.execute(f"PRAGMA user_version={number}")
            await connection.commit()

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()
            self.connection = None

    async def rows(self, sql: str, parameters: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        connection = await self.open()
        async with connection.execute(sql, tuple(parameters)) as cursor:
            return list(await cursor.fetchall())

    async def row(self, sql: str, parameters: Sequence[Any] = ()) -> aiosqlite.Row | None:
        found = await self.rows(sql, parameters)
        return found[0] if found else None

    # -- projects --------------------------------------------------------------------------

    async def project(self, project_id: int) -> Project | None:
        row = await self.row(f"SELECT p.*, {COUNTS} FROM projects p WHERE p.id = ?", (project_id,))
        return to_project(row) if row else None

    async def project_by_slug(self, slug: str) -> Project | None:
        row = await self.row(f"SELECT p.*, {COUNTS} FROM projects p WHERE p.slug = ?", (slug,))
        return to_project(row) if row else None

    async def project_by_root(self, root: Path | str) -> Project | None:
        row = await self.row(f"SELECT p.*, {COUNTS} FROM projects p WHERE p.root = ?", (str(root),))
        return to_project(row) if row else None

    async def project_for_path(self, path: Path) -> Project | None:
        """The registered project whose root is the closest ancestor of `path`."""
        candidates = [str(path), *(str(parent) for parent in path.parents)]
        holes = ", ".join("?" for _ in candidates)
        rows = await self.rows(
            f"SELECT p.*, {COUNTS} FROM projects p WHERE p.root IN ({holes})", candidates
        )
        found = [to_project(row) for row in rows]
        return max(found, key=lambda item: len(item.root)) if found else None

    async def projects(self, *, archived: bool = False) -> list[Project]:
        where = "" if archived else "WHERE p.archived = 0"
        rows = await self.rows(
            f"SELECT p.*, {COUNTS} FROM projects p {where} ORDER BY p.archived, p.slug"
        )
        return [to_project(row) for row in rows]

    async def slugs(self) -> list[str]:
        return [row["slug"] for row in await self.rows("SELECT slug FROM projects ORDER BY slug")]

    async def add_project(
        self, *, slug: str, name: str, root: Path | str, description: str = ""
    ) -> Project:
        connection = await self.open()
        now = time.time()
        cursor = await connection.execute(
            "INSERT INTO projects (slug, name, root, description, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (slug, name, str(root), description, now, now),
        )
        await connection.commit()
        created = await self.project(cursor.lastrowid or 0)
        assert created is not None, "the project just written is not there"
        return created

    async def update_project(
        self,
        project_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        archived: bool | None = None,
        slug: str | None = None,
    ) -> Project | None:
        changes: dict[str, Any] = {}
        if name is not None:
            changes["name"] = name
        if description is not None:
            changes["description"] = description
        if archived is not None:
            changes["archived"] = int(archived)
        if slug is not None:
            changes["slug"] = slug
        if changes:
            changes["updated_at"] = time.time()
            assignments = ", ".join(f"{column} = ?" for column in changes)
            connection = await self.open()
            await connection.execute(
                f"UPDATE projects SET {assignments} WHERE id = ?",
                (*changes.values(), project_id),
            )
            await connection.commit()
        return await self.project(project_id)

    # -- tasks -----------------------------------------------------------------------------

    async def task(self, task_id: int) -> Task | None:
        row = await self.row(
            f"SELECT {TASK_COLUMNS} FROM tasks t JOIN projects p ON p.id = t.project_id "
            "WHERE t.id = ?",
            (task_id,),
        )
        return to_task(row) if row else None

    async def add_task(
        self,
        *,
        project_id: int,
        title: str,
        body: str = "",
        status: Status = Status.TODO,
        priority: Priority = Priority.NORMAL,
        tags: Iterable[str] = (),
    ) -> Task:
        connection = await self.open()
        now = time.time()
        closed = now if status in (Status.DONE, Status.CANCELLED) else None
        cursor = await connection.execute(
            "INSERT INTO tasks "
            "(project_id, title, body, status, priority, tags, created_at, updated_at, closed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                title,
                body,
                status.value,
                RANKS[priority],
                join_tags(tags),
                now,
                now,
                closed,
            ),
        )
        await connection.commit()
        created = await self.task(cursor.lastrowid or 0)
        assert created is not None, "the task just written is not there"
        return created

    async def update_task(
        self,
        task_id: int,
        *,
        title: str | None = None,
        body: str | None = None,
        status: Status | None = None,
        priority: Priority | None = None,
        tags: Iterable[str] | None = None,
        project_id: int | None = None,
    ) -> Task | None:
        changes: dict[str, Any] = {}
        if title is not None:
            changes["title"] = title
        if body is not None:
            changes["body"] = body
        if status is not None:
            changes["status"] = status.value
            #: Closing stamps the time; reopening clears it.
            closing = status in (Status.DONE, Status.CANCELLED)
            changes["closed_at"] = time.time() if closing else None
        if priority is not None:
            changes["priority"] = RANKS[priority]
        if tags is not None:
            changes["tags"] = join_tags(tags)
        if project_id is not None:
            changes["project_id"] = project_id
        if changes:
            changes["updated_at"] = time.time()
            assignments = ", ".join(f"{column} = ?" for column in changes)
            connection = await self.open()
            await connection.execute(
                f"UPDATE tasks SET {assignments} WHERE id = ?", (*changes.values(), task_id)
            )
            await connection.commit()
        return await self.task(task_id)

    async def delete_task(self, task_id: int) -> bool:
        connection = await self.open()
        cursor = await connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await connection.commit()
        return bool(cursor.rowcount)

    def filters(
        self,
        *,
        project_id: int | None,
        status: Sequence[Status] | None,
        tag: str | None,
        priority: Priority | None,
    ) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if project_id is not None:
            clauses.append("t.project_id = ?")
            parameters.append(project_id)
        if status:
            clauses.append(f"t.status IN ({', '.join('?' for _ in status)})")
            parameters.extend(item.value for item in status)
        if tag:
            clauses.append("(',' || t.tags || ',') LIKE ?")
            parameters.append(f"%,{tag.strip().lower()},%")
        if priority is not None:
            clauses.append("t.priority = ?")
            parameters.append(RANKS[priority])
        return clauses, parameters

    async def tasks(
        self,
        *,
        project_id: int | None = None,
        status: Sequence[Status] | None = None,
        tag: str | None = None,
        priority: Priority | None = None,
        order: str = "backlog",
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        clauses, parameters = self.filters(
            project_id=project_id, status=status, tag=tag, priority=priority
        )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self.rows(
            f"SELECT {TASK_COLUMNS} FROM tasks t JOIN projects p ON p.id = t.project_id "
            f"{where} ORDER BY {ORDERS[order]} LIMIT ? OFFSET ?",
            (*parameters, limit, offset),
        )
        return [to_task(row) for row in rows]

    async def count_tasks(
        self,
        *,
        project_id: int | None = None,
        status: Sequence[Status] | None = None,
        tag: str | None = None,
        priority: Priority | None = None,
    ) -> int:
        clauses, parameters = self.filters(
            project_id=project_id, status=status, tag=tag, priority=priority
        )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = await self.row(f"SELECT COUNT(*) AS found FROM tasks t {where}", parameters)
        return int(row["found"]) if row else 0

    async def search(
        self,
        query: str,
        *,
        project_id: int | None = None,
        status: Sequence[Status] | None = None,
        tag: str | None = None,
        limit: int = 20,
    ) -> list[Task]:
        """Full-text search over title, body and tags, best match first.

        Pass `project_id` to stay inside one project, or leave it out to search every project.
        """
        match = match_query(query)
        if not match:
            return []
        clauses, parameters = self.filters(
            project_id=project_id, status=status, tag=tag, priority=None
        )
        extra = f"AND {' AND '.join(clauses)}" if clauses else ""
        rows = await self.rows(
            f"SELECT {TASK_COLUMNS}, "
            "snippet(tasks_fts, 1, '[', ']', '…', 10) AS snippet "
            "FROM tasks_fts "
            "JOIN tasks t ON t.id = tasks_fts.rowid "
            "JOIN projects p ON p.id = t.project_id "
            f"WHERE tasks_fts MATCH ? {extra} "
            "ORDER BY bm25(tasks_fts), t.priority, t.id DESC LIMIT ?",
            (match, *parameters, limit),
        )
        return [to_task(row) for row in rows]

    async def tags(self, *, project_id: int | None = None) -> list[str]:
        clauses = ["tags != ''"]
        parameters: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            parameters.append(project_id)
        rows = await self.rows(f"SELECT tags FROM tasks WHERE {' AND '.join(clauses)}", parameters)
        found: set[str] = set()
        for row in rows:
            found.update(split_tags(row["tags"]))
        return sorted(found)

    async def group(self, column: str, *, project_id: int | None = None) -> dict[str, int]:
        where = "WHERE project_id = ?" if project_id is not None else ""
        parameters = (project_id,) if project_id is not None else ()
        rows = await self.rows(
            f"SELECT {column} AS key, COUNT(*) AS found FROM tasks {where} GROUP BY {column}",
            parameters,
        )
        return {str(row["key"]): int(row["found"]) for row in rows}

    # -- notes -----------------------------------------------------------------------------

    async def note(self, note_id: int) -> Note | None:
        row = await self.row(
            f"SELECT {NOTE_COLUMNS} FROM notes n JOIN projects p ON p.id = n.project_id "
            "WHERE n.id = ?",
            (note_id,),
        )
        return to_note(row) if row else None

    async def note_by_title(self, title: str, *, project_id: int) -> Note | None:
        """The note of one project with that title; a title is unique inside a project."""
        row = await self.row(
            f"SELECT {NOTE_COLUMNS} FROM notes n JOIN projects p ON p.id = n.project_id "
            "WHERE n.project_id = ? AND n.title = ?",
            (project_id, title),
        )
        return to_note(row) if row else None

    async def add_note(
        self,
        *,
        project_id: int,
        title: str,
        body: str = "",
        tags: Iterable[str] = (),
        shared: bool = False,
    ) -> Note:
        check_note(body)
        connection = await self.open()
        now = time.time()
        cursor = await connection.execute(
            "INSERT INTO notes (project_id, title, body, tags, shared, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project_id, title, body, join_tags(tags), int(shared), now, now),
        )
        await connection.commit()
        created = await self.note(cursor.lastrowid or 0)
        assert created is not None, "the note just written is not there"
        return created

    async def update_note(
        self,
        note_id: int,
        *,
        title: str | None = None,
        body: str | None = None,
        tags: Iterable[str] | None = None,
        shared: bool | None = None,
        project_id: int | None = None,
    ) -> Note | None:
        changes: dict[str, Any] = {}
        if title is not None:
            changes["title"] = title
        if body is not None:
            check_note(body)
            changes["body"] = body
        if tags is not None:
            changes["tags"] = join_tags(tags)
        if shared is not None:
            changes["shared"] = int(shared)
        if project_id is not None:
            changes["project_id"] = project_id
        if changes:
            changes["updated_at"] = time.time()
            assignments = ", ".join(f"{column} = ?" for column in changes)
            connection = await self.open()
            await connection.execute(
                f"UPDATE notes SET {assignments} WHERE id = ?", (*changes.values(), note_id)
            )
            await connection.commit()
        return await self.note(note_id)

    async def append_note(self, note_id: int, text: str) -> Note | None:
        """Add a paragraph at the end of a note.

        One statement writes the whole body, so the FTS triggers see the final text once. The
        result must fit `NOTE_LIMIT` like any other body; a note that would overflow is untouched.
        """
        existing = await self.note(note_id)
        if existing is None:
            return None
        check_note(f"{existing.body}\n\n{text}" if existing.body else text)
        connection = await self.open()
        await connection.execute(
            "UPDATE notes SET body = CASE WHEN body = '' THEN ? ELSE body || ? END, "
            "updated_at = ? WHERE id = ?",
            (text, f"\n\n{text}", time.time(), note_id),
        )
        await connection.commit()
        return await self.note(note_id)

    async def delete_note(self, note_id: int) -> bool:
        connection = await self.open()
        cursor = await connection.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        await connection.commit()
        return bool(cursor.rowcount)

    def note_filters(
        self, *, project_id: int | None, tag: str | None, shared: bool | None
    ) -> tuple[list[str], list[Any]]:
        """A project sees its own notes and every shared note of the other projects."""
        clauses: list[str] = []
        parameters: list[Any] = []
        if project_id is not None:
            clauses.append("(n.project_id = ? OR n.shared = 1)")
            parameters.append(project_id)
        if shared is not None:
            clauses.append("n.shared = ?")
            parameters.append(int(shared))
        if tag:
            clauses.append("(',' || n.tags || ',') LIKE ?")
            parameters.append(f"%,{tag.strip().lower()},%")
        return clauses, parameters

    async def notes(
        self,
        *,
        project_id: int | None = None,
        tag: str | None = None,
        shared: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Note]:
        clauses, parameters = self.note_filters(project_id=project_id, tag=tag, shared=shared)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self.rows(
            f"SELECT {NOTE_COLUMNS} FROM notes n JOIN projects p ON p.id = n.project_id "
            f"{where} ORDER BY n.updated_at DESC, n.id DESC LIMIT ? OFFSET ?",
            (*parameters, limit, offset),
        )
        return [to_note(row) for row in rows]

    async def count_notes(
        self,
        *,
        project_id: int | None = None,
        tag: str | None = None,
        shared: bool | None = None,
    ) -> int:
        clauses, parameters = self.note_filters(project_id=project_id, tag=tag, shared=shared)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = await self.row(f"SELECT COUNT(*) AS found FROM notes n {where}", parameters)
        return int(row["found"]) if row else 0

    async def search_notes(
        self,
        query: str,
        *,
        project_id: int | None = None,
        tag: str | None = None,
        shared: bool | None = None,
        limit: int = 20,
    ) -> list[Note]:
        """Full-text search over the title, the body and the tags of the notes in reach.

        Pass `project_id` to stay in one project plus the shared notes, or leave it out for all.
        """
        match = match_query(query)
        if not match:
            return []
        clauses, parameters = self.note_filters(project_id=project_id, tag=tag, shared=shared)
        extra = f"AND {' AND '.join(clauses)}" if clauses else ""
        rows = await self.rows(
            f"SELECT {NOTE_COLUMNS}, "
            "snippet(notes_fts, 1, '[', ']', '…', 10) AS snippet "
            "FROM notes_fts "
            "JOIN notes n ON n.id = notes_fts.rowid "
            "JOIN projects p ON p.id = n.project_id "
            f"WHERE notes_fts MATCH ? {extra} "
            "ORDER BY bm25(notes_fts), n.updated_at DESC LIMIT ?",
            (match, *parameters, limit),
        )
        return [to_note(row) for row in rows]


__all__ = [
    "MIGRATIONS",
    "NOTE_LIMIT",
    "Note",
    "Project",
    "Store",
    "Task",
    "clean_tags",
    "join_tags",
    "match_query",
    "split_tags",
]
