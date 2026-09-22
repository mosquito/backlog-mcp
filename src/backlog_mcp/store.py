"""SQLite storage: projects, tasks, notes, and the FTS5 index over them."""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Any, Literal

import aiosql
import aiosqlite

from .computer import hostname
from .migrate import migrate
from .models import (
    CLOSED_STATUSES,
    OPEN_STATUSES,
    PRIORITIES,
    RANKS,
    Actor,
    Priority,
    Relation,
    Status,
    TaskLinkOut,
    relation,
)
from .names import owner_name, project_name

QUERIES = aiosql.from_path(Path(__file__).with_name("queries.sql"), "aiosqlite")
LOCAL_PRINCIPAL = "local"
ACCESS = (
    "EXISTS (SELECT 1 FROM project_members membership "
    "WHERE membership.project_id = p.id AND membership.principal = ?)"
)


def write[**P, T](method: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
    """Serialize complete mutations, including their authorization and history."""

    @wraps(method)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        store = args[0]
        assert isinstance(store, Store)
        root = store.database
        task = asyncio.current_task()
        if root.writer is task:
            return await method(*args, **kwargs)
        async with root.task_writes:
            root.writer = task
            try:
                return await method(*args, **kwargs)
            except BaseException:
                if root.connection is not None:
                    await root.connection.rollback()
                raise
            finally:
                root.writer = None

    return wrapped


OPEN_LIST = ", ".join(f"'{status.value}'" for status in OPEN_STATUSES)

COUNTS = (
    "(SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id) AS total_tasks, "
    f"(SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id "
    f"AND t.status IN ({OPEN_LIST})) AS open_tasks"
)

PROJECT_NAME = "(SELECT handle FROM principals WHERE identity = p.owner) || '/' || p.slug"

TASK_COLUMNS = f"t.*, {PROJECT_NAME} AS project"

NOTE_COLUMNS = f"n.*, {PROJECT_NAME} AS project"

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
    kind: Literal["comment", "status", "move"]
    created_at: float
    principal: str = LOCAL_PRINCIPAL
    computer: str = ""
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
    owner: str = LOCAL_PRINCIPAL
    role: str = "owner"
    created_by: Actor | None = None
    updated_by: Actor | None = None

    @property
    def full_name(self) -> str:
        return project_name(self.owner, self.slug)


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
    created_by: Actor | None = None
    updated_by: Actor | None = None


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
    created_by: Actor | None = None
    updated_by: Actor | None = None


def to_project(row: aiosqlite.Row) -> Project:
    keys = row.keys()
    return Project(
        id=row["id"],
        created_by=Actor(principal=row["created_principal"], computer=row["created_computer"]),
        updated_by=Actor(principal=row["updated_principal"], computer=row["updated_computer"]),
        slug=row["slug"],
        owner=row["owner"],
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
        created_by=Actor(principal=row["created_principal"], computer=row["created_computer"]),
        updated_by=Actor(principal=row["updated_principal"], computer=row["updated_computer"]),
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
        created_by=Actor(principal=row["created_principal"], computer=row["created_computer"]),
        updated_by=Actor(principal=row["updated_principal"], computer=row["updated_computer"]),
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
    Each view binds an authenticated principal and a client computer.
    """

    def __init__(
        self, path: Path | str, *, computer: str | None = None, principal: str = LOCAL_PRINCIPAL
    ) -> None:
        self.path = Path(path) if path != ":memory:" else Path(":memory:")
        self.computer = computer
        self.principal = principal
        self.database = self
        self.writer: asyncio.Task[Any] | None = None
        self.views: dict[tuple[str, str | None], Store] = {}
        self.connection: aiosqlite.Connection | None = None
        self.opening = asyncio.Lock()
        self.task_writes = asyncio.Lock()

    @property
    def in_memory(self) -> bool:
        return str(self.path) == ":memory:"

    def view(self, *, principal: str | None = None, computer: str | None = None) -> Store:
        key = (principal or self.principal, computer or self.computer)
        if key not in self.database.views:
            scoped = Store(self.path, computer=key[1], principal=key[0])
            scoped.database = self.database
            self.database.views[key] = scoped
        return self.database.views[key]

    @property
    def actor(self) -> dict[str, str]:
        return {"principal": self.principal, "computer": self.computer or hostname()}

    @write
    async def register(self, identity: str, name: str) -> None:
        connection = await self.open()
        await QUERIES.register_principal(
            connection, identity=identity, handle=owner_name(identity), name=name
        )
        await connection.commit()

    async def require(self, project_id: int, *, role: str = "reader") -> str:
        row = await QUERIES.membership(
            await self.open(), project_id=project_id, principal=self.principal
        )
        if row is None:
            raise LookupError(f"no project #{project_id}")
        actual = str(row["role"])
        if role == "owner" and actual != "owner" or role == "editor" and actual == "reader":
            raise PermissionError(f"project #{project_id} requires {role} access")
        return actual

    async def members(self, project_id: int) -> list[dict[str, str]]:
        await self.require(project_id)
        return [
            dict(row) async for row in QUERIES.members(await self.open(), project_id=project_id)
        ]

    @write
    async def share(self, project_id: int, principal: str, role: str) -> None:
        await self.require(project_id, role="owner")
        if role not in {"reader", "editor"}:
            raise ValueError("membership role must be reader or editor")
        connection = await self.open()
        if await QUERIES.principal(connection, identity=principal) is None:
            raise LookupError("unknown principal; use a registered authentication identity")
        current = await QUERIES.membership(connection, project_id=project_id, principal=principal)
        if current and current["role"] == "owner":
            raise ValueError("the project owner cannot be changed")
        await QUERIES.save_member(connection, project_id=project_id, principal=principal, role=role)
        await connection.commit()

    async def open(self) -> aiosqlite.Connection:
        if self.database is not self:
            return await self.database.open()
        async with self.opening:
            if self.connection is None:
                if not self.in_memory:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                connection = await aiosqlite.connect(str(self.path))
                connection.row_factory = aiosqlite.Row
                try:
                    await connection.execute("PRAGMA busy_timeout=5000")
                    await connection.execute("PRAGMA foreign_keys=ON")
                    await migrate(connection)
                    # Switching journal mode can return BUSY immediately when another startup
                    # enters its migration transaction. Release the failed attempt and retry.
                    for attempt in range(5):
                        try:
                            await connection.execute_fetchall("PRAGMA journal_mode=WAL")
                            break
                        except sqlite3.OperationalError as error:
                            if error.sqlite_errorcode != sqlite3.SQLITE_BUSY or attempt == 4:
                                raise
                            await asyncio.sleep(0.05)
                except BaseException:
                    await connection.close()
                    raise
                await QUERIES.register_principal(
                    connection,
                    identity=self.principal,
                    handle=owner_name(self.principal),
                    name=owner_name(self.principal),
                )
                await connection.commit()
                self.connection = connection
            return self.connection

    async def close(self) -> None:
        if self.database is not self:
            return
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
            f"WHERE project_id IN ({ids}) AND principal = ? ORDER BY computer, root",
            [*(project.id for project in projects), self.principal],
        )
        by_id: dict[int, list[ProjectRoot]] = {project.id: [] for project in projects}
        for row in rows:
            by_id[row["project_id"]].append(ProjectRoot(row["computer"], row["root"]))
        for project in projects:
            project.roots = by_id[project.id]
            project.role = await self.require(project.id)
        return projects

    async def roots(self, project_id: int) -> list[ProjectRoot]:
        project = await self.project(project_id)
        return project.roots if project else []

    async def project(self, project_id: int) -> Project | None:
        row = await self.row(
            f"SELECT p.*, {COUNTS} FROM projects p WHERE p.id = ? AND {ACCESS}",
            (project_id, self.principal),
        )
        return (await self.attach_roots([to_project(row)]))[0] if row else None

    async def project_by_slug(self, slug: str) -> Project | None:
        if "/" in slug:
            handle, local_slug = slug.split("/", 1)
            rows = await self.rows(
                f"SELECT p.*, {COUNTS} FROM projects p JOIN principals u ON u.identity=p.owner "
                f"WHERE u.handle = ? AND p.slug = ? AND {ACCESS}",
                (handle, local_slug, self.principal),
            )
        else:
            rows = await self.rows(
                f"SELECT p.*, {COUNTS} FROM projects p WHERE p.slug = ? AND {ACCESS}",
                (slug, self.principal),
            )
        if len(rows) > 1:
            references = ", ".join(project_name(row["owner"], row["slug"]) for row in rows)
            raise ValueError(f"ambiguous project {slug!r}; use its full name: {references}")
        return (await self.attach_roots([to_project(rows[0])]))[0] if rows else None

    async def project_by_root(self, root: Path | str, *, computer: str) -> Project | None:
        """The project registered for exactly this directory of that computer."""
        row = await self.row(
            f"SELECT p.*, {COUNTS} FROM projects p JOIN project_roots r ON r.project_id = p.id "
            f"WHERE r.computer = ? AND r.root = ? AND r.principal = ? AND {ACCESS}",
            (computer, str(root), self.principal, self.principal),
        )
        return (await self.attach_roots([to_project(row)]))[0] if row else None

    async def project_for_path(self, path: Path, *, computer: str) -> Project | None:
        """The project of that computer whose root is the closest ancestor of `path`."""
        candidates = [str(path), *(str(parent) for parent in path.parents)]
        holes = ", ".join("?" for _ in candidates)
        rows = await self.rows(
            f"SELECT p.*, {COUNTS} FROM projects p JOIN project_roots r ON r.project_id = p.id "
            f"WHERE r.computer = ? AND r.root != '' AND r.root IN ({holes}) AND "
            f"r.principal = ? AND {ACCESS}",
            (computer, *candidates, self.principal, self.principal),
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
        clauses: list[str] = [ACCESS]
        parameters: list[Any] = [self.principal]
        if not archived:
            clauses.append("p.archived = 0")
        if computer is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM project_roots r "
                "WHERE r.project_id = p.id AND r.principal = ? AND r.computer = ?)"
            )
            parameters.extend((self.principal, computer))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self.rows(
            f"SELECT p.*, {COUNTS} FROM projects p {where} ORDER BY p.archived, p.slug",
            parameters,
        )
        return await self.attach_roots([to_project(row) for row in rows])

    async def search_projects(
        self,
        query: str,
        *,
        project_id: int | None = None,
        computer: str | None = None,
        limit: int = 20,
    ) -> list[Project]:
        """Apply membership and scope before ranking project matches."""
        match = match_query(query)
        if not match:
            return []
        clauses = [ACCESS, "p.archived = 0"]
        parameters: list[Any] = [match, self.principal]
        if project_id is not None:
            clauses.append("p.id = ?")
            parameters.append(project_id)
        if computer is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM project_roots r WHERE r.project_id = p.id "
                "AND r.principal = ? AND r.computer = ?)"
            )
            parameters.extend((self.principal, computer))
        rows = await self.rows(
            f"SELECT p.*, {COUNTS} FROM projects_fts JOIN projects p "
            "ON p.id = projects_fts.rowid WHERE projects_fts MATCH ? AND "
            + " AND ".join(clauses)
            + " ORDER BY bm25(projects_fts), p.id LIMIT ?",
            (*parameters, limit),
        )
        return await self.attach_roots([to_project(row) for row in rows])

    async def computers(self) -> list[str]:
        """Every computer with a registered project, sorted."""
        rows = await self.rows(
            f"SELECT DISTINCT r.computer FROM project_roots r JOIN projects p ON "
            f"p.id=r.project_id WHERE r.principal = ? AND {ACCESS} ORDER BY r.computer",
            (self.principal, self.principal),
        )
        return [row["computer"] for row in rows]

    async def slugs(self, *, owned: bool = False) -> list[str]:
        """Owned short slugs for creation, or qualified accessible names for completion."""
        if owned:
            rows = await self.rows("SELECT slug FROM projects WHERE owner = ?", (self.principal,))
            return [row["slug"] for row in rows]
        return sorted(project.full_name for project in await self.projects(archived=True))

    @write
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
        inserted_id = await QUERIES.add_project(
            connection,
            slug=slug,
            name=name,
            description=description,
            preferred_language=preferred_language,
            created_at=now,
            updated_at=now,
            owner=self.principal,
            created_principal=self.principal,
            created_computer=self.actor["computer"],
            updated_principal=self.principal,
            updated_computer=self.actor["computer"],
        )
        await QUERIES.save_member(
            connection, project_id=inserted_id, principal=self.principal, role="owner"
        )
        await connection.execute(
            "INSERT INTO project_roots (project_id, principal, computer, root) VALUES (?, ?, ?, ?)",
            (inserted_id, self.principal, computer, str(root)),
        )
        await connection.commit()
        created = await self.project(inserted_id or 0)
        assert created is not None, "the project just written is not there"
        return created

    @write
    async def add_root(self, project_id: int, *, computer: str, root: Path | str) -> Project:
        """Bind a project to one location; non-empty paths belong to only one project."""
        await self.require(project_id)
        connection = await self.open()
        existing = await self.row(
            "SELECT project_id FROM project_roots "
            "WHERE project_id = ? AND principal = ? AND computer = ? AND root = ?",
            (project_id, self.principal, computer, str(root)),
        )
        if existing is not None:
            project = await self.project(project_id)
            assert project is not None
            return project
        try:
            await connection.execute(
                "INSERT INTO project_roots (project_id, principal, computer, root) "
                "VALUES (?, ?, ?, ?)",
                (project_id, self.principal, computer, str(root)),
            )
            await connection.commit()
        except aiosqlite.IntegrityError as error:
            raise ValueError(f"path {root!s} on {computer!r} is already registered") from error
        project = await self.project(project_id)
        assert project is not None, "the project just updated is gone"
        return project

    async def project_contents(self, project_id: int) -> ProjectContents:
        """Count the tasks and the notes of a project, deleted tasks included."""
        await self.require(project_id)
        row = await self.row(
            "SELECT (SELECT COUNT(*) FROM tasks WHERE project_id = ?) AS tasks, "
            "(SELECT COUNT(*) FROM notes WHERE project_id = ?) AS notes, "
            "(SELECT COUNT(*) FROM notes WHERE project_id = ? AND shared = 1) AS shared_notes",
            (project_id, project_id, project_id),
        )
        assert row is not None, "an aggregate query always returns one row"
        return ProjectContents(row["tasks"], row["notes"], row["shared_notes"])

    @write
    async def delete_project(self, project_id: int) -> bool:
        """Delete a project with its roots, tasks and notes. Nothing of it stays.

        The rows go one table at a time, so the FTS triggers see every task and note.
        """
        connection = await self.open()
        if await self.project(project_id) is None:
            return False
        await self.require(project_id, role="owner")
        await connection.execute("DELETE FROM tasks WHERE project_id = ?", (project_id,))
        await connection.execute("DELETE FROM notes WHERE project_id = ?", (project_id,))
        await connection.execute("DELETE FROM project_roots WHERE project_id = ?", (project_id,))
        cursor = await connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        await connection.commit()
        return bool(cursor.rowcount)

    @write
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
            await self.require(project_id, role="editor")
            changes["updated_principal"] = self.principal
            changes["updated_computer"] = self.actor["computer"]
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

    async def task_links(self, task_id: int) -> list[TaskLinkOut]:
        if await self.task(task_id) is None:
            raise LookupError(f"no task #{task_id}")
        rows = QUERIES.task_links(await self.open(), task_id=task_id, principal=self.principal)
        return [TaskLinkOut(**dict(row)) async for row in rows]

    @write
    async def link_task(
        self, task_id: int, kind: Relation, target_id: int, *, reason: str | None = None
    ) -> bool:
        """Write both directions under one SQLite writer lock, including cycle checks."""
        return await self._task_link(task_id, kind, target_id, reason=reason, remove=False)

    @write
    async def unlink_task(self, task_id: int, kind: Relation, target_id: int) -> bool:
        return await self._task_link(task_id, kind, target_id, reason=None, remove=True)

    async def _task_link(
        self, task_id: int, kind: Relation, target_id: int, *, reason: str | None, remove: bool
    ) -> bool:
        if task_id == target_id:
            raise ValueError("a task cannot link to itself")
        forward, inverse = relation(kind)
        connection = await self.open()
        # The process-local @write lock alone cannot serialize other SQLite connections.
        await connection.execute("BEGIN IMMEDIATE")
        for record_id in (task_id, target_id):
            task = await self.task(record_id)
            if task is None:
                raise LookupError(f"no task #{record_id}")
            await self.require(task.project_id, role="editor")
        pair = {
            "task_id": task_id,
            "target_id": target_id,
            "relation": forward.value,
            "inverse": inverse.value,
        }
        existing = [row async for row in QUERIES.link_pair(connection, **pair)]
        if remove:
            if not existing:
                await connection.commit()
                return False
            await QUERIES.unlink_tasks(connection, **pair)
        else:
            reasons = {row["reason"] for row in existing}
            if len(reasons) > 1:
                raise ValueError("inverse links have conflicting reasons; unlink them first")
            saved_reason = reason if reason is not None else next(iter(reasons), "")
            if len(existing) == 2 and reasons == {saved_reason}:
                await connection.commit()
                return False
            # Inspect only one orientation; reverse rows must not create false cycles.
            source, target, directed = task_id, target_id, forward
            if forward in (Relation.CONTAINS, Relation.BLOCKS):
                source, target, directed = target_id, task_id, inverse
            if directed in (Relation.PART_OF, Relation.DEPENDS_ON):
                cycle = await QUERIES.link_cycle(
                    connection, source=source, target=target, relation=directed.value
                )
                if cycle:
                    raise ValueError(f"{directed.value} would create a cycle")
            now = time.time()
            await QUERIES.link_tasks(
                connection,
                **pair,
                reason=saved_reason,
                now=now,
                principal=self.principal,
                computer=self.actor["computer"],
            )
        await QUERIES.touch_link_tasks(
            connection,
            task_id=task_id,
            target_id=target_id,
            now=time.time(),
            principal=self.principal,
            computer=self.actor["computer"],
        )
        await connection.commit()
        return True

    async def task(self, task_id: int) -> Task | None:
        row = await self.row(
            f"SELECT {TASK_COLUMNS} FROM tasks t JOIN projects p ON p.id = t.project_id "
            f"WHERE t.id = ? AND {ACCESS}",
            (task_id, self.principal),
        )
        return to_task(row) if row else None

    @write
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
        await self.require(project_id, role="editor")
        connection = await self.open()
        now = time.time()
        closed = now if status in CLOSED_STATUSES else None
        inserted_id = await QUERIES.add_task(
            connection,
            project_id=project_id,
            title=title,
            body=body,
            status=status.value,
            priority=RANKS[priority],
            tags=join_tags(tags),
            created_at=now,
            updated_at=now,
            closed_at=closed,
            created_principal=self.principal,
            created_computer=self.actor["computer"],
            updated_principal=self.principal,
            updated_computer=self.actor["computer"],
        )
        await connection.commit()
        created = await self.task(inserted_id or 0)
        assert created is not None, "the task just written is not there"
        return created

    @write
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
        connection = await self.open()
        existing = await self.task(task_id)
        if existing is None:
            return None
        await self.require(existing.project_id, role="editor")
        changes: dict[str, Any] = {}
        now = time.time()
        if title is not None:
            changes["title"] = title
        if body is not None:
            changes["body"] = body
        if status is not None and status != existing.status:
            changes["status"] = status.value
            changes["closed_at"] = now if status in CLOSED_STATUSES else None
        if priority is not None:
            changes["priority"] = RANKS[priority]
        if tags is not None:
            changes["tags"] = join_tags(tags)
        if project_id is not None and project_id != existing.project_id:
            await self.require(project_id, role="editor")
            destination = await self.project(project_id)
            if destination is None:
                raise ValueError(f"no project #{project_id}")
            changes["project_id"] = project_id
        if changes:
            changes["updated_principal"] = self.principal
            changes["updated_computer"] = self.actor["computer"]
            changes["updated_at"] = now
            assignments = ", ".join(f"{column} = ?" for column in changes)
            await connection.execute(
                f"UPDATE tasks SET {assignments} WHERE id = ?",
                (*changes.values(), task_id),
            )
        await connection.commit()
        return await self.task(task_id)

    @write
    async def delete_task(self, task_id: int) -> bool:
        task = await self.task(task_id)
        if task is None or task.status == Status.DELETED:
            return False
        return await self.update_task(task_id, status=Status.DELETED) is not None

    async def comment(self, comment_id: int) -> Comment | None:
        row = await self.row(
            f"SELECT c.*, t.project_id, {PROJECT_NAME} AS project FROM task_comments c "
            "JOIN tasks t ON t.id = c.task_id JOIN projects p ON p.id = t.project_id "
            f"WHERE c.id = ? AND {ACCESS}",
            (comment_id, self.principal),
        )
        return Comment(**dict(row)) if row else None

    @write
    async def add_comment(self, task_id: int, body: str) -> Comment:
        if not body.strip():
            raise ValueError("a comment must not be empty")
        connection = await self.open()
        task = await self.task(task_id)
        if task is None:
            raise LookupError(f"no task #{task_id}")
        await self.require(task.project_id, role="editor")
        now = time.time()
        inserted_id = await QUERIES.add_comment(
            connection,
            task_id=task_id,
            body=body,
            created_at=now,
            principal=self.principal,
            computer=self.actor["computer"],
        )
        await connection.commit()
        saved = await self.comment(inserted_id or 0)
        assert saved is not None
        return saved

    async def count_comments(self, task_id: int) -> int:
        if await self.task(task_id) is None:
            return 0
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
            f"SELECT c.*, t.project_id, {PROJECT_NAME} AS project {snippet} FROM {source} "
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
        clauses: list[str] = [ACCESS]
        parameters: list[Any] = [self.principal]
        if project_id is not None:
            clauses.append("t.project_id = ?")
            parameters.append(project_id)
        if computer is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM project_roots r "
                "WHERE r.project_id = p.id AND r.principal = ? AND r.computer = ?)"
            )
            parameters.extend((self.principal, computer))
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
        if column not in {"status", "priority"}:
            raise ValueError("unsupported grouping column")
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
            f"WHERE n.id = ? AND {ACCESS}",
            (note_id, self.principal),
        )
        return to_note(row) if row else None

    async def note_by_title(self, title: str, *, project_id: int) -> Note | None:
        """The note of one project with that title; a title is unique inside a project."""
        row = await self.row(
            f"SELECT {NOTE_COLUMNS} FROM notes n JOIN projects p ON p.id = n.project_id "
            f"WHERE n.project_id = ? AND n.title = ? AND {ACCESS}",
            (project_id, title, self.principal),
        )
        return to_note(row) if row else None

    @write
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
        await self.require(project_id, role="editor")
        connection = await self.open()
        now = time.time()
        inserted_id = await QUERIES.add_note(
            connection,
            project_id=project_id,
            title=title,
            body=body,
            tags=join_tags(tags),
            shared=int(shared),
            created_at=now,
            updated_at=now,
            created_principal=self.principal,
            created_computer=self.actor["computer"],
            updated_principal=self.principal,
            updated_computer=self.actor["computer"],
        )
        await connection.commit()
        created = await self.note(inserted_id or 0)
        assert created is not None, "the note just written is not there"
        return created

    @write
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
        existing = await self.note(note_id)
        if existing is None:
            return None
        await self.require(existing.project_id, role="editor")
        if project_id is not None:
            await self.require(project_id, role="editor")
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
            changes["updated_principal"] = self.principal
            changes["updated_computer"] = self.actor["computer"]
            changes["updated_at"] = time.time()
            assignments = ", ".join(f"{column} = ?" for column in changes)
            connection = await self.open()
            await connection.execute(
                f"UPDATE notes SET {assignments} WHERE id = ?", (*changes.values(), note_id)
            )
            await connection.commit()
        return await self.note(note_id)

    @write
    async def append_note(self, note_id: int, text: str) -> Note | None:
        """Add a paragraph at the end of a note.

        One statement writes the whole body, so the FTS triggers see the final text once. The
        result must fit `NOTE_LIMIT` like any other body; a note that would overflow is untouched.
        """
        existing = await self.note(note_id)
        if existing is None:
            return None
        await self.require(existing.project_id, role="editor")
        check_note(f"{existing.body}\n\n{text}" if existing.body else text)
        connection = await self.open()
        await connection.execute(
            "UPDATE notes SET body = CASE WHEN body = '' THEN ? ELSE body || ? END, "
            "updated_at = ?, updated_principal = ?, updated_computer = ? WHERE id = ?",
            (text, f"\n\n{text}", time.time(), *self.actor.values(), note_id),
        )
        await connection.commit()
        return await self.note(note_id)

    @write
    async def delete_note(self, note_id: int) -> bool:
        existing = await self.note(note_id)
        if existing is None:
            return False
        await self.require(existing.project_id, role="editor")
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
        clauses: list[str] = [ACCESS]
        parameters: list[Any] = [self.principal]
        if project_id is not None:
            clauses.append("(n.project_id = ? OR n.shared = 1)")
            parameters.append(project_id)
        if computer is not None:
            clauses.append(
                "(EXISTS (SELECT 1 FROM project_roots r "
                "WHERE r.project_id = p.id AND r.principal = ? AND r.computer = ?) OR n.shared = 1)"
            )
            parameters.extend((self.principal, computer))
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
