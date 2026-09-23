"""The browser subapplication is public while MCP remains authenticated."""

import re

import pytest
from aiohttp.test_utils import TestClient, TestServer

from backlog_mcp.backlog import Backlog
from backlog_mcp.httpd import application
from backlog_mcp.webui import STATIC


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
        for path in [
            "app.js",
            "breadcrumbs.js",
            "app.css",
            "mcp.js",
            "editor.js",
            "comment-composer.js",
            "task-picker.js",
            "task-table.js",
            "task-pagination.js",
            "memory.js",
            "vendor/purify.es.mjs",
            "connect.html",
            "connect.js",
            "agent-config.js",
            "connect.css",
        ]:
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


async def test_index_supports_head_and_conditional_requests(backlog: Backlog) -> None:
    async with TestClient(TestServer(application(backlog, console=False))) as client:
        for path in ["/ui", "/ui/"]:
            response = await client.get(path)
            assert response.status == 200
            assert response.content_type == "text/html"
            body = await response.read()
            etag = response.headers["ETag"]
            links = response.headers["Link"]
            head = await client.head(path)
            assert head.status == 200
            assert head.headers["Link"] == links
            assert head.content_length == len(body)
            assert await head.read() == b""
            cached = await client.get(path, headers={"If-None-Match": etag})
            assert cached.status == 304
            assert cached.headers["Link"] == links
            assert await cached.read() == b""


async def test_index_preloads_eager_assets(backlog: Backlog) -> None:
    async with TestClient(TestServer(application(backlog, console=False))) as client:
        response = await client.get("/ui/")
        hints = dict(re.findall(r"</ui/static/([^>]+)>; ([^,]+)", response.headers["Link"]))
        pending = ["app.js"]
        modules = set()
        while pending:
            name = pending.pop()
            if name in modules:
                continue
            modules.add(name)
            source = (STATIC / name).read_text()
            for dependency in re.findall(
                r"import\s+(?:[^;]*?\s+from\s+)?[\"\'](\./[^\"\']+)", source
            ):
                pending.append(str((STATIC / name).parent.joinpath(dependency).relative_to(STATIC)))
        assert {name for name, hint in hints.items() if hint == "rel=modulepreload"} == modules
        for name in ("app.css", "markdown.css"):
            assert hints[name] == "rel=preload; as=style"
        assert hints["vendor/highlight.min.js"] == "rel=preload; as=script"
        for theme in ("light", "dark"):
            assert (
                f'media="(prefers-color-scheme: {theme})"' in hints[f"vendor/highlight-{theme}.css"]
            )
        assert "vendor/mermaid.min.js" not in hints
        for name in hints:
            asset = await client.get("/ui/static/" + name)
            assert asset.status == 200
            assert await asset.read()
