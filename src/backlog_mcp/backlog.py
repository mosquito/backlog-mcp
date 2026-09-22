"""Scope resolution: which project a call works on when nothing names one."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from .computer import check, hostname
from .database import Database
from .models import (
    Actor,
    Origin,
)
from .names import owner_name, project_name
from .queries import QUERIES
from .records import (
    Project,
    ProjectContents,
    ProjectRoot,
    Unset,
    to_project,
)
from .transaction import Transaction

log = logging.getLogger(__name__)

#: A directory holding one of these is the root of a checkout, and so of a project.
MARKERS = (".git", ".hg", ".svn", ".jj")


class UnknownProject(LookupError):
    """A project or a computer was named and there is no such thing."""


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "project"


def checkout_root(path: Path) -> Path:
    """The nearest ancestor that looks like a checkout, else `path` itself."""
    for candidate in (path, *path.parents):
        if any((candidate / marker).exists() for marker in MARKERS):
            return candidate
    return path


@dataclass(frozen=True, slots=True)
class Reach:
    """What a listing covers: one project, every project of one computer, or every project."""

    project: Project | None = None
    computer: str | None = None

    @property
    def project_id(self) -> int | None:
        return self.project.id if self.project else None

    @property
    def slug(self) -> str | None:
        return self.project.full_name if self.project else None

    @property
    def scope_computer(self) -> str | None:
        """The named computer the listing covers, absent for a whole logical project."""
        return self.computer

    @property
    def label(self) -> str:
        """The scope as a phrase, for titles."""
        if self.project:
            return self.project.full_name
        if self.computer:
            return f"every project on {self.computer}"
        return "every project on every computer"


class Backlog:
    """The database and one working directory, as the tools see them.

    Every handler asks this object which project it is working on. Resolution order:
    the project named on the command line or in the URL, then the deepest registered project of
    this computer containing the working directory, then a project created for it. Nothing else
    chooses: no pin and no remembered choice, so two clients never redirect each other.
    """

    def __init__(
        self,
        database: Database | Path | str,
        *,
        principal: str = "local",
        cwd: Path | str | None = None,
        project: str | None = None,
        computer: str | None = None,
    ) -> None:
        self.auth_type: Literal["basic", "jwt", "none"] = "none"
        self.database = database if isinstance(database, Database) else Database(Path(database))
        self.principal = principal
        self.cwd = Path(cwd).expanduser().resolve() if cwd else Path.cwd().resolve()
        #: Named by `--project`, and so fixed for the life of the process.
        self.named = project or None
        self.computer = check(computer) if computer else hostname()
        #: True for a view of another computer: its directories are not here, so nothing is
        #: read from the file system and nothing is registered from `cwd`.
        self.remote = False

    def view(
        self,
        *,
        computer: str | None = None,
        project: str | None = None,
        principal: str | None = None,
    ) -> Backlog:
        """The same database, seen from a computer, with a project fixed when given.

        One HTTP server answers for several computers: each request gets its own view.
        """
        seen = Backlog(
            self.database,
            principal=principal or self.principal,
            cwd=self.cwd,
            project=project or self.named,
            computer=computer or self.computer,
        )
        seen.auth_type = self.auth_type
        seen.remote = seen.computer != self.computer
        return seen

    @property
    def actor(self) -> Actor:
        return Actor(principal=self.principal, computer=self.computer)

    @property
    def transaction(self) -> Transaction:
        assert self.database.transaction is not None, "database is not open"
        return self.database.transaction

    async def open(self) -> aiosqlite.Connection:
        return await self.database.open(self.principal)

    async def close(self) -> None:
        await self.database.close()

    def absolute(self, path: Path | str) -> Path:
        """A path as given, made canonical against the working directory.

        A path on another computer is only normalized: it must be absolute, and this file system
        knows nothing about it.
        """
        found = Path(path)
        if self.remote:
            if not found.is_absolute():
                raise ValueError(
                    f"{str(path)!r} is relative: give an absolute path on {self.computer!r}"
                )
            return Path(os.path.normpath(found))
        found = found.expanduser()
        if not found.is_absolute():
            found = self.cwd / found
        return found.resolve()

    async def current(self) -> tuple[Project, Origin]:
        """The project the tools work on, and why that one. Raises when there is none."""
        return await self.decide()

    async def decide(self) -> tuple[Project, Origin]:
        project, origin = await self.choose()
        if project is None:
            raise UnknownProject(
                f"no project chosen on computer {self.computer!r}: pass project=<slug> to the "
                "tool, or add project=<slug> to the URL"
            )
        return project, origin

    async def choose(self) -> tuple[Project | None, Origin]:
        """The project the tools work on, or `None` with `Origin.NONE` when nothing chose one.

        Only a view of another computer ends with no project: this computer always has the
        working directory to fall back on.
        """
        connection = await self.open()
        if self.named:
            return await self.lookup(self.named), Origin.ARGUMENT
        if self.remote:
            return None, Origin.NONE
        found = await project_for_path(connection, self.actor, self.cwd, computer=self.computer)
        if found is not None:
            return found, Origin.DIRECTORY
        return await self.create(checkout_root(self.cwd)), Origin.CREATED

    async def scope(self) -> tuple[Project | None, Origin]:
        """Like `current`, with no project as a state to report, not an error."""
        return await self.choose()

    async def lookup(self, reference: str) -> Project:
        """Resolve an owner/slug name, unique short slug, #id, or explicit filesystem path."""
        connection = await self.open()
        found = (
            await read_project(connection, self.actor, int(reference[1:]))
            if reference.startswith("#") and reference[1:].isdigit()
            else await project_by_slug(connection, self.actor, reference)
        )
        if found is not None:
            return found
        if reference.startswith((".", "/", "~")):
            path = self.absolute(reference)
            found = await project_by_root(connection, self.actor, path, computer=self.computer)
            if found is None:
                found = await project_for_path(connection, self.actor, path, computer=self.computer)
            if found is not None:
                return found
        raise UnknownProject(
            f"no project {reference!r}: name a registered slug, or a path inside the project"
        )

    async def resolve(self, reference: str | None) -> Project:
        """The named project, or the current one when nothing is named."""
        if reference:
            return await self.lookup(reference)
        project, _ = await self.current()
        return project

    async def reach(
        self,
        project: str | None = None,
        *,
        all_projects: bool = False,
        computer: str | None = None,
        all_computers: bool = False,
    ) -> Reach:
        """What a listing covers. A named project narrows a computer scope.

        `all_computers` covers everything, `computer` every project of that computer,
        `all_projects` every project of this computer, and `project` one project, else the
        current one. A computer nobody registered a project on is `UnknownProject`.
        """
        connection = await self.open()
        if all_computers:
            return Reach()
        if project:
            selected = await self.resolve(project)
            name = check(computer) if computer else None
            if name and not any(root.computer == name for root in selected.roots):
                raise UnknownProject(f"project {selected.slug!r} has no binding on {name!r}")
            return Reach(project=selected, computer=name)
        if computer:
            name = check(computer)
            known = await computers(connection, self.actor)
            if name not in known and name != self.computer:
                raise UnknownProject(
                    f"no computer {name!r}: known computers are {', '.join(known) or 'none'}"
                )
            return Reach(computer=name)
        if all_projects:
            return Reach(computer=self.computer)
        return Reach(project=await self.resolve(project))

    async def free_slug(self, wanted: str) -> str:
        connection = await self.open()
        taken = set(await slugs(connection, self.actor, owned=True))
        if wanted not in taken:
            return wanted
        for suffix in range(2, len(taken) + 3):
            candidate = f"{wanted}-{suffix}"
            if candidate not in taken:
                return candidate
        raise AssertionError("every candidate slug was taken")

    async def create(
        self, root: Path | str, *, name: str | None = None, description: str = ""
    ) -> Project:
        """Register a directory of this computer as a project, or return the project it has.

        The directory must exist. A view of another computer cannot check that and takes the
        path as given.
        """
        from .embeddings import enqueue

        connection = await self.open()
        if str(root) == "":
            label = name or "project"
            slug = await self.free_slug(slugify(label))
            async with self.transaction:
                created = await add_project(
                    connection,
                    self.actor,
                    slug=slug,
                    name=label,
                    root="",
                    computer=self.computer,
                    description=description,
                )
            enqueue(self, created)
            return created
        path = self.absolute(root)
        existing = await project_by_root(connection, self.actor, path, computer=self.computer)
        if existing is not None:
            return existing
        if not self.remote and not path.is_dir():
            raise ValueError(f"no such directory on {self.computer!r}: {path}")
        label = name or path.name or "project"
        slug = await self.free_slug(slugify(label))
        async with self.transaction:
            created = await add_project(
                connection,
                self.actor,
                slug=slug,
                name=label,
                root=path,
                computer=self.computer,
                description=description,
            )
        enqueue(self, created)
        log.info("registered project %s at %s on %s", created.slug, path, self.computer)
        return created

    async def save(
        self,
        connection: aiosqlite.Connection,
        transaction: Transaction,
        *,
        reference: str | None = None,
        root: Path | str | None = None,
        name: str | None = None,
        description: str | None = None,
        archived: bool | None = None,
        preferred_language: str | None | Unset = Unset.VALUE,
    ) -> Project:
        """Register a project, or update the one named. Defaults to the working directory."""
        from .embeddings import enqueue

        match reference, root:
            case None | "", None:
                if self.remote:
                    raise ValueError(
                        f"on computer {self.computer!r} name the project, or give its root"
                    )
                #: Nothing named: the working directory, registered if it is new.
                path = self.absolute(self.cwd)
                found = await project_by_root(connection, self.actor, path, computer=self.computer)
                project = found if found is not None else await self.create(checkout_root(path))
            case None | "", (str() | Path()) as directory:
                project = await self.create(directory, name=name, description=description or "")
            case str() as named, (str() | Path()) as directory:
                project = await self.lookup(named)
                root_text = str(directory)
                if root_text:
                    path = self.absolute(directory)
                    if not self.remote and not path.is_dir():
                        raise ValueError(f"no such directory on {self.computer!r}: {path}")
                    async with transaction:
                        project = await add_root(
                            connection, self.actor, project.id, computer=self.computer, root=path
                        )
                else:
                    async with transaction:
                        project = await add_root(
                            connection, self.actor, project.id, computer=self.computer, root=""
                        )
            case str() as named, None:
                project = await self.lookup(named)
        async with transaction:
            updated = await update_project(
                connection,
                self.actor,
                project.id,
                name=name,
                description=description,
                archived=archived,
                preferred_language=preferred_language,
            )
        assert updated is not None, "the project just updated is gone"
        enqueue(self, updated)
        return updated

    async def delete(
        self, project: Project, connection: aiosqlite.Connection, transaction: Transaction
    ) -> ProjectContents:
        """Delete a project for good: its roots, tasks and notes.

        The project fixed for this process by `--project` or the URL stays, because every later
        call would fail to find it.
        """
        if self.named and (await self.lookup(self.named)).id == project.id:
            raise ValueError(
                f"project {project.slug!r} is fixed for this client; delete it from another one"
            )
        from .embeddings import invalidate

        contents = await project_contents(connection, self.actor, project.id)
        async with transaction:
            await delete_project(connection, self.actor, project.id)
        invalidate(self)
        log.info(
            "deleted project %s with %d tasks and %d notes",
            project.slug,
            contents.tasks,
            contents.notes,
        )
        return contents


