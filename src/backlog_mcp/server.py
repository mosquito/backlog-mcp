"""The MCP surface: tools, resources, prompts and completion over one backlog."""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any, Literal

from aiohttp_tiny_mcp import Exchange, Hint, NeedInput, Registry, elicit
from aiohttp_tiny_mcp.hub import NOTIFICATIONS, topic
from aiohttp_tiny_mcp.models import CompleteParams, TextResourceContents
from pydantic import BaseModel, Field

from . import embeddings, skills
from .backlog import Backlog, Reach, UnknownProject
from .models import (
    CLOSED_STATUSES,
    OPEN_STATUSES,
    PRIORITIES,
    Deleted,
    FullNotes,
    FullTasks,
    NoteOut,
    NoteListItem,
    Notes,
    Priority,
    ProjectOut,
    Projects,
    Scope,
    Stats,
    Status,
    TaskOut,
    TaskListItem,
    Tasks,
)
from .store import NOTE_LIMIT, Note, Project, Task

log = logging.getLogger(__name__)

VERSION = "0.1.0"

INSTRUCTIONS = """
A task backlog. It holds three kinds of thing: a project, which is one board covering one \
directory tree, a task, which is one item on that board, and a note, which is one piece of \
lasting context from the user that persists across sessions.

Scope. Every tool works on one project, the current one, unless the call names another. The \
current project is the one this server was started on, else the project pinned by \
`project_use`, else the project whose root contains the working directory, else a project \
registered for that directory on the spot. `project_current` reports which project is current \
and which of those reasons chose it. A computer reached over HTTP with nothing named and nothing \
pinned has no current project: `project_current` then says `origin: none`, and the tools that \
need a project ask for one.

Computers. A project is a directory tree on one computer, and one backlog can hold the projects \
of several computers. `project_current` reports the computer this session works from and every \
computer that has a project. Every project, task and note says which computer it is on, and a \
project says `here: false` when it is on another one. The `root` of such a project is a path on \
that computer: do not read, edit, build or run anything there from this one, and say so when a \
task needs it. Tasks and notes can still be read, created, edited and moved from anywhere.

Naming a project. A `project` argument takes a slug, such as `backlog-mcp`, or any path inside \
the project, such as `.` or `~/dev/thing`. A name that matches nothing is an error and creates \
nothing; `project_save` is the tool that registers a directory, and `project_list` shows every \
project with its open and total task counts.

Tasks. A task has a title, an optional body, a status (todo, doing, blocked, done or cancelled), \
a priority (urgent, high, normal or low) and tags. Listings show the open statuses (todo, doing, \
blocked), most important first, until `status` asks for others or `order="recent"` sorts by last \
change instead. Setting the status to done or cancelled stamps the closing time.

Notes. Code changes; notes remain. Keep lasting user context unavailable elsewhere: \
preferences, working agreements, people and responsibilities, business constraints, and reasons \
for decisions. Notes concern the context around the work, not the project's implementation. \
Do not save snapshots of code, architecture, configuration, bugs, or documentation. They become \
stale as the project changes. A useful note should still help after the code is rewritten. \
Project scope organizes notes; it does not define their subject.

Use `search` as a regular information source alongside web search. Check saved notes to recall \
past agreements, preferences, and constraints before making decisions or asking the user again. \
Use code, documentation, and the web for current technical facts. Retrieve relevant notes by \
search instead of reading them all. Recheck an old agreement when the user provides new context.

Save useful new context proactively as soon as the user supplies it. Do not wait for a request \
to remember it or for the session to end. Search first, then update an existing note or create \
one short note per topic. Use `note_save` to replace outdated context and `note_append` for \
related facts. A supplied body replaces the whole body. Omit guesses, duplicates, and secrets. \
Shared notes are visible across projects. Link notes by stable id, such as `note #3`.

Reach. `task_list`, `note_list`, `search` and `stats` stay inside one project, cover every \
project of this computer with `all_projects=true`, every project of another computer with \
`computer=<name>`, and everything with `all_computers=true`; `project_list` takes the same two \
computer arguments. Shared notes are visible from every project and every computer. Task and \
note ids are unique across projects and computers, so `task_get`, `task_update`, `task_move`, \
`task_delete`, `note_get`, `note_append` and `note_delete` take an id and no project.

Search. `search` is the default retrieval tool for project knowledge and previous work. Search at \
session start, before investigating a problem or choosing an approach, before asking the \
user for project facts, and before creating a task or note. Search again when a new question \
arises during work. Use a short natural-language question or specific terms; try another \
phrasing if the results are weak. Inspect the returned matches and fetch individual records \
with `note_get` or `task_get` only when more detail is needed. Check facts that may have changed. \
Avoid full listings and reading every task or note to find information. Reserve `task_list` \
and `note_list` for an explicitly requested inventory or a review that requires every record; \
use filters and pagination then. Use `stats` when only counts are needed.

`search` combines full-text prefix matching and multilingual semantic search over titles, \
bodies and tags. FTS matches come first, then semantic matches by cosine similarity, without \
duplicate IDs. Status filters apply to tasks; tag filters apply to both. Embeddings run \
locally; no text is sent to an external service.

Writes. `task_add`, `task_update`, `task_move`, `task_delete`, `note_save`, `note_append`, \
`note_delete` and `project_save` are the only tools that change anything; each one tells \
subscribed clients which resources went stale. `task_delete` and `note_delete` delete immediately \
within the current project and ask for confirmation when the record belongs to another project.

Skills. This server carries written skills: `triage`, for keeping a board true, and `planning`, \
for cutting a request into tasks. A client that speaks the skills extension gets them from \
`skills/list` and `skills/get`. Older clients read \
`mcp-extensions://io.modelcontextprotocol/skills/skills/list` through `resources/read`, \
then read the file URIs in that catalogue. Older clients see ordinary namespaced resources. \
Skills are instructions only: read them, then use the tools above to do the work.

Also available: the resources `backlog://project`, `backlog://projects`, `backlog://tasks`, \
`backlog://tasks/{id}`, `backlog://notes` and `backlog://notes/{id}` for reading without a tool \
call, and the prompts `standup`, which drafts an update from recent work, and `groom`, which \
reviews the open backlog.
""".strip()

