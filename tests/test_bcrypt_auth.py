"""Bcrypt Basic credentials retain HTTP identity and do not block the event loop."""

import asyncio
import threading
from base64 import b64encode
from urllib.parse import quote

import bcrypt
import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer
from aiohttp_tiny_mcp import Client
from aiohttp_tiny_mcp.protocol.models import CallToolResult
from aiohttp_tiny_mcp.testing import pick

from backlog_mcp.auth import BasicAuthentication, normalize_accounts
from backlog_mcp.backlog import Backlog
from backlog_mcp.httpd import application

USERNAME = "пользователь"
PASSWORD = "пароль:🔑"


@pytest.fixture(scope="session")
def password_hash() -> str:
    return bcrypt.hashpw(PASSWORD.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("ascii")


@pytest.fixture(scope="session")
def boundary_hash() -> str:
    return bcrypt.hashpw(("é" * 36).encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("ascii")


def basic_token(username: str, password: str) -> str:
    return b64encode(f"{username}:{password}".encode()).decode("ascii")


async def test_verify_accepts_raw_password_and_preserves_subject(
    password_hash: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert password_hash.startswith("$2b$10$")
    authentication = BasicAuthentication(hashed_accounts=[(USERNAME, password_hash)])
    principal = await authentication.verify(USERNAME, PASSWORD)
    assert principal is not None
    assert principal.subject == USERNAME
    assert await authentication.verify(USERNAME, "wrong") is None
    event_loop_thread = threading.get_ident()
    unknown_calls: list[tuple[int, bytes, bytes]] = []
    checkpw = bcrypt.checkpw

    def track_unknown(password: bytes, hashed: bytes) -> bool:
        unknown_calls.append((threading.get_ident(), password, hashed))
        return checkpw(password, hashed)

    with monkeypatch.context() as patch:
        patch.setattr(bcrypt, "checkpw", track_unknown)
        assert await authentication.verify("unknown", PASSWORD) is None
    assert len(unknown_calls) == 1
    thread, password, hashed = unknown_calls[0]
    assert thread != event_loop_thread
    assert password == PASSWORD.encode("utf-8")
    assert hashed == password_hash.encode("ascii")
    assert await authentication.verify(USERNAME, password_hash) is None
    assert await authentication.verify(USERNAME, f"bcrypt|{password_hash}") is None


@pytest.mark.parametrize("transport", ["header", "query"])
@pytest.mark.parametrize("account", ["hashed", "plaintext", "literal-prefix"])
async def test_mixed_accounts_work_over_http_and_keep_username_namespace(
    backlog: Backlog, password_hash: str, transport: str, account: str
) -> None:
    app = application(
        backlog,
        basic=[
            f"bcrypt|{USERNAME}|{password_hash}",
            "plain:plain-password",
            "literal:bcrypt|literal-password",
        ],
        console=False,
    )
    username, password = {
        "hashed": (USERNAME, PASSWORD),
        "plaintext": ("plain", "plain-password"),
        "literal-prefix": ("literal", "bcrypt|literal-password"),
    }[account]
    token = basic_token(username, password)
    headers = {"Authorization": f"Basic {token}"} if transport == "header" else {}
    async with TestServer(app) as server, ClientSession(headers=headers) as session:
        endpoint = server.make_url("/mcp").with_query(computer="workstation")
        if transport == "query":
            endpoint = endpoint.update_query(auth=token)
        async with Client(str(endpoint), pick("2025-11-25"), session=session) as client:
            await client.initialize()
            result = await client.call_tool("whoami", {})
            assert isinstance(result, CallToolResult)
            assert not result.is_error
            assert isinstance(result.structured_content, dict)
            assert (
                result.structured_content["principal"] == f"{quote(username, safe='')}@workstation"
            )
            assert result.structured_content["auth_type"] == "basic"


@pytest.mark.parametrize("attempt", ["wrong-password", "wrong-username", "hash", "stored-value"])
async def test_http_rejects_wrong_credentials_and_hash_as_password(
    backlog: Backlog, password_hash: str, attempt: str
) -> None:
    username, password = {
        "wrong-password": (USERNAME, "wrong"),
        "wrong-username": ("unknown", PASSWORD),
        "hash": (USERNAME, password_hash),
        "stored-value": (USERNAME, f"bcrypt|{password_hash}"),
    }[attempt]
    app = application(backlog, basic=[f"bcrypt|{USERNAME}|{password_hash}"], console=False)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/mcp",
            json={},
            headers={"Authorization": f"Basic {basic_token(username, password)}"},
        )
        assert response.status == 401
        assert response.headers["WWW-Authenticate"].startswith("Basic ")


async def test_bcrypt_uses_a_72_byte_limit_not_a_character_limit(boundary_hash: str) -> None:
    authentication = BasicAuthentication(hashed_accounts=[("user", boundary_hash)])
    assert await authentication.verify("user", "é" * 36) is not None
    assert await authentication.verify("user", "é" * 37) is None
    assert await authentication.verify("user", "é" * 36 + "a") is None


@pytest.mark.parametrize("password", ["a" * 73, "é" * 37])
async def test_http_rejects_oversized_bcrypt_passwords_without_server_error(
    backlog: Backlog, password_hash: str, password: str
) -> None:
    app = application(backlog, basic=[f"bcrypt|{USERNAME}|{password_hash}"], console=False)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/mcp",
            json={},
            headers={"Authorization": f"Basic {basic_token(USERNAME, password)}"},
        )
        assert response.status == 401


@pytest.mark.parametrize("password", ["a" * 73, "é" * 37])
def test_oversized_plaintext_configuration_fails_early(password: str) -> None:
    with pytest.raises(ValueError):
        BasicAuthentication(("plain", password))


async def test_plaintext_passwords_beginning_with_bcrypt_prefix_remain_literal() -> None:
    password = "bcrypt|this-is-the-actual-password"
    authentication = BasicAuthentication(("plain", password))
    principal = await authentication.verify("plain", password)
    assert principal is not None
    assert principal.subject == "plain"
    assert await authentication.verify("plain", password.removeprefix("bcrypt|")) is None


@pytest.mark.parametrize("username", ["bcrypt|user", "team|user"])
async def test_usernames_containing_pipes_round_trip_through_configuration(username: str) -> None:
    normalized = normalize_accounts([f"{username}:{PASSWORD}"])
    assert len(normalized) == 1
    assert normalized[0].startswith(f"bcrypt|{username}|$2b$10$")
    assert normalize_accounts(normalized) == normalized
    parsed_username, hashed = normalized[0].removeprefix("bcrypt|").rsplit("|", 1)
    assert parsed_username == username
    authentication = BasicAuthentication(hashed_accounts=[(parsed_username, hashed)])
    principal = await authentication.verify(username, PASSWORD)
    assert principal is not None
    assert principal.subject == username


async def test_plaintext_is_hashed_once_with_cost_ten_in_a_worker_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller_thread = threading.get_ident()
    generated: list[tuple[int, bytes, bytes]] = []
    hashpw = bcrypt.hashpw

    def track_hash(password: bytes, salt: bytes) -> bytes:
        generated.append((threading.get_ident(), password, salt))
        return hashpw(password, salt)

    monkeypatch.setattr(bcrypt, "hashpw", track_hash)
    authentication = BasicAuthentication((USERNAME, PASSWORD))
    assert len(generated) == 1
    thread, password, salt = generated[0]
    assert thread != caller_thread
    assert password == PASSWORD.encode("utf-8")
    assert salt.startswith(b"$2b$10$")
    assert await authentication.verify(USERNAME, PASSWORD) is not None
    assert await authentication.verify(USERNAME, PASSWORD) is not None
    assert len(generated) == 1


async def test_prehashed_accounts_are_not_hashed_again(
    password_hash: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject_hash(password: bytes, salt: bytes) -> bytes:
        raise AssertionError("An existing bcrypt hash must remain unchanged")

    monkeypatch.setattr(bcrypt, "hashpw", reject_hash)
    authentication = BasicAuthentication(hashed_accounts=[(USERNAME, password_hash)])
    assert await authentication.verify(USERNAME, PASSWORD) is not None


@pytest.mark.parametrize(
    "configured",
    [
        "",
        "not-a-bcrypt-hash",
        "$2b$10$short",
        "$2b$03$" + "A" * 53,
        "$2b$32$" + "A" * 53,
        "$2b$10$" + "!" * 53,
    ],
)
def test_malformed_bcrypt_configuration_fails_early(configured: str) -> None:
    with pytest.raises(ValueError):
        BasicAuthentication(hashed_accounts=[("user", configured)])


@pytest.mark.parametrize("duplicates", ["plaintext", "hashed", "mixed"])
def test_duplicate_usernames_fail_before_authentication(
    password_hash: str, duplicates: str
) -> None:
    with pytest.raises(ValueError, match="unique"):
        if duplicates == "plaintext":
            BasicAuthentication(("user", "one"), ("user", "two"))
        elif duplicates == "hashed":
            BasicAuthentication(hashed_accounts=[("user", password_hash), ("user", password_hash)])
        else:
            BasicAuthentication(("user", "one"), hashed_accounts=[("user", password_hash)])


async def test_invalid_bcrypt_salt_rejects_authentication(
    backlog: Backlog, password_hash: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def invalid_salt(password: bytes, hashed: bytes) -> bool:
        raise ValueError("Invalid salt")

    monkeypatch.setattr(bcrypt, "checkpw", invalid_salt)
    app = application(backlog, basic=[f"bcrypt|{USERNAME}|{password_hash}"], console=False)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/mcp",
            json={},
            headers={"Authorization": f"Basic {basic_token(USERNAME, PASSWORD)}"},
        )
        assert response.status == 401


@pytest.mark.parametrize("prehashed", [False, True])
async def test_bcrypt_runs_in_a_worker_while_the_event_loop_remains_responsive(
    password_hash: str, monkeypatch: pytest.MonkeyPatch, prehashed: bool
) -> None:
    loop = asyncio.get_running_loop()
    event_loop_thread = threading.get_ident()
    started = asyncio.Event()
    responsive = asyncio.Event()
    release = threading.Event()
    calls: list[tuple[int, bytes, bytes]] = []
    checkpw = bcrypt.checkpw

    def held_check(password: bytes, hashed: bytes) -> bool:
        thread = threading.get_ident()
        calls.append((thread, password, hashed))
        loop.call_soon_threadsafe(started.set)
        if thread == event_loop_thread:
            return False
        assert release.wait(timeout=10), "The test did not release the bcrypt worker"
        return checkpw(password, hashed)

    monkeypatch.setattr(bcrypt, "checkpw", held_check)
    authentication = (
        BasicAuthentication(hashed_accounts=[(USERNAME, password_hash)])
        if prehashed
        else BasicAuthentication((USERNAME, PASSWORD))
    )
    verification = asyncio.create_task(authentication.verify(USERNAME, PASSWORD))
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        assert not verification.done()
        loop.call_soon(responsive.set)
        await asyncio.wait_for(responsive.wait(), timeout=10)
        assert not verification.done()
    finally:
        release.set()
        principal = await asyncio.wait_for(verification, timeout=10)
    assert principal is not None
    assert len(calls) == 1
    thread, password, hashed = calls[0]
    assert thread != event_loop_thread
    assert password == PASSWORD.encode("utf-8")
    if prehashed:
        assert hashed == password_hash.encode("ascii")
    else:
        assert hashed.startswith(b"$2b$10$")
