"""The SQLite layer: projects, tasks, notes and the full-text index behind them."""

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from helpers import mutate

from backlog_mcp import backlog as scope
from backlog_mcp import server as operations
from backlog_mcp.backlog import Backlog
from backlog_mcp.models import Priority, Status
from backlog_mcp.records import (
    NOTE_LIMIT,
    Project,
    ProjectContents,
    clean_tags,
    match_query,
    split_tags,
)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Backlog]:
    instance = Backlog(tmp_path / "backlog.sqlite3")
    await instance.open()
    try:
        yield instance
    finally:
        await instance.close()


async def register(store: Backlog, slug: str, root: Path, computer: str = "here") -> Project:
    return await mutate(
        scope.add_project(
            await store.open(), store.actor, slug=slug, name=slug, root=root, computer=computer
        ),
        store.transaction,
    )


def test_tag_helpers_normalise() -> None:
    assert clean_tags([" API ", "api", "", "docs"]) == ["api", "docs"]
    assert split_tags("api,docs") == ["api", "docs"]
    assert split_tags("") == []


def test_match_query_keeps_operators_out() -> None:
    assert match_query("auth serv") == '"auth"* "serv"*'
    assert match_query('" OR *') == '"OR"*'


async def test_reopening_keeps_the_schema_and_the_rows(tmp_path: Path) -> None:
    store = Backlog(tmp_path / "backlog.sqlite3")
    project = await register(store, "one", tmp_path / "one")
    await mutate(
        operations.add_task(
            await store.open(), store.actor, project_id=project.id, title="Survive a restart"
        ),
        store.transaction,
    )
    await store.close()

    again = Backlog(tmp_path / "backlog.sqlite3")
    connection = await again.open()
    async with connection.execute("PRAGMA application_id") as cursor:
        row = await cursor.fetchone()
    assert row is not None and row[0] > 0
    assert (await operations.count_tasks(await again.open(), again.actor)) == 1
    await again.close()


async def test_project_language_persists_and_can_be_cleared(tmp_path: Path) -> None:
    path = tmp_path / "backlog.sqlite3"
    store = Backlog(path)
    project = await mutate(
        scope.add_project(
            await store.open(),
            store.actor,
            slug="one",
            name="One",
            root="",
            computer="here",
            preferred_language="ru",
        ),
        store.transaction,
    )
    assert project.preferred_language == "ru"
    await store.close()

    reopened = Backlog(path)
    try:
        project = await scope.read_project(await reopened.open(), reopened.actor, project.id)
        assert project is not None and project.preferred_language == "ru"
        updated = await mutate(
            scope.update_project(
                await reopened.open(), reopened.actor, project.id, description="Changed"
            ),
            reopened.transaction,
        )
        assert updated is not None and updated.preferred_language == "ru"
        updated = await mutate(
            scope.update_project(
                await reopened.open(), reopened.actor, project.id, preferred_language="en"
            ),
            reopened.transaction,
        )
        assert updated is not None and updated.preferred_language == "en"
        updated = await mutate(
            scope.update_project(
                await reopened.open(), reopened.actor, project.id, preferred_language=None
            ),
            reopened.transaction,
        )
        assert updated is not None and updated.preferred_language is None
    finally:
        await reopened.close()


async def test_project_for_path_takes_the_deepest_root(store: Backlog, tmp_path: Path) -> None:
    outer = await register(store, "outer", tmp_path)
    inner = await register(store, "inner", tmp_path / "inner")
    deep = tmp_path / "inner" / "deep"
    assert (
        await scope.project_for_path(await store.open(), store.actor, deep, computer="here")
    ).id == inner.id
    assert (
        await scope.project_for_path(
            await store.open(), store.actor, tmp_path / "other", computer="here"
        )
    ).id == outer.id
    assert (
        await scope.project_for_path(
            await store.open(), store.actor, Path("/nowhere"), computer="here"
        )
        is None
    )
    assert (
        await scope.project_for_path(await store.open(), store.actor, deep, computer="elsewhere")
        is None
    )
    assert (
        await scope.project_by_root(
            await store.open(), store.actor, tmp_path / "inner", computer="here"
        )
        == inner
    )
    assert (
        await scope.project_by_root(
            await store.open(), store.actor, tmp_path / "inner", computer="elsewhere"
        )
        is None
    )
    assert await scope.project_by_slug(await store.open(), store.actor, "missing") is None
    assert await scope.slugs(await store.open(), store.actor) == ["local/inner", "local/outer"]


