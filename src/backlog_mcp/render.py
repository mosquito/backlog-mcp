"""Terminal output: tables, panels and colours for the command line."""

from __future__ import annotations

import sys
import time
from collections.abc import Sequence
from datetime import datetime

from rich.box import ROUNDED
from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .client import RemoteSkill
from .models import (
    Action,
    CommentOut,
    Event,
    NoteOut,
    Priority,
    ProjectBoard,
    ProjectOut,
    Stats,
    Status,
    TaskDetails,
    TaskOut,
)
from .names import principal_label
from .skills import Skill

STATUS_STYLE = {
    Status.TODO: "white",
    Status.DOING: "bold cyan",
    Status.BLOCKED: "bold red",
    Status.DONE: "green",
    Status.CANCELLED: "dim strike",
    Status.DELETED: "dim red strike",
}

PRIORITY_STYLE = {
    Priority.URGENT: "bold red",
    Priority.HIGH: "yellow",
    Priority.NORMAL: "default",
    Priority.LOW: "dim",
}

UNITS = ((86400.0, "d"), (3600.0, "h"), (60.0, "m"))


def terminal(*, stderr: bool = False) -> Console:
    """A console that colours a terminal only.

    Rich obeys `FORCE_COLOR` even on a pipe or a file. A stream that is not a TTY gets no
    control codes at all, so captured output stays plain text.
    """
    stream = sys.stderr if stderr else sys.stdout
    return Console(stderr=stderr, force_terminal=None if stream.isatty() else False)


console = terminal()
errors = terminal(stderr=True)


def age(moment: float) -> str:
    """How long ago, in one or two characters plus a unit."""
    passed = max(time.time() - moment, 0.0)
    for size, unit in UNITS:
        if passed >= size:
            return f"{int(passed // size)}{unit}"
    return "now"


def since(moment: float) -> str:
    """How long ago, as a phrase."""
    passed = age(moment)
    return "just now" if passed == "now" else f"{passed} ago"


def titled(title: str) -> Table:
    """An empty table wide enough for its own title, so the heading never wraps."""
    return Table(
        box=ROUNDED,
        title=title,
        title_justify="left",
        expand=False,
        min_width=min(len(title) + 4, console.width),
    )


def excerpt(snippet: str | None) -> str:
    """A body excerpt on one line, so a match never breaks the row apart."""
    return " ".join((snippet or "").split())


def status_text(status: Status) -> Text:
    return Text(status.value, style=STATUS_STYLE[status])


def priority_text(priority: Priority) -> Text:
    return Text(priority.value, style=PRIORITY_STYLE[priority])


def tasks_table(
    items: Sequence[TaskOut], *, title: str, wide: bool = False, computer: str | None = None
) -> Table:
    """One row per task. `wide` adds the project column, for listings across projects.

    `computer` is the one the command runs on: a column names the computer of the tasks that
    are on another one.
    """
    #: Search results carry an excerpt of the body; a plain listing has none, so no column.
    matched = any(item.snippet for item in items)
    table = titled(title)
    table.add_column("id", justify="right", style="bold")
    if wide:
        table.add_column("project", style="magenta")
    table.add_column("status")
    table.add_column("pri")
    table.add_column("title", overflow="fold")
    assigned = any(item.assigned is not None for item in items)
    if assigned:
        table.add_column("assigned", overflow="fold")
    if matched:
        table.add_column("match", overflow="fold", style="dim italic")
    table.add_column("tags", style="blue")
    table.add_column("age", justify="right", style="dim")
    for item in items:
        row = [str(item.id)]
        if wide:
            row.append(item.project)
        cells: list[str | Text] = [
            *row,
            status_text(item.status),
            priority_text(item.priority),
            Text(item.title),
        ]
        if assigned:
            cells.append(Text(item.assigned or "—"))
        if matched:
            cells.append(Text(excerpt(item.snippet)))
        cells.extend([", ".join(item.tags), age(item.updated_at)])
        table.add_row(*cells)
    return table


