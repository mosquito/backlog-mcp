"""Bcrypt cost follows each parser's configuration without changing stored hashes."""

import json
import os
import tomllib
from pathlib import Path

import argclass
import bcrypt
import pytest

from backlog_mcp import auth, cli


@pytest.fixture(autouse=True)
def isolated_cli_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.paths, "config_files", list)
    for name in [key for key in os.environ if key.startswith("BACKLOG_MCP_")]:
        monkeypatch.delenv(name)


@pytest.fixture(scope="session")
def existing_account() -> str:
    hashed = bcrypt.hashpw(b"existing-password", bcrypt.gensalt(rounds=4)).decode("ascii")
    return f"bcrypt|existing|{hashed}"


def assert_account(
    account: str, cost: int, username: str = "user", password: str = "password"
) -> None:
    prefix, actual_username, hashed = str(account).split("|", 2)
    assert prefix == "bcrypt"
    assert actual_username == username
    assert int(hashed.split("$")[2]) == cost
    assert bcrypt.checkpw(password.encode("utf-8"), hashed.encode("ascii"))


@pytest.mark.parametrize("value", [4, 5, 10, 31, "4", "10", "31"])
def test_cost_accepts_the_bcrypt_range(value: str | int) -> None:
    assert auth.validate_bcrypt_cost(value) == int(value)


@pytest.mark.parametrize("value", [3, 32, 0, -1, "3", "32", "", "invalid", "4.5"])
def test_cost_rejects_values_outside_the_bcrypt_range(value: str | int) -> None:
    with pytest.raises(ValueError):
        auth.validate_bcrypt_cost(value)


@pytest.mark.parametrize("cost", [4, 5])
def test_account_helpers_use_selected_cost_and_preserve_existing_hashes(
    cost: int, existing_account: str
) -> None:
    assert_account(auth.bcrypt_account("user:password", cost=cost), cost)
    accounts = auth.normalize_accounts(["user:password", existing_account], cost=cost)
    assert_account(accounts[0], cost)
    assert accounts[1] == existing_account


async def test_direct_authentication_uses_selected_cost(existing_account: str) -> None:
    hashed = existing_account.split("|", 2)[2]
    policy = auth.BasicAuthentication(
        ("user", "password"), hashed_accounts=[("existing", hashed)], cost=5
    )
    assert policy.password_hashes["user"].startswith(b"$2b$05$")
    assert policy.password_hashes["existing"] == hashed.encode("ascii")
    assert await policy.verify("user", "password") is not None
    assert await policy.verify("existing", "existing-password") is not None


def test_default_cost_remains_ten() -> None:
    parser = cli.CLI()
    parser.parse_args(["mcp", "--auth-basic", "user:password"])
    assert parser.mcp.auth.bcrypt_cost == 10
    assert_account(parser.mcp.auth.basic[0], 10)


@pytest.mark.parametrize("cost_first", [True, False])
def test_cli_cost_is_independent_of_flag_order(cost_first: bool) -> None:
    cost = ["--auth-bcrypt-cost", "4"]
    account = ["--auth-basic", "user:password"]
    arguments = [*cost, *account] if cost_first else [*account, *cost]
    parser = cli.CLI()
    parser.parse_args(["mcp", *arguments])
    assert parser.mcp.auth.bcrypt_cost == 4
    assert_account(parser.mcp.auth.basic[0], 4)


@pytest.mark.parametrize(
    "environment,flag,expected", [(None, None, 4), ("5", None, 5), ("5", "4", 4)]
)
def test_cli_environment_and_toml_cost_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment: str | None,
    flag: str | None,
    expected: int,
) -> None:
    source = tmp_path / "source.toml"
    source.write_text('[mcp.auth]\nbcrypt_cost = 4\nbasic = ["user:password"]\n')
    if environment is not None:
        monkeypatch.setenv("BACKLOG_MCP_AUTH_BCRYPT_COST", environment)
    parser = cli.CLI(
        config_files=[source],
        config_parser_class=argclass.TOMLDefaultsParser,
        auto_env_var_prefix="BACKLOG_MCP_",
    )
    parser.parse_args(["mcp", *(["--auth-bcrypt-cost", flag] if flag is not None else [])])
    assert parser.mcp.auth.bcrypt_cost == expected
    assert_account(parser.mcp.auth.basic[0], expected)


