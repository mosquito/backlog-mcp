"""What the tools accept and return: the server's public shape."""

from __future__ import annotations

import re
from enum import StrEnum, unique
from typing import Annotated, Any, Literal
from urllib.parse import unquote

from pydantic import (
    AfterValidator,
    BaseModel,
    Field,
    PlainSerializer,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    WithJsonSchema,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)

from .names import principal_label

# Publish the json shape: whole seconds, not the stored float.
Seconds = Annotated[
    float,
    PlainSerializer(round, return_type=int, when_used="json"),
    WithJsonSchema({"type": "integer"}),
]

ASSIGNMENT_PATTERN = (
    r"^(?:[A-Za-z0-9_.~-]|%[0-9A-Fa-f]{2})+"
    r"@(?:[A-Za-z0-9_.~-]|%[0-9A-Fa-f]{2})+:[A-Za-z0-9_-]{1,64}$"
)


def check_assignment(value: str) -> str:
    """Validate a principal label followed by an agent's URL-safe marker."""
    if re.fullmatch(ASSIGNMENT_PATTERN, value) is None:
        raise ValueError(
            "assignment must be username@computer:marker; "
            "marker must contain 1–64 letters, digits, underscores or hyphens"
        )
    return value


Assignment = Annotated[str, Field(pattern=ASSIGNMENT_PATTERN), AfterValidator(check_assignment)]
# Keep explicit null values in the published schema on every supported MCP adapter.
NullableAssignment = Annotated[
    Assignment | None,
    WithJsonSchema({"type": ["string", "null"], "pattern": ASSIGNMENT_PATTERN}),
]
NullableString = Annotated[str | None, WithJsonSchema({"type": ["string", "null"]})]


class Status(StrEnum):
    """Where a task stands. Only `DONE`, `CANCELLED` and `DELETED` close it."""

    TODO = "todo"
    DOING = "doing"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"
    DELETED = "deleted"


@unique
class Relation(StrEnum):
    """A relation read from the source task toward the target task."""

    PART_OF = "part_of"
    CONTAINS = "contains"
    DEPENDS_ON = "depends_on"
    BLOCKS = "blocks"
    CAUSED_BY = "caused_by"
    CAUSES = "causes"
    RELATED_TO = "related_to"


RELATIONS: dict[Relation, Relation] = {
    Relation.PART_OF: Relation.CONTAINS,
    Relation.CONTAINS: Relation.PART_OF,
    Relation.DEPENDS_ON: Relation.BLOCKS,
    Relation.BLOCKS: Relation.DEPENDS_ON,
    Relation.CAUSED_BY: Relation.CAUSES,
    Relation.CAUSES: Relation.CAUSED_BY,
    Relation.RELATED_TO: Relation.RELATED_TO,
}


def relation(value: str | Relation) -> tuple[Relation, Relation]:
    """Return the relation and its inverse."""
    if isinstance(value, str):
        value = Relation[value.upper()]
    return value, RELATIONS[value]


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


def principal_schema(schema: dict[str, Any]) -> None:
    properties = schema.get("properties", {})
    if "computers" in properties:
        properties["principals"] = {"type": "array", "items": {"type": "string"}}
    if "this_computer" in properties:
        properties["this_principal"] = {"type": "string"}
    for key in ("computer", "computers", "this_computer"):
        schema.get("properties", {}).pop(key, None)
        if key in schema.get("required", []):
            schema["required"].remove(key)


class Actor(BaseModel):
    model_config = {"json_schema_extra": principal_schema}

    principal: str
    computer: str | None = None

    @model_validator(mode="before")
    @classmethod
    def parse_principal(cls, value: Any) -> Any:
        if (
            isinstance(value, dict)
            and "computer" not in value
            and "@" in value.get("principal", "")
        ):
            value = dict(value)
            user, _, computer = value["principal"].rpartition("@")
            value["principal"] = unquote(user)
            value["computer"] = (
                None
                if computer == "unknown" and cls.model_fields["computer"].annotation is not str
                else unquote(computer)
            )
            if "principals" in value:
                value["computers"] = [
                    unquote(item.rpartition("@")[2]) for item in value["principals"]
                ]
            if "this_principal" in value:
                value["this_computer"] = unquote(value["this_principal"].rpartition("@")[2])
        return value

    @model_serializer(mode="wrap")
    # No return annotation: preserve the model schema for the wrapping serializer.
    def actor_output(  # type: ignore[no-untyped-def]
        self, handler: SerializerFunctionWrapHandler, info: SerializationInfo
    ):
        output: dict[str, Any] = handler(self)
        if info.mode == "json":
            output["principal"] = principal_label(self.principal, self.computer)
            output.pop("computer", None)
            if "computers" in output:
                output["principals"] = [
                    principal_label(self.principal, item) for item in output.pop("computers")
                ]
            if "this_computer" in output:
                output["this_principal"] = principal_label(
                    self.principal, output.pop("this_computer")
                )
        return output


