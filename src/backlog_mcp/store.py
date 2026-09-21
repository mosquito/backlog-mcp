"""SQLite storage: projects, tasks, notes, and the FTS5 index over them."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from .computer import hostname
from .history import split_history
from .models import CLOSED_STATUSES, OPEN_STATUSES, PRIORITIES, RANKS, Priority, Status

#: Applied in order; the file's `user_version` says how many already ran. `:computer` is the
#: name of this computer, given to the projects registered before projects had one.
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
    (
        """CREATE TABLE projects_v3 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            computer TEXT NOT NULL,
            root TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            archived INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE (computer, root)
        )""",
        """INSERT INTO projects_v3
            (id, slug, name, computer, root, description, archived, created_at, updated_at)
            SELECT id, slug, name, :computer, root, description, archived, created_at, updated_at
            FROM projects""",
        "DROP TABLE projects",
        "ALTER TABLE projects_v3 RENAME TO projects",
    ),
    (
        """CREATE TABLE projects_v4 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            archived INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )""",
        """INSERT INTO projects_v4
            (id, slug, name, description, archived, created_at, updated_at)
            SELECT id, slug, name, description, archived, created_at, updated_at FROM projects""",
        """CREATE TABLE project_roots (
            project_id INTEGER NOT NULL REFERENCES projects_v4(id) ON DELETE CASCADE,
            computer TEXT NOT NULL,
            root TEXT NOT NULL,
            PRIMARY KEY (project_id, computer, root)
        )""",
        """INSERT INTO project_roots (project_id, computer, root)
            SELECT id, computer, root FROM projects""",
        "CREATE UNIQUE INDEX project_roots_unique_path "
        "ON project_roots (computer, root) WHERE root != ''",
        "DROP TABLE projects",
        "ALTER TABLE projects_v4 RENAME TO projects",
    ),
    ("ALTER TABLE projects ADD COLUMN preferred_language TEXT DEFAULT NULL",),
    (
        "ALTER TABLE tasks ADD COLUMN history_warning INTEGER NOT NULL DEFAULT 0",
        """CREATE TABLE task_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            body TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'comment',
            created_at REAL NOT NULL
        )""",
        "CREATE INDEX comments_by_task ON task_comments (task_id, created_at, id)",
        """CREATE VIRTUAL TABLE comments_fts USING fts5 (
            body, content='task_comments', content_rowid='id',
            tokenize="unicode61 remove_diacritics 2"
        )""",
        """CREATE TRIGGER comments_fts_insert AFTER INSERT ON task_comments BEGIN
            INSERT INTO comments_fts (rowid, body) VALUES (new.id, new.body);
        END""",
        """CREATE TRIGGER comments_fts_delete AFTER DELETE ON task_comments BEGIN
            INSERT INTO comments_fts (comments_fts, rowid, body)
            VALUES ('delete', old.id, old.body);
        END""",
        """CREATE TRIGGER comments_fts_update AFTER UPDATE ON task_comments BEGIN
            INSERT INTO comments_fts (comments_fts, rowid, body)
            VALUES ('delete', old.id, old.body);
            INSERT INTO comments_fts (rowid, body) VALUES (new.id, new.body);
        END""",
        """CREATE TABLE task_history_originals (
            task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            body TEXT NOT NULL
        )""",
        """CREATE TRIGGER task_status_history AFTER UPDATE OF status ON tasks
        WHEN old.status != new.status BEGIN
            INSERT INTO task_comments (task_id, body, kind, created_at)
            VALUES (new.id, 'Status changed: ' || old.status || ' → ' || new.status,
                    'status', new.updated_at);
        END""",
        """CREATE TRIGGER task_move_history AFTER UPDATE OF project_id ON tasks
        WHEN old.project_id != new.project_id BEGIN
            INSERT INTO task_comments (task_id, body, kind, created_at)
            VALUES (new.id, 'Moved: ' || (SELECT slug FROM projects WHERE id = old.project_id)
                    || ' → ' || (SELECT slug FROM projects WHERE id = new.project_id),
                    'move', new.updated_at);
        END""",
        """CREATE TRIGGER comment_updates_task AFTER INSERT ON task_comments
        WHEN new.kind = 'comment' BEGIN
            UPDATE tasks SET updated_at = new.created_at WHERE id = new.task_id;
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

PROJECTS_JOIN = "JOIN projects p ON p.id = t.project_id"

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


class Unset(Enum):
    """An omitted update field; None explicitly clears a nullable value."""

    VALUE = 0


