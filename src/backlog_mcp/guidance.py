"""Packaged Markdown shared by the guide tool, resources and Web UI."""

from importlib.resources import files

TOPICS = ("workflow", "tasks", "memory", "search", "projects")


def read(topic: str | None = None) -> str:
    if topic is not None and topic not in TOPICS:
        raise ValueError(f"Unknown guide topic {topic!r}. Topics: {', '.join(TOPICS)}")
    return files("backlog_mcp").joinpath("guide", f"{topic or 'index'}.md").read_text()
