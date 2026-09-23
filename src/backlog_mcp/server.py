"""The MCP surface: tools, resources, prompts and completion over one backlog."""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import Iterable, Sequence
from contextvars import ContextVar
from importlib.resources import files
from typing import Any, Literal

import aiosqlite
from aiohttp_tiny_mcp import Authentication, Exchange, Extension, Hint, NeedInput, Registry, elicit
from aiohttp_tiny_mcp.protocol.models import CompleteParams, TextResourceContents
from aiohttp_tiny_mcp.storage.hub import NOTIFICATIONS, topic
from pydantic import BaseModel, Field, field_serializer

from . import embeddings, guidance, skills
from .backlog import (
    Backlog,
    Reach,
    UnknownProject,
    attach_roots,
    computers,
    members,
    project_contents,
    read_project,
    read_projects,
    require,
    share,
    slugs,
)
from .computer import hostname
from .models import (
    CLOSED_STATUSES,
    EVENT_KEY,
    OPEN_STATUSES,
    PRIORITIES,
    RANKS,
    Action,
    Actor,
    BoardCounts,
    BoardProject,
    BoardSection,
    BoardTask,
    CommentOut,
    Comments,
    Deleted,
    Event,
    FullNotes,
    FullTasks,
    Identity,
    Kind,
    LinkChange,
    NoteListItem,
    NoteOut,
    Notes,
    NullableAssignment,
    NullableString,
    Priority,
    ProjectBoard,
    ProjectDeleted,
    ProjectOut,
    ProjectRootOut,
    Projects,
    Relation,
    Stats,
    Status,
    TaskDetails,
    TaskLinkOut,
    TaskListItem,
    TaskOut,
    Tasks,
    check_assignment,
    relation,
)
from .names import owner_name, principal_label, principal_user
from .protocol_storage import ProtocolHub, ProtocolSessions, ProtocolStorage
from .queries import QUERIES
from .records import (
    NOTE_LIMIT,
    AssignmentConflict,
    Comment,
    Note,
    Project,
    Task,
    Unset,
    assignment_pattern,
    check_note,
    join_tags,
    match_query,
    split_tags,
    to_note,
    to_project,
    to_task,
)
from .transaction import Transaction
from .version import VERSION

log = logging.getLogger(__name__)

INSTRUCTIONS = """Backlog is a task tracker and memory for agents.
Start with project_board(project). For workflow rules and topic help, call guide().
The same Markdown is available at backlog://guide and backlog://guide/<topic>.
""".strip()

#: The backlog the handlers work on. A context variable, so tests can run several at once.
ACTIVE: ContextVar[Backlog] = ContextVar("backlog")

# Return the whole catalogue for clients that do not follow nextCursor.
registry = Registry("backlog", VERSION, instructions=INSTRUCTIONS)


async def active(ex: Exchange) -> Backlog:
    return ACTIVE.get()


async def active_connection(ex: Exchange) -> aiosqlite.Connection:
    return await ACTIVE.get().open()


async def active_transaction(ex: Exchange) -> Transaction:
    backlog = ACTIVE.get()
    await backlog.open()
    return backlog.transaction


registry.provide(Backlog, active)
registry.provide(aiosqlite.Connection, active_connection)
registry.provide(Transaction, active_transaction)
registry.extension(embeddings.extension)

#: The extension that carries the change notifications; see `changed`.
EVENTS = "backlog-mcp/events"
TASK_CHANGED = "notifications/backlog/task"
NOTE_CHANGED = "notifications/backlog/memory"
PROJECT_CHANGED = "notifications/backlog/project"
#: Task and note events use owner-qualified project topics.
#: Project events go to every authorized listener.
events = Extension(
    EVENTS,
    notifications={TASK_CHANGED: "project", NOTE_CHANGED: "project", PROJECT_CHANGED: None},
)
registry.extension(events)
METHODS = {Kind.TASK: TASK_CHANGED, Kind.NOTE: NOTE_CHANGED, Kind.PROJECT: PROJECT_CHANGED}


ACTIVE_REGISTRY: ContextVar[Registry] = ContextVar("backlog_registry", default=registry)


def persistent_registry(
    backlog: Backlog, policies: Sequence[Authentication] = ()
) -> tuple[Registry, ProtocolStorage]:
    """Use the application database for protocol sessions and notification events."""
    storage = ProtocolStorage(backlog)
    configured = Registry(
        "backlog",
        VERSION,
        instructions=registry.instructions,
        hub=ProtocolHub(storage),
        session_store=ProtocolSessions(storage),
        auth=policies or None,
        page_size=registry.page_size,
    )
    configured.tools = registry.tools
    configured.resources_fixed = registry.resources_fixed
    configured.resources_templated = registry.resources_templated
    configured.prompts = registry.prompts
    configured.completer = registry.completer
    configured.providers = registry.providers
    configured.extensions = registry.extensions
    configured.resource_aliases = registry.resource_aliases
    return configured, storage


def use(backlog: Backlog) -> None:
    """Bind the backlog every handler resolves. Call it before serving."""
    ACTIVE.set(backlog)


def bound() -> Backlog:
    """The backlog `use` bound. Raises `LookupError` when nothing is bound yet."""
    return ACTIVE.get()