@pytest.mark.parametrize("command", [[], ["mcp"]])
@pytest.mark.parametrize("environment", [False, True])
def test_generated_config_uses_selected_default_cost_and_keeps_stored_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_account: str,
    command: list[str],
    environment: bool,
) -> None:
    source = tmp_path / "source.toml"
    source.write_text(
        "[mcp.auth]\nbcrypt_cost = 4\nbasic = "
        + json.dumps([existing_account, "user:password"])
        + "\n"
    )
    if environment:
        monkeypatch.setenv("BACKLOG_MCP_AUTH_BCRYPT_COST", "5")
    target = tmp_path / "generated.toml"
    with pytest.raises(SystemExit) as raised:
        cli.main(["--config", str(source), *command, "--generate-config", str(target)])
    assert raised.value.code == 0
    generated = tomllib.loads(target.read_text())["mcp"]["auth"]
    expected = 5 if environment else 4
    assert generated["bcrypt_cost"] == expected
    assert generated["basic"][0] == existing_account
    assert_account(generated["basic"][1], expected)
    assert "user:password" not in target.read_text()


@pytest.mark.parametrize("cost_first", [True, False])
def test_generate_config_honors_cost_after_or_before_account_flags(
    tmp_path: Path, cost_first: bool
) -> None:
    cost = ["--auth-bcrypt-cost", "5"]
    account = ["--auth-basic", "user:password"]
    arguments = [*cost, *account] if cost_first else [*account, *cost]
    target = tmp_path / "generated.toml"
    with pytest.raises(SystemExit) as raised:
        cli.main(["mcp", *arguments, "--generate-config", str(target)])
    assert raised.value.code == 0
    generated = tomllib.loads(target.read_text())["mcp"]["auth"]
    assert generated["bcrypt_cost"] == 5
    assert_account(generated["basic"][0], 5)


def test_separate_parsers_and_repeated_parses_keep_their_own_cost() -> None:
    first = cli.CLI()
    second = cli.CLI()
    for parser, cost in [(first, 4), (second, 5), (first, 5), (second, 4), (first, 4)]:
        parser.parse_args(["mcp", "--auth-basic", "user:password", "--auth-bcrypt-cost", str(cost)])
        assert len(parser.mcp.auth.basic) == 1
        assert_account(parser.mcp.auth.basic[0], cost)


@pytest.mark.parametrize("command", ["hash-password", "pwhash", "pw", "hash"])
def test_interactive_hash_aliases_use_selected_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    def ask(prompt: str, **kwargs: object) -> str:
        return "password" if kwargs.get("password") else "user"

    monkeypatch.setattr(cli.Prompt, "ask", ask)
    database = tmp_path / "unused.sqlite3"
    assert cli.main(["--database", str(database), command, "--bcrypt-cost", "4"]) == 0
    output = capsys.readouterr()
    assert len(output.out.splitlines()) == 1
    assert_account(output.out.strip(), 4)
    assert not database.exists()


@pytest.mark.parametrize(
    "command,option", [("mcp", "--auth-bcrypt-cost"), ("hash-password", "--bcrypt-cost")]
)
@pytest.mark.parametrize("value", ["3", "32", "invalid"])
def test_cli_rejects_invalid_cost_before_hashing(
    monkeypatch: pytest.MonkeyPatch, command: str, option: str, value: str
) -> None:
    def unexpected(*args: object, **kwargs: object) -> bytes:
        pytest.fail("Invalid cost must fail before bcrypt work")

    monkeypatch.setattr(bcrypt, "gensalt", unexpected)
    monkeypatch.setattr(bcrypt, "hashpw", unexpected)
    parser = cli.CLI()
    with pytest.raises((SystemExit, ValueError)):
        parser.parse_args([command, option, value])


@pytest.mark.parametrize("value", ["true", "false", "4.0", "4.5"])
@pytest.mark.parametrize("generate", [False, True])
def test_toml_cost_rejects_boolean_and_float_values(
    tmp_path: Path, value: str, generate: bool
) -> None:
    source = tmp_path / "invalid.toml"
    source.write_text(f"[mcp.auth]\nbcrypt_cost = {value}\n")
    parser = cli.CLI(config_files=[source], config_parser_class=argclass.TOMLDefaultsParser)
    arguments = ["--generate-config", str(tmp_path / "generated.toml")] if generate else ["mcp"]
    with pytest.raises((SystemExit, ValueError)) as raised:
        parser.parse_args(arguments)
    if isinstance(raised.value, SystemExit):
        assert raised.value.code != 0


def test_upper_cost_boundary_is_forwarded_without_expensive_test_work(
    monkeypatch: pytest.MonkeyPatch, existing_account: str
) -> None:
    costs: list[int] = []
    hashed = existing_account.split("|", 2)[2].replace("$04$", "$31$").encode("ascii")

    def gensalt(rounds: int) -> bytes:
        costs.append(rounds)
        return b"test-salt"

    def hashpw(password: bytes, salt: bytes) -> bytes:
        assert password == b"password"
        assert salt == b"test-salt"
        return hashed

    monkeypatch.setattr(bcrypt, "gensalt", gensalt)
    monkeypatch.setattr(bcrypt, "hashpw", hashpw)
    assert auth.bcrypt_account("user:password", cost=31) == "bcrypt|user|" + hashed.decode("ascii")
    assert costs == [31]
