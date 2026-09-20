"""Scope resolution: which project a call works on when nothing names one."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .computer import check
from .models import Origin
from .state import State
from .store import Project, Store

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
        return self.project.slug if self.project else None

    @property
    def scope_computer(self) -> str | None:
        """The computer the listing covers: the project's, or the named one; None for all."""
        if self.project:
            return self.project.computer
        return self.computer

    @property
    def label(self) -> str:
        """The scope as a phrase, for titles."""
        if self.project:
            return self.project.slug
        if self.computer:
            return f"every project on {self.computer}"
        return "every project on every computer"


class Backlog:
    """The store, the state file and one working directory, as the tools see them.

    Every handler asks this object which project it is working on. Resolution order:
    the project named on the command line, then the project pinned on this computer, then the
    deepest registered project of this computer containing the working directory, then a
    project created for it.
    """

    def __init__(
        self,
        store: Store,
        state: State,
        *,
        cwd: Path | str | None = None,
        project: str | None = None,
        computer: str | None = None,
    ) -> None:
        self.store = store
        self.state = state
        self.cwd = Path(cwd).expanduser().resolve() if cwd else Path.cwd().resolve()
        #: Named by `--project`, and so fixed for the life of the process.
        self.named = project or None
        self.computer = state.computer_name(computer)
        #: True for a view of another computer: its directories are not here, so nothing is
        #: read from the file system and nothing is registered from `cwd`.
        self.remote = False
        if store.computer is None:
            store.computer = self.computer

    def view(self, *, computer: str | None = None, project: str | None = None) -> Backlog:
        """The same store and state, seen from a computer, with a project fixed when given.

        One HTTP server answers for several computers: each request gets its own view.
        """
        seen = Backlog(
            self.store,
            self.state,
            cwd=self.cwd,
            project=project or self.named,
            computer=computer or self.computer,
        )
        seen.remote = seen.computer != self.computer
        return seen

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
        project, origin = await self.decide()
        self.state.remember(self.computer, project.slug)
        return project, origin

    async def decide(self) -> tuple[Project, Origin]:
        project, origin = await self.choose()
        if project is None:
            raise UnknownProject(
                f"no project chosen on computer {self.computer!r}: add project=<slug> to the "
                "URL, or pin one with project_use"
            )
        return project, origin

    async def choose(self) -> tuple[Project | None, Origin]:
        """The project the tools work on, or `None` with `Origin.NONE` when nothing chose one.

        Only a view of another computer ends with no project: this computer always has the
        working directory to fall back on.
        """
        if self.named:
            return await self.lookup(self.named), Origin.ARGUMENT
        if pinned := self.state.pinned(self.computer):
            found = await self.store.project_by_slug(pinned)
            if found is not None:
                return found, Origin.PINNED
            log.warning("pinned project %r no longer exists, clearing the pin", pinned)
            self.state.pin(self.computer, None)
        if self.remote:
            return None, Origin.NONE
        found = await self.store.project_for_path(self.cwd, computer=self.computer)
        if found is not None:
            return found, Origin.DIRECTORY
        return await self.create(checkout_root(self.cwd)), Origin.CREATED

    async def scope(self) -> tuple[Project | None, Origin]:
        """Like `current`, with no project as a state to report, not an error."""
        project, origin = await self.choose()
        if project is not None:
            self.state.remember(self.computer, project.slug)
        return project, origin

    async def lookup(self, reference: str) -> Project:
        """A project by slug, or by any path inside it on this computer. Raises `UnknownProject`."""
        found = await self.store.project_by_slug(reference)
        if found is not None:
            return found
        if reference.startswith((".", "/", "~")) or "/" in reference:
            path = self.absolute(reference)
            found = await self.store.project_by_root(path, computer=self.computer)
            if found is None:
                found = await self.store.project_for_path(path, computer=self.computer)
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
        if all_computers:
            return Reach()
        if project:
            selected = await self.resolve(project)
            if computer and selected.computer != check(computer):
                raise UnknownProject(
                    f"project {selected.slug!r} is on {selected.computer!r}, not {computer!r}"
                )
            return Reach(project=selected)
        if computer:
            name = check(computer)
            known = await self.store.computers()
            if name not in known and name != self.computer:
                raise UnknownProject(
                    f"no computer {name!r}: known computers are {', '.join(known) or 'none'}"
                )
            return Reach(computer=name)
        if all_projects:
            return Reach(computer=self.computer)
        return Reach(project=await self.resolve(project))

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
        """Register a directory of this computer as a project, or return the project it has.

        The directory must exist. A view of another computer cannot check that and takes the
        path as given.
        """
        path = self.absolute(root)
        existing = await self.store.project_by_root(path, computer=self.computer)
        if existing is not None:
            return existing
        if not self.remote and not path.is_dir():
            raise ValueError(f"no such directory on {self.computer!r}: {path}")
        label = name or path.name or "project"
        slug = await self.free_slug(slugify(label))
        created = await self.store.add_project(
            slug=slug, name=label, root=path, computer=self.computer, description=description
        )
        log.info("registered project %s at %s on %s", created.slug, created.root, self.computer)
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
                if self.remote:
                    raise ValueError(
                        f"on computer {self.computer!r} name the project, or give its root"
                    )
                #: Nothing named: the working directory, registered if it is new.
                path = self.absolute(self.cwd)
                found = await self.store.project_by_root(path, computer=self.computer)
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


__all__ = ["MARKERS", "Backlog", "Reach", "UnknownProject", "checkout_root", "slugify"]