__all__ = ["MARKERS", "Backlog", "Reach", "UnknownProject", "checkout_root", "slugify"]


async def register(
    connection: aiosqlite.Connection, actor: Actor, identity: str, name: str
) -> None:

    await QUERIES.register_principal(
        connection, identity=identity, handle=owner_name(identity), name=name
    )


async def require(
    connection: aiosqlite.Connection, actor: Actor, project_id: int, *, role: str = "reader"
) -> str:
    row = await QUERIES.membership(connection, project_id=project_id, principal=actor.principal)
    if row is None:
        raise LookupError(f"no project #{project_id}")
    actual = str(row["role"])
    if role == "owner" and actual != "owner" or role == "editor" and actual == "reader":
        raise PermissionError(f"project #{project_id} requires {role} access")
    return actual


async def members(
    connection: aiosqlite.Connection, actor: Actor, project_id: int
) -> list[dict[str, str]]:
    await require(connection, actor, project_id)
    return [dict(row) async for row in QUERIES.members(connection, project_id=project_id)]


async def share(
    connection: aiosqlite.Connection, actor: Actor, project_id: int, principal: str, role: str
) -> None:
    await require(connection, actor, project_id, role="owner")
    if role not in {"reader", "editor"}:
        raise ValueError("membership role must be reader or editor")

    if await QUERIES.principal(connection, identity=principal) is None:
        raise LookupError("unknown principal; use a registered authentication identity")
    current = await QUERIES.membership(connection, project_id=project_id, principal=principal)
    if current and current["role"] == "owner":
        raise ValueError("the project owner cannot be changed")
    await QUERIES.save_member(connection, project_id=project_id, principal=principal, role=role)


