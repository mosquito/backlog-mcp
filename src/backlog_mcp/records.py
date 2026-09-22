"""Internal records, row conversion and input normalization."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

import aiosqlite

from .models import PRIORITIES, Actor, Priority, Status
from .names import project_name

LOCAL_PRINCIPAL = "local"

#: A note is a short fact, not a page; the body stops here, in characters.
NOTE_LIMIT = 1024


def clean_tags(tags: Iterable[str]) -> list[str]:
    """Tags are lower-case, unique and sorted, so listings and completion are stable."""
    return sorted({tag.strip().lower() for tag in tags if tag.strip()})


def join_tags(tags: Iterable[str]) -> str:
    return ",".join(clean_tags(tags))


def split_tags(text: str) -> list[str]:
    return [tag for tag in text.split(",") if tag]


def check_note(body: str) -> None:
    """Refuse a body longer than `NOTE_LIMIT`, before anything is written."""
    if len(body) > NOTE_LIMIT:
        raise ValueError(f"a note holds at most {NOTE_LIMIT} characters, this one has {len(body)}")


def match_query(text: str) -> str:
    """Turn user text into an FTS5 prefix query where every token must match.

    Quoting each token keeps FTS5 operators out of the query, so no input is a syntax error.
    """
    return " ".join(f'"{token}"*' for token in re.findall(r"\w+", text))


def assignment_pattern(prefix: str) -> str:
    """Escape a literal assignment prefix for LIKE."""
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class Unset(Enum):
    """An omitted update field; None explicitly clears a nullable value."""

    VALUE = 0


class AssignmentConflict(ValueError):
    """The current assignment did not match the caller's expected value."""

    def __init__(self, task_id: int, expected: str | None, assigned: str | None) -> None:
        self.assigned = assigned
        super().__init__(
            f"assignment conflict for task #{task_id}: expected {expected!r}, current {assigned!r}"
        )


@dataclass(slots=True)
class Comment:
    id: int
    task_id: int
    project_id: int
    project: str
    body: str
    kind: Literal["comment", "status", "move"]
    created_at: float
    task_assigned: str | None = None
    principal: str = LOCAL_PRINCIPAL
    computer: str = ""
    snippet: str | None = None


@dataclass(slots=True)
class Project:
    id: int
    slug: str
    name: str
    description: str
    archived: bool
    created_at: float
    updated_at: float
    total_tasks: int = 0
    open_tasks: int = 0
    roots: list[ProjectRoot] = field(default_factory=list)
    preferred_language: str | None = None
    owner: str = LOCAL_PRINCIPAL
    role: str = "owner"
    created_by: Actor | None = None
    updated_by: Actor | None = None

    @property
    def full_name(self) -> str:
        return project_name(self.owner, self.slug)


@dataclass(frozen=True, slots=True)
class ProjectRoot:
    computer: str
    root: str


@dataclass(frozen=True, slots=True)
class ProjectContents:
    """What a project holds, counted before it is deleted."""

    tasks: int
    notes: int
    shared_notes: int


@dataclass(slots=True)
class Task:
    id: int
    project_id: int
    project: str
    title: str
    body: str
    status: Status
    priority: Priority
    tags: list[str]
    created_at: float
    updated_at: float
    assigned: str | None = None
    closed_at: float | None = None
    snippet: str | None = None
    created_by: Actor | None = None
    updated_by: Actor | None = None


@dataclass(slots=True)
class Note:
    id: int
    project_id: int
    project: str
    title: str
    body: str
    tags: list[str]
    shared: bool
    created_at: float
    updated_at: float
    snippet: str | None = None
    created_by: Actor | None = None
    updated_by: Actor | None = None


def to_project(row: aiosqlite.Row) -> Project:
    keys = row.keys()
    return Project(
        id=row["id"],
        created_by=Actor(principal=row["created_principal"], computer=row["created_computer"]),
        updated_by=Actor(principal=row["updated_principal"], computer=row["updated_computer"]),
        slug=row["slug"],
        owner=row["owner"],
        name=row["name"],
        description=row["description"],
        preferred_language=row["preferred_language"],
        archived=bool(row["archived"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        total_tasks=row["total_tasks"] if "total_tasks" in keys else 0,
        open_tasks=row["open_tasks"] if "open_tasks" in keys else 0,
    )


def to_task(row: aiosqlite.Row) -> Task:
    keys = row.keys()
    return Task(
        id=row["id"],
        created_by=Actor(principal=row["created_principal"], computer=row["created_computer"]),
        updated_by=Actor(principal=row["updated_principal"], computer=row["updated_computer"]),
        project_id=row["project_id"],
        project=row["project"],
        title=row["title"],
        body=row["body"],
        status=Status(row["status"]),
        assigned=row["assigned"],
        priority=PRIORITIES.get(row["priority"], Priority.NORMAL),
        tags=split_tags(row["tags"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        closed_at=row["closed_at"],
        snippet=row["snippet"] if "snippet" in keys else None,
    )


def to_note(row: aiosqlite.Row) -> Note:
    keys = row.keys()
    return Note(
        id=row["id"],
        created_by=Actor(principal=row["created_principal"], computer=row["created_computer"]),
        updated_by=Actor(principal=row["updated_principal"], computer=row["updated_computer"]),
        project_id=row["project_id"],
        project=row["project"],
        title=row["title"],
        body=row["body"],
        tags=split_tags(row["tags"]),
        shared=bool(row["shared"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        snippet=row["snippet"] if "snippet" in keys else None,
    )
