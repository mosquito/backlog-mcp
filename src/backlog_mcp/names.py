"""Stable project owner names, separate from display names and filesystem paths."""

import os
from urllib.parse import quote, unquote


def owner_name(identity: str) -> str:
    """Escape usernames for unambiguous owner/project names."""
    handle = quote(identity, safe="")
    if handle.startswith((".", "~")):
        handle = f"%{ord(handle[0]):02X}" + handle[1:]
    return handle


def project_name(identity: str, slug: str) -> str:
    return f"{owner_name(identity)}/{slug}"


def default_user(value: str | None = None) -> str:
    """Resolve the default username for transports without an authorizer."""
    username = value if value is not None else os.environ.get("USER", "")
    if not username.strip() or username != username.strip() or ":" in username:
        raise ValueError("set --default-user or a valid USER environment variable")
    return username


def principal_label(user: str, computer: str | None = None) -> str:
    """Display an account with its computer context; escape separators in either part."""
    return f"{quote(user, safe='')}@{quote(computer or 'unknown', safe='')}"


def principal_user(value: str) -> str:
    """Accept a tool's user@computer label or a plain username without an @ sign."""
    user, separator, _computer = value.rpartition("@")
    return unquote(user) if separator else value
