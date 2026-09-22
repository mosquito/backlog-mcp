"""The MCP surface: tools, resources, prompts and completion over one backlog."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Sequence
from contextvars import ContextVar
from typing import Any, Literal

from aiohttp_tiny_mcp import Authentication, Exchange, Extension, Hint, NeedInput, Registry, elicit
from aiohttp_tiny_mcp.protocol.models import CompleteParams, TextResourceContents
from aiohttp_tiny_mcp.storage.hub import NOTIFICATIONS, topic
from aiohttp_tiny_mcp.storage.sqlite import SqliteHub, SqliteSessionStore, SqliteStorage
from pydantic import BaseModel, Field, field_serializer

from . import embeddings, skills
from .backlog import Backlog, Reach, UnknownProject
from .models import (
    CLOSED_STATUSES,
    EVENT_KEY,
    OPEN_STATUSES,
    PRIORITIES,
    Action,
    Actor,
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
    ProjectDeleted,
    ProjectOut,
    ProjectRootOut,
    Projects,
    Relation,
    Scope,
    Stats,
    Status,
    TaskDetails,
    TaskListItem,
    TaskOut,
    Tasks,
)
from .names import owner_name, principal_label, principal_user
from .store import NOTE_LIMIT, Comment, Note, Project, Task, Unset
from .version import COMMIT, VERSION

log = logging.getLogger(__name__)

INSTRUCTIONS = """
A task backlog and persistent project knowledge store. It holds three kinds of thing: a project, \
which is one board covering one directory tree, a task, which is one item on that board, and a \
note, which is one piece of lasting context from the user that persists across sessions.

`whoami` reports `principal` as `user@computer`, auth type and owner handle without a project.

Agent workflow:
1. Check project registration first. At session start or when changing projects or directories,
call `project_current` before searching context, managing tasks, or investigating code.
Compare its computer and roots with the actual local workspace. For HTTP connections, the
server cwd is not the local cwd. Use `project_list`, with `all_computers=true` when needed,
to find an existing logical project. If the local binding is missing or wrong, call
`project_save(project=<existing full_name>, root=<absolute local project root>)`.
Create a project only if no matching logical project exists. Leave correct bindings unchanged.
Explicit `project=` does not replace this check. Registration does not select a project for
an unscoped HTTP connection; pass `project=` explicitly when needed.

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

Scope. Every tool works on one project, the current one, unless the call names another with \
`project`. The current project is the one fixed for this connection (`--project` on stdio, \
`?project=<slug>` in the URL), else the project whose root contains the working directory, else \
a project registered for that directory on the spot. Nothing else chooses a project: there is \
no pin and no remembered choice, so two clients never redirect each other. `project_current` \
reports which project is current and which of those reasons chose it. A computer reached over \
HTTP with nothing named has no current project: `project_current` then says `origin: none`, and \
every tool that needs a project takes `project=<slug>`. `project_current` also reports `version` \
and `commit`: the package version that answers and the git commit of its checkout, so a stale \
deploy is visible from the client.

Computers. One logical project can have several `(computer, root)` bindings. `project_current` \
reports the computer this session works from and every computer with a binding; a project lists \
its bindings as `user@computer:path` strings in `roots`. `here: true` means this computer has \
a non-empty path. An \
empty stored root is a contextual binding with no files and is omitted from output. \
Do not read, edit, build or run a path belonging \
to another computer. Tasks and notes belong to the logical project, not to one computer.

Projects are private to their creator until shared with project_share. Owners manage access
and deletion; editors change project content; readers can read it. project_members lists
members. Lists, direct IDs, resources, search and notifications require project membership.
Shared notes do not bypass membership. IDs are unique in the common database. Roots belong
to a principal and computer. project_current reports the authenticated principal. Comments
record `principal` as `user@computer`; records expose created_by and updated_by as the same strings.
Use owner/slug names, such as alice/foo, to distinguish projects. Short slugs work only
when unambiguous; #42 selects a stable numeric ID.

Naming a project. A `project` argument accepts owner/slug, an unambiguous short slug,
#id, or an explicit path such as `./src`, `/srv/app`, or `~/dev/thing`.
Unknown names are errors and create nothing. Use `project_save` to register a directory.
`project_list` shows accessible projects with their full names and task counts.