async def test_archiving_hides_a_project_from_the_default_listing(store: Backlog) -> None:
    project = await register(store, "one", Path("/tmp/one"))
    await mutate(
        scope.update_project(
            await store.open(), store.actor, project.id, archived=True, description="Retired"
        ),
        store.transaction,
    )
    assert await scope.read_projects(await store.open(), store.actor) == []
    archived = await scope.read_projects(await store.open(), store.actor, archived=True)
    assert [item.slug for item in archived] == ["one"]
    assert archived[0].description == "Retired"


async def test_closing_stamps_the_time_and_reopening_clears_it(store: Backlog) -> None:
    project = await register(store, "one", Path("/tmp/one"))
    task = await mutate(
        operations.add_task(
            await store.open(),
            store.actor,
            project_id=project.id,
            title="Work",
            tags=[" API ", "api"],
        ),
        store.transaction,
    )
    assert task.closed_at is None
    assert task.tags == ["api"]

    closed = await mutate(
        operations.update_task(await store.open(), store.actor, task.id, status=Status.DONE),
        store.transaction,
    )
    assert closed is not None and closed.closed_at is not None
    reopened = await mutate(
        operations.update_task(await store.open(), store.actor, task.id, status=Status.TODO),
        store.transaction,
    )
    assert reopened is not None and reopened.closed_at is None

    born_closed = await mutate(
        operations.add_task(
            await store.open(),
            store.actor,
            project_id=project.id,
            title="Already done",
            status=Status.CANCELLED,
        ),
        store.transaction,
    )
    assert born_closed.closed_at is not None


async def test_listing_orders_open_tasks_by_priority(store: Backlog) -> None:
    project = await register(store, "one", Path("/tmp/one"))
    low = await mutate(
        operations.add_task(
            await store.open(),
            store.actor,
            project_id=project.id,
            title="Low",
            priority=Priority.LOW,
        ),
        store.transaction,
    )
    urgent = await mutate(
        operations.add_task(
            await store.open(),
            store.actor,
            project_id=project.id,
            title="Urgent",
            priority=Priority.URGENT,
        ),
        store.transaction,
    )
    done = await mutate(
        operations.add_task(
            await store.open(), store.actor, project_id=project.id, title="Done", status=Status.DONE
        ),
        store.transaction,
    )

    #: Ranking is by priority alone; the open statuses come first because callers filter.
    listed = await operations.read_tasks(
        await store.open(), store.actor, project_id=project.id, limit=10
    )
    assert [item.id for item in listed] == [urgent.id, done.id, low.id]
    recent = await operations.read_tasks(
        await store.open(), store.actor, project_id=project.id, order="recent", limit=10
    )
    assert [item.id for item in recent][0] == done.id
    open_only = await operations.read_tasks(
        await store.open(), store.actor, project_id=project.id, status=[Status.TODO], limit=10
    )
    assert [item.id for item in open_only] == [urgent.id, low.id]
    assert (
        await operations.count_tasks(
            await store.open(), store.actor, project_id=project.id, status=[Status.DONE]
        )
        == 1
    )
    paged = await operations.read_tasks(
        await store.open(), store.actor, project_id=project.id, limit=1, offset=1
    )
    assert [item.id for item in paged] == [done.id]


