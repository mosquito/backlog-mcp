#!/usr/bin/env python3
"""Add task links to an existing 0.2 database without changing its records.

Stop the server, then run: uv run python scripts/add_task_links.py DATABASE
The operation is transactional and safe to repeat. It does not import 0.1 databases.
"""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from backlog_mcp import store


def upgrade(path: Path) -> bool:
    """Return whether the schema changed. Refuse unknown or conflicting schemas."""
    with (
        closing(sqlite3.connect(":memory:")) as expected,
        closing(sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True)) as connection,
    ):
        expected.executescript(Path(store.__file__).with_name("schema.sql").read_text())
        connection.execute("PRAGMA foreign_keys=ON")
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("PRAGMA application_id").fetchone()[0] != 1112296275:
                raise ValueError("expected a backlog 0.2 database")
            for table in ("principals", "projects", "project_members", "tasks", "task_comments"):
                if (
                    connection.execute(f"PRAGMA table_info({table})").fetchall()
                    != expected.execute(f"PRAGMA table_info({table})").fetchall()
                ):
                    raise ValueError(f"unsupported {table} schema")
            changed = False
            for name in ("task_links", "links_by_target"):
                sql = expected.execute(
                    "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
                ).fetchone()[0]
                existing = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
                ).fetchone()
                if existing:
                    if " ".join(existing[0].split()) != " ".join(sql.split()):
                        raise ValueError(f"conflicting {name} schema")
                else:
                    connection.execute(sql)
                    changed = True
            if connection.execute("PRAGMA foreign_key_check(task_links)").fetchone():
                raise ValueError("task links contain invalid references")
        return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    try:
        changed = upgrade(args.database)
    except (ValueError, sqlite3.Error) as error:
        parser.exit(1, f"{error}\n")
    print("Task links added." if changed else "Task links already present.")


if __name__ == "__main__":
    main()
