"""The SQLite layer: projects, tasks, notes and the full-text index behind them."""

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from backlog_mcp.models import Priority, Status
from backlog_mcp.store import (
    MIGRATIONS,
    NOTE_LIMIT,
    Project,
    ProjectContents,
    ProjectRoot,
    Store,
    clean_tags,
    match_query,
    split_tags,
)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    instance = Store(tmp_path / "backlog.sqlite3")
    await instance.open()
    try:
        yield instance
    finally:
        await instance.close()


async def register(store: Store, slug: str, root: Path, computer: str = "here") -> Project:
    return await store.add_project(slug=slug, name=slug, root=root, computer=computer)


def test_tag_helpers_normalise() -> None:
    assert clean_tags([" API ", "api", "", "docs"]) == ["api", "docs"]
    assert split_tags("api,docs") == ["api", "docs"]
    assert split_tags("") == []


def test_match_query_keeps_operators_out() -> None:
    assert match_query("auth serv") == '"auth"* "serv"*'
    assert match_query('" OR *') == '"OR"*'


async def test_reopening_keeps_the_schema_and_the_rows(tmp_path: Path) -> None:
    store = Store(tmp_path / "backlog.sqlite3")
    project = await register(store, "one", tmp_path / "one")
    await store.add_task(project_id=project.id, title="Survive a restart")
    await store.close()

    again = Store(tmp_path / "backlog.sqlite3")
    connection = await again.open()
    async with connection.execute("PRAGMA user_version") as cursor:
        row = await cursor.fetchone()
    assert row is not None and row[0] > 0
    assert (await again.count_tasks()) == 1
    await again.close()


async def test_project_language_persists_and_can_be_cleared(tmp_path: Path) -> None:
    path = tmp_path / "backlog.sqlite3"
    store = Store(path)
    project = await store.add_project(
        slug="one", name="One", root="", computer="here", preferred_language="ru"
    )
    assert project.preferred_language == "ru"
    await store.close()

    reopened = Store(path)
    try:
        project = await reopened.project(project.id)
        assert project is not None and project.preferred_language == "ru"
        updated = await reopened.update_project(project.id, description="Changed")
        assert updated is not None and updated.preferred_language == "ru"
        updated = await reopened.update_project(project.id, preferred_language="en")
        assert updated is not None and updated.preferred_language == "en"
        updated = await reopened.update_project(project.id, preferred_language=None)
        assert updated is not None and updated.preferred_language is None
    finally:
        await reopened.close()


async def test_existing_projects_gain_a_null_language(tmp_path: Path) -> None:
    path = tmp_path / "backlog.sqlite3"
    with sqlite3.connect(path) as connection:
        for migration in MIGRATIONS[:4]:
            for statement in migration:
                connection.execute(statement, {"computer": "here"})
        connection.execute("PRAGMA user_version=4")
        connection.execute(
            "INSERT INTO projects (slug, name, description, created_at, updated_at) "
            "VALUES ('old', 'Old project', 'Keep this', 1, 2)"
        )
        connection.execute("INSERT INTO project_roots VALUES (1, 'here', '/tmp/old')")
        connection.execute(
            "INSERT INTO tasks (project_id, title, created_at, updated_at) "
            "VALUES (1, 'Keep this task', 1, 2)"
        )
    store = Store(path)
    try:
        project = await store.project(1)
        assert project is not None
        assert project.preferred_language is None
        assert project.description == "Keep this"
        assert project.roots == [ProjectRoot("here", "/tmp/old")]
        assert (project.created_at, project.updated_at) == (1, 2)
        assert await store.count_tasks(project_id=1) == 1
        fresh = await register(store, "new", tmp_path / "new")
        assert fresh.preferred_language is None
    finally:
        await store.close()


async def test_project_for_path_takes_the_deepest_root(store: Store, tmp_path: Path) -> None:
    outer = await register(store, "outer", tmp_path)
    inner = await register(store, "inner", tmp_path / "inner")
    deep = tmp_path / "inner" / "deep"
    assert (await store.project_for_path(deep, computer="here")).id == inner.id
    assert (await store.project_for_path(tmp_path / "other", computer="here")).id == outer.id
    assert await store.project_for_path(Path("/nowhere"), computer="here") is None
    assert await store.project_for_path(deep, computer="elsewhere") is None
    assert await store.project_by_root(tmp_path / "inner", computer="here") == inner
    assert await store.project_by_root(tmp_path / "inner", computer="elsewhere") is None
    assert await store.project_by_slug("missing") is None
    assert await store.slugs() == ["inner", "outer"]


