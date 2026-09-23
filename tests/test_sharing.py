"""Project membership applies to storage, search and HTTP notifications."""

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestServer
from helpers import mutate, rows
from test_events import call, collect
from test_tenants import account

from backlog_mcp import backlog as scope
from backlog_mcp import embeddings, httpd
from backlog_mcp import server as operations
from backlog_mcp.auth import user_identity
from backlog_mcp.backlog import Backlog
from backlog_mcp.models import Status


async def test_project_access_and_search_candidates(backlog: Backlog) -> None:
    base = backlog
    for user in ("alice", "bob", "eve"):
        await mutate(
            scope.register(await base.open(), base.actor, user_identity(user), user),
            base.transaction,
        )
    alice = base.view(principal=user_identity("alice"), computer="same")
    bob = base.view(principal=user_identity("bob"), computer="same")
    private = await mutate(
        scope.add_project(
            await alice.open(),
            alice.actor,
            slug="private",
            name="Private",
            root="/work",
            computer="same",
        ),
        alice.transaction,
    )
    shared = await mutate(
        scope.add_project(
            await alice.open(), alice.actor, slug="shared", name="Shared", root="", computer="same"
        ),
        alice.transaction,
    )
    hidden = await mutate(
        operations.add_task(
            await alice.open(), alice.actor, project_id=private.id, title="needle " * 10
        ),
        alice.transaction,
    )
    visible = await mutate(
        operations.add_task(await alice.open(), alice.actor, project_id=shared.id, title="needle"),
        alice.transaction,
    )
    secret_note = await mutate(
        operations.add_note(
            await alice.open(),
            alice.actor,
            project_id=private.id,
            title="needle " * 10,
            body="secret",
            shared=True,
        ),
        alice.transaction,
    )
    visible_note = await mutate(
        operations.add_note(
            await alice.open(), alice.actor, project_id=shared.id, title="needle", body="public"
        ),
        alice.transaction,
    )
    await mutate(
        operations.add_comment(await alice.open(), alice.actor, hidden.id, "needle " * 10),
        alice.transaction,
    )
    comment = await mutate(
        operations.add_comment(await alice.open(), alice.actor, visible.id, "needle"),
        alice.transaction,
    )
    await mutate(
        scope.share(await alice.open(), alice.actor, shared.id, bob.principal, "reader"),
        alice.transaction,
    )

    assert [p.id for p in await scope.read_projects(await bob.open(), bob.actor)] == [shared.id]
    assert await scope.read_project(await bob.open(), bob.actor, private.id) is None
    assert (
        await scope.project_by_root(await bob.open(), bob.actor, "/work", computer="same") is None
    )
    assert await operations.read_task(await bob.open(), bob.actor, hidden.id) is None
    assert await operations.read_note(await bob.open(), bob.actor, secret_note.id) is None
    assert [
        t.id for t in await operations.search_tasks(await bob.open(), bob.actor, "needle", limit=1)
    ] == [visible.id]
    assert [
        n.id for n in await operations.search_notes(await bob.open(), bob.actor, "needle", limit=1)
    ] == [visible_note.id]
    assert [
        c.id
        for c in await operations.read_comments(
            await bob.open(), bob.actor, query="needle", limit=1
        )
    ] == [comment.id]
    assert await operations.count_tasks(await bob.open(), bob.actor) == 1
    assert await operations.count_notes(await bob.open(), bob.actor) == 1
    assert await operations.count_comments(await bob.open(), bob.actor, hidden.id) == 0
    assert await operations.read_tags(await bob.open(), bob.actor, project_id=private.id) == []
    assert (
        await operations.group(await bob.open(), bob.actor, "status", project_id=private.id) == {}
    )

    scoped = backlog.view(principal=bob.principal)
    found = await embeddings.search(
        embeddings.Search(query="needle", all_computers=True, limit=1, min_score=-1),
        scoped,
        connection=await scoped.open(),
        transaction=scoped.transaction,
    )
    assert [t.id for t in found.tasks] == [visible.id]
    assert [n.id for n in found.memory] == [visible_note.id]
    assert [c.id for c in found.comments] == [comment.id]
    snapshot = await embeddings.snapshot(scoped, connection=await scoped.open())
    assert [t.id for t in snapshot[0]] == [visible.id]
    indexer = embeddings.indexers[backlog.database]
    assert indexer.states[scoped.principal].indexes[0].ids == [visible.id]

    for mutation in (
        mutate(
            operations.add_task(
                await bob.open(), bob.actor, project_id=shared.id, title="Forbidden"
            ),
            bob.transaction,
        ),
        mutate(
            operations.update_task(await bob.open(), bob.actor, visible.id, title="Forbidden"),
            bob.transaction,
        ),
        mutate(
            operations.add_comment(await bob.open(), bob.actor, visible.id, "Forbidden"),
            bob.transaction,
        ),
        mutate(
            operations.update_note(await bob.open(), bob.actor, visible_note.id, body="Forbidden"),
            bob.transaction,
        ),
        mutate(
            operations.append_note(await bob.open(), bob.actor, visible_note.id, "Forbidden"),
            bob.transaction,
        ),
        mutate(
            operations.delete_note(await bob.open(), bob.actor, visible_note.id), bob.transaction
        ),
        mutate(
            scope.update_project(await bob.open(), bob.actor, shared.id, description="Forbidden"),
            bob.transaction,
        ),
        mutate(scope.delete_project(await bob.open(), bob.actor, shared.id), bob.transaction),
        mutate(
            scope.share(await bob.open(), bob.actor, shared.id, user_identity("eve"), "editor"),
            bob.transaction,
        ),
    ):
        with pytest.raises(PermissionError):
            await mutation
    await mutate(
        scope.add_root(await bob.open(), bob.actor, shared.id, computer="same", root="/work"),
        bob.transaction,
    )
    assert (await scope.roots(await bob.open(), bob.actor, shared.id))[0].root == "/work"
    assert await scope.roots(await alice.open(), alice.actor, shared.id) != await scope.roots(
        await bob.open(), bob.actor, shared.id
    )
    from backlog_mcp.server import project_out

    viewed = await scope.read_project(await bob.open(), bob.actor, shared.id)
    assert viewed is not None
    output = project_out(viewed, computer="same", principal=bob.principal).model_dump(mode="json")
    assert output["roots"] == ["bob@same:/work"]
    assert output["owner"] == "alice@unknown"

    await mutate(
        scope.share(await alice.open(), alice.actor, shared.id, bob.principal, "editor"),
        alice.transaction,
    )
    await mutate(
        operations.update_task(await bob.open(), bob.actor, visible.id, status=Status.DOING),
        bob.transaction,
    )
    history = (await operations.read_comments(await alice.open(), alice.actor, task_id=visible.id))[
        -1
    ]
    assert (history.principal, history.computer, history.kind) == (bob.principal, "same", "status")
    edited = await operations.read_task(await alice.open(), alice.actor, visible.id)
    assert edited and edited.created_by and edited.updated_by
    assert edited.created_by.principal == alice.principal
    assert edited.updated_by.principal == bob.principal
    with pytest.raises(LookupError):
        await mutate(
            operations.update_task(await bob.open(), bob.actor, visible.id, project_id=private.id),
            bob.transaction,
        )
    with pytest.raises(PermissionError):
        await mutate(scope.delete_project(await bob.open(), bob.actor, shared.id), bob.transaction)


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
        assert (await call(bob, "whoami"))["principal"] == f"bob@{backlog.computer}"
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
            assert event["principal"] == f"alice@{backlog.computer}"
            assert event["project_id"] == project["id"]
            assert (await call(bob, "task_get", id=task["id"]))["title"] == "Together"
        finally:
            collecting.cancel()
            await asyncio.gather(collecting, return_exceptions=True)
        comment = await call(alice, "task_comment_add", task_id=task["id"], body="Written by Alice")
        assert comment["principal"] == f"alice@{backlog.computer}"
        assert "computer" not in comment
        tables = await rows(
            await backlog.open(), "SELECT name FROM sqlite_master WHERE type='table'"
        )
        assert {"mcp_sessions", "mcp_events", "projects"} <= {row[0] for row in tables}