@dataclass(slots=True)
class Comment:
    id: int
    task_id: int
    project_id: int
    project: str
    body: str
    kind: Literal["comment", "status", "move", "legacy"]
    created_at: float
    snippet: str | None = None


@dataclass(slots=True)
class Project:
    id: int
    slug: str
    name: str
    description: str
    archived: bool
    created_at: float
    updated_at: float
    total_tasks: int = 0
    open_tasks: int = 0
    roots: list[ProjectRoot] = field(default_factory=list)
    preferred_language: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectRoot:
    computer: str
    root: str


@dataclass(frozen=True, slots=True)
class ProjectContents:
    """What a project holds, counted before it is deleted."""

    tasks: int
    notes: int
    shared_notes: int


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
    history_warning: bool = False


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
        description=row["description"],
        preferred_language=row["preferred_language"],
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
        history_warning=bool(row["history_warning"]),
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
    `computer` names this computer; it is given to the projects of a file written before
    projects had a computer, and defaults to the host name.
    """

    def __init__(self, path: Path | str, *, computer: str | None = None) -> None:
        self.path = Path(path) if path != ":memory:" else Path(":memory:")
        self.computer = computer
        self.connection: aiosqlite.Connection | None = None
        self.opening = asyncio.Lock()
        self.task_writes = asyncio.Lock()

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
                try:
                    await connection.execute("PRAGMA journal_mode=WAL")
                    await connection.execute("PRAGMA busy_timeout=5000")
                    await self.migrate(connection)
                    await connection.execute("PRAGMA foreign_keys=ON")
                except BaseException:
                    await connection.close()
                    raise
                self.connection = connection
            return self.connection

    async def migrate(self, connection: aiosqlite.Connection) -> None:
        """Run the migrations this file has not seen yet.

        Foreign keys must be off here: one migration rebuilds `projects`, and a cascade would
        else delete every task and note.
        """
        async with connection.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        done = int(row[0]) if row else 0
        parameters = {"computer": self.computer or hostname()}
        for number, steps in enumerate(MIGRATIONS[done:], start=done + 1):
            await connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in steps:
                    await connection.execute(statement, parameters)
                if number == 6:
                    await self.migrate_history(connection)
                await connection.execute(f"PRAGMA user_version={number}")
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def migrate_history(self, connection: aiosqlite.Connection) -> None:
        async with connection.execute("SELECT id, body FROM tasks") as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            description, entries, warning = split_history(row["body"])
            if not entries and not warning:
                continue
            await connection.execute(
                "INSERT INTO task_history_originals (task_id, body) VALUES (?, ?)",
                (row["id"], row["body"]),
            )
            await connection.execute(
                "UPDATE tasks SET body = ?, history_warning = ? WHERE id = ?",
                (description, int(warning), row["id"]),
            )
            await connection.executemany(
                "INSERT INTO task_comments (task_id, body, kind, created_at) "
                "VALUES (?, ?, 'legacy', ?)",
                [(row["id"], entry.body, entry.created_at) for entry in entries],
            )

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

    async def attach_roots(self, projects: list[Project]) -> list[Project]:
        """Populate path bindings without duplicating project rows in callers' queries."""
        if not projects:
            return projects
        ids = ", ".join("?" for _ in projects)
        rows = await self.rows(
            f"SELECT project_id, computer, root FROM project_roots "
            f"WHERE project_id IN ({ids}) ORDER BY computer, root",
            [project.id for project in projects],
        )
        by_id: dict[int, list[ProjectRoot]] = {project.id: [] for project in projects}
        for row in rows:
            by_id[row["project_id"]].append(ProjectRoot(row["computer"], row["root"]))
        for project in projects:
            project.roots = by_id[project.id]
        return projects

    async def roots(self, project_id: int) -> list[ProjectRoot]:
        project = await self.project(project_id)
        return project.roots if project else []

    async def project(self, project_id: int) -> Project | None:
        row = await self.row(f"SELECT p.*, {COUNTS} FROM projects p WHERE p.id = ?", (project_id,))
        return (await self.attach_roots([to_project(row)]))[0] if row else None

    async def project_by_slug(self, slug: str) -> Project | None:
        row = await self.row(f"SELECT p.*, {COUNTS} FROM projects p WHERE p.slug = ?", (slug,))
        return (await self.attach_roots([to_project(row)]))[0] if row else None

    async def project_by_root(self, root: Path | str, *, computer: str) -> Project | None:
        """The project registered for exactly this directory of that computer."""
        row = await self.row(
            f"SELECT p.*, {COUNTS} FROM projects p JOIN project_roots r ON r.project_id = p.id "
            "WHERE r.computer = ? AND r.root = ?",
            (computer, str(root)),
        )
        return (await self.attach_roots([to_project(row)]))[0] if row else None

    async def project_for_path(self, path: Path, *, computer: str) -> Project | None:
        """The project of that computer whose root is the closest ancestor of `path`."""
        candidates = [str(path), *(str(parent) for parent in path.parents)]
        holes = ", ".join("?" for _ in candidates)
        rows = await self.rows(
            f"SELECT p.*, {COUNTS} FROM projects p JOIN project_roots r ON r.project_id = p.id "
            f"WHERE r.computer = ? AND r.root != '' AND r.root IN ({holes})",
            (computer, *candidates),
        )
        found = await self.attach_roots([to_project(row) for row in rows])
        return (
            max(
                found,
                key=lambda item: max(
                    len(root.root) for root in item.roots if root.computer == computer
                ),
            )
            if found
            else None
        )

    async def projects(
        self, *, archived: bool = False, computer: str | None = None
    ) -> list[Project]:
        """Every project of one computer, or of all of them when `computer` is None."""
        clauses: list[str] = []
        parameters: list[Any] = []
        if not archived:
            clauses.append("p.archived = 0")
        if computer is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM project_roots r "
                "WHERE r.project_id = p.id AND r.computer = ?)"
            )
            parameters.append(computer)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self.rows(
            f"SELECT p.*, {COUNTS} FROM projects p {where} ORDER BY p.archived, p.slug",
            parameters,
        )
        return await self.attach_roots([to_project(row) for row in rows])

    async def computers(self) -> list[str]:
        """Every computer with a registered project, sorted."""
        rows = await self.rows("SELECT DISTINCT computer FROM project_roots ORDER BY computer")
        return [row["computer"] for row in rows]

    async def slugs(self) -> list[str]:
        return [row["slug"] for row in await self.rows("SELECT slug FROM projects ORDER BY slug")]

    async def add_project(
        self,
        *,
        slug: str,
        name: str,
        root: Path | str,
        computer: str,
        description: str = "",
        preferred_language: str | None = None,
    ) -> Project:
        connection = await self.open()
        now = time.time()
        cursor = await connection.execute(
            "INSERT INTO projects "
            "(slug, name, description, preferred_language, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (slug, name, description, preferred_language, now, now),
        )
        await connection.execute(
            "INSERT INTO project_roots (project_id, computer, root) VALUES (?, ?, ?)",
            (cursor.lastrowid, computer, str(root)),
        )
        await connection.commit()
        created = await self.project(cursor.lastrowid or 0)
        assert created is not None, "the project just written is not there"
        return created

    async def add_root(self, project_id: int, *, computer: str, root: Path | str) -> Project:
        """Bind a project to one location; non-empty paths belong to only one project."""
        connection = await self.open()
        existing = await self.row(
            "SELECT project_id FROM project_roots "
            "WHERE project_id = ? AND computer = ? AND root = ?",
            (project_id, computer, str(root)),
        )
        if existing is not None:
            project = await self.project(project_id)
            assert project is not None
            return project
        try:
            await connection.execute(
                "INSERT INTO project_roots (project_id, computer, root) VALUES (?, ?, ?)",
                (project_id, computer, str(root)),
            )
            await connection.commit()
        except aiosqlite.IntegrityError as error:
            raise ValueError(f"path {root!s} on {computer!r} is already registered") from error
        project = await self.project(project_id)
        assert project is not None, "the project just updated is gone"
        return project

    async def project_contents(self, project_id: int) -> ProjectContents:
        """Count the tasks and the notes of a project, deleted tasks included."""
        row = await self.row(
            "SELECT (SELECT COUNT(*) FROM tasks WHERE project_id = ?) AS tasks, "
            "(SELECT COUNT(*) FROM notes WHERE project_id = ?) AS notes, "
            "(SELECT COUNT(*) FROM notes WHERE project_id = ? AND shared = 1) AS shared_notes",
            (project_id, project_id, project_id),
        )
        assert row is not None, "an aggregate query always returns one row"
        return ProjectContents(row["tasks"], row["notes"], row["shared_notes"])

    async def delete_project(self, project_id: int) -> bool:
        """Delete a project with its roots, tasks and notes. Nothing of it stays.

        The rows go one table at a time, so the FTS triggers see every task and note.
        """
        connection = await self.open()
        await connection.execute("DELETE FROM tasks WHERE project_id = ?", (project_id,))
        await connection.execute("DELETE FROM notes WHERE project_id = ?", (project_id,))
        await connection.execute("DELETE FROM project_roots WHERE project_id = ?", (project_id,))
        cursor = await connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        await connection.commit()
        return bool(cursor.rowcount)

    async def update_project(
        self,
        project_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        archived: bool | None = None,
        slug: str | None = None,
        preferred_language: str | None | Unset = Unset.VALUE,
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
        if preferred_language is not Unset.VALUE:
            changes["preferred_language"] = preferred_language
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
        closed = now if status in CLOSED_STATUSES else None
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
        async with self.task_writes:
            connection = await self.open()
            existing = await self.task(task_id)
            if existing is None:
                return None
            changes: dict[str, Any] = {}
            now = time.time()
            if title is not None:
                changes["title"] = title
            if body is not None:
                changes["body"] = body
                changes["history_warning"] = 0
            if status is not None and status != existing.status:
                changes["status"] = status.value
                changes["closed_at"] = now if status in CLOSED_STATUSES else None
            if priority is not None:
                changes["priority"] = RANKS[priority]
            if tags is not None:
                changes["tags"] = join_tags(tags)
            if project_id is not None and project_id != existing.project_id:
                destination = await self.project(project_id)
                if destination is None:
                    raise ValueError(f"no project #{project_id}")
                changes["project_id"] = project_id
            if changes:
                changes["updated_at"] = now
                assignments = ", ".join(f"{column} = ?" for column in changes)
                await connection.execute(
                    f"UPDATE tasks SET {assignments} WHERE id = ?",
                    (*changes.values(), task_id),
                )
            await connection.commit()
            return await self.task(task_id)

    async def delete_task(self, task_id: int) -> bool:
        task = await self.task(task_id)
        if task is None or task.status == Status.DELETED:
            return False
        return await self.update_task(task_id, status=Status.DELETED) is not None

    async def comment(self, comment_id: int) -> Comment | None:
        row = await self.row(
            "SELECT c.*, t.project_id, p.slug AS project FROM task_comments c "
            "JOIN tasks t ON t.id = c.task_id JOIN projects p ON p.id = t.project_id "
            "WHERE c.id = ?",
            (comment_id,),
        )
        return Comment(**dict(row)) if row else None

    async def add_comment(self, task_id: int, body: str) -> Comment:
        if not body.strip():
            raise ValueError("a comment must not be empty")
        async with self.task_writes:
            connection = await self.open()
            if await self.task(task_id) is None:
                raise LookupError(f"no task #{task_id}")
            now = time.time()
            cursor = await connection.execute(
                "INSERT INTO task_comments (task_id, body, created_at) VALUES (?, ?, ?)",
                (task_id, body, now),
            )
            await connection.commit()
            saved = await self.comment(cursor.lastrowid or 0)
            assert saved is not None
            return saved

    async def count_comments(self, task_id: int) -> int:
        row = await self.row("SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (task_id,))
        return int(row[0]) if row else 0

    async def comments(
        self,
        *,
        task_id: int | None = None,
        project_id: int | None = None,
        computer: str | None = None,
        status: Sequence[Status] | None = None,
        tag: str | None = None,
        query: str | None = None,
        limit: int = -1,
    ) -> list[Comment]:
        clauses, parameters = self.filters(
            project_id=project_id, computer=computer, status=status, tag=tag, priority=None
        )
        if task_id is not None:
            clauses.append("c.task_id = ?")
            parameters.append(task_id)
        elif not status:
            clauses.append("t.status != ?")
            parameters.append(Status.DELETED.value)
        source = "task_comments c"
        snippet = ""
        order = "c.created_at, c.id"
        if query is not None:
            match = match_query(query)
            if not match:
                return []
            source = "comments_fts JOIN task_comments c ON c.id = comments_fts.rowid"
            clauses.append("comments_fts MATCH ?")
            parameters.append(match)
            snippet = ", snippet(comments_fts, 0, '[', ']', '…', 16) AS snippet"
            order = "bm25(comments_fts), c.created_at, c.id"
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self.rows(
            f"SELECT c.*, t.project_id, p.slug AS project {snippet} FROM {source} "
            "JOIN tasks t ON t.id = c.task_id JOIN projects p ON p.id = t.project_id "
            f"{where} ORDER BY {order} LIMIT ?",
            (*parameters, limit),
        )
        return [Comment(**dict(row)) for row in rows]

    def filters(
        self,
        *,
        project_id: int | None,
        status: Sequence[Status] | None,
        tag: str | None,
        priority: Priority | None,
        computer: str | None = None,
    ) -> tuple[list[str], list[Any]]:
        """The WHERE clauses of a task query over `tasks t` joined with `projects p`."""
        clauses: list[str] = []
        parameters: list[Any] = []
        if project_id is not None:
            clauses.append("t.project_id = ?")
            parameters.append(project_id)
        if computer is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM project_roots r "
                "WHERE r.project_id = p.id AND r.computer = ?)"
            )
            parameters.append(computer)
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
        computer: str | None = None,
        order: str = "backlog",
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        clauses, parameters = self.filters(
            project_id=project_id, status=status, tag=tag, priority=priority, computer=computer
        )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self.rows(
            f"SELECT {TASK_COLUMNS} FROM tasks t {PROJECTS_JOIN} "
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
        computer: str | None = None,
    ) -> int:
        clauses, parameters = self.filters(
            project_id=project_id, status=status, tag=tag, priority=priority, computer=computer
        )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = await self.row(
            f"SELECT COUNT(*) AS found FROM tasks t {PROJECTS_JOIN} {where}", parameters
        )
        return int(row["found"]) if row else 0

    async def search(
        self,
        query: str,
        *,
        project_id: int | None = None,
        status: Sequence[Status] | None = None,
        tag: str | None = None,
        computer: str | None = None,
        limit: int = 20,
    ) -> list[Task]:
        """Full-text search over title, body and tags, best match first.

        Pass `project_id` to stay inside one project, `computer` to stay on one computer, or
        neither to search every project.
        """
        match = match_query(query)
        if not match:
            return []
        clauses, parameters = self.filters(
            project_id=project_id, status=status, tag=tag, priority=None, computer=computer
        )
        if status is None:
            clauses.append("t.status != ?")
            parameters.append(Status.DELETED.value)
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

    async def tags(
        self, *, project_id: int | None = None, computer: str | None = None
    ) -> list[str]:
        clauses, parameters = self.filters(
            project_id=project_id, status=None, tag=None, priority=None, computer=computer
        )
        clauses.append("t.status != ?")
        parameters.append(Status.DELETED.value)
        clauses.append("t.tags != ''")
        rows = await self.rows(
            f"SELECT t.tags FROM tasks t {PROJECTS_JOIN} WHERE {' AND '.join(clauses)}",
            parameters,
        )
        found: set[str] = set()
        for row in rows:
            found.update(split_tags(row["tags"]))
        return sorted(found)

    async def group(
        self, column: str, *, project_id: int | None = None, computer: str | None = None
    ) -> dict[str, int]:
        clauses, parameters = self.filters(
            project_id=project_id, status=None, tag=None, priority=None, computer=computer
        )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self.rows(
            f"SELECT t.{column} AS key, COUNT(*) AS found FROM tasks t {PROJECTS_JOIN} "
            f"{where} GROUP BY t.{column}",
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
        self,
        *,
        project_id: int | None,
        tag: str | None,
        shared: bool | None,
        computer: str | None = None,
    ) -> tuple[list[str], list[Any]]:
        """A project, or a computer, sees its own notes and every shared note of the others."""
        clauses: list[str] = []
        parameters: list[Any] = []
        if project_id is not None:
            clauses.append("(n.project_id = ? OR n.shared = 1)")
            parameters.append(project_id)
        if computer is not None:
            clauses.append(
                "(EXISTS (SELECT 1 FROM project_roots r "
                "WHERE r.project_id = p.id AND r.computer = ?) OR n.shared = 1)"
            )
            parameters.append(computer)
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
        computer: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Note]:
        clauses, parameters = self.note_filters(
            project_id=project_id, tag=tag, shared=shared, computer=computer
        )
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
        computer: str | None = None,
    ) -> int:
        clauses, parameters = self.note_filters(
            project_id=project_id, tag=tag, shared=shared, computer=computer
        )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = await self.row(
            f"SELECT COUNT(*) AS found FROM notes n JOIN projects p ON p.id = n.project_id {where}",
            parameters,
        )
        return int(row["found"]) if row else 0

    async def search_notes(
        self,
        query: str,
        *,
        project_id: int | None = None,
        tag: str | None = None,
        shared: bool | None = None,
        computer: str | None = None,
        limit: int = 20,
    ) -> list[Note]:
        """Full-text search over the title, the body and the tags of the notes in reach.

        Pass `project_id` to stay in one project plus the shared notes, `computer` to stay on
        one computer plus the shared notes, or neither for all.
        """
        match = match_query(query)
        if not match:
            return []
        clauses, parameters = self.note_filters(
            project_id=project_id, tag=tag, shared=shared, computer=computer
        )
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
    "ProjectContents",
    "Store",
    "Task",
    "clean_tags",
    "join_tags",
    "match_query",
    "split_tags",
]