async def test_archiving_hides_a_project_from_the_default_listing(store: Store) -> None:
    project = await register(store, "one", Path("/tmp/one"))
    await store.update_project(project.id, archived=True, description="Retired")
    assert await store.projects() == []
    archived = await store.projects(archived=True)
    assert [item.slug for item in archived] == ["one"]
    assert archived[0].description == "Retired"


async def test_closing_stamps_the_time_and_reopening_clears_it(store: Store) -> None:
    project = await register(store, "one", Path("/tmp/one"))
    task = await store.add_task(project_id=project.id, title="Work", tags=[" API ", "api"])
    assert task.closed_at is None
    assert task.tags == ["api"]

    closed = await store.update_task(task.id, status=Status.DONE)
    assert closed is not None and closed.closed_at is not None
    reopened = await store.update_task(task.id, status=Status.TODO)
    assert reopened is not None and reopened.closed_at is None

    born_closed = await store.add_task(
        project_id=project.id, title="Already done", status=Status.CANCELLED
    )
    assert born_closed.closed_at is not None


async def test_listing_orders_open_tasks_by_priority(store: Store) -> None:
    project = await register(store, "one", Path("/tmp/one"))
    low = await store.add_task(project_id=project.id, title="Low", priority=Priority.LOW)
    urgent = await store.add_task(project_id=project.id, title="Urgent", priority=Priority.URGENT)
    done = await store.add_task(project_id=project.id, title="Done", status=Status.DONE)

    #: Ranking is by priority alone; the open statuses come first because callers filter.
    listed = await store.tasks(project_id=project.id, limit=10)
    assert [item.id for item in listed] == [urgent.id, done.id, low.id]
    recent = await store.tasks(project_id=project.id, order="recent", limit=10)
    assert [item.id for item in recent][0] == done.id
    open_only = await store.tasks(project_id=project.id, status=[Status.TODO], limit=10)
    assert [item.id for item in open_only] == [urgent.id, low.id]
    assert await store.count_tasks(project_id=project.id, status=[Status.DONE]) == 1
    paged = await store.tasks(project_id=project.id, limit=1, offset=1)
    assert [item.id for item in paged] == [done.id]


async def test_search_is_local_by_default_and_global_on_request(store: Store) -> None:
    here = await register(store, "here", Path("/tmp/here"))
    there = await register(store, "there", Path("/tmp/there"))
    mine = await store.add_task(
        project_id=here.id, title="Authentication service", body="Token refresh", tags=["security"]
    )
    await store.add_task(project_id=there.id, title="Authentication proxy")

    assert [item.id for item in await store.search("auth serv", project_id=here.id)] == [mine.id]
    assert len(await store.search("auth")) == 2
    assert await store.search("auth", project_id=here.id, tag="docs") == []
    assert await store.search("nothing matches this") == []
    found = await store.search("token", project_id=here.id)
    assert found[0].snippet is not None and "[Token]" in found[0].snippet


async def test_the_index_follows_updates_and_deletions(store: Store) -> None:
    project = await register(store, "one", Path("/tmp/one"))
    task = await store.add_task(project_id=project.id, title="Authentication", tags=["security"])
    await store.update_task(task.id, title="Login", tags=[])
    assert await store.search("authentication") == []
    assert len(await store.search("login")) == 1

    assert await store.delete_task(task.id) is True
    assert await store.delete_task(task.id) is False
    assert await store.search("login") == []
    deleted = await store.task(task.id)
    assert deleted is not None
    assert deleted.status == Status.DELETED


async def test_counts_and_tags_group_per_project(store: Store) -> None:
    here = await register(store, "here", Path("/tmp/here"))
    there = await register(store, "there", Path("/tmp/there"))
    await store.add_task(project_id=here.id, title="One", tags=["api"], priority=Priority.URGENT)
    await store.add_task(project_id=here.id, title="Two", status=Status.DONE, tags=["docs"])
    await store.add_task(project_id=there.id, title="Three", tags=["away"])

    assert await store.group("status", project_id=here.id) == {"todo": 1, "done": 1}
    assert await store.group("status") == {"todo": 2, "done": 1}
    assert await store.tags(project_id=here.id) == ["api", "docs"]
    assert await store.tags() == ["api", "away", "docs"]
    counted = {item.slug: (item.open_tasks, item.total_tasks) for item in await store.projects()}
    assert counted == {"here": (1, 2), "there": (1, 1)}


