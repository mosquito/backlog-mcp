import asyncio
import logging
import sqlite3
import threading
from hashlib import sha256
from pathlib import Path

import aiosqlite
import pytest
from helpers import rows

from backlog_mcp import migrate as migration
from backlog_mcp.backlog import Backlog
from backlog_mcp.migrate import APPLICATION_ID, MIGRATIONS, migrate


async def test_fresh_schema_and_repeat() -> None:
    async with aiosqlite.connect(":memory:") as connection:
        await migrate(connection)
        before = await connection.execute_fetchall("SELECT * FROM migrations")
        assert before[0][0] == "0001-initial.sql"
        assert before[0][1] == sha256((MIGRATIONS / "0001-initial.sql").read_bytes()).hexdigest()
        assert await connection.execute_fetchall("PRAGMA application_id") == [(APPLICATION_ID,)]
        assert await connection.execute_fetchall("SELECT * FROM task_links") == []
        await migrate(connection)
        assert await connection.execute_fetchall("SELECT * FROM migrations") == before


async def test_memory_migration_preserves_access_data_ids_and_search(tmp_path: Path) -> None:
    for path in MIGRATIONS.glob("000[123]-*.sql"):
        (tmp_path / path.name).write_bytes(path.read_bytes())
    async with aiosqlite.connect(":memory:") as connection:
        await connection.execute("PRAGMA foreign_keys=ON")
        await migrate(connection, tmp_path)
        await connection.execute("INSERT INTO principals VALUES ('alice', 'alice', 'Alice')")
        await connection.execute(
            "INSERT INTO projects(id, owner, slug, name, created_at, updated_at, "
            "created_principal, "
            "created_computer, updated_principal, updated_computer) "
            "VALUES (1, 'alice', 'project', 'Project', 1, 2, 'alice', 'here', 'alice', 'here')"
        )
        for note_id, shared in ((1, 0), (2, 1), (100, 0)):
            await connection.execute(
                "INSERT INTO notes VALUES (?, 1, ?, 'needle body', 'tag', ?, 1, 2, "
                "'alice', 'here', 'alice', 'here')",
                (note_id, f"Note {note_id}", shared),
            )
        await connection.execute("DELETE FROM notes WHERE id=100")
        await connection.commit()
        expected = await connection.execute_fetchall("SELECT * FROM notes ORDER BY id")
        path = MIGRATIONS / "0004-memory-visibility.sql"
        (tmp_path / path.name).write_bytes(path.read_bytes())
        await migrate(connection, tmp_path)
        actual = await connection.execute_fetchall("SELECT * FROM notes ORDER BY id")
        assert actual == [(*row[:5], 1, 0, *row[6:]) for row in expected]
        assert await connection.execute_fetchall("PRAGMA foreign_key_check") == []
        assert await connection.execute_fetchall(
            "SELECT rowid FROM notes_fts WHERE notes_fts MATCH 'needle' ORDER BY rowid"
        ) == [(1,), (2,)]
        cursor = await connection.execute(
            "INSERT INTO notes(project_id, title, body, created_at, updated_at, "
            "created_principal, created_computer, updated_principal, updated_computer) "
            "VALUES (1, 'Personal', 'after', 3, 3, 'alice', 'here', 'alice', 'here')"
        )
        assert cursor.lastrowid > 100
        await connection.execute("UPDATE notes SET body='changed' WHERE title='Personal'")
        assert (
            len(
                await connection.execute_fetchall(
                    "SELECT rowid FROM notes_fts WHERE notes_fts MATCH 'changed'"
                )
            )
            == 1
        )
        await connection.execute("DELETE FROM notes WHERE title='Personal'")
        assert (
            await connection.execute_fetchall(
                "SELECT rowid FROM notes_fts WHERE notes_fts MATCH 'changed'"
            )
            == []
        )


async def test_order_triggers_quoted_semicolons_and_apply_once(tmp_path: Path) -> None:
    (tmp_path / "0002-change.sql").write_text(
        "ALTER TABLE sample ADD COLUMN label TEXT;\n"
        "CREATE TRIGGER copy AFTER INSERT ON sample BEGIN\n"
        " UPDATE sample SET label = 'one;two' WHERE id = new.id;\nEND;\n"
        "INSERT INTO sample(id) VALUES (1);"
    )
    (tmp_path / "0001-initial.sql").write_text(
        f"PRAGMA application_id = {APPLICATION_ID}; CREATE TABLE sample(id INTEGER);"
    )
    async with aiosqlite.connect(":memory:") as connection:
        await migrate(connection, tmp_path)
        assert await connection.execute_fetchall("SELECT * FROM sample") == [(1, "one;two")]
        await migrate(connection, tmp_path)
        assert await connection.execute_fetchall("SELECT COUNT(*) FROM sample") == [(1,)]
        (tmp_path / "0003-next.sql").write_text("INSERT INTO sample(id) VALUES (2);")
        await migrate(connection, tmp_path)
        assert await connection.execute_fetchall("SELECT COUNT(*) FROM sample") == [(2,)]
        assert await connection.execute_fetchall(
            "SELECT filename FROM migrations ORDER BY filename"
        ) == [("0001-initial.sql",), ("0002-change.sql",), ("0003-next.sql",)]