#: The backlog the handlers work on. A context variable, so tests can run several at once.
ACTIVE: ContextVar[Backlog] = ContextVar("backlog")

registry = Registry("backlog", VERSION, instructions=INSTRUCTIONS)


async def active(ex: Exchange) -> Backlog:
    return ACTIVE.get()


registry.provide(Backlog, active)
registry.extension(embeddings.extension)


def use(backlog: Backlog) -> None:
    """Bind the backlog every handler resolves. Call it before serving."""
    ACTIVE.set(backlog)


def bound() -> Backlog:
    """The backlog `use` bound. Raises `LookupError` when nothing is bound yet."""
    return ACTIVE.get()


def project_out(project: Project, *, computer: str) -> ProjectOut:
    """The project as the tools return it; `computer` is the one this session works from."""
    return ProjectOut(
        id=project.id,
        slug=project.slug,
        name=project.name,
        computer=project.computer,
        root=project.root,
        here=project.computer == computer,
        description=project.description,
        archived=project.archived,
        open_tasks=project.open_tasks,
        total_tasks=project.total_tasks,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def task_out(task: Task) -> TaskOut:
    return TaskOut(
        id=task.id,
        project=task.project,
        computer=task.computer,
        title=task.title,
        body=task.body,
        status=task.status,
        priority=task.priority,
        tags=task.tags,
        created_at=task.created_at,
        updated_at=task.updated_at,
        closed_at=task.closed_at,
        snippet=task.snippet,
    )


def task_list_item(task: Task) -> TaskListItem:
    """The compact task shape used by listings."""
    return TaskListItem(
        id=task.id,
        project=task.project,
        computer=task.computer,
        title=task.title,
        status=task.status,
        priority=task.priority,
        tags=task.tags,
        created_at=task.created_at,
        updated_at=task.updated_at,
        closed_at=task.closed_at,
        snippet=task.body[:150],
    )


def note_out(note: Note) -> NoteOut:
    return NoteOut(
        id=note.id,
        project=note.project,
        computer=note.computer,
        title=note.title,
        body=note.body,
        tags=note.tags,
        shared=note.shared,
        created_at=note.created_at,
        updated_at=note.updated_at,
        snippet=note.snippet,
    )


def note_list_item(note: Note) -> NoteListItem:
    """The compact note shape used by listings."""
    return NoteListItem(
        id=note.id,
        project=note.project,
        computer=note.computer,
        title=note.title,
        tags=note.tags,
        shared=note.shared,
        created_at=note.created_at,
        updated_at=note.updated_at,
        snippet=note.body[:150],
    )


async def scope_of(backlog: Backlog) -> Scope:
    """Where the tools work, and why there."""
    project, origin = await backlog.scope()
    return Scope(
        project=project_out(project, computer=backlog.computer) if project else None,
        origin=origin,
        computer=backlog.computer,
        computers=await backlog.store.computers(),
        cwd=str(backlog.cwd),
        database=str(backlog.store.path),
        state=str(backlog.state.path),
    )


async def stats_of(backlog: Backlog, reach: Reach) -> Stats:
    """Counts for one project, for one computer, or for everything."""
    project_id, computer = reach.project_id, reach.computer
    by_priority = await backlog.store.group("priority", project_id=project_id, computer=computer)
    by_status = await backlog.store.group("status", project_id=project_id, computer=computer)
    return Stats(
        project=reach.slug,
        computer=reach.scope_computer,
        this_computer=backlog.computer,
        total=sum(by_status.values()),
        open=sum(by_status.get(status.value, 0) for status in OPEN_STATUSES),
        closed=sum(by_status.get(status.value, 0) for status in CLOSED_STATUSES),
        by_status=by_status,
        by_priority={
            PRIORITIES[int(rank)].value: count
            for rank, count in by_priority.items()
            if int(rank) in PRIORITIES
        },
        tags=await backlog.store.tags(project_id=project_id, computer=computer),
        notes=await backlog.store.count_notes(project_id=project_id, computer=computer),
    )


def statuses(wanted: list[Status] | None, *, default_open: bool = True) -> list[Status] | None:
    """The statuses to filter by. Listings show open tasks unless asked otherwise."""
    if wanted:
        return wanted
    return list(OPEN_STATUSES) if default_open else None


async def stale(uris: list[str]) -> None:
    """Tell subscribed clients which resources a write invalidated."""
    for uri in uris:
        await registry.hub.publish(
            topic(NOTIFICATIONS),
            {
                "jsonrpc": "2.0",
                "method": "notifications/resources/updated",
                "params": {"uri": uri},
            },
        )


async def changed(*task_ids: int) -> None:
    """A task or a project write: the boards and the touched tasks went stale."""
    uris = ["backlog://tasks", "backlog://projects", "backlog://project"]
    uris.extend(f"backlog://tasks/{task_id}" for task_id in task_ids)
    await stale(uris)


async def noted(*note_ids: int) -> None:
    """A note write: the note listing and the touched notes went stale."""
    await stale(["backlog://notes", *(f"backlog://notes/{note_id}" for note_id in note_ids)])


async def note_of(
    backlog: Backlog, note_id: int | None, title: str | None, project: str | None
) -> Note:
    """The note an id or a title names, or `LookupError`.

    A title is looked up in the resolved project first, then among the shared notes of the other
    projects, so a shared note reads from anywhere and only its own project can replace it.
    """
    if note_id is not None:
        found = await backlog.store.note(note_id)
        if found is None:
            raise LookupError(f"no note #{note_id}")
        return found
    if not title:
        raise ValueError("name the note by id or by title")
    owner = await backlog.resolve(project)
    found = await backlog.store.note_by_title(title, project_id=owner.id)
    if found is None:
        shared = [item for item in await backlog.store.notes(shared=True) if item.title == title]
        found = shared[0] if shared else None
    if found is None:
        raise LookupError(f"no note {title!r} in {owner.slug}")
    return found


class Nothing(BaseModel):
    pass


class ProjectRef(BaseModel):
    project: str | None = Field(
        default=None, description="Project slug, or any path inside it. Defaults to the current."
    )


class ListProjects(BaseModel):
    archived: bool = Field(default=False, description="Include archived projects.")
    computer: str | None = Field(
        default=None, description="Projects of this other computer instead of this one."
    )
    all_computers: bool = Field(default=False, description="Projects of every computer.")


class SaveProject(BaseModel):
    project: str | None = Field(
        default=None, description="Project to update. Defaults to the one for `root`, else current."
    )
    root: str | None = Field(
        default=None, description="Directory to register. Defaults to the working directory."
    )
    name: str | None = None
    description: str | None = None
    archived: bool | None = Field(
        default=None, description="Archive the project, or bring it back with false."
    )


class UseProject(BaseModel):
    project: str | None = Field(
        default=None, description="Project to pin. Omit to drop the pin and follow the directory."
    )


class AddTask(BaseModel):
    title: str
    body: str = ""
    status: Status = Status.TODO
    priority: Priority = Priority.NORMAL
    tags: list[str] = Field(default_factory=list)
    project: str | None = Field(default=None, description="Defaults to the current project.")


class ListTasks(BaseModel):
    project: str | None = Field(default=None, description="Defaults to the current project.")
    all_projects: bool = Field(
        default=False, description="List tasks from every project of this computer."
    )
    computer: str | None = Field(
        default=None, description="Every project of the named computer instead."
    )
    all_computers: bool = Field(default=False, description="Every project of every computer.")
    status: list[Status] | None = Field(
        default=None, description="Statuses to include. Defaults to the open ones."
    )
    tag: str | None = None
    priority: Priority | None = None
    order: Literal["backlog", "recent"] = Field(
        default="backlog", description="`backlog` is by priority, `recent` is by last change."
    )
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)
    full: bool = Field(
        default=False,
        description="Include complete task bodies. Defaults to compact task metadata and snippets.",
    )


