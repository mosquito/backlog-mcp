"""Several computers in one backlog: what each one sees, and what it is told about the others."""

import json
from pathlib import Path

import pytest
from aiohttp_tiny_mcp.client.base import BaseClient
from aiohttp_tiny_mcp.protocol.models import CallToolResult, TextContent
from helpers import mutate
from test_server import call

from backlog_mcp import backlog as scope
from backlog_mcp import server as operations
from backlog_mcp.backlog import Backlog, UnknownProject
from backlog_mcp.computer import PATTERN, check, hostname
from backlog_mcp.models import Origin
from backlog_mcp.state import State


def test_computer_names_are_one_plain_word() -> None:
    assert check(" MacBook ") == "MacBook"
    assert check("desk-01.local") == "desk-01.local"
    for wrong in ("", "two words", "-dash", "slash/", "x" * 64):
        with pytest.raises(ValueError, match="invalid computer name"):
            check(wrong)
    assert PATTERN.match(hostname())


def test_the_host_name_is_remembered_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = State(tmp_path / "state.json")
    #: An explicit name is used as given and changes nothing on disk.
    assert state.computer_name(" laptop ") == "laptop"
    assert state.computer is None
    monkeypatch.setattr("backlog_mcp.state.hostname", lambda: "first-host")
    assert state.computer_name() == "first-host"
    monkeypatch.setattr("backlog_mcp.state.hostname", lambda: "renamed-host")
    assert state.computer_name() == "first-host"
    assert state.computer == "first-host"


def test_legacy_pins_leave_the_state_on_the_next_write(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {"version": 2, "computer": "desk", "pins": {"desk": "work"}, "last": {"desk": "work"}}
        )
    )
    state = State(path)
    assert state.computer_name() == "desk"
    state.update(computer="desk")
    assert set(state.read()) == {"version", "computer", "updated_at"}


async def test_one_project_has_roots_on_several_computers(tmp_path: Path) -> None:
    store = Backlog(tmp_path / "backlog.sqlite3", computer="desk")
    root = "/home/me/work"
    one = await mutate(
        scope.add_project(
            await store.open(), store.actor, slug="work", name="work", root=root, computer="desk"
        ),
        store.transaction,
    )
    two = await mutate(
        scope.add_root(await store.open(), store.actor, one.id, computer="laptop", root=root),
        store.transaction,
    )
    assert await scope.computers(await store.open(), store.actor) == ["desk", "laptop"]
    assert [item.slug for item in await scope.read_projects(await store.open(), store.actor)] == [
        "work"
    ]
    assert [
        item.slug
        for item in await scope.read_projects(await store.open(), store.actor, computer="laptop")
    ] == ["work"]
    found = await scope.project_by_root(await store.open(), store.actor, root, computer="laptop")
    assert found is not None and found.id == one.id == two.id
    assert (
        await scope.project_for_path(
            await store.open(), store.actor, Path(root) / "src", computer="desk"
        )
    ).id == one.id

    await mutate(
        operations.add_task(
            await store.open(), store.actor, project_id=one.id, title="Here", tags=["desk"]
        ),
        store.transaction,
    )
    await mutate(
        operations.add_task(
            await store.open(), store.actor, project_id=two.id, title="There", tags=["laptop"]
        ),
        store.transaction,
    )
    assert [
        item.title
        for item in await operations.read_tasks(await store.open(), store.actor, computer="laptop")
    ] == ["Here", "There"]
    assert await operations.count_tasks(await store.open(), store.actor, computer="desk") == 2
    assert await operations.count_tasks(await store.open(), store.actor) == 2
    assert await operations.read_tags(await store.open(), store.actor, computer="laptop") == [
        "desk",
        "laptop",
    ]
    assert await operations.group(await store.open(), store.actor, "status", computer="laptop") == {
        "todo": 2
    }
    assert [
        item.title
        for item in await operations.search_tasks(
            await store.open(), store.actor, "There", computer="desk"
        )
    ] == ["There"]
    assert [
        item.title
        for item in await operations.search_tasks(
            await store.open(), store.actor, "There", computer="laptop"
        )
    ] == ["There"]

    #: Shared notes cross computers, the others stay on theirs.
    await mutate(
        operations.add_note(
            await store.open(), store.actor, project_id=one.id, title="local", body="alpha"
        ),
        store.transaction,
    )
    await mutate(
        operations.add_note(
            await store.open(),
            store.actor,
            project_id=two.id,
            title="shared",
            body="bravo",
            shared=True,
        ),
        store.transaction,
    )
    assert {
        item.title
        for item in await operations.read_notes(await store.open(), store.actor, computer="desk")
    } == {"local", "shared"}
    assert await operations.count_notes(await store.open(), store.actor, computer="laptop") == 2
    assert [
        item.title
        for item in await operations.search_notes(
            await store.open(), store.actor, "bravo", computer="desk"
        )
    ] == ["shared"]
    laptop_notes = await operations.search_notes(
        await store.open(), store.actor, "alpha", computer="laptop"
    )
    assert [item.title for item in laptop_notes] == ["local"]
    await store.close()


