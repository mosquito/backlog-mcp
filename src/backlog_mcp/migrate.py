"""Apply numbered SQL files on the application database connection."""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from hashlib import sha256
from pathlib import Path

import aiosqlite

from .transaction import Transaction

# Migration setup and bookkeeping are inline SQL exceptions: the migrator must not
# depend on application queries.sql. Application queries belong in queries.sql;
# application schema changes belong in migrations/*.sql.
MIGRATIONS = Path(__file__).with_name("migrations")
APPLICATION_ID = 1112296275
log = logging.getLogger(__name__)


def candidates(directory: Path) -> dict[Path, str]:
    """Collect migration paths and SHA-256 hashes."""
    files: dict[Path, str] = {}
    versions: set[int] = set()
    for path in directory.glob("*.sql"):
        match = re.fullmatch(r"(\d{4})-[a-z0-9-]+\.sql", path.name)
        if match is None:
            raise ValueError(f"invalid migration filename: {path.name}")
        version = int(match[1])
        if version in versions:
            raise ValueError(f"duplicate migration number: {version:04d}")
        versions.add(version)
        files[path] = sha256(path.read_bytes()).hexdigest()
    return files


async def applied(connection: aiosqlite.Connection, directory: Path) -> dict[Path, str]:
    rows = await connection.execute_fetchall("SELECT filename, sha256 FROM migrations")
    return {directory / row[0]: row[1] for row in rows}


async def migrate(
    connection: aiosqlite.Connection,
    directory: Path = MIGRATIONS,
    *,
    transaction: Transaction | None = None,
) -> None:
    """Apply each pending file and record it in the same transaction.

    SQL files contain statements, without BEGIN/COMMIT. complete_statement preserves trigger
    bodies and quoted semicolons; executescript would implicitly commit an existing transaction.
    """
    current = "migration setup"
    try:
        async with transaction or Transaction(connection, asyncio.Lock()):
            async with connection.execute("PRAGMA application_id") as cursor:
                row = await cursor.fetchone()
            assert row is not None
            marker = row[0]
            if marker != APPLICATION_ID:
                tables = await connection.execute_fetchall(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'mcp_%'"
                )
                if marker or tables:
                    raise ValueError("Unsupported database schema. Import old data separately.")
            await connection.execute("""
                CREATE TABLE IF NOT EXISTS migrations (
                    filename TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL,
                    applied_at REAL NOT NULL
                )
            """)
            files, previous = await asyncio.gather(
                asyncio.to_thread(candidates, directory),
                applied(connection, directory),
            )
            missing = previous.keys() - files.keys()
            if missing:
                raise ValueError(f"applied migration is missing: {min(missing).name}")
            pending: set[tuple[Path, str]] = files.items() - previous.items()
            changed = {path for path, _ in pending} & previous.keys()
            if changed:
                raise ValueError(f"applied migration SHA-256 changed: {min(changed).name}")
            log.info("%d migrations needed", len(pending))
            for path, checksum in sorted(pending):
                current = path.name
                log.info("Applying migration %s", path.name)
                content = await asyncio.to_thread(path.read_bytes)
                if sha256(content).hexdigest() != checksum:
                    raise ValueError(f"migration changed after discovery: {path.name}")
                statement = ""
                for part in content.decode("utf-8").split(";"):
                    statement += part + ";"
                    if sqlite3.complete_statement(statement):
                        await connection.execute(statement)
                        statement = ""
                if statement.strip():
                    await connection.execute(statement)
                await connection.execute(
                    "INSERT INTO migrations(filename, sha256, applied_at) VALUES (?, ?, ?)",
                    (path.name, checksum, time.time()),
                )
            current = "migration commit"
    except BaseException:
        log.exception("Migration failed during %s; transaction rolled back", current)
        log.warning("Migration transaction rolled back")
        raise
    for path, checksum in sorted(pending):
        log.info("Applied migration %s (sha256=%s)", path.name, checksum)