async def test_search_is_local_by_default_and_global_on_request(store: Backlog) -> None:
    here = await register(store, "here", Path("/tmp/here"))
    there = await register(store, "there", Path("/tmp/there"))
    mine = await mutate(
        operations.add_task(
            await store.open(),
            store.actor,
            project_id=here.id,
            title="Authentication service",
            body="Token refresh",
            tags=["security"],
        ),
        store.transaction,
    )
    await mutate(
        operations.add_task(
            await store.open(), store.actor, project_id=there.id, title="Authentication proxy"
        ),
        store.transaction,
    )

    assert [
        item.id
        for item in await operations.search_tasks(
            await store.open(), store.actor, "auth serv", project_id=here.id
        )
    ] == [mine.id]
    assert len(await operations.search_tasks(await store.open(), store.actor, "auth")) == 2
    assert (
        await operations.search_tasks(
            await store.open(), store.actor, "auth", project_id=here.id, tag="docs"
        )
        == []
    )
    assert (
        await operations.search_tasks(await store.open(), store.actor, "nothing matches this") == []
    )
    found = await operations.search_tasks(
        await store.open(), store.actor, "token", project_id=here.id
    )
    assert found[0].snippet is not None and "[Token]" in found[0].snippet


async def test_the_index_follows_updates_and_deletions(store: Backlog) -> None:
    project = await register(store, "one", Path("/tmp/one"))
    task = await mutate(
        operations.add_task(
            await store.open(),
            store.actor,
            project_id=project.id,
            title="Authentication",
            tags=["security"],
        ),
        store.transaction,
    )
    await mutate(
        operations.update_task(await store.open(), store.actor, task.id, title="Login", tags=[]),
        store.transaction,
    )
    assert await operations.search_tasks(await store.open(), store.actor, "authentication") == []
    assert len(await operations.search_tasks(await store.open(), store.actor, "login")) == 1

    assert (
        await mutate(
            operations.delete_task(await store.open(), store.actor, task.id), store.transaction
        )
        is True
    )
    assert (
        await mutate(
            operations.delete_task(await store.open(), store.actor, task.id), store.transaction
        )
        is False
    )
    assert await operations.search_tasks(await store.open(), store.actor, "login") == []
    deleted = await operations.read_task(await store.open(), store.actor, task.id)
    assert deleted is not None
    assert deleted.status == Status.DELETED


async def test_counts_and_tags_group_per_project(store: Backlog) -> None:
    here = await register(store, "here", Path("/tmp/here"))
    there = await register(store, "there", Path("/tmp/there"))
    await mutate(
        operations.add_task(
            await store.open(),
            store.actor,
            project_id=here.id,
            title="One",
            tags=["api"],
            priority=Priority.URGENT,
        ),
        store.transaction,
    )
    await mutate(
        operations.add_task(
            await store.open(),
            store.actor,
            project_id=here.id,
            title="Two",
            status=Status.DONE,
            tags=["docs"],
        ),
        store.transaction,
    )
    await mutate(
        operations.add_task(
            await store.open(), store.actor, project_id=there.id, title="Three", tags=["away"]
        ),
        store.transaction,
    )

    assert await operations.group(
        await store.open(), store.actor, "status", project_id=here.id
    ) == {"todo": 1, "done": 1}
    assert await operations.group(await store.open(), store.actor, "status") == {
        "todo": 2,
        "done": 1,
    }
    assert await operations.read_tags(await store.open(), store.actor, project_id=here.id) == [
        "api",
        "docs",
    ]
    assert await operations.read_tags(await store.open(), store.actor) == ["api", "away", "docs"]
    counted = {
        item.slug: (item.open_tasks, item.total_tasks)
        for item in await scope.read_projects(await store.open(), store.actor)
    }
    assert counted == {"here": (1, 2), "there": (1, 1)}