async def attach_roots(
    connection: aiosqlite.Connection, actor: Actor, projects: list[Project]
) -> list[Project]:
    if not projects:
        return projects
    rows = QUERIES.project_roots(
        connection, ids=json.dumps([p.id for p in projects]), principal=actor.principal
    )
    by_id: dict[int, list[ProjectRoot]] = {project.id: [] for project in projects}
    async for row in rows:
        by_id[row["project_id"]].append(ProjectRoot(row["computer"], row["root"]))
    for project in projects:
        project.roots = by_id[project.id]
        project.role = await require(connection, actor, project.id)
    return projects


async def roots(
    connection: aiosqlite.Connection, actor: Actor, project_id: int
) -> list[ProjectRoot]:
    project = await read_project(connection, actor, project_id)
    return project.roots if project else []


async def read_project(
    connection: aiosqlite.Connection, actor: Actor, project_id: int
) -> Project | None:
    row = await QUERIES.project(connection, project_id=project_id, principal=actor.principal)
    return (await attach_roots(connection, actor, [to_project(row)]))[0] if row else None


async def project_by_slug(
    connection: aiosqlite.Connection, actor: Actor, slug: str
) -> Project | None:
    handle, local_slug = slug.split("/", 1) if "/" in slug else (None, slug)
    rows = [
        row
        async for row in QUERIES.project_by_slug(
            connection, handle=handle, slug=local_slug, principal=actor.principal
        )
    ]
    if len(rows) > 1:
        references = ", ".join(project_name(row["owner"], row["slug"]) for row in rows)
        raise ValueError(f"ambiguous project {slug!r}; use its full name: {references}")
    return (await attach_roots(connection, actor, [to_project(rows[0])]))[0] if rows else None


