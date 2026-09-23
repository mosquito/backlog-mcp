"""The command line: serve MCP, or connect to it over HTTP or local stdio."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import AsyncGenerator, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import argclass
import rtoml
from aiohttp import ClientError as HTTPClientError
from aiohttp_tiny_mcp import ClientError, elicit_accept
from rich.prompt import Confirm, Prompt

from . import embeddings, httpd, paths, skills
from .auth import user_identity
from .backlog import Backlog, UnknownProject, slugify
from .client import Connection, Registration, ToolError, bind_directory, connect, directory_project
from .models import (
    OPEN_STATUSES,
    CommentOut,
    Comments,
    Deleted,
    Event,
    FullNotes,
    FullTasks,
    Identity,
    LinkChange,
    NoteOut,
    Priority,
    ProjectDeleted,
    ProjectOut,
    Relation,
    Status,
    TaskDetails,
    TaskOut,
    Tasks,
)
from .models import (
    Projects as ProjectList,
)
from .models import (
    Stats as StatsOut,
)
from .names import default_user, principal_label
from .records import NOTE_LIMIT, Note, Task, check_note
from .render import (
    board_panel,
    comment_panel,
    comments_table,
    console,
    errors,
    event_line,
    fail,
    fields,
    note,
    note_panel,
    notes_table,
    projects_table,
    skill_panel,
    skills_table,
    stats_view,
    task_panel,
    tasks_table,
    warn,
)
from .server import (
    NOTE_CHANGED,
    PROJECT_CHANGED,
    TASK_CHANGED,
    VERSION,
    bound,
    registry,
    use,
)

log = logging.getLogger("backlog_mcp")

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

STATUSES = tuple(item.value for item in Status)
PRIORITIES = tuple(item.value for item in Priority)

FORM = """\
Title: {title}
Status: {status}
Priority: {priority}
Tags: {tags}

{body}

# Everything above the blank line sets a field; everything below it is the description.
# Lines starting with '#' are dropped. An empty title cancels the task.
"""

HEADERS = ("title", "status", "priority", "tags")

#: The hints sit on top: the body is markdown, so a '#' line below the headers is a heading.
NOTE_FORM = """\
# Everything above the blank line sets a field; everything below it is the note, in markdown.
# Shared: yes makes the note visible from every project. An empty title cancels the note.
# A note is a short fact: the body holds at most {limit} characters.
Title: {title}
Tags: {tags}
Shared: {shared}

{body}
"""

NOTE_HEADERS = ("title", "tags", "shared")

YES = ("yes", "y", "true", "1")


def repeated(*aliases: str, help: str, choices: tuple[str, ...] | None = None) -> list[str]:
    """A flag that may be repeated, such as `-t one -t two`. Every value is kept."""
    settings: dict[str, Any] = {
        "action": "append",
        "type": str,
        "nargs": None,
        "default": [],
        "help": help,
    }
    if choices is not None:
        settings["choices"] = choices
    return cast(list[str], argclass.Argument(*aliases, **settings))


def secret_accounts(value: str | Sequence[str]) -> list[str]:
    """Normalize environment defaults and keep every account masked in repr."""
    values = shlex.split(value) if isinstance(value, str) else value
    return [argclass.SecretString(str(item)) for item in values]


class BasicAccountsAction(argparse.Action):
    """Extend nargs values, including string defaults supplied by the environment."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        accounts = secret_accounts(getattr(namespace, self.dest, []) or [])
        setattr(namespace, self.dest, [*accounts, *secret_accounts(values)])


def build(options: CLI) -> Backlog:
    """The backlog the global options describe."""
    principal = "local"
    auth = options.mcp.auth
    authorized_http = options.mcp.protocol == "http" and (
        auth.basic or auth.jwt.public_key or auth.jwt.pre_shared_key
    )
    configured_user = (
        options.mcp.default_user_override
        if options.mcp.default_user_override is not None
        else options.default_user
    )
    if authorized_http and configured_user is not None:
        raise ValueError("HTTP --default-user cannot be combined with authentication")
    if not authorized_http:
        username = default_user(configured_user)
        principal = user_identity(username)
    return Backlog(
        options.database or paths.database_path(),
        cwd=options.root,
        project=options.project,
        computer=options.computer,
        principal=principal,
    )


def editor() -> list[str]:
    """The editor to open, from `VISUAL`, then `EDITOR`, then `vi`."""
    chosen = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    return chosen.split()


def compose(text: str) -> str | None:
    """Open the editor on this text and return what came back, or `None` when nothing changed."""
    with tempfile.NamedTemporaryFile("w+", suffix=".backlog.md", delete=False) as handle:
        handle.write(text)
        name = handle.name
    try:
        subprocess.call([*editor(), name])
        written = Path(name).read_text(encoding="utf-8")
    finally:
        Path(name).unlink(missing_ok=True)
    return None if written.strip() == text.strip() else written


def split_form(
    text: str, headers: tuple[str, ...], *, markdown: bool = False
) -> tuple[dict[str, str], str]:
    """Split an edited form into its `Key: value` headers and the text below them.

    Lines starting with '#' are hints and are dropped. A markdown body keeps them: they are its
    headings, so only the hints above the body go.
    """
    lines = text.splitlines()
    if not markdown:
        lines = [line for line in lines if not line.startswith("#")]
    while lines and not lines[0].strip():
        lines.pop(0)
    found: dict[str, str] = {}
    rest = len(lines)
    for index, line in enumerate(lines):
        if markdown and line.startswith("#"):
            continue
        key, separator, value = line.partition(":")
        name = key.strip().lower()
        if not separator or name not in headers:
            rest = index
            break
        found[name] = value.strip()
    return found, "\n".join(lines[rest:]).strip()


def split_list(text: str) -> list[str]:
    return [item.strip() for item in text.split(",") if item.strip()]


def parse_form(text: str) -> dict[str, Any]:
    """Read the edited task form back: the headers at the top, the description below them."""
    headers, body = split_form(text, HEADERS)
    return {
        "title": headers.get("title", ""),
        "body": body,
        "status": Status(headers.get("status", "").lower() or Status.TODO),
        "priority": Priority(headers.get("priority", "").lower() or Priority.NORMAL),
        "tags": split_list(headers.get("tags", "")),
    }


def parse_note_form(text: str) -> dict[str, Any]:
    """Read the edited note form back: title, tags and the shared flag, then the markdown."""
    headers, body = split_form(text, NOTE_HEADERS, markdown=True)
    return {
        "title": headers.get("title", ""),
        "body": body,
        "tags": split_list(headers.get("tags", "")),
        "shared": headers.get("shared", "").lower() in YES,
    }


def form_of(task: Task | TaskOut | None = None) -> str:
    """The form to edit: empty for a new task, filled in for one that exists."""
    if task is None:
        return FORM.format(
            title="", status=Status.TODO.value, priority=Priority.NORMAL.value, tags="", body=""
        )
    return FORM.format(
        title=task.title,
        status=task.status.value,
        priority=task.priority.value,
        tags=", ".join(task.tags),
        body=task.body,
    )


def note_form_of(existing: Note | NoteOut | None = None) -> str:
    """The note form to edit: empty for a new note, filled in for one that exists."""
    if existing is None:
        return NOTE_FORM.format(title="", tags="", shared="no", body="", limit=NOTE_LIMIT)
    return NOTE_FORM.format(
        limit=NOTE_LIMIT,
        title=existing.title,
        tags=", ".join(existing.tags),
        shared="yes" if existing.shared else "no",
        body=existing.body,
    )


