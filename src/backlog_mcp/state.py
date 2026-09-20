"""The state file: the name of this computer."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from .computer import check, hostname

log = logging.getLogger(__name__)

VERSION = 2

#: Keys of older versions. They selected a project; nothing in the file selects one now.
LEGACY_KEYS = ("pins", "last")


class State:
    """A small JSON file, written whole and replaced atomically.

    It holds the name of this computer, not a cache: the database stays the only source of
    truth about projects, and nothing in the file chooses one.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def read(self) -> dict[str, Any]:
        try:
            loaded = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return {"version": VERSION}
        except (OSError, ValueError) as error:
            log.warning("ignoring unreadable state %s: %s", self.path, error)
            return {"version": VERSION}
        if not isinstance(loaded, dict):
            log.warning("ignoring state %s: expected an object", self.path)
            return {"version": VERSION}
        return loaded

    def write(self, values: dict[str, Any]) -> None:
        """Replace the file in one step, so a reader never sees half a document."""
        for key in LEGACY_KEYS:
            values.pop(key, None)
        values["version"] = VERSION
        values["updated_at"] = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
        try:
            temporary.write_text(json.dumps(values, indent=2, sort_keys=True) + "\n", "utf-8")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def update(self, **changes: Any) -> dict[str, Any]:
        values = self.read()
        values.update(changes)
        self.write(values)
        return values

    @property
    def computer(self) -> str | None:
        """The name this computer was given on first use, if any."""
        value = self.read().get("computer")
        return value if isinstance(value, str) and value else None

    def computer_name(self, explicit: str | None = None) -> str:
        """The name of this computer: the one given, else the one remembered, else the host name.

        The host name is remembered on first use. A later change of the host name then does not
        move the projects to a new computer.
        """
        if explicit:
            return check(explicit)
        if remembered := self.computer:
            return remembered
        name = hostname()
        self.update(computer=name)
        return name


__all__ = ["LEGACY_KEYS", "VERSION", "State"]