async def test_notes_are_saved_by_title_appended_and_deleted(store: Backlog) -> None:
    project = await register(store, "one", Path("/tmp/one"))
    saved = await mutate(
        operations.add_note(
            await store.open(),
            store.actor,
            project_id=project.id,
            title="dev vm",
            body="host: vm-3",
            tags=[" Infra ", "infra"],
        ),
        store.transaction,
    )
    assert saved.tags == ["infra"]
    assert saved.shared is False
    assert saved.project == "local/one"
    assert (
        await operations.note_by_title(
            await store.open(), store.actor, "dev vm", project_id=project.id
        )
        == saved
    )
    assert (
        await operations.note_by_title(
            await store.open(), store.actor, "dev vm", project_id=project.id + 1
        )
        is None
    )
    #: One note per title per project, so "save" always has one target.
    with pytest.raises(sqlite3.IntegrityError):
        await mutate(
            operations.add_note(
                await store.open(), store.actor, project_id=project.id, title="dev vm"
            ),
            store.transaction,
        )

    appended = await mutate(
        operations.append_note(await store.open(), store.actor, saved.id, "port 2222"),
        store.transaction,
    )
    assert appended is not None and appended.body == "host: vm-3\n\nport 2222"
    assert appended.updated_at > saved.updated_at
    empty = await mutate(
        operations.add_note(await store.open(), store.actor, project_id=project.id, title="empty"),
        store.transaction,
    )
    first = await mutate(
        operations.append_note(await store.open(), store.actor, empty.id, "first fact"),
        store.transaction,
    )
    assert first is not None and first.body == "first fact"
    assert (
        await mutate(
            operations.append_note(await store.open(), store.actor, 999, "nowhere"),
            store.transaction,
        )
        is None
    )

    renamed = await mutate(
        operations.update_note(
            await store.open(), store.actor, saved.id, title="dev host", shared=True, tags=[]
        ),
        store.transaction,
    )
    assert renamed is not None
    assert (renamed.title, renamed.shared, renamed.tags) == ("dev host", True, [])
    assert renamed.body == "host: vm-3\n\nport 2222"
    assert (
        await mutate(
            operations.update_note(await store.open(), store.actor, saved.id), store.transaction
        )
        == renamed
    )
    assert (
        await mutate(
            operations.update_note(await store.open(), store.actor, 999, title="nowhere"),
            store.transaction,
        )
        is None
    )

    assert (
        await mutate(
            operations.delete_note(await store.open(), store.actor, saved.id), store.transaction
        )
        is True
    )
    assert (
        await mutate(
            operations.delete_note(await store.open(), store.actor, saved.id), store.transaction
        )
        is False
    )
    assert await operations.read_note(await store.open(), store.actor, saved.id) is None


async def test_note_listing_keeps_shared_notes_in_their_project(store: Backlog) -> None:
    here = await register(store, "here", Path("/tmp/here"))
    there = await register(store, "there", Path("/tmp/there"))
    mine = await mutate(
        operations.add_note(
            await store.open(), store.actor, project_id=here.id, title="local", tags=["infra"]
        ),
        store.transaction,
    )
    theirs = await mutate(
        operations.add_note(await store.open(), store.actor, project_id=there.id, title="theirs"),
        store.transaction,
    )
    shared = await mutate(
        operations.add_note(
            await store.open(),
            store.actor,
            project_id=there.id,
            title="everywhere",
            shared=True,
            tags=["infra", "docs"],
        ),
        store.transaction,
    )

    listed = await operations.read_notes(await store.open(), store.actor, project_id=here.id)
    assert [item.id for item in listed] == [mine.id]
    assert [
        item.id
        for item in await operations.read_notes(
            await store.open(), store.actor, project_id=there.id
        )
    ] == [shared.id, theirs.id]
    assert [
        item.id
        for item in await operations.read_notes(
            await store.open(), store.actor, project_id=here.id, shared=True
        )
    ] == []
    assert [
        item.id
        for item in await operations.read_notes(
            await store.open(), store.actor, project_id=here.id, shared=False
        )
    ] == [mine.id]
    assert len(await operations.read_notes(await store.open(), store.actor)) == 3
    assert await operations.count_notes(await store.open(), store.actor, project_id=here.id) == 1
    assert (
        await operations.count_notes(
            await store.open(), store.actor, project_id=here.id, tag="docs"
        )
        == 0
    )
    assert [
        item.id
        for item in await operations.read_notes(
            await store.open(), store.actor, project_id=here.id, tag="infra"
        )
    ] == [mine.id]
    assert [
        item.id
        for item in await operations.read_notes(await store.open(), store.actor, limit=1, offset=1)
    ] == [theirs.id]