def project_out(project: Project, *, computer: str, principal: str) -> ProjectOut:
    """The project as the tools return it; `computer` is the one this session works from."""
    return ProjectOut(
        id=project.id,
        slug=project.slug,
        full_name=project.full_name,
        owner=project.owner,
        role=project.role,
        name=project.name,
        roots=[
            ProjectRootOut(principal=principal, computer=root.computer, root=root.root)
            for root in project.roots
            if root.root
        ],
        here=any(root.computer == computer and root.root for root in project.roots),
        description=project.description,
        preferred_language=project.preferred_language,
        archived=project.archived,
        open_tasks=project.open_tasks,
        total_tasks=project.total_tasks,
        created_by=project.created_by,
        updated_by=project.updated_by,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def task_out(task: Task) -> TaskOut:
    return TaskOut(
        id=task.id,
        project=task.project,
        title=task.title,
        body=task.body,
        status=task.status,
        assigned=task.assigned,
        priority=task.priority,
        tags=task.tags,
        created_by=task.created_by,
        updated_by=task.updated_by,
        created_at=task.created_at,
        updated_at=task.updated_at,
        closed_at=task.closed_at,
        snippet=task.snippet,
    )


def compact_snippet(body: str, limit: int = 150) -> str:
    """Return the first meaningful markdown paragraph, shortened at a word boundary."""
    paragraph: list[str] = []
    for line in body.splitlines():
        text = line.strip()
        if not text:
            if paragraph:
                break
            continue
        if not paragraph and text.startswith("#"):
            continue
        paragraph.append(text)
    text = " ".join(paragraph)
    if len(text) <= limit:
        return text
    shortened = text[: limit - 1].rsplit(maxsplit=1)[0]
    return f"{shortened or text[: limit - 1]}…"


def comment_out(comment: Comment) -> CommentOut:
    return CommentOut(
        id=comment.id,
        task_id=comment.task_id,
        task_assigned=comment.task_assigned,
        project=comment.project,
        body=comment.body,
        kind=comment.kind,
        principal=comment.principal,
        computer=comment.computer,
        created_at=comment.created_at,
    )


async def task_details(task: Task, backlog: Backlog) -> TaskDetails:
    connection = await backlog.open()
    comments = await read_comments(connection, backlog.actor, task_id=task.id)
    return TaskDetails(
        **task_out(task).model_dump(),
        comments=[comment_out(item) for item in comments],
        links=await task_links(connection, backlog.actor, task.id),
    )


def task_list_item(task: Task) -> TaskListItem:
    """The compact task shape used by listings."""
    return TaskListItem(
        id=task.id,
        project=task.project,
        title=task.title,
        status=task.status,
        assigned=task.assigned,
        priority=task.priority,
        tags=task.tags,
        created_by=task.created_by,
        updated_by=task.updated_by,
        created_at=task.created_at,
        updated_at=task.updated_at,
        closed_at=task.closed_at,
        snippet=compact_snippet(task.body),
    )


def note_out(note: Note) -> NoteOut:
    return NoteOut(
        id=note.id,
        project=note.project,
        title=note.title,
        body=note.body,
        tags=note.tags,
        shared=note.shared,
        created_by=note.created_by,
        updated_by=note.updated_by,
        created_at=note.created_at,
        updated_at=note.updated_at,
        snippet=note.snippet,
    )


def note_list_item(note: Note) -> NoteListItem:
    """The compact memory entry shape used by listings."""
    return NoteListItem(
        id=note.id,
        project=note.project,
        title=note.title,
        tags=note.tags,
        shared=note.shared,
        created_by=note.created_by,
        updated_by=note.updated_by,
        created_at=note.created_at,
        updated_at=note.updated_at,
        snippet=compact_snippet(note.body),
    )


async def stats_of(backlog: Backlog, reach: Reach) -> Stats:
    """Counts for one project, for one computer, or for everything."""
    connection = await backlog.open()
    project_id, computer = reach.project_id, reach.computer
    by_priority = await group(
        connection, backlog.actor, "priority", project_id=project_id, computer=computer
    )
    by_status = await group(
        connection, backlog.actor, "status", project_id=project_id, computer=computer
    )
    return Stats(
        project=reach.slug,
        computer=backlog.computer,
        principal=backlog.principal,
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
        tags=await read_tags(connection, backlog.actor, project_id=project_id, computer=computer),
        memory=await count_notes(
            connection, backlog.actor, project_id=project_id, computer=computer
        ),
    )


def statuses(wanted: list[Status] | None, *, default_open: bool = True) -> list[Status] | None:
    """The statuses to filter by. Listings show open tasks unless asked otherwise."""
    if wanted:
        return wanted
    return list(OPEN_STATUSES) if default_open else None


async def stale(uris: list[str], event: Event) -> None:
    """Tell subscribed clients which resources a write invalidated, and what the write did.

    The event travels in `_meta` of each notification, so a subscriber does not have to read
    the resource again to learn what happened.
    """
    meta = {EVENT_KEY: event.model_dump(mode="json", exclude_none=True)}
    for uri in uris:
        await ACTIVE_REGISTRY.get().hub.publish(
            topic(NOTIFICATIONS),
            {
                "jsonrpc": "2.0",
                "method": "notifications/resources/updated",
                "params": {"uri": uri, "_meta": meta},
            },
        )


async def publish_change(event: Event) -> None:
    """One write: the resources it touched went stale, and every listener hears the event.

    Subscribers by URI get `resources/updated` with the event in `_meta`; listeners of the
    `backlog-mcp/events` extension get the event as `notifications/backlog/<kind>`, task and
    memory entry events only for the projects they named.
    """
    match event.kind:
        case Kind.TASK:
            uris = ["backlog://tasks", "backlog://projects", "backlog://project"]
            uris.append(f"backlog://tasks/{event.id}")
        case Kind.NOTE:
            uris = ["backlog://memory", f"backlog://memory/{event.id}"]
        case Kind.PROJECT:
            uris = ["backlog://tasks", "backlog://projects", "backlog://project"]
            if event.action is Action.DELETED:
                #: The notes of the project went with it.
                uris.append("backlog://memory")
    await stale(uris, event)
    await ACTIVE_REGISTRY.get().broadcast(
        METHODS[event.kind], event.model_dump(mode="json", exclude_none=True)
    )


async def changed(event: Event, *, recipients: list[str] | None = None) -> None:
    """Publish only to the current members of the affected project."""
    from aiohttp_tiny_mcp.storage.namespaces import namespace

    backlog = bound()
    connection = await backlog.open()
    event.principal = backlog.principal
    if recipients is None:
        recipients = [backlog.principal]
        if event.project_id is not None:
            with contextlib.suppress(LookupError):
                recipients = [
                    member["principal"]
                    for member in await members(connection, backlog.actor, event.project_id)
                ]
    for identity in recipients:
        anonymous_default = ACTIVE_REGISTRY.get().auth is None and identity == backlog.principal
        token = namespace.set(None if anonymous_default else identity)
        try:
            await publish_change(event)
        finally:
            namespace.reset(token)


async def linked_tasks(
    backlog: Backlog, *, task_id: int | None = None, project_id: int | None = None
) -> list[Task]:
    """Find resource dependents for notification routing, never for a public response."""
    connection = await backlog.open()
    return await read_linked_tasks(
        connection, backlog.actor, task_id=task_id, project_id=project_id
    )


async def invalidate_linked_tasks(tasks: list[Task], backlog: Backlog) -> None:
    """Invalidate neighbor resources without claiming the neighbor was edited.

    Each recipient receives only metadata from their own accessible task. A link may cross
    project boundaries, so the source event must not be copied into the neighbor's namespace.
    """
    connection = await backlog.open()
    from aiohttp_tiny_mcp.storage.namespaces import namespace

    for task in tasks:
        recipients = await notification_recipients(connection, backlog.actor, task.project_id)
        event = task_event(task, Action.UPDATED, computer=backlog.computer)
        for identity in recipients:
            anonymous = ACTIVE_REGISTRY.get().auth is None and identity == backlog.principal
            token = namespace.set(None if anonymous else identity)
            try:
                await stale([f"backlog://tasks/{task.id}"], event)
            finally:
                namespace.reset(token)


def task_event(task: Task, action: Action, *, computer: str) -> Event:
    return Event(
        kind=Kind.TASK,
        action=action,
        id=task.id,
        project=task.project,
        project_id=task.project_id,
        title=task.title,
        status=task.status,
        priority=task.priority,
        computer=computer,
        principal=bound().principal,
    )


def note_event(note: Note, action: Action, *, computer: str) -> Event:
    return Event(
        kind=Kind.NOTE,
        action=action,
        id=note.id,
        project=note.project,
        project_id=note.project_id,
        title=note.title,
        computer=computer,
        principal=bound().principal,
    )


def project_event(project: Project, action: Action, *, computer: str) -> Event:
    return Event(
        kind=Kind.PROJECT,
        action=action,
        id=project.id,
        project=project.full_name,
        project_id=project.id,
        title=project.name,
        computer=computer,
        principal=bound().principal,
    )


async def note_of(
    backlog: Backlog, note_id: int | None, title: str | None, project: str | None
) -> Note:
    """The memory entry an id or a title names, or `LookupError`.

    A title is looked up in the resolved project first, then among shared memory entries
    of other projects. Only the owning project can replace it by title.
    """
    connection = await backlog.open()
    if note_id is not None:
        found = await read_note(connection, backlog.actor, note_id)
        if found is None:
            raise LookupError(f"no note #{note_id}")
        return found
    if not title:
        raise ValueError("name the note by id or by title")
    owner = await backlog.resolve(project)
    found = await note_by_title(connection, backlog.actor, title, project_id=owner.id)
    if found is None:
        shared = [
            item
            for item in await read_notes(connection, backlog.actor, shared=True)
            if item.title == title
        ]
        found = shared[0] if shared else None
    if found is None:
        raise LookupError(f"no note {title!r} in {owner.full_name}")
    return found


class Nothing(BaseModel):
    pass


class ProjectRef(BaseModel):
    project: str | None = Field(
        default=None,
        description=(
            "Project owner/slug, unique short slug, #id, or path. Defaults to connection "
            "project scope; required if unscoped."
        ),
    )


class ShareProject(ProjectRef):
    principal: str = Field(
        description="user@computer or a username without @; computer does not affect access."
    )
    role: Literal["reader", "editor"] = "reader"


class Members(Actor):
    items: list[dict[str, str]]
    roots: list[ProjectRootOut] = Field(default_factory=list)

    @field_serializer("items", when_used="json")
    def principal_labels(self, values: list[dict[str, str]]) -> list[dict[str, str]]:
        return [{**item, "principal": principal_label(item["principal"])} for item in values]


@registry.tool()
async def project_members(
    args: ProjectRef, backlog: Backlog, connection: aiosqlite.Connection
) -> Members:
    """List project members, roles and their computer/path bindings. Members only."""
    project = await backlog.resolve(args.project)
    return await member_details(backlog, connection, project.id)


async def member_details(
    backlog: Backlog, connection: aiosqlite.Connection, project_id: int
) -> Members:
    return Members(
        items=await members(connection, backlog.actor, project_id),
        roots=[
            ProjectRootOut(**dict(row))
            async for row in QUERIES.member_roots(connection, project_id=project_id)
        ],
        principal=backlog.principal,
        computer=backlog.computer,
    )


@registry.tool()
async def project_share(
    args: ShareProject, backlog: Backlog, connection: aiosqlite.Connection, transaction: Transaction
) -> Members:
    """Grant read or edit access to a registered principal. Only the owner can share."""
    from .auth import user_identity

    project = await backlog.resolve(args.project)
    principal = user_identity(principal_user(args.principal))
    async with transaction:
        await share(connection, backlog.actor, project.id, principal, args.role)
    embeddings.invalidate(backlog)
    await changed(project_event(project, Action.UPDATED, computer=backlog.computer))
    return await member_details(backlog, connection, project.id)


class ListProjects(BaseModel):
    archived: bool = Field(default=False, description="Include archived projects.")
    computer: str | None = Field(
        default=None, description="Projects of this other computer instead of this one."
    )
    all_computers: bool = Field(default=False, description="Projects of every computer.")


class SaveProject(BaseModel):
    project: str | None = Field(
        default=None,
        description=(
            "Project to update. Defaults to the one for `root`, else connection project scope."
        ),
    )
    root: str | None = Field(
        default=None, description="Directory to register. Defaults to the working directory."
    )
    name: str | None = None
    description: str | None = None
    preferred_language: str | None = Field(
        default=None,
        description=(
            "Language to use for project work and task text. "
            "Omit to keep the current value; null clears it. New projects default to null."
        ),
    )
    archived: bool | None = Field(
        default=None, description="Archive the project, or bring it back with false."
    )


class DeleteProject(BaseModel):
    project: str = Field(
        description="Project owner/slug, unique short slug, #id, or explicit path."
    )


class AddTask(BaseModel):
    title: str = Field(description="Clear title stating what outcome is to be achieved.")
    body: str = Field(
        default="",
        description=(
            "Reason, constraints and acceptance criteria. Express dependencies, parents "
            "and causes with task_link."
        ),
    )
    status: Status = Field(
        default=Status.TODO,
        description="Initial status (todo, doing, blocked, done, cancelled).",
    )
    priority: Priority = Field(
        default=Priority.NORMAL,
        description="Priority ranking (urgent, high, normal, low).",
    )
    tags: list[str] = Field(default_factory=list, description="Tags for grouping and filtering.")
    project: str | None = Field(
        default=None,
        description=(
            "Defaults to connection project scope (?project= in HTTP, --project on "
            "stdio); required if unscoped."
        ),
    )


class ListTasks(BaseModel):
    project: str | None = Field(
        default=None,
        description=(
            "Defaults to connection project scope (?project= in HTTP, --project on "
            "stdio); required if unscoped."
        ),
    )
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


class ClaimTask(BaseModel):
    model_config = {"extra": "forbid"}

    task: int = Field(description="Task id to claim, release or reassign.")
    expected: NullableAssignment = Field(
        description="Expected current assignment; null requires an unassigned task."
    )
    assigned: NullableAssignment = Field(
        description="New username@computer:marker of the current principal; null releases."
    )
    temporary: bool = Field(
        default=False,
        description="Begin an exclusive edit claim; remember the previous assignment.",
    )
    restore: bool = Field(
        default=False,
        description="Restore the assignment saved by an edit claim. assigned must be null.",
    )


class AssignedTasks(BaseModel):
    prefix: NullableString = Field(
        default=None,
        description="Literal prefix; null or omitted selects the current username@computer:.",
    )
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class UnlinkTask(BaseModel):
    task: int = Field(description="Source task id.")
    relation: Relation = Field(description="Relation from task to target.")
    target: int = Field(description="Target task id.")


class LinkTask(UnlinkTask):
    reason: str | None = Field(
        default=None, description="Explanation. Omit to preserve it; an empty string clears it."
    )


class AddComment(BaseModel):
    task_id: int
    body: str = Field(
        min_length=1, description="New Markdown comment. Does not change the task description."
    )


class ListComments(BaseModel):
    task_id: int


class UpdateTask(BaseModel):
    id: int = Field(description="Task id to update.")
    title: str | None = Field(default=None, description="Updated task title.")
    body: str | None = Field(
        default=None,
        description="Replaces the task description. Use task_comment_add for progress updates.",
    )
    status: Status | None = Field(
        default=None,
        description="Transition: `doing` when active, `done` when finished, `blocked` if stuck.",
    )
    priority: Priority | None = Field(default=None, description="Updated priority ranking.")
    tags: list[str] | None = Field(default=None, description="Replaces the whole tag list.")
    expected_assigned: NullableAssignment = Field(
        default=None,
        description="Compare assignment atomically before writing. Required for an edit claim.",
    )
    release_edit: bool = Field(
        default=False,
        description="Restore the previous assignment atomically after saving an edit claim.",
    )


class MoveTask(BaseModel):
    id: int
    project: str = Field(
        description="Destination project: owner/slug, unique short slug, #id, or explicit path."
    )


class SaveNote(BaseModel):
    model_config = {"title": "SaveMemory"}

    create_only: bool = Field(
        default=False,
        description="Create a new memory entry; fail if its title already exists. No id allowed.",
    )
    title: str | None = Field(
        default=None,
        description="Title for one lasting fact, preference, or convention. Optional with `id`.",
    )
    body: str | None = Field(
        default=None,
        max_length=NOTE_LIMIT,
        description=(
            "Lasting user context or constraint unavailable elsewhere; no code snapshots. "
            "Replaces the whole body. "
            f"At most {NOTE_LIMIT} characters."
        ),
    )
    tags: list[str] | None = Field(default=None, description="Replaces the whole tag list.")
    shared: bool | None = Field(
        default=None, description="Make memory entry visible across all projects."
    )
    id: int | None = Field(
        default=None, description="Change this memory entry, whatever its title."
    )
    project: str | None = Field(
        default=None,
        description=(
            "Defaults to connection project scope (?project= in HTTP, --project on "
            "stdio); required if unscoped."
        ),
    )


class AppendNote(BaseModel):
    model_config = {"title": "AppendMemory"}

    text: str = Field(
        max_length=NOTE_LIMIT,
        description=(
            "One related fact, preference, or constraint from the user. "
            f"The whole memory entry must stay within {NOTE_LIMIT} characters."
        ),
    )
    id: int | None = Field(default=None, description="The memory entry to extend.")
    title: str | None = Field(default=None, description="Its title instead, in `project`.")
    project: str | None = Field(
        default=None,
        description=(
            "Defaults to connection project scope (?project= in HTTP, --project on "
            "stdio); required if unscoped."
        ),
    )


class NoteId(BaseModel):
    model_config = {"title": "MemoryId"}

    id: int


class ReadNote(BaseModel):
    model_config = {"title": "ReadMemory"}

    id: int | None = Field(
        default=None,
        description="Memory entry id.",
    )
    title: str | None = Field(default=None, description="Its title instead, in `project`.")
    project: str | None = Field(
        default=None,
        description=(
            "Defaults to connection project scope (?project= in HTTP, --project on "
            "stdio); required if unscoped."
        ),
    )


class ListNotes(BaseModel):
    model_config = {"title": "ListMemory"}

    project: str | None = Field(
        default=None,
        description=(
            "Defaults to connection project scope (?project= in HTTP, --project on "
            "stdio); required if unscoped."
        ),
    )
    all_projects: bool = Field(
        default=False, description="List memory entries from every project of this computer."
    )
    computer: str | None = Field(
        default=None, description="Every project of the named computer instead."
    )
    all_computers: bool = Field(default=False, description="Every project of every computer.")
    tag: str | None = None
    shared: bool | None = Field(
        default=None, description="Only shared memory entries, or only local."
    )
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)
    full: bool = Field(
        default=False,
        description=(
            "Include complete memory entry bodies. Defaults to compact memory entry "
            "metadata and snippets."
        ),
    )


