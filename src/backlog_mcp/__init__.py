"""backlog-mcp: a project backlog on the command line and over MCP, on stdio or over HTTP.

Tasks belong to projects, and a project is a directory tree. The working directory therefore
decides which backlog a command works on, with nothing to pass. `backlog mcp` serves the same
data over MCP: on stdio by default, or over HTTP behind Basic auth with `--protocol=http`.
"""

from __future__ import annotations

from .backlog import Backlog, UnknownProject
from .cli import CLI, build, main, server_main
from .database import Database
from .models import Priority, Status
from .server import VERSION, registry, use
from .state import State

__all__ = [
    "CLI",
    "VERSION",
    "Backlog",
    "Priority",
    "State",
    "Status",
    "Database",
    "UnknownProject",
    "build",
    "main",
    "registry",
    "server_main",
    "use",
]