async def test_note_search_follows_scope_and_edits(store: Backlog) -> None:
    here = await register(store, "here", Path("/tmp/here"))
    there = await register(store, "there", Path("/tmp/there"))
    vm = await mutate(
        operations.add_note(
            await store.open(),
            store.actor,
            project_id=here.id,
            title="dev vm",
            body="host: vm-3.internal",
            tags=["infra"],
        ),
        store.transaction,
    )
    await mutate(
        operations.add_note(
            await store.open(),
            store.actor,
            project_id=there.id,
            title="staging",
            body="https://staging",
            shared=True,
        ),
        store.transaction,
    )
    await mutate(
        operations.add_note(
            await store.open(), store.actor, project_id=there.id, title="private vm", body="hidden"
        ),
        store.transaction,
    )

    found = await operations.search_notes(await store.open(), store.actor, "vm", project_id=here.id)
    assert [item.id for item in found] == [vm.id]
    assert found[0].snippet is not None and "[vm]" in found[0].snippet
    assert (
        len(
            await operations.search_notes(
                await store.open(), store.actor, "staging", project_id=here.id
            )
        )
        == 0
    )
    assert len(await operations.search_notes(await store.open(), store.actor, "vm")) == 2
    assert (
        await operations.search_notes(
            await store.open(), store.actor, "vm", project_id=here.id, tag="docs"
        )
        == []
    )
    assert await operations.search_notes(await store.open(), store.actor, "") == []
    assert (
        await operations.search_notes(await store.open(), store.actor, "nothing matches this") == []
    )

    await mutate(
        operations.append_note(await store.open(), store.actor, vm.id, "port 2222"),
        store.transaction,
    )
    assert [
        item.id
        for item in await operations.search_notes(
            await store.open(), store.actor, "port", project_id=here.id
        )
    ] == [vm.id]
    await mutate(
        operations.update_note(await store.open(), store.actor, vm.id, body="moved away"),
        store.transaction,
    )
    assert (
        await operations.search_notes(await store.open(), store.actor, "host", project_id=here.id)
        == []
    )
    assert (
        len(
            await operations.search_notes(
                await store.open(), store.actor, "moved", project_id=here.id
            )
        )
        == 1
    )
    await mutate(operations.delete_note(await store.open(), store.actor, vm.id), store.transaction)
    assert await operations.search_notes(await store.open(), store.actor, "moved") == []