class TaskId(BaseModel):
    id: int


class UpdateTask(BaseModel):
    id: int
    title: str | None = None
    body: str | None = None
    status: Status | None = None
    priority: Priority | None = None
    tags: list[str] | None = Field(default=None, description="Replaces the whole tag list.")


class MoveTask(BaseModel):
    id: int
    project: str = Field(description="Project to move the task into: a slug or a path inside it.")


class SaveNote(BaseModel):
    title: str | None = Field(
        default=None,
        description="Title for one lasting fact from the user. Optional with `id`.",
    )
    body: str | None = Field(
        default=None,
        max_length=NOTE_LIMIT,
        description=(
            "Lasting user context unavailable elsewhere; no implementation snapshots. "
            "Replaces the whole body. "
            f"At most {NOTE_LIMIT} characters."
        ),
    )
    tags: list[str] | None = Field(default=None, description="Replaces the whole tag list.")
    shared: bool | None = Field(default=None, description="Make the note visible everywhere.")
    id: int | None = Field(default=None, description="Change this note, whatever its title.")
    project: str | None = Field(default=None, description="Defaults to the current project.")


class AppendNote(BaseModel):
    text: str = Field(
        max_length=NOTE_LIMIT,
        description=(
            "One related fact from the user, unavailable elsewhere. "
            f"The whole note must stay within {NOTE_LIMIT} characters."
        ),
    )
    id: int | None = Field(default=None, description="The note to extend.")
    title: str | None = Field(default=None, description="Its title instead, in `project`.")
    project: str | None = Field(default=None, description="Defaults to the current project.")