class ConfigTemplate(argclass.TOMLConfigGenerator):
    """The TOML the config files are read as, with the log level written as a lowercase name.

    Only the root options and the tables in `TABLES` are written: the other commands take
    their options on the command line. A log level given on the command line or in the
    environment arrives as an integer. `--log-level` accepts names only, so an integer would
    not read back.
    """

    #: The subcommands whose options a config file may set.
    TABLES = frozenset({"mcp"})

    def render(self, fields: Sequence[argclass.ConfigField]) -> str:
        kept = [
            self.named(field)
            for field in fields
            if not field.attr_path[:-1] or field.attr_path[0] in self.TABLES
        ]
        return super().render(kept)

    @staticmethod
    def named(field: argclass.ConfigField) -> argclass.ConfigField:
        if field.key != "log_level" or field.value is None:
            return field
        value = field.value
        if isinstance(value, int):
            value = logging.getLevelName(value)
        return dataclasses.replace(field, value=str(value).lower())


class Command(argclass.Parser):
    """One subcommand, using the MCP connection opened by the root parser."""

    def options(self) -> CLI:
        """The root parser, however deep this command is nested."""
        parent = self.__parent__
        while isinstance(parent, Command):
            parent = parent.__parent__
        assert isinstance(parent, CLI), "a command runs under the root parser"
        return parent

    def reference(self) -> str | None:
        """The project this command works on: the `--project` of the root parser."""
        return self.options().project

    def listing(self) -> dict[str, Any]:
        return {
            "project": self.reference(),
            "all_projects": bool(getattr(self, "all_projects", False)),
            "computer": getattr(self, "at", None) or None,
            "all_computers": bool(getattr(self, "all_computers", False)),
        }

    @property
    def connection(self) -> Connection:
        return self.options().connection

    async def ask(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        params = request.get("params") or request
        message = str(params.get("message", "Confirm this operation?"))
        accepted = bool(getattr(self, "yes", False)) or Confirm.ask(message, default=False)
        return elicit_accept({"ok": accepted})

    def registration(self, computer: str, root: Path, projects: list[ProjectOut]) -> Registration:
        return prompt_registration(computer, root, projects)

    async def task(self, task_id: int) -> TaskDetails:
        task = await self.connection.call(TaskDetails, "task_get", id=task_id)
        if task.status == Status.DELETED:
            raise ToolError(f"no task #{task_id}")
        return task

    def label(self, result: FullTasks | FullNotes | embeddings.Matches | StatsOut) -> str:
        if result.project:
            return result.project
        if getattr(self, "all_computers", False):
            return "every project on every computer"
        computer = getattr(self, "at", None) or result.computer
        return f"every project on {computer}"

    def show_help(self) -> int:
        """Print the help of this command, the way `--help` after it does.

        Only the root parser renders a subparser's help, so this asks it to parse `--help`
        behind the words that lead here, and swallows the exit that argparse then raises.
        """
        words: list[str] = []
        parser: argclass.Parser = self
        while isinstance(parser, Command):
            parent = parser.__parent__
            assert isinstance(parent, argclass.Parser), "a command runs under the root parser"
            words[:0] = [name for name, item in parent.__subparsers__.items() if item is parser]
            parser = parent
        with contextlib.suppress(SystemExit):
            parser.parse_args([*words, "--help"])
        return 0

    async def __call__(self) -> int:
        raise NotImplementedError


class HTTPOptions(argclass.Group):
    """HTTP listener and browser access settings."""

    listen: str = argclass.Argument(default="127.0.0.1", help="Address to listen on, HTTP only")
    port: int = argclass.Argument(default=3009, help="Port to listen on, HTTP only")
    no_console: bool = argclass.Argument(
        default=False, help="Do not serve the bundled MCP console at /console"
    )
    trust_proxy: bool = argclass.Argument(
        default=False,
        help=(
            "Behind a TLS-terminating reverse proxy: skip the Origin check of the endpoint, "
            "the proxy validates origins instead"
        ),
    )


class JWTOptions(argclass.Group):
    """Independent JWT verification keys and common claim requirements."""

    public_key: str = argclass.Argument(
        default="", help="JWT PEM public key or path to its file; algorithms follow the key type"
    )
    pre_shared_key: str = argclass.Secret(
        default="",
        help="JWT HS256 pre-shared key, at least 32 bytes; independent of public-key JWT",
    )
    issuer: str = argclass.Argument(
        default="", help="Required JWT issuer; empty accepts any issuer"
    )
    audience: str = argclass.Argument(
        default="", help="Required JWT audience; empty disables the audience check"
    )


class AuthOptions(argclass.Group):
    """Credentials accepted by the HTTP server."""

    basic: list[str] = argclass.Secret(
        action=BasicAccountsAction,
        nargs="+",
        type=str,
        converter=secret_accounts,
        default=[],
        metavar="USERNAME:PASSWORD",
        help="Basic accounts, HTTP only; accepts multiple values or repeated flags alongside JWT",
    )

    jwt = JWTOptions(title="JWT authentication")


class Serve(Command):
    """Run the MCP server: on stdio by default, or over HTTP with `--protocol=http`.

    Groups map to `[mcp.http]`, `[mcp.auth]` and `[mcp.auth.jwt]` in TOML.
    Environment variables use the same group prefixes after `BACKLOG_MCP_`.
    """

    @property
    def backlog(self) -> Backlog:
        return bound()

    protocol: str = argclass.Argument(
        default="stdio",
        choices=("stdio", "http"),
        help="stdio speaks MCP on stdin and stdout; http serves the endpoint and the console",
    )
    default_user_override: str | None = argclass.Argument(
        "--default-user",
        default=None,
        type=str,
        help="Username for stdio or HTTP without authorizers (default: global option, then $USER)",
    )
    http = HTTPOptions(title="HTTP transport")
    auth = AuthOptions(title="HTTP authentication")
    generate_config: str = argclass.Argument(
        action=argclass.GenerateConfigAction,
        generator=ConfigTemplate,
        metavar="FILE",
        help=(
            "Write a TOML config file with these options in its [mcp] tables and exit; '-' "
            "prints it. Options given before this flag are written active, the rest as "
            "commented-out defaults"
        ),
    )

    async def stdio(self) -> int:
        """MCP on stdin and stdout, the transport every client starts by itself."""
        from aiohttp_tiny_mcp import run_stdio

        from .server import ACTIVE_REGISTRY, persistent_registry

        backlog = self.backlog
        log.info("serving %s on stdio, database %s", registry.info.name, backlog.database.path)

        configured, storage = persistent_registry(backlog)
        token = ACTIVE_REGISTRY.set(configured)
        try:
            await storage.open()
            await storage.sweep()
            await run_stdio(configured)
        finally:
            ACTIVE_REGISTRY.reset(token)
            await storage.close()
        return 0

    async def __call__(self) -> int:
        match self.protocol:
            case "http":
                await httpd.serve(
                    self.backlog,
                    address=self.http.listen,
                    port=self.http.port,
                    basic=[str(account) for account in self.auth.basic],
                    jwt_public_key=self.auth.jwt.public_key,
                    jwt_pre_shared_key=str(self.auth.jwt.pre_shared_key),
                    jwt_issuer=self.auth.jwt.issuer,
                    jwt_audience=self.auth.jwt.audience,
                    console=not self.http.no_console,
                    trust_proxy=self.http.trust_proxy,
                )
            case _:
                await self.stdio()
        return 0


class Whoami(Command):
    """Show the identity used by this connection without registering a project."""

    async def __call__(self) -> int:
        identity = await self.connection.call(Identity, "whoami")
        console.print(
            fields(
                [
                    ("principal", principal_label(identity.principal, identity.computer)),
                    ("auth_type", identity.auth_type),
                    ("handle", identity.handle),
                    ("assigned_tasks", str(identity.assigned_tasks)),
                ]
            )
        )
        return 0


class Board(Command):
    """Show project context, counts and ready/claimed tasks."""

    project: str | None = argclass.Argument(
        "project",
        nargs="?",
        default=None,
        type=str,
        help="Project reference; omit for this checkout",
    )

    async def __call__(self) -> int:
        console.print(board_panel(await self.connection.board(self.project)))
        return 0


def matching_projects(name: str, projects: list[ProjectOut]) -> list[ProjectOut]:
    exact = [project for project in projects if project.full_name == name or project.slug == name]
    return exact or [
        project
        for project in projects
        if project.name.casefold() == name.casefold() or project.slug == slugify(name)
    ]


def prompt_registration(computer: str, root: Path, projects: list[ProjectOut]) -> Registration:
    """Collect registration details before any project or task is written."""
    if not sys.stdin.isatty():
        raise ToolError(
            f"no project registered for {root} on {computer!r}; "
            "run `backlog init NAME` or `backlog init --existing SLUG` first"
        )
    try:
        while True:
            value = Prompt.ask("Input project directory", default=str(root), console=errors)
            directory = Path(value.strip()).expanduser()
            directory = (root / directory).resolve()
            if directory.is_dir():
                root = directory
                break
            errors.print(f"No such directory: {directory}", markup=False)
        if existing := directory_project(computer, root, projects):
            errors.print(f"This directory belongs to {existing.full_name!r}.", markup=False)
            if not Confirm.ask("Use this project?", default=True, console=errors):
                raise ToolError("registration cancelled")
            return Registration(project=existing.full_name, computer=computer, root=root)
        name = ""
        while not name:
            name = Prompt.ask(
                "Input project name", default=root.name or "project", console=errors
            ).strip()
        matches = matching_projects(name, projects)
        if matches:
            if len(matches) > 1:
                slug = Prompt.ask(
                    "Input existing project slug",
                    choices=[item.full_name for item in matches],
                    console=errors,
                )
                existing = next(item for item in matches if item.full_name == slug)
            else:
                existing = matches[0]
            errors.print(f"Existing project: {existing.name} ({existing.full_name})", markup=False)
            computers = ", ".join(sorted({item.computer for item in existing.roots})) or "none"
            errors.print(f"Registered computers: {computers}", markup=False)
            if not Confirm.ask(
                "Bind this directory to the existing project?", default=True, console=errors
            ):
                raise ToolError(
                    "registration cancelled; choose another name to create a new project"
                )
            return Registration(project=existing.full_name, computer=computer, root=root)
        description = Prompt.ask(
            "Input project description", default="", show_default=False, console=errors
        )
        if not Confirm.ask("Register this project?", default=True, console=errors):
            raise ToolError("registration cancelled")
        return Registration(name=name, description=description, computer=computer, root=root)
    except EOFError:
        raise ToolError("registration cancelled") from None


class Init(Command):
    """Register the current checkout, interactively or with explicit arguments."""

    project_name: str = argclass.Argument(
        "project_name", nargs="?", default="", help="New project name"
    )
    details: str = argclass.Argument("--description", default="", help="New project description")
    existing: str = argclass.Argument(
        default="", help="Bind this checkout to an existing project slug"
    )

    def registration(self, computer: str, root: Path, projects: list[ProjectOut]) -> Registration:
        if self.project_name and self.existing:
            raise ToolError("choose a new project name or --existing, not both")
        if self.existing:
            return Registration(project=self.existing)
        if self.project_name:
            name = self.project_name.strip()
            if not name:
                raise ToolError("project name must not be empty")
            if matches := matching_projects(name, [p for p in projects if p.role == "owner"]):
                raise ToolError(
                    f"project already exists; use `backlog init --existing {matches[0].full_name}` "
                    "to bind this directory, or choose a different name"
                )
            return Registration(name=name, description=self.details)
        return super().registration(computer, root, projects)

    async def __call__(self) -> int:
        if self.project_name and self.existing:
            raise ToolError("choose a new project name or --existing, not both")
        if self.project_name and not self.project_name.strip():
            raise ToolError("project name must not be empty")
        if self.existing:
            known = await self.connection.call(
                ProjectList, "project_list", archived=True, all_computers=True
            )
            matches = [
                item
                for item in known.items
                if self.existing in {item.full_name, item.slug, f"#{item.id}"}
            ]
            if not matches:
                raise ToolError(f"no project {self.existing!r}")
            if len(matches) > 1:
                names = ", ".join(item.full_name for item in matches)
                raise ToolError(f"ambiguous project {self.existing!r}; use its full name: {names}")
            self.existing = matches[0].full_name
        project = await bind_directory(self.connection, self.registration)
        if self.existing and self.existing != project.full_name:
            raise ToolError(f"this directory already belongs to {project.slug!r}")
        if self.project_name and not matching_projects(self.project_name, [project]):
            raise ToolError(f"this directory already belongs to {project.slug!r}")
        note(f"registered {project.full_name!r} on {self.connection.computer}")
        return 0


def computer_options() -> tuple[str, bool]:
    """The two arguments that widen a listing to another computer, or to all of them.

    The class annotations do not reach here, so the type and the action are explicit.
    """
    at: str = argclass.Argument(
        "--computer",
        type=str,
        default="",
        help="Every project on that computer instead of this one",
    )
    all_computers: bool = argclass.Argument(
        "--all-computers",
        action=argclass.Actions.STORE_TRUE,
        default=False,
        help="Every project on every computer",
    )
    return at, all_computers


class ProjectDelete(Command):
    """Delete a project with every task and note on its board."""

    target: str = argclass.Argument("target", help="Project slug, or a path inside it")
    yes: bool = argclass.Argument("-y", "--yes", default=False, help="Delete without asking")

    async def __call__(self) -> int:
        result = await self.connection.call(ProjectDeleted, "project_delete", project=self.target)
        if not result.deleted:
            warn("nothing deleted")
            return 1
        note(
            f"deleted project {result.full_name!r}: {result.tasks} task(s), {result.memory} note(s)"
        )
        return 0


class ProjectUpdate(Command):
    """Set or clear the language for project work and task text."""

    target: str = argclass.Argument("target", help="Project slug, or a path inside it")
    preferred_language: str | None = argclass.Argument(
        type=str, default=None, help="Language for project work and task text, such as ru or en"
    )
    clear_preferred_language: bool = argclass.Argument(
        default=False, help="Clear the project language preference"
    )

    async def __call__(self) -> int:
        if (self.preferred_language is not None) == self.clear_preferred_language:
            raise ToolError("choose --preferred-language or --clear-preferred-language")
        project = await self.connection.call(
            ProjectOut,
            "project_save",
            exclude_none=False,
            project=self.target,
            preferred_language=self.preferred_language,
        )
        note(f"{project.full_name}: preferred language = {project.preferred_language or 'none'}")
        return 0


class Projects(Command):
    """List the registered projects of this computer, or of another, or of all."""

    archived: bool = argclass.Argument(default=False, help="Include archived projects")
    at, all_computers = computer_options()
    update = ProjectUpdate(aliases=["pu"], help="Set or clear the project language preference")
    delete = ProjectDelete(
        aliases=["pd"],
        help="Delete a project for good",
        description=(
            "Delete a project with every task and note on its board, and free its directories "
            "for a new registration. Asks first unless --yes is given. Prefer archiving through "
            "the MCP tool project_save when the history should stay."
        ),
    )

    async def __call__(self) -> int:
        found = await self.connection.call(
            ProjectList,
            "project_list",
            archived=self.archived,
            computer=self.at or None,
            all_computers=self.all_computers,
        )
        console.print(projects_table(found.items, computer=self.connection.computer))
        return 0


class List(Command):
    """List tasks, open ones first, of this project or of every project."""

    all_projects: bool = argclass.Argument(
        "-a", "--all-projects", default=False, help="List tasks of every project on this computer"
    )
    at, all_computers = computer_options()
    status: list[str] = repeated(
        "--status",
        choices=STATUSES,
        help="Status to show. Repeat for more (default: the open ones)",
    )
    every: bool = argclass.Argument("--every", default=False, help="Show closed tasks as well")
    tag: str = argclass.Argument(default="", help="Only tasks carrying this tag")
    priority: str = argclass.Argument(default="", choices=("", *PRIORITIES), help="Only this one")
    limit: int = argclass.Argument(default=50, help="Most tasks to print")

    def wanted(self) -> list[Status] | None:
        if self.status:
            return [Status(item) for item in self.status]
        return None if self.every else list(OPEN_STATUSES)

    async def __call__(self) -> int:
        found = await self.connection.call(
            FullTasks,
            "task_list",
            **self.listing(),
            status=self.wanted() or [item for item in Status if item != Status.DELETED],
            tag=self.tag or None,
            priority=self.priority or None,
            limit=self.limit,
            full=True,
        )
        console.print(
            tasks_table(
                found.items,
                title=f"{len(found.items)} task(s) of {self.label(found)}",
                wide=found.project is None,
                computer=self.connection.computer,
            )
        )
        return 0


class Search(Command):
    """FTS and semantic search across projects, tasks, comments, and memory."""

    kind: list[str] = repeated(
        "--kind",
        choices=("tasks", "comments", "memory", "projects"),
        help="Content to search; repeat for several kinds (default: all)",
    )
    query: list[str] = argclass.Argument("query", nargs="+", help="Words or meaning to search for")
    all_projects: bool = argclass.Argument(
        "-a", "--all-projects", default=False, help="Search every project on this computer"
    )
    at, all_computers = computer_options()
    tag: str = argclass.Argument(
        default="", help="Filter by task or note tags; comments use their task tags"
    )
    limit: int = argclass.Argument(default=20, help="Most matches to print, of each kind")
    min_score: float | None = argclass.Argument(
        default=None,
        help="Minimum semantic similarity (-1 to 1); default: 0.15, or 0.25 with FTS matches",
    )

    async def __call__(self) -> int:
        text = " ".join(self.query)
        matches = await self.connection.call(
            embeddings.Matches,
            "search",
            **self.listing(),
            query=text,
            kinds=self.kind or None,
            tag=self.tag or None,
            limit=self.limit,
            min_score=self.min_score,
        )
        where = self.label(matches)
        if not self.kind or "tasks" in self.kind:
            console.print(
                tasks_table(
                    matches.tasks,
                    title=f"{len(matches.tasks)} task match(es) for {text!r} in {where}",
                    wide=matches.project is None,
                    computer=self.connection.computer,
                )
            )
        if matches.projects or "projects" in self.kind:
            table = projects_table(matches.projects, computer=self.connection.computer)
            table.title = f"{len(matches.projects)} project match(es) for {text!r} in {where}"
            console.print(table)
        if matches.comments or "comments" in self.kind:
            console.print(
                comments_table(
                    matches.comments,
                    title=f"{len(matches.comments)} comment match(es) for {text!r} in {where}",
                )
            )
        if matches.memory or "memory" in self.kind:
            console.print(
                notes_table(
                    matches.memory,
                    title=f"{len(matches.memory)} note match(es) for {text!r} in {where}",
                    wide=matches.project is None
                    or any(n.project != matches.project for n in matches.memory),
                    computer=self.connection.computer,
                )
            )
        return 0


class Show(Command):
    """Print one task in full."""

    id: int = argclass.Argument("id", type=int, help="Task id")

    async def __call__(self) -> int:
        console.print(task_panel(await self.task(self.id), computer=self.connection.computer))
        return 0


class Assigned(Command):
    """List assigned tasks across accessible projects, including closed tasks."""

    prefix: str | None = argclass.Argument(
        default=None, type=str, help="Assignment prefix; omit for your principal"
    )
    limit: int = argclass.Argument(default=50, type=int, help="Maximum tasks to return")
    offset: int = argclass.Argument(default=0, type=int, help="Skip this many matching tasks")

    async def __call__(self) -> int:
        result = await self.connection.call(
            Tasks, "task_assigned", prefix=self.prefix, limit=self.limit, offset=self.offset
        )
        console.print(
            tasks_table(
                [TaskOut(**item.model_dump()) for item in result.items],
                title=f"{result.count} assigned task(s)",
                wide=True,
            )
        )
        return 0


class Claim(Command):
    """Replace a task assignment only when its current value matches --expected."""

    id: int = argclass.Argument("id", type=int, metavar="TASK", help="Task id")
    expected: str = argclass.Argument(
        required=True, help="Current username@computer:marker, or null"
    )
    assigned: str = argclass.Argument(
        required=True, help="New username@computer:marker, or null to release"
    )

    async def __call__(self) -> int:
        result = await self.connection.call(
            TaskOut,
            "task_claim",
            exclude_none=False,
            task=self.id,
            expected=None if self.expected == "null" else self.expected,
            assigned=None if self.assigned == "null" else self.assigned,
        )
        console.print(task_panel(result, computer=self.connection.computer))
        return 0


class Link(Command):
    """Create a relation and its inverse, or change their reason."""

    source: int = argclass.Argument("source", type=int, metavar="TASK", help="Source task id")
    relation: str = argclass.Argument("relation", choices=[item.value for item in Relation])
    target: int = argclass.Argument("target", type=int, help="Target task id")
    reason: str | None = argclass.Argument(
        default=None, type=str, help="Explanation; omit to preserve it, pass empty text to clear"
    )

    async def __call__(self) -> int:
        result = await self.connection.call(
            LinkChange,
            "task_link",
            task=self.source,
            relation=self.relation,
            target=self.target,
            reason=self.reason,
        )
        note("link saved" if result.changed else "link unchanged")
        return 0


class Unlink(Command):
    """Remove a relation and its inverse."""

    source: int = argclass.Argument("source", type=int, metavar="TASK", help="Source task id")
    relation: str = argclass.Argument("relation", choices=[item.value for item in Relation])
    target: int = argclass.Argument("target", type=int, help="Target task id")

    async def __call__(self) -> int:
        result = await self.connection.call(
            LinkChange,
            "task_unlink",
            task=self.source,
            relation=self.relation,
            target=self.target,
        )
        note("link removed" if result.changed else "link absent")
        return 0


class CommentAdd(Command):
    """Add a Markdown comment without changing the task description."""

    id: int = argclass.Argument("id", type=int, help="Task id")
    body: list[str] = argclass.Argument("body", nargs="+", help="Comment text")

    async def __call__(self) -> int:
        comment = await self.connection.call(
            CommentOut,
            "task_comment_add",
            task_id=self.id,
            body=" ".join(self.body),
        )
        console.print(comment_panel(comment))
        return 0


class CommentList(Command):
    """Read all comments and history in chronological order."""

    id: int = argclass.Argument("id", type=int, help="Task id")

    async def __call__(self) -> int:
        result = await self.connection.call(
            Comments,
            "task_comment_list",
            task_id=self.id,
        )
        for item in result.items:
            console.print(comment_panel(item))
        note(f"{result.count} comments and history entries")
        return 0


class Add(Command):
    """Add a task from the command line, without opening an editor."""

    title: list[str] = argclass.Argument("title", nargs="+", help="Title of the task")
    body: str = argclass.Argument("-b", "--body", default="", help="Description")
    status: str = argclass.Argument("-s", "--status", default=Status.TODO.value, choices=STATUSES)
    priority: str = argclass.Argument(default=Priority.NORMAL.value, choices=PRIORITIES)
    tag: list[str] = repeated("-t", "--tag", help="Tag to attach. Repeat for more than one")

    async def __call__(self) -> int:
        task = await self.connection.call(
            TaskOut,
            "task_add",
            project=self.reference(),
            title=" ".join(self.title),
            body=self.body,
            status=self.status,
            priority=self.priority,
            tags=self.tag,
        )
        note(f"added #{task.id} to {task.project}")
        console.print(task_panel(task, computer=self.connection.computer))
        return 0


class Create(Command):
    """Write a task in `$EDITOR`, from a filled-in form."""

    async def __call__(self) -> int:
        written = compose(form_of())
        if written is None:
            warn("nothing written, no task added")
            return 1
        fields = parse_form(written)
        if not fields["title"]:
            warn("the title is empty, no task added")
            return 1
        task = await self.connection.call(TaskOut, "task_add", project=self.reference(), **fields)
        note(f"added #{task.id} to {task.project}")
        console.print(task_panel(task, computer=self.connection.computer))
        return 0


class Edit(Command):
    """Edit a task in `$EDITOR`."""

    id: int = argclass.Argument("id", type=int, help="Task id")

    async def __call__(self) -> int:
        task = await self.task(self.id)
        written = compose(form_of(task))
        if written is None:
            warn("nothing changed")
            return 0
        fields = parse_form(written)
        if not fields["title"]:
            warn("the title is empty, nothing changed")
            return 1
        changed = await self.connection.call(TaskOut, "task_update", id=task.id, **fields)
        note(f"updated #{changed.id}")
        console.print(task_panel(changed, computer=self.connection.computer))
        return 0


class Transition(Command):
    """Base of the commands that only move a task from one status to another."""

    ids: list[int] = argclass.Argument("ids", type=int, nargs="+", help="Task ids")
    target = Status.DONE

    async def __call__(self) -> int:
        failed = 0
        for task_id in self.ids:
            try:
                task = await self.connection.call(
                    TaskOut, "task_update", id=task_id, status=self.target
                )
            except ToolError as error:
                fail(str(error))
                failed += 1
                continue
            note(f"#{task.id} is {task.status.value}: {task.title}")
        return 1 if failed else 0


class Close(Transition):
    """Mark tasks done."""

    target = Status.DONE


class Cancel(Transition):
    """Mark tasks cancelled: kept, but closed."""

    target = Status.CANCELLED


class Start(Transition):
    """Mark tasks in progress."""

    target = Status.DOING


class Reopen(Transition):
    """Put tasks back on the pile."""

    target = Status.TODO


class Block(Transition):
    """Mark tasks blocked."""

    target = Status.BLOCKED


class Move(Command):
    """Move a task into another project."""

    id: int = argclass.Argument("id", type=int, help="Task id")
    destination: str = argclass.Argument("destination", help="Project slug, or a path inside it")

    async def __call__(self) -> int:
        existing = await self.task(self.id)
        task = await self.connection.call(
            TaskOut, "task_move", id=self.id, project=self.destination
        )
        if existing.project == task.project:
            note(f"#{task.id} already belongs to {task.project}")
        else:
            note(f"#{task.id} now belongs to {task.project}")
        return 0


class Delete(Command):
    """Delete a task for good."""

    id: int = argclass.Argument("id", type=int, help="Task id")
    yes: bool = argclass.Argument(
        "-y",
        "--yes",
        default=False,
        help="Confirm deletion from another project",
    )

    async def __call__(self) -> int:
        task = await self.task(self.id)
        current = (await self.connection.board()).project
        if (
            task.project != current.full_name
            and not self.yes
            and not Confirm.ask(
                f"delete task #{task.id} {task.title!r} from {task.project!r}?",
                default=False,
            )
        ):
            warn("nothing deleted")
            return 1
        await self.connection.call(Deleted, "task_delete", id=self.id)
        note(f"deleted #{self.id}")
        return 0


class Stats(Command):
    """Open tasks, closed tasks, notes, and detailed task counts."""

    all_projects: bool = argclass.Argument(
        "-a", "--all-projects", default=False, help="Count every project on this computer together"
    )
    at, all_computers = computer_options()

    async def __call__(self) -> int:
        result = await self.connection.call(StatsOut, "stats", **self.listing())
        console.print(stats_view(result, where=self.label(result)))
        return 0


class NoteList(Command):
    """List the notes of this project and the shared ones, or of every project."""

    all_projects: bool = argclass.Argument(
        "-a", "--all-projects", default=False, help="List notes of every project on this computer"
    )
    at, all_computers = computer_options()
    tag: str = argclass.Argument(default="", help="Only notes carrying this tag")
    shared: bool = argclass.Argument("--shared", default=False, help="Only shared notes")
    limit: int = argclass.Argument(default=50, help="Most notes to print")

    async def __call__(self) -> int:
        found = await self.connection.call(
            FullNotes,
            "memory_list",
            **self.listing(),
            tag=self.tag or None,
            shared=True if self.shared else None,
            limit=self.limit,
            full=True,
        )
        console.print(
            notes_table(
                found.items,
                title=f"{len(found.items)} note(s) of {self.label(found)}",
                wide=found.project is None or any(n.project != found.project for n in found.items),
                computer=self.connection.computer,
            )
        )
        return 0


class NoteShow(Command):
    """Print one note in full."""

    id: int = argclass.Argument("id", type=int, help="Note id")

    async def __call__(self) -> int:
        found = await self.connection.call(NoteOut, "memory_get", id=self.id)
        console.print(note_panel(found, computer=self.connection.computer))
        return 0


class NoteAdd(Command):
    """Save a note: create it, or replace the body of the one with that title."""

    title: list[str] = argclass.Argument("title", nargs="+", help="Title of the note")
    body: str = argclass.Argument(
        "-b", "--body", default="", help=f"The note, in markdown, at most {NOTE_LIMIT} characters"
    )
    tag: list[str] = repeated("-t", "--tag", help="Tag to attach. Repeat for more than one")
    shared: bool = argclass.Argument(
        "--shared", default=False, help="Make the note visible from every project"
    )

    async def __call__(self) -> int:
        title = " ".join(self.title)
        check_note(self.body)
        existing: NoteOut | None = None
        try:
            existing = await self.connection.call(
                NoteOut,
                "memory_get",
                title=title,
                project=self.reference(),
            )
        except ToolError as error:
            if not str(error).startswith("no note"):
                raise
        saved = await self.connection.call(
            NoteOut,
            "memory_save",
            project=self.reference(),
            title=title,
            body=self.body,
            tags=self.tag or None,
            shared=self.shared or None,
        )
        if existing is not None and existing.id == saved.id:
            note(f"replaced note #{saved.id} in {saved.project}")
        else:
            note(f"added note #{saved.id} to {saved.project}")
        console.print(note_panel(saved, computer=self.connection.computer))
        return 0


class NoteAppend(Command):
    """Add a paragraph at the end of a note."""

    id: int = argclass.Argument("id", type=int, help="Note id")
    text: list[str] = argclass.Argument("text", nargs="+", help="The paragraph to add")

    async def __call__(self) -> int:
        updated = await self.connection.call(
            NoteOut, "memory_append", id=self.id, text=" ".join(self.text)
        )
        note(f"appended to note #{updated.id}")
        console.print(note_panel(updated, computer=self.connection.computer))
        return 0


class NoteEdit(Command):
    """Edit a note in `$EDITOR`."""

    id: int = argclass.Argument("id", type=int, help="Note id")

    async def __call__(self) -> int:
        found = await self.connection.call(NoteOut, "memory_get", id=self.id)
        written = compose(note_form_of(found))
        if written is None:
            warn("nothing changed")
            return 0
        fields = parse_note_form(written)
        if not fields["title"]:
            warn("the title is empty, nothing changed")
            return 1
        check_note(fields["body"])
        updated = await self.connection.call(NoteOut, "memory_save", id=found.id, **fields)
        note(f"updated note #{updated.id}")
        console.print(note_panel(updated, computer=self.connection.computer))
        return 0


class NoteDelete(Command):
    """Delete a note for good."""

    id: int = argclass.Argument("id", type=int, help="Note id")
    yes: bool = argclass.Argument(
        "-y",
        "--yes",
        default=False,
        help="Confirm deletion from another project",
    )

    async def __call__(self) -> int:
        result = await self.connection.call(Deleted, "memory_delete", id=self.id)
        if not result.deleted:
            warn("nothing deleted")
            return 1
        note(f"deleted note #{self.id}")
        return 0


class Notes(Command):
    """Lasting user context unavailable from other sources."""

    list = NoteList(
        aliases=["nl"],
        help="List the notes of this project and the shared ones",
        description=(
            "Print the notes of the current project plus every shared note, most recently "
            "changed first. --tag keeps one tag, --shared keeps the shared notes only, and "
            "--all-projects lists every board."
        ),
    )
    show = NoteShow(
        aliases=["ns"],
        help="Print one note in full",
        description="Print a note by id: its tags, whether it is shared, and the markdown body.",
    )
    add = NoteAdd(
        aliases=["na"],
        help="Write a note, or replace the one with that title",
        description=(
            "Save a note from the command line. A title is unique inside a project: a new "
            "title creates the note, a known one replaces its body and tags. --shared makes "
            "the note visible from every project. Keep secrets out; write where they live."
        ),
    )
    append = NoteAppend(
        aliases=["np"],
        help="Add a paragraph at the end of a note",
        description=(
            "Extend a note by id without rewriting it. The text lands after a blank line, so "
            "one fact per call keeps the note readable. `backlog search` finds notes as well."
        ),
    )
    edit = NoteEdit(
        aliases=["ne"],
        help="Edit a note in $EDITOR",
        description=(
            "Open a note in the editor as a form: the title, the tags and the shared flag on "
            "top, the markdown body below. Nothing changes when the file comes back unchanged."
        ),
    )
    delete = NoteDelete(
        aliases=["nd"],
        help="Delete a note for good",
        description=(
            "Delete a note permanently. Ask first only for another project, unless --yes is given. "
            "Tasks that link the note as `note #id` keep the dead link."
        ),
    )

    async def __call__(self) -> int:
        return self.show_help()


class Skills(Command):
    """The written skills this server offers to MCP clients."""

    skill: str = argclass.Argument("skill", nargs="?", default=None, help="Skill to print in full")

    async def __call__(self) -> int:
        found = await self.connection.skills()
        if self.skill is None:
            if not found:
                warn("no skills")
                return 1
            console.print(skills_table(found))
            return 0
        one = next((item for item in found if item.name == self.skill), None)
        if one is None:
            fail(f"no skill {self.skill!r}")
            return 1
        content = await self.connection.read(one.uri)
        one.instructions = skills.body(content.text)
        console.print(skill_panel(one))
        return 0


class Watch(Command):
    """Print each change of a backlog server as it happens."""

    endpoint: str = argclass.Argument(
        "endpoint",
        nargs="?",
        default="",
        help="The MCP endpoint of a running server, e.g. https://host/mcp?computer=laptop",
    )
    auth_user: str = argclass.Argument("-u", "--user", default="", help="Basic auth user")
    auth_password: str = argclass.Secret("--password", default="", help="Basic auth password")
    count: int = argclass.Argument(
        "-n",
        "--count",
        default=0,
        help="Stop after this many events; 0 watches until interrupted",
    )

    async def __call__(self) -> int:
        from aiohttp_tiny_mcp import MethodFilter

        project = self.reference()
        if project:
            selected = (await self.connection.board()).project
            project = selected.full_name
        methods: list[str | MethodFilter] = [
            MethodFilter(method=method, topics=[project]) if project else method
            for method in (TASK_CHANGED, NOTE_CHANGED)
        ]
        methods.append(PROJECT_CHANGED)
        seen = 0
        listening = cast(
            AsyncGenerator[dict[str, Any]], self.connection.client.listen(methods=methods)
        )
        async with contextlib.aclosing(listening) as changes:
            async for frame in changes:
                event = Event.model_validate(frame.get("params") or {})
                if project and event.project != project:
                    continue
                console.print(event_line(event), highlight=False)
                seen += 1
                if self.count and seen >= self.count:
                    break
        return 0


class Install(Command):
    """Register this server with the MCP clients on this machine."""

    name: str = argclass.Argument(default="backlog", help="Name the clients will show")
    claude: bool = argclass.Argument(default=False, help="Only Claude Code")
    codex: bool = argclass.Argument(default=False, help="Only Codex")
    force: bool = argclass.Argument(default=False, help="Replace an entry that is already there")

    def wanted(self) -> tuple[bool, bool]:
        """Both clients unless one was named."""
        return (True, True) if not (self.claude or self.codex) else (self.claude, self.codex)

    async def __call__(self) -> int:
        claude, codex = self.wanted()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.name):
            fail("server name must contain only letters, digits, underscores, or hyphens")
            return 1
        command, arguments = server_command()
        failed = 0
        if claude:
            failed += install_claude(self.name, command, arguments, force=self.force)
        if codex:
            failed += install_codex(self.name, command, arguments, force=self.force)
        return 1 if failed else 0