class Identity(Actor):
    assigned_tasks: int = Field(
        default=0,
        description="Accessible tasks assigned to this user@computer, across all statuses.",
    )
    auth_type: Literal["basic", "jwt", "none"] = Field(
        description="Authentication used by this connection."
    )
    handle: str = Field(description="Owner handle used in qualified project names.")


# Publish the json shape: attribution serializes to a user@computer label.
ActorLabel = Annotated[
    Actor | None, WithJsonSchema({"anyOf": [{"type": "string"}, {"type": "null"}]})
]


class Attributed(BaseModel):
    created_by: ActorLabel = None
    updated_by: ActorLabel = None

    @field_serializer("created_by", "updated_by", when_used="json")
    def actor_text(self, value: Actor | None) -> str | None:
        return principal_label(value.principal, value.computer) if value is not None else None

    @field_validator("created_by", "updated_by", mode="before")
    @classmethod
    def parse_actor(cls, value: Any) -> Any:
        if isinstance(value, str):
            return Actor.model_validate({"principal": value})
        return value


class ProjectRootOut(BaseModel):
    principal: str = Field(description="User this binding belongs to.")
    computer: str = Field(description="Computer this binding belongs to.")
    root: str = Field(description="Root directory; empty when the project has no files here.")

    @model_serializer(when_used="json")
    def root_text(self) -> str:
        return f"{principal_label(self.principal, self.computer)}:{self.root}"

    @model_validator(mode="before")
    @classmethod
    def parse_root(cls, value: object) -> object:
        if isinstance(value, str):
            actor, separator, root = value.partition(":")
            principal, at, computer = actor.rpartition("@")
            if not separator or not at or not principal or not computer:
                raise ValueError("root must use user@computer:path format")
            return {"principal": unquote(principal), "computer": unquote(computer), "root": root}
        return value


# Publish the json shape: a binding serializes to user@computer:path text.
ProjectRoot = Annotated[ProjectRootOut, WithJsonSchema({"type": "string"})]


class ProjectOut(Attributed):
    """A project with its task counts."""

    id: int
    slug: str
    full_name: str = Field(description="Stable owner/slug project reference.")
    name: str
    owner: str
    role: str

    @field_serializer("owner", when_used="json")
    def owner_text(self, value: str) -> str:
        return principal_label(value)

    roots: list[ProjectRoot] = Field(default_factory=list)
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
    created_at: Seconds
    updated_at: Seconds


class CommentOut(Actor):
    id: int = Field(description="Stable comment id.")
    task_id: int
    task_assigned: NullableAssignment = Field(
        default=None,
        description="Current task assignment, not a historical snapshot.",
    )
    project: str
    body: str
    kind: Literal["comment", "status", "move"] = "comment"
    created_at: Seconds


class Comments(Actor):
    items: list[CommentOut]
    task_id: int
    count: int = Field(description="Total number of comments and history entries.")


class TaskOut(Attributed):
    id: int
    project: str = Field(description="Owner-qualified name of the owning project.")
    title: str
    body: str = ""
    status: Status
    assigned: NullableAssignment = Field(
        default=None, description="Current username@computer:marker, or null when unassigned."
    )
    priority: Priority
    tags: list[str] = Field(default_factory=list)
    created_at: Seconds
    updated_at: Seconds
    closed_at: Seconds | None = None
    snippet: str | None = Field(default=None, description="Matched text, search results only.")


class TaskLinkOut(BaseModel):
    task: int = Field(description="The linked task id.")
    project: str
    relation: Relation = Field(description="Relation from the requested task to this task.")
    title: str
    status: Status
    reason: str = ""


class LinkChange(BaseModel):
    task: int
    relation: Relation
    target: int
    changed: bool


class TaskDetails(TaskOut):
    comments: list[CommentOut] = Field(default_factory=list)
    links: list[TaskLinkOut] = Field(default_factory=list)


class TaskListItem(Attributed):
    """Compact task metadata for listings; retrieve the task for its full body."""

    id: int
    project: str = Field(description="Owner-qualified name of the owning project.")
    title: str
    status: Status
    assigned: NullableAssignment = None
    priority: Priority
    tags: list[str] = Field(default_factory=list)
    created_at: Seconds
    updated_at: Seconds
    closed_at: Seconds | None = None
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
    global_scope: bool = Field(
        default=False, description="Visible in all projects, to the same audience."
    )
    shared: bool = Field(
        default=False,
        description="True: project members can read it. False: only the entry author can read it.",
    )
    created_at: Seconds
    updated_at: Seconds
    snippet: str | None = Field(default=None, description="Matched text, search results only.")


