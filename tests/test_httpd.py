"""The HTTP front end: Basic auth, the console, and the same MCP surface as stdio."""

import json
from base64 import b64encode, urlsafe_b64encode

import pytest
from aiohttp import ClientSession, encode_basic_auth
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from aiohttp_tiny_mcp import Client
from aiohttp_tiny_mcp.protocol.models import CallToolResult, TextContent, TextResourceContents
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.testing import pick
from conftest import Credentials
from test_server import call

from backlog_mcp import server as operations
from backlog_mcp.auth import credentials, user_identity
from backlog_mcp.backlog import Backlog
from backlog_mcp.httpd import CONSOLE_PATH, ENDPOINT_PATH, application
from backlog_mcp.server import ACTIVE


def payload(result: CallToolResult) -> dict[str, object]:
    """The tool result as data. Revisions before 2025-06-18 carry the same JSON as text."""
    if isinstance(result.structured_content, dict):
        return result.structured_content
    block = result.content[0]
    assert isinstance(block, TextContent)
    decoded = json.loads(block.text)
    assert isinstance(decoded, dict)
    return decoded


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "Bearer wrong",
        "Basic invalid!",
        "Basic bm9jb2xvbg==",  # Decodes to a value with no colon.
        encode_basic_auth("wrong", "secret"),
        encode_basic_auth("user", "wrong"),
    ],
)
async def test_authentication_protects_mcp(backlog: Backlog, authorization: str | None) -> None:
    app = application(backlog, basic=["user:secret"])
    headers = {"Authorization": authorization} if authorization is not None else {}
    async with (
        TestClient(TestServer(app)) as client,
        client.get(ENDPOINT_PATH, headers=headers) as response,
    ):
        assert response.status == 401
        assert response.headers["WWW-Authenticate"] == 'Basic realm="backlog-mcp", charset="UTF-8"'


@pytest.mark.parametrize("adapter", AdapterSet.default().versions)
async def test_authenticated_mcp_requests_share_persisted_tasks(
    backlog: Backlog, adapter: str
) -> None:
    app = application(backlog, basic=["user:secret"])
    async with (
        TestServer(app) as server,
        ClientSession(headers={"Authorization": encode_basic_auth("user", "secret")}) as session,
        Client(str(server.make_url("/mcp")), pick(adapter), session=session) as client,
    ):
        await client.initialize()
        prompts = {prompt.name: prompt for prompt in await client.list_prompts()}
        assert not prompts["install_backlog_rules"].arguments
        installed = await client.get_prompt("install_backlog_rules", {})
        assert isinstance(installed.messages[0].content, TextContent)
        assert "<!-- BACKLOG_START -->" in installed.messages[0].content.text
        result = await client.call_tool("task_add", {"title": "Over HTTP"})
        assert isinstance(result, CallToolResult)
        assert not result.is_error
        task = payload(result)
        assert task["title"] == "Over HTTP"
        read = await client.call_tool("task_get", {"id": task["id"]})
        assert isinstance(read, CallToolResult)
        assert payload(read) == {
            **task,
            "comments": [],
            "links": [],
        }
        async with session.get(server.make_url(CONSOLE_PATH)) as response:
            assert response.status == 200
            assert response.content_type == "text/html"
    assert backlog.database.connection is None
    # Reopening preserves data and still requires the authenticated principal.
    assert await operations.read_task(await backlog.open(), backlog.actor, task["id"]) is None
    view = backlog.view(principal=user_identity("user"))
    stored = await operations.read_task(await view.open(), view.actor, task["id"])
    assert stored is not None
    assert stored.title == "Over HTTP"


