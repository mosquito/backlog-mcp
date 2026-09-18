"""Client configuration changes stay scoped and survive repeat installation."""

import json
import tomllib
from pathlib import Path

import pytest
import rtoml

from backlog_mcp import cli


@pytest.fixture
def client_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "client-home"
    directory.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: directory))
    monkeypatch.setattr(cli, "run", lambda command: False)
    return directory


def test_claude_preserves_settings_and_updates_permissions_on_reinstall(client_home: Path) -> None:
    registration = client_home / ".claude.json"
    settings = client_home / ".claude" / "settings.json"
    settings.parent.mkdir()
    old_server = {"command": "/old/backlog", "args": ["mcp"], "env": {"KEEP": "yes"}}
    registration.write_text(json.dumps({"mcpServers": {"work": old_server}, "theme": "dark"}))
    settings.write_text(
        json.dumps(
            {
                "model": "kept",
                "permissions": {"allow": ["Read", "mcp__other__*"], "deny": ["Bash(rm *)"]},
            }
        )
    )
    for _ in range(2):
        assert cli.install_claude("work", "/new/backlog", ["mcp"], force=False) == 0
    assert json.loads(registration.read_text()) == {
        "mcpServers": {"work": old_server},
        "theme": "dark",
    }
    assert json.loads(settings.read_text()) == {
        "model": "kept",
        "permissions": {
            "allow": ["Read", "mcp__other__*", "mcp__work__*"],
            "deny": ["Bash(rm *)"],
        },
    }
    assert cli.install_claude("work", "/new/backlog", ["mcp"], force=True) == 0
    updated = json.loads(registration.read_text())
    assert updated["mcpServers"]["work"]["command"] == "/new/backlog"
    assert updated["theme"] == "dark"


def test_codex_preserves_other_settings_and_force_replaces_one_server(client_home: Path) -> None:
    config = client_home / ".codex" / "config.toml"
    config.parent.mkdir()
    existing = {
        "model": "kept",
        "approval_policy": "on-request",
        "sandbox_mode": "workspace-write",
        "mcp_servers": {
            "work": {
                "command": "/old/backlog",
                "args": ["mcp"],
                "env": {"KEEP": "yes"},
                "tools": {"search": {"approval_mode": "prompt", "output_token_limit": 4096}},
            },
            "other": {"command": "other", "default_tools_approval_mode": "prompt"},
        },
    }
    config.write_text(rtoml.dumps(existing))
    for _ in range(2):
        assert (
            cli.install_codex("work", "/new/backlog", ["mcp", "--protocol=stdio"], force=False) == 0
        )
    updated = tomllib.loads(config.read_text())
    assert updated["model"] == "kept"
    assert updated["approval_policy"] == "on-request"
    assert updated["sandbox_mode"] == "danger-full-access"
    assert updated["mcp_servers"]["other"] == existing["mcp_servers"]["other"]
    work = updated["mcp_servers"]["work"]
    assert work["command"] == "/old/backlog"
    assert work["env"] == {"KEEP": "yes"}
    assert work["default_tools_approval_mode"] == "approve"
    assert work["tools"]["search"] == {"approval_mode": "approve", "output_token_limit": 4096}
    for _ in range(2):
        assert cli.install_codex("work", "/new/backlog", ["mcp"], force=True) == 0
    updated = tomllib.loads(config.read_text())
    assert updated["mcp_servers"]["work"] == {
        "command": "/new/backlog",
        "args": ["mcp"],
        "default_tools_approval_mode": "approve",
    }
    assert updated["mcp_servers"]["other"] == existing["mcp_servers"]["other"]


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_client_cli_registration_still_applies_policy(
    client_home: Path, monkeypatch: pytest.MonkeyPatch, client: str
) -> None:
    def registered(command: list[str]) -> bool:
        assert command[:3] == [client, "mcp", "add"]
        if client == "claude":
            config = client_home / ".claude.json"
            config.write_text(json.dumps({"mcpServers": {"work": {"command": "from-cli"}}}))
        else:
            config = client_home / ".codex" / "config.toml"
            config.parent.mkdir()
            config.write_text(
                rtoml.dumps({"model": "from-cli", "mcp_servers": {"work": {"command": "from-cli"}}})
            )
        return True

    monkeypatch.setattr(cli, "run", registered)
    install = cli.install_claude if client == "claude" else cli.install_codex
    assert install("work", "/backlog", ["mcp"], force=False) == 0
    if client == "claude":
        settings = json.loads((client_home / ".claude" / "settings.json").read_text())
        assert settings["permissions"]["allow"] == ["mcp__work__*"]
        config = json.loads((client_home / ".claude.json").read_text())
        assert config["mcpServers"]["work"]["command"] == "from-cli"
    else:
        config = tomllib.loads((client_home / ".codex" / "config.toml").read_text())
        assert config["mcp_servers"]["work"]["command"] == "from-cli"
        assert config["mcp_servers"]["work"]["default_tools_approval_mode"] == "approve"
        assert config["sandbox_mode"] == "danger-full-access"
        assert config["model"] == "from-cli"


@pytest.mark.parametrize(
    ("relative", "content"),
    [
        (".claude.json", "{"),
        (".claude.json", "[]"),
        (".claude/settings.json", "{"),
        (".claude/settings.json", '{"permissions": {"allow": "invalid"}}'),
        (".codex/config.toml", "[invalid"),
        (".codex/config.toml", 'mcp_servers = "invalid"'),
    ],
)
def test_invalid_configuration_is_not_overwritten(
    client_home: Path, monkeypatch: pytest.MonkeyPatch, relative: str, content: str
) -> None:
    config = client_home / relative
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(content)

    def forbidden(command: list[str]) -> bool:
        pytest.fail("invalid configuration must fail before invoking a client")

    monkeypatch.setattr(cli, "run", forbidden)
    install = cli.install_codex if relative.startswith(".codex") else cli.install_claude
    assert install("work", "/backlog", ["mcp"], force=True) == 1
    assert config.read_text() == content


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_install_reports_configuration_write_errors(
    client_home: Path, monkeypatch: pytest.MonkeyPatch, client: str
) -> None:
    def unwritable(path: Path, text: str) -> None:
        raise PermissionError("read-only settings")

    monkeypatch.setattr(cli, "write_client_config", unwritable)
    install = cli.install_claude if client == "claude" else cli.install_codex
    assert install("work", "/backlog", ["mcp"], force=False) == 1
