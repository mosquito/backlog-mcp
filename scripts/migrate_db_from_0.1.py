#!/usr/bin/env python3
"""Copy a single-user 0.1 database into a fresh 0.2 database.

Run with the 0.2 checkout: uv run python scripts/migrate_db_from_0.1.py \
old.sqlite3 new.sqlite3 --user alice

The source is read-only. Dated UPDATE blocks become separate comments.
Existing comments retain their IDs. Sessions and authentication state are not copied.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backlog_mcp import store
from backlog_mcp.auth import user_identity
from backlog_mcp.names import owner_name

HEADING = re.compile(r"# UPDATE: (\d{4}-\d{2}-\d{2}(?:T| )\d{2}:\d{2}:\d{2}Z?)$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def split_history(body: str) -> tuple[str, list[tuple[str, float]], bool]:
    """Extract dated updates outside code fences. Keep ambiguous bodies intact.

    The old writer used UTC for dates without a timezone. The source file retains
    the exact original separators and date spelling.
    """
    lines = body.splitlines(keepends=True)
    boundaries: list[tuple[int, float]] = []
    fence = ""
    fence_size = 0
    ambiguous = False
    for index, line in enumerate(lines):
        text = line.rstrip("\r\n")
        if match := FENCE.match(text):
            marker, suffix = match.groups()
            if not fence:
                fence, fence_size = marker[0], len(marker)
            elif marker[0] == fence and len(marker) >= fence_size and not suffix.strip():
                fence = ""
            continue
        if fence or not text.startswith("# UPDATE:"):
            continue
        heading = HEADING.fullmatch(text)
        if index == 0 or lines[index - 1].rstrip("\r\n") != "---" or heading is None:
            ambiguous = True
            continue
        try:
            moment = datetime.fromisoformat(heading[1].replace("Z", "+00:00"))
            timestamp = moment.replace(tzinfo=UTC).timestamp()
        except ValueError:
            ambiguous = True
            continue
        boundaries.append((index - 1, timestamp))
    if ambiguous or not boundaries:
        return body, [], ambiguous
    entries = [
        (
            "".join(
                lines[
                    start + 2 : boundaries[index + 1][0]
                    if index + 1 < len(boundaries)
                    else len(lines)
                ]
            ),
            timestamp,
        )
        for index, (start, timestamp) in enumerate(boundaries)
    ]
    return "".join(lines[: boundaries[0][0]]), entries, False


def rows(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    # Table names are constants in this module, never command-line input.
    return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')]


def insert(connection: sqlite3.Connection, table: str, values: dict[str, Any]) -> None:
    columns = ", ".join(f'"{column}"' for column in values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})', tuple(values.values())
    )


def migrate(
    source: Path, destination: Path, *, user: str, computer: str = "unknown"
) -> dict[str, int]:
    """Publish a validated database only if the destination does not exist."""
    if not user or user != user.strip() or ":" in user:
        raise ValueError(
            "--user must be a non-empty Basic username without colons or surrounding spaces"
        )
    source = source.resolve(strict=True)
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("destination already exists; choose a new file")
    principal = user_identity(user)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        original.row_factory = sqlite3.Row
        original.execute("BEGIN")
        tables = {
            row[0] for row in original.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not {"projects", "tasks"} <= tables or "principals" in tables:
            raise ValueError("source must be a single-user 0.1 database")
        data = {
            table: rows(original, table)
            for table in ("projects", "tasks", "notes", "project_roots", "task_comments")
            if table in tables
        }
        # Preserve AUTOINCREMENT high-water marks, including IDs deleted before import.
        sequences = (
            dict(original.execute("SELECT name, seq FROM sqlite_sequence"))
            if "sqlite_sequence" in tables
            else {}
        )
    extracted: list[dict[str, Any]] = []
    for row in data["tasks"]:
        description, entries, ambiguous = split_history(row.get("body", ""))
        if ambiguous:
            print(
                f"Task #{row['id']}: ambiguous UPDATE history left in the description",
                file=sys.stderr,
            )
        row["body"] = description
        extracted.extend(
            {
                "task_id": row["id"],
                "body": body,
                "kind": "comment",
                "created_at": created_at,
                "principal": principal,
                "computer": computer,
            }
            for body, created_at in entries
        )
    descriptor, filename = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(filename)
    try:
        with closing(sqlite3.connect(temporary)) as target, target:
            target.execute("PRAGMA foreign_keys=ON")
            target.executescript(Path(store.__file__).with_name("schema.sql").read_text())
            insert(
                target,
                "principals",
                {"identity": principal, "handle": owner_name(principal), "name": user},
            )
            actor = {
                "created_principal": principal,
                "created_computer": computer,
                "updated_principal": principal,
                "updated_computer": computer,
            }
            fields = {
                "projects": (
                    "id",
                    "slug",
                    "name",
                    "description",
                    "preferred_language",
                    "archived",
                    "created_at",
                    "updated_at",
                ),
                "tasks": (
                    "id",
                    "project_id",
                    "title",
                    "body",
                    "status",
                    "priority",
                    "tags",
                    "created_at",
                    "updated_at",
                    "closed_at",
                ),
                "notes": (
                    "id",
                    "project_id",
                    "title",
                    "body",
                    "tags",
                    "shared",
                    "created_at",
                    "updated_at",
                ),
            }
            for table, columns in fields.items():
                for row in data.get(table, []):
                    values = {key: row[key] for key in columns if key in row}
                    values.update(actor)
                    if table == "projects":
                        values["owner"] = principal
                    insert(target, table, values)
                    if table == "projects":
                        insert(
                            target,
                            "project_members",
                            {"project_id": row["id"], "principal": principal, "role": "owner"},
                        )
            roots = data.get("project_roots")
            if roots is None:
                roots = [
                    {
                        "project_id": row["id"],
                        "computer": row.get("computer", computer),
                        "root": row["root"],
                    }
                    for row in data["projects"]
                    if "root" in row
                ]
            for row in roots:
                insert(
                    target,
                    "project_roots",
                    {
                        "project_id": row["project_id"],
                        "principal": principal,
                        "computer": row["computer"],
                        "root": row["root"],
                    },
                )
            for row in data.get("task_comments", []):
                kind = row.get("kind", "comment")
                insert(
                    target,
                    "task_comments",
                    {
                        "id": row["id"],
                        "task_id": row["task_id"],
                        "body": row["body"],
                        "kind": kind if kind in ("comment", "status", "move") else "comment",
                        "created_at": row["created_at"],
                        "principal": principal,
                        "computer": row.get("computer") or computer,
                    },
                )
            for table in (*fields, "task_comments"):
                if table in sequences:
                    target.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
                    target.execute(
                        "INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)",
                        (table, sequences[table]),
                    )
            for entry in extracted:
                insert(target, "task_comments", entry)
            for row in data["tasks"]:
                target.execute(
                    "UPDATE tasks SET updated_at=?, updated_computer=? WHERE id=?",
                    (row["updated_at"], computer, row["id"]),
                )
            if target.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("import contains invalid references")
            if target.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("import failed SQLite integrity check")
            for table in ("projects_fts", "tasks_fts", "notes_fts", "comments_fts"):
                target.execute(f"INSERT INTO {table}({table}, rank) VALUES ('integrity-check', 1)")
        # A hard link publishes the completed file atomically and cannot replace another file.
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "projects": len(data["projects"]),
        "tasks": len(data["tasks"]),
        "memory": len(data.get("notes", [])),
        "comments": len(data.get("task_comments", [])) + len(extracted),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--user",
        "--default-user",
        default=os.environ.get("USER", ""),
        help="Username that will own all imported projects (default: $USER)",
    )
    parser.add_argument(
        "--computer", default="unknown", help="Actor computer for old records without attribution"
    )
    args = parser.parse_args()
    try:
        counts = migrate(args.source, args.destination, user=args.user, computer=args.computer)
    except (OSError, sqlite3.Error, ValueError) as error:
        parser.exit(1, f"Import failed: {error}\n")
    print("Imported " + ", ".join(f"{count} {kind}" for kind, count in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