async def test_a_view_of_another_computer_never_touches_this_file_system(tmp_path: Path) -> None:
    root = tmp_path / "work"
    (root / ".git").mkdir(parents=True)
    backlog = Backlog(
        tmp_path / "backlog.sqlite3", State(tmp_path / "state.json"), cwd=root, computer="desk"
    )
    local, _ = await backlog.current()
    assert local.roots[0].computer == "desk"

    laptop = backlog.view(computer="laptop")
    assert laptop.remote is True and laptop.computer == "laptop"
    assert backlog.view().remote is False
    assert await laptop.scope() == (None, Origin.NONE)
    with pytest.raises(UnknownProject, match="project=<slug>"):
        await laptop.current()
    #: This computer checks that the directory exists; another one cannot.
    with pytest.raises(ValueError, match="no such directory"):
        await backlog.save(
            root=str(tmp_path / "missing"),
            connection=await backlog.open(),
            transaction=backlog.transaction,
        )
    with pytest.raises(ValueError, match="name the project, or give its root"):
        await laptop.save(await laptop.open(), laptop.transaction)
    with pytest.raises(ValueError, match="absolute path"):
        await laptop.save(await laptop.open(), laptop.transaction, root="relative/app")
    app = await laptop.save(await laptop.open(), laptop.transaction, root="/srv/app/./", name="App")
    assert (app.roots[0].computer, app.roots[0].root, app.slug) == ("laptop", "/srv/app", "app")
    #: The same path on this computer is a different project, once the directory exists.
    same = tmp_path / "srv" / "app"
    same.mkdir(parents=True)
    twin = await laptop.save(await laptop.open(), laptop.transaction, root=str(same), name="Twin")
    assert (await backlog.create(same)).id != twin.id

    fixed = backlog.view(computer="laptop", project="app")
    chosen, origin = await fixed.current()
    assert (chosen.id, origin) == (app.id, Origin.ARGUMENT)

    #: A slug reaches any computer, a path only this one.
    assert (await backlog.lookup("twin")).roots[0].computer == "laptop"
    assert (await backlog.lookup(str(same))).roots[0].computer == "desk"

    assert (await backlog.reach()).project == local
    assert (await backlog.reach(all_projects=True)).computer == "desk"
    assert (await backlog.reach(computer="laptop")).computer == "laptop"
    assert (await backlog.reach(project="app", computer="laptop")).project == app
    with pytest.raises(UnknownProject, match="has no binding"):
        await backlog.reach(project="app", computer="desk")
    assert (await backlog.reach(all_computers=True)).computer is None
    assert (await backlog.reach(all_computers=True)).label == "every project on every computer"
    with pytest.raises(UnknownProject, match="known computers are desk, laptop"):
        await backlog.reach(computer="nowhere")
    await backlog.close()


async def test_tools_tell_the_computers_apart(client: BaseClient, backlog: Backlog) -> None:
    saved = await call(client, "project_save", root=".")
    current = await call(client, "project_board", project=saved["full_name"])
    assert current["principal"] == "local@here"
    assert current["project"]["roots"] == [f"local@here:{backlog.cwd}"]
    mine = saved["slug"]

    app = await backlog.view(computer="laptop").save(
        await backlog.open(), backlog.transaction, root="/srv/app", name="App"
    )
    await mutate(
        operations.add_task(
            await backlog.open(), backlog.actor, project_id=app.id, title="Deploy on the laptop"
        ),
        backlog.transaction,
    )

    listed = await call(client, "project_list")
    assert [item["slug"] for item in listed["items"]] == [mine]
    everything = await call(client, "project_list", all_computers=True)
    assert {item["slug"]: item["here"] for item in everything["items"]} == {
        mine: True,
        "app": False,
    }
    theirs = await call(client, "project_list", computer="laptop")
    assert theirs["items"][0]["roots"] == ["local@laptop:/srv/app"]

    local = await call(client, "task_add", title="Local work")
    assert local["project"] == current["project"]["full_name"]
    #: A listing of one project names that project's computer, not the filter given.
    assert (await call(client, "task_list"))["principal"] == "local@unknown"
    assert (await call(client, "stats"))["principal"] == "local@unknown"
    assert (await call(client, "task_list", all_projects=True))["count"] == 1
    away = await call(client, "task_list", computer="laptop")
    assert away["principal"] == "local@laptop"
    assert (await call(client, "task_list", all_computers=True))["count"] == 2

    #: Semantic matches score anything above zero, so only exact matches count here.
    assert (await call(client, "search", query="laptop", min_score=1))["count"] == 0
    found = await call(client, "search", query="laptop", all_computers=True)
    assert found["principal"] == "local@unknown"
    assert found["tasks"][0]["project"] == "local/app"

    counted = await call(client, "stats", computer="laptop")
    assert (counted["principal"], counted["total"]) == ("local@laptop", 1)
    scoped_task = await call(client, "task_list", project="app", computer="laptop")
    assert (scoped_task["project"], scoped_task["principal"], scoped_task["count"]) == (
        "local/app",
        "local@laptop",
        1,
    )
    scoped_stats = await call(client, "stats", project="app", computer="laptop")
    assert (scoped_stats["project"], scoped_stats["principal"], scoped_stats["total"]) == (
        "local/app",
        "local@laptop",
        1,
    )
    scoped_search = await call(client, "search", query="laptop", project="app", computer="laptop")
    assert (scoped_search["project"], scoped_search["principal"]) == ("local/app", "local@laptop")
    assert [item["id"] for item in scoped_search["tasks"]] == [away["items"][0]["id"]]
    assert counted["this_principal"] == "local@here"
    assert (await call(client, "stats", all_computers=True))["total"] == 2

    result = await client.call_tool("task_list", {"computer": "nowhere"})
    assert isinstance(result, CallToolResult) and result.is_error
    assert isinstance(result.content[0], TextContent)
    assert "known computers are here, laptop" in result.content[0].text