async def test_a_note_body_stops_at_the_limit(store: Backlog) -> None:
    project = await register(store, "one", Path("/tmp/one"))
    full = "я" * NOTE_LIMIT
    saved = await mutate(
        operations.add_note(
            await store.open(), store.actor, project_id=project.id, title="full", body=full
        ),
        store.transaction,
    )
    assert len(saved.body) == NOTE_LIMIT
    with pytest.raises(ValueError, match="at most 1024 characters, this one has 1025"):
        await mutate(
            operations.add_note(
                await store.open(),
                store.actor,
                project_id=project.id,
                title="too long",
                body=full + "x",
            ),
            store.transaction,
        )
    with pytest.raises(ValueError, match="at most 1024"):
        await mutate(
            operations.update_note(await store.open(), store.actor, saved.id, body=full + "x"),
            store.transaction,
        )
    #: An append that would overflow leaves the note as it was.
    with pytest.raises(ValueError, match="at most 1024"):
        await mutate(
            operations.append_note(await store.open(), store.actor, saved.id, "one more"),
            store.transaction,
        )
    assert (await operations.read_note(await store.open(), store.actor, saved.id)) == saved
    assert (
        await operations.note_by_title(
            await store.open(), store.actor, "too long", project_id=project.id
        )
        is None
    )

    short = await mutate(
        operations.add_note(
            await store.open(),
            store.actor,
            project_id=project.id,
            title="short",
            body="a" * (NOTE_LIMIT - 5),
        ),
        store.transaction,
    )
    assert (
        await mutate(
            operations.append_note(await store.open(), store.actor, short.id, "bcd"),
            store.transaction,
        )
        is not None
    )
    with pytest.raises(ValueError, match="at most 1024"):
        await mutate(
            operations.append_note(await store.open(), store.actor, short.id, "e"),
            store.transaction,
        )


async def test_project_delete_takes_everything_on_the_board(store: Backlog) -> None:
    gone = await mutate(
        scope.add_project(
            await store.open(),
            store.actor,
            slug="gone",
            name="gone",
            root="/tmp/gone",
            computer="desk",
        ),
        store.transaction,
    )
    await mutate(
        scope.add_root(
            await store.open(), store.actor, gone.id, computer="laptop", root="/home/me/gone"
        ),
        store.transaction,
    )
    kept = await mutate(
        scope.add_project(
            await store.open(),
            store.actor,
            slug="kept",
            name="kept",
            root="/tmp/kept",
            computer="desk",
        ),
        store.transaction,
    )
    doomed = await mutate(
        operations.add_task(await store.open(), store.actor, project_id=gone.id, title="Doomed"),
        store.transaction,
    )
    hidden = await mutate(
        operations.add_note(
            await store.open(), store.actor, project_id=gone.id, title="Hidden", body="doomed"
        ),
        store.transaction,
    )
    shared = await mutate(
        operations.add_note(
            await store.open(),
            store.actor,
            project_id=gone.id,
            title="Shared",
            body="doomed",
            shared=True,
        ),
        store.transaction,
    )
    stays = await mutate(
        operations.add_task(await store.open(), store.actor, project_id=kept.id, title="Stays"),
        store.transaction,
    )
    assert await scope.project_contents(
        await store.open(), store.actor, gone.id
    ) == ProjectContents(1, 2, 1)

    assert (
        await mutate(
            scope.delete_project(await store.open(), store.actor, gone.id), store.transaction
        )
        is True
    )
    assert (
        await mutate(
            scope.delete_project(await store.open(), store.actor, gone.id), store.transaction
        )
        is False
    )
    assert await scope.read_project(await store.open(), store.actor, gone.id) is None
    with pytest.raises(LookupError):
        await scope.project_contents(await store.open(), store.actor, gone.id)
    assert await operations.read_task(await store.open(), store.actor, doomed.id) is None
    assert await operations.read_note(await store.open(), store.actor, hidden.id) is None
    assert await operations.read_note(await store.open(), store.actor, shared.id) is None
    assert await operations.search_tasks(await store.open(), store.actor, "Doomed") == []
    assert await operations.search_notes(await store.open(), store.actor, "doomed") == []
    assert (
        await scope.project_by_root(
            await store.open(), store.actor, "/home/me/gone", computer="laptop"
        )
        is None
    )
    assert await scope.computers(await store.open(), store.actor) == ["desk"]
    assert await operations.read_task(await store.open(), store.actor, stays.id) is not None
    #: The directories are free again.
    again = await mutate(
        scope.add_project(
            await store.open(),
            store.actor,
            slug="gone-2",
            name="gone",
            root="/tmp/gone",
            computer="desk",
        ),
        store.transaction,
    )
    assert again.id != gone.id
