"""The command line: one process per call, through `main`."""

import asyncio
import json
import os
import tomllib
from collections.abc import Iterator, Sequence
from pathlib import Path

import argclass
import pytest

from backlog_mcp import cli, render
from backlog_mcp.backlog import Backlog
from backlog_mcp.models import Priority, Status
from backlog_mcp.records import Note, Task


class Runner:
    """Calls `main` with the paths of one temporary workspace already filled in."""

    def __init__(self, root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        self.root = root
        self.capsys = capsys
        self.database = root.parent / "backlog.sqlite3"

    def __call__(self, *argv: str) -> tuple[int, str]:
        code = cli.main(
            [
                "--database",
                str(self.database),
                "--root",
                str(self.root),
                *argv,
            ]
        )
        captured = self.capsys.readouterr()
        return code, captured.out + captured.err


@pytest.fixture
def run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Iterator[Runner]:
    root = tmp_path / "workspace"
    (root / ".git").mkdir(parents=True)
    #: The machine running the tests may have its own configuration and environment.
    monkeypatch.setattr(cli.paths, "config_files", list)
    monkeypatch.setattr(cli.paths, "database_path", lambda: tmp_path / "default.sqlite3")
    monkeypatch.setattr(
        cli.Command,
        "registration",
        lambda self, computer, root, projects: cli.Registration(name=root.name),
    )
    for name in [key for key in os.environ if key.startswith("BACKLOG_MCP_")]:
        monkeypatch.delenv(name)
    #: A fixed width keeps the tables and the panels off the width of whatever terminal runs this.
    for output in (render.console, render.errors):
        monkeypatch.setattr(output, "size", (120, 40))
    monkeypatch.setenv("USER", "stdio-user")
    yield Runner(root, capsys)


def test_task_link_commands(run: Runner) -> None:
    assert run("task", "add", "Consumer")[0] == 0
    assert run("task", "add", "Provider")[0] == 0
    code, output = run("task", "link", "1", "depends_on", "2", "--reason", "New schema")
    assert code == 0, output
    code, output = run("task", "show", "2")
    assert code == 0 and "blocks" in output and "Consumer" in output and "New schema" in output
    code, output = run("task", "link", "2", "blocks", "1")
    assert code == 0 and "unchanged" in output
    assert run("task", "unlink", "2", "blocks", "1")[0] == 0
    code, output = run("task", "show", "1")
    assert code == 0 and "depends_on" not in output


def task(**changes: object) -> Task:
    values: dict[str, object] = {
        "id": 7,
        "project_id": 1,
        "project": "workspace",
        "title": "Write the form",
        "body": "Some detail",
        "status": Status.DOING,
        "priority": Priority.HIGH,
        "tags": ["docs", "cli"],
        "created_at": 0.0,
        "updated_at": 0.0,
        "closed_at": None,
    }
    values.update(changes)
    return Task(**values)  # type: ignore[arg-type]


def test_no_command_prints_the_help(run: Runner) -> None:
    code, output = run()
    assert code == 0
    assert "Serve MCP, on stdio or over HTTP" in output
    assert "install" in output


def test_aliases_are_unique_and_show_the_same_help(capsys: pytest.CaptureFixture[str]) -> None:
    root = cli.CLI()
    seen: set[str] = set()

    def help_text(words: list[str]) -> str:
        with pytest.raises(SystemExit) as error:
            root.parse_args([*words, "--help"])
        assert error.value.code == 0
        return capsys.readouterr().out

    def check(parser: argclass.Parser, names: list[str], short: list[str]) -> None:
        for name, child in parser.__subparsers__.items():
            aliases = child._parser_kwargs["aliases"]
            assert len(aliases) == 1
            alias = aliases[0]
            assert len(alias) == 2 and alias.isalpha() and alias.islower()
            assert alias not in seen
            seen.add(alias)
            assert help_text([*short, alias]) == help_text([*names, name])
            check(child, [*names, name], [*short, alias])

    check(root, [], [])


def test_add_list_search_and_close_one_task(run: Runner) -> None:
    code, output = run("task", "add", "Ship", "the", "release", "-t", "ops", "-t", "urgent-ish")
    assert code == 0
    assert "added #1 to stdio-user/workspace" in output
    assert "ops, urgent-ish" in output

    code, output = run("task", "list")
    assert code == 0
    assert "1 task(s) of stdio-user/workspace" in output
    assert "Ship the release" in output

    code, output = run("search", '"rele"*')
    assert "1 task match(es)" in output
    assert "note match(es)" not in output
    #: The title column stays the title; a body excerpt would be the `match` column.
    assert "Ship the release" in output

    code, output = run("task", "close", "1")
    assert code == 0
    assert "#1 is done" in output
    assert run("task", "list")[1].count("Ship the release") == 0
    assert "Ship the release" in run("task", "list", "--every")[1]
    assert "Ship the release" in run("task", "list", "--status", "done")[1]


def test_statuses_move_back_and_forth(run: Runner) -> None:
    run("task", "add", "Move me")
    for command, expected in [
        ("start", "doing"),
        ("block", "blocked"),
        ("cancel", "cancelled"),
        ("reopen", "todo"),
    ]:
        code, output = run("task", command, "1")
        assert code == 0
        assert f"#1 is {expected}" in output
    assert run("task", "close", "999") == (1, "no task #999\n")


def test_project_language_can_be_set_changed_and_cleared(run: Runner) -> None:
    run("task", "add", "Keep this task")
    for language in ("Russian", "English"):
        code, output = run("projects", "update", "workspace", "--preferred-language", language)
        assert code == 0
        assert f"preferred language = {language}" in output
        assert language in run("board")[1]
        assert language in run("projects")[1]
    code, output = run("projects", "update", "workspace", "--clear-preferred-language")
    assert code == 0
    assert "preferred language = none" in output
    assert "English" not in run("board")[1]
    assert "Keep this task" in run("task", "show", "1")[1]


def test_project_language_update_requires_one_option(run: Runner) -> None:
    for options in ((), ("--preferred-language", "ru", "--clear-preferred-language")):
        code, output = run("projects", "update", "workspace", *options)
        assert code == 1
        assert "choose --preferred-language or --clear-preferred-language" in output


def test_show_prints_the_body_and_the_fields(run: Runner) -> None:
    run("task", "add", "Explain", "-b", "The **long** description.", "--priority", "urgent")
    code, output = run("task", "show", "1")
    assert code == 0
    assert "#1 Explain" in output
    assert "The long description." in output
    assert "**long**" not in output
    assert "urgent" in output
    assert run("task", "show", "42") == (1, "no task #42\n")


def test_show_renders_comments_separately(run: Runner) -> None:
    run("task", "add", "Explain", "-b", "The description")
    code, output = run("task", "comment", "1", "The **comment**")
    assert code == 0 and "The comment" in output
    code, output = run("task", "show", "1")
    assert code == 0
    assert "The description" in output and "The comment" in output
    assert "comments and history (1)" in output
    assert "**comment**" not in output
    assert "comment #1" in output
    code, output = run("task", "comments", "1")
    assert code == 0 and "1 comments and history entries" in output


def test_skills_are_listed_and_printed_in_full(run: Runner) -> None:
    code, output = run("skills")
    assert code == 0
    assert "planning" in output
    assert "triage" in output
    code, output = run("skills", "triage")
    assert code == 0
    assert "skill://triage/SKILL.md" in output
    assert "task_update" in output
    assert run("skills", "nothing") == (1, "no skill 'nothing'\n")


async def test_an_empty_library_says_so(
    backlog: Backlog,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiohttp.test_utils import TestServer

    monkeypatch.setattr(cli.skills, "library", cli.skills.Library(tmp_path / "empty"))
    monkeypatch.setattr(cli.paths, "config_files", list)
    async with TestServer(cli.httpd.application(backlog, console=False)) as server:
        code = await asyncio.to_thread(
            cli.main,
            [
                "--url",
                str(server.make_url("/mcp")),
                "skills",
            ],
        )
    output = capsys.readouterr()
    assert (code, output.out + output.err) == (1, "no skills\n")


def test_projects_and_scope(run: Runner, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    run("task", "add", "Local work")
    code, output = run("--project", str(other), "task", "add", "Elsewhere")
    assert code == 2
    assert "no project" in output

    #: No `use` command: only --project and the directory choose.
    with pytest.raises(SystemExit):
        run("use", "workspace")
    code, output = run("board")
    assert "workspace" in output
    assert "unclaimed" in output
    assert "memory" in output
    assert "workspace" in run("projects")[1]


def test_move_between_projects_and_count(run: Runner, tmp_path: Path) -> None:
    second = tmp_path / "second"
    second.mkdir()
    run("task", "add", "Travelling task")
    #: Visiting the other directory registers it as a project of its own.
    run("-r", str(second), "board")
    code, output = run("task", "move", "1", "second")
    assert code == 0
    assert "#1 now belongs to stdio-user/second" in output
    assert run("task", "list")[1].count("Travelling task") == 0
    assert "Travelling task" in run("--project", "second", "task", "list")[1]
    assert "2 open of 2" not in run("board")[1]

    code, output = run("stats", "--all-projects")
    assert "total" in output
    assert "every project" in output


@pytest.mark.parametrize("flags", [[], ["--yes"]])
def test_delete_does_not_prompt(run: Runner, flags: list[str]) -> None:
    run("task", "add", "Delete me")
    assert run("task", "delete", "1", *flags) == (0, "deleted #1\n")
    assert run("task", "delete", "1", *flags) == (1, "no task #1\n")


def test_create_and_edit_use_the_form(run: Runner, monkeypatch: pytest.MonkeyPatch) -> None:
    written = (
        "Title: From the editor\nStatus: doing\nPriority: high\nTags: docs, api\n\nThe body.\n"
    )
    monkeypatch.setattr(cli, "compose", lambda text: written)
    code, output = run("task", "create")
    assert code == 0
    assert "added #1" in output
    assert "The body." in output

    monkeypatch.setattr(cli, "compose", lambda text: "Title: Renamed\n\nOther body.\n")
    code, output = run("task", "edit", "1")
    assert code == 0
    assert "updated #1" in output
    assert "Renamed" in output

    monkeypatch.setattr(cli, "compose", lambda text: None)
    assert run("task", "edit", "1") == (0, "nothing changed\n")
    assert run("task", "create") == (1, "nothing written, no task added\n")

    monkeypatch.setattr(cli, "compose", lambda text: "Title:\n\nNo title at all.\n")
    assert run("task", "edit", "1")[0] == 1
    assert run("task", "create")[0] == 1
    assert run("task", "edit", "999") == (1, "no task #999\n")


def test_form_round_trip() -> None:
    parsed = cli.parse_form(cli.form_of(task()))
    assert parsed == {
        "title": "Write the form",
        "body": "Some detail",
        "status": Status.DOING,
        "priority": Priority.HIGH,
        "tags": ["docs", "cli"],
    }
    empty = cli.parse_form(cli.form_of())
    assert empty == {
        "title": "",
        "body": "",
        "status": Status.TODO,
        "priority": Priority.NORMAL,
        "tags": [],
    }


def test_form_keeps_text_that_looks_like_a_header() -> None:
    text = "Title: One\n\nNote: this line is body, not a field.\nSecond line.\n"
    parsed = cli.parse_form(text)
    assert parsed["title"] == "One"
    assert parsed["body"] == "Note: this line is body, not a field.\nSecond line."


def test_compose_returns_none_when_nothing_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISUAL", "true")
    assert cli.compose("Title: kept\n") is None
    monkeypatch.setenv("VISUAL", "/usr/bin/sed -i.bak s/kept/changed/")
    written = cli.compose("Title: kept\n")
    assert written is not None and "changed" in written


@pytest.mark.parametrize(
    ("environment", "expected"),
    [({"VISUAL": "code -w"}, ["code", "-w"]), ({"EDITOR": "nano"}, ["nano"]), ({}, ["vi"])],
)
def test_editor_choice(
    monkeypatch: pytest.MonkeyPatch, environment: dict[str, str], expected: list[str]
) -> None:
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    assert cli.editor() == expected


def test_install_writes_both_client_files(
    run: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    #: Neither client CLI is installed here, so both fall back to writing the files.
    monkeypatch.setattr(cli, "run", lambda command: False)
    monkeypatch.setattr(cli, "server_command", lambda: ("/usr/bin/backlog", ["mcp"]))

    code, output = run("install")
    assert code == 0
    assert "claude: backlog written" in output
    assert "codex: backlog written" in output

    claude = json.loads((home / ".claude.json").read_text())
    assert claude["mcpServers"]["backlog"] == {
        "type": "stdio",
        "command": "/usr/bin/backlog",
        "args": ["mcp"],
        "env": {},
    }
    codex = tomllib.loads((home / ".codex" / "config.toml").read_text())
    assert codex["mcp_servers"]["backlog"] == {
        "command": "/usr/bin/backlog",
        "args": ["mcp"],
        "default_tools_approval_mode": "approve",
    }
    assert codex["sandbox_mode"] == "danger-full-access"
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings["permissions"]["allow"] == ["mcp__backlog__*"]

    code, output = run("install")
    assert "already registered" in output
    assert run("install", "--claude", "--force")[0] == 0


def test_install_prefers_the_client_cli(
    run: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    calls: list[Sequence[str]] = []
    monkeypatch.setattr(cli, "run", lambda command: bool(calls.append(command)) or True)
    monkeypatch.setattr(cli, "server_command", lambda: ("/usr/bin/backlog", ["mcp"]))
    code, output = run("install", "--name", "tasks")
    assert code == 0
    assert calls[0][:2] == ["claude", "mcp"]
    assert calls[1][:2] == ["codex", "mcp"]
    assert "tasks registered" in output


def test_generate_config_writes_the_global_options_and_reads_back(
    run: Runner, tmp_path: Path
) -> None:
    target = tmp_path / "config.toml"
    with pytest.raises(SystemExit) as raised:
        run("--log-level", "debug", "--generate-config", str(target))
    assert raised.value.code == 0
    text = target.read_text()
    loaded = tomllib.loads(text)
    assert loaded["log_level"] == "debug"
    assert loaded["database"] == str(run.database)
    assert "state" not in loaded
    #: The subcommand was not parsed yet: its table holds commented-out defaults only.
    assert loaded["mcp"] == {"http": {}, "auth": {"jwt": {}}}
    assert "[mcp.http]" in text
    assert "# port = 3009" in text
    #: The flag writes the file and opens nothing.
    assert not run.database.exists()
    assert "generate_config" not in loaded

    code = cli.main(["--config", str(target), "board"])
    run.capsys.readouterr()
    assert code == 0
    assert run.database.exists()


def test_generate_config_on_the_server_command_sees_its_options(
    run: Runner, tmp_path: Path
) -> None:
    target = tmp_path / "config.toml"
    with pytest.raises(SystemExit) as raised:
        run("mcp", "--protocol", "http", "--http-port", "3010", "--generate-config", str(target))
    assert raised.value.code == 0
    loaded = tomllib.loads(target.read_text())
    assert loaded["mcp"] == {"protocol": "http", "http": {"port": 3010}, "auth": {"jwt": {}}}


def test_grouped_server_config_round_trip(
    run: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "server.toml"
    secret = "test-only-jwt-secret-32-bytes-long"
    with pytest.raises(SystemExit) as raised:
        run(
            "mcp",
            "--protocol=http",
            "--http-listen=0.0.0.0",
            "--http-port=4321",
            "--http-no-console",
            "--http-trust-proxy",
            "--auth-basic=alice:one",
            "--auth-basic=bob:two:parts",
            "--auth-jwt-public-key=/etc/public.pem",
            f"--auth-jwt-pre-shared-key={secret}",
            "--auth-jwt-issuer=issuer",
            "--auth-jwt-audience=backlog",
            "--generate-config",
            str(target),
        )
    assert raised.value.code == 0
    values = tomllib.loads(target.read_text())["mcp"]
    assert values == {
        "protocol": "http",
        "http": {"listen": "0.0.0.0", "port": 4321, "no_console": True, "trust_proxy": True},
        "auth": {
            "basic": ["alice:one", "bob:two:parts"],
            "jwt": {
                "public_key": "/etc/public.pem",
                "pre_shared_key": secret,
                "issuer": "issuer",
                "audience": "backlog",
            },
        },
    }
    seen: dict[str, object] = {}

    async def serve(backlog: object, **kwargs: object) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(cli.httpd, "serve", serve)
    assert cli.server_main(["--config", str(target)]) == 0
    assert seen == {
        "address": "0.0.0.0",
        "port": 4321,
        "console": False,
        "trust_proxy": True,
        "basic": ["alice:one", "bob:two:parts"],
        "jwt_public_key": "/etc/public.pem",
        "jwt_pre_shared_key": secret,
        "jwt_issuer": "issuer",
        "jwt_audience": "backlog",
    }


def test_generate_config_prints_defaults_commented_out(run: Runner) -> None:
    with pytest.raises(SystemExit) as raised:
        run("--generate-config", "-")
    assert raised.value.code == 0
    text = run.capsys.readouterr().out
    assert '# log_level = "info"' in text
    assert "# Directory to scope to" in text
    assert "[mcp]" in text
    assert '# protocol = "stdio"' in text
    assert "[mcp.http]" in text
    assert "# port = 3009" in text
    #: The other commands take their options on the command line only.
    assert "[install]" not in text
    assert "[list]" not in text


def test_server_options_come_from_the_config_file(
    run: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "config.toml"
    target.write_text('[mcp]\nprotocol = "http"\n[mcp.http]\nport = 4321\ntrust_proxy = true\n')
    seen: dict[str, object] = {}

    async def serve(backlog: object, **kw: object) -> None:
        seen.update(kw)

    monkeypatch.setattr(cli.httpd, "serve", serve)
    assert cli.main(["--config", str(target), "mcp"]) == 0
    assert seen["port"] == 4321
    assert seen["trust_proxy"] is True
    #: The flag is accepted after the subcommand too, which is where `backlog-mcp` puts it.
    seen.clear()
    assert cli.server_main(["--config", str(target)]) == 0
    assert seen["port"] == 4321
    #: The search list reaches `backlog-mcp` too, and the command line still wins.
    monkeypatch.setattr(cli.paths, "config_files", lambda: [target])
    assert cli.server_main(["--http-port", "5000"]) == 0
    assert seen["port"] == 5000
    monkeypatch.setenv("BACKLOG_MCP_HTTP_PORT", "6000")
    assert cli.server_main([]) == 0
    assert seen["port"] == 6000


def test_jwt_options_use_config_environment_and_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "jwt.toml"
    target.write_text(
        '[mcp.auth.jwt]\npublic_key = "/etc/public.pem"\nissuer = "issuer"\naudience = "backlog"\n'
    )
    parser = cli.CLI(config_files=[target], config_parser_class=argclass.TOMLDefaultsParser)
    parser.parse_args(["mcp"])
    assert parser.mcp.auth.jwt.public_key == "/etc/public.pem"
    assert parser.mcp.auth.jwt.issuer == "issuer"
    assert parser.mcp.auth.jwt.audience == "backlog"
    monkeypatch.setenv("BACKLOG_MCP_AUTH_JWT_PUBLIC_KEY", "/etc/environment.pem")
    parser.parse_args(["mcp"])
    assert parser.mcp.auth.jwt.public_key == "/etc/environment.pem"
    parser.parse_args(["mcp", "--auth-jwt-public-key", "/etc/flag.pem"])
    assert parser.mcp.auth.jwt.public_key == "/etc/flag.pem"
    monkeypatch.setenv("BACKLOG_MCP_AUTH_JWT_PRE_SHARED_KEY", "test-key" * 8)
    parser.parse_args(["mcp"])
    assert str(parser.mcp.auth.jwt.pre_shared_key) == "test-key" * 8
    assert "test-key" not in repr(parser.mcp.auth.jwt.pre_shared_key)
    monkeypatch.setenv("BACKLOG_MCP_TOKEN", "test-token")
    parser = cli.CLI(auto_env_var_prefix="BACKLOG_MCP_")
    parser.parse_args(["task", "list"])
    assert str(parser.token) == "test-token"
    assert "test-token" not in repr(parser.token)


def test_basic_accounts_accept_multiple_values_and_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "basic.toml"
    target.write_text('[mcp.auth]\nbasic = ["alice:one", "bob:two:parts"]\n')
    parser = cli.CLI(config_files=[target], config_parser_class=argclass.TOMLDefaultsParser)
    parser.parse_args(["mcp"])
    assert [str(value) for value in parser.mcp.auth.basic] == ["alice:one", "bob:two:parts"]
    assert "two:parts" not in repr(parser.mcp.auth.basic)
    parser = cli.CLI(auto_env_var_prefix="BACKLOG_MCP_")
    parser.parse_args(["mcp", "--auth-basic=alice:one", "--auth-basic", "bob:two", "carol:three"])
    assert [str(value) for value in parser.mcp.auth.basic] == [
        "alice:one",
        "bob:two",
        "carol:three",
    ]
    assert "alice:one" not in repr(parser.mcp.auth.basic)
    monkeypatch.setenv("BACKLOG_MCP_AUTH_BASIC", "alice:one bob:two")
    parser = cli.CLI(auto_env_var_prefix="BACKLOG_MCP_")
    parser.parse_args(["mcp"])
    assert [str(value) for value in parser.mcp.auth.basic] == ["alice:one", "bob:two"]


def test_computers_on_the_command_line(run: Runner, tmp_path: Path) -> None:
    code, output = run("--computer", "desk", "board")
    assert code == 0
    assert "desk" in output
    elsewhere = tmp_path / "laptop-work"
    elsewhere.mkdir()
    code, output = run(
        "--computer", "laptop", "--root", str(elsewhere), "task", "add", "Fix the laptop build"
    )
    assert code == 0
    code, output = run("--computer", "desk", "task", "list", "--all-projects")
    assert code == 0
    assert "Fix the laptop build" not in output
    code, output = run("--computer", "desk", "task", "list", "--computer", "laptop")
    assert code == 0
    assert "Fix the laptop build" in output
    assert "laptop" in output
    code, output = run("--computer", "desk", "task", "list", "--all-computers")
    assert "Fix the laptop build" in output
    code, output = run("--computer", "desk", "projects", "--all-computers")
    assert "laptop-work" in output
    code, output = run("--computer", "desk", "stats", "--computer", "laptop")
    assert "every project on laptop" in output
    code, output = run("--computer", "desk", "stats", "--all-computers")
    assert code == 0
    assert "every project on every computer" in output
    code, output = run("--computer", "desk", "task", "list", "--computer", "nowhere")
    assert code == 2
    assert "known computers" in output
    code, output = run("--computer", "two words", "board")
    assert code != 0


def test_server_command_points_at_this_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.sys, "argv", ["/usr/bin/python", "-m", "backlog_mcp"])
    command, arguments = cli.server_command()
    assert command == cli.sys.executable
    assert arguments == ["-m", "backlog_mcp", "mcp"]


def test_server_main_runs_the_server_command(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(cli, "main", lambda argv: bool(seen.append(list(argv))) or 0)
    assert cli.server_main(["--protocol", "stdio"]) == 0
    assert seen == [["mcp", "--protocol", "stdio"]]


@pytest.mark.parametrize("command", ["memory", "mm"])
def test_note_group_prints_its_help_and_opens_nothing(run: Runner, command: str) -> None:
    code, output = run(command)
    assert code == 0
    assert "memory [-h]" in output
    assert "{list,nl,show,ns,add,na,append,np,edit,ne,delete,nd}" in output
    assert "List the notes of this project" in output
    assert "append" in output
    assert not run.database.exists()


def test_notes_add_replace_append_search_and_show(run: Runner) -> None:
    code, output = run("memory", "add", "dev", "vm", "-b", "host: vm-3.internal", "-t", "infra")
    assert code == 0
    assert "added note #1 to stdio-user/workspace" in output
    assert "vm-3.internal" in output

    code, output = run("memory", "add", "dev", "vm", "-b", "host: vm-4.internal")
    assert code == 0
    assert "replaced note #1 in stdio-user/workspace" in output
    assert "vm-3.internal" not in output

    code, output = run("memory", "append", "1", "port", "2222")
    assert code == 0
    assert "appended to note #1" in output
    assert "port 2222" in output

    code, output = run("memory", "list")
    assert code == 0
    assert "1 note(s) of stdio-user/workspace" in output
    assert "dev vm" in output

    run("task", "add", "Move the vm, see note #1")
    code, output = run("search", '"vm"')
    assert code == 0
    assert "1 task match(es) for '\"vm\"' in stdio-user/workspace" in output
    assert "1 note match(es) for '\"vm\"' in stdio-user/workspace" in output
    #: The title column keeps the title; the excerpt with the hit is the `match` column.
    assert "dev vm" in output
    assert "[vm]" in output

    code, output = run("memory", "show", "1")
    assert code == 0
    assert "note #1 dev vm" in output
    assert "vm-4.internal" in output
    assert "infra" in output
    assert run("memory", "show", "42") == (1, "no note #42\n")
    assert run("memory", "append", "42", "more") == (1, "no note #42\n")


def test_shared_notes_show_up_in_another_project(run: Runner, tmp_path: Path) -> None:
    second = tmp_path / "second"
    second.mkdir()
    run("-r", str(second), "memory", "add", "staging", "-b", "https://staging", "--shared")
    run("-r", str(second), "memory", "add", "private")

    code, output = run("memory", "list")
    assert code == 0
    assert "1 note(s) of stdio-user/workspace" in output
    assert "staging" in output
    assert "private" not in output
    #: A note from another board says where it comes from, and that it is shared.
    assert "second" in output
    assert "yes" in output

    assert "private" in run("memory", "list", "--all-projects")[1]
    assert "2 note(s) of every project" in run("memory", "list", "--all-projects")[1]
    assert "1 note(s)" in run("memory", "list", "--shared")[1]
    assert "within accessible projects" in run("memory", "show", "1")[1]


@pytest.mark.parametrize("flags", [[], ["--yes"]])
def test_note_delete_does_not_prompt(run: Runner, flags: list[str]) -> None:
    run("memory", "add", "Delete me")
    assert run("memory", "delete", "1", *flags) == (0, "deleted note #1\n")
    assert run("memory", "delete", "1", *flags) == (1, "no note #1\n")


def test_project_delete_asks_unless_yes(
    run: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    run("-r", str(other), "task", "add", "Doomed")
    run("-r", str(other), "memory", "add", "Doomed note", "--shared")
    monkeypatch.setattr(cli.Confirm, "ask", lambda *args, **kwargs: False)
    assert run("projects", "delete", "other") == (1, "nothing deleted\n")
    assert "other" in run("projects")[1]
    #: The project fixed by --project stays, because every later call would look for it.
    code, output = run("-p", "other", "projects", "delete", "other", "--yes")
    assert code == 1
    assert "fixed for this client" in output
    assert run("projects", "delete", "other", "--yes") == (
        0,
        "deleted project 'stdio-user/other': 1 task(s), 1 note(s)\n",
    )
    assert "other" not in run("projects")[1]
    assert "0 task match(es)" in run("search", "Doomed", "--all-projects")[1]
    code, output = run("projects", "delete", "other", "--yes")
    assert code == 2
    assert "no project 'other'" in output


def test_note_edit_uses_the_form(run: Runner, monkeypatch: pytest.MonkeyPatch) -> None:
    run("memory", "add", "Old", "-b", "old body")
    written = "Title: New\nTags: infra, docs\nShared: yes\n\n# Hosts\n\nNew body.\n"
    monkeypatch.setattr(cli, "compose", lambda text: written)
    code, output = run("memory", "edit", "1")
    assert code == 0
    assert "updated note #1" in output
    assert "New" in output
    assert "docs, infra" in output
    assert "within accessible projects" in output
    assert "Hosts" in output
    assert "New body." in output

    monkeypatch.setattr(cli, "compose", lambda text: None)
    assert run("memory", "edit", "1") == (0, "nothing changed\n")
    monkeypatch.setattr(cli, "compose", lambda text: "Title:\n\nNo title at all.\n")
    assert run("memory", "edit", "1")[0] == 1
    assert run("memory", "edit", "999") == (1, "no note #999\n")


def test_note_form_round_trip() -> None:
    existing = Note(
        id=1,
        project_id=1,
        project="workspace",
        title="Dev vm",
        body="# Hosts\n\nvm-3.internal",
        tags=["infra"],
        shared=True,
        created_at=0.0,
        updated_at=0.0,
    )
    #: The body is markdown, so its `#` headings survive; only the hints on top are dropped.
    assert cli.parse_note_form(cli.note_form_of(existing)) == {
        "title": "Dev vm",
        "body": "# Hosts\n\nvm-3.internal",
        "tags": ["infra"],
        "shared": True,
    }
    assert cli.parse_note_form(cli.note_form_of()) == {
        "title": "",
        "body": "",
        "tags": [],
        "shared": False,
    }


@pytest.mark.parametrize("entity", ["task", "note"])
@pytest.mark.parametrize("explicit_yes", [False, True])
def test_delete_confirms_other_project_only(
    run: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entity: str, explicit_yes: bool
) -> None:
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    prefix = ["memory"] if entity == "note" else ["task"]
    assert run("--root", str(other), *prefix, "add", "Elsewhere")[0] == 0
    prompts: list[str] = []

    def decline(prompt: str, **kwargs: object) -> bool:
        prompts.append(prompt)
        return False

    monkeypatch.setattr(cli.Confirm, "ask", decline)
    if explicit_yes:
        assert run(*prefix, "delete", "1", "--yes")[0] == 0
        assert prompts == []
    else:
        assert run(*prefix, "delete", "1") == (1, "nothing deleted\n")
        assert "other" in prompts[0]
        assert run(*prefix, "show", "1")[0] == 0
        monkeypatch.setattr(cli.Confirm, "ask", lambda *args, **kwargs: True)
        assert run(*prefix, "delete", "1")[0] == 0
    assert run(*prefix, "show", "1")[0] == 1


def test_note_bodies_are_limited_on_the_command_line(
    run: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, output = run("memory", "add", "limit", "-b", "x" * 1025)
    assert (code, output) == (1, "a note holds at most 1024 characters, this one has 1025\n")
    assert run("memory", "add", "limit", "-b", "x" * 1024)[0] == 0
    code, output = run("memory", "append", "1", "y")
    assert code == 1 and "at most 1024 characters" in output
    monkeypatch.setattr(cli, "compose", lambda text: "Title: limit\n\n" + "z" * 1025 + "\n")
    code, output = run("memory", "edit", "1")
    assert code == 1 and "at most 1024 characters" in output
    assert "x" * 1024 in run("memory", "show", "1")[1].replace("│", "").replace("\n", "").replace(
        " ", ""
    )


def test_stats_shows_open_closed_and_note_counts(run: Runner) -> None:
    run("task", "add", "Open work")
    run("task", "add", "Finished work")
    run("task", "close", "2")
    run("task", "add", "Cancelled work")
    run("task", "cancel", "3")
    run("memory", "add", "User preference")
    code, output = run("stats")
    assert code == 0
    rows = [line.split("│") for line in output.splitlines() if "│" in line]
    counts = {cells[1].strip(): cells[2].strip() for cells in rows}
    assert counts["open tasks"] == "1"
    assert counts["closed tasks"] == "2"
    assert counts["total tasks"] == "3"
    assert counts["memory"] == "1"


@pytest.mark.parametrize("name", ["*", "work.*", "", "work\nother"])
def test_install_rejects_ambiguous_server_names(
    run: Runner, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    def forbidden(command: Sequence[str]) -> bool:
        pytest.fail("invalid names must fail before client configuration changes")

    monkeypatch.setattr(cli, "run", forbidden)
    code, output = run("install", "--name", name)
    assert code == 1
    assert "server name must contain only" in output


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_install_changes_only_selected_client(
    run: Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: str
) -> None:
    directory = tmp_path / "client-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: directory))
    monkeypatch.setattr(cli, "run", lambda command: False)
    assert run("install", f"--{client}")[0] == 0
    assert (directory / ".claude" / "settings.json").exists() is (client == "claude")
    assert (directory / ".codex" / "config.toml").exists() is (client == "codex")


async def test_watch_prints_each_event(
    backlog: Backlog, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`backlog watch` runs its own loop in a thread; the server and the writer stay here."""
    from aiohttp.test_utils import TestServer
    from aiohttp_tiny_mcp import Client
    from aiohttp_tiny_mcp.testing import pick

    from backlog_mcp.httpd import ENDPOINT_PATH, application

    async with TestServer(application(backlog, console=False)) as server:
        url = str(server.make_url(ENDPOINT_PATH))
        flags = ["--database", str(tmp_path / "unused.sqlite3")]
        watching = asyncio.create_task(
            asyncio.to_thread(cli.main, [*flags, "-r", str(tmp_path), "watch", url, "-n", "1"])
        )
        async with Client(url, pick("2026-07-28")) as writer:
            await writer.initialize()
            title = "Watched"
            while not watching.done():
                await writer.call_tool("task_add", {"title": title})
                await asyncio.sleep(0.1)
        assert await watching == 0
    output = capsys.readouterr().out
    assert "task #" in output and "added" in output and "'Watched'" in output


def test_search_renders_projects_as_qualified_names(run: Runner) -> None:
    run("task", "add", "A task")
    code, output = run("search", "workspace", "--kind", "projects", "--all-computers")
    assert code == 0
    assert "stdio-user/workspace" in output
    assert "A task" not in output
    assert "1 project match(es)" in output
    assert "task match(es)" not in output


def test_whoami_does_not_prompt_for_registration(
    run: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> str:
        pytest.fail("whoami must not ask to register a project")

    monkeypatch.setattr(cli.Prompt, "ask", refuse)
    code, output = run("whoami")
    assert (
        code == 0 and "stdio-user" in output and "principal" in output and "computer" not in output
    )
    assert run("wi") == (code, output)


def test_stdio_default_user_override_and_isolation(run: Runner) -> None:
    assert run("--default-user", "alice", "task", "add", "Private Alice")[0] == 0
    code, output = run("--default-user", "alice", "whoami")
    assert code == 0 and "alice" in output and "none" in output
    code, output = run("--default-user", "bob", "task", "show", "1")
    assert code == 1 and "no task #1" in output
    assert "Private Alice" in run("--default-user", "alice", "task", "show", "1")[1]


def test_stdio_requires_user_when_environment_is_missing(
    run: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("USER")
    code, output = run("whoami")
    assert code != 0
    assert "set --default-user" in output
    assert run("--default-user", "explicit", "whoami")[0] == 0


def test_server_default_user_without_authorizers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backlog_mcp.auth import user_identity

    monkeypatch.setenv("USER", "environment-user")
    for arguments, expected in (
        ([], user_identity("environment-user")),
        (["--default-user", "override"], user_identity("override")),
        (["--protocol", "http"], user_identity("environment-user")),
        (["--protocol", "http", "--default-user", "override"], user_identity("override")),
        (["--protocol", "http", "--auth-basic", "alice:password"], "local"),
        (["--protocol", "http", "--auth-jwt-pre-shared-key", "x" * 32], "local"),
        (["--protocol", "http", "--auth-jwt-public-key", "/unused.pem"], "local"),
    ):
        parser = cli.CLI()
        parser.parse_args(["--database", str(tmp_path / "unused.db"), "mcp", *arguments])
        assert cli.build(parser).principal == expected


@pytest.mark.parametrize(
    "auth",
    [
        ["--auth-basic", "alice:password"],
        ["--auth-jwt-pre-shared-key", "x" * 32],
        ["--auth-jwt-public-key", "/unused.pem"],
    ],
)
@pytest.mark.parametrize("global_option", [True, False])
def test_http_rejects_default_user_with_authorizers(
    tmp_path: Path, auth: list[str], global_option: bool
) -> None:
    parser = cli.CLI()
    user = ["--default-user", "alice"]
    parser.parse_args(
        [
            "--database",
            str(tmp_path / "unused.db"),
            *(user if global_option else []),
            "mcp",
            "--protocol",
            "http",
            *([] if global_option else user),
            *auth,
        ]
    )
    with pytest.raises(ValueError, match="cannot be combined"):
        cli.build(parser)
    assert not (tmp_path / "unused.db").exists()


def test_root_output_parses_paths_with_colons() -> None:
    from backlog_mcp.models import ProjectRootOut

    root = ProjectRootOut(
        principal="alice@example.org", computer="laptop", root="/work/project:copy"
    )
    assert root.model_dump(mode="json") == "alice%40example.org@laptop:/work/project:copy"
    assert ProjectRootOut.model_validate(root.model_dump(mode="json")) == root
    empty = ProjectRootOut.model_validate("alice@laptop:")
    assert empty.root == ""


def test_task_claim_and_assigned_commands(run: Runner) -> None:
    prefix = "stdio-user@test-pc:"
    target = prefix + "agent_one"
    assert run("--computer", "test-pc", "task", "add", "Claim this")[0] == 0
    code, output = run(
        "--computer", "test-pc", "task", "claim", "1", "--expected", "null", "--assigned", target
    )
    assert code == 0, output
    assert target in output
    code, output = run("--computer", "test-pc", "task", "assigned")
    assert code == 0 and target in output
    code, output = run("--computer", "test-pc", "task", "assigned", "--prefix", prefix)
    assert code == 0 and target in output
    code, output = run("--computer", "test-pc", "whoami")
    assert code == 0 and "assigned_tasks" in output and "1" in output
    code, output = run(
        "--computer", "test-pc", "task", "claim", "1", "--expected", "null", "--assigned", target
    )
    assert code != 0 and "conflict" in output
    code, output = run(
        "--computer", "test-pc", "task", "claim", "1", "--expected", target, "--assigned", "null"
    )
    assert code == 0, output


def test_board_explicit_project_does_not_register_checkout(run: Runner, tmp_path: Path) -> None:
    assert run("task", "add", "Available work")[0] == 0
    other = tmp_path / "unregistered"
    other.mkdir()
    code, output = run("--root", str(other), "board", "workspace")
    assert code == 0
    assert "Available work" in output
    assert "unclaimed" in output
    code, output = run("--root", str(other), "projects", "--all-computers")
    assert code == 0
    assert "unregistered" not in output
    assert "workspace" in output


def test_computer_uses_config_environment_and_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "config.toml"
    target.write_text('computer = "configured-host"\n')
    monkeypatch.delenv("BACKLOG_MCP_COMPUTER", raising=False)
    monkeypatch.setattr("backlog_mcp.backlog.hostname", lambda: "current-host")

    def computer(files: list[Path], args: list[str]) -> str:
        options = cli.CLI(
            config_files=files,
            config_parser_class=argclass.TOMLDefaultsParser,
            auto_env_var_prefix="BACKLOG_MCP_",
        )
        options.parse_args([*args, "mcp"])
        return cli.build(options).computer

    assert computer([], []) == "current-host"
    assert computer([target], []) == "configured-host"
    monkeypatch.setenv("BACKLOG_MCP_COMPUTER", "environment-host")
    assert computer([target], []) == "environment-host"
    assert computer([target], ["--computer", "explicit-host"]) == "explicit-host"