class NoteListItem(Attributed):
    """Compact note metadata for listings; retrieve the note for its full body."""

    id: int
    project: str = Field(description="Owner-qualified name of the owning project.")
    title: str
    tags: list[str] = Field(default_factory=list)
    global_scope: bool = Field(
        default=False, description="Visible in all projects, to the same audience."
    )
    shared: bool = Field(
        default=False,
        description="True: project members can read it. False: only the entry author can read it.",
    )
    created_at: Seconds
    updated_at: Seconds
    snippet: str = Field(
        description="First meaningful body paragraph, shortened to 150 characters."
    )


class Projects(Actor):
    items: list[ProjectOut]
    count: int


class Tasks(Actor):
    items: list[TaskListItem]
    count: int
    project: str | None = Field(
        default=None, description="Owner-qualified project searched, absent when global."
    )


class Notes(Actor):
    items: list[NoteListItem]
    count: int
    project: str | None = Field(
        default=None, description="Owner-qualified project searched, absent when global."
    )


class FullTasks(Actor):
    """Task listing with complete task bodies."""

    items: list[TaskOut]
    count: int
    project: str | None = Field(
        default=None, description="Owner-qualified project searched, absent when global."
    )


class FullNotes(Actor):
    """Note listing with complete note bodies."""

    items: list[NoteOut]
    count: int
    project: str | None = Field(
        default=None, description="Owner-qualified project searched, absent when global."
    )


class BoardProject(BaseModel):
    id: int
    full_name: str
    name: str
    description: str
    preferred_language: str | None
    role: str
    archived: bool
    roots: list[ProjectRoot]


class BoardCounts(BaseModel):
    total: int = Field(description="All project tasks, including closed and deleted tasks.")
    open: int
    claimed: int = Field(description="Open tasks with an assignment.")
    unclaimed: int = Field(description="Open tasks without an assignment.")
    blocked: int = Field(description="Open tasks blocked by status or dependencies.")
    done: int
    cancelled: int
    deleted: int


class BoardTask(BaseModel):
    id: int
    title: str


class BoardSection(BaseModel):
    count: int
    items: list[BoardTask]


class ProjectBoard(Actor):
    project: BoardProject
    tasks: BoardCounts
    global_memory: list[NoteOut] = Field(
        default_factory=list, description="All visible global memory entries with full bodies."
    )
    memory: int = Field(
        description="Personal and project-shared memory, plus visible global entries."
    )
    ready: BoardSection = Field(description="Unassigned, unblocked tasks; up to ten by priority.")
    claimed: BoardSection = Field(
        description="Open tasks assigned to this principal and computer, across all markers."
    )


class Stats(Actor):
    project: str | None = None
    this_computer: str = Field(description="Computer this session works from.")
    total: int = 0
    open: int = Field(default=0, description="Tasks with status todo, doing, or blocked.")
    closed: int = Field(default=0, description="Tasks with status done or cancelled.")
    by_status: dict[str, int] = Field(default_factory=dict)
    by_priority: dict[str, int] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    memory: int = Field(default=0, description="Memory entries in scope, including shared entries.")


class Deleted(Actor):
    id: int
    title: str
    deleted: bool
    detail: str = ""


class ProjectDeleted(Actor):
    """What `project_delete` found on the board, and whether the project is gone."""

    id: int
    slug: str
    full_name: str
    name: str
    tasks: int = Field(description="Tasks the project held, deleted ones included.")
    memory: int = Field(description="Memory entries the project held, including shared entries.")
    shared_notes: int = Field(
        title="Shared memory", description="Shared memory entries the project held."
    )
    deleted: bool
    detail: str = ""


__all__ = [
    "RELATIONS",
    "Relation",
    "relation",
    "TaskLinkOut",
    "LinkChange",
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
    "ProjectRoot",
    "ProjectRootOut",
    "Projects",
    "ProjectBoard",
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


class Event(Actor):
    """What a write did.

    Travels in `_meta["backlog/event"]` of each `notifications/resources/updated`.
    """

    principal: str
    project_id: int | None = None
    kind: Kind
    action: Action
    id: int
    project: str = Field(description="Owner-qualified project name after the write.")
    title: str
    status: Status | None = Field(default=None, description="Tasks only.")
    priority: Priority | None = Field(default=None, description="Tasks only.")
    computer: str = Field(description="Computer the writing client works from.")
