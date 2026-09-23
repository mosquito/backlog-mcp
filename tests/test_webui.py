"""The browser subapplication is public while MCP remains authenticated."""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from backlog_mcp.backlog import Backlog
from backlog_mcp.httpd import application


@pytest.mark.parametrize("console", [False, True])
async def test_ui_is_public_and_mcp_is_protected(backlog: Backlog, console: bool) -> None:
    async with TestClient(
        TestServer(application(backlog, basic=["user:secret"], console=console))
    ) as client:
        for path in ["/ui", "/ui/"]:
            response = await client.get(path)
            assert response.status == 200
            text = await response.text()
            assert 'data-endpoint="/mcp"' in text
            assert "/ui/static/app.js" in text
        for path in ["app.js", "app.css", "mcp.js", "editor.js", "vendor/purify.es.mjs"]:
            response = await client.get("/ui/static/" + path)
            assert response.status == 200
            assert await response.read()
            assert (await client.head("/ui/static/" + path)).status == 200
        assert (await client.post("/mcp", json={})).status == 401
        assert (await client.get("/mcp")).status == 401
        assert (await client.post("/ui/static/app.js")).status == 405
        assert (await client.get("/ui/static/../webui.py")).status == 404
        assert (await client.get("/ui/static/missing.js")).status == 404
        assert (await client.get("/ui/static/")).status == 403
        assert (await client.get("/ui-other")).status == 404


async def test_missing_browser_assets_do_not_challenge(backlog: Backlog) -> None:
    async with TestClient(TestServer(application(backlog, basic=["user:secret"]))) as client:
        for path in ["/favicon.ico", "/x", "/ui/static/missing.png"]:
            response = await client.get(path)
            assert response.status == 404
            assert "WWW-Authenticate" not in response.headers


async def test_root_redirects_to_public_ui(backlog: Backlog) -> None:
    async with TestClient(TestServer(application(backlog, basic=["user:secret"]))) as client:
        response = await client.get("/", allow_redirects=False)
        assert response.status == 302
        assert response.headers["Location"] == "/ui/"
        assert "WWW-Authenticate" not in response.headers