class StatsOf(BaseModel):
    project: str | None = Field(
        default=None,
        description=(
            "Defaults to connection project scope (?project= in HTTP, --project on "
            "stdio); required if unscoped."
        ),
    )
    all_projects: bool = Field(
        default=False, description="Count every project of this computer together."
    )
    computer: str | None = Field(
        default=None, description="Every project of the named computer instead."
    )
    all_computers: bool = Field(default=False, description="Every project of every computer.")


@registry.tool(title="Current identity", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def whoami(args: Nothing, backlog: Backlog, connection: aiosqlite.Connection) -> Identity:
    """Report principal as user@computer, authentication type and owner handle without selecting a
    project."""
    return Identity(
        principal=backlog.principal,
        handle=owner_name(backlog.principal),
        auth_type=backlog.auth_type,
        computer=backlog.computer,
        assigned_tasks=await count_assigned(
            connection,
            backlog.actor,
            principal_label(backlog.principal, backlog.computer) + ":",
        ),
    )


class ReadBoard(BaseModel):
    project: str = Field(
        min_length=1,
        description="Existing project: owner/slug, unique slug, #id or registered path.",
    )


@registry.tool(title="Project board", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def project_board(
    args: ReadBoard, backlog: Backlog, connection: aiosqlite.Connection
) -> ProjectBoard:
    """Start here for project context, counts and ready/claimed tasks.
    Read memory_list(full=true) before work. Verify principal and roots, and follow
    preferred_language. Read task_get before claiming; the board is not a reservation.
    This read never creates a project or changes connection scope. See guide('workflow')."""
    project = await backlog.lookup(args.project)
    shown = project_out(project, computer=backlog.computer, principal=backlog.principal)
    counts, ready, claimed = await board(connection, backlog.actor, project.id)
    return ProjectBoard(
        principal=backlog.principal,
        computer=backlog.computer,
        project=BoardProject(**shown.model_dump()),
        tasks=BoardCounts(**counts),
        memory=await count_notes(connection, backlog.actor, project_id=project.id),
        ready=ready,
        claimed=claimed,
    )


@registry.tool(title="List projects", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def project_list(
    args: ListProjects, backlog: Backlog, connection: aiosqlite.Connection
) -> Projects:
    """List registered projects with open and total task counts.

    `computer` lists another computer's projects, `all_computers` every project there is."""
    reach = await backlog.reach(
        all_projects=True, computer=args.computer, all_computers=args.all_computers
    )
    found = await read_projects(
        connection, backlog.actor, archived=args.archived, computer=reach.computer
    )
    return Projects(
        principal=backlog.principal,
        computer=backlog.computer,
        items=[
            project_out(item, computer=backlog.computer, principal=backlog.principal)
            for item in found
        ],
        count=len(found),
    )


@registry.tool(title="Register or update a project", annotations=Hint.IDEMPOTENT)
async def project_save(
    args: SaveProject, backlog: Backlog, connection: aiosqlite.Connection, transaction: Transaction
) -> ProjectOut:
    """Register a project or update its name, description, language, or archive status.

    `preferred_language` sets the language for project work and task text. Omit it to keep
    the current value, or send null to clear it. New projects default to null.
    """
    project = await backlog.save(
        connection,
        transaction,
        reference=args.project,
        root=args.root,
        name=args.name,
        description=args.description,
        archived=args.archived,
        preferred_language=(
            args.preferred_language
            if "preferred_language" in args.model_fields_set
            else Unset.VALUE
        ),
    )
    await changed(project_event(project, Action.UPDATED, computer=backlog.computer))
    return project_out(project, computer=backlog.computer, principal=backlog.principal)


@registry.tool(title="Delete a project", annotations=Hint.DESTRUCTIVE | ~Hint.OPEN_WORLD)
async def project_delete(
    args: DeleteProject,
    ex: Exchange,
    backlog: Backlog,
    connection: aiosqlite.Connection,
    transaction: Transaction,
) -> ProjectDeleted:
    """Delete a project with every task and memory entry on its board, and free its directories.

    DESTRUCTIVE and irreversible. Call it only when the user asked to delete this project in so
    many words. Never call it on your own initiative, and never to tidy up: archive with
    `project_save(archived=true)`, which keeps the history. The tool always asks the user to
    confirm. A client that cannot ask is refused; the terminal command `backlog projects delete`
    asks there instead."""
    project = await backlog.lookup(args.project)
    await require(connection, backlog.actor, project.id, role="owner")
    contents = await project_contents(connection, backlog.actor, project.id)
    found = ProjectDeleted(
        principal=backlog.principal,
        computer=backlog.computer,
        id=project.id,
        slug=project.slug,
        full_name=project.full_name,
        name=project.name,
        tasks=contents.tasks,
        memory=contents.notes,
        shared_notes=contents.shared_notes,
        deleted=False,
    )
    held = (
        f"{project.full_name!r} with {contents.tasks} task(s) and {contents.notes} note(s), "
        f"{contents.shared_notes} of them shared"
    )
    key = f"delete-project:{project.id}"
    answer = ex.answers.get(key)
    if answer is None:
        if not ex.can_ask:
            raise PermissionError(
                f"this client cannot ask the user, so it cannot delete project {held}; "
                "use a client with elicitation, or `backlog projects delete`"
            )
        raise NeedInput(
            {
                key: elicit(
                    f"Delete project {held}? This cannot be undone.",
                    {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                )
            }
        )
    if not answer.get("ok"):
        found.detail = "cancelled"
        return found
    recipients = [
        member["principal"] for member in await members(connection, backlog.actor, project.id)
    ]
    neighbors = await linked_tasks(backlog, project_id=project.id)
    await backlog.delete(project, connection, transaction)
    await invalidate_linked_tasks(
        [task for task in neighbors if task.project_id != project.id], backlog
    )
    await changed(
        project_event(project, Action.DELETED, computer=backlog.computer), recipients=recipients
    )
    found.deleted = True
    return found


@registry.tool(title="Add a task")
async def task_add(
    args: AddTask, backlog: Backlog, connection: aiosqlite.Connection, transaction: Transaction
) -> TaskOut:
    """Create a task for work that spans more than one step or session. Search for an existing task
    first. See guide('tasks')."""
    project = await backlog.resolve(args.project)
    async with transaction:
        task = await add_task(
            connection,
            backlog.actor,
            project_id=project.id,
            title=args.title,
            body=args.body,
            status=args.status,
            priority=args.priority,
            tags=args.tags,
        )
    embeddings.enqueue(backlog, task)
    await changed(task_event(task, Action.ADDED, computer=backlog.computer))
    return task_out(task)


@registry.tool(title="List tasks", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def task_list(
    args: ListTasks, backlog: Backlog, connection: aiosqlite.Connection
) -> Tasks | FullTasks:
    """List tasks with filters. To pick the next task, use project_board. Defaults to open tasks
    ordered by priority. See guide('tasks')."""
    reach = await backlog.reach(
        args.project,
        all_projects=args.all_projects,
        computer=args.computer,
        all_computers=args.all_computers,
    )
    found = await read_tasks(
        connection,
        backlog.actor,
        project_id=reach.project_id,
        computer=reach.computer,
        status=statuses(args.status),
        tag=args.tag,
        priority=args.priority,
        order=args.order,
        limit=args.limit,
        offset=args.offset,
    )
    total = await count_tasks(
        connection,
        backlog.actor,
        project_id=reach.project_id,
        computer=reach.computer,
        status=statuses(args.status),
        tag=args.tag,
        priority=args.priority,
    )
    if args.full:
        return FullTasks(
            items=[task_out(item) for item in found],
            count=total,
            project=reach.slug,
            computer=backlog.computer,
            principal=backlog.principal,
        )
    return Tasks(
        items=[task_list_item(item) for item in found],
        count=total,
        project=reach.slug,
        computer=backlog.computer,
        principal=backlog.principal,
    )


@registry.tool(title="Read a task", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def task_get(args: TaskId, backlog: Backlog, connection: aiosqlite.Connection) -> TaskDetails:
    """Read a task with its direct links, comments and history.

    The body is only the description. Comments are chronological. Links use relation types
    relative to this task and include current target titles and statuses, without nested links.
    Links to tasks you cannot access are omitted.
    """
    task = await read_task(connection, backlog.actor, args.id)
    if task is None:
        raise LookupError(f"no task #{args.id}")
    return await task_details(task, backlog)


@registry.tool(title="List assigned tasks", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def task_assigned(
    args: AssignedTasks, backlog: Backlog, connection: aiosqlite.Connection
) -> Tasks:
    """Find assignments by literal prefix across all accessible projects and statuses.

    Null or omitted prefix selects the current principal. Use username@computer: for a
    principal or username@computer:marker for an agent marker prefix. Percent and underscore
    are literal characters, not wildcards. Matching is case-sensitive. Closed tasks remain
    assigned until explicitly released. Count includes all matches before pagination.
    """
    prefix = args.prefix
    if prefix is None:
        prefix = principal_label(backlog.principal, backlog.computer) + ":"
    tasks = await assigned_tasks(
        connection, backlog.actor, prefix, limit=args.limit, offset=args.offset
    )
    return Tasks(
        principal=backlog.principal,
        computer=backlog.computer,
        items=[task_list_item(task) for task in tasks],
        count=await count_assigned(connection, backlog.actor, prefix),
    )


@registry.tool(title="Claim a task")
async def task_claim(
    args: ClaimTask, backlog: Backlog, connection: aiosqlite.Connection, transaction: Transaction
) -> TaskOut:
    """Atomically replace assigned only when it equals expected (CAS).
    Both nullable arguments are required; null expected requires an unassigned task.
    A mismatch is an error even if assigned already has the requested value.

    Assignment is advisory: it keeps two agents from starting the same task.
    Any editor may take a task over through CAS. Non-null assigned must use this principal
    and a URL-safe marker. Keep the marker across reconnects. On conflict, reread before
    retrying.
    Closing does not release assignment; release with assigned=null. See guide('tasks').

    temporary=true saves the previous assignment for exclusive UI editing. restore=true
    with assigned=null restores it; only that claimant may restore. Writes require the edit
    marker as expected_assigned. Ordinary CAS reassignment invalidates a temporary claim."""
    async with transaction:
        task, updated = await claim_task(
            connection,
            backlog.actor,
            args.task,
            expected=args.expected,
            assigned=args.assigned,
            temporary=args.temporary,
            restore=args.restore,
        )
        comments = (
            await read_comments(
                connection, backlog.actor, task_id=args.task, status=list(Status), limit=-1
            )
            if updated
            else []
        )
    if updated:
        embeddings.enqueue(backlog, *comments)
        await changed(task_event(task, Action.UPDATED, computer=backlog.computer))
    return task_out(task)


@registry.tool(title="Link tasks", annotations=Hint.IDEMPOTENT)
async def task_link(
    args: LinkTask, backlog: Backlog, connection: aiosqlite.Connection, transaction: Transaction
) -> LinkChange:
    """Add a typed relation and its inverse atomically, or update their reason.

    Read links through task_get. Requires editor access to both tasks. Self-links and cycles
    in part_of or depends_on are rejected. Repeated calls are safe, including from the inverse
    direction. Links do not change task statuses. Closing or soft-deleting tasks keeps links.
    """
    async with transaction:
        updated = await link_task(
            connection, backlog.actor, args.task, args.relation, args.target, reason=args.reason
        )
    if updated:
        for task_id in (args.task, args.target):
            task = await read_task(connection, backlog.actor, task_id)
            assert task is not None
            await changed(task_event(task, Action.UPDATED, computer=backlog.computer))
    return LinkChange(task=args.task, relation=args.relation, target=args.target, changed=updated)


@registry.tool(title="Unlink tasks", annotations=Hint.IDEMPOTENT)
async def task_unlink(
    args: UnlinkTask, backlog: Backlog, connection: aiosqlite.Connection, transaction: Transaction
) -> LinkChange:
    """Remove a relation and its inverse atomically, from either direction.

    Requires editor access to both tasks. A missing link is a safe no-op. Other relation types
    between the same tasks are preserved.
    """
    async with transaction:
        updated = await unlink_task(
            connection, backlog.actor, args.task, args.relation, args.target
        )
    if updated:
        for task_id in (args.task, args.target):
            task = await read_task(connection, backlog.actor, task_id)
            assert task is not None
            await changed(task_event(task, Action.UPDATED, computer=backlog.computer))
    return LinkChange(task=args.task, relation=args.relation, target=args.target, changed=updated)


@registry.tool(title="Add a task comment", annotations=Hint.OPEN_WORLD)
async def task_comment_add(
    args: AddComment, backlog: Backlog, connection: aiosqlite.Connection, transaction: Transaction
) -> CommentOut:
    """Record progress, findings, blockers, or verification as a separate Markdown comment.

    Use this for routine updates. Follow the project's preferred_language. Task descriptions
    change only through task_update. Repeated calls create separate comments.
    """
    async with transaction:
        comment = await add_comment(connection, backlog.actor, args.task_id, args.body)
    embeddings.enqueue(backlog, comment)
    task = await read_task(connection, backlog.actor, args.task_id)
    assert task is not None
    await changed(task_event(task, Action.UPDATED, computer=backlog.computer))
    return comment_out(comment)


@registry.tool(title="List task comments", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def task_comment_list(
    args: ListComments, backlog: Backlog, connection: aiosqlite.Connection
) -> Comments:
    """Read comments and history in creation-time order, with id as a stable tie-breaker."""
    task = await read_task(connection, backlog.actor, args.task_id)
    if task is None:
        raise LookupError(f"no task #{args.task_id}")
    details = await task_details(task, backlog)
    return Comments(
        principal=backlog.principal,
        computer=backlog.computer,
        items=details.comments,
        task_id=task.id,
        count=len(details.comments),
    )


@registry.tool(title="Update a task", annotations=Hint.IDEMPOTENT)
async def task_update(
    args: UpdateTask, backlog: Backlog, connection: aiosqlite.Connection, transaction: Transaction
) -> TaskOut:
    """Edit task fields. A supplied body replaces the description; an empty string clears it.

    Use task_comment_add for progress, findings, and verification. Status transitions create
    separate history entries. Mark active work doing and completed work done or cancelled.
    Closing stamps the closing time; reopening clears it. expected_assigned, when supplied,
    is compared under the writer lock. An active temporary edit claim requires that marker.
    release_edit=true restores its previous assignment in the same transaction.
    Waiting for another task is a depends_on link, not blocked status. Use blocked for external
    blockers and explain in a comment. Before closing, update project memory this task made
    obsolete. After closing, release through task_claim(assigned=null). See guide('tasks')."""
    async with transaction:
        task = await update_task(
            connection,
            backlog.actor,
            args.id,
            title=args.title,
            body=args.body,
            status=args.status,
            priority=args.priority,
            tags=args.tags,
            expected_assigned=args.expected_assigned
            if "expected_assigned" in args.model_fields_set
            else Unset.VALUE,
        )
        if args.release_edit:
            if "expected_assigned" not in args.model_fields_set:
                raise ValueError("release_edit requires expected_assigned")
            task, _ = await claim_task(
                connection,
                backlog.actor,
                args.id,
                expected=args.expected_assigned,
                assigned=None,
                restore=True,
            )
        comments = await read_comments(
            connection, backlog.actor, task_id=args.id, status=list(Status), limit=-1
        )
    if task is None:
        raise LookupError(f"no task #{args.id}")
    embeddings.enqueue(backlog, task, *comments)
    await changed(task_event(task, Action.UPDATED, computer=backlog.computer))
    if args.title is not None or args.status is not None:
        await invalidate_linked_tasks(await linked_tasks(backlog, task_id=task.id), backlog)
    return task_out(task)


@registry.tool(title="Move a task", annotations=Hint.IDEMPOTENT)
async def task_move(
    args: MoveTask, backlog: Backlog, connection: aiosqlite.Connection, transaction: Transaction
) -> TaskOut:
    """Move a task to another project board and record the move in its history."""
    project = await backlog.lookup(args.project)
    existing = await read_task(connection, backlog.actor, args.id)
    if existing is None:
        raise LookupError(f"no task #{args.id}")
    if existing.project_id == project.id:
        return task_out(existing)
    async with transaction:
        task = await update_task(connection, backlog.actor, args.id, project_id=project.id)
        comments = await read_comments(
            connection, backlog.actor, task_id=args.id, status=list(Status), limit=-1
        )
    assert task is not None
    embeddings.enqueue(backlog, task, *comments)
    await changed(task_event(task, Action.MOVED, computer=backlog.computer))
    await invalidate_linked_tasks(await linked_tasks(backlog, task_id=task.id), backlog)
    return task_out(task)


async def confirm_cross_project_delete(
    record: Note, ex: Exchange, backlog: Backlog
) -> Deleted | None:
    """Ask when the memory entry belongs to another project, or when no project is chosen.

    A client that cannot ask is refused with `PermissionError`: nothing stands in for the
    user's answer.
    """
    current, _ = await backlog.scope()
    if current is not None and record.project_id == current.id:
        return None
    key = f"delete:{record.project_id}:{record.id}"
    if (answer := ex.answers.get(key)) is not None:
        if answer.get("ok"):
            return None
        return Deleted(
            principal=backlog.principal,
            computer=backlog.computer,
            id=record.id,
            title=record.title,
            deleted=False,
            detail="cancelled",
        )
    where = record.project
    if not ex.can_ask:
        raise PermissionError(
            f"this client cannot ask the user, so it cannot delete note #{record.id} from "
            f"{where!r}; use a client with elicitation, or `backlog memory delete`"
        )
    chosen = (
        f"The current project is {current.full_name!r}." if current else "No project is chosen."
    )
    raise NeedInput(
        {
            key: elicit(
                f"Delete note #{record.id} ({record.title!r}) from {where!r}? "
                f"{chosen} This cannot be undone.",
                {"type": "object", "properties": {"ok": {"type": "boolean"}}},
            )
        }
    )


@registry.tool(title="Delete a task", annotations=Hint.IDEMPOTENT)
async def task_delete(
    args: TaskId, backlog: Backlog, connection: aiosqlite.Connection, transaction: Transaction
) -> Deleted:
    """Delete a task. Marks its status as `deleted` without removing the record.

    Keeps history intact and allows recovery. Works across projects without confirmation."""
    task = await read_task(connection, backlog.actor, args.id)
    if task is None:
        raise LookupError(f"no task #{args.id}")
    if task.status != Status.DELETED:
        async with transaction:
            gone = await update_task(connection, backlog.actor, task.id, status=Status.DELETED)
            comments = await read_comments(
                connection, backlog.actor, task_id=task.id, status=list(Status), limit=-1
            )
        assert gone is not None
        embeddings.enqueue(backlog, gone, *comments)
        await changed(task_event(gone, Action.DELETED, computer=backlog.computer))
        await invalidate_linked_tasks(await linked_tasks(backlog, task_id=task.id), backlog)
    return Deleted(
        principal=backlog.principal,
        computer=backlog.computer,
        id=args.id,
        title=task.title,
        deleted=True,
    )


@registry.tool(title="Save a memory entry", annotations=Hint.IDEMPOTENT)
async def memory_save(
    args: SaveNote, backlog: Backlog, connection: aiosqlite.Connection, transaction: Transaction
) -> NoteOut:
    """Record a lasting agreement, preference or invariant the user stated.
    Read project memory first and update an existing entry by id instead of creating a
    near-duplicate. A correction rewrites the body with the current truth; do not append
    history.
    Do not store task status, progress or code snapshots; use tasks and comments.
    Body replaces the whole body, at most 1024 characters. See guide('memory')."""
    if args.create_only and args.id is not None:
        raise ValueError("create_only cannot be used with id")
    if args.id is None:
        if not args.title:
            raise ValueError("a new note needs a title")
        project = await backlog.resolve(args.project)
        existing = await note_by_title(connection, backlog.actor, args.title, project_id=project.id)
        if existing is None:
            async with transaction:
                saved = await add_note(
                    connection,
                    backlog.actor,
                    project_id=project.id,
                    title=args.title,
                    body=args.body or "",
                    tags=args.tags or (),
                    shared=bool(args.shared),
                )
            embeddings.enqueue(backlog, saved)
            await changed(note_event(saved, Action.ADDED, computer=backlog.computer))
            return note_out(saved)
        if args.create_only:
            raise ValueError("a note with this title already exists; choose another title")
        args.id = existing.id
    async with transaction:
        updated = await update_note(
            connection,
            backlog.actor,
            args.id,
            title=args.title,
            body=args.body,
            tags=args.tags,
            shared=args.shared,
        )
    if updated is None:
        raise LookupError(f"no note #{args.id}")
    embeddings.enqueue(backlog, updated)
    await changed(note_event(updated, Action.UPDATED, computer=backlog.computer))
    return note_out(updated)


@registry.tool(title="Append to a memory entry")
async def memory_append(
    args: AppendNote, backlog: Backlog, connection: aiosqlite.Connection, transaction: Transaction
) -> NoteOut:
    """Add a new related fact to an entry. To correct or update existing content, use memory_save.
    See guide('memory')."""
    found = await note_of(backlog, args.id, args.title, args.project)
    async with transaction:
        updated = await append_note(connection, backlog.actor, found.id, args.text)
    if updated is None:
        raise LookupError(f"no note #{found.id}")
    embeddings.enqueue(backlog, updated)
    await changed(note_event(updated, Action.UPDATED, computer=backlog.computer))
    return note_out(updated)


@registry.tool(title="Read a memory entry", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def memory_get(args: ReadNote, backlog: Backlog) -> NoteOut:
    """Read one memory entry by id or title. IDs work across accessible projects; titles resolve
    within project scope and shared memory. See guide('memory')."""
    return note_out(await note_of(backlog, args.id, args.title, args.project))


@registry.tool(title="List memory entries", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def memory_list(
    args: ListNotes, backlog: Backlog, connection: aiosqlite.Connection
) -> Notes | FullNotes:
    """List project memory, including accessible shared entries. Read it with full=true before
    working on a project; project memory is kept small on purpose. See guide('memory')."""
    reach = await backlog.reach(
        args.project,
        all_projects=args.all_projects,
        computer=args.computer,
        all_computers=args.all_computers,
    )
    found = await read_notes(
        connection,
        backlog.actor,
        project_id=reach.project_id,
        computer=reach.computer,
        tag=args.tag,
        shared=args.shared,
        limit=args.limit,
        offset=args.offset,
    )
    total = await count_notes(
        connection,
        backlog.actor,
        project_id=reach.project_id,
        computer=reach.computer,
        tag=args.tag,
        shared=args.shared,
    )
    if args.full:
        return FullNotes(
            items=[note_out(item) for item in found],
            count=total,
            project=reach.slug,
            computer=backlog.computer,
            principal=backlog.principal,
        )
    return Notes(
        items=[note_list_item(item) for item in found],
        count=total,
        project=reach.slug,
        computer=backlog.computer,
        principal=backlog.principal,
    )


@registry.tool(title="Delete a memory entry", annotations=Hint.DESTRUCTIVE | ~Hint.OPEN_WORLD)
async def memory_delete(
    args: NoteId,
    ex: Exchange,
    backlog: Backlog,
    connection: aiosqlite.Connection,
    transaction: Transaction,
) -> Deleted:
    """Delete a memory entry permanently, only at the user's request. Entries within the
    connection's project scope are deleted immediately. Other entries require user confirmation;
    clients without elicitation are refused."""
    found = await read_note(connection, backlog.actor, args.id)
    if found is None:
        raise LookupError(f"no note #{args.id}")
    if refusal := await confirm_cross_project_delete(found, ex, backlog):
        return refusal
    async with transaction:
        deleted = await delete_note(connection, backlog.actor, args.id)
    embeddings.enqueue(backlog, found, deleted=True)
    await changed(note_event(found, Action.DELETED, computer=backlog.computer))
    return Deleted(
        principal=backlog.principal,
        computer=backlog.computer,
        id=args.id,
        title=found.title,
        deleted=deleted,
    )


@registry.tool(title="Search the backlog", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def search(
    args: embeddings.Search,
    backlog: Backlog,
    connection: aiosqlite.Connection,
    transaction: Transaction,
) -> embeddings.Matches:
    """Find tasks, comments, memory and projects by text or meaning.
    Use it to look up past work or agreements on a topic; for project context, use
    project_board.
    Quoted expressions use full-text search, unquoted text uses vectors, mixed queries use both.
    Examples: `"refresh token"` matches a phrase; `"token"*` matches a prefix;
    `token reset` searches meaning. See guide('search') for full syntax and filters."""
    return await embeddings.search(args, backlog, connection, transaction)


@registry.tool(title="Backlog statistics", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def stats(args: StatsOf, backlog: Backlog) -> Stats:
    """Aggregate counts across projects or computers. For one project, use project_board."""
    reach = await backlog.reach(
        args.project,
        all_projects=args.all_projects,
        computer=args.computer,
        all_computers=args.all_computers,
    )
    return await stats_of(backlog, reach)


class GuideTopic(BaseModel):
    topic: str | None = Field(default=None, description="Topic name; omit for the index.")


@registry.tool(title="Backlog guide", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def guide(args: GuideTopic) -> str:
    """Read workflow rules or help on tasks, memory, search and projects."""
    return guidance.read(args.topic)


def register_guide(topic: str | None) -> None:
    uri = "backlog://guide" + (f"/{topic}" if topic else "")

    @registry.resource(uri, name=f"guide_{topic or 'index'}", mime_type="text/markdown")
    async def content(args: Nothing) -> TextResourceContents:
        return TextResourceContents(uri=uri, mime_type="text/markdown", text=guidance.read(topic))


for guide_topic in (None, *guidance.TOPICS):
    register_guide(guide_topic)


@registry.resource("backlog://project", mime_type="application/json")
async def current_project(args: Nothing, backlog: Backlog) -> dict[str, Any]:
    """The project the tools work on, or `null` with origin `none` when there is none."""
    project, origin = await backlog.scope()
    shown = (
        project_out(project, computer=backlog.computer, principal=backlog.principal).model_dump(
            mode="json"
        )
        if project
        else None
    )
    return {"project": shown, "origin": origin.value, "computer": backlog.computer}


@registry.resource("backlog://projects", mime_type="application/json")
async def all_projects(
    args: Nothing, backlog: Backlog, connection: aiosqlite.Connection
) -> list[dict[str, Any]]:
    """Every registered project of this computer."""
    found = await read_projects(connection, backlog.actor, computer=backlog.computer)
    return [
        project_out(item, computer=backlog.computer, principal=backlog.principal).model_dump(
            mode="json"
        )
        for item in found
    ]


@registry.resource("backlog://tasks", mime_type="application/json")
async def open_tasks(
    args: Nothing, backlog: Backlog, connection: aiosqlite.Connection
) -> list[dict[str, Any]]:
    """Open tasks of the current project, most important first."""
    project = await backlog.resolve(None)
    found = await read_tasks(
        connection, backlog.actor, project_id=project.id, status=list(OPEN_STATUSES), limit=200
    )
    return [task_out(item).model_dump(mode="json") for item in found]


class TaskUri(BaseModel):
    id: str


@registry.resource("backlog://tasks/{id}", name="task", mime_type="application/json")
async def one_task(
    args: TaskUri, backlog: Backlog, connection: aiosqlite.Connection
) -> dict[str, Any]:
    """One task by id."""
    if not args.id.isdigit():
        return {"error": f"invalid task id {args.id!r}"}
    task = await read_task(connection, backlog.actor, int(args.id))
    if task is None:
        return {"error": f"no task #{args.id}"}
    return (await task_details(task, backlog)).model_dump(mode="json")


@registry.resource("backlog://memory", mime_type="application/json")
async def known_notes(
    args: Nothing, backlog: Backlog, connection: aiosqlite.Connection
) -> list[dict[str, Any]]:
    """Memory entries in connection project scope, including accessible shared entries."""
    project = await backlog.resolve(None)
    found = await read_notes(connection, backlog.actor, project_id=project.id, limit=200)
    return [note_out(item).model_dump(mode="json") for item in found]


@registry.resource("backlog://memory/{id}", name="note", mime_type="application/json")
async def one_note(
    args: TaskUri, backlog: Backlog, connection: aiosqlite.Connection
) -> dict[str, Any]:
    """One memory entry by id."""
    if not args.id.isdigit():
        return {"error": f"invalid note id {args.id!r}"}
    found = await read_note(connection, backlog.actor, int(args.id))
    if found is None:
        return {"error": f"no note #{args.id}"}
    return note_out(found).model_dump(mode="json")


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


@registry.prompt(title="Install Backlog rules into the global Agents config")
async def install_backlog_rules(args: Nothing) -> str:
    """Install the Backlog workflow in global AGENTS.md and CLAUDE.md files."""
    rules = files("backlog_mcp").joinpath("global_rules.md").read_text(encoding="utf-8")
    return (
        "Install the following Backlog rules in the user's global agent configuration.\n\n"
        "Work on the user's local computer, not on the MCP server. For Codex, use "
        "AGENTS.md under CODEX_HOME when configured, otherwise ~/.codex/AGENTS.md. "
        "For Claude Code, use ~/.claude/CLAUDE.md. Respect an explicitly configured "
        "alternative global location. Do not put these rules in repository-local files.\n\n"
        "Read both existing files first. Create missing parent directories and files. "
        "Replace the complete section between <!-- BACKLOG_START --> and "
        "<!-- BACKLOG_END -->, including the markers, with the block below. "
        "If no marked section exists, append the block with a blank line before it. "
        "Preserve unrelated instructions. If markers are incomplete or duplicated, "
        "resolve the ambiguity before replacing any content. Do not duplicate the rules "
        "when this prompt is run again.\n\n"
        "Verify that both files contain exactly one identical Backlog block and that "
        "unrelated content is unchanged. Report the actual paths and any conflicting "
        "existing instructions. If local file access is unavailable, return the block "
        "and installation instructions without claiming that files were changed.\n\n"
        "Install this exact block:\n\n" + rules
    )


class Standup(BaseModel):
    project: str | None = None
    all_projects: bool = False
    computer: str | None = None
    all_computers: bool = False


@registry.prompt
async def standup(args: Standup, backlog: Backlog, connection: aiosqlite.Connection) -> str:
    """Draft a standup update from what closed recently and what is still open."""
    reach = await backlog.reach(
        args.project,
        all_projects=args.all_projects,
        computer=args.computer,
        all_computers=args.all_computers,
    )
    done = await read_tasks(
        connection,
        backlog.actor,
        project_id=reach.project_id,
        computer=reach.computer,
        status=[Status.DONE],
        order="recent",
        limit=10,
    )
    open_ = await read_tasks(
        connection,
        backlog.actor,
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
async def groom(args: Groom, backlog: Backlog, connection: aiosqlite.Connection) -> str:
    """Ask for a review of the open backlog: order, duplicates and missing detail."""
    project = await backlog.resolve(args.project)
    found = await read_tasks(
        connection,
        backlog.actor,
        project_id=project.id,
        status=list(OPEN_STATUSES),
        limit=args.limit,
    )
    lines = [
        f"- #{item.id} [{item.priority.value}/{item.status.value}] {item.title}"
        + (f" (tags: {', '.join(item.tags)})" if item.tags else "")
        for item in found
    ]
    return (
        f"Open backlog of {project.full_name}:\n\n"
        + ("\n".join(lines) or "- nothing open")
        + "\n\nReview it: what should be done first, what duplicates what, "
        "and which items are too vague to start. Suggest concrete edits."
    )


@registry.completions
async def complete(
    args: CompleteParams, backlog: Backlog, connection: aiosqlite.Connection
) -> list[str]:
    """Complete project names, tags, statuses, priorities, task ids and memory entry ids."""
    value = args.argument.value
    #: The task and the note templates both call their argument `id`; the URI tells them apart.
    about_notes = "memory" in (args.ref.uri or "")
    match args.argument.name, args.ref.type:
        case "project", _:
            return [
                name
                for name in await slugs(connection, backlog.actor)
                if name.startswith(value) or name.split("/", 1)[1].startswith(value)
            ]
        case "computer", _:
            return [
                name
                for name in await computers(connection, backlog.actor)
                if name.startswith(value)
            ]
        case "tag", _:
            project, _origin = await backlog.current()
            tags = await read_tags(connection, backlog.actor, project_id=project.id)
            return [tag for tag in tags if tag.startswith(value)]
        case "status", _:
            return [item.value for item in Status if item.value.startswith(value)]
        case "priority", _:
            return [item.value for item in Priority if item.value.startswith(value)]
        case "id", "ref/resource" if about_notes:
            project, _origin = await backlog.current()
            notes = await read_notes(connection, backlog.actor, project_id=project.id, limit=200)
            return [str(item.id) for item in notes if str(item.id).startswith(value)]
        case "id", "ref/resource":
            project, _origin = await backlog.current()
            found = await read_tasks(connection, backlog.actor, project_id=project.id, limit=200)
            return [str(item.id) for item in found if str(item.id).startswith(value)]
        case _:
            return []


__all__ = ["INSTRUCTIONS", "VERSION", "UnknownProject", "bound", "registry", "use"]


async def search_projects(
    connection: aiosqlite.Connection,
    actor: Actor,
    query: str,
    *,
    project_id: int | None = None,
    computer: str | None = None,
    raw: bool = False,
    limit: int = 20,
) -> list[Project]:
    match = query if raw else match_query(query)
    if not match:
        return []
    rows = QUERIES.search_projects(
        connection,
        match=match,
        project_id=project_id,
        computer=computer,
        principal=actor.principal,
        limit=limit,
    )
    return await attach_roots(connection, actor, [to_project(row) async for row in rows])


async def board(
    connection: aiosqlite.Connection, actor: Actor, project_id: int
) -> tuple[dict[str, int], BoardSection, BoardSection]:
    await require(connection, actor, project_id)
    row = await QUERIES.board_counts(connection, project_id=project_id)
    assert row is not None
    counts = dict(row)
    blocked = await QUERIES.board_blocked_count(
        connection, project_id=project_id, principal=actor.principal
    )
    assert blocked is not None
    counts["blocked"] = blocked[0]

    ready_count = await QUERIES.board_ready_count(
        connection, project_id=project_id, principal=actor.principal
    )
    assert ready_count is not None
    ready = BoardSection(
        count=ready_count[0],
        items=[
            BoardTask.model_validate(dict(row))
            async for row in QUERIES.board_ready_tasks(
                connection, project_id=project_id, principal=actor.principal
            )
        ],
    )

    prefix = principal_label(actor.principal, actor.computer) + ":"
    claimed_count = await QUERIES.board_claimed_count(
        connection, project_id=project_id, prefix=prefix
    )
    assert claimed_count is not None
    claimed = BoardSection(
        count=claimed_count[0],
        items=[
            BoardTask.model_validate(dict(row))
            async for row in QUERIES.board_claimed_tasks(
                connection, project_id=project_id, prefix=prefix
            )
        ],
    )
    return counts, ready, claimed


async def assigned_tasks(
    connection: aiosqlite.Connection, actor: Actor, prefix: str, *, limit: int = 50, offset: int = 0
) -> list[Task]:
    rows = QUERIES.assigned_tasks(
        connection,
        prefix=prefix,
        pattern=assignment_pattern(prefix),
        principal=actor.principal,
        limit=limit,
        offset=offset,
    )
    return [to_task(row) async for row in rows]


async def count_assigned(connection: aiosqlite.Connection, actor: Actor, prefix: str) -> int:
    row = await QUERIES.count_assigned(
        connection,
        prefix=prefix,
        pattern=assignment_pattern(prefix),
        principal=actor.principal,
    )
    assert row is not None
    return int(row[0])


async def claim_task(
    connection: aiosqlite.Connection,
    actor: Actor,
    task_id: int,
    *,
    expected: str | None,
    assigned: str | None,
    temporary: bool = False,
    restore: bool = False,
) -> tuple[Task, bool]:
    """Compare and replace an assignment under a database writer lock."""
    if temporary and restore:
        raise ValueError("temporary and restore are mutually exclusive")
    if temporary and (assigned is None or assigned == expected):
        raise ValueError("An edit claim requires a new assignment marker")
    if restore and (assigned is not None or expected is None):
        raise ValueError("Restore requires expected and assigned=null")
    if expected is not None:
        check_assignment(expected)
    if assigned is not None:
        check_assignment(assigned)
        principal = principal_label(actor.principal, actor.computer or hostname())
        if assigned.rsplit(":", 1)[0] != principal:
            raise PermissionError(f"assigned must belong to the current principal {principal!r}")

    task = await read_task(connection, actor, task_id)
    if task is None:
        raise LookupError(f"no task #{task_id}")
    await require(connection, actor, task.project_id, role="editor")
    if task.assigned != expected:
        raise AssignmentConflict(task_id, expected, task.assigned)
    lease = await QUERIES.edit_claim(connection, task_id=task_id)
    if restore:
        principal = principal_label(actor.principal, actor.computer or hostname())
        if expected is None or expected.rsplit(":", 1)[0] != principal:
            raise PermissionError("Only the edit claimant can restore its previous assignment")
        if lease is None or lease["assigned"] != expected:
            raise ValueError("No matching edit claim to restore")
        assigned = lease["previous"]
    elif temporary and lease is not None:
        raise ValueError(f"Task is being edited by {task.assigned}")
    if expected == assigned:
        return task, False
    if temporary:
        await QUERIES.save_edit_claim(
            connection, task_id=task_id, assigned=assigned, previous=expected
        )
    else:
        await QUERIES.remove_edit_claim(connection, task_id=task_id)
    now = time.time()
    await QUERIES.claim_task(
        connection,
        task_id=task_id,
        expected=expected,
        assigned=assigned,
        now=now,
        principal=actor.principal,
        computer=actor.computer or hostname(),
    )
    await QUERIES.add_comment(
        connection,
        task_id=task_id,
        body=f"Assignment changed: {expected or 'null'} → {assigned or 'null'}",
        created_at=now,
        principal=actor.principal,
        computer=actor.computer or hostname(),
    )
    saved = await read_task(connection, actor, task_id)
    assert saved is not None
    return saved, True


async def read_linked_tasks(
    connection: aiosqlite.Connection,
    actor: Actor,
    *,
    task_id: int | None = None,
    project_id: int | None = None,
) -> list[Task]:
    """Resource dependents for internal routing, never for a public response."""
    rows = QUERIES.linked_tasks(connection, task_id=task_id, project_id=project_id)
    return [to_task(row) async for row in rows]


async def notification_recipients(
    connection: aiosqlite.Connection, actor: Actor, project_id: int
) -> list[str]:
    return [
        row["principal"]
        async for row in QUERIES.notification_recipients(connection, project_id=project_id)
    ]


async def task_links(
    connection: aiosqlite.Connection, actor: Actor, task_id: int
) -> list[TaskLinkOut]:
    if await read_task(connection, actor, task_id) is None:
        raise LookupError(f"no task #{task_id}")
    rows = QUERIES.task_links(connection, task_id=task_id, principal=actor.principal)
    return [TaskLinkOut(**dict(row)) async for row in rows]


async def link_task(
    connection: aiosqlite.Connection,
    actor: Actor,
    task_id: int,
    kind: Relation,
    target_id: int,
    *,
    reason: str | None = None,
) -> bool:
    """Write both directions under one SQLite writer lock, including cycle checks."""
    return await _task_link(
        connection, actor, task_id, kind, target_id, reason=reason, remove=False
    )


async def unlink_task(
    connection: aiosqlite.Connection, actor: Actor, task_id: int, kind: Relation, target_id: int
) -> bool:
    return await _task_link(connection, actor, task_id, kind, target_id, reason=None, remove=True)


async def _task_link(
    connection: aiosqlite.Connection,
    actor: Actor,
    task_id: int,
    kind: Relation,
    target_id: int,
    *,
    reason: str | None,
    remove: bool,
) -> bool:
    if task_id == target_id:
        raise ValueError("a task cannot link to itself")
    forward, inverse = relation(kind)

    for record_id in (task_id, target_id):
        task = await read_task(connection, actor, record_id)
        if task is None:
            raise LookupError(f"no task #{record_id}")
        await require(connection, actor, task.project_id, role="editor")
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
            principal=actor.principal,
            computer=actor.computer or hostname(),
        )
    await QUERIES.touch_link_tasks(
        connection,
        task_id=task_id,
        target_id=target_id,
        now=time.time(),
        principal=actor.principal,
        computer=actor.computer or hostname(),
    )
    return True


async def read_task(connection: aiosqlite.Connection, actor: Actor, task_id: int) -> Task | None:
    row = await QUERIES.task(connection, task_id=task_id, principal=actor.principal)
    return to_task(row) if row else None


async def add_task(
    connection: aiosqlite.Connection,
    actor: Actor,
    *,
    project_id: int,
    title: str,
    body: str = "",
    status: Status = Status.TODO,
    priority: Priority = Priority.NORMAL,
    tags: Iterable[str] = (),
) -> Task:
    await require(connection, actor, project_id, role="editor")

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
        created_principal=actor.principal,
        created_computer=actor.computer or hostname(),
        updated_principal=actor.principal,
        updated_computer=actor.computer or hostname(),
    )
    created = await read_task(connection, actor, inserted_id or 0)
    assert created is not None, "the task just written is not there"
    return created


async def update_task(
    connection: aiosqlite.Connection,
    actor: Actor,
    task_id: int,
    *,
    title: str | None = None,
    body: str | None = None,
    status: Status | None = None,
    priority: Priority | None = None,
    tags: Iterable[str] | None = None,
    project_id: int | None = None,
    expected_assigned: str | None | Unset = Unset.VALUE,
) -> Task | None:

    existing = await read_task(connection, actor, task_id)
    if existing is None:
        return None
    await require(connection, actor, existing.project_id, role="editor")
    if expected_assigned is not Unset.VALUE and existing.assigned != expected_assigned:
        raise AssignmentConflict(task_id, expected_assigned, existing.assigned)
    lease = await QUERIES.edit_claim(connection, task_id=task_id)
    if lease is not None:
        principal = principal_label(actor.principal, actor.computer or hostname())
        if expected_assigned != lease["assigned"]:
            raise ValueError(
                f"Task is being edited by {lease['assigned']}; expected_assigned is required"
            )
        if lease["assigned"].rsplit(":", 1)[0] != principal:
            raise PermissionError("Only the edit claimant can save this task")
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
        await require(connection, actor, project_id, role="editor")
        destination = await read_project(connection, actor, project_id)
        if destination is None:
            raise ValueError(f"no project #{project_id}")
        changes["project_id"] = project_id
    if changes:
        changes["updated_principal"] = actor.principal
        changes["updated_computer"] = actor.computer or hostname()
        changes["updated_at"] = now
        await QUERIES.update_task(connection, task_id=task_id, changes=json.dumps(changes))
    return await read_task(connection, actor, task_id)


async def delete_task(connection: aiosqlite.Connection, actor: Actor, task_id: int) -> bool:
    task = await read_task(connection, actor, task_id)
    if task is None or task.status == Status.DELETED:
        return False
    return await update_task(connection, actor, task_id, status=Status.DELETED) is not None


async def read_comment(
    connection: aiosqlite.Connection, actor: Actor, comment_id: int
) -> Comment | None:
    row = await QUERIES.comment(connection, comment_id=comment_id, principal=actor.principal)
    return Comment(**dict(row)) if row else None


async def add_comment(
    connection: aiosqlite.Connection, actor: Actor, task_id: int, body: str
) -> Comment:
    if not body.strip():
        raise ValueError("a comment must not be empty")

    task = await read_task(connection, actor, task_id)
    if task is None:
        raise LookupError(f"no task #{task_id}")
    await require(connection, actor, task.project_id, role="editor")
    now = time.time()
    inserted_id = await QUERIES.add_comment(
        connection,
        task_id=task_id,
        body=body,
        created_at=now,
        principal=actor.principal,
        computer=actor.computer or hostname(),
    )
    saved = await read_comment(connection, actor, inserted_id or 0)
    assert saved is not None
    return saved


async def count_comments(connection: aiosqlite.Connection, actor: Actor, task_id: int) -> int:
    if await read_task(connection, actor, task_id) is None:
        return 0
    row = await QUERIES.count_comments(connection, task_id=task_id)
    return int(row[0]) if row else 0


async def read_comments(
    connection: aiosqlite.Connection,
    actor: Actor,
    *,
    task_id: int | None = None,
    project_id: int | None = None,
    computer: str | None = None,
    status: Sequence[Status] | None = None,
    tag: str | None = None,
    query: str | None = None,
    raw: bool = False,
    limit: int = -1,
) -> list[Comment]:
    parameters = filters(
        connection,
        actor,
        project_id=project_id,
        computer=computer,
        status=status,
        tag=tag,
        priority=None,
    )
    parameters.update(task_id=task_id, exclude_deleted=task_id is None and not status, limit=limit)
    if query is not None:
        match = query if raw else match_query(query)
        if not match:
            return []
        rows = QUERIES.search_comments(connection, match=match, **parameters)
    else:
        rows = QUERIES.comments(connection, **parameters)
    return [Comment(**dict(row)) async for row in rows]


def filters(
    connection: aiosqlite.Connection,
    actor: Actor,
    *,
    project_id: int | None,
    status: Sequence[Status] | None,
    tag: str | None,
    priority: Priority | None,
    computer: str | None = None,
) -> dict[str, Any]:
    return {
        "principal": actor.principal,
        "project_id": project_id,
        "computer": computer,
        "statuses": json.dumps([item.value for item in status]) if status else None,
        "tag": f"%,{tag.strip().lower()},%" if tag else None,
        "priority": RANKS[priority] if priority is not None else None,
    }


async def read_tasks(
    connection: aiosqlite.Connection,
    actor: Actor,
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
    parameters = filters(
        connection,
        actor,
        project_id=project_id,
        status=status,
        tag=tag,
        priority=priority,
        computer=computer,
    )
    rows = QUERIES.tasks(connection, **parameters, ordering=order, limit=limit, offset=offset)
    return [to_task(row) async for row in rows]


async def count_tasks(
    connection: aiosqlite.Connection,
    actor: Actor,
    *,
    project_id: int | None = None,
    status: Sequence[Status] | None = None,
    tag: str | None = None,
    priority: Priority | None = None,
    computer: str | None = None,
) -> int:
    parameters = filters(
        connection,
        actor,
        project_id=project_id,
        status=status,
        tag=tag,
        priority=priority,
        computer=computer,
    )
    row = await QUERIES.count_tasks(connection, **parameters)
    return int(row["found"]) if row else 0


async def search_tasks(
    connection: aiosqlite.Connection,
    actor: Actor,
    query: str,
    *,
    project_id: int | None = None,
    status: Sequence[Status] | None = None,
    tag: str | None = None,
    computer: str | None = None,
    raw: bool = False,
    limit: int = 20,
) -> list[Task]:
    match = query if raw else match_query(query)
    if not match:
        return []
    parameters = filters(
        connection,
        actor,
        project_id=project_id,
        status=status,
        tag=tag,
        priority=None,
        computer=computer,
    )
    rows = QUERIES.search_tasks(
        connection,
        **parameters,
        match=match,
        exclude_deleted=status is None,
        limit=limit,
    )
    return [to_task(row) async for row in rows]


async def read_tags(
    connection: aiosqlite.Connection,
    actor: Actor,
    *,
    project_id: int | None = None,
    computer: str | None = None,
) -> list[str]:
    parameters = filters(
        connection,
        actor,
        project_id=project_id,
        status=None,
        tag=None,
        priority=None,
        computer=computer,
    )
    rows = QUERIES.tags(connection, **parameters)
    found: set[str] = set()
    async for row in rows:
        found.update(split_tags(row["tags"]))
    return sorted(found)


async def group(
    connection: aiosqlite.Connection,
    actor: Actor,
    column: str,
    *,
    project_id: int | None = None,
    computer: str | None = None,
) -> dict[str, int]:
    if column not in {"status", "priority"}:
        raise ValueError("unsupported grouping column")
    parameters = filters(
        connection,
        actor,
        project_id=project_id,
        status=None,
        tag=None,
        priority=None,
        computer=computer,
    )
    rows = QUERIES.group_tasks(connection, column=column, **parameters)
    return {str(row["key"]): int(row["found"]) async for row in rows}


async def read_note(connection: aiosqlite.Connection, actor: Actor, note_id: int) -> Note | None:
    row = await QUERIES.note(connection, note_id=note_id, principal=actor.principal)
    return to_note(row) if row else None


async def note_by_title(
    connection: aiosqlite.Connection, actor: Actor, title: str, *, project_id: int
) -> Note | None:
    row = await QUERIES.note_by_title(
        connection, project_id=project_id, title=title, principal=actor.principal
    )
    return to_note(row) if row else None


async def add_note(
    connection: aiosqlite.Connection,
    actor: Actor,
    *,
    project_id: int,
    title: str,
    body: str = "",
    tags: Iterable[str] = (),
    shared: bool = False,
) -> Note:
    check_note(body)
    await require(connection, actor, project_id, role="editor")

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
        created_principal=actor.principal,
        created_computer=actor.computer or hostname(),
        updated_principal=actor.principal,
        updated_computer=actor.computer or hostname(),
    )
    created = await read_note(connection, actor, inserted_id or 0)
    assert created is not None, "the note just written is not there"
    return created


async def update_note(
    connection: aiosqlite.Connection,
    actor: Actor,
    note_id: int,
    *,
    title: str | None = None,
    body: str | None = None,
    tags: Iterable[str] | None = None,
    shared: bool | None = None,
    project_id: int | None = None,
) -> Note | None:
    existing = await read_note(connection, actor, note_id)
    if existing is None:
        return None
    await require(connection, actor, existing.project_id, role="editor")
    if project_id is not None:
        await require(connection, actor, project_id, role="editor")
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
        changes["updated_principal"] = actor.principal
        changes["updated_computer"] = actor.computer or hostname()
        changes["updated_at"] = time.time()

        await QUERIES.update_note(connection, note_id=note_id, changes=json.dumps(changes))
    return await read_note(connection, actor, note_id)


async def append_note(
    connection: aiosqlite.Connection, actor: Actor, note_id: int, text: str
) -> Note | None:
    """Add a paragraph at the end of a note.

    One statement writes the whole body, so the FTS triggers see the final text once. The
    result must fit `NOTE_LIMIT` like any other body; a note that would overflow is untouched.
    """
    existing = await read_note(connection, actor, note_id)
    if existing is None:
        return None
    await require(connection, actor, existing.project_id, role="editor")
    check_note(f"{existing.body}\n\n{text}" if existing.body else text)

    await QUERIES.append_note(
        connection,
        text=text,
        suffix=f"\n\n{text}",
        now=time.time(),
        principal=actor.principal,
        computer=actor.computer or hostname(),
        note_id=note_id,
    )
    return await read_note(connection, actor, note_id)


async def delete_note(connection: aiosqlite.Connection, actor: Actor, note_id: int) -> bool:
    existing = await read_note(connection, actor, note_id)
    if existing is None:
        return False
    await require(connection, actor, existing.project_id, role="editor")

    removed = await QUERIES.delete_note(connection, note_id=note_id)
    return bool(removed)


def note_filters(
    connection: aiosqlite.Connection,
    actor: Actor,
    *,
    project_id: int | None,
    tag: str | None,
    shared: bool | None,
    computer: str | None = None,
) -> dict[str, Any]:
    return {
        "principal": actor.principal,
        "project_id": project_id,
        "computer": computer,
        "shared": shared,
        "tag": f"%,{tag.strip().lower()},%" if tag else None,
    }


async def read_notes(
    connection: aiosqlite.Connection,
    actor: Actor,
    *,
    project_id: int | None = None,
    tag: str | None = None,
    shared: bool | None = None,
    computer: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Note]:
    parameters = note_filters(
        connection, actor, project_id=project_id, tag=tag, shared=shared, computer=computer
    )
    rows = QUERIES.notes(connection, **parameters, limit=limit, offset=offset)
    return [to_note(row) async for row in rows]


async def count_notes(
    connection: aiosqlite.Connection,
    actor: Actor,
    *,
    project_id: int | None = None,
    tag: str | None = None,
    shared: bool | None = None,
    computer: str | None = None,
) -> int:
    parameters = note_filters(
        connection, actor, project_id=project_id, tag=tag, shared=shared, computer=computer
    )
    row = await QUERIES.count_notes(connection, **parameters)
    return int(row["found"]) if row else 0


async def search_notes(
    connection: aiosqlite.Connection,
    actor: Actor,
    query: str,
    *,
    project_id: int | None = None,
    tag: str | None = None,
    shared: bool | None = None,
    computer: str | None = None,
    raw: bool = False,
    limit: int = 20,
) -> list[Note]:
    match = query if raw else match_query(query)
    if not match:
        return []
    parameters = note_filters(
        connection, actor, project_id=project_id, tag=tag, shared=shared, computer=computer
    )
    rows = QUERIES.search_notes(connection, **parameters, match=match, limit=limit)
    return [to_note(row) async for row in rows]