Project language. A project's nullable `preferred_language` specifies the language for project \
work. Follow it when writing task titles, descriptions, and progress updates. Read it from \
`project_current` or `project_list` before writing tasks. Null means no language preference. \
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


registry.provide(Backlog, active)
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
    storage = SqliteStorage(backlog.store.path)
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
    comments = await backlog.store.comments(task_id=task.id)
    return TaskDetails(
        **task_out(task).model_dump(),
        comments=[comment_out(item) for item in comments],
        links=await backlog.store.task_links(task.id),
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


async def scope_of(backlog: Backlog) -> Scope:
    """Where the tools work, and why there."""
    project, origin = await backlog.scope()
    return Scope(
        project=project_out(project, computer=backlog.computer, principal=backlog.store.principal)
        if project
        else None,
        origin=origin,
        principal=backlog.store.principal,
        computer=backlog.computer,
        computers=await backlog.store.computers(),
        cwd=str(backlog.cwd),
        database=str(backlog.store.path),
        state=str(backlog.state.path),
        version=VERSION,
        commit=COMMIT,
    )


async def stats_of(backlog: Backlog, reach: Reach) -> Stats:
    """Counts for one project, for one computer, or for everything."""
    project_id, computer = reach.project_id, reach.computer
    by_priority = await backlog.store.group("priority", project_id=project_id, computer=computer)
    by_status = await backlog.store.group("status", project_id=project_id, computer=computer)
    return Stats(
        project=reach.slug,
        computer=reach.scope_computer,
        principal=backlog.store.principal,
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
        memory=await backlog.store.count_notes(project_id=project_id, computer=computer),
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

    store = bound().store
    event.principal = store.principal
    if recipients is None:
        recipients = [store.principal]
        if event.project_id is not None:
            with contextlib.suppress(LookupError):
                recipients = [
                    member["principal"] for member in await store.members(event.project_id)
                ]
    for identity in recipients:
        anonymous_default = ACTIVE_REGISTRY.get().auth is None and identity == store.principal
        token = namespace.set(None if anonymous_default else identity)
        try:
            await publish_change(event)
        finally:
            namespace.reset(token)


async def linked_tasks(
    backlog: Backlog, *, task_id: int | None = None, project_id: int | None = None
) -> list[Task]:
    """Find resource dependents for notification routing, never for a public response."""
    return await backlog.store.linked_tasks(task_id=task_id, project_id=project_id)


async def invalidate_linked_tasks(tasks: list[Task], backlog: Backlog) -> None:
    """Invalidate neighbor resources without claiming the neighbor was edited.

    Each recipient receives only metadata from their own accessible task. A link may cross
    project boundaries, so the source event must not be copied into the neighbor's namespace.
    """
    from aiohttp_tiny_mcp.storage.namespaces import namespace

    for task in tasks:
        recipients = await backlog.store.notification_recipients(task.project_id)
        event = task_event(task, Action.UPDATED, computer=backlog.computer)
        for identity in recipients:
            anonymous = ACTIVE_REGISTRY.get().auth is None and identity == backlog.store.principal
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
        principal=bound().store.principal,
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
        principal=bound().store.principal,
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
        principal=bound().store.principal,
    )


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
async def project_members(args: ProjectRef, backlog: Backlog) -> Members:
    """List principals with access to a project and their roles."""
    project = await backlog.resolve(args.project)
    return Members(
        items=await backlog.store.members(project.id),
        principal=backlog.store.principal,
        computer=backlog.computer,
    )


@registry.tool()
async def project_share(args: ShareProject, backlog: Backlog) -> Members:
    """Grant read or edit access to a registered principal. Only the owner can share."""
    from .auth import user_identity

    project = await backlog.resolve(args.project)
    principal = user_identity(principal_user(args.principal))
    await backlog.store.share(project.id, principal, args.role)
    await changed(project_event(project, Action.UPDATED, computer=backlog.computer))
    return Members(
        items=await backlog.store.members(project.id),
        principal=backlog.store.principal,
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
async def whoami(args: Nothing, backlog: Backlog) -> Identity:
    """Report principal as user@computer, auth type and owner handle without selecting a project.

    Stdio and HTTP without authorizers use --default-user (default: $USER), with the same
    principal as that Basic username. Authenticated Basic and JWT connections report their
    verified identity. Computer is context, not identity.
    """
    return Identity(
        principal=backlog.store.principal,
        handle=owner_name(backlog.store.principal),
        auth_type=backlog.auth_type,
        computer=backlog.computer,
        assigned_tasks=await backlog.store.count_assigned(
            principal_label(backlog.store.principal, backlog.computer) + ":"
        ),
    )


@registry.tool(title="Current project", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def project_current(args: Nothing, backlog: Backlog) -> Scope:
    """Report the current project, how it was chosen, the computer, and the storage paths.

    Diagnostics only: it changes nothing. Call at session start."""
    return await scope_of(backlog)


@registry.tool(title="List projects", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def project_list(args: ListProjects, backlog: Backlog) -> Projects:
    """List registered projects with open and total task counts.

    `computer` lists another computer's projects, `all_computers` every project there is."""
    reach = await backlog.reach(
        all_projects=True, computer=args.computer, all_computers=args.all_computers
    )
    found = await backlog.store.projects(archived=args.archived, computer=reach.computer)
    return Projects(
        principal=backlog.store.principal,
        computer=backlog.computer,
        items=[
            project_out(item, computer=backlog.computer, principal=backlog.store.principal)
            for item in found
        ],
        count=len(found),
    )


@registry.tool(title="Register or update a project", annotations=Hint.IDEMPOTENT)
async def project_save(args: SaveProject, backlog: Backlog) -> ProjectOut:
    """Register a project or update its name, description, language, or archive status.

    `preferred_language` sets the language for project work and task text. Omit it to keep
    the current value, or send null to clear it. New projects default to null.
    """
    project = await backlog.save(
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
    return project_out(project, computer=backlog.computer, principal=backlog.store.principal)


@registry.tool(title="Delete a project", annotations=Hint.DESTRUCTIVE | ~Hint.OPEN_WORLD)
async def project_delete(args: DeleteProject, ex: Exchange, backlog: Backlog) -> ProjectDeleted:
    """Delete a project with every task and note on its board, and free its directories.

    DESTRUCTIVE and irreversible. Call it only when the user asked to delete this project in so
    many words. Never call it on your own initiative, and never to tidy up: archive with
    `project_save(archived=true)`, which keeps the history. The tool always asks the user to
    confirm. A client that cannot ask is refused; the terminal command `backlog projects delete`
    asks there instead."""
    project = await backlog.lookup(args.project)
    await backlog.store.require(project.id, role="owner")
    contents = await backlog.store.project_contents(project.id)
    found = ProjectDeleted(
        principal=backlog.store.principal,
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
    recipients = [member["principal"] for member in await backlog.store.members(project.id)]
    neighbors = await linked_tasks(backlog, project_id=project.id)
    await backlog.delete(project)
    await invalidate_linked_tasks(
        [task for task in neighbors if task.project_id != project.id], backlog
    )
    await changed(
        project_event(project, Action.DELETED, computer=backlog.computer), recipients=recipients
    )
    found.deleted = True
    return found


@registry.tool(title="Add a task")
async def task_add(args: AddTask, backlog: Backlog) -> TaskOut:
    """Always work through tasks: create a task to track work on the board before doing it.

    Deconstruct user requests into discrete, verifiable tasks. Search for existing work first
    to avoid duplicates. Returns the created task with its id."""
    project = await backlog.resolve(args.project)
    task = await backlog.store.add_task(
        project_id=project.id,
        title=args.title,
        body=args.body,
        status=args.status,
        priority=args.priority,
        tags=args.tags,
    )
    await changed(task_event(task, Action.ADDED, computer=backlog.computer))
    return task_out(task)


@registry.tool(title="List tasks", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def task_list(args: ListTasks, backlog: Backlog) -> Tasks | FullTasks:
    """List tasks to review current work, inspect open items, or pick the next task to work on.

    Always work through tasks. Defaults to open tasks (todo, doing, blocked) ordered by priority.
    For finding specific topics, prefer `search`. Use filters and pagination."""
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
    if args.full:
        return FullTasks(
            items=[task_out(item) for item in found],
            count=total,
            project=reach.slug,
            computer=reach.scope_computer,
            principal=backlog.store.principal,
        )
    return Tasks(
        items=[task_list_item(item) for item in found],
        count=total,
        project=reach.slug,
        computer=reach.scope_computer,
        principal=backlog.store.principal,
    )


@registry.tool(title="Read a task", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def task_get(args: TaskId, backlog: Backlog) -> TaskDetails:
    """Read a task with its direct links, comments and history.

    The body is only the description. Comments are chronological. Links use relation types
    relative to this task and include current target titles and statuses, without nested links.
    Links to tasks you cannot access are omitted.
    """
    task = await backlog.store.task(args.id)
    if task is None:
        raise LookupError(f"no task #{args.id}")
    return await task_details(task, backlog)


@registry.tool(title="List assigned tasks", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def task_assigned(args: AssignedTasks, backlog: Backlog) -> Tasks:
    """Find assignments by literal prefix across all accessible projects and statuses.

    Null or omitted prefix selects the current principal. Use username@computer: for a
    principal or username@computer:marker for an agent marker prefix. Percent and underscore
    are literal characters, not wildcards. Matching is case-sensitive. Closed tasks remain
    assigned until explicitly released. Count includes all matches before pagination.
    """
    prefix = args.prefix
    if prefix is None:
        prefix = principal_label(backlog.store.principal, backlog.computer) + ":"
    tasks = await backlog.store.assigned_tasks(prefix, limit=args.limit, offset=args.offset)
    return Tasks(
        principal=backlog.store.principal,
        computer=backlog.computer,
        items=[task_list_item(task) for task in tasks],
        count=await backlog.store.count_assigned(prefix),
    )


@registry.tool(title="Claim a task")
async def task_claim(args: ClaimTask, backlog: Backlog) -> TaskOut:
    """Atomically replace assigned only if its current value equals expected.

    Both nullable arguments are required. A mismatch is an error with the current assignment,
    even if it already equals the requested value. Requires editor access. Non-null assigned
    must use the current principal and a URL-safe marker chosen by the agent. Keep the marker
    across reconnects. Claim before starting work; on conflict do not start the task.
    Use assigned=null to release. Status is independent; closing does not release ownership.
    """
    task, updated = await backlog.store.claim_task(
        args.task, expected=args.expected, assigned=args.assigned
    )
    if updated:
        await changed(task_event(task, Action.UPDATED, computer=backlog.computer))
    return task_out(task)


@registry.tool(title="Link tasks", annotations=Hint.IDEMPOTENT)
async def task_link(args: LinkTask, backlog: Backlog) -> LinkChange:
    """Add a typed relation and its inverse atomically, or update their reason.

    Read links through task_get. Requires editor access to both tasks. Self-links and cycles
    in part_of or depends_on are rejected. Repeated calls are safe, including from the inverse
    direction. Links do not change task statuses. Closing or soft-deleting tasks keeps links.
    """
    updated = await backlog.store.link_task(
        args.task, args.relation, args.target, reason=args.reason
    )
    if updated:
        for task_id in (args.task, args.target):
            task = await backlog.store.task(task_id)
            assert task is not None
            await changed(task_event(task, Action.UPDATED, computer=backlog.computer))
    return LinkChange(task=args.task, relation=args.relation, target=args.target, changed=updated)


@registry.tool(title="Unlink tasks", annotations=Hint.IDEMPOTENT)
async def task_unlink(args: UnlinkTask, backlog: Backlog) -> LinkChange:
    """Remove a relation and its inverse atomically, from either direction.

    Requires editor access to both tasks. A missing link is a safe no-op. Other relation types
    between the same tasks are preserved.
    """
    updated = await backlog.store.unlink_task(args.task, args.relation, args.target)
    if updated:
        for task_id in (args.task, args.target):
            task = await backlog.store.task(task_id)
            assert task is not None
            await changed(task_event(task, Action.UPDATED, computer=backlog.computer))
    return LinkChange(task=args.task, relation=args.relation, target=args.target, changed=updated)


@registry.tool(title="Add a task comment", annotations=Hint.OPEN_WORLD)
async def task_comment_add(args: AddComment, backlog: Backlog) -> CommentOut:
    """Record progress, findings, blockers, or verification as a separate Markdown comment.

    Use this for routine updates. Follow the project's preferred_language. Task descriptions
    change only through task_update. Repeated calls create separate comments.
    """
    comment = await backlog.store.add_comment(args.task_id, args.body)
    task = await backlog.store.task(args.task_id)
    assert task is not None
    await changed(task_event(task, Action.UPDATED, computer=backlog.computer))
    return comment_out(comment)


@registry.tool(title="List task comments", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def task_comment_list(args: ListComments, backlog: Backlog) -> Comments:
    """Read comments and history in creation-time order, with id as a stable tie-breaker."""
    task = await backlog.store.task(args.task_id)
    if task is None:
        raise LookupError(f"no task #{args.task_id}")
    details = await task_details(task, backlog)
    return Comments(
        principal=backlog.store.principal,
        computer=backlog.computer,
        items=details.comments,
        task_id=task.id,
        count=len(details.comments),
    )


@registry.tool(title="Update a task", annotations=Hint.IDEMPOTENT)
async def task_update(args: UpdateTask, backlog: Backlog) -> TaskOut:
    """Edit task fields. A supplied body replaces the description; an empty string clears it.

    Use task_comment_add for progress, findings, and verification. Status transitions create
    separate history entries. Mark active work doing and completed work done or cancelled.
    Closing stamps the closing time; reopening clears it.
    """
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
    await changed(task_event(task, Action.UPDATED, computer=backlog.computer))
    if args.title is not None or args.status is not None:
        await invalidate_linked_tasks(await linked_tasks(backlog, task_id=task.id), backlog)
    return task_out(task)


@registry.tool(title="Move a task", annotations=Hint.IDEMPOTENT)
async def task_move(args: MoveTask, backlog: Backlog) -> TaskOut:
    """Move a task to another project board and record the move in its history."""
    project = await backlog.lookup(args.project)
    existing = await backlog.store.task(args.id)
    if existing is None:
        raise LookupError(f"no task #{args.id}")
    if existing.project_id == project.id:
        return task_out(existing)
    task = await backlog.store.update_task(
        args.id,
        project_id=project.id,
    )
    assert task is not None
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
            principal=backlog.store.principal,
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
async def task_delete(args: TaskId, backlog: Backlog) -> Deleted:
    """Delete a task. Marks its status as `deleted` without removing the record.

    Keeps history intact and allows recovery. Works across projects without confirmation."""
    task = await backlog.store.task(args.id)
    if task is None:
        raise LookupError(f"no task #{args.id}")
    if task.status != Status.DELETED:
        gone = await backlog.store.update_task(task.id, status=Status.DELETED)
        assert gone is not None
        await changed(task_event(gone, Action.DELETED, computer=backlog.computer))
        await invalidate_linked_tasks(await linked_tasks(backlog, task_id=task.id), backlog)
    return Deleted(
        principal=backlog.store.principal,
        computer=backlog.computer,
        id=args.id,
        title=task.title,
        deleted=True,
    )


@registry.tool(title="Save a note", annotations=Hint.IDEMPOTENT)
async def memory_save(args: SaveNote, backlog: Backlog) -> NoteOut:
    """Save lasting user context: preferences, agreements, architectural invariants, or constraints.

    Proactively save useful new knowledge as soon as the user supplies it; do not wait.
    Check notes first if unsure what to do. Code changes; notes remain. Exclude temporary
    implementation details and doc summaries. Update by id or title; otherwise create a note.
    Only supplied fields change. Body replaces whole body. Use id to update shared notes."""
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
            await changed(note_event(saved, Action.ADDED, computer=backlog.computer))
            return note_out(saved)
        args.id = existing.id
    updated = await backlog.store.update_note(
        args.id, title=args.title, body=args.body, tags=args.tags, shared=args.shared
    )
    if updated is None:
        raise LookupError(f"no note #{args.id}")
    await changed(note_event(updated, Action.UPDATED, computer=backlog.computer))
    return note_out(updated)


@registry.tool(title="Append to a note")
async def memory_append(args: AppendNote, backlog: Backlog) -> NoteOut:
    """Append a related fact, preference, or constraint to lasting user context.

    Find the note with `search` first. Use `memory_save` to correct or replace existing content."""
    found = await note_of(backlog, args.id, args.title, args.project)
    updated = await backlog.store.append_note(found.id, args.text)
    if updated is None:
        raise LookupError(f"no note #{found.id}")
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
async def memory_list(args: ListNotes, backlog: Backlog) -> Notes | FullNotes:
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
    if args.full:
        return FullNotes(
            items=[note_out(item) for item in found],
            count=total,
            project=reach.slug,
            computer=reach.scope_computer,
            principal=backlog.store.principal,
        )
    return Notes(
        items=[note_list_item(item) for item in found],
        count=total,
        project=reach.slug,
        computer=reach.scope_computer,
        principal=backlog.store.principal,
    )


@registry.tool(title="Delete a note", annotations=Hint.DESTRUCTIVE | ~Hint.OPEN_WORLD)
async def memory_delete(args: NoteId, ex: Exchange, backlog: Backlog) -> Deleted:
    """Delete a note permanently. DESTRUCTIVE: call it only when the user asked for exactly that.

    Never delete a note on your own initiative. A note of the current project goes at once. A
    note of another project, or any note when no project is chosen, goes only after the user
    confirms; a client that cannot ask is refused."""
    found = await backlog.store.note(args.id)
    if found is None:
        raise LookupError(f"no note #{args.id}")
    if refusal := await confirm_cross_project_delete(found, ex, backlog):
        return refusal
    deleted = await backlog.store.delete_note(args.id)
    await changed(note_event(found, Action.DELETED, computer=backlog.computer))
    return Deleted(
        principal=backlog.store.principal,
        computer=backlog.computer,
        id=args.id,
        title=found.title,
        deleted=deleted,
    )


@registry.tool(title="Search the backlog", annotations=Hint.READ_ONLY | ~Hint.OPEN_WORLD)
async def search(args: embeddings.Search, backlog: Backlog) -> embeddings.Matches:
    """Primary discovery tool. ALWAYS search first before investigating or deciding.

    Use frequently: at session start, before choosing an approach, before asking questions, and
    whenever you are uncertain what to do or what conventions apply. Combines full-text prefix
    matching and multilingual semantic search (FTS matches first).
    Prefer targeted search over full listings. Read individual matches with `memory_get` or
    `task_get` only when detail is needed."""
    return await embeddings.search(args, backlog)


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
        project_out(
            project, computer=backlog.computer, principal=backlog.store.principal
        ).model_dump(mode="json")
        if project
        else None
    )
    return {"project": shown, "origin": origin.value, "computer": backlog.computer}


@registry.resource("backlog://projects", mime_type="application/json")
async def all_projects(args: Nothing, backlog: Backlog) -> list[dict[str, Any]]:
    """Every registered project of this computer."""
    found = await backlog.store.projects(computer=backlog.computer)
    return [
        project_out(item, computer=backlog.computer, principal=backlog.store.principal).model_dump(
            mode="json"
        )
        for item in found
    ]


@registry.resource("backlog://tasks", mime_type="application/json")
async def open_tasks(args: Nothing, backlog: Backlog) -> list[dict[str, Any]]:
    """Open tasks of the current project, most important first."""
    project = await backlog.resolve(None)
    found = await backlog.store.tasks(project_id=project.id, status=list(OPEN_STATUSES), limit=200)
    return [task_out(item).model_dump(mode="json") for item in found]


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
    return (await task_details(task, backlog)).model_dump(mode="json")


@registry.resource("backlog://memory", mime_type="application/json")
async def known_notes(args: Nothing, backlog: Backlog) -> list[dict[str, Any]]:
    """Notes of the current project plus every shared note, most recently changed first."""
    project = await backlog.resolve(None)
    found = await backlog.store.notes(project_id=project.id, limit=200)
    return [note_out(item).model_dump(mode="json") for item in found]


@registry.resource("backlog://memory/{id}", name="note", mime_type="application/json")
async def one_note(args: TaskUri, backlog: Backlog) -> dict[str, Any]:
    """One note by id."""
    if not args.id.isdigit():
        return {"error": f"invalid note id {args.id!r}"}
    found = await backlog.store.note(int(args.id))
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
        f"Open backlog of {project.full_name}:\n\n"
        + ("\n".join(lines) or "- nothing open")
        + "\n\nReview it: what should be done first, what duplicates what, "
        "and which items are too vague to start. Suggest concrete edits."
    )


@registry.completions
async def complete(args: CompleteParams, backlog: Backlog) -> list[str]:
    """Complete owner-qualified project names, tags, statuses, priorities, task ids and note ids."""
    value = args.argument.value
    #: The task and the note templates both call their argument `id`; the URI tells them apart.
    about_notes = "memory" in (args.ref.uri or "")
    match args.argument.name, args.ref.type:
        case "project", _:
            return [
                name
                for name in await backlog.store.slugs()
                if name.startswith(value) or name.split("/", 1)[1].startswith(value)
            ]
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
