"""What the tools accept and return: the server's public shape."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Status(StrEnum):
    """Where a task stands. Only `DONE`, `CANCELLED` and `DELETED` close it."""

    TODO = "todo"
    DOING = "doing"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"
    DELETED = "deleted"


class Priority(StrEnum):
    URGENT = "urgent"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


OPEN_STATUSES: tuple[Status, ...] = (Status.TODO, Status.DOING, Status.BLOCKED)
CLOSED_STATUSES: tuple[Status, ...] = (Status.DONE, Status.CANCELLED, Status.DELETED)

#: Priorities are stored as a rank so SQL can order by them.
RANKS: dict[Priority, int] = {
    Priority.URGENT: 0,
    Priority.HIGH: 1,
    Priority.NORMAL: 2,
    Priority.LOW: 3,
}
PRIORITIES: dict[int, Priority] = {rank: name for name, rank in RANKS.items()}


class Origin(StrEnum):
    """How the current project was chosen."""

    ARGUMENT = "argument"
    DIRECTORY = "directory"
    CREATED = "created"
    #: A computer with no project chosen: nothing named, no directory here.
    NONE = "none"


class Actor(BaseModel):
    principal: str
    computer: str


class Attributed(BaseModel):
    created_by: Actor | None = None
    updated_by: Actor | None = None


class ProjectRootOut(BaseModel):
    computer: str = Field(description="Computer this binding belongs to.")
    root: str = Field(description="Root directory; empty when the project has no files here.")


class ProjectOut(Attributed):
    """A project with its task counts."""

    id: int
    slug: str
    full_name: str = Field(description="Stable owner/slug project reference.")
    name: str
    owner: str
    role: str
    roots: list[ProjectRootOut] = Field(default_factory=list)
    here: bool = Field(
        description=("True when this computer has a non-empty filesystem binding for the project.")
    )
    description: str = ""
    preferred_language: str | None = Field(
        default=None,
        description="Language to use for project work and task text; null means no preference.",
    )
    archived: bool = False
    open_tasks: int = 0
    total_tasks: int = 0
    created_at: float
    updated_at: float


class CommentOut(BaseModel):
    principal: str
    computer: str
    id: int = Field(description="Stable comment id.")
    task_id: int
    project: str
    body: str
    kind: Literal["comment", "status", "move"] = "comment"
    created_at: float


class Comments(BaseModel):
    items: list[CommentOut]
    task_id: int
    count: int = Field(description="Total number of comments and history entries.")


class TaskOut(Attributed):
    id: int
    project: str = Field(description="Owner-qualified name of the owning project.")
    title: str
    body: str = ""
    status: Status
    priority: Priority
    tags: list[str] = Field(default_factory=list)
    created_at: float
    updated_at: float
    closed_at: float | None = None
    snippet: str | None = Field(default=None, description="Matched text, search results only.")


class TaskDetails(TaskOut):
    comments: list[CommentOut] = Field(default_factory=list)


class TaskListItem(Attributed):
    """Compact task metadata for listings; retrieve the task for its full body."""

    id: int
    project: str = Field(description="Owner-qualified name of the owning project.")
    title: str
    status: Status
    priority: Priority
    tags: list[str] = Field(default_factory=list)
    created_at: float
    updated_at: float
    closed_at: float | None = None
    snippet: str = Field(
        description="First meaningful body paragraph, shortened to 150 characters."
    )


class NoteOut(Attributed):
    """A piece of knowledge about a project: a titled markdown text, addressed by id."""

    id: int
    project: str = Field(description="Owner-qualified name of the owning project.")
    title: str
    body: str = ""
    tags: list[str] = Field(default_factory=list)
    shared: bool = Field(
        default=False,
        description="Visible across accessible projects; source-project membership is required.",
    )
    created_at: float
    updated_at: float
    snippet: str | None = Field(default=None, description="Matched text, search results only.")


class NoteListItem(Attributed):
    """Compact note metadata for listings; retrieve the note for its full body."""

    id: int
    project: str = Field(description="Owner-qualified name of the owning project.")
    title: str
    tags: list[str] = Field(default_factory=list)
    shared: bool = Field(
        default=False,
        description="Visible across accessible projects; source-project membership is required.",
    )
    created_at: float
    updated_at: float
    snippet: str = Field(
        description="First meaningful body paragraph, shortened to 150 characters."
    )


class Projects(BaseModel):
    items: list[ProjectOut]
    count: int


#: The `computer` of a listing: the project's, or the one named, or nothing for all of them.
COVERED = "Computer of the project or computer covered, absent when every computer."


class Tasks(BaseModel):
    items: list[TaskListItem]
    count: int
    project: str | None = Field(
        default=None, description="Owner-qualified project searched, absent when global."
    )
    computer: str | None = Field(default=None, description=COVERED)


class Notes(BaseModel):
    items: list[NoteListItem]
    count: int
    project: str | None = Field(
        default=None, description="Owner-qualified project searched, absent when global."
    )
    computer: str | None = Field(default=None, description=COVERED)


class FullTasks(BaseModel):
    """Task listing with complete task bodies."""

    items: list[TaskOut]
    count: int
    project: str | None = Field(
        default=None, description="Owner-qualified project searched, absent when global."
    )
    computer: str | None = Field(default=None, description=COVERED)


class FullNotes(BaseModel):
    """Note listing with complete note bodies."""

    items: list[NoteOut]
    count: int
    project: str | None = Field(
        default=None, description="Owner-qualified project searched, absent when global."
    )
    computer: str | None = Field(default=None, description=COVERED)


class Scope(BaseModel):
    principal: str = '["local"]'
    """The project the tools work on when no project is named, and the computer this is."""

    project: ProjectOut | None = Field(
        description="The current project; null when this computer has none chosen."
    )
    origin: Origin = Field(
        description="How the project was chosen; `none` when there is no project."
    )
    computer: str = Field(description="Computer this session works from.")
    computers: list[str] = Field(
        default_factory=list, description="Every computer with a registered project."
    )
    cwd: str
    database: str
    state: str
    version: str = Field(description="Version of the backlog-mcp package that answers.")
    commit: str | None = Field(
        default=None,
        description=(
            "Short git commit of the checkout the package runs from; null for an installed "
            "package without one."
        ),
    )


class Stats(BaseModel):
    project: str | None = None
    computer: str | None = Field(default=None, description=COVERED)
    this_computer: str = Field(description="Computer this session works from.")
    total: int = 0
    open: int = Field(default=0, description="Tasks with status todo, doing, or blocked.")
    closed: int = Field(default=0, description="Tasks with status done or cancelled.")
    by_status: dict[str, int] = Field(default_factory=dict)
    by_priority: dict[str, int] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    memory: int = Field(default=0, description="Notes in scope, including shared notes.")


class Deleted(BaseModel):
    id: int
    title: str
    deleted: bool
    detail: str = ""


class ProjectDeleted(BaseModel):
    """What `project_delete` found on the board, and whether the project is gone."""

    id: int
    slug: str
    full_name: str
    name: str
    tasks: int = Field(description="Tasks the project held, deleted ones included.")
    memory: int = Field(description="Notes the project held, shared ones included.")
    shared_notes: int = Field(description="Notes among them that every project could see.")
    deleted: bool
    detail: str = ""


__all__ = [
    "CLOSED_STATUSES",
    "OPEN_STATUSES",
    "PRIORITIES",
    "RANKS",
    "Deleted",
    "CommentOut",
    "Comments",
    "TaskDetails",
    "NoteOut",
    "Notes",
    "Origin",
    "Priority",
    "ProjectDeleted",
    "ProjectOut",
    "ProjectRootOut",
    "Projects",
    "Scope",
    "Stats",
    "Status",
    "TaskOut",
    "Tasks",
]


EVENT_KEY = "backlog/event"


class Kind(StrEnum):
    TASK = "task"
    NOTE = "memory"
    PROJECT = "project"


class Action(StrEnum):
    ADDED = "added"
    UPDATED = "updated"
    MOVED = "moved"
    DELETED = "deleted"


class Event(BaseModel):
    """What a write did.

    Travels in `_meta["backlog/event"]` of each `notifications/resources/updated`.
    """

    principal: str | None = None
    project_id: int | None = None
    kind: Kind
    action: Action
    id: int
    project: str = Field(description="Owner-qualified project name after the write.")
    title: str
    status: Status | None = Field(default=None, description="Tasks only.")
    priority: Priority | None = Field(default=None, description="Tasks only.")
    computer: str = Field(description="Computer the writing client works from.")