async def project_by_root(
    connection: aiosqlite.Connection, actor: Actor, root: Path | str, *, computer: str
) -> Project | None:
    row = await QUERIES.project_by_root(
        connection, computer=computer, root=str(root), principal=actor.principal
    )
    return (await attach_roots(connection, actor, [to_project(row)]))[0] if row else None


async def project_for_path(
    connection: aiosqlite.Connection, actor: Actor, path: Path, *, computer: str
) -> Project | None:
    candidates = [str(path), *(str(parent) for parent in path.parents)]
    rows = QUERIES.projects_for_paths(
        connection,
        computer=computer,
        paths=json.dumps(candidates),
        principal=actor.principal,
    )
    found = await attach_roots(connection, actor, [to_project(row) async for row in rows])
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


async def read_projects(
    connection: aiosqlite.Connection,
    actor: Actor,
    *,
    archived: bool = False,
    computer: str | None = None,
) -> list[Project]:
    rows = QUERIES.projects(
        connection, archived=archived, computer=computer, principal=actor.principal
    )
    return await attach_roots(connection, actor, [to_project(row) async for row in rows])


async def computers(connection: aiosqlite.Connection, actor: Actor) -> list[str]:
    return [
        row["computer"] async for row in QUERIES.computers(connection, principal=actor.principal)
    ]