async def test_an_older_file_gains_the_notes_table(tmp_path: Path) -> None:
    #: A file written before notes existed: the first migration only, with a task in it.
    path = tmp_path / "backlog.sqlite3"
    connection = sqlite3.connect(path)
    for statement in MIGRATIONS[0]:
        connection.execute(statement)
    connection.execute("PRAGMA user_version=1")
    connection.execute(
        "INSERT INTO projects (slug, name, root, created_at, updated_at) VALUES (?, ?, ?, 0, 0)",
        ("old", "old", "/tmp/old"),
    )
    connection.execute(
        "INSERT INTO tasks (project_id, title, created_at, updated_at) VALUES (1, ?, 0, 0)",
        ("Survive the upgrade",),
    )
    connection.commit()
    connection.close()

    store = Store(path, computer="old-box")
    opened = await store.open()
    async with opened.execute("PRAGMA user_version") as cursor:
        row = await cursor.fetchone()
    assert row is not None and row[0] == len(MIGRATIONS)
    assert await store.count_tasks() == 1
    #: The projects of a file from before computers belong to the computer that upgraded it.
    upgraded = await store.project(1)
    assert upgraded is not None and upgraded.roots == [ProjectRoot("old-box", "/tmp/old")]
    assert (await store.task(1)) is not None
    async with opened.execute("PRAGMA foreign_key_check") as cursor:
        assert await cursor.fetchall() == []
    saved = await store.add_note(project_id=1, title="dev vm", body="host: vm-3")
    assert [item.id for item in await store.search_notes("vm")] == [saved.id]
    await store.close()


async def test_notes_are_saved_by_title_appended_and_deleted(store: Store) -> None:
    project = await register(store, "one", Path("/tmp/one"))
    saved = await store.add_note(
        project_id=project.id, title="dev vm", body="host: vm-3", tags=[" Infra ", "infra"]
    )
    assert saved.tags == ["infra"]
    assert saved.shared is False
    assert saved.project == "one"
    assert await store.note_by_title("dev vm", project_id=project.id) == saved
    assert await store.note_by_title("dev vm", project_id=project.id + 1) is None
    #: One note per title per project, so "save" always has one target.
    with pytest.raises(sqlite3.IntegrityError):
        await store.add_note(project_id=project.id, title="dev vm")

    appended = await store.append_note(saved.id, "port 2222")
    assert appended is not None and appended.body == "host: vm-3\n\nport 2222"
    assert appended.updated_at > saved.updated_at
    empty = await store.add_note(project_id=project.id, title="empty")
    first = await store.append_note(empty.id, "first fact")
    assert first is not None and first.body == "first fact"
    assert await store.append_note(999, "nowhere") is None

    renamed = await store.update_note(saved.id, title="dev host", shared=True, tags=[])
    assert renamed is not None
    assert (renamed.title, renamed.shared, renamed.tags) == ("dev host", True, [])
    assert renamed.body == "host: vm-3\n\nport 2222"
    assert await store.update_note(saved.id) == renamed
    assert await store.update_note(999, title="nowhere") is None

    assert await store.delete_note(saved.id) is True
    assert await store.delete_note(saved.id) is False
    assert await store.note(saved.id) is None


async def test_note_listing_reaches_shared_notes_of_other_projects(store: Store) -> None:
    here = await register(store, "here", Path("/tmp/here"))
    there = await register(store, "there", Path("/tmp/there"))
    mine = await store.add_note(project_id=here.id, title="local", tags=["infra"])
    theirs = await store.add_note(project_id=there.id, title="theirs")
    shared = await store.add_note(
        project_id=there.id, title="everywhere", shared=True, tags=["infra", "docs"]
    )

    listed = await store.notes(project_id=here.id)
    assert [item.id for item in listed] == [shared.id, mine.id]
    assert [item.id for item in await store.notes(project_id=there.id)] == [shared.id, theirs.id]
    assert [item.id for item in await store.notes(project_id=here.id, shared=True)] == [shared.id]
    assert [item.id for item in await store.notes(project_id=here.id, shared=False)] == [mine.id]
    assert len(await store.notes()) == 3
    assert await store.count_notes(project_id=here.id) == 2
    assert await store.count_notes(project_id=here.id, tag="docs") == 1
    assert [item.id for item in await store.notes(project_id=here.id, tag="infra")] == [
        shared.id,
        mine.id,
    ]
    assert [item.id for item in await store.notes(limit=1, offset=1)] == [theirs.id]


