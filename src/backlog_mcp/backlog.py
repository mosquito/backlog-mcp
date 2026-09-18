"""Scope resolution: which project a call works on when nothing names one."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .models import Origin
from .state import State
from .store import Project, Store

log = logging.getLogger(__name__)

#: A directory holding one of these is the root of a checkout, and so of a project.
MARKERS = (".git", ".hg", ".svn", ".jj")


class UnknownProject(LookupError):
    """A project was named and there is no such project."""


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "project"


def checkout_root(path: Path) -> Path:
    """The nearest ancestor that looks like a checkout, else `path` itself."""
    for candidate in (path, *path.parents):
        if any((candidate / marker).exists() for marker in MARKERS):
            return candidate
    return path


class Backlog:
    """The store, the state file and one working directory, as the tools see them.

    Every handler asks this object which project it is working on. Resolution order:
    the project named on the command line, then the pinned project, then the deepest
    registered project containing the working directory, then a project created for it.
    """

    def __init__(
        self,
        store: Store,
        state: State,
        *,
        cwd: Path | str | None = None,
        project: str | None = None,
    ) -> None:
        self.store = store
        self.state = state
        self.cwd = Path(cwd).expanduser().resolve() if cwd else Path.cwd().resolve()
        #: Named by `--project`, and so fixed for the life of the process.
        self.named = project or None

    def absolute(self, path: Path | str) -> Path:
        """A path as given, made canonical against the working directory."""
        found = Path(path).expanduser()
        if not found.is_absolute():
            found = self.cwd / found
        return found.resolve()

    async def current(self) -> tuple[Project, Origin]:
        """The project the tools work on, and why that one."""
        project, origin = await self.decide()
        self.state.remember(project.slug)
        return project, origin

    async def decide(self) -> tuple[Project, Origin]:
        if self.named:
            return await self.lookup(self.named), Origin.ARGUMENT
        if pinned := self.state.pinned:
            found = await self.store.project_by_slug(pinned)
            if found is not None:
                return found, Origin.PINNED
            log.warning("pinned project %r no longer exists, clearing the pin", pinned)
            self.state.pin(None)
        found = await self.store.project_for_path(self.cwd)
        if found is not None:
            return found, Origin.DIRECTORY
        return await self.create(checkout_root(self.cwd)), Origin.CREATED

    async def lookup(self, reference: str) -> Project:
        """A project by slug, or by any path inside it. Raises `UnknownProject`."""
        found = await self.store.project_by_slug(reference)
        if found is not None:
            return found
        if reference.startswith((".", "/", "~")) or "/" in reference:
            path = self.absolute(reference)
            found = await self.store.project_by_root(path)
            if found is None:
                found = await self.store.project_for_path(path)
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

    async def free_slug(self, wanted: str) -> str:
        taken = set(await self.store.slugs())
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
        """Register a directory as a project, or return the project already registered for it."""
        path = self.absolute(root)
        existing = await self.store.project_by_root(path)
        if existing is not None:
            return existing
        label = name or path.name or "project"
        slug = await self.free_slug(slugify(label))
        created = await self.store.add_project(
            slug=slug, name=label, root=path, description=description
        )
        log.info("registered project %s at %s", created.slug, created.root)
        return created

    async def save(
        self,
        *,
        reference: str | None = None,
        root: Path | str | None = None,
        name: str | None = None,
        description: str | None = None,
        archived: bool | None = None,
    ) -> Project:
        """Register a project, or update the one named. Defaults to the working directory."""
        match reference, root:
            case None | "", None:
                #: Nothing named: the working directory, registered if it is new.
                path = self.absolute(self.cwd)
                found = await self.store.project_by_root(path)
                project = found if found is not None else await self.create(checkout_root(path))
            case None | "", (str() | Path()) as directory:
                project = await self.create(directory, name=name, description=description or "")
            case str() as named, _:
                project = await self.lookup(named)
        updated = await self.store.update_project(
            project.id, name=name, description=description, archived=archived
        )
        assert updated is not None, "the project just updated is gone"
        return updated


__all__ = ["MARKERS", "Backlog", "UnknownProject", "checkout_root", "slugify"]
