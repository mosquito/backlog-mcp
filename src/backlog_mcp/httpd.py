"""The optional HTTP front end: the same MCP surface, plus the bundled console."""

from __future__ import annotations

import asyncio
import logging
from base64 import b64decode
from collections.abc import Awaitable, Callable
from secrets import compare_digest
from typing import Any

from aiohttp import web
from aiohttp.typedefs import Middleware
from aiohttp_tiny_mcp import Endpoint
from aiohttp_tiny_mcp.console import Console

from .backlog import Backlog
from .server import VERSION, registry, use

log = logging.getLogger(__name__)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

ENDPOINT_PATH = "/mcp"
CONSOLE_PATH = "/"
REALM = "backlog-mcp"

CONSOLE_ABOUT = (
    "Projects and tasks of this backlog. Tools work on one project at a time; "
    "list and search tools reach every project with all_projects."
)


def credentials(request: web.Request) -> tuple[str, str] | None:
    """The user and password of a Basic header, or `None` when it is absent or malformed."""
    header = request.headers.get("Authorization", "")
    scheme, _, payload = header.partition(" ")
    if scheme.lower() != "basic" or not payload:
        return None
    try:
        decoded = b64decode(payload, validate=True).decode("utf-8")
    except ValueError, UnicodeDecodeError:
        return None
    user, separator, password = decoded.partition(":")
    return (user, password) if separator else None


def basic_auth(user: str, password: str) -> Middleware:
    """Middleware that demands these credentials on every request."""

    @web.middleware
    async def middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        given = credentials(request)
        #: Both halves are always compared, so a wrong user takes as long as a wrong password.
        matched = given is not None and compare_digest(given[0], user)
        matched &= given is not None and compare_digest(given[1], password)
        if not matched:
            log.warning("refused %s %s from %s", request.method, request.path, request.remote)
            raise web.HTTPUnauthorized(headers={"WWW-Authenticate": f'Basic realm="{REALM}"'})
        return await handler(request)

    return middleware


def bind(backlog: Backlog) -> Middleware:
    """Middleware that points the handlers at this backlog, per request."""

    @web.middleware
    async def middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        use(backlog)
        return await handler(request)

    return middleware


def application(
    backlog: Backlog,
    *,
    user: str = "",
    password: str = "",
    console: bool = True,
) -> web.Application:
    """The MCP endpoint at `/mcp`, the console at `/console`, both behind Basic auth.

    Without a user and a password nothing is asked for, so bind such a server to localhost only.
    """
    middlewares: list[Middleware] = [bind(backlog)]
    if user and password:
        middlewares.insert(0, basic_auth(user, password))
    else:
        log.warning("no credentials given: every request is served without authentication")
    app = web.Application(middlewares=middlewares)
    Endpoint(registry).setup(app, ENDPOINT_PATH)
    if console:
        page = Console(
            endpoint_path=ENDPOINT_PATH,
            title=f"backlog {VERSION}",
            description=CONSOLE_ABOUT,
        )
        page.setup(app, CONSOLE_PATH)

    async def opened(_: web.Application) -> None:
        await backlog.store.open()
        project, origin = await backlog.current()
        log.info("project %s (%s) at %s", project.slug, origin.value, project.root)

    async def closed(_: web.Application) -> None:
        await backlog.store.close()

    app.on_startup.append(opened)
    app.on_cleanup.append(closed)
    return app


async def serve(backlog: Backlog, *, address: str, port: int, **kw: Any) -> None:
    """Serve until cancelled or interrupted. Logs go to stderr, as they do on stdio."""
    runner = web.AppRunner(application(backlog, **kw))
    await runner.setup()
    try:
        await web.TCPSite(runner, address, port).start()
        where = f"http://{address}:{port}"
        log.info("serving %s%s, console at %s%s", where, ENDPOINT_PATH, where, CONSOLE_PATH)
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


__all__ = ["CONSOLE_PATH", "ENDPOINT_PATH", "application", "basic_auth", "serve"]
