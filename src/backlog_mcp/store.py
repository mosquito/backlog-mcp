"""SQLite storage: projects, tasks, notes, and the FTS5 index over them."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from .computer import hostname
from .migrate import migrate
from .models import (
    CLOSED_STATUSES,
    PRIORITIES,
    RANKS,
    Actor,
    Priority,
    Relation,
    Status,
    TaskLinkOut,
    check_assignment,
    relation,
)
from .names import owner_name, principal_label, project_name
from .queries import QUERIES
from .transaction import transaction

LOCAL_PRINCIPAL = "local"


def write[**P, T](method: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
    """Serialize complete mutations, including their authorization and history."""

    @wraps(method)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        store = args[0]
        assert isinstance(store, Store)
        async with transaction(await store.open()):
            return await method(*args, **kwargs)

    return wrapped


#: A note is a short fact, not a page; the body stops here, in characters.
NOTE_LIMIT = 1024


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


def assignment_pattern(prefix: str) -> str:
    """Escape a literal assignment prefix for LIKE."""
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class Unset(Enum):
    """An omitted update field; None explicitly clears a nullable value."""

    VALUE = 0


class AssignmentConflict(ValueError):
    """The current assignment did not match the caller's expected value."""

    def __init__(self, task_id: int, expected: str | None, assigned: str | None) -> None:
        self.assigned = assigned
        super().__init__(
            f"assignment conflict for task #{task_id}: expected {expected!r}, current {assigned!r}"
        )