def server_command() -> tuple[str, list[str]]:
    """How another program should start this server."""
    found = Path(sys.argv[0]).resolve()
    if found.name.startswith("backlog") and os.access(found, os.X_OK):
        return str(found), ["mcp"]
    return sys.executable, ["-m", "backlog_mcp", "mcp"]


def run(command: Sequence[str]) -> bool:
    """Run a client's own CLI. False when it is missing or refuses."""
    try:
        done = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError:
        return False
    if done.returncode:
        log.debug("%s failed: %s", command[0], done.stderr.strip())
    return done.returncode == 0


def client_json(path: Path) -> dict[str, Any]:
    """Read a client JSON object without discarding unrelated settings."""
    found = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(found, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return found


def config_table(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.setdefault(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a configuration table")
    return value


def write_client_config(path: Path, text: str) -> None:
    """Replace a complete configuration atomically, preserving existing file permissions."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        if path.exists():
            temporary.chmod(path.stat().st_mode & 0o777)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def install_claude(name: str, command: str, arguments: list[str], *, force: bool) -> int:
    """Register the server and allow its tools in Claude's user settings."""
    config = Path.home() / ".claude.json"
    settings = Path.home() / ".claude" / "settings.json"
    try:
        found = client_json(config)
        servers = config_table(found, "mcpServers")
        preferences = client_json(settings)
        permissions = config_table(preferences, "permissions")
        allowed = permissions.setdefault("allow", [])
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            raise ValueError("permissions.allow must be a list of strings")
        rule = f"mcp__{name}__*"
        if rule not in allowed:
            allowed.append(rule)
        if name in servers and not force:
            warn(f"claude: {name} is already registered, --force replaces it")
        elif run(["claude", "mcp", "add", "--scope", "user", name, "--", command, *arguments]):
            note(f"claude: {name} registered")
        else:
            servers[name] = {"type": "stdio", "command": command, "args": arguments, "env": {}}
            write_client_config(config, json.dumps(found, indent=2) + "\n")
            note(f"claude: {name} written to {config}")
        write_client_config(settings, json.dumps(preferences, indent=2) + "\n")
    except (OSError, ValueError) as error:
        fail(f"claude: cannot configure {name}: {error}")
        return 1
    note(f"claude: {name} tools auto-approved in {settings}")
    return 0


def install_codex(name: str, command: str, arguments: list[str], *, force: bool) -> int:
    """Approve this server's tools and disable the Codex sandbox in user configuration."""
    config = Path.home() / ".codex" / "config.toml"
    try:
        found = rtoml.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
        servers = config_table(found, "mcp_servers")
        if name in servers and not force:
            warn(f"codex: {name} is already registered, --force replaces it")
        else:
            registered = run(["codex", "mcp", "add", name, "--", command, *arguments])
            if registered:
                found = rtoml.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
                servers = config_table(found, "mcp_servers")
                note(f"codex: {name} registered")
            if not registered or name not in servers:
                servers[name] = {"command": command, "args": arguments}
        server = config_table(servers, name)
        server["default_tools_approval_mode"] = "approve"
        overrides = server.get("tools", {})
        if not isinstance(overrides, dict):
            raise ValueError("tools must be a configuration table")
        for tool in overrides:
            config_table(overrides, tool)["approval_mode"] = "approve"
        found["sandbox_mode"] = "danger-full-access"
        write_client_config(config, rtoml.dumps(found))
    except (OSError, ValueError) as error:
        fail(f"codex: cannot configure {name}: {error}")
        return 1
    note(f"codex: {name} written to {config}; tools auto-approved, user sandbox disabled")
    return 0


class TaskCommands(Command):
    """Tasks, their status, and their comment history."""

    list = List(
        aliases=["ls"],
        help="List tasks of this project, or of all",
        description=(
            "List tasks, most important first, open ones (todo, doing, blocked) before the "
            "rest. --status narrows to named statuses and may be repeated, --every adds the "
            "closed ones, and --all-projects reaches every board at once."
        ),
    )

    link = Link(aliases=["lk"], help="Create a typed relation and its inverse")
    claim = Claim(aliases=["ck"], help="Atomically claim, release or reassign a task")
    assigned = Assigned(aliases=["as"], help="List tasks by assignment prefix")
    unlink = Unlink(aliases=["ul"], help="Remove a relation and its inverse")
    comment = CommentAdd(aliases=["co"], help="Add a task comment")

    comments = CommentList(aliases=["cm"], help="Read task comments and history")

    show = Show(
        aliases=["sh"],
        help="Print one task in full",
        description="Print the title, body, status, priority, tags and timestamps of one task.",
    )

    add = Add(
        aliases=["ad"],
        help="Add a task from the command line",
        description=(
            "Add a task without opening an editor. The title is the positional argument; "
            "--body, --status, --priority and repeated --tag flags set the rest."
        ),
    )

    create = Create(
        aliases=["cr"],
        help="Write a task in $EDITOR",
        description=(
            "Open $VISUAL, else $EDITOR, else vi on a form. The headers at the top set the "
            "title, status, priority and tags; everything under the blank line is the "
            "description. An empty title cancels the task."
        ),
    )

    edit = Edit(
        aliases=["ed"],
        help="Edit a task in $EDITOR",
        description=(
            "Edit the task fields and description. "
            "Use `backlog task comment` for progress updates. "
            "Leaving the file unchanged changes nothing."
        ),
    )

    start = Start(
        aliases=["st"],
        help="Mark tasks in progress",
        description="Move one or more tasks to the doing status.",
    )

    block = Block(
        aliases=["bl"],
        help="Mark tasks blocked",
        description="Move one or more tasks to the blocked status. They stay open.",
    )

    close = Close(
        aliases=["cl"],
        help="Mark tasks done",
        description="Move one or more tasks to the done status, which stamps the closing time.",
    )

    cancel = Cancel(
        aliases=["ca"],
        help="Mark tasks cancelled",
        description=(
            "Move one or more tasks to the cancelled status: kept and searchable, but closed. "
            "Prefer this to `backlog task delete` for work that was dropped."
        ),
    )

    reopen = Reopen(
        aliases=["re"],
        help="Put tasks back on the pile",
        description="Move one or more tasks back to the todo status and clear the closing time.",
    )

    move = Move(
        aliases=["mv"],
        help="Move a task into another project",
        description=(
            "Give a task to another board. The destination is a project slug or any path "
            "inside the project. The task keeps its id."
        ),
    )

    delete = Delete(
        aliases=["rm"],
        help="Delete a task for good",
        description=(
            "Delete a task permanently. Ask first only for another project, unless --yes is given. "
            "`backlog task cancel` keeps the record instead."
        ),
    )

    async def __call__(self) -> int:
        return self.show_help()


class CLI(argclass.Parser):
    """Defaults come from the config files, then `BACKLOG_MCP_*`, then these arguments.

    The options of `backlog mcp` read the `[mcp]` table of the same files and the same
    environment prefix. `--generate-config` here, or on `mcp`, writes both back. The options
    of the other commands are command-line only.
    """

    log_level: int = argclass.LogLevel

    url: str = argclass.Argument(
        "--url",
        default="",
        help="MCP HTTP endpoint; unset starts a local stdio server",
    )
    user: str = argclass.Argument("--user", default="", help="MCP HTTP Basic auth user")
    password: str = argclass.Secret("--password", default="", help="MCP HTTP Basic auth password")
    token: str = argclass.Secret(
        "--token", default="", help="MCP HTTP Bearer token; mutually exclusive with Basic auth"
    )
    default_user: str | None = argclass.Argument(
        default=None,
        type=str,
        help="Default username for stdio or HTTP without authorizers (default: $USER)",
    )
    timeout: float = argclass.Argument(default=120.0, help="MCP request timeout in seconds")

    database: Path = argclass.Argument(
        "-d",
        "--database",
        type=Path,
        default=None,
        help=(
            "SQLite file holding every project "
            "(default: $XDG_DATA_HOME/backlog-mcp/backlog.sqlite3)"
        ),
    )
    root: Path = argclass.Argument(
        "-r",
        "--root",
        type=Path,
        default=None,
        help="Directory to scope to (default: the working directory)",
    )
    project: str = argclass.Argument(
        "-p",
        "--project",
        default=None,
        help="Work on this project whatever the directory says: a slug, or a path inside it",
    )
    computer: str = argclass.Argument(
        "--computer",
        default=None,
        help=(
            "Name of this computer, the one the projects of this directory tree belong to "
            "(default: the short host name)"
        ),
    )
    generate_config: str = argclass.Argument(
        action=argclass.GenerateConfigAction,
        generator=ConfigTemplate,
        metavar="FILE",
        help=(
            "Write these options and the [mcp] tables as a TOML config file and exit; '-' "
            "prints it. Options given before this flag are written active, the rest as "
            "commented-out defaults"
        ),
    )

    mcp = Serve(
        aliases=["mc"],
        help="Serve MCP, on stdio or over HTTP",
        description=(
            "Run the MCP server on the same backlog the other commands use. stdio is the "
            "transport clients start by themselves; --protocol=http serves the endpoint at "
            "/mcp and the bundled console at /console. The http group configures the listener; "
            "the auth group configures Basic accounts and JWT verification. `backlog-mcp` is "
            "the same command without the word mcp. Over HTTP, "
            "?computer=<name>&project=<slug> on the endpoint URL serves a client as another "
            "computer."
        ),
        #: The same prefix as the root, so the port is BACKLOG_MCP_HTTP_PORT, not _MCP_MCP_.
        auto_env_var_prefix="BACKLOG_MCP_",
        #: `--config` after `mcp` as well, so `backlog-mcp --config FILE` works.
        config_argument="--config",
        config_parser_class=argclass.TOMLDefaultsParser,
    )
    whoami = Whoami(aliases=["wi"], help="Show the current principal and computer")
    board = Board(aliases=["bo"], help="Show a project board with ready and claimed tasks")
    init = Init(
        aliases=["in"],
        help="Register this checkout or bind it to an existing project",
        description=(
            "Register the current computer and checkout through MCP. Without arguments, ask "
            "for the computer, directory, name, and description. NAME creates a project without "
            "prompts; "
            "--existing SLUG binds the directory to an existing project."
        ),
    )
    projects = Projects(
        aliases=["pr"],
        help="List the registered projects",
        description=(
            "Print the projects of this computer with their root directory and their open and "
            "total task counts; --computer shows another computer's, --all-computers all. "
            "Archived projects are hidden until --archived asks for them. `backlog projects "
            "delete <slug>` removes a project with everything on its board."
        ),
    )
    task = TaskCommands(aliases=["ta"], help="Manage tasks and comments")
    search = Search(
        aliases=["se"],
        help="FTS and semantic search over projects, tasks, comments, and memory",
        description=(
            "Search titles, bodies and tags with FTS prefixes and local multilingual embeddings. "
            "FTS matches come first, followed by semantic matches without duplicate IDs. "
            "--min-score filters semantic matches only. --all-projects searches every board."
        ),
    )
    stats = Stats(
        aliases=["ss"],
        help="Count open tasks, closed tasks, and notes",
        description=(
            "Show open tasks, closed tasks, and notes, followed by status and priority counts "
            "and tags. Notes include shared notes. --all-projects counts every board together."
        ),
    )
    memory = Notes(
        aliases=["mm"],
        help="Lasting user context: preferences, agreements, and constraints",
        description=(
            "Code changes; notes remain. Keep lasting user context unavailable elsewhere, not "
            "implementation details or documentation summaries. `memory add` creates a note "
            "or replaces the one with that title, `memory append` adds a paragraph, and `backlog "
            "search` finds notes as well as tasks. A note has an id for good, so a task body "
            "can say `#note3`."
        ),
    )
    skills = Skills(
        aliases=["sk"],
        help="Show the skills this server offers to MCP clients",
        description=(
            "Print the written skills served over MCP: their names, what each one is for, and "
            "how many files it carries. Name a skill to read its instructions in full. Skills "
            "are text: a client reads them, then calls the tools."
        ),
    )
    watch = Watch(
        aliases=["wa"],
        help="Print the changes of a backlog server as they happen",
        description=(
            "Listen to a running server over HTTP and print one line per change: tasks, notes "
            "and projects added, updated, moved or deleted, by any client. --project keeps one "
            "board. The server sends these as MCP notifications of the backlog-mcp/events "
            "extension; agents do not see them, this command and dashboards do."
        ),
    )
    install = Install(
        aliases=["is"],
        help="Register this server with Claude Code and Codex",
        description=(
            "Add this backlog as an MCP server to the clients on this machine. Their own CLIs "
            "write the entry when they are installed; otherwise ~/.claude.json and "
            "~/.codex/config.toml are edited directly. Auto-approve this server's tools in both "
            "clients and disable the Codex sandbox in user configuration. "
            "--claude or --codex picks one client."
        ),
    )

    async def __call__(self) -> int:
        """Serve locally, or run a command over one MCP connection."""
        command = self.current_subparser
        if not isinstance(command, Command):
            self.print_help()
            return 0
        try:
            if isinstance(command, Serve):
                backlog = build(self)
                use(backlog)
                try:
                    await backlog.open()
                    return await command()
                finally:
                    await backlog.close()
            if isinstance(command, (Install, Notes, TaskCommands)):
                return await command()
            if isinstance(command, Watch):
                self.url = command.endpoint or self.url
                self.user = command.auth_user or self.user
                self.password = command.auth_password or self.password
            scoped = isinstance(
                command,
                (Board, List, Search, Add, Create, Delete, Stats, NoteList, NoteAdd, NoteDelete),
            )
            if (
                (isinstance(command, Board) and command.project is not None)
                or getattr(command, "all_projects", False)
                or getattr(command, "all_computers", False)
                or getattr(command, "at", None)
            ):
                scoped = False
            async with connect(
                self, on_ask=command.ask, on_register=command.registration, scope_directory=scoped
            ) as connection:
                self.connection = connection
                return await command()
        except ToolError as error:
            fail(str(error))
            return error.exit_code
        except UnknownProject as error:
            fail(str(error))
            return 2
        except HTTPClientError as error:
            fail(f"MCP connection failed: {type(error).__name__}")
            return 1
        except (ClientError, OSError, TimeoutError, ValueError) as error:
            fail(str(error) or "MCP request timed out")
            return 1


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. stdout carries the MCP protocol, so every log line goes to stderr."""
    parser = CLI(
        config_files=paths.config_files(),
        config_parser_class=argclass.TOMLDefaultsParser,
        config_argument="--config",
        auto_env_var_prefix="BACKLOG_MCP_",
        description=f"backlog {VERSION}: projects and tasks, on the command line and over MCP",
        #: argparse obeys FORCE_COLOR like rich does; a pipe gets plain help text.
        color=sys.stdout.isatty(),
    )
    parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=parser.log_level, stream=sys.stderr, format=LOG_FORMAT)
    try:
        return asyncio.run(parser())
    except KeyboardInterrupt:
        return 130


def server_main(argv: Sequence[str] | None = None) -> int:
    """The `backlog-mcp` entry point: the server, with no subcommand to type."""
    return main(["mcp", *(argv if argv is not None else sys.argv[1:])])


__all__ = [
    "CLI",
    "Command",
    "Transition",
    "build",
    "main",
    "parse_form",
    "parse_note_form",
    "server_main",
]
