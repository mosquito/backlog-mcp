"""Project membership applies to storage, search and HTTP notifications."""

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestServer
from test_events import call, collect
from test_tenants import account

from backlog_mcp import embeddings, httpd
from backlog_mcp.auth import basic_namespace
from backlog_mcp.backlog import Backlog
from backlog_mcp.models import Status
from backlog_mcp.store import Store


async def test_project_access_and_search_candidates(backlog: Backlog) -> None:
    base = backlog.store
    for user in ("alice", "bob", "eve"):
        await base.register(basic_namespace(user), user)
    alice = base.view(principal=basic_namespace("alice"), computer="same")
    bob = base.view(principal=basic_namespace("bob"), computer="same")
    private = await alice.add_project(slug="private", name="Private", root="/work", computer="same")
    shared = await alice.add_project(slug="shared", name="Shared", root="", computer="same")
    hidden = await alice.add_task(project_id=private.id, title="needle " * 10)
    visible = await alice.add_task(project_id=shared.id, title="needle")
    secret_note = await alice.add_note(
        project_id=private.id, title="needle " * 10, body="secret", shared=True
    )
    visible_note = await alice.add_note(project_id=shared.id, title="needle", body="public")
    await alice.add_comment(hidden.id, "needle " * 10)
    comment = await alice.add_comment(visible.id, "needle")
    await alice.share(shared.id, bob.principal, "reader")

    assert [p.id for p in await bob.projects()] == [shared.id]
    assert await bob.project(private.id) is None
    assert await bob.project_by_root("/work", computer="same") is None
    assert await bob.task(hidden.id) is None
    assert await bob.note(secret_note.id) is None
    assert [t.id for t in await bob.search("needle", limit=1)] == [visible.id]
    assert [n.id for n in await bob.search_notes("needle", limit=1)] == [visible_note.id]
    assert [c.id for c in await bob.comments(query="needle", limit=1)] == [comment.id]
    assert await bob.count_tasks() == 1
    assert await bob.count_notes() == 1
    assert await bob.count_comments(hidden.id) == 0
    assert await bob.tags(project_id=private.id) == []
    assert await bob.group("status", project_id=private.id) == {}

    scoped = backlog.view(principal=bob.principal)
    found = await embeddings.search(
        embeddings.Search(query="needle", all_computers=True, limit=1, min_score=-1), scoped
    )
    assert [t.id for t in found.tasks] == [visible.id]
    assert [n.id for n in found.memory] == [visible_note.id]
    assert [c.id for c in found.comments] == [comment.id]
    snapshot = await embeddings.snapshot(scoped)
    assert [t.id for t in snapshot[0]] == [visible.id]
    assert embeddings.database_key(scoped) != embeddings.database_key(backlog)

    for mutation in (
        bob.add_task(project_id=shared.id, title="Forbidden"),
        bob.update_task(visible.id, title="Forbidden"),
        bob.add_comment(visible.id, "Forbidden"),
        bob.update_note(visible_note.id, body="Forbidden"),
        bob.append_note(visible_note.id, "Forbidden"),
        bob.delete_note(visible_note.id),
        bob.update_project(shared.id, description="Forbidden"),
        bob.delete_project(shared.id),
        bob.share(shared.id, basic_namespace("eve"), "editor"),
    ):
        with pytest.raises(PermissionError):
            await mutation
    await bob.add_root(shared.id, computer="same", root="/work")
    assert (await bob.roots(shared.id))[0].root == "/work"
    assert await alice.roots(shared.id) != await bob.roots(shared.id)

    await alice.share(shared.id, bob.principal, "editor")
    await bob.update_task(visible.id, status=Status.DOING)
    history = (await alice.comments(task_id=visible.id))[-1]
    assert (history.principal, history.computer, history.kind) == (bob.principal, "same", "status")
    edited = await alice.task(visible.id)
    assert edited and edited.created_by and edited.updated_by
    assert edited.created_by.principal == alice.principal
    assert edited.updated_by.principal == bob.principal
    with pytest.raises(LookupError):
        await bob.update_task(visible.id, project_id=private.id)
    with pytest.raises(PermissionError):
        await bob.delete_project(shared.id)


