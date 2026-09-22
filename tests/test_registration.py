"""Initial CLI registration over both supported MCP transports."""

import asyncio
import io
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer

from backlog_mcp import backlog as scope
from backlog_mcp import cli
from backlog_mcp import server as operations
from backlog_mcp.auth import user_identity
from backlog_mcp.backlog import Backlog


class TerminalInput(io.StringIO):
    def isatty(self) -> bool:
        return True


class RegistrationRunner:
    def __init__(
        self, backlog: Backlog, root: Path, url: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.backlog = backlog
        self.root = root
        self.capsys = capsys
        self.flags = [
            "--url",
            url,
            "--database",
            str(backlog.database.path),
            "--state",
            str(root.parent / "client-state.json"),
            "--computer",
            "client",
            "--root",
            str(root),
        ]

    async def __call__(self, *args: str) -> tuple[int, str]:
        result = await asyncio.to_thread(cli.main, [*self.flags, *args])
        captured = self.capsys.readouterr()
        return result, captured.out + captured.err


@pytest.fixture(params=["stdio", "http"])
async def registration(
    request: pytest.FixtureRequest,
    backlog: Backlog,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> AsyncIterator[RegistrationRunner]:
    monkeypatch.setattr(cli.paths, "config_files", list)
    for name in [key for key in os.environ if key.startswith("BACKLOG_MCP_")]:
        monkeypatch.delenv(name)
    root = tmp_path / "client-directory"
    (root / ".git").mkdir(parents=True)
    if request.param == "http":
        async with TestServer(cli.httpd.application(backlog, console=False)) as server:
            yield RegistrationRunner(backlog, root, str(server.make_url("/mcp")), capsys)
    else:
        monkeypatch.setenv("USER", "stdio-user")
        backlog.principal = user_identity("stdio-user")
        yield RegistrationRunner(backlog, root, "", capsys)


async def test_first_use_prompts_once_and_then_runs_the_command(
    registration: RegistrationRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", TerminalInput("\n\n\nA shared project\ny\n"))
    code, output = await registration("task", "add", "First task")
    assert code == 0
    assert "Input computer name (client)" in output
    assert "Input project name (client-directory)" in output
    project = await scope.project_by_slug(
        await registration.backlog.open(), registration.backlog.actor, "client-directory"
    )
    assert project is not None and project.description == "A shared project"
    assert [(item.computer, item.root) for item in project.roots] == [
        ("client", str(registration.root))
    ]
    task = await operations.read_task(
        await registration.backlog.open(), registration.backlog.actor, 1
    )
    assert task is not None and task.project == project.full_name
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    code, output = await registration("task", "list")
    assert code == 0 and "First task" in output
    assert "Input project" not in output


async def test_custom_project_name_is_used_for_the_original_command(
    registration: RegistrationRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", TerminalInput("\n\nRelease Board\nRelease coordination\ny\n"))
    code, output = await registration("task", "add", "Ship it")
    assert code == 0 and "release-board" in output
    project = await scope.project_by_slug(
        await registration.backlog.open(), registration.backlog.actor, "release-board"
    )
    assert project is not None and project.name == "Release Board"
    assert (
        await scope.project_by_slug(
            await registration.backlog.open(), registration.backlog.actor, "client-directory"
        )
        is None
    )


async def test_existing_project_on_another_computer_is_reused(
    registration: RegistrationRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = await registration.backlog.save(
        await registration.backlog.open(),
        registration.backlog.transaction,
        root=registration.backlog.cwd,
        name="client-directory",
        description="Keep this description",
    )
    monkeypatch.setattr(sys, "stdin", TerminalInput("\n\n\ny\n"))
    code, output = await registration("task", "add", "Same board")
    assert code == 0 and "Bind this directory" in output, output
    projects = await scope.read_projects(
        await registration.backlog.open(), registration.backlog.actor
    )
    assert len(projects) == 1 and projects[0].id == existing.id
    assert projects[0].description == "Keep this description"
    assert {item.computer for item in projects[0].roots} == {"here", "client"}
    task = await operations.read_task(
        await registration.backlog.open(), registration.backlog.actor, 1
    )
    assert task is not None and task.project_id == existing.id


@pytest.mark.parametrize("answer", ["\nDetails\nn\n", ""])
async def test_cancel_or_eof_creates_no_project_or_task(
    registration: RegistrationRunner,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
) -> None:
    monkeypatch.setattr(sys, "stdin", TerminalInput("\n\n" + answer))
    code, output = await registration("task", "add", "Not created")
    assert code == 1 and "registration cancelled" in output
    assert (
        await scope.read_projects(
            await registration.backlog.open(), registration.backlog.actor, computer="client"
        )
        == []
    )
    assert (
        await operations.read_task(await registration.backlog.open(), registration.backlog.actor, 1)
        is None
    )


async def test_noninteractive_first_use_requires_explicit_init(
    registration: RegistrationRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    code, output = await registration("task", "add", "Not created")
    assert code == 2 and "backlog init NAME" in output
    assert (
        await scope.read_projects(
            await registration.backlog.open(), registration.backlog.actor, computer="client"
        )
        == []
    )
    assert (await registration("init", "Automation", "--description", "Scripted work"))[0] == 0
    assert (await registration("task", "add", "Created explicitly"))[0] == 0
    project = await scope.project_by_slug(
        await registration.backlog.open(), registration.backlog.actor, "automation"
    )
    assert project is not None and project.description == "Scripted work"


async def test_init_requires_explicit_existing_flag_to_reuse_a_name(
    registration: RegistrationRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = await registration.backlog.save(
        await registration.backlog.open(),
        registration.backlog.transaction,
        root=registration.backlog.cwd,
        name="Shared",
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    code, output = await registration("init", "Shared")
    assert code == 1 and f"--existing {existing.full_name}" in output
    assert (
        len(
            await scope.read_projects(await registration.backlog.open(), registration.backlog.actor)
        )
        == 1
    )
    assert (await registration("init", "--existing", existing.slug))[0] == 0
    projects = await scope.read_projects(
        await registration.backlog.open(), registration.backlog.actor
    )
    assert len(projects) == 1 and len(projects[0].roots) == 2
    assert (await registration("init", "--existing", existing.slug))[0] == 0
    assert (await registration("init", "Different name"))[0] == 1
    assert (
        len(
            await scope.read_projects(await registration.backlog.open(), registration.backlog.actor)
        )
        == 1
    )


async def test_nested_directory_uses_checkout_root_as_default(
    registration: RegistrationRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested = registration.root / "src"
    nested.mkdir()
    monkeypatch.setattr(sys, "stdin", TerminalInput("\n\n\n\ny\n"))
    code, output = await registration("--root", str(nested), "task", "list")
    assert code == 0 and "Input project name (client-directory)" in output
    project = await scope.project_by_slug(
        await registration.backlog.open(), registration.backlog.actor, "client-directory"
    )
    assert project is not None and project.roots[0].root == str(registration.root)


async def test_edit_all_fields_and_remember_computer(
    registration: RegistrationRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = registration.root.parent / "selected-directory"
    directory.mkdir()
    monkeypatch.setattr(
        sys, "stdin", TerminalInput(f"laptop\n{directory}\nChosen Board\nChosen description\ny\n")
    )
    code, output = await registration("task", "add", "Chosen task")
    assert code == 0 and "chosen-board" in output
    project = await scope.project_by_slug(
        await registration.backlog.open(), registration.backlog.actor, "chosen-board"
    )
    assert project is not None and project.description == "Chosen description"
    assert [(item.computer, item.root) for item in project.roots] == [("laptop", str(directory))]
    task = await operations.read_task(
        await registration.backlog.open(), registration.backlog.actor, 1
    )
    assert task is not None and task.project_id == project.id
    state = cli.State(registration.root.parent / "client-state.json")
    assert state.computer == "laptop"
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    # An empty flag allows the saved computer name to take effect.
    code, output = await registration("--computer", "", "--root", str(directory), "task", "list")
    assert code == 0 and "Chosen task" in output
    assert "Input computer" not in output


async def test_edited_location_reuses_its_existing_binding(
    registration: RegistrationRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = await registration.backlog.save(
        await registration.backlog.open(),
        registration.backlog.transaction,
        root=registration.backlog.cwd,
        name="Existing board",
        description="Unchanged",
    )
    baseline = {
        item.id
        for item in await scope.read_projects(
            await registration.backlog.open(), registration.backlog.actor
        )
    }
    monkeypatch.setattr(sys, "stdin", TerminalInput(f"here\n{registration.backlog.cwd}\ny\n"))
    code, output = await registration("task", "add", "Existing scope")
    assert code == 0 and "Use this project?" in output
    assert "Input project name" not in output
    assert {
        item.id
        for item in await scope.read_projects(
            await registration.backlog.open(), registration.backlog.actor
        )
    } == baseline
    project = await scope.project_by_slug(
        await registration.backlog.open(), registration.backlog.actor, existing.slug
    )
    assert project is not None and project.description == "Unchanged"
    assert len(project.roots) == 1
    task = await operations.read_task(
        await registration.backlog.open(), registration.backlog.actor, 1
    )
    assert task is not None and task.project_id == existing.id


async def test_cancel_does_not_remember_edited_computer(
    registration: RegistrationRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = cli.State(registration.root.parent / "client-state.json")
    state.update(computer="original")
    monkeypatch.setattr(sys, "stdin", TerminalInput("edited\n\n\nDescription\nn\n"))
    code, output = await registration("init")
    assert code == 1 and "registration cancelled" in output
    assert state.computer == "original"
    assert (
        await scope.read_projects(
            await registration.backlog.open(), registration.backlog.actor, computer="edited"
        )
        == []
    )


async def test_invalid_location_fields_can_be_corrected(
    registration: RegistrationRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = registration.root / "does-not-exist"
    monkeypatch.setattr(
        sys, "stdin", TerminalInput(f"invalid computer\nvalid\n{missing}\n\n\n\ny\n")
    )
    code, output = await registration("init")
    assert code == 0
    assert "invalid computer name" in output and "No such directory" in output
    project = await scope.project_by_slug(
        await registration.backlog.open(), registration.backlog.actor, "client-directory"
    )
    assert project is not None and project.roots[0].computer == "valid"
