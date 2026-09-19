"""Terminal output: tables, panels and colours for the command line."""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence

from rich.box import ROUNDED
from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .models import NoteOut, Priority, ProjectOut, Scope, Stats, Status, TaskOut
from .skills import Skill

STATUS_STYLE = {
    Status.TODO: "white",
    Status.DOING: "bold cyan",
    Status.BLOCKED: "bold red",
    Status.DONE: "green",
    Status.CANCELLED: "dim strike",
}

PRIORITY_STYLE = {
    Priority.URGENT: "bold red",
    Priority.HIGH: "yellow",
    Priority.NORMAL: "default",
    Priority.LOW: "dim",
}

UNITS = ((86400.0, "d"), (3600.0, "h"), (60.0, "m"))

console = Console()
errors = Console(stderr=True)


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


def elsewhere(items: Iterable[TaskOut | NoteOut | ProjectOut], computer: str | None) -> bool:
    """True when a record is on another computer than `computer`, so the column is needed."""
    return computer is not None and any(item.computer != computer for item in items)


def tasks_table(
    items: Sequence[TaskOut], *, title: str, wide: bool = False, computer: str | None = None
) -> Table:
    """One row per task. `wide` adds the project column, for listings across projects.

    `computer` is the one the command runs on: a column names the computer of the tasks that
    are on another one.
    """
    #: Search results carry an excerpt of the body; a plain listing has none, so no column.
    matched = any(item.snippet for item in items)
    away = elsewhere(items, computer)
    table = titled(title)
    table.add_column("id", justify="right", style="bold")
    if wide:
        table.add_column("project", style="magenta")
    if away:
        table.add_column("computer", style="yellow")
    table.add_column("status")
    table.add_column("pri")
    table.add_column("title", overflow="fold")
    if matched:
        table.add_column("match", overflow="fold", style="dim italic")
    table.add_column("tags", style="blue")
    table.add_column("age", justify="right", style="dim")
    for item in items:
        row = [str(item.id)]
        if wide:
            row.append(item.project)
        if away:
            row.append(item.computer if item.computer != computer else "")
        cells: list[str | Text] = [
            *row,
            status_text(item.status),
            priority_text(item.priority),
            Text(item.title),
        ]
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
    away = elsewhere(items, computer)
    table = titled(title)
    table.add_column("id", justify="right", style="bold")
    if wide:
        table.add_column("project", style="magenta")
    if away:
        table.add_column("computer", style="yellow")
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
        if away:
            cells.append(item.computer if item.computer != computer else "")
        cells.append(Text(item.title))
        if matched:
            cells.append(Text(excerpt(item.snippet)))
        if shared:
            cells.append(Text("yes", style="green") if item.shared else "")
        cells.extend([", ".join(item.tags), age(item.updated_at)])
        table.add_row(*cells)
    return table


def projects_table(items: Sequence[ProjectOut], *, computer: str | None = None) -> Table:
    """One row per project. A project on another computer names it, and its root is dim."""
    away = elsewhere(items, computer)
    table = titled("projects")
    table.add_column("slug", style="bold magenta")
    if away:
        table.add_column("computer", style="yellow")
    table.add_column("open", justify="right")
    table.add_column("total", justify="right", style="dim")
    table.add_column("root", overflow="fold")
    table.add_column("name", overflow="fold", style="dim")
    for item in items:
        slug = Text(item.slug, style="dim strike" if item.archived else "bold magenta")
        cells: list[str | Text] = [slug]
        if away:
            cells.append(item.computer if item.computer != computer else "")
        root = Text(item.root, style="dim" if computer and item.computer != computer else "")
        cells.extend([str(item.open_tasks), str(item.total_tasks), root, item.name])
        table.add_row(*cells)
    return table


def skills_table(items: Sequence[Skill]) -> Table:
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
    """The project of a record, and its computer when that is not the one given."""
    if computer is None or item.computer == computer:
        return Text(item.project, style="magenta")
    return Text.assemble((item.project, "magenta"), " on ", (item.computer, "yellow"))


def task_panel(task: TaskOut, *, computer: str | None = None) -> Panel:
    """One task in full: the id and title name the panel, the body follows the fields."""
    rows: list[tuple[str, RenderableType]] = [
        ("status", status_text(task.status)),
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
    parts.append(Text(body) if body else Text("no description", style="dim italic"))
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


def note_panel(item: NoteOut, *, computer: str | None = None) -> Panel:
    """One note in full: the id and title name the panel, the markdown body follows the fields."""
    rows: list[tuple[str, RenderableType]] = []
    if item.tags:
        rows.append(("tags", Text(", ".join(item.tags), style="blue")))
    if item.shared:
        rows.append(("shared", Text("yes, every project sees it", style="green")))
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


def skill_panel(skill: Skill) -> Panel:
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


def stats_view(stats: Stats) -> Table:
    if stats.project:
        where = stats.project
    elif stats.computer:
        where = f"every project on {stats.computer}"
    else:
        where = "every project on every computer"
    table = titled(f"backlog of {where}")
    table.add_column("bucket", style="bold")
    table.add_column("count", justify="right")
    table.add_row(Text("open tasks", style="bold"), str(stats.open))
    table.add_row(Text("closed tasks", style="bold"), str(stats.closed))
    table.add_row(Text("total tasks", style="bold"), str(stats.total))
    table.add_row(Text("notes", style="bold"), str(stats.notes))
    table.add_section()
    for status in Status:
        table.add_row(status_text(status), str(stats.by_status.get(status.value, 0)))
    table.add_section()
    for priority in Priority:
        table.add_row(priority_text(priority), str(stats.by_priority.get(priority.value, 0)))
    table.add_section()
    if stats.tags:
        table.add_row(Text("tags", style="blue"), Text(", ".join(stats.tags), style="blue"))
    table.add_row(Text("this computer", style="dim"), Text(stats.this_computer, style="yellow"))
    return table


def scope_panel(scope: Scope) -> Panel:
    """Where the commands work, and why there."""
    project = scope.project
    rows: list[tuple[str, RenderableType]] = []
    if project is None:
        rows.append(("project", Text("none chosen", style="dim italic")))
    else:
        rows.extend(
            [
                ("project", Text(project.slug, style="bold magenta")),
                ("chosen by", Text(scope.origin.value, style="dim")),
                ("root", Text(project.root)),
                ("tasks", Text(f"{project.open_tasks} open of {project.total_tasks}")),
            ]
        )
    rows.append(("computer", Text(scope.computer, style="yellow")))
    others = [name for name in scope.computers if name != scope.computer]
    if others:
        rows.append(("also known", Text(", ".join(others), style="dim")))
    rows.extend(
        [
            ("database", Text(scope.database, style="dim")),
            ("state", Text(scope.state, style="dim")),
        ]
    )
    return Panel(
        fields(rows),
        box=ROUNDED,
        title="current project",
        title_align="left",
        padding=(1, 2),
        width=width(),
    )


def note(message: str) -> None:
    console.print(Text(message, style="green"))


def warn(message: str) -> None:
    errors.print(Text(message, style="yellow"))


def fail(message: str) -> None:
    errors.print(Text(message, style="bold red"))


__all__ = [
    "age",
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
    "scope_panel",
    "skill_panel",
    "skills_table",
    "stats_view",
    "task_panel",
    "tasks_table",
    "warn",
]