async def test_failed_migration_rolls_back_sql_and_record(tmp_path: Path) -> None:
    (tmp_path / "0001-initial.sql").write_bytes((MIGRATIONS / "0001-initial.sql").read_bytes())
    async with aiosqlite.connect(":memory:") as connection:
        await migrate(connection, tmp_path)
        before = await connection.execute_fetchall("SELECT * FROM migrations")
        migration = tmp_path / "0002-failure.sql"
        migration.write_text("CREATE TABLE sample(id INTEGER); INSERT INTO missing VALUES (1);")
        with pytest.raises(sqlite3.OperationalError):
            await migrate(connection, tmp_path)
        assert await connection.execute_fetchall("SELECT * FROM migrations") == before
        assert (
            await connection.execute_fetchall("SELECT 1 FROM sqlite_master WHERE name = 'sample'")
            == []
        )
        migration.write_text("CREATE TABLE sample(id INTEGER);")
        await migrate(connection, tmp_path)
        assert await connection.execute_fetchall("SELECT COUNT(*) FROM migrations") == [(2,)]


async def test_unsupported_database_is_not_modified() -> None:
    async with aiosqlite.connect(":memory:") as connection:
        await connection.execute("CREATE TABLE tasks(id INTEGER)")
        await connection.execute("INSERT INTO tasks VALUES (42)")
        await connection.commit()
        with pytest.raises(ValueError, match="Unsupported database"):
            await migrate(connection)
        assert await connection.execute_fetchall("SELECT * FROM tasks") == [(42,)]
        assert (
            await connection.execute_fetchall(
                "SELECT 1 FROM sqlite_master WHERE name = 'migrations'"
            )
            == []
        )


@pytest.mark.parametrize("change", ["edit", "delete"])
async def test_applied_files_must_keep_names_and_checksums(tmp_path: Path, change: str) -> None:
    initial = tmp_path / "0001-initial.sql"
    initial.write_bytes((MIGRATIONS / initial.name).read_bytes())
    async with aiosqlite.connect(":memory:") as connection:
        await migrate(connection, tmp_path)
        before = await connection.execute_fetchall("SELECT * FROM migrations")
        (tmp_path / "0002-next.sql").write_text("CREATE TABLE sample(id INTEGER);")
        if change == "edit":
            initial.write_text(initial.read_text() + "\n-- changed\n")
        else:
            initial.unlink()
        with pytest.raises(ValueError, match="applied migration"):
            await migrate(connection, tmp_path)
        assert await connection.execute_fetchall("SELECT * FROM migrations") == before
        assert (
            await connection.execute_fetchall("SELECT 1 FROM sqlite_master WHERE name = 'sample'")
            == []
        )


async def test_duplicate_numbers_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "0001-first.sql").write_text("SELECT 1;")
    (tmp_path / "0001-second.sql").write_text("SELECT 2;")
    async with aiosqlite.connect(":memory:") as connection:
        with pytest.raises(ValueError, match="duplicate"):
            await migrate(connection, tmp_path)


async def test_memory_database_uses_same_connection() -> None:
    store = Backlog(Path(":memory:"))
    try:
        await store.open()
        assert len(await rows(await store.open(), "SELECT * FROM migrations")) == 4
        assert await rows(await store.open(), "SELECT * FROM task_links") == []
    finally:
        await store.close()


async def test_concurrent_startups(tmp_path: Path) -> None:
    stores = [Backlog(tmp_path / "shared.sqlite3") for _ in range(3)]
    try:
        await asyncio.gather(*(store.open() for store in stores))
        for store in stores:
            assert len(await rows(await store.open(), "SELECT * FROM migrations")) == 4
    finally:
        await asyncio.gather(*(store.close() for store in stores))


async def test_candidates_are_collected_on_a_worker_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_loop_thread = threading.get_ident()
    collect = migration.candidates
    seen: list[dict[Path, str]] = []

    def candidates(directory: Path) -> dict[Path, str]:
        assert threading.get_ident() != event_loop_thread
        result = collect(directory)
        seen.append(result)
        return result

    monkeypatch.setattr(migration, "candidates", candidates)
    async with aiosqlite.connect(":memory:") as connection:
        await migrate(connection)
    assert len(seen) == 1 and isinstance(seen[0], dict)
    assert set(seen[0]) == {
        MIGRATIONS / "0001-initial.sql",
        MIGRATIONS / "0002-task-assignment.sql",
        MIGRATIONS / "0003-task-edit-claims.sql",
        MIGRATIONS / "0004-memory-visibility.sql",
    }


async def test_migration_logs_include_commit_and_no_work(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="backlog_mcp.migrate")
    async with aiosqlite.connect(":memory:") as connection:
        await migrate(connection)
        assert "4 migrations needed" in caplog.messages
        assert "Applying migration 0001-initial.sql" in caplog.messages
        assert any(
            message.startswith("Applied migration 0001-initial.sql (sha256=")
            for message in caplog.messages
        )
        caplog.clear()
        await migrate(connection)
        assert caplog.messages == ["0 migrations needed"]


async def test_failed_batch_never_logs_applied_migrations(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="backlog_mcp.migrate")
    (tmp_path / "0001-initial.sql").write_bytes((MIGRATIONS / "0001-initial.sql").read_bytes())
    (tmp_path / "0002-failure.sql").write_text("INSERT INTO missing VALUES (1);")
    async with aiosqlite.connect(":memory:") as connection:
        with pytest.raises(sqlite3.OperationalError):
            await migrate(connection, tmp_path)
        assert "2 migrations needed" in caplog.messages
        assert "Applying migration 0001-initial.sql" in caplog.messages
        assert "Applying migration 0002-failure.sql" in caplog.messages
        assert any(
            "0002-failure.sql; transaction rolled back" in message for message in caplog.messages
        )
        assert "Migration transaction rolled back" in caplog.messages
        assert not any(message.startswith("Applied migration") for message in caplog.messages)
        assert (
            await connection.execute_fetchall(
                "SELECT 1 FROM sqlite_master WHERE name = 'migrations'"
            )
            == []
        )
