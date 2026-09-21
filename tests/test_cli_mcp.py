"""CLI operations cross a real MCP transport and never open client-side storage."""

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestServer

from backlog_mcp import cli
from backlog_mcp import client as transport
from backlog_mcp.backlog import Backlog
from backlog_mcp.models import Status


class RemoteRunner:
    def __init__(self, root: Path, endpoint: str, capture: pytest.CaptureFixture[str]) -> None:
        self.root = root
        self.endpoint = endpoint
        self.capture = capture
        self.database = root.parent / "unused.sqlite3"
        self.flags = [
            "--database",
            str(self.database),
            "--state",
            str(root.parent / "client-state.json"),
            "--root",
            str(root),
            "--computer",
            "client",
            "--url",
            endpoint,
            "--user",
            "tester",
            "--password",
            "test-password",
        ]

    async def __call__(self, *arguments: str) -> tuple[int, str]:
        code = await asyncio.to_thread(cli.main, [*self.flags, *arguments])
        output = self.capture.readouterr()
        assert not self.database.exists()
        return code, output.out + output.err


@pytest.fixture
async def remote(
    backlog: Backlog,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[RemoteRunner]:
    monkeypatch.setattr(cli.paths, "config_files", list)
    monkeypatch.setattr(
        cli.Command,
        "registration",
        lambda self, computer, root, projects: cli.Registration(name=root.name),
    )
    for name in [key for key in os.environ if key.startswith("BACKLOG_MCP_")]:
        monkeypatch.delenv(name)
    root = tmp_path / "client-workspace"
    (root / ".git").mkdir(parents=True)

    def forbidden(options: cli.CLI) -> Backlog:
        pytest.fail("a CLI client must not build a local backlog")

    monkeypatch.setattr(cli, "build", forbidden)
    async with TestServer(
        cli.httpd.application(
            backlog,
            console=False,
            user="tester",
            password="test-password",
        )
    ) as server:
        yield RemoteRunner(root, str(server.make_url("/mcp")), capsys)


async def test_http_operations_use_server_storage(remote: RemoteRunner, backlog: Backlog) -> None:
    code, output = await remote("add", "Remote task", "-b", "Original requirement")
    assert code == 0 and "added #1 to client-workspace" in output
    task = await backlog.store.task(1)
    assert task is not None and task.title == "Remote task"
    project = await backlog.store.project_by_slug("client-workspace")
    assert project is not None
    assert [(root.computer, root.root) for root in project.roots] == [("client", str(remote.root))]
    assert "Remote task" in (await remote("list"))[1]
    assert (await remote("close", "1"))[0] == 0
    task = await backlog.store.task(1)
    assert task is not None and task.status == Status.DONE
    assert (await backlog.store.comments(task_id=task.id))[0].body == "Status changed: todo → done"
    assert (await remote("note", "add", "Constraint", "-b", "Keep history"))[0] == 0
    assert (await remote("note", "append", "1", "Use MCP"))[0] == 0
    assert "Use MCP" in (await remote("note", "show", "1"))[1]
    assert "Constraint" in (await remote("note", "list"))[1]
    assert (await remote("stats"))[0] == 0
    assert (await remote("show", "999")) == (1, "no task #999\n")


async def test_aliases_route_tasks_notes_and_projects(
    remote: RemoteRunner, backlog: Backlog
) -> None:
    assert (await remote("ad", "Alias task", "-t", "cli"))[0] == 0
    assert "Alias task" in (await remote("ls"))[1]
    assert (await remote("co", "1", "Alias comment"))[0] == 0
    assert "Alias comment" in (await remote("cm", "1"))[1]
    assert "Alias comment" in (await remote("sh", "1"))[1]
    assert (await remote("st", "1"))[0] == 0
    task = await backlog.store.task(1)
    assert task is not None and task.status == Status.DOING
    assert (await remote("cl", "1"))[0] == 0
    task = await backlog.store.task(1)
    assert task is not None and task.status == Status.DONE

    assert (await remote("no", "na", "Alias note", "-b", "Original text"))[0] == 0
    assert (await remote("note", "np", "1", "More text"))[0] == 0
    assert "More text" in (await remote("no", "show", "1"))[1]
    assert "Alias note" in (await remote("no", "nl"))[1]
    assert (await remote("no", "nd", "1"))[0] == 0
    assert await backlog.store.note(1) is None

    code, output = await remote("pr", "pu", "client-workspace", "--preferred-language", "ru")
    assert code == 0 and "preferred language = ru" in output
    project = await backlog.store.project_by_slug("client-workspace")
    assert project is not None and project.preferred_language == "ru"


async def test_remote_scope_uses_nested_local_root_and_explicit_override(
    remote: RemoteRunner,
    backlog: Backlog,
) -> None:
    await remote("add", "Parent task")
    nested = remote.root / "nested"
    (nested / ".git").mkdir(parents=True)
    assert (await remote("--root", str(nested), "list"))[0] == 0
    assert len(await backlog.store.projects(computer="client")) == 1
    other = remote.root.parent / "other"
    other.mkdir(exist_ok=True)
    assert (await remote("--root", str(other), "add", "Other task"))[0] == 0
    code, output = await remote("--url", remote.endpoint + "?project=other", "list")
    assert code == 0 and "Other task" in output and "Parent task" not in output
    code, output = await remote(
        "--url",
        remote.endpoint + "?project=other",
        "--project",
        "client-workspace",
        "list",
    )
    assert code == 0 and "Parent task" in output and "Other task" not in output
    code, output = await remote("--project", "./nested", "list")
    assert code == 0 and "Parent task" in output


async def test_configured_endpoint_does_not_fall_back(remote: RemoteRunner) -> None:
    code, _ = await remote("--password", "wrong", "add", "Must not exist")
    assert code != 0
    code, _ = await remote("--url", "http://127.0.0.1:1/mcp", "add", "Must not exist")
    assert code != 0
    code, _ = await remote("--url", "not-a-url", "list")
    assert code != 0


async def test_endpoint_config_environment_and_flag_precedence(
    remote: RemoteRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "client.toml"
    config.write_text(f'url = "{remote.endpoint}"\nuser = "tester"\npassword = "test-password"\n')
    flags = remote.flags[: remote.flags.index("--url")]
    assert (
        await asyncio.to_thread(cli.main, [*flags, "--config", str(config), "add", "Configured"])
        == 0
    )
    monkeypatch.setenv("BACKLOG_MCP_URL", "http://127.0.0.1:1/mcp")
    assert await asyncio.to_thread(cli.main, [*flags, "--config", str(config), "list"]) != 0
    assert (await remote("--config", str(config), "list"))[0] == 0
    assert not remote.database.exists()


async def test_remote_confirmations_are_answered_in_the_terminal(
    remote: RemoteRunner,
    monkeypatch: pytest.MonkeyPatch,
    backlog: Backlog,
) -> None:
    await remote("note", "add", "Delete me")
    monkeypatch.setattr(cli.Confirm, "ask", lambda *args, **kwargs: False)
    assert (await remote("projects", "delete", "client-workspace")) == (1, "nothing deleted\n")
    assert await backlog.store.note(1) is not None
    assert (await remote("projects", "delete", "client-workspace", "--yes"))[0] == 0
    assert await backlog.store.note(1) is None


async def test_edit_replaces_description_and_preserves_comments(
    remote: RemoteRunner,
    backlog: Backlog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await remote("add", "Original", "-b", "Original requirement")

    def edit(text: str) -> str:
        assert "Original requirement" in text
        return text.replace("Title: Original", "Title: Renamed").replace(
            "Original requirement", "Revised requirement"
        )

    assert (await remote("comment", "1", "Progress update"))[0] == 0
    monkeypatch.setattr(cli, "compose", edit)
    assert (await remote("edit", "1"))[0] == 0
    task = await backlog.store.task(1)
    assert task is not None and task.title == "Renamed"
    assert task.body == "Revised requirement"
    assert (await backlog.store.comments(task_id=1))[0].body == "Progress update"
    assert (await remote("delete", "1"))[0] == 0
    task = await backlog.store.task(1)
    assert task is not None and task.status == Status.DELETED
    assert task.body == "Revised requirement"
    assert "Renamed" not in (await remote("list", "--every"))[1]


async def test_move_accepts_local_relative_destination(
    remote: RemoteRunner,
    backlog: Backlog,
) -> None:
    await remote("add", "Move me")
    other = remote.root.parent / "other"
    assert (await remote("--root", str(other), "current"))[0] == 0
    assert (await remote("move", "1", "../other"))[0] == 0
    task = await backlog.store.task(1)
    assert task is not None and task.project == "other"


async def test_remote_skills_and_broad_listings_do_not_register_projects(
    remote: RemoteRunner,
    backlog: Backlog,
) -> None:
    assert (await remote("projects"))[0] == 0
    assert (await remote("list", "--all-projects"))[0] == 0
    assert "planning" in (await remote("skills"))[1]
    assert "task_update" in (await remote("skills", "planning"))[1]
    assert await backlog.store.projects(computer="client") == []


async def test_watch_outlives_the_regular_request_timeout(remote: RemoteRunner) -> None:
    watching = asyncio.create_task(remote("--timeout", "0.2", "watch", "-n", "1"))
    await asyncio.sleep(0.4)
    assert not watching.done()
    await remote("add", "Watched after timeout")
    assert (await asyncio.wait_for(watching, 5))[0] == 0


def test_local_fallback_uses_a_child_and_closes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.paths, "config_files", list)
    monkeypatch.setenv("BACKLOG_MCP_PROTOCOL", "http")
    processes: list[asyncio.subprocess.Process] = []
    spawn = asyncio.create_subprocess_exec

    async def tracked(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process

    def forbidden(options: cli.CLI) -> Backlog:
        pytest.fail("a local CLI client must access storage through its child")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", tracked)
    monkeypatch.setattr(cli, "build", forbidden)
    flags = [
        "--url",
        "",
        "--database",
        str(tmp_path / "db.sqlite3"),
        "--state",
        str(tmp_path / "state.json"),
        "--root",
        str(tmp_path),
    ]
    assert cli.main([*flags, "init", tmp_path.name]) == 0
    assert len(processes) == 1 and processes[0].returncode == 0
    assert cli.main([*flags, "add", "Through stdio"]) == 0
    assert len(processes) == 2 and processes[1].returncode == 0
    assert cli.main([*flags, "show", "404"]) == 1
    assert len(processes) == 3 and processes[2].returncode == 0


@pytest.mark.parametrize("script", ["import time; time.sleep(60)", "raise SystemExit(7)"])
def test_stdio_startup_failure_reaps_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: str,
) -> None:
    monkeypatch.setattr(cli.paths, "config_files", list)
    monkeypatch.setattr(transport, "local_command", lambda *args: [sys.executable, "-c", script])
    processes: list[asyncio.subprocess.Process] = []
    spawn = asyncio.create_subprocess_exec

    async def tracked(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", tracked)
    assert (
        cli.main(
            [
                "--url",
                "",
                "--state",
                str(tmp_path / "state.json"),
                "--timeout",
                "0.1",
                "current",
            ]
        )
        != 0
    )
    assert len(processes) == 1 and processes[0].returncode is not None


async def test_comment_search_and_complete_history_over_http(remote: RemoteRunner) -> None:
    await remote("add", "Task", "-b", "Definition")
    await remote("comment", "1", "uniquemarker first")
    await remote("comment", "1", "Second comment")
    code, output = await remote("search", "uniquemarker", "--kind", "comments", "--min-score", "1")
    assert code == 0 and "1 comment match(es)" in output
    assert "uniquemarker" in output
    code, output = await remote("search", "uniquemarker", "--kind", "tasks", "--min-score", "1")
    assert code == 0 and "0 task match(es)" in output and "comment match(es)" not in output
    code, output = await remote("comments", "1")
    assert code == 0 and "2 comments and history entries" in output
    assert "uniquemarker" in output and "Second comment" in output