async def test_sharing_tools_and_member_notifications(backlog: Backlog) -> None:
    async with (
        TestServer(httpd.application(backlog, basic=["alice:password", "bob:password"])) as server,
        account(server, "alice") as alice,
        account(server, "bob") as bob,
    ):
        project = await call(alice, "project_save", root="", name="Shared")
        members = await call(
            alice, "project_share", project=project["slug"], principal="bob", role="reader"
        )
        assert {m["role"] for m in members["items"]} == {"owner", "reader"}
        assert (await call(bob, "project_current"))["principal"] == basic_namespace("bob")
        assert (await call(bob, "project_list", all_computers=True))["items"][0]["role"] == "reader"
        denied = await bob.call_tool("task_add", {"project": project["slug"], "title": "No"})
        assert denied.is_error
        frames: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        collecting = collect(bob.listen(resources=["backlog://tasks"]), frames)
        try:
            async with asyncio.timeout(5):
                while not bob.accepted:
                    await asyncio.sleep(0.01)
            task = await call(alice, "task_add", project=project["slug"], title="Together")
            event = (await asyncio.wait_for(frames.get(), 5))["params"]["_meta"]["backlog/event"]
            assert event["principal"] == basic_namespace("alice")
            assert event["project_id"] == project["id"]
            assert (await call(bob, "task_get", id=task["id"]))["title"] == "Together"
        finally:
            collecting.cancel()
            await asyncio.gather(collecting, return_exceptions=True)
        comment = await call(alice, "task_comment_add", task_id=task["id"], body="Written by Alice")
        assert comment["principal"] == basic_namespace("alice")
        assert comment["computer"] == backlog.computer
        tables = await backlog.store.rows("SELECT name FROM sqlite_master WHERE type='table'")
        assert {"mcp_sessions", "mcp_events", "projects"} <= {row[0] for row in tables}


