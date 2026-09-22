"""The MCP surface: tools, resources, prompts and completion over one backlog."""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import Iterable, Sequence
from contextvars import ContextVar
from typing import Any, Literal

import aiosqlite
from aiohttp_tiny_mcp import Authentication, Exchange, Extension, Hint, NeedInput, Registry, elicit
from aiohttp_tiny_mcp.protocol.models import CompleteParams, TextResourceContents
from aiohttp_tiny_mcp.storage.hub import NOTIFICATIONS, topic
from aiohttp_tiny_mcp.storage.sqlite import SqliteHub, SqliteSessionStore, SqliteStorage
from pydantic import BaseModel, Field, field_serializer

from . import embeddings, skills
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

INSTRUCTIONS = """
A task backlog and persistent project knowledge store. It holds three kinds of thing: a project, \
which is one board covering one directory tree, a task, which is one item on that board, and a \
note, which is one piece of lasting context from the user that persists across sessions.

`whoami` reports `principal` as `user@computer`, auth type and owner handle without a project.

Agent workflow:
1. Check project registration first. At session start or when changing projects or directories,
use `whoami` and `project_list` to compare the principal and project roots with the local
workspace. Use `all_computers=true` to find an existing logical project on another computer.
For HTTP connections, the server cwd is not the local cwd. If the local binding is missing
or wrong, call `project_save(project=<existing full_name>, root=<absolute local project root>)`.
Create a project only if no matching logical project exists. Leave correct bindings unchanged.
Read `project_board(project=<full_name>)` for project context, counts and ready/claimed tasks.
Registration and reading a board do not select a project for an unscoped HTTP connection;
pass `project=` explicitly when needed.

2. Search context before decisions. After registration, use `search` before investigating,
choosing an approach, creating tasks or memory, or asking about prior agreements.
Use natural-language queries. Search again when a new question arises. Read relevant matches
with `task_get` or `memory_get` when needed. Check memory before guessing user preferences
or project conventions. Use filtered lists for board reviews and `stats` for counts.
Verify current technical facts in code or documentation.

3. Track work through tasks. Reuse a relevant task or create one with `task_add` before
substantive work, including investigation and verification. Define one verifiable outcome
per task. A brief answer with no separate work does not need a task. Set `doing` when work
starts. Use `task_comment_add` for findings, decisions, checks, blockers, remaining work,
and relevant commit hashes or deployment state. Send only new update text.
`task_update(body=...)` replaces the description; it does not append history.
If scope must be replaced, close the old task and create a successor referencing it.
Set `done` only after verification, `blocked` with the blocker and next step, or `cancelled`
when work is abandoned. Before ending a turn, leave task status and history accurate.
Keep task tracking in backlog. Never create task lists, work plans, progress logs, or task
checklists in files, even when backlog is unavailable.

4. Keep lasting agreements in memory. Proactively save user preferences, constraints, and
decisions with `memory_save` or `memory_append`. Search first and update an existing record
when appropriate. Use `shared=true` for agreements that apply across accessible projects.
Use task comments for temporary findings and implementation progress. Do not save code or
documentation snapshots as lasting memory. `memory_save.body` replaces the body;
`memory_append` adds related context. Keep each record within 1024 characters.
Never store secrets or unconfirmed guesses. After a user correction, save the lasting
agreement that prevents recurrence.

5. Handle outages explicitly. If backlog is unavailable, report the failure and continue
authorized work that does not require missing context. Keep pending task and memory updates
in the conversation and save them when access returns. Do not claim a write succeeded
unless the tool confirmed it.

Scope. `project_board(project=...)` requires an explicit existing project. It returns project
context, task counts, accessible memory count and up to ten ready tasks and ten tasks claimed
by this principal on this computer.
It never creates a project or changes connection scope. Read a task before claiming it;
another agent can claim it after the board was read.
Other project-scoped tools use their explicit `project`, then the connection project
(`--project` on stdio or `?project=<slug>` over HTTP). Local connections can fall back to
the working directory and register it. An unscoped remote computer must pass `project=`.
There is no mutable or remembered project selection shared between clients.

Computers. One logical project can have several `(computer, root)` bindings.
`whoami` reports the authenticated principal as `user@computer`. Project roots use
`user@computer:path` strings. In `project_list`, `here: true` means this computer has
a non-empty path. Empty contextual bindings are omitted from roots.
Do not read, edit, build or run a path belonging to another computer.
Tasks and memory belong to the logical project, not to one computer.

Projects are private to their creator until shared with project_share. Owners manage access
and deletion; editors change project content; readers can read it. project_members lists
members. Lists, direct IDs, resources, search and notifications require project membership.
Shared notes do not bypass membership. IDs are unique in the common database. Roots belong
to a principal and computer. whoami reports the authenticated principal. Comments
record `principal` as `user@computer`; records expose created_by and updated_by as the same strings.
Use owner/slug names, such as alice/foo, to distinguish projects. Short slugs work only
when unambiguous; #42 selects a stable numeric ID.

Naming a project. A `project` argument accepts owner/slug, an unambiguous short slug,
#id, or an explicit path such as `./src`, `/srv/app`, or `~/dev/thing`.
Unknown names are errors and create nothing. Use `project_save` to register a directory.
`project_list` shows accessible projects with their full names and task counts.

Project language. A project's nullable `preferred_language` specifies the language for project \
work. Follow it when writing task titles, descriptions, and progress updates. Read it from \
`project_board` or `project_list` before writing tasks. Null means no language preference. \
Set or change it with `project_save`; omit the field to keep it, or send null to clear it.

Tasks. A task has a title, a Markdown description in `body`, a status, a priority, and tags.
Always track work: set `doing` when starting and `done`, `cancelled`, or `deleted` when appropriate.
Listings show open statuses, most important first, unless filters ask for others.

Assignment. Before starting work, claim the task with task_claim(task, expected, assigned).
Both nullable fields are required. Use expected=null for a free task and assigned=null to
release. Non-null assigned must be your principal from whoami plus :marker, with a distinct
URL-safe marker per agent run. On conflict, do not start the task. Status does not release an
assignment. task_assigned(prefix=null) lists your assignments across accessible projects;
an explicit literal prefix selects another principal or marker. whoami reports assigned_tasks.

Comments. Use `task_comment_add` for progress, findings, blockers, decisions, and verification.
Comments are stored separately from the description. `task_update(body=...)` replaces the
whole description; use it only to revise the task definition. An empty body clears it.
Status transitions and moves create separate history entries. `task_get` returns all `comments`
in chronological order. Use `task_comment_list` to read the complete history separately.
Each entry has its own id, task_id, kind, and creation time.

Notes. Code changes; notes remain. Keep lasting user context unavailable elsewhere: \
preferences, working agreements, people and responsibilities, business constraints, and reasons \
for decisions. Check notes whenever you are unsure what to do or need guidance. Notes concern the \
context around the work, not the project's implementation. Do not save snapshots of code, \
architecture, configuration, bugs, or documentation. They become stale as the project changes. \
A useful note should still help after the code is rewritten. Project scope organizes notes; it \
does not define their subject.

Use `search` as the primary retrieval tool alongside web search. Check saved notes to recall \
past agreements, preferences, and constraints before making decisions or asking the user again. \
Use code, documentation, and the web for current technical facts. Retrieve relevant notes by \
search instead of reading them all. Recheck an old agreement when the user provides new context.

Save useful new context proactively as soon as the user supplies it. Do not wait for a request \
to remember it or for the session to end. Search first, then update an existing note or create \
one short note per topic. Use `memory_save` to replace outdated context and `memory_append` for \
related facts. A supplied body replaces the whole body. Omit guesses, duplicates, and secrets. \
Shared notes are visible across projects. Link notes by stable id, such as `note #3`.

Reach. `task_list`, `memory_list`, `search` and `stats` stay inside one project, cover every \
project of this computer with `all_projects=true`, every project of another computer with \
`computer=<name>`, and everything with `all_computers=true`; `project_list` takes the same two \
computer arguments. Shared notes are visible from every project and every computer. Task and \
note ids are unique across projects and computers, so `task_get`, `task_update`, `task_move`, \
`task_delete`, `memory_get`, `memory_append` and `memory_delete` take an id and no project.

Search. `search` is the default retrieval tool for project knowledge, previous work, and user \
preferences. Search after registration, before investigating a problem or choosing an approach, \
before asking the user for project facts, and before creating a task or note. Search again \
whenever any uncertainty or question arises during work. Use a short natural-language question \
or specific terms; try another phrasing if the results are weak. Inspect returned matches and \
fetch individual records with `memory_get` or `task_get` only when more detail is needed. Check \
facts that may have changed. Avoid full listings and reading every task or note. Reserve \
`task_list` and `memory_list` for an explicitly requested inventory or a complete review; \
use filters and pagination then. Use `stats` when only counts are needed.

`search` combines full-text prefix matching and multilingual semantic search. Use `kinds`
to select projects, tasks, comments, memory, or several kinds. Task descriptions and comments
are indexed separately; comment matches include the comment id and task_id. FTS matches come first,
then semantic matches, without duplicate IDs within each kind. Embeddings run locally.
Project results include full_name and exclude archived projects. Membership and scope filter
FTS and semantic candidates before ranking and limits. Use all_computers=true to discover
accessible projects when no current project is selected.

Writes. `task_add`, `task_comment_add`, `task_update`, `task_move`, `task_delete`,
`task_link`, `task_unlink`, `task_claim`,
`memory_save`, `memory_append`, \
`memory_delete`, `project_save` and `project_delete` are the only tools that change anything; each \
one tells subscribed clients which resources went stale, and `_meta["backlog/event"]` of that \
notification says what the write did: kind, action, id, project, title, status, priority and \
the writer's computer. The extension `backlog-mcp/events` sends the same event to every \
listener as `notifications/backlog/task`, `notifications/backlog/memory` (multicast by project \
slug) and `notifications/backlog/project`; a 2026-07-28 client asks for them in \
`subscriptions/listen`, older streams get them all. `task_delete` marks a task's status as \
`deleted` without removing the record. `memory_delete` and `project_delete` are destructive and \
irreversible: call them only when the user asked for exactly that, never on your own initiative. \
`memory_delete` deletes at once within the current project and asks the user to confirm when the \
note belongs to another project or no project is chosen. `project_delete` removes a project with \
every task and note it holds and always asks the user to confirm. Nothing stands in for the \
user's answer: a client that cannot ask is refused. Archive with `project_save` when the history \
should stay.

Skills. This server carries written skills: `triage`, for keeping a board true, and `planning`, \
for cutting a request into tasks. A client that speaks the skills extension gets them from \
`skills/list` and `skills/get`. Older clients read \
`mcp-extensions://io.modelcontextprotocol/skills/skills/list` through `resources/read`, \
then read the file URIs in that catalogue. Older clients see ordinary namespaced resources. \
Skills are instructions only: read them, then use the tools above to do the work.

Also available: the resources `backlog://project`, `backlog://projects`, `backlog://tasks`, \
`backlog://tasks/{id}`, `backlog://memory` and `backlog://memory/{id}` for reading without a tool \
call, and the prompts `standup`, which drafts an update from recent work, and `groom`, which \
reviews the open backlog.
""".strip()

