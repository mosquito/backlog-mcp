"""XDG base directories for the database and the config files.

Environment overrides are not read here. `argclass` binds `BACKLOG_MCP_*` variables to the
command-line arguments, so every override arrives through one path.
"""

from __future__ import annotations

import os
from pathlib import Path

APP = "backlog-mcp"

#: Read as TOML, lowest priority first. A missing file is skipped.
SYSTEM_CONFIG = Path("/etc") / APP / "config.toml"
LOCAL_CONFIG = Path(".") / f".{APP}.toml"


def base(variable: str, fallback: str) -> Path:
    """An XDG base directory. The specification says to ignore a relative value."""
    value = os.environ.get(variable, "")
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / fallback


def data_home() -> Path:
    return base("XDG_DATA_HOME", ".local/share")


def config_home() -> Path:
    return base("XDG_CONFIG_HOME", ".config")


def database_path() -> Path:
    """Where the tasks live."""
    return data_home() / APP / "backlog.sqlite3"


def config_files() -> list[Path]:
    """The config search list: system, then user, then the current directory."""
    return [SYSTEM_CONFIG, config_home() / APP / "config.toml", LOCAL_CONFIG]


__all__ = [
    "APP",
    "LOCAL_CONFIG",
    "SYSTEM_CONFIG",
    "config_files",
    "config_home",
    "data_home",
    "database_path",
]