async def test_the_query_string_serves_a_client_as_another_computer(backlog: Backlog) -> None:
    app = application(backlog, console=False)
    view = backlog.view(computer="laptop")
    await view.save(await view.open(), view.transaction, root="/srv/app", name="App")
    async with TestServer(app) as server:
        endpoint = server.make_url(ENDPOINT_PATH)
        fixed = str(endpoint.with_query(computer="laptop", project="app"))
        async with Client(fixed, pick(None)) as client:
            await client.initialize()
            scope = payload(await client.call_tool("project_board", {"project": "app"}))
            assert scope["principal"] == "local@laptop"
            assert scope["project"]["full_name"] == "local/app"
            assert scope["project"]["roots"] == ["local@laptop:/srv/app"]
            task = payload(await client.call_tool("task_add", {"title": "From the laptop"}))
            assert task["project"] == "local/app"
        async with Client(str(endpoint.with_query(computer="laptop")), pick(None)) as client:
            await client.initialize()
            result = await client.call_tool("project_board", {})
            assert isinstance(result, CallToolResult) and result.is_error
            scope = payload(await client.call_tool("project_board", {"project": "app"}))
            assert scope["principal"] == "local@laptop"
            assert scope["project"]["full_name"] == "local/app"
            result = await client.call_tool("task_add", {"title": "Nowhere"})
            assert isinstance(result, CallToolResult) and result.is_error
            assert isinstance(result.content[0], TextContent)
            assert "project=<slug>" in result.content[0].text
        async with Client(str(endpoint), pick(None)) as client:
            await client.initialize()
            scope = payload(await client.call_tool("project_board", {"project": "app"}))
            assert scope["principal"] == "local@here"
            assert scope["project"]["roots"] == ["local@laptop:/srv/app"]
            listed = payload(await client.call_tool("project_list", {"all_computers": True}))
            assert {item["slug"]: item["here"] for item in listed["items"]}["app"] is False
        async with (
            ClientSession() as session,
            session.post(endpoint.with_query(computer="bad name"), json={}) as response,
        ):
            assert response.status == 400
            assert "invalid computer name" in await response.text()


@pytest.mark.parametrize(
    "token",
    [
        b64encode(b"a:>>>").decode(),  # standard alphabet, escaped by the URL builder
        urlsafe_b64encode(b"a:>>>").decode(),
        urlsafe_b64encode(b"a:>>>").decode().rstrip("="),
    ],
)
async def test_the_auth_query_parameter_stands_in_for_the_header(
    backlog: Backlog, token: str
) -> None:
    app = application(backlog, basic=["a:>>>"], console=False)
    async with TestServer(app) as server:
        endpoint = server.make_url(ENDPOINT_PATH)
        async with Client(str(endpoint.with_query(auth=token)), pick(None)) as client:
            await client.initialize()
            task = payload(await client.call_tool("task_add", {"title": "Via the URL"}))
            assert task["title"] == "Via the URL"
        async with ClientSession() as session:
            #: A raw `+` in the query decodes to a space; the server puts it back.
            raw = f"{endpoint}?auth={b64encode(b'a:>>>').decode()}"
            assert "+" in raw
            async with session.post(raw, json={}) as response:
                assert response.status != 401
            for wrong in (
                "garbage!",
                b64encode(b"a:wrong").decode(),
                b64encode(b"nocolon").decode(),
            ):
                async with session.post(endpoint.with_query(auth=wrong), json={}) as response:
                    assert response.status == 401


@pytest.mark.parametrize("trust_proxy", [False, True])
async def test_a_foreign_origin_is_refused_unless_the_proxy_is_trusted(
    backlog: Backlog, trust_proxy: bool
) -> None:
    app = application(backlog, console=False, trust_proxy=trust_proxy)
    async with (
        TestServer(app) as server,
        ClientSession(headers={"Origin": "https://evil.example"}) as session,
        session.post(server.make_url(ENDPOINT_PATH), json={}) as response,
    ):
        assert (response.status == 403) is not trust_proxy


async def test_console_can_be_disabled(backlog: Backlog) -> None:
    async with (
        TestClient(TestServer(application(backlog, console=False))) as client,
        client.get(CONSOLE_PATH) as response,
    ):
        assert response.status == 404


async def test_get_notification_stream_survives_restart(
    backlog: Backlog, caplog: pytest.LogCaptureFixture
) -> None:
    headers = {
        "Authorization": encode_basic_auth("user", "secret"),
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    }
    async with ClientSession(headers=headers) as client:
        async with TestServer(application(backlog, basic=["user:secret"])) as server:
            url = server.make_url("/mcp?computer=laptop")
            async with client.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "get-regression", "version": "1"},
                    },
                },
            ) as response:
                assert response.status == 200
                await response.read()
                session = {"Mcp-Session-Id": response.headers["Mcp-Session-Id"]}
            async with client.get(url, headers=session) as response:
                assert response.status == 200
                assert response.content_type == "text/event-stream"

        async with TestServer(application(backlog, basic=["user:secret"])) as server:
            url = server.make_url("/mcp?computer=laptop")
            async with client.get(url, headers=session) as response:
                assert response.status == 200
                assert response.content_type == "text/event-stream"
            async with client.get(url, headers={"Mcp-Session-Id": "missing"}) as response:
                assert response.status == 404
                assert (await response.json())["error"]["message"] == "session not found"
            assert "MCP GET rejected: session not found" in caplog.text
            assert session["Mcp-Session-Id"] not in caplog.text


