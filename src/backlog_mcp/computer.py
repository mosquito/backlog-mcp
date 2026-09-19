"""The name of the computer a project lives on.

A project is a directory tree on one computer. The name tells the computers apart when one
database, synced or served over HTTP, holds the projects of several of them.
"""

from __future__ import annotations

import re
import socket

#: One word of letters, digits, dots, dashes and underscores: it travels in URLs and config files.
PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}\Z")


def check(name: str) -> str:
    """The name stripped, or `ValueError` when it is not one plain word."""
    cleaned = name.strip()
    if not PATTERN.match(cleaned):
        raise ValueError(
            f"invalid computer name {name!r}: use letters, digits, dots, dashes and underscores"
        )
    return cleaned


def hostname() -> str:
    """The short host name, made a valid computer name. Falls back to `localhost`."""
    short = socket.gethostname().split(".")[0]
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", short).strip("-._")
    return cleaned if PATTERN.match(cleaned) else "localhost"


__all__ = ["PATTERN", "check", "hostname"]
