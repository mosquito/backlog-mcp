"""The command line. `backlog mcp` serves MCP; every other command edits the backlog directly."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import argclass
import rtoml
from rich.prompt import Confirm

from . import embeddings, httpd, paths, skills
from .backlog import Backlog, Reach, UnknownProject
from .models import OPEN_STATUSES, Priority, Status
from .render import (
    console,
    fail,
    note,
    note_panel,
    notes_table,
    projects_table,
    scope_panel,
    skill_panel,
    skills_table,
    stats_view,
    task_panel,
    tasks_table,
    warn,
)
from .server import (
    VERSION,
    append_history,
    bound,
    note_out,
    project_out,
    registry,
    scope_of,
    stats_of,
    task_out,
    use,
)
from .state import State
from .store import NOTE_LIMIT, Note, Store, Task, check_note

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


def build(options: CLI) -> Backlog:
    """The backlog the global options describe."""
    state = State(options.state or paths.state_path())
    computer = state.computer_name(options.computer)
    store = Store(options.database or paths.database_path(), computer=computer)
    return Backlog(store, state, cwd=options.root, project=options.project, computer=computer)


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


def form_of(task: Task | None = None) -> str:
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


def note_form_of(existing: Note | None = None) -> str:
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
    """One subcommand. The root parser opens the backlog, then awaits this."""

    #: False for the commands that read no tasks, so they never create a database file.
    opens = True

    def options(self) -> CLI:
        """The root parser, however deep this command is nested."""
        parent = self.__parent__
        while isinstance(parent, Command):
            parent = parent.__parent__
        assert isinstance(parent, CLI), "a command runs under the root parser"
        return parent

    @property
    def backlog(self) -> Backlog:
        """The backlog the root parser opened, the one the MCP handlers also see."""
        return bound()

    def reference(self) -> str | None:
        """The project this command works on: the `--project` of the root parser."""
        return self.options().project

    async def reach(self) -> Reach:
        """What a listing covers, from the project and computer options of this command."""
        #: The attribute is `at`, not `computer`, for the same reason as `scope`.
        return await self.backlog.reach(
            self.reference(),
            all_projects=bool(getattr(self, "all_projects", False)),
            computer=getattr(self, "at", None) or None,
            all_computers=bool(getattr(self, "all_computers", False)),
        )

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


class Serve(Command):
    """Run the MCP server: on stdio by default, or over HTTP with `--protocol=http`.

    These options also come from the `[mcp]` table of a config file and from `BACKLOG_MCP_*`.
    """

    protocol: str = argclass.Argument(
        default="stdio",
        choices=("stdio", "http"),
        help="stdio speaks MCP on stdin and stdout; http serves the endpoint and the console",
    )
    http_listen: str = argclass.Argument(
        default="127.0.0.1", help="Address to listen on, HTTP only"
    )
    http_port: int = argclass.Argument(default=3009, help="Port to listen on, HTTP only")
    http_user: str = argclass.Argument(default="", help="Basic auth user, HTTP only")
    http_password: str = argclass.Secret(
        default="",
        help="Basic auth password. Without a user and a password the server asks for nothing",
    )
    http_no_console: bool = argclass.Argument(
        default=False, help="Do not serve the bundled MCP console at /"
    )
    http_trust_proxy: bool = argclass.Argument(
        default=False,
        help=(
            "Behind a TLS-terminating reverse proxy: skip the Origin check of the endpoint, "
            "the proxy validates origins instead"
        ),
    )
    generate_config: str = argclass.Argument(
        action=argclass.GenerateConfigAction,
        generator=ConfigTemplate,
        metavar="FILE",
        help=(
            "Write a TOML config file with these options in its [mcp] table and exit; '-' "
            "prints it. Options given before this flag are written active, the rest as "
            "commented-out defaults"
        ),
    )

    async def stdio(self) -> int:
        """MCP on stdin and stdout, the transport every client starts by itself."""
        from aiohttp_tiny_mcp import run_stdio

        backlog = self.backlog
        project, origin = await backlog.current()
        log.info("project %s (%s)", project.slug, origin.value)
        log.info("serving %s on stdio, database %s", registry.info.name, backlog.store.path)
        await run_stdio(registry)
        return 0

    async def __call__(self) -> int:
        match self.protocol:
            case "http":
                await httpd.serve(
                    self.backlog,
                    address=self.http_listen,
                    port=self.http_port,
                    user=self.http_user,
                    password=str(self.http_password),
                    console=not self.http_no_console,
                    trust_proxy=self.http_trust_proxy,
                )
            case _:
                await self.stdio()
        return 0


class Current(Command):
    """Show the project the commands work on, and why that one."""

    async def __call__(self) -> int:
        console.print(scope_panel(await scope_of(self.backlog)))
        return 0


class Use(Command):
    """Pin a project, so the working directory stops deciding. Drop the pin with `--clear`."""

    target: str = argclass.Argument(
        "target", nargs="?", default=None, help="Project slug, or a path inside it"
    )
    clear: bool = argclass.Argument(default=False, help="Forget the pin and follow the directory")

    async def __call__(self) -> int:
        computer = self.backlog.computer
        if self.clear:
            self.backlog.state.pin(computer, None)
        elif self.target:
            self.backlog.state.pin(computer, (await self.backlog.lookup(self.target)).slug)
        console.print(scope_panel(await scope_of(self.backlog)))
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
        project = await self.backlog.lookup(self.target)
        contents = await self.backlog.store.project_contents(project.id)
        if not self.yes and not Confirm.ask(
            f"delete project {project.slug!r} with {contents.tasks} task(s) and "
            f"{contents.notes} note(s), {contents.shared_notes} of them shared? "
            "This cannot be undone",
            default=False,
        ):
            warn("nothing deleted")
            return 1
        try:
            await self.backlog.delete(project)
        except ValueError as error:
            fail(str(error))
            return 1
        note(
            f"deleted project {project.slug!r}: {contents.tasks} task(s), {contents.notes} note(s)"
        )
        return 0


class Projects(Command):
    """List the registered projects of this computer, or of another, or of all."""

    archived: bool = argclass.Argument(default=False, help="Include archived projects")
    at, all_computers = computer_options()
    delete = ProjectDelete(
        help="Delete a project for good",
        description=(
            "Delete a project with every task and note on its board, and free its directories "
            "for a new registration. Asks first unless --yes is given. Prefer archiving through "
            "the MCP tool project_save when the history should stay."
        ),
    )

    async def __call__(self) -> int:
        reach = await self.backlog.reach(
            all_projects=True, computer=self.at or None, all_computers=self.all_computers
        )
        found = await self.backlog.store.projects(archived=self.archived, computer=reach.computer)
        computer = self.backlog.computer
        console.print(
            projects_table(
                [project_out(item, computer=computer) for item in found], computer=computer
            )
        )
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
        reach = await self.reach()
        found = await self.backlog.store.tasks(
            project_id=reach.project_id,
            computer=reach.computer,
            status=self.wanted(),
            tag=self.tag or None,
            priority=Priority(self.priority) if self.priority else None,
            limit=self.limit,
        )
        console.print(
            tasks_table(
                [task_out(item) for item in found],
                title=f"{len(found)} task(s) of {reach.label}",
                wide=reach.project is None,
                computer=self.backlog.computer,
            )
        )
        return 0


class Search(Command):
    """FTS and semantic search across tasks and notes, including shared notes."""

    query: list[str] = argclass.Argument("query", nargs="+", help="Words or meaning to search for")
    all_projects: bool = argclass.Argument(
        "-a", "--all-projects", default=False, help="Search every project on this computer"
    )
    at, all_computers = computer_options()
    tag: str = argclass.Argument(default="", help="Only tasks and notes carrying this tag")
    limit: int = argclass.Argument(default=20, help="Most matches to print, of each kind")
    min_score: float = argclass.Argument(
        default=0.15,
        help="Minimum semantic similarity (-1 to 1); FTS matches are retained, with a 0.25 floor",
    )

    async def __call__(self) -> int:
        text = " ".join(self.query)
        args = embeddings.Search(
            query=text,
            project=self.reference(),
            all_projects=self.all_projects,
            computer=self.at or None,
            all_computers=self.all_computers,
            tag=self.tag or None,
            limit=self.limit,
            min_score=self.min_score,
        )
        matches = await embeddings.search(args, self.backlog)
        if matches.project:
            where = matches.project
        elif matches.computer:
            where = f"every project on {matches.computer}"
        else:
            where = "every project on every computer"
        computer = self.backlog.computer
        console.print(
            tasks_table(
                matches.tasks,
                title=f"{len(matches.tasks)} task match(es) for {text!r} in {where}",
                wide=matches.project is None,
                computer=computer,
            )
        )
        if matches.notes:
            console.print(
                notes_table(
                    matches.notes,
                    title=f"{len(matches.notes)} note match(es) for {text!r} in {where}",
                    wide=matches.project is None
                    or any(item.project != matches.project for item in matches.notes),
                    computer=computer,
                )
            )
        return 0


class Show(Command):
    """Print one task in full."""

    id: int = argclass.Argument("id", type=int, help="Task id")

    async def __call__(self) -> int:
        task = await self.backlog.store.task(self.id)
        if task is None or task.status == Status.DELETED:
            fail(f"no task #{self.id}")
            return 1
        console.print(task_panel(task_out(task), computer=self.backlog.computer))
        return 0


class Add(Command):
    """Add a task from the command line, without opening an editor."""

    title: list[str] = argclass.Argument("title", nargs="+", help="Title of the task")
    body: str = argclass.Argument("-b", "--body", default="", help="Description")
    status: str = argclass.Argument("-s", "--status", default=Status.TODO.value, choices=STATUSES)
    priority: str = argclass.Argument(default=Priority.NORMAL.value, choices=PRIORITIES)
    tag: list[str] = repeated("-t", "--tag", help="Tag to attach. Repeat for more than one")

    async def __call__(self) -> int:
        project = await self.backlog.resolve(self.reference())
        task = await self.backlog.store.add_task(
            project_id=project.id,
            title=" ".join(self.title),
            body=self.body,
            status=Status(self.status),
            priority=Priority(self.priority),
            tags=self.tag,
        )
        note(f"added #{task.id} to {project.slug}")
        console.print(task_panel(task_out(task), computer=self.backlog.computer))
        return 0


class Create(Command):
    """Write a task in `$EDITOR`, from a filled-in form."""

    async def __call__(self) -> int:
        project = await self.backlog.resolve(self.reference())
        written = compose(form_of())
        if written is None:
            warn("nothing written, no task added")
            return 1
        fields = parse_form(written)
        if not fields["title"]:
            warn("the title is empty, no task added")
            return 1
        task = await self.backlog.store.add_task(project_id=project.id, **fields)
        note(f"added #{task.id} to {project.slug}")
        console.print(task_panel(task_out(task), computer=self.backlog.computer))
        return 0


class Edit(Command):
    """Edit a task in `$EDITOR`."""

    id: int = argclass.Argument("id", type=int, help="Task id")

    async def __call__(self) -> int:
        task = await self.backlog.store.task(self.id)
        if task is None or task.status == Status.DELETED:
            fail(f"no task #{self.id}")
            return 1
        written = compose(form_of(task))
        if written is None:
            warn("nothing changed")
            return 0
        fields = parse_form(written)
        if not fields["title"]:
            warn("the title is empty, nothing changed")
            return 1
        changed = await self.backlog.store.update_task(task.id, **fields)
        assert changed is not None, "the task edited is gone"
        note(f"updated #{changed.id}")
        console.print(task_panel(task_out(changed), computer=self.backlog.computer))
        return 0


class Transition(Command):
    """Base of the commands that only move a task from one status to another."""

    ids: list[int] = argclass.Argument("ids", type=int, nargs="+", help="Task ids")
    target = Status.DONE

    async def __call__(self) -> int:
        failed = 0
        for task_id in self.ids:
            task = await self.backlog.store.update_task(task_id, status=self.target)
            if task is None:
                fail(f"no task #{task_id}")
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
        project = await self.backlog.lookup(self.destination)
        existing = await self.backlog.store.task(self.id)
        if existing is None:
            fail(f"no task #{self.id}")
            return 1
        if existing.project_id == project.id:
            note(f"#{existing.id} already belongs to {project.slug}")
            return 0
        task = await self.backlog.store.update_task(
            self.id,
            project_id=project.id,
            body=append_history(existing.body, f"Moved: {existing.project} → {project.slug}"),
        )
        assert task is not None
        note(f"#{task.id} now belongs to {project.slug}")
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
        task = await self.backlog.store.task(self.id)
        if task is None or task.status == Status.DELETED:
            fail(f"no task #{self.id}")
            return 1
        current = await self.backlog.resolve(self.reference())
        where = task.project
        if (
            task.project_id != current.id
            and not self.yes
            and not Confirm.ask(
                f"delete task #{task.id} {task.title!r} from {where!r}? "
                f"Current project: {current.slug}",
                default=False,
            )
        ):
            warn("nothing deleted")
            return 1
        await self.backlog.store.delete_task(task.id)
        note(f"deleted #{task.id}")
        return 0


class Stats(Command):
    """Open tasks, closed tasks, notes, and detailed task counts."""

    all_projects: bool = argclass.Argument(
        "-a", "--all-projects", default=False, help="Count every project on this computer together"
    )
    at, all_computers = computer_options()

    async def __call__(self) -> int:
        console.print(stats_view(await stats_of(self.backlog, await self.reach())))
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
        reach = await self.reach()
        found = await self.backlog.store.notes(
            project_id=reach.project_id,
            computer=reach.computer,
            tag=self.tag or None,
            shared=True if self.shared else None,
            limit=self.limit,
        )
        #: A shared note of another project shows up here, so say where it comes from.
        wide = reach.project is None or any(item.project_id != reach.project_id for item in found)
        console.print(
            notes_table(
                [note_out(item) for item in found],
                title=f"{len(found)} note(s) of {reach.label}",
                wide=wide,
                computer=self.backlog.computer,
            )
        )
        return 0


class NoteShow(Command):
    """Print one note in full."""

    id: int = argclass.Argument("id", type=int, help="Note id")

    async def __call__(self) -> int:
        found = await self.backlog.store.note(self.id)
        if found is None:
            fail(f"no note #{self.id}")
            return 1
        console.print(note_panel(note_out(found), computer=self.backlog.computer))
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
        project = await self.backlog.resolve(self.reference())
        title = " ".join(self.title)
        try:
            check_note(self.body)
        except ValueError as error:
            fail(str(error))
            return 1
        existing = await self.backlog.store.note_by_title(title, project_id=project.id)
        if existing is None:
            saved = await self.backlog.store.add_note(
                project_id=project.id,
                title=title,
                body=self.body,
                tags=self.tag,
                shared=self.shared,
            )
            note(f"added note #{saved.id} to {project.slug}")
        else:
            #: Flags not given leave the tags and the shared state alone; `note edit` clears them.
            replaced = await self.backlog.store.update_note(
                existing.id, body=self.body, tags=self.tag or None, shared=self.shared or None
            )
            assert replaced is not None, "the note replaced is gone"
            saved = replaced
            note(f"replaced note #{saved.id} in {project.slug}")
        console.print(note_panel(note_out(saved), computer=self.backlog.computer))
        return 0


class NoteAppend(Command):
    """Add a paragraph at the end of a note."""

    id: int = argclass.Argument("id", type=int, help="Note id")
    text: list[str] = argclass.Argument("text", nargs="+", help="The paragraph to add")

    async def __call__(self) -> int:
        found = await self.backlog.store.note(self.id)
        if found is None:
            fail(f"no note #{self.id}")
            return 1
        try:
            updated = await self.backlog.store.append_note(found.id, " ".join(self.text))
        except ValueError as error:
            fail(str(error))
            return 1
        assert updated is not None, "the note appended to is gone"
        note(f"appended to note #{updated.id}")
        console.print(note_panel(note_out(updated), computer=self.backlog.computer))
        return 0


class NoteEdit(Command):
    """Edit a note in `$EDITOR`."""

    id: int = argclass.Argument("id", type=int, help="Note id")

    async def __call__(self) -> int:
        found = await self.backlog.store.note(self.id)
        if found is None:
            fail(f"no note #{self.id}")
            return 1
        written = compose(note_form_of(found))
        if written is None:
            warn("nothing changed")
            return 0
        fields = parse_note_form(written)
        if not fields["title"]:
            warn("the title is empty, nothing changed")
            return 1
        try:
            updated = await self.backlog.store.update_note(found.id, **fields)
        except ValueError as error:
            fail(str(error))
            return 1
        assert updated is not None, "the note edited is gone"
        note(f"updated note #{updated.id}")
        console.print(note_panel(note_out(updated), computer=self.backlog.computer))
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
        found = await self.backlog.store.note(self.id)
        if found is None:
            fail(f"no note #{self.id}")
            return 1
        current = await self.backlog.resolve(self.reference())
        if (
            found.project_id != current.id
            and not self.yes
            and not Confirm.ask(
                f"delete note #{found.id} {found.title!r} from {found.project!r}? "
                f"Current project: {current.slug}",
                default=False,
            )
        ):
            warn("nothing deleted")
            return 1
        await self.backlog.store.delete_note(found.id)
        note(f"deleted note #{found.id}")
        return 0


class Notes(Command):
    """Lasting user context unavailable from other sources."""

    opens = False

    list = NoteList(
        help="List the notes of this project and the shared ones",
        description=(
            "Print the notes of the current project plus every shared note, most recently "
            "changed first. --tag keeps one tag, --shared keeps the shared notes only, and "
            "--all-projects lists every board."
        ),
    )
    show = NoteShow(
        help="Print one note in full",
        description="Print a note by id: its tags, whether it is shared, and the markdown body.",
    )
    add = NoteAdd(
        help="Write a note, or replace the one with that title",
        description=(
            "Save a note from the command line. A title is unique inside a project: a new "
            "title creates the note, a known one replaces its body and tags. --shared makes "
            "the note visible from every project. Keep secrets out; write where they live."
        ),
    )
    append = NoteAppend(
        help="Add a paragraph at the end of a note",
        description=(
            "Extend a note by id without rewriting it. The text lands after a blank line, so "
            "one fact per call keeps the note readable. `backlog search` finds notes as well."
        ),
    )
    edit = NoteEdit(
        help="Edit a note in $EDITOR",
        description=(
            "Open a note in the editor as a form: the title, the tags and the shared flag on "
            "top, the markdown body below. Nothing changes when the file comes back unchanged."
        ),
    )
    delete = NoteDelete(
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

    opens = False

    skill: str = argclass.Argument("skill", nargs="?", default=None, help="Skill to print in full")

    async def __call__(self) -> int:
        if self.skill is None:
            found = skills.library.skills()
            if not found:
                warn("no skills")
                return 1
            console.print(skills_table(found))
            return 0
        one = skills.library.skill(self.skill)
        if one is None:
            fail(f"no skill {self.skill!r}")
            return 1
        console.print(skill_panel(one))
        return 0


class Install(Command):
    """Register this server with the MCP clients on this machine."""

    opens = False

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


class CLI(argclass.Parser):
    """Defaults come from the config files, then `BACKLOG_MCP_*`, then these arguments.

    The options of `backlog mcp` read the `[mcp]` table of the same files and the same
    environment prefix. `--generate-config` here, or on `mcp`, writes both back. The options
    of the other commands are command-line only.
    """

    log_level: int = argclass.LogLevel

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
    state: Path = argclass.Argument(
        "-s",
        "--state",
        type=Path,
        default=None,
        help=(
            "JSON file holding the pinned project (default: $XDG_STATE_HOME/backlog-mcp/state.json)"
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
            "(default: the name remembered in the state file, else the short host name)"
        ),
    )
    generate_config: str = argclass.Argument(
        action=argclass.GenerateConfigAction,
        generator=ConfigTemplate,
        metavar="FILE",
        help=(
            "Write these options and the [mcp] table as a TOML config file and exit; '-' "
            "prints it. Options given before this flag are written active, the rest as "
            "commented-out defaults"
        ),
    )

    mcp = Serve(
        help="Serve MCP, on stdio or over HTTP",
        description=(
            "Run the MCP server on the same backlog the other commands use. stdio is the "
            "transport clients start by themselves; --protocol=http serves the endpoint at "
            "/mcp and the bundled console at /, behind Basic auth when a user and a password "
            "are given. `backlog-mcp` is the same command without the word mcp. Over HTTP, "
            "?computer=<name>&project=<slug> on the endpoint URL serves a client as another "
            "computer."
        ),
        #: The same prefix as the root, so the port is BACKLOG_MCP_HTTP_PORT, not _MCP_MCP_.
        auto_env_var_prefix="BACKLOG_MCP_",
        #: `--config` after `mcp` as well, so `backlog-mcp --config FILE` works.
        config_argument="--config",
        config_parser_class=argclass.TOMLDefaultsParser,
    )
    current = Current(
        help="Show which project the commands work on",
        description=(
            "Print the current project and why it is current: named by --project, pinned by "
            "`backlog use`, found from the working directory, or registered for it just now. "
            "Also prints the database and the state file in use."
        ),
    )
    use = Use(
        help="Pin a project, or drop the pin",
        description=(
            "Pin a project so the working directory stops deciding which board is current. "
            "The pin lives in the state file and outlives the process. `backlog use --clear` "
            "drops it and the directory decides again."
        ),
    )
    projects = Projects(
        help="List the registered projects",
        description=(
            "Print the projects of this computer with their root directory and their open and "
            "total task counts; --computer shows another computer's, --all-computers all. "
            "Archived projects are hidden until --archived asks for them. `backlog projects "
            "delete <slug>` removes a project with everything on its board."
        ),
    )
    list = List(
        help="List tasks of this project, or of all",
        description=(
            "List tasks, most important first, open ones (todo, doing, blocked) before the "
            "rest. --status narrows to named statuses and may be repeated, --every adds the "
            "closed ones, and --all-projects reaches every board at once."
        ),
    )
    search = Search(
        help="FTS and semantic search over tasks and notes",
        description=(
            "Search titles, bodies and tags with FTS prefixes and local multilingual embeddings. "
            "FTS matches come first, followed by semantic matches without duplicate IDs. "
            "--min-score filters semantic matches only. --all-projects searches every board."
        ),
    )
    show = Show(
        help="Print one task in full",
        description="Print the title, body, status, priority, tags and timestamps of one task.",
    )
    add = Add(
        help="Add a task from the command line",
        description=(
            "Add a task without opening an editor. The title is the positional argument; "
            "--body, --status, --priority and repeated --tag flags set the rest."
        ),
    )
    create = Create(
        help="Write a task in $EDITOR",
        description=(
            "Open $VISUAL, else $EDITOR, else vi on a form. The headers at the top set the "
            "title, status, priority and tags; everything under the blank line is the "
            "description. An empty title cancels the task."
        ),
    )
    edit = Edit(
        help="Edit a task in $EDITOR",
        description=(
            "Open the same form as `backlog create`, filled in with the task. Leaving the "
            "file unchanged changes nothing."
        ),
    )
    start = Start(
        help="Mark tasks in progress",
        description="Move one or more tasks to the doing status.",
    )
    block = Block(
        help="Mark tasks blocked",
        description="Move one or more tasks to the blocked status. They stay open.",
    )
    close = Close(
        help="Mark tasks done",
        description="Move one or more tasks to the done status, which stamps the closing time.",
    )
    cancel = Cancel(
        help="Mark tasks cancelled",
        description=(
            "Move one or more tasks to the cancelled status: kept and searchable, but closed. "
            "Prefer this to `backlog delete` for work that was dropped."
        ),
    )
    reopen = Reopen(
        help="Put tasks back on the pile",
        description="Move one or more tasks back to the todo status and clear the closing time.",
    )
    move = Move(
        help="Move a task into another project",
        description=(
            "Give a task to another board. The destination is a project slug or any path "
            "inside the project. The task keeps its id."
        ),
    )
    delete = Delete(
        help="Delete a task for good",
        description=(
            "Delete a task permanently. Ask first only for another project, unless --yes is given. "
            "`backlog cancel` keeps the record instead."
        ),
    )
    stats = Stats(
        help="Count open tasks, closed tasks, and notes",
        description=(
            "Show open tasks, closed tasks, and notes, followed by status and priority counts "
            "and tags. Notes include shared notes. --all-projects counts every board together."
        ),
    )
    note = Notes(
        help="Lasting user context: preferences, agreements, and constraints",
        description=(
            "Code changes; notes remain. Keep lasting user context unavailable elsewhere, not "
            "implementation details or documentation summaries. `note add` creates a note "
            "or replaces the one with that title, `note append` adds a paragraph, and `backlog "
            "search` finds notes as well as tasks. A note has an id for good, so a task body "
            "can say `note #3`."
        ),
    )
    skills = Skills(
        help="Show the skills this server offers to MCP clients",
        description=(
            "Print the written skills served over MCP: their names, what each one is for, and "
            "how many files it carries. Name a skill to read its instructions in full. Skills "
            "are text: a client reads them, then calls the tools."
        ),
    )
    install = Install(
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
        """Run the chosen command against one open backlog, or print the help when none was."""
        command = self.current_subparser
        if not isinstance(command, Command):
            self.print_help()
            return 0
        try:
            backlog = build(self)
        except ValueError as error:
            #: A computer name that is not one plain word.
            fail(str(error))
            return 2
        #: The command and the MCP handlers resolve the backlog the same way.
        use(backlog)
        try:
            if command.opens:
                await backlog.store.open()
            return await command()
        except UnknownProject as error:
            fail(str(error))
            return 2
        finally:
            await backlog.store.close()


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
