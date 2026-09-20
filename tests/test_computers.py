"""Several computers in one backlog: what each one sees, and what it is told about the others."""

from pathlib import Path

import pytest
from aiohttp_tiny_mcp.client_base import BaseClient
from aiohttp_tiny_mcp.models import CallToolResult, TextContent
from test_server import call

from backlog_mcp.backlog import Backlog, UnknownProject
from backlog_mcp.computer import PATTERN, check, hostname
from backlog_mcp.models import Origin
from backlog_mcp.state import State
from backlog_mcp.store import Store


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


def test_pins_are_kept_per_computer(tmp_path: Path) -> None:
    state = State(tmp_path / "state.json")
    state.pin("desk", "work")
    state.pin("laptop", "app")
    assert (state.pinned("desk"), state.pinned("laptop")) == ("work", "app")
    state.pin("desk", None)
    assert state.pinned("desk") is None
    assert state.pinned("laptop") == "app"
    state.remember("desk", "work")
    state.remember("laptop", "app")
    assert (state.last_project("desk"), state.last_project("laptop")) == ("work", "app")
    assert state.pinned("nowhere") is None


async def test_one_project_has_roots_on_several_computers(tmp_path: Path) -> None:
    store = Store(tmp_path / "backlog.sqlite3", computer="desk")
    root = "/home/me/work"
    one = await store.add_project(slug="work", name="work", root=root, computer="desk")
    two = await store.add_root(one.id, computer="laptop", root=root)
    assert await store.computers() == ["desk", "laptop"]
    assert [item.slug for item in await store.projects()] == ["work"]
    assert [item.slug for item in await store.projects(computer="laptop")] == ["work"]
    found = await store.project_by_root(root, computer="laptop")
    assert found is not None and found.id == one.id == two.id
    assert (await store.project_for_path(Path(root) / "src", computer="desk")).id == one.id

    await store.add_task(project_id=one.id, title="Here", tags=["desk"])
    await store.add_task(project_id=two.id, title="There", tags=["laptop"])
    assert [item.title for item in await store.tasks(computer="laptop")] == ["Here", "There"]
    assert await store.count_tasks(computer="desk") == 2
    assert await store.count_tasks() == 2
    assert await store.tags(computer="laptop") == ["desk", "laptop"]
    assert await store.group("status", computer="laptop") == {"todo": 2}
    assert [item.title for item in await store.search("There", computer="desk")] == ["There"]
    assert [item.title for item in await store.search("There", computer="laptop")] == ["There"]

    #: Shared notes cross computers, the others stay on theirs.
    await store.add_note(project_id=one.id, title="local", body="alpha")
    await store.add_note(project_id=two.id, title="shared", body="bravo", shared=True)
    assert {item.title for item in await store.notes(computer="desk")} == {"local", "shared"}
    assert await store.count_notes(computer="laptop") == 2
    assert [item.title for item in await store.search_notes("bravo", computer="desk")] == ["shared"]
    laptop_notes = await store.search_notes("alpha", computer="laptop")
    assert [item.title for item in laptop_notes] == ["local"]
    await store.close()


async def test_a_view_of_another_computer_never_touches_this_file_system(tmp_path: Path) -> None:
    root = tmp_path / "work"
    (root / ".git").mkdir(parents=True)
    backlog = Backlog(
        Store(tmp_path / "backlog.sqlite3"),
        State(tmp_path / "state.json"),
        cwd=root,
        computer="desk",
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
        await backlog.save(root=str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="name the project, or give its root"):
        await laptop.save()
    with pytest.raises(ValueError, match="absolute path"):
        await laptop.save(root="relative/app")
    app = await laptop.save(root="/srv/app/./", name="App")
    assert (app.roots[0].computer, app.roots[0].root, app.slug) == ("laptop", "/srv/app", "app")
    #: The same path on this computer is a different project, once the directory exists.
    same = tmp_path / "srv" / "app"
    same.mkdir(parents=True)
    twin = await laptop.save(root=str(same), name="Twin")
    assert (await backlog.create(same)).id != twin.id

    fixed = backlog.view(computer="laptop", project="app")
    chosen, origin = await fixed.current()
    assert (chosen.id, origin) == (app.id, Origin.ARGUMENT)
    backlog.state.pin("laptop", "app")
    pinned, origin = await backlog.view(computer="laptop").current()
    assert (pinned.id, origin) == (app.id, Origin.PINNED)
    assert backlog.state.pinned("desk") is None

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
    await backlog.store.close()


async def test_tools_tell_the_computers_apart(client: BaseClient, backlog: Backlog) -> None:
    current = await call(client, "project_current")
    assert current["computer"] == "here"
    assert current["computers"] == ["here"]
    assert current["project"]["here"] is True
    mine = current["project"]["slug"]

    app = await backlog.view(computer="laptop").save(root="/srv/app", name="App")
    await backlog.store.add_task(project_id=app.id, title="Deploy on the laptop")
    assert (await call(client, "project_current"))["computers"] == ["here", "laptop"]

    listed = await call(client, "project_list")
    assert [item["slug"] for item in listed["items"]] == [mine]
    everything = await call(client, "project_list", all_computers=True)
    assert {item["slug"]: item["here"] for item in everything["items"]} == {
        mine: True,
        "app": False,
    }
    theirs = await call(client, "project_list", computer="laptop")
    assert theirs["items"][0]["roots"] == [{"computer": "laptop", "root": "/srv/app"}]

    local = await call(client, "task_add", title="Local work")
    assert local["project"] == mine
    #: A listing of one project names that project's computer, not the filter given.
    assert (await call(client, "task_list"))["computer"] is None
    assert (await call(client, "stats"))["computer"] is None
    assert (await call(client, "task_list", all_projects=True))["count"] == 1
    away = await call(client, "task_list", computer="laptop")
    assert away["computer"] == "laptop"
    assert (await call(client, "task_list", all_computers=True))["count"] == 2

    #: Semantic matches score anything above zero, so only exact matches count here.
    assert (await call(client, "search", query="laptop", min_score=1))["count"] == 0
    found = await call(client, "search", query="laptop", all_computers=True)
    assert found["computer"] is None
    assert found["tasks"][0]["project"] == "app"

    counted = await call(client, "stats", computer="laptop")
    assert (counted["computer"], counted["total"]) == ("laptop", 1)
    scoped_task = await call(client, "task_list", project="app", computer="laptop")
    assert (scoped_task["project"], scoped_task["computer"], scoped_task["count"]) == (
        "app",
        "laptop",
        1,
    )
    scoped_stats = await call(client, "stats", project="app", computer="laptop")
    assert (scoped_stats["project"], scoped_stats["computer"], scoped_stats["total"]) == (
        "app",
        "laptop",
        1,
    )
    scoped_search = await call(client, "search", query="laptop", project="app", computer="laptop")
    assert (scoped_search["project"], scoped_search["computer"]) == ("app", "laptop")
    assert [item["id"] for item in scoped_search["tasks"]] == [away["items"][0]["id"]]
    assert counted["this_computer"] == "here"
    assert (await call(client, "stats", all_computers=True))["total"] == 2

    result = await client.call_tool("task_list", {"computer": "nowhere"})
    assert isinstance(result, CallToolResult) and result.is_error
    assert isinstance(result.content[0], TextContent)
    assert "known computers are here, laptop" in result.content[0].text