@dataclass(slots=True)
class Comment:
    id: int
    task_id: int
    project_id: int
    project: str
    body: str
    kind: Literal["comment", "status", "move"]
    created_at: float
    task_assigned: str | None = None
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
    assigned: str | None = None
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
        assigned=row["assigned"],
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
        self.views: dict[tuple[str, str | None], Store] = {}
        self.connection: aiosqlite.Connection | None = None
        self.opening = asyncio.Lock()

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
                    await QUERIES.busy_timeout(connection)
                    await QUERIES.foreign_keys(connection)
                    await migrate(connection)
                    # Switching journal mode can return BUSY immediately when another startup
                    # enters its migration transaction. Release the failed attempt and retry.
                    for attempt in range(5):
                        try:
                            await QUERIES.journal_mode(connection)
                            break
                        except sqlite3.OperationalError as error:
                            if error.sqlite_errorcode != sqlite3.SQLITE_BUSY or attempt == 4:
                                raise
                            await asyncio.sleep(0.05)
                    async with transaction(connection):
                        await QUERIES.register_principal(
                            connection,
                            identity=self.principal,
                            handle=owner_name(self.principal),
                            name=owner_name(self.principal),
                        )
                except BaseException:
                    await connection.close()
                    raise
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
        if not projects:
            return projects
        rows = QUERIES.project_roots(
            await self.open(), ids=json.dumps([p.id for p in projects]), principal=self.principal
        )
        by_id: dict[int, list[ProjectRoot]] = {project.id: [] for project in projects}
        async for row in rows:
            by_id[row["project_id"]].append(ProjectRoot(row["computer"], row["root"]))
        for project in projects:
            project.roots = by_id[project.id]
            project.role = await self.require(project.id)
        return projects

    async def roots(self, project_id: int) -> list[ProjectRoot]:
        project = await self.project(project_id)
        return project.roots if project else []

    async def project(self, project_id: int) -> Project | None:
        row = await QUERIES.project(
            await self.open(), project_id=project_id, principal=self.principal
        )
        return (await self.attach_roots([to_project(row)]))[0] if row else None

    async def project_by_slug(self, slug: str) -> Project | None:
        handle, local_slug = slug.split("/", 1) if "/" in slug else (None, slug)
        rows = [
            row
            async for row in QUERIES.project_by_slug(
                await self.open(), handle=handle, slug=local_slug, principal=self.principal
            )
        ]
        if len(rows) > 1:
            references = ", ".join(project_name(row["owner"], row["slug"]) for row in rows)
            raise ValueError(f"ambiguous project {slug!r}; use its full name: {references}")
        return (await self.attach_roots([to_project(rows[0])]))[0] if rows else None

    async def project_by_root(self, root: Path | str, *, computer: str) -> Project | None:
        row = await QUERIES.project_by_root(
            await self.open(), computer=computer, root=str(root), principal=self.principal
        )
        return (await self.attach_roots([to_project(row)]))[0] if row else None

    async def project_for_path(self, path: Path, *, computer: str) -> Project | None:
        candidates = [str(path), *(str(parent) for parent in path.parents)]
        rows = QUERIES.projects_for_paths(
            await self.open(),
            computer=computer,
            paths=json.dumps(candidates),
            principal=self.principal,
        )
        found = await self.attach_roots([to_project(row) async for row in rows])
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
        rows = QUERIES.projects(
            await self.open(), archived=archived, computer=computer, principal=self.principal
        )
        return await self.attach_roots([to_project(row) async for row in rows])

    async def search_projects(
        self,
        query: str,
        *,
        project_id: int | None = None,
        computer: str | None = None,
        limit: int = 20,
    ) -> list[Project]:
        match = match_query(query)
        if not match:
            return []
        rows = QUERIES.search_projects(
            await self.open(),
            match=match,
            project_id=project_id,
            computer=computer,
            principal=self.principal,
            limit=limit,
        )
        return await self.attach_roots([to_project(row) async for row in rows])

    async def computers(self) -> list[str]:
        return [
            row["computer"]
            async for row in QUERIES.computers(await self.open(), principal=self.principal)
        ]

    async def slugs(self, *, owned: bool = False) -> list[str]:
        if owned:
            return [
                row["slug"]
                async for row in QUERIES.owned_slugs(await self.open(), principal=self.principal)
            ]
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
        await QUERIES.add_root(
            connection,
            project_id=inserted_id,
            principal=self.principal,
            computer=computer,
            root=str(root),
        )
        created = await self.project(inserted_id or 0)
        assert created is not None, "the project just written is not there"
        return created

    @write
    async def add_root(self, project_id: int, *, computer: str, root: Path | str) -> Project:
        """Bind a project to one location; non-empty paths belong to only one project."""
        await self.require(project_id)
        connection = await self.open()
        existing = await QUERIES.existing_root(
            connection,
            project_id=project_id,
            principal=self.principal,
            computer=computer,
            root=str(root),
        )
        if existing is not None:
            project = await self.project(project_id)
            assert project is not None
            return project
        try:
            await QUERIES.add_root(
                connection,
                project_id=project_id,
                principal=self.principal,
                computer=computer,
                root=str(root),
            )
        except aiosqlite.IntegrityError as error:
            raise ValueError(f"path {root!s} on {computer!r} is already registered") from error
        project = await self.project(project_id)
        assert project is not None, "the project just updated is gone"
        return project

    async def project_contents(self, project_id: int) -> ProjectContents:
        await self.require(project_id)
        row = await QUERIES.project_contents(await self.open(), project_id=project_id)
        assert row is not None
        return ProjectContents(row["tasks"], row["notes"], row["shared_notes"])

    @write
    async def delete_project(self, project_id: int) -> bool:
        connection = await self.open()
        if await self.project(project_id) is None:
            return False
        await self.require(project_id, role="owner")
        await QUERIES.delete_project_tasks(connection, project_id=project_id)
        await QUERIES.delete_project_notes(connection, project_id=project_id)
        await QUERIES.delete_project_project_roots(connection, project_id=project_id)
        removed = await QUERIES.delete_project(connection, project_id=project_id)
        return bool(removed)

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
            connection = await self.open()
            await QUERIES.update_project(
                connection, project_id=project_id, changes=json.dumps(changes)
            )
        return await self.project(project_id)

    # -- tasks -----------------------------------------------------------------------------

    async def assigned_tasks(self, prefix: str, *, limit: int = 50, offset: int = 0) -> list[Task]:
        rows = QUERIES.assigned_tasks(
            await self.open(),
            prefix=prefix,
            pattern=assignment_pattern(prefix),
            principal=self.principal,
            limit=limit,
            offset=offset,
        )
        return [to_task(row) async for row in rows]

    async def count_assigned(self, prefix: str) -> int:
        row = await QUERIES.count_assigned(
            await self.open(),
            prefix=prefix,
            pattern=assignment_pattern(prefix),
            principal=self.principal,
        )
        assert row is not None
        return int(row[0])

    @write
    async def claim_task(
        self, task_id: int, *, expected: str | None, assigned: str | None
    ) -> tuple[Task, bool]:
        """Compare and replace an assignment under a database writer lock."""
        if expected is not None:
            check_assignment(expected)
        if assigned is not None:
            check_assignment(assigned)
            principal = principal_label(self.principal, self.actor["computer"])
            if assigned.rsplit(":", 1)[0] != principal:
                raise PermissionError(
                    f"assigned must belong to the current principal {principal!r}"
                )
        connection = await self.open()
        task = await self.task(task_id)
        if task is None:
            raise LookupError(f"no task #{task_id}")
        await self.require(task.project_id, role="editor")
        if task.assigned != expected:
            raise AssignmentConflict(task_id, expected, task.assigned)
        if expected == assigned:
            return task, False
        now = time.time()
        await QUERIES.claim_task(
            connection,
            task_id=task_id,
            expected=expected,
            assigned=assigned,
            now=now,
            principal=self.principal,
            computer=self.actor["computer"],
        )
        await QUERIES.add_comment(
            connection,
            task_id=task_id,
            body=f"Assignment changed: {expected or 'null'} → {assigned or 'null'}",
            created_at=now,
            principal=self.principal,
            computer=self.actor["computer"],
        )
        saved = await self.task(task_id)
        assert saved is not None
        return saved, True

    async def linked_tasks(
        self, *, task_id: int | None = None, project_id: int | None = None
    ) -> list[Task]:
        """Resource dependents for internal routing, never for a public response."""
        rows = QUERIES.linked_tasks(await self.open(), task_id=task_id, project_id=project_id)
        return [to_task(row) async for row in rows]

    async def notification_recipients(self, project_id: int) -> list[str]:
        return [
            row["principal"]
            async for row in QUERIES.notification_recipients(
                await self.open(), project_id=project_id
            )
        ]

    async def data_version(self) -> int:
        row = await QUERIES.data_version(await self.open())
        assert row is not None
        return int(row[0])

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
                return False
            await QUERIES.unlink_tasks(connection, **pair)
        else:
            reasons = {row["reason"] for row in existing}
            if len(reasons) > 1:
                raise ValueError("inverse links have conflicting reasons; unlink them first")
            saved_reason = reason if reason is not None else next(iter(reasons), "")
            if len(existing) == 2 and reasons == {saved_reason}:
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
        return True

    async def task(self, task_id: int) -> Task | None:
        row = await QUERIES.task(await self.open(), task_id=task_id, principal=self.principal)
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
            await QUERIES.update_task(connection, task_id=task_id, changes=json.dumps(changes))
        return await self.task(task_id)

    @write
    async def delete_task(self, task_id: int) -> bool:
        task = await self.task(task_id)
        if task is None or task.status == Status.DELETED:
            return False
        return await self.update_task(task_id, status=Status.DELETED) is not None

    async def comment(self, comment_id: int) -> Comment | None:
        row = await QUERIES.comment(
            await self.open(), comment_id=comment_id, principal=self.principal
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
        saved = await self.comment(inserted_id or 0)
        assert saved is not None
        return saved

    async def count_comments(self, task_id: int) -> int:
        if await self.task(task_id) is None:
            return 0
        row = await QUERIES.count_comments(await self.open(), task_id=task_id)
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
        parameters = self.filters(
            project_id=project_id, computer=computer, status=status, tag=tag, priority=None
        )
        parameters.update(
            task_id=task_id, exclude_deleted=task_id is None and not status, limit=limit
        )
        if query is not None:
            match = match_query(query)
            if not match:
                return []
            rows = QUERIES.search_comments(await self.open(), match=match, **parameters)
        else:
            rows = QUERIES.comments(await self.open(), **parameters)
        return [Comment(**dict(row)) async for row in rows]

    def filters(
        self,
        *,
        project_id: int | None,
        status: Sequence[Status] | None,
        tag: str | None,
        priority: Priority | None,
        computer: str | None = None,
    ) -> dict[str, Any]:
        return {
            "principal": self.principal,
            "project_id": project_id,
            "computer": computer,
            "statuses": json.dumps([item.value for item in status]) if status else None,
            "tag": f"%,{tag.strip().lower()},%" if tag else None,
            "priority": RANKS[priority] if priority is not None else None,
        }

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
        if order not in {"backlog", "recent"}:
            raise KeyError(order)
        parameters = self.filters(
            project_id=project_id, status=status, tag=tag, priority=priority, computer=computer
        )
        rows = QUERIES.tasks(
            await self.open(), **parameters, ordering=order, limit=limit, offset=offset
        )
        return [to_task(row) async for row in rows]

    async def count_tasks(
        self,
        *,
        project_id: int | None = None,
        status: Sequence[Status] | None = None,
        tag: str | None = None,
        priority: Priority | None = None,
        computer: str | None = None,
    ) -> int:
        parameters = self.filters(
            project_id=project_id, status=status, tag=tag, priority=priority, computer=computer
        )
        row = await QUERIES.count_tasks(await self.open(), **parameters)
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
        match = match_query(query)
        if not match:
            return []
        parameters = self.filters(
            project_id=project_id, status=status, tag=tag, priority=None, computer=computer
        )
        rows = QUERIES.search_tasks(
            await self.open(),
            **parameters,
            match=match,
            exclude_deleted=status is None,
            limit=limit,
        )
        return [to_task(row) async for row in rows]

    async def tags(
        self, *, project_id: int | None = None, computer: str | None = None
    ) -> list[str]:
        parameters = self.filters(
            project_id=project_id, status=None, tag=None, priority=None, computer=computer
        )
        rows = QUERIES.tags(await self.open(), **parameters)
        found: set[str] = set()
        async for row in rows:
            found.update(split_tags(row["tags"]))
        return sorted(found)

    async def group(
        self, column: str, *, project_id: int | None = None, computer: str | None = None
    ) -> dict[str, int]:
        if column not in {"status", "priority"}:
            raise ValueError("unsupported grouping column")
        parameters = self.filters(
            project_id=project_id, status=None, tag=None, priority=None, computer=computer
        )
        rows = QUERIES.group_tasks(await self.open(), column=column, **parameters)
        return {str(row["key"]): int(row["found"]) async for row in rows}

    # -- notes -----------------------------------------------------------------------------

    async def note(self, note_id: int) -> Note | None:
        row = await QUERIES.note(await self.open(), note_id=note_id, principal=self.principal)
        return to_note(row) if row else None

    async def note_by_title(self, title: str, *, project_id: int) -> Note | None:
        row = await QUERIES.note_by_title(
            await self.open(), project_id=project_id, title=title, principal=self.principal
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
            connection = await self.open()
            await QUERIES.update_note(connection, note_id=note_id, changes=json.dumps(changes))
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
        await QUERIES.append_note(
            connection,
            text=text,
            suffix=f"\n\n{text}",
            now=time.time(),
            **self.actor,
            note_id=note_id,
        )
        return await self.note(note_id)

    @write
    async def delete_note(self, note_id: int) -> bool:
        existing = await self.note(note_id)
        if existing is None:
            return False
        await self.require(existing.project_id, role="editor")
        connection = await self.open()
        removed = await QUERIES.delete_note(connection, note_id=note_id)
        return bool(removed)

    def note_filters(
        self,
        *,
        project_id: int | None,
        tag: str | None,
        shared: bool | None,
        computer: str | None = None,
    ) -> dict[str, Any]:
        return {
            "principal": self.principal,
            "project_id": project_id,
            "computer": computer,
            "shared": shared,
            "tag": f"%,{tag.strip().lower()},%" if tag else None,
        }

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
        parameters = self.note_filters(
            project_id=project_id, tag=tag, shared=shared, computer=computer
        )
        rows = QUERIES.notes(await self.open(), **parameters, limit=limit, offset=offset)
        return [to_note(row) async for row in rows]

    async def count_notes(
        self,
        *,
        project_id: int | None = None,
        tag: str | None = None,
        shared: bool | None = None,
        computer: str | None = None,
    ) -> int:
        parameters = self.note_filters(
            project_id=project_id, tag=tag, shared=shared, computer=computer
        )
        row = await QUERIES.count_notes(await self.open(), **parameters)
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
        match = match_query(query)
        if not match:
            return []
        parameters = self.note_filters(
            project_id=project_id, tag=tag, shared=shared, computer=computer
        )
        rows = QUERIES.search_notes(await self.open(), **parameters, match=match, limit=limit)
        return [to_note(row) async for row in rows]


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