async def test_note_search_follows_scope_and_edits(store: Store) -> None:
    here = await register(store, "here", Path("/tmp/here"))
    there = await register(store, "there", Path("/tmp/there"))
    vm = await store.add_note(
        project_id=here.id, title="dev vm", body="host: vm-3.internal", tags=["infra"]
    )
    await store.add_note(project_id=there.id, title="staging", body="https://staging", shared=True)
    await store.add_note(project_id=there.id, title="private vm", body="hidden")

    found = await store.search_notes("vm", project_id=here.id)
    assert [item.id for item in found] == [vm.id]
    assert found[0].snippet is not None and "[vm]" in found[0].snippet
    assert len(await store.search_notes("staging", project_id=here.id)) == 1
    assert len(await store.search_notes("vm")) == 2
    assert await store.search_notes("vm", project_id=here.id, tag="docs") == []
    assert await store.search_notes("") == []
    assert await store.search_notes("nothing matches this") == []

    await store.append_note(vm.id, "port 2222")
    assert [item.id for item in await store.search_notes("port", project_id=here.id)] == [vm.id]
    await store.update_note(vm.id, body="moved away")
    assert await store.search_notes("host", project_id=here.id) == []
    assert len(await store.search_notes("moved", project_id=here.id)) == 1
    await store.delete_note(vm.id)
    assert await store.search_notes("moved") == []


async def test_a_note_body_stops_at_the_limit(store: Store) -> None:
    project = await register(store, "one", Path("/tmp/one"))
    full = "я" * NOTE_LIMIT
    saved = await store.add_note(project_id=project.id, title="full", body=full)
    assert len(saved.body) == NOTE_LIMIT
    with pytest.raises(ValueError, match="at most 1024 characters, this one has 1025"):
        await store.add_note(project_id=project.id, title="too long", body=full + "x")
    with pytest.raises(ValueError, match="at most 1024"):
        await store.update_note(saved.id, body=full + "x")
    #: An append that would overflow leaves the note as it was.
    with pytest.raises(ValueError, match="at most 1024"):
        await store.append_note(saved.id, "one more")
    assert (await store.note(saved.id)) == saved
    assert await store.note_by_title("too long", project_id=project.id) is None

    short = await store.add_note(project_id=project.id, title="short", body="a" * (NOTE_LIMIT - 5))
    assert await store.append_note(short.id, "bcd") is not None
    with pytest.raises(ValueError, match="at most 1024"):
        await store.append_note(short.id, "e")


async def test_project_delete_takes_everything_on_the_board(store: Store) -> None:
    gone = await store.add_project(slug="gone", name="gone", root="/tmp/gone", computer="desk")
    await store.add_root(gone.id, computer="laptop", root="/home/me/gone")
    kept = await store.add_project(slug="kept", name="kept", root="/tmp/kept", computer="desk")
    doomed = await store.add_task(project_id=gone.id, title="Doomed")
    hidden = await store.add_note(project_id=gone.id, title="Hidden", body="doomed")
    shared = await store.add_note(project_id=gone.id, title="Shared", body="doomed", shared=True)
    stays = await store.add_task(project_id=kept.id, title="Stays")
    assert await store.project_contents(gone.id) == ProjectContents(1, 2, 1)

    assert await store.delete_project(gone.id) is True
    assert await store.delete_project(gone.id) is False
    assert await store.project(gone.id) is None
    assert await store.project_contents(gone.id) == ProjectContents(0, 0, 0)
    assert await store.task(doomed.id) is None
    assert await store.note(hidden.id) is None
    assert await store.note(shared.id) is None
    assert await store.search("Doomed") == []
    assert await store.search_notes("doomed") == []
    assert await store.project_by_root("/home/me/gone", computer="laptop") is None
    assert await store.computers() == ["desk"]
    assert await store.task(stays.id) is not None
    #: The directories are free again.
    again = await store.add_project(slug="gone-2", name="gone", root="/tmp/gone", computer="desk")
    assert again.id != gone.id