async def test_http_binding_uses_application_backlog(backlog: Backlog) -> None:
    app = application(backlog, console=False)
    unrelated = Backlog(backlog.database, project="not-registered")
    token = ACTIVE.set(unrelated)
    try:
        async with (
            TestServer(app) as server,
            Client(str(server.make_url("/mcp")), pick(None)) as client,
        ):
            await client.initialize()
            result = await client.read_resource("backlog://project")
            assert isinstance(result.contents[0], TextResourceContents)
            assert json.loads(result.contents[0].text)["project"]["roots"] == [
                f"local@here:{backlog.cwd}"
            ]
    finally:
        ACTIVE.reset(token)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ({}, None),
        ({"Authorization": "Basic"}, None),
        ({"Authorization": "Basic  "}, None),
        ({"Authorization": encode_basic_auth("user", "")}, ("user", "")),
        ({"Authorization": encode_basic_auth("user", "pass:word")}, ("user", "pass:word")),
    ],
)
def test_credentials_reads_only_a_well_formed_header(
    header: dict[str, str], expected: tuple[str, str] | None
) -> None:
    request = make_mocked_request("GET", ENDPOINT_PATH, headers=header)
    assert credentials(request) == expected


def test_credentials_prefer_the_header_over_the_query() -> None:
    query = f"{ENDPOINT_PATH}?auth={b64encode(b'url:one').decode()}"
    assert credentials(make_mocked_request("GET", query)) == ("url", "one")
    headed = make_mocked_request(
        "GET", query, headers={"Authorization": encode_basic_auth("head", "two")}
    )
    assert credentials(headed) == ("head", "two")
    assert credentials(make_mocked_request("GET", f"{ENDPOINT_PATH}?auth=")) is None


async def test_without_accounts_the_server_asks_for_nothing(backlog: Backlog) -> None:
    """Without configured accounts or JWT keys, the server is open."""
    async with TestClient(TestServer(application(backlog))) as client:
        async with client.get(CONSOLE_PATH) as response:
            assert response.status == 200
        async with Client(str(client.make_url(ENDPOINT_PATH)), pick(None)) as mcp:
            handshake = await mcp.initialize()
            assert handshake["resultType"] == "complete"
            result = await mcp.call_tool("task_add", {"title": "No password needed"})
            assert isinstance(result, CallToolResult)
            assert payload(result)["title"] == "No password needed"


@pytest.mark.parametrize("computer", ["desk", "laptop"])
async def test_response_principal_uses_connection_computer(
    backlog: Backlog, credentials: Credentials, computer: str
) -> None:
    async with (
        TestServer(application(backlog, **credentials.options)) as server,
        ClientSession(headers=credentials.headers) as session,
        Client(
            str(server.make_url(f"/mcp?computer={computer}")),
            pick("2026-07-28"),
            session=session,
        ) as client,
    ):
        await client.initialize()
        identity = await call(client, "whoami")
        assert identity["principal"] == f"user@{computer}"
        saved = await call(client, "project_save", root="/srv/work", name="Work")
        project = saved["full_name"]
        task = await call(client, "task_add", project=project, title="Principal regression")
        await call(
            client, "memory_save", project=project, title="Principal regression", body="Note"
        )
        # Historical attribution can legitimately lack a computer.
        connection = await backlog.open()
        await connection.execute(
            "UPDATE tasks SET created_computer = 'unknown', updated_computer = 'unknown' "
            "WHERE id = ?",
            (task["id"],),
        )
        await connection.commit()
        other = backlog.view(computer="other", principal="user")
        await other.save(connection, backlog.transaction, reference=project, root="/srv/work")
        for scope in (
            {"project": project},
            {"all_projects": True},
            {"computer": "other"},
            {"project": project, "computer": "other"},
            {"all_computers": True},
        ):
            for tool, arguments in (
                ("task_list", {}),
                ("task_list", {"full": True}),
                ("memory_list", {}),
                ("memory_list", {"full": True}),
                ("search", {"query": "Principal regression"}),
                ("stats", {}),
            ):
                result = await call(client, tool, **scope, **arguments)
                assert result["principal"] == identity["principal"], (tool, scope, result)
                if tool == "task_list":
                    assert [item["id"] for item in result["items"]] == [task["id"]]
                    assert result["items"][0]["created_by"] == "user@unknown"
                    assert result["items"][0]["updated_by"] == "user@unknown"
        for tool, arguments in (
            ("project_board", {"project": project}),
            ("project_members", {"project": project}),
            ("project_list", {"all_computers": True}),
            ("task_assigned", {}),
            ("task_comment_list", {"task_id": task["id"]}),
        ):
            result = await call(client, tool, **arguments)
            assert result["principal"] == identity["principal"]