def notes_table(
    items: Sequence[NoteOut], *, title: str, wide: bool = False, computer: str | None = None
) -> Table:
    """One row per note. `wide` adds the project column, for listings that cross projects."""
    matched = any(item.snippet for item in items)
    shared = any(item.shared for item in items)
    table = titled(title)
    table.add_column("id", justify="right", style="bold")
    if wide:
        table.add_column("project", style="magenta")
    table.add_column("title", overflow="fold")
    if matched:
        table.add_column("match", overflow="fold", style="dim italic")
    if shared:
        table.add_column("shared")
    table.add_column("tags", style="blue")
    table.add_column("age", justify="right", style="dim")
    for item in items:
        cells: list[str | Text] = [str(item.id)]
        if wide:
            cells.append(item.project)
        cells.append(Text(item.title))
        if matched:
            cells.append(Text(excerpt(item.snippet)))
        if shared:
            cells.append(Text("yes", style="green") if item.shared else "")
        cells.extend([", ".join(item.tags), age(item.updated_at)])
        table.add_row(*cells)
    return table


def projects_table(items: Sequence[ProjectOut], *, computer: str | None = None) -> Table:
    """One row per logical project, with all its path bindings."""
    table = titled("projects")
    table.add_column("slug", style="bold magenta")
    table.add_column("open", justify="right")
    table.add_column("total", justify="right", style="dim")
    table.add_column("root", overflow="fold")
    table.add_column("name", overflow="fold", style="dim")
    table.add_column("language")
    for item in items:
        slug = Text(item.full_name, style="dim strike" if item.archived else "bold magenta")
        cells: list[str | Text] = [slug]
        root = Text(", ".join(root.root_text() for root in item.roots))
        cells.extend(
            [
                str(item.open_tasks),
                str(item.total_tasks),
                root,
                item.name,
                Text(item.preferred_language if item.preferred_language is not None else "—"),
            ]
        )
        table.add_row(*cells)
    return table


def skills_table(items: Sequence[Skill | RemoteSkill]) -> Table:
    """One row per skill: what it is called, what it is for, how many files it carries."""
    table = titled("skills")
    table.add_column("name", style="bold magenta")
    table.add_column("description", overflow="fold")
    table.add_column("files", justify="right", style="dim")
    for item in items:
        table.add_row(item.name, item.description, str(len(item.files)))
    return table