#: The backlog the handlers work on. A context variable, so tests can run several at once.
ACTIVE: ContextVar[Backlog] = ContextVar("backlog")

# Keep tool catalog pages below the default stdio reader's 64 KiB frame limit.
registry = Registry("backlog", VERSION, instructions=INSTRUCTIONS, page_size=20)


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
) -> tuple[Registry, SqliteStorage]:
    """Use the application database for protocol sessions and notification events."""
    storage = SqliteStorage(backlog.database.path)
    configured = Registry(
        "backlog",
        VERSION,
        instructions=registry.instructions,
        hub=SqliteHub(storage),
        session_store=SqliteSessionStore(storage),
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
    """The compact note shape used by listings."""
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
        computer=reach.scope_computer,
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
    note events only for the projects they named.
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
    """The note an id or a title names, or `LookupError`.

    A title is looked up in the resolved project first, then among the shared notes of the other
    projects, so a shared note reads from anywhere and only its own project can replace it.
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
        description="Project owner/slug, unique short slug, #id, or path. Defaults to current.",
    )


class ShareProject(ProjectRef):
    principal: str = Field(
        description="user@computer or a username without @; computer does not affect access."
    )
    role: Literal["reader", "editor"] = "reader"


class Members(Actor):
    items: list[dict[str, str]]

    @field_serializer("items", when_used="json")
    def principal_labels(self, values: list[dict[str, str]]) -> list[dict[str, str]]:
        return [{**item, "principal": principal_label(item["principal"])} for item in values]


@registry.tool()
async def project_members(
    args: ProjectRef, backlog: Backlog, connection: aiosqlite.Connection
) -> Members:
    """List principals with access to a project and their roles."""
    project = await backlog.resolve(args.project)
    return Members(
        items=await members(connection, backlog.actor, project.id),
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
    return Members(
        items=await members(connection, backlog.actor, project.id),
        principal=backlog.principal,
        computer=backlog.computer,
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
        description="Reason, constraints, acceptance criteria, and dependent task ids.",
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


class ClaimTask(BaseModel):
    model_config = {"extra": "forbid"}

    task: int = Field(description="Task id to claim, release or reassign.")
    expected: NullableAssignment = Field(
        description="Expected current assignment; null requires an unassigned task."
    )
    assigned: NullableAssignment = Field(
        description="New username@computer:marker of the current principal; null releases."
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


class MoveTask(BaseModel):
    id: int
    project: str = Field(
        description="Destination project: owner/slug, unique short slug, #id, or explicit path."
    )


class SaveNote(BaseModel):
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
    shared: bool | None = Field(default=None, description="Make note visible across all projects.")
    id: int | None = Field(default=None, description="Change this note, whatever its title.")
    project: str | None = Field(default=None, description="Defaults to the current project.")


class AppendNote(BaseModel):
    text: str = Field(
        max_length=NOTE_LIMIT,
        description=(
            "One related fact, preference, or constraint from the user. "
            f"The whole note must stay within {NOTE_LIMIT} characters."
        ),
    )
    id: int | None = Field(default=None, description="The note to extend.")
    title: str | None = Field(default=None, description="Its title instead, in `project`.")
    project: str | None = Field(default=None, description="Defaults to the current project.")


class NoteId(BaseModel):
    id: int


class ReadNote(BaseModel):
    id: int | None = Field(
        default=None,
        description="Note to read. Check notes whenever uncertain about preferences or rules.",
    )
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


@registry.tool(title="Current identity", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def whoami(args: Nothing, backlog: Backlog, connection: aiosqlite.Connection) -> Identity:
    """Report principal as user@computer, auth type and owner handle without selecting a project.

    Stdio and HTTP without authorizers use --default-user (default: $USER), with the same
    principal as that Basic username. Authenticated Basic and JWT connections report their
    verified identity. Computer is context, not identity.
    """
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
    """Start here for an existing project's context, counts and ready/claimed tasks.

    Project is required; this read never creates a project or changes connection scope.
    Ready tasks are unassigned todo/doing tasks with all direct dependencies closed
    (done/cancelled/deleted). Inaccessible dependencies prevent readiness.
    Claimed lists open tasks assigned to this principal and computer, across all markers.
    Each section has a full count and up to ten {id, title} items ordered by priority, age and id.
    Task counters cover the whole project; blocked includes status and dependency blockers.
    Read task_get before claiming one with task_claim; the board is not a reservation.
    Memory counts include accessible shared memory; search it for relevant agreements.
    Follow project.preferred_language when writing tasks and progress updates.
    """
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
    """Delete a project with every task and note on its board, and free its directories.

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
    """Always work through tasks: create a task to track work on the board before doing it.

    Deconstruct user requests into discrete, verifiable tasks. Search for existing work first
    to avoid duplicates. Returns the created task with its id."""
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
    """List tasks to review current work, inspect open items, or pick the next task to work on.

    Always work through tasks. Defaults to open tasks (todo, doing, blocked) ordered by priority.
    For finding specific topics, prefer `search`. Use filters and pagination."""
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
            computer=reach.scope_computer,
            principal=backlog.principal,
        )
    return Tasks(
        items=[task_list_item(item) for item in found],
        count=total,
        project=reach.slug,
        computer=reach.scope_computer,
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
    """Atomically replace assigned only if its current value equals expected.

    Both nullable arguments are required. A mismatch is an error with the current assignment,
    even if it already equals the requested value. Requires editor access. Non-null assigned
    must use the current principal and a URL-safe marker chosen by the agent. Keep the marker
    across reconnects. Claim before starting work; on conflict do not start the task.
    Use assigned=null to release. Status is independent; closing does not release ownership.
    """
    async with transaction:
        task, updated = await claim_task(
            connection, backlog.actor, args.task, expected=args.expected, assigned=args.assigned
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
    Closing stamps the closing time; reopening clears it.
    """
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
    """Ask when the note belongs to another project, or when no project is chosen.

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


@registry.tool(title="Save a note", annotations=Hint.IDEMPOTENT)
async def memory_save(
    args: SaveNote, backlog: Backlog, connection: aiosqlite.Connection, transaction: Transaction
) -> NoteOut:
    """Save lasting user context: preferences, agreements, architectural invariants, or constraints.

    Proactively save useful new knowledge as soon as the user supplies it; do not wait.
    Check notes first if unsure what to do. Code changes; notes remain. Exclude temporary
    implementation details and doc summaries. Update by id or title; otherwise create a note.
    Only supplied fields change. Body replaces whole body. Use id to update shared notes."""
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


@registry.tool(title="Append to a note")
async def memory_append(
    args: AppendNote, backlog: Backlog, connection: aiosqlite.Connection, transaction: Transaction
) -> NoteOut:
    """Append a related fact, preference, or constraint to lasting user context.

    Find the note with `search` first. Use `memory_save` to correct or replace existing content."""
    found = await note_of(backlog, args.id, args.title, args.project)
    async with transaction:
        updated = await append_note(connection, backlog.actor, found.id, args.text)
    if updated is None:
        raise LookupError(f"no note #{found.id}")
    embeddings.enqueue(backlog, updated)
    await changed(note_event(updated, Action.UPDATED, computer=backlog.computer))
    return note_out(updated)


@registry.tool(title="Read a note", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def memory_get(args: ReadNote, backlog: Backlog) -> NoteOut:
    """Read saved user context, preferences, conventions, or past agreements.

    ALWAYS check notes whenever you do not know what to do, what style or architecture to follow,
    or what the user prefers. Check notes before asking questions or making assumptions.
    Ids work across projects. Titles resolve within the selected project and shared notes."""
    return note_out(await note_of(backlog, args.id, args.title, args.project))


@registry.tool(title="List notes", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def memory_list(
    args: ListNotes, backlog: Backlog, connection: aiosqlite.Connection
) -> Notes | FullNotes:
    """List persistent notes containing user context, preferences, and agreements.

    Browse notes whenever you lack direction, are unsure how to proceed, or need rules.
    For targeted topic search, prefer `search`. Use filters and pagination."""
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
            computer=reach.scope_computer,
            principal=backlog.principal,
        )
    return Notes(
        items=[note_list_item(item) for item in found],
        count=total,
        project=reach.slug,
        computer=reach.scope_computer,
        principal=backlog.principal,
    )


@registry.tool(title="Delete a note", annotations=Hint.DESTRUCTIVE | ~Hint.OPEN_WORLD)
async def memory_delete(
    args: NoteId,
    ex: Exchange,
    backlog: Backlog,
    connection: aiosqlite.Connection,
    transaction: Transaction,
) -> Deleted:
    """Delete a note permanently. DESTRUCTIVE: call it only when the user asked for exactly that.

    Never delete a note on your own initiative. A note of the current project goes at once. A
    note of another project, or any note when no project is chosen, goes only after the user
    confirms; a client that cannot ask is refused."""
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
    """Primary discovery tool. ALWAYS search first before investigating or deciding.

    Use frequently: at session start, before choosing an approach, before asking questions, and
    whenever you are uncertain what to do or what conventions apply. Combines full-text prefix
    matching and multilingual semantic search (FTS matches first).
    Prefer targeted search over full listings. Read individual matches with `memory_get` or
    `task_get` only when detail is needed."""
    return await embeddings.search(args, backlog, connection, transaction)


@registry.tool(title="Backlog statistics", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def stats(args: StatsOf, backlog: Backlog) -> Stats:
    """Count open tasks, closed tasks, and notes, with status and priority breakdowns and tags.

    Use for a high-level board overview before planning or after triage."""
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
    """Notes of the current project plus every shared note, most recently changed first."""
    project = await backlog.resolve(None)
    found = await read_notes(connection, backlog.actor, project_id=project.id, limit=200)
    return [note_out(item).model_dump(mode="json") for item in found]


@registry.resource("backlog://memory/{id}", name="note", mime_type="application/json")
async def one_note(
    args: TaskUri, backlog: Backlog, connection: aiosqlite.Connection
) -> dict[str, Any]:
    """One note by id."""
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
    """Complete owner-qualified project names, tags, statuses, priorities, task ids and note ids."""
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
    limit: int = 20,
) -> list[Project]:
    match = match_query(query)
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
    row = await QUERIES.board_summary(
        connection,
        project_id=project_id,
        principal=actor.principal,
        prefix=principal_label(actor.principal, actor.computer) + ":",
    )
    assert row is not None
    counts = dict(row)
    sections = [
        BoardSection(
            count=counts.pop(f"{name}_count"),
            items=json.loads(counts.pop(f"{name}_items")),
        )
        for name in ("ready", "claimed")
    ]
    return counts, sections[0], sections[1]


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
) -> tuple[Task, bool]:
    """Compare and replace an assignment under a database writer lock."""
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
    if expected == assigned:
        return task, False
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
) -> Task | None:

    existing = await read_task(connection, actor, task_id)
    if existing is None:
        return None
    await require(connection, actor, existing.project_id, role="editor")
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
        match = match_query(query)
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
    limit: int = 20,
) -> list[Task]:
    match = match_query(query)
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
    limit: int = 20,
) -> list[Note]:
    match = match_query(query)
    if not match:
        return []
    parameters = note_filters(
        connection, actor, project_id=project_id, tag=tag, shared=shared, computer=computer
    )
    rows = QUERIES.search_notes(connection, **parameters, match=match, limit=limit)
    return [to_note(row) async for row in rows]
