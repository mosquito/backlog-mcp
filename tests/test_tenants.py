"""Authenticated identities isolate storage, search, sessions and notifications."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from aiohttp import ClientSession, encode_basic_auth
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from aiohttp_tiny_mcp import Client
from aiohttp_tiny_mcp.testing import pick
from test_events import call, collect

from backlog_mcp import httpd
from backlog_mcp.auth import BasicAuthentication, basic_namespace
from backlog_mcp.backlog import Backlog


@asynccontextmanager
async def account(
    server: TestServer, username: str, *, password: str = "password", adapter: str = "2026-07-28"
) -> AsyncIterator[Client]:
    async with (
        ClientSession(headers={"Authorization": encode_basic_auth(username, password)}) as session,
        Client(str(server.make_url("/mcp")), pick(adapter), session=session) as client,
    ):
        await client.initialize()
        yield client


@pytest.mark.parametrize(
    "adapter", ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"]
)
async def test_users_cannot_read_or_mutate_each_others_data(backlog: Backlog, adapter: str) -> None:
    app = httpd.application(backlog, basic=["alice:password", "bob:password"])
    async with TestServer(app) as server, account(server, "alice", adapter=adapter) as alice:
        private = await call(alice, "project_save", root="", name="Alice private")
        task = await call(alice, "task_add", project=private["slug"], title="ConfidentialTask")
        await call(alice, "task_comment_add", task_id=task["id"], body="ConfidentialComment")
        note = await call(
            alice,
            "memory_save",
            project=private["slug"],
            title="ConfidentialNote",
            body="Private agreement",
            shared=True,
        )
        async with account(server, "bob", adapter=adapter) as bob:
            assert (await call(bob, "project_list", all_computers=True))["items"] == []
            assert (await call(bob, "task_list", all_computers=True))["items"] == []
            assert (await call(bob, "memory_list", all_computers=True))["items"] == []
            found = await call(bob, "search", query="Confidential", all_computers=True)
            assert not found["tasks"] and not found["memory"] and not found["comments"]
            assert (await call(bob, "stats", all_computers=True))["total"] == 0
            for method, args in (
                ("task_get", {"id": task["id"]}),
                ("task_update", {"id": task["id"], "title": "Stolen"}),
                ("task_delete", {"id": task["id"]}),
                ("task_comment_add", {"task_id": task["id"], "body": "Injected"}),
                ("memory_get", {"id": note["id"]}),
                ("memory_append", {"id": note["id"], "text": "Injected"}),
                ("memory_delete", {"id": note["id"]}),
                ("project_delete", {"project": private["slug"]}),
                ("task_list", {"project": private["slug"]}),
            ):
                result = await bob.call_tool(method, args)
                assert result.is_error, (method, result)
            own = await call(bob, "task_add", title="Bob task")
            # IDs are unique across the shared database.
            assert own["id"] != task["id"]
            await call(bob, "task_update", id=own["id"], title="Bob updated")
            found = await call(bob, "search", query="Confidential", all_projects=True)
            assert all(item["title"] != "ConfidentialTask" for item in found["tasks"])
            assert not found["memory"] and not found["comments"]
            assert (await call(alice, "task_get", id=task["id"]))["title"] == "ConfidentialTask"
            assert (await call(bob, "task_get", id=own["id"]))["title"] == "Bob updated"
            assert (await call(alice, "memory_get", id=note["id"]))["body"] == "Private agreement"


async def test_basic_principals_and_sessions_are_per_username(backlog: Backlog) -> None:
    policy = BasicAuthentication(("alice", "password"), ("bob", "password"))
    principals = []
    for name in ("alice", "bob"):
        principal = await policy.authenticate(
            make_mocked_request(
                "GET", "/mcp", headers={"Authorization": encode_basic_auth(name, "password")}
            )
        )
        assert principal is not None
        assert principal.subject == name
        assert principal.namespace == basic_namespace(name)
        principals.append(principal)
    assert principals[0].identity != principals[1].identity
    async with (
        TestServer(httpd.application(backlog, basic=["alice:password", "bob:password"])) as server,
        account(server, "alice", adapter="2025-06-18") as alice,
        ClientSession() as session,
    ):
        response = await session.post(
            server.make_url("/mcp"),
            headers={
                "Authorization": encode_basic_auth("bob", "password"),
                "Mcp-Session-Id": alice.session_id,
                "MCP-Protocol-Version": "2025-06-18",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert response.status == 404


async def test_basic_notifications_do_not_cross_users(backlog: Backlog) -> None:
    async with (
        TestServer(httpd.application(backlog, basic=["alice:password", "bob:password"])) as server,
        account(server, "alice") as listener,
        account(server, "alice") as writer,
        account(server, "bob") as bob,
    ):
        frames: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        collecting = collect(listener.listen(resources=["backlog://tasks"]), frames)
        try:
            async with asyncio.timeout(5):
                while not listener.accepted:
                    await asyncio.sleep(0.01)
            await call(bob, "task_add", title="Bob private")
            await call(writer, "task_add", title="Alice private")
            frame = await asyncio.wait_for(frames.get(), 5)
            assert frame["params"]["_meta"]["backlog/event"]["title"] == "Alice private"
            assert frames.empty()
        finally:
            collecting.cancel()
            await asyncio.gather(collecting, return_exceptions=True)


@pytest.mark.parametrize(
    "accounts",
    [["missing"], [":password"], ["user:"], ["user:a", "user:b"], ["user:bad\npassword"]],
)
def test_invalid_accounts_are_rejected_without_exposing_passwords(
    backlog: Backlog, accounts: list[str]
) -> None:
    with pytest.raises(ValueError) as error:
        httpd.application(backlog, basic=accounts)
    assert "bad\npassword" not in str(error.value)


async def test_invalid_credentials_cannot_create_tenant_files(backlog: Backlog) -> None:
    app = httpd.application(backlog, basic=["alice:password"])
    async with TestClient(TestServer(app)) as client:
        assert (
            await client.get(
                "/mcp", headers={"Authorization": encode_basic_auth("../escape", "wrong")}
            )
        ).status == 401
        assert await backlog.store.projects() == []


async def test_basic_and_jwt_with_the_same_subject_have_separate_data(backlog: Backlog) -> None:
    from test_jwt_auth import PSK, token

    async with (
        TestServer(
            httpd.application(backlog, basic=["alice:password"], jwt_pre_shared_key=PSK)
        ) as server,
        account(server, "alice") as basic,
        ClientSession(headers={"Authorization": f"Bearer {token(sub='alice')}"}) as session,
        Client(str(server.make_url("/mcp")), pick(None), session=session) as jwt,
    ):
        await jwt.initialize()
        await call(basic, "task_add", title="Basic private")
        assert (await jwt.call_tool("task_get", {"id": 1})).is_error
        jwt_task = await call(jwt, "task_add", title="JWT private")
        assert (await call(basic, "task_get", id=1))["title"] == "Basic private"
        assert (await call(jwt, "task_get", id=jwt_task["id"]))["title"] == "JWT private"