def fields(rows: Sequence[tuple[str, RenderableType]]) -> Table:
    """A two-column grid: the label dim on the left, the value on the right."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", justify="right", no_wrap=True)
    grid.add_column(overflow="fold")
    for label, value in rows:
        grid.add_row(label, value)
    return grid


def width() -> int:
    """Wide enough for a sentence, never wider than the terminal."""
    return min(console.width, 88)


def owner(item: TaskOut | NoteOut, computer: str | None) -> Text:
    """The logical project that owns a record."""
    return Text(item.project, style="magenta")


def task_panel(task: TaskOut, *, computer: str | None = None) -> Panel:
    """One task in full: the id and title name the panel, the Markdown body follows the fields."""
    rows: list[tuple[str, RenderableType]] = [
        ("status", status_text(task.status)),
        ("assigned", Text(task.assigned or "—")),
        ("priority", priority_text(task.priority)),
    ]
    if task.tags:
        rows.append(("tags", Text(", ".join(task.tags), style="blue")))
    rows.append(("created", Text(since(task.created_at), style="dim")))
    if task.updated_at > task.created_at:
        rows.append(("changed", Text(since(task.updated_at), style="dim")))
    if task.closed_at:
        rows.append(("closed", Text(since(task.closed_at), style="dim")))
    body = task.body.strip()
    parts: list[RenderableType] = [fields(rows), Rule(style="dim")]
    parts.append(Markdown(body) if body else Text("no description", style="dim italic"))
    if isinstance(task, TaskDetails) and task.links:
        parts.append(Rule(f"links ({len(task.links)})", style="dim"))
        for link in task.links:
            parts.append(Text(f"{link.relation} → #{link.task} {link.title} [{link.status}]"))
            parts.append(Text(link.project, style="magenta"))
            if link.reason:
                parts.append(Text(link.reason))
    if isinstance(task, TaskDetails) and task.comments:
        parts.append(Rule(f"comments and history ({len(task.comments)})", style="dim"))
        parts.extend(comment_panel(item) for item in task.comments)
    return Panel(
        Group(*parts),
        box=ROUNDED,
        title=Text.assemble((f"#{task.id}", "bold cyan"), " ", (task.title, "bold")),
        title_align="left",
        subtitle=owner(task, computer),
        subtitle_align="right",
        padding=(1, 2),
        width=width(),
    )


def comment_panel(comment: CommentOut) -> Panel:
    when = datetime.fromtimestamp(comment.created_at).astimezone().isoformat(timespec="seconds")
    return Panel(
        Markdown(comment.body) if comment.body.strip() else Text("empty comment", style="dim"),
        title=Text(f"{comment.kind} #{comment.id} · {when}"),
        title_align="left",
        box=ROUNDED,
        subtitle=Text(
            f"{principal_label(comment.principal, comment.computer)} · task #{comment.task_id}"
            f" · assigned: {comment.task_assigned or '—'}"
        ),
        width=width(),
    )


def comments_table(items: Sequence[CommentOut], *, title: str) -> Table:
    table = titled(title)
    for label in ("comment", "task", "project", "task_assigned", "kind", "match"):
        table.add_column(label, overflow="fold")
    for item in items:
        snippet = getattr(item, "snippet", None) or item.body[:200]
        table.add_row(
            str(item.id),
            str(item.task_id),
            Text(item.project),
            Text(item.task_assigned or "—"),
            item.kind,
            Text(snippet),
        )
    return table


def note_panel(item: NoteOut, *, computer: str | None = None) -> Panel:
    """One note in full: the id and title name the panel, the markdown body follows the fields."""
    rows: list[tuple[str, RenderableType]] = []
    if item.tags:
        rows.append(("tags", Text(", ".join(item.tags), style="blue")))
    if item.shared:
        rows.append(("shared", Text("yes, project members", style="green")))
    if item.global_scope:
        rows.append(("scope", Text("all projects", style="green")))
    rows.append(("created", Text(since(item.created_at), style="dim")))
    if item.updated_at > item.created_at:
        rows.append(("changed", Text(since(item.updated_at), style="dim")))
    body = item.body.strip()
    parts: list[RenderableType] = [fields(rows), Rule(style="dim")]
    parts.append(Markdown(body) if body else Text("no content", style="dim italic"))
    return Panel(
        Group(*parts),
        box=ROUNDED,
        title=Text.assemble((f"note #{item.id}", "bold cyan"), " ", (item.title, "bold")),
        title_align="left",
        subtitle=owner(item, computer),
        subtitle_align="right",
        padding=(1, 2),
        width=width(),
    )


def skill_panel(skill: Skill | RemoteSkill) -> Panel:
    """One skill in full: what it is for, what it is made of, then the instructions."""
    rows: list[tuple[str, RenderableType]] = [
        ("description", Text(skill.description)),
        ("uri", Text(skill.uri, style="dim")),
    ]
    for item in skill.references:
        rows.append(("cites", Text(f"{item.uri} ({item.size} bytes)", style="dim")))
    parts: list[RenderableType] = [
        fields(rows),
        Rule(style="dim"),
        Markdown(skill.instructions),
    ]
    return Panel(
        Group(*parts),
        box=ROUNDED,
        title=Text(skill.name, style="bold magenta"),
        title_align="left",
        subtitle=Text("instructions for an MCP client", style="dim"),
        subtitle_align="right",
        padding=(1, 2),
        width=width(),
    )


def stats_view(stats: Stats, *, where: str) -> Table:
    table = titled(f"backlog of {where}")
    table.add_column("bucket", style="bold")
    table.add_column("count", justify="right")
    table.add_row(Text("open tasks", style="bold"), str(stats.open))
    table.add_row(Text("closed tasks", style="bold"), str(stats.closed))
    table.add_row(Text("total tasks", style="bold"), str(stats.total))
    table.add_row(Text("memory", style="bold"), str(stats.memory))
    table.add_section()
    for status in Status:
        table.add_row(status_text(status), str(stats.by_status.get(status.value, 0)))
    table.add_section()
    for priority in Priority:
        table.add_row(priority_text(priority), str(stats.by_priority.get(priority.value, 0)))
    table.add_section()
    if stats.tags:
        table.add_row(Text("tags", style="blue"), Text(", ".join(stats.tags), style="blue"))
    table.add_row(
        Text("principal", style="dim"),
        Text(principal_label(stats.principal, stats.this_computer), style="yellow"),
    )
    return table


def board_panel(board: ProjectBoard) -> Panel:
    project = board.project
    rows: list[tuple[str, RenderableType]] = [
        ("project", Text(project.full_name, style="bold magenta")),
        ("principal", Text(principal_label(board.principal, board.computer))),
        ("role", Text(project.role)),
        ("roots", Text(", ".join(root.root_text() for root in project.roots))),
        ("preferred language", Text(project.preferred_language or "none")),
        ("tasks", Text(f"{board.tasks.open} open of {board.tasks.total}")),
        ("claimed", Text(str(board.tasks.claimed))),
        ("unclaimed", Text(str(board.tasks.unclaimed))),
        ("blocked", Text(str(board.tasks.blocked))),
        ("done", Text(str(board.tasks.done))),
        ("cancelled", Text(str(board.tasks.cancelled))),
        ("deleted", Text(str(board.tasks.deleted))),
        ("memory", Text(str(board.memory))),
    ]
    if project.archived:
        rows.append(("archived", Text("yes")))
    parts: list[RenderableType] = [fields(rows)]
    if project.description:
        parts.append(Markdown(project.description))
    for name, section in (("ready", board.ready), ("claimed", board.claimed)):
        tasks = Table("id", "title", title=f"{name} · {section.count} total · top 10")
        for task in section.items:
            tasks.add_row(str(task.id), Text(task.title))
        parts.append(tasks)
    return Panel(Group(*parts), title=Text(project.name), width=width(), padding=(1, 2))


ACTION_STYLES = {
    Action.ADDED: "green",
    Action.UPDATED: "cyan",
    Action.MOVED: "yellow",
    Action.DELETED: "red",
}
STATUS_STYLES = {
    Status.DONE: "green",
    Status.DOING: "cyan",
    Status.BLOCKED: "red",
    Status.CANCELLED: "dim",
    Status.DELETED: "dim",
}


def event_line(event: Event, *, at: datetime | None = None) -> Text:
    """One event of the backlog as `backlog watch` prints it, stamped with the local time."""
    when = (at or datetime.now()).strftime("%H:%M:%S")
    line = Text(f"{when} ", style="dim")
    line.append(f"{event.kind.value} #{event.id} ", style="bold")
    line.append(event.action.value, style=ACTION_STYLES.get(event.action, ""))
    if event.status is not None:
        line.append(f" {event.status.value}", style=STATUS_STYLES.get(event.status, ""))
    line.append(f" {event.title!r} ")
    line.append(f"[{event.project}] ", style="magenta")
    line.append(f"from {principal_label(event.principal, event.computer)}", style="dim")
    return line


def note(message: str) -> None:
    console.print(Text(message, style="green"))


def warn(message: str) -> None:
    errors.print(Text(message, style="yellow"))


def fail(message: str) -> None:
    errors.print(Text(message, style="bold red"))


__all__ = [
    "age",
    "event_line",
    "excerpt",
    "titled",
    "fields",
    "since",
    "console",
    "errors",
    "fail",
    "note",
    "note_panel",
    "notes_table",
    "projects_table",
    "board_panel",
    "skill_panel",
    "skills_table",
    "stats_view",
    "task_panel",
    "tasks_table",
    "warn",
]