async def test_old_database_is_refused_and_left_intact(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE projects(id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute("INSERT INTO projects VALUES (1, 'Keep me')")
        connection.execute("PRAGMA user_version=6")
    store = Store(path)
    with pytest.raises(ValueError, match="fresh database"):
        await store.open()
    assert store.connection is None
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM projects").fetchall() == [(1, "Keep me")]
        assert connection.execute("PRAGMA user_version").fetchone() == (6,)


async def test_same_slugs_are_private_and_shared_ids_are_unambiguous(backlog: Backlog) -> None:
    alice = backlog.store
    await alice.register(basic_namespace("bob"), "bob")
    bob = alice.view(principal=basic_namespace("bob"))
    first = await alice.add_project(slug="same", name="A", root="", computer="here")
    second = await bob.add_project(slug="same", name="B", root="", computer="here")
    assert (await alice.project_by_slug("same")).id == first.id
    assert (await bob.project_by_slug("same")).id == second.id
    await alice.share(first.id, bob.principal, "reader")
    with pytest.raises(ValueError, match="ambiguous"):
        await bob.project_by_slug("same")
    assert (await backlog.view(principal=bob.principal).lookup(f"#{first.id}")).id == first.id


async def test_qualified_names_resolve_shared_projects_without_exposing_private_ones(
    backlog: Backlog,
) -> None:
    async with (
        TestServer(httpd.application(backlog, basic=["alice:password", "bob:password"])) as server,
        account(server, "alice") as alice,
        account(server, "bob") as bob,
    ):
        first = await call(alice, "project_save", root="", name="Foo")
        second = await call(bob, "project_save", root="", name="Foo")
        assert first["slug"] == second["slug"] == "foo"
        assert first["full_name"] == "alice/foo"
        assert second["full_name"] == "bob/foo"
        denied = await bob.call_tool("task_add", {"project": "alice/foo", "title": "Hidden"})
        assert denied.is_error
        await call(alice, "project_share", project="alice/foo", principal="bob", role="editor")
        ambiguous = await bob.call_tool("task_add", {"project": "foo", "title": "Ambiguous"})
        assert ambiguous.is_error
        ours = await call(bob, "task_add", project="bob/foo", title="Ours")
        theirs = await call(bob, "task_add", project="alice/foo", title="Theirs")
        assert ours["project"] == "bob/foo"
        assert theirs["project"] == "alice/foo"
        assert ours["id"] != theirs["id"]
        listed = await call(bob, "task_list", project="alice/foo")
        assert [task["id"] for task in listed["items"]] == [theirs["id"]]
        completion = await bob.complete(
            {"type": "ref/prompt", "name": "groom"}, {"name": "project", "value": "foo"}
        )
        assert completion.completion.values == ["alice/foo", "bob/foo"]


async def test_shared_names_do_not_reserve_the_recipients_own_slug(backlog: Backlog) -> None:
    await backlog.store.register(basic_namespace("alice"), "alice")
    await backlog.store.register(basic_namespace("bob"), "bob")
    alice = backlog.view(principal=basic_namespace("alice"))
    bob = backlog.view(principal=basic_namespace("bob"))
    first = await alice.create("", name="Foo")
    await alice.store.share(first.id, bob.store.principal, "reader")
    own = await bob.create("", name="Foo")
    assert (first.full_name, own.full_name) == ("alice/foo", "bob/foo")
    assert (await bob.lookup("alice/foo")).id == first.id
    assert (await bob.lookup("bob/foo")).id == own.id
    with pytest.raises(ValueError, match="alice/foo, bob/foo"):
        await bob.lookup("foo")


async def test_owner_handles_are_stable_and_do_not_collide(backlog: Backlog) -> None:
    import json

    from backlog_mcp.names import owner_name

    identities = [
        basic_namespace("alice"),
        basic_namespace("a/b"),
        basic_namespace("a%2Fb"),
        basic_namespace("@local"),
        basic_namespace(".."),
        '["local"]',
        json.dumps(["jwt", "issuer-one", "subject", "alice"]),
        json.dumps(["jwt", "issuer-two", "subject", "alice"]),
    ]
    handles = [owner_name(identity) for identity in identities]
    assert len(set(handles)) == len(identities)
    assert handles[:3] == ["alice", "a%2Fb", "a%252Fb"]
    for identity in identities:
        await backlog.store.register(identity, "Same display name")
        scoped = backlog.view(principal=identity)
        project = await scoped.create("", name="Foo")
        assert (await scoped.lookup(project.full_name)).id == project.id
        assert "/" not in owner_name(identity)
        await backlog.store.register(identity, "Changed display name")
        assert (await scoped.lookup(project.full_name)).id == project.id


async def test_project_search_filters_acl_scope_and_archived_before_limit(backlog: Backlog) -> None:
    for user in ("alice", "bob"):
        await backlog.store.register(basic_namespace(user), user)
    alice = backlog.store.view(principal=basic_namespace("alice"), computer="same")
    bob = backlog.store.view(principal=basic_namespace("bob"), computer="same")
    hidden = await alice.add_project(slug="hidden", name="Needle " * 10, root="", computer="same")
    visible = await alice.add_project(slug="visible", name="Needle", root="", computer="same")
    archived = await alice.add_project(slug="archived", name="Needle", root="", computer="same")
    await alice.update_project(archived.id, archived=True)
    await alice.share(visible.id, bob.principal, "reader")
    await alice.share(archived.id, bob.principal, "reader")
    assert [p.id for p in await bob.search_projects("needle", limit=1)] == [visible.id]
    scoped = backlog.view(principal=bob.principal)
    query = embeddings.Search(query="needle", kinds=["projects"], all_computers=True, limit=1)
    found = await embeddings.search(query, scoped)
    assert found.count == 1 and found.projects[0].full_name == "alice/visible"
    assert found.projects[0].match == "fts"
    assert found.tasks == found.comments == found.memory == []
    assert {p.id for p in (await embeddings.snapshot(scoped))[3]} == {visible.id}
    semantic = await embeddings.search(
        query.model_copy(update={"query": "sewing", "min_score": -1}), scoped
    )
    assert [p.id for p in semantic.projects] == [visible.id]
    assert semantic.projects[0].match == "semantic"
    assert await bob.search_projects("needle", project_id=hidden.id) == []
    assert await bob.search_projects("needle", computer="same") == []
    await bob.add_root(visible.id, computer="same", root="/bob")
    assert [p.id for p in await bob.search_projects("needle", computer="same")] == [visible.id]
    await alice.update_project(visible.id, name="Changed", description="Orchestration")
    assert await bob.search_projects("needle") == []
    assert [p.id for p in await bob.search_projects("orchestration")] == [visible.id]
    await alice.delete_project(visible.id)
    assert await bob.search_projects("orchestration") == []
