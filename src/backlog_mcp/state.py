"""The state file: which project the tools work on when the directory does not say."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

VERSION = 1


class State:
    """A small JSON file, written whole and replaced atomically.

    It holds a pin, not a cache: the database stays the only source of truth about projects.
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
    def pinned(self) -> str | None:
        """The slug a user chose with `project_use`, if any."""
        value = self.read().get("pinned_project")
        return value if isinstance(value, str) and value else None

    def pin(self, slug: str | None) -> None:
        self.update(pinned_project=slug)

    @property
    def last_project(self) -> str | None:
        value = self.read().get("last_project")
        return value if isinstance(value, str) and value else None

    def remember(self, slug: str) -> None:
        """Record the project last worked on. Diagnostic only; it never changes resolution."""
        if self.last_project != slug:
            self.update(last_project=slug)


__all__ = ["VERSION", "State"]