async def slugs(
    connection: aiosqlite.Connection, actor: Actor, *, owned: bool = False
) -> list[str]:
    if owned:
        return [
            row["slug"] async for row in QUERIES.owned_slugs(connection, principal=actor.principal)
        ]
    return sorted(
        project.full_name for project in await read_projects(connection, actor, archived=True)
    )


async def add_project(
    connection: aiosqlite.Connection,
    actor: Actor,
    *,
    slug: str,
    name: str,
    root: Path | str,
    computer: str,
    description: str = "",
    preferred_language: str | None = None,
) -> Project:

    now = time.time()
    inserted_id = await QUERIES.add_project(
        connection,
        slug=slug,
        name=name,
        description=description,
        preferred_language=preferred_language,
        created_at=now,
        updated_at=now,
        owner=actor.principal,
        created_principal=actor.principal,
        created_computer=actor.computer or hostname(),
        updated_principal=actor.principal,
        updated_computer=actor.computer or hostname(),
    )
    await QUERIES.save_member(
        connection, project_id=inserted_id, principal=actor.principal, role="owner"
    )
    await QUERIES.add_root(
        connection,
        project_id=inserted_id,
        principal=actor.principal,
        computer=computer,
        root=str(root),
    )
    created = await read_project(connection, actor, inserted_id or 0)
    assert created is not None, "the project just written is not there"
    return created


async def add_root(
    connection: aiosqlite.Connection,
    actor: Actor,
    project_id: int,
    *,
    computer: str,
    root: Path | str,
) -> Project:
    """Bind a project to one location; non-empty paths belong to only one project."""
    await require(connection, actor, project_id)

    existing = await QUERIES.existing_root(
        connection,
        project_id=project_id,
        principal=actor.principal,
        computer=computer,
        root=str(root),
    )
    if existing is not None:
        project = await read_project(connection, actor, project_id)
        assert project is not None
        return project
    try:
        await QUERIES.add_root(
            connection,
            project_id=project_id,
            principal=actor.principal,
            computer=computer,
            root=str(root),
        )
    except aiosqlite.IntegrityError as error:
        raise ValueError(f"path {root!s} on {computer!r} is already registered") from error
    project = await read_project(connection, actor, project_id)
    assert project is not None, "the project just updated is gone"
    return project


async def project_contents(
    connection: aiosqlite.Connection, actor: Actor, project_id: int
) -> ProjectContents:
    await require(connection, actor, project_id)
    row = await QUERIES.project_contents(connection, project_id=project_id)
    assert row is not None
    return ProjectContents(row["tasks"], row["notes"], row["shared_notes"])


async def delete_project(connection: aiosqlite.Connection, actor: Actor, project_id: int) -> bool:

    if await read_project(connection, actor, project_id) is None:
        return False
    await require(connection, actor, project_id, role="owner")
    await QUERIES.delete_project_tasks(connection, project_id=project_id)
    await QUERIES.delete_project_notes(connection, project_id=project_id)
    await QUERIES.delete_project_project_roots(connection, project_id=project_id)
    removed = await QUERIES.delete_project(connection, project_id=project_id)
    return bool(removed)


async def update_project(
    connection: aiosqlite.Connection,
    actor: Actor,
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
        await require(connection, actor, project_id, role="editor")
        changes["updated_principal"] = actor.principal
        changes["updated_computer"] = actor.computer or hostname()
        changes["updated_at"] = time.time()

        await QUERIES.update_project(connection, project_id=project_id, changes=json.dumps(changes))
    return await read_project(connection, actor, project_id)