async def test_old_database_is_refused_and_left_intact(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE projects(id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute("INSERT INTO projects VALUES (1, 'Keep me')")
        connection.execute("PRAGMA user_version=6")
    store = Backlog(path)
    with pytest.raises(ValueError, match="Unsupported database schema"):
        await store.open()
    assert store.database.connection is None
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM projects").fetchall() == [(1, "Keep me")]
        assert connection.execute("PRAGMA user_version").fetchone() == (6,)


async def test_same_slugs_are_private_and_shared_ids_are_unambiguous(backlog: Backlog) -> None:
    alice = backlog
    await mutate(
        scope.register(await alice.open(), alice.actor, user_identity("bob"), "bob"),
        alice.transaction,
    )
    bob = alice.view(principal=user_identity("bob"))
    first = await mutate(
        scope.add_project(
            await alice.open(), alice.actor, slug="same", name="A", root="", computer="here"
        ),
        alice.transaction,
    )
    second = await mutate(
        scope.add_project(
            await bob.open(), bob.actor, slug="same", name="B", root="", computer="here"
        ),
        bob.transaction,
    )
    assert (await scope.project_by_slug(await alice.open(), alice.actor, "same")).id == first.id
    assert (await scope.project_by_slug(await bob.open(), bob.actor, "same")).id == second.id
    await mutate(
        scope.share(await alice.open(), alice.actor, first.id, bob.principal, "reader"),
        alice.transaction,
    )
    with pytest.raises(ValueError, match="ambiguous"):
        await scope.project_by_slug(await bob.open(), bob.actor, "same")
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
    await mutate(
        scope.register(await backlog.open(), backlog.actor, user_identity("alice"), "alice"),
        backlog.transaction,
    )
    await mutate(
        scope.register(await backlog.open(), backlog.actor, user_identity("bob"), "bob"),
        backlog.transaction,
    )
    alice = backlog.view(principal=user_identity("alice"))
    bob = backlog.view(principal=user_identity("bob"))
    first = await alice.create("", name="Foo")
    await mutate(
        scope.share(await alice.open(), alice.actor, first.id, bob.principal, "reader"),
        alice.transaction,
    )
    own = await bob.create("", name="Foo")
    assert (first.full_name, own.full_name) == ("alice/foo", "bob/foo")
    assert (await bob.lookup("alice/foo")).id == first.id
    assert (await bob.lookup("bob/foo")).id == own.id
    with pytest.raises(ValueError, match="alice/foo, bob/foo"):
        await bob.lookup("foo")


async def test_owner_handles_are_stable_and_do_not_collide(backlog: Backlog) -> None:
    from backlog_mcp.names import owner_name

    identities = [
        user_identity("alice"),
        user_identity("a/b"),
        user_identity("a%2Fb"),
        user_identity("@local"),
        user_identity(".."),
        "local",
        user_identity("~alice"),
        user_identity("alice@example.org"),
    ]
    handles = [owner_name(identity) for identity in identities]
    assert len(set(handles)) == len(identities)
    assert handles[:3] == ["alice", "a%2Fb", "a%252Fb"]
    for identity in identities:
        await mutate(
            scope.register(await backlog.open(), backlog.actor, identity, "Same display name"),
            backlog.transaction,
        )
        scoped = backlog.view(principal=identity)
        project = await scoped.create("", name="Foo")
        assert (await scoped.lookup(project.full_name)).id == project.id
        assert "/" not in owner_name(identity)
        await mutate(
            scope.register(await backlog.open(), backlog.actor, identity, "Changed display name"),
            backlog.transaction,
        )
        assert (await scoped.lookup(project.full_name)).id == project.id


async def test_project_search_filters_acl_scope_and_archived_before_limit(backlog: Backlog) -> None:
    for user in ("alice", "bob"):
        await mutate(
            scope.register(await backlog.open(), backlog.actor, user_identity(user), user),
            backlog.transaction,
        )
    alice = backlog.view(principal=user_identity("alice"), computer="same")
    bob = backlog.view(principal=user_identity("bob"), computer="same")
    hidden = await mutate(
        scope.add_project(
            await alice.open(),
            alice.actor,
            slug="hidden",
            name="Needle " * 10,
            root="",
            computer="same",
        ),
        alice.transaction,
    )
    visible = await mutate(
        scope.add_project(
            await alice.open(), alice.actor, slug="visible", name="Needle", root="", computer="same"
        ),
        alice.transaction,
    )
    archived = await mutate(
        scope.add_project(
            await alice.open(),
            alice.actor,
            slug="archived",
            name="Needle",
            root="",
            computer="same",
        ),
        alice.transaction,
    )
    await mutate(
        scope.update_project(await alice.open(), alice.actor, archived.id, archived=True),
        alice.transaction,
    )
    await mutate(
        scope.share(await alice.open(), alice.actor, visible.id, bob.principal, "reader"),
        alice.transaction,
    )
    await mutate(
        scope.share(await alice.open(), alice.actor, archived.id, bob.principal, "reader"),
        alice.transaction,
    )
    assert [
        p.id
        for p in await operations.search_projects(await bob.open(), bob.actor, "needle", limit=1)
    ] == [visible.id]
    scoped = backlog.view(principal=bob.principal)
    query = embeddings.Search(query="needle", kinds=["projects"], all_computers=True, limit=1)
    found = await embeddings.search(
        query, scoped, connection=await scoped.open(), transaction=scoped.transaction
    )
    assert found.count == 1 and found.projects[0].full_name == "alice/visible"
    assert found.projects[0].match == "fts"
    assert found.tasks == found.comments == found.memory == []
    assert {
        p.id for p in (await embeddings.snapshot(scoped, connection=await scoped.open()))[3]
    } == {visible.id}
    semantic = await embeddings.search(
        query.model_copy(update={"query": "sewing", "min_score": -1}),
        scoped,
        connection=await scoped.open(),
        transaction=scoped.transaction,
    )
    assert [p.id for p in semantic.projects] == [visible.id]
    assert semantic.projects[0].match == "semantic"
    assert (
        await operations.search_projects(
            await bob.open(), bob.actor, "needle", project_id=hidden.id
        )
        == []
    )
    assert (
        await operations.search_projects(await bob.open(), bob.actor, "needle", computer="same")
        == []
    )
    await mutate(
        scope.add_root(await bob.open(), bob.actor, visible.id, computer="same", root="/bob"),
        bob.transaction,
    )
    assert [
        p.id
        for p in await operations.search_projects(
            await bob.open(), bob.actor, "needle", computer="same"
        )
    ] == [visible.id]
    await mutate(
        scope.update_project(
            await alice.open(), alice.actor, visible.id, name="Changed", description="Orchestration"
        ),
        alice.transaction,
    )
    assert await operations.search_projects(await bob.open(), bob.actor, "needle") == []
    assert [
        p.id for p in await operations.search_projects(await bob.open(), bob.actor, "orchestration")
    ] == [visible.id]
    await mutate(
        scope.delete_project(await alice.open(), alice.actor, visible.id), alice.transaction
    )
    assert await operations.search_projects(await bob.open(), bob.actor, "orchestration") == []


async def test_background_index_follows_access_and_project_deletion(backlog: Backlog) -> None:
    project, _ = await backlog.current()
    connection = await backlog.open()
    await mutate(
        scope.register(connection, backlog.actor, user_identity("bob"), "Bob"), backlog.transaction
    )
    bob = backlog.view(principal=user_identity("bob"))
    await embeddings.indexes(backlog, connection, backlog.transaction)
    await embeddings.indexes(bob, connection, bob.transaction)
    indexer = embeddings.indexers[backlog.database]
    task = await operations.task_add(
        operations.AddTask(title="Private password"), backlog, connection, backlog.transaction
    )
    await indexer.refresh()
    assert task.id in indexer.states[backlog.principal].indexes[0].ids
    assert task.id not in indexer.states[bob.principal].indexes[0].ids
    await operations.project_share(
        operations.ShareProject(project=project.full_name, principal="bob", role="reader"),
        backlog,
        connection,
        backlog.transaction,
    )
    await indexer.refresh()
    assert task.id in indexer.states[bob.principal].indexes[0].ids
    # Even before the next refresh, revoked access must filter the old vectors.
    with sqlite3.connect(backlog.database.path) as external:
        external.execute(
            "DELETE FROM project_members WHERE project_id = ? AND principal = ?",
            (project.id, bob.principal),
        )
    result = await embeddings.search(
        embeddings.Search(query="credentials", all_computers=True, min_score=-1),
        bob,
        connection,
        bob.transaction,
    )
    assert result.tasks == []
    await indexer.refresh()
    assert task.id not in indexer.states[bob.principal].indexes[0].ids
    await backlog.delete(project, connection, backlog.transaction)
    await indexer.refresh()
    assert task.id not in indexer.states[backlog.principal].indexes[0].ids
    assert project.id not in indexer.states[backlog.principal].indexes[3].ids


async def test_members_include_only_accessible_project_bindings(
    backlog: Backlog, tmp_path: Path
) -> None:
    for directory in ("alice/work", "alice/private", "bob/work"):
        (tmp_path / directory).mkdir(parents=True)
    app = httpd.application(backlog, basic=["alice:password", "bob:password", "eve:password"])
    async with TestServer(app) as server, account(server, "alice") as alice:
        shared = await call(alice, "project_save", root=str(tmp_path / "alice/work"), name="Shared")
        name = shared["full_name"]
        await call(alice, "project_save", root=str(tmp_path / "alice/private"), name="Private")
        granted = await call(alice, "project_share", project=name, principal="bob", role="reader")
        assert any(root.endswith("/alice/work") for root in granted["roots"])
        async with account(server, "bob") as bob:
            await call(bob, "project_save", project=name, root=str(tmp_path / "bob/work"))
            shown = await call(bob, "project_members", project=name)
            assert any(
                root.startswith("alice@") and root.endswith("/alice/work")
                for root in shown["roots"]
            )
            assert any(
                root.startswith("bob@") and root.endswith("/bob/work") for root in shown["roots"]
            )
            assert not any(root.endswith("/alice/private") for root in shown["roots"])
            refused = await bob.call_tool("project_share", {"project": name, "principal": "eve"})
            assert refused.is_error
        async with account(server, "eve") as eve:
            refused = await eve.call_tool("project_members", {"project": name})
            assert refused.is_error