class NoteId(BaseModel):
    id: int


class ReadNote(BaseModel):
    id: int | None = Field(default=None, description="The note to read.")
    title: str | None = Field(default=None, description="Its title instead, in `project`.")
    project: str | None = Field(default=None, description="Defaults to the current project.")


class ListNotes(BaseModel):
    project: str | None = Field(default=None, description="Defaults to the current project.")
    all_projects: bool = Field(
        default=False, description="List notes from every project of this computer."
    )
    computer: str | None = Field(
        default=None, description="Every project of the named computer instead."
    )
    all_computers: bool = Field(default=False, description="Every project of every computer.")
    tag: str | None = None
    shared: bool | None = Field(default=None, description="Only shared notes, or only local.")
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)
    full: bool = Field(
        default=False,
        description="Include complete note bodies. Defaults to compact note metadata and snippets.",
    )


class StatsOf(BaseModel):
    project: str | None = Field(default=None, description="Defaults to the current project.")
    all_projects: bool = Field(
        default=False, description="Count every project of this computer together."
    )
    computer: str | None = Field(
        default=None, description="Every project of the named computer instead."
    )
    all_computers: bool = Field(default=False, description="Every project of every computer.")


@registry.tool(title="Current project", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def project_current(args: Nothing, backlog: Backlog) -> Scope:
    """Report the current project, how it was selected, and storage paths."""
    return await scope_of(backlog)


@registry.tool(title="List projects", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def project_list(args: ListProjects, backlog: Backlog) -> Projects:
    """List the projects of this computer with open and total task counts.

    `computer` lists another computer's projects, `all_computers` every project there is."""
    reach = await backlog.reach(
        all_projects=True, computer=args.computer, all_computers=args.all_computers
    )
    found = await backlog.store.projects(archived=args.archived, computer=reach.computer)
    return Projects(
        items=[project_out(item, computer=backlog.computer) for item in found], count=len(found)
    )


@registry.tool(title="Register or update a project", annotations=Hint.IDEMPOTENT)
async def project_save(args: SaveProject, backlog: Backlog) -> ProjectOut:
    """Register a project directory or update its name, description, or archive status."""
    project = await backlog.save(
        reference=args.project,
        root=args.root,
        name=args.name,
        description=args.description,
        archived=args.archived,
    )
    await changed()
    return project_out(project, computer=backlog.computer)


@registry.tool(title="Pin a project", annotations=Hint.IDEMPOTENT)
async def project_use(args: UseProject, backlog: Backlog) -> Scope:
    """Pin the current project across sessions. Omit `project` to clear the pin.

    A project fixed at server startup takes precedence."""
    if args.project:
        project = await backlog.lookup(args.project)
        backlog.state.pin(backlog.computer, project.slug)
    else:
        backlog.state.pin(backlog.computer, None)
    return await scope_of(backlog)


@registry.tool(title="Add a task")
async def task_add(args: AddTask, backlog: Backlog) -> TaskOut:
    """Create a task in the selected project and return its id. Search for existing work first."""
    project = await backlog.resolve(args.project)
    task = await backlog.store.add_task(
        project_id=project.id,
        title=args.title,
        body=args.body,
        status=args.status,
        priority=args.priority,
        tags=args.tags,
    )
    await changed(task.id)
    return task_out(task)


@registry.tool(title="List tasks", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def task_list(args: ListTasks, backlog: Backlog) -> Tasks | FullTasks:
    """List tasks to review current work, inspect open items, or pick the next task to work on.

    Defaults to open tasks. Use filters and pagination."""
    reach = await backlog.reach(
        args.project,
        all_projects=args.all_projects,
        computer=args.computer,
        all_computers=args.all_computers,
    )
    found = await backlog.store.tasks(
        project_id=reach.project_id,
        computer=reach.computer,
        status=statuses(args.status),
        tag=args.tag,
        priority=args.priority,
        order=args.order,
        limit=args.limit,
        offset=args.offset,
    )
    total = await backlog.store.count_tasks(
        project_id=reach.project_id,
        computer=reach.computer,
        status=statuses(args.status),
        tag=args.tag,
        priority=args.priority,
    )
    result = dict(count=total, project=reach.slug, computer=reach.scope_computer)
    if args.full:
        return FullTasks(items=[task_out(item) for item in found], **result)
    return Tasks(
        items=[task_list_item(item) for item in found],
        **result,
    )


@registry.tool(title="Read a task", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def task_get(args: TaskId, backlog: Backlog) -> TaskOut:
    """Read one task by its globally unique id. Use after `search` when more detail is needed."""
    task = await backlog.store.task(args.id)
    if task is None:
        raise LookupError(f"no task #{args.id}")
    return task_out(task)


@registry.tool(title="Update a task", annotations=Hint.IDEMPOTENT)
async def task_update(args: UpdateTask, backlog: Backlog) -> TaskOut:
    """Update only the supplied task fields.

    Status `done` or `cancelled` sets the closing time; other statuses clear it."""
    task = await backlog.store.update_task(
        args.id,
        title=args.title,
        body=args.body,
        status=args.status,
        priority=args.priority,
        tags=args.tags,
    )
    if task is None:
        raise LookupError(f"no task #{args.id}")
    await changed(task.id)
    return task_out(task)


@registry.tool(title="Move a task", annotations=Hint.IDEMPOTENT)
async def task_move(args: MoveTask, backlog: Backlog) -> TaskOut:
    """Move a task to another project by slug or path. Preserve its id and other fields."""
    project = await backlog.lookup(args.project)
    task = await backlog.store.update_task(args.id, project_id=project.id)
    if task is None:
        raise LookupError(f"no task #{args.id}")
    await changed(task.id)
    return task_out(task)


async def confirm_cross_project_delete(
    record: Task | Note, ex: Exchange, backlog: Backlog
) -> Deleted | None:
    """Ask only when the record belongs to another project."""
    current, _ = await backlog.current()
    if record.project_id == current.id:
        return None
    key = f"delete:{record.project_id}:{record.id}"
    if (answer := ex.answers.get(key)) is not None:
        if answer.get("ok"):
            return None
        return Deleted(id=record.id, title=record.title, deleted=False, detail="cancelled")
    if not ex.can_ask:
        return Deleted(
            id=record.id,
            title=record.title,
            deleted=False,
            detail="this client cannot confirm deletion from another project",
        )
    kind = "task" if isinstance(record, Task) else "note"
    where = (
        record.project
        if record.computer == current.computer
        else f"{record.project} on {record.computer}"
    )
    raise NeedInput(
        {
            key: elicit(
                f"Delete {kind} #{record.id} ({record.title!r}) from {where!r}? "
                f"The current project is {current.slug!r}. This cannot be undone.",
                {"type": "object", "properties": {"ok": {"type": "boolean"}}},
            )
        }
    )


@registry.tool(title="Delete a task", annotations=Hint.DESTRUCTIVE | ~Hint.OPEN_WORLD)
async def task_delete(args: TaskId, ex: Exchange, backlog: Backlog) -> Deleted:
    """Delete a task permanently. Confirm only when it belongs to another project.

    Set status to `cancelled` instead to retain its history."""
    task = await backlog.store.task(args.id)
    if task is None:
        raise LookupError(f"no task #{args.id}")
    if refusal := await confirm_cross_project_delete(task, ex, backlog):
        return refusal
    deleted = await backlog.store.delete_task(args.id)
    await changed(args.id)
    return Deleted(id=args.id, title=task.title, deleted=deleted)


@registry.tool(title="Save a note", annotations=Hint.IDEMPOTENT)
async def note_save(args: SaveNote, backlog: Backlog) -> NoteOut:
    """Save lasting user context unavailable elsewhere: preferences, agreements, or constraints.

    Save useful new knowledge promptly; do not wait to be asked. Search first to avoid duplicates.
    Code changes; notes remain. Exclude implementation details and documentation summaries.
    Update by id or project-local title; otherwise create a note. Only supplied fields change.
    A supplied body replaces the whole body. Use id to update another project's shared note."""
    if args.id is None:
        if not args.title:
            raise ValueError("a new note needs a title")
        project = await backlog.resolve(args.project)
        existing = await backlog.store.note_by_title(args.title, project_id=project.id)
        if existing is None:
            saved = await backlog.store.add_note(
                project_id=project.id,
                title=args.title,
                body=args.body or "",
                tags=args.tags or (),
                shared=bool(args.shared),
            )
            await noted(saved.id)
            return note_out(saved)
        args.id = existing.id
    updated = await backlog.store.update_note(
        args.id, title=args.title, body=args.body, tags=args.tags, shared=args.shared
    )
    if updated is None:
        raise LookupError(f"no note #{args.id}")
    await noted(updated.id)
    return note_out(updated)


@registry.tool(title="Append to a note")
async def note_append(args: AppendNote, backlog: Backlog) -> NoteOut:
    """Append a short, related fact about lasting user context unavailable elsewhere.

    Find the note with `search` first. Use `note_save` to correct existing content."""
    found = await note_of(backlog, args.id, args.title, args.project)
    updated = await backlog.store.append_note(found.id, args.text)
    if updated is None:
        raise LookupError(f"no note #{found.id}")
    await noted(updated.id)
    return note_out(updated)


@registry.tool(title="Read a note", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def note_get(args: ReadNote, backlog: Backlog) -> NoteOut:
    """Read saved user context, preferences, conventions, or past agreements.

    ALWAYS check notes whenever you do not know what to do, what style or architecture to follow,
    or what the user prefers. Check notes before asking questions or making assumptions.
    Ids work across projects. Titles resolve within the selected project and shared notes."""
    return note_out(await note_of(backlog, args.id, args.title, args.project))


@registry.tool(title="List notes", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def note_list(args: ListNotes, backlog: Backlog) -> Notes | FullNotes:
    """List persistent notes containing user context, preferences, and agreements.

    Browse notes whenever you lack direction, are unsure how to proceed, or need rules.
    For targeted topic search, prefer `search`. Use filters and pagination."""
    reach = await backlog.reach(
        args.project,
        all_projects=args.all_projects,
        computer=args.computer,
        all_computers=args.all_computers,
    )
    found = await backlog.store.notes(
        project_id=reach.project_id,
        computer=reach.computer,
        tag=args.tag,
        shared=args.shared,
        limit=args.limit,
        offset=args.offset,
    )
    total = await backlog.store.count_notes(
        project_id=reach.project_id, computer=reach.computer, tag=args.tag, shared=args.shared
    )
    result = dict(count=total, project=reach.slug, computer=reach.scope_computer)
    if args.full:
        return FullNotes(items=[note_out(item) for item in found], **result)
    return Notes(items=[note_list_item(item) for item in found], **result)


@registry.tool(title="Delete a note", annotations=Hint.DESTRUCTIVE | ~Hint.OPEN_WORLD)
async def note_delete(args: NoteId, ex: Exchange, backlog: Backlog) -> Deleted:
    """Delete a note permanently. Confirm only when it belongs to another project.

    References to the deleted note remain unchanged."""
    found = await backlog.store.note(args.id)
    if found is None:
        raise LookupError(f"no note #{args.id}")
    if refusal := await confirm_cross_project_delete(found, ex, backlog):
        return refusal
    deleted = await backlog.store.delete_note(args.id)
    await noted(args.id)
    return Deleted(id=args.id, title=found.title, deleted=deleted)


@registry.tool(title="Search the backlog", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def search(args: embeddings.Search, backlog: Backlog) -> embeddings.Matches:
    """Find tasks, past agreements, and user context. Use regularly alongside web search.

    Prefer targeted search over full listings. Read individual matches only when needed.
    Combines FTS prefixes and multilingual semantic search, with FTS matches first.
    Defaults to the current project plus shared notes."""
    return await embeddings.search(args, backlog)


@registry.tool(title="Backlog statistics", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def stats(args: StatsOf, backlog: Backlog) -> Stats:
    """Count open tasks, closed tasks, and notes, with status and priority breakdowns and tags."""
    reach = await backlog.reach(
        args.project,
        all_projects=args.all_projects,
        computer=args.computer,
        all_computers=args.all_computers,
    )
    return await stats_of(backlog, reach)


@registry.resource("backlog://project", mime_type="application/json")
async def current_project(args: Nothing, backlog: Backlog) -> dict[str, Any]:
    """The project the tools work on, or `null` with origin `none` when there is none."""
    project, origin = await backlog.scope()
    shown = project_out(project, computer=backlog.computer).model_dump() if project else None
    return {"project": shown, "origin": origin.value, "computer": backlog.computer}


@registry.resource("backlog://projects", mime_type="application/json")
async def all_projects(args: Nothing, backlog: Backlog) -> list[dict[str, Any]]:
    """Every registered project of this computer."""
    found = await backlog.store.projects(computer=backlog.computer)
    return [project_out(item, computer=backlog.computer).model_dump() for item in found]


@registry.resource("backlog://tasks", mime_type="application/json")
async def open_tasks(args: Nothing, backlog: Backlog) -> list[dict[str, Any]]:
    """Open tasks of the current project, most important first."""
    project = await backlog.resolve(None)
    found = await backlog.store.tasks(project_id=project.id, status=list(OPEN_STATUSES), limit=200)
    return [task_out(item).model_dump() for item in found]


class TaskUri(BaseModel):
    id: str


@registry.resource("backlog://tasks/{id}", name="task", mime_type="application/json")
async def one_task(args: TaskUri, backlog: Backlog) -> dict[str, Any]:
    """One task by id."""
    if not args.id.isdigit():
        return {"error": f"invalid task id {args.id!r}"}
    task = await backlog.store.task(int(args.id))
    if task is None:
        return {"error": f"no task #{args.id}"}
    return task_out(task).model_dump()


@registry.resource("backlog://notes", mime_type="application/json")
async def known_notes(args: Nothing, backlog: Backlog) -> list[dict[str, Any]]:
    """Notes of the current project plus every shared note, most recently changed first."""
    project = await backlog.resolve(None)
    found = await backlog.store.notes(project_id=project.id, limit=200)
    return [note_out(item).model_dump() for item in found]


@registry.resource("backlog://notes/{id}", name="note", mime_type="application/json")
async def one_note(args: TaskUri, backlog: Backlog) -> dict[str, Any]:
    """One note by id."""
    if not args.id.isdigit():
        return {"error": f"invalid note id {args.id!r}"}
    found = await backlog.store.note(int(args.id))
    if found is None:
        return {"error": f"no note #{args.id}"}
    return note_out(found).model_dump()


class SkillName(BaseModel):
    name: str


class SkillFile(BaseModel):
    name: str
    file: str


@skills.extension.resource(skills.LIST_URI, name="skills", mime_type="application/json")
async def skill_list(args: Nothing) -> list[dict[str, Any]]:
    """Every skill on offer: the URI of its instructions, its frontmatter and its files."""
    return [item.entry() for item in skills.library.skills()]


@skills.extension.resource(skills.ENTRY_TEMPLATE, name="skill_entry", mime_type="application/json")
async def skill_entry(args: SkillName) -> dict[str, Any]:
    """One skill by name, described the same way."""
    found = skills.library.skill(args.name)
    if found is None:
        return {"error": f"no skill {args.name!r}"}
    return found.entry()


@skills.extension.resource(f"{skills.SCHEME}{{name}}/{skills.MANIFEST}", name="skill")
async def skill_manifest(args: SkillName) -> TextResourceContents | dict[str, Any]:
    """The instructions of one skill, as they stand on disk."""
    found = skills.library.file(args.name, skills.MANIFEST)
    if found is None:
        return {"error": f"no skill {args.name!r}"}
    return found.contents()


@skills.extension.resource(
    f"{skills.SCHEME}{{name}}/{skills.REFERENCES}/{{file}}", name="skill_reference"
)
async def skill_reference(args: SkillFile) -> TextResourceContents | dict[str, Any]:
    """One text file the instructions of a skill cite."""
    found = skills.library.file(args.name, f"{skills.REFERENCES}/{args.file}")
    if found is None:
        return {"error": f"no file {args.file!r} in skill {args.name!r}"}
    return found.contents()


registry.extension(skills.extension)


class Standup(BaseModel):
    project: str | None = None
    all_projects: bool = False
    computer: str | None = None
    all_computers: bool = False


@registry.prompt
async def standup(args: Standup, backlog: Backlog) -> str:
    """Draft a standup update from what closed recently and what is still open."""
    reach = await backlog.reach(
        args.project,
        all_projects=args.all_projects,
        computer=args.computer,
        all_computers=args.all_computers,
    )
    done = await backlog.store.tasks(
        project_id=reach.project_id,
        computer=reach.computer,
        status=[Status.DONE],
        order="recent",
        limit=10,
    )
    open_ = await backlog.store.tasks(
        project_id=reach.project_id,
        computer=reach.computer,
        status=list(OPEN_STATUSES),
        limit=15,
    )
    lines = [f"- done: {item.title}" for item in done]
    lines += [f"- {item.status.value} ({item.priority.value}): {item.title}" for item in open_]
    return (
        f"Backlog for {reach.label}:\n\n"
        + ("\n".join(lines) or "- nothing recorded yet")
        + "\n\nWrite a short standup update: done, next, blockers."
    )


class Groom(BaseModel):
    project: str | None = None
    limit: int = Field(default=30, ge=1, le=200)


@registry.prompt
async def groom(args: Groom, backlog: Backlog) -> str:
    """Ask for a review of the open backlog: order, duplicates and missing detail."""
    project = await backlog.resolve(args.project)
    found = await backlog.store.tasks(
        project_id=project.id, status=list(OPEN_STATUSES), limit=args.limit
    )
    lines = [
        f"- #{item.id} [{item.priority.value}/{item.status.value}] {item.title}"
        + (f" (tags: {', '.join(item.tags)})" if item.tags else "")
        for item in found
    ]
    return (
        f"Open backlog of {project.slug} ({project.root} on {project.computer}):\n\n"
        + ("\n".join(lines) or "- nothing open")
        + "\n\nReview it: what should be done first, what duplicates what, "
        "and which items are too vague to start. Suggest concrete edits."
    )


@registry.completions
async def complete(args: CompleteParams, backlog: Backlog) -> list[str]:
    """Complete project slugs, tags, statuses, priorities, task ids and note ids."""
    value = args.argument.value
    #: The task and the note templates both call their argument `id`; the URI tells them apart.
    about_notes = "notes" in (args.ref.uri or "")
    match args.argument.name, args.ref.type:
        case "project", _:
            return [slug for slug in await backlog.store.slugs() if slug.startswith(value)]
        case "computer", _:
            return [name for name in await backlog.store.computers() if name.startswith(value)]
        case "tag", _:
            project, _origin = await backlog.current()
            tags = await backlog.store.tags(project_id=project.id)
            return [tag for tag in tags if tag.startswith(value)]
        case "status", _:
            return [item.value for item in Status if item.value.startswith(value)]
        case "priority", _:
            return [item.value for item in Priority if item.value.startswith(value)]
        case "id", "ref/resource" if about_notes:
            project, _origin = await backlog.current()
            notes = await backlog.store.notes(project_id=project.id, limit=200)
            return [str(item.id) for item in notes if str(item.id).startswith(value)]
        case "id", "ref/resource":
            project, _origin = await backlog.current()
            found = await backlog.store.tasks(project_id=project.id, limit=200)
            return [str(item.id) for item in found if str(item.id).startswith(value)]
        case _:
            return []


__all__ = ["INSTRUCTIONS", "VERSION", "UnknownProject", "bound", "registry", "use"]
