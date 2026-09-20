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
    "list and search tools reach every project with all_projects, and every computer with "
    "all_computers. Connect as another computer with ?computer=<name>&project=<slug>."
)


def decode_pair(token: str) -> tuple[str, str] | None:
    """`user:password` from base64, standard or URL-safe alphabet, or `None` when malformed."""
    stripped = token.strip()
    padded = stripped + "=" * (-len(stripped) % 4)
    for altchars in (None, b"-_"):
        try:
            decoded = b64decode(padded, altchars=altchars, validate=True).decode("utf-8")
        except ValueError, UnicodeDecodeError:
            continue
        user, separator, password = decoded.partition(":")
        return (user, password) if separator else None
    return None


def credentials(request: web.Request) -> tuple[str, str] | None:
    """The user and password of the request: from the Basic header, else from `?auth=`.

    Both carry base64 of `user:password`. The query form serves clients that cannot send
    headers. Returns `None` when neither is present or well formed.
    """
    header = request.headers.get("Authorization", "")
    scheme, _, payload = header.partition(" ")
    if scheme.lower() == "basic" and payload:
        return decode_pair(payload)
    token = request.query.get("auth")
    if token:
        #: A `+` of the standard alphabet arrives as a space when the URL did not escape it.
        return decode_pair(token.replace(" ", "+"))
    return None


def basic_auth(user: str, password: str) -> Middleware:
    """Middleware that demands these credentials on every request, in a header or in the URL."""

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
    """Middleware that points the handlers at this backlog, per request.

    `?computer=<name>` serves the request as that computer and `?project=<slug>` fixes its
    project, so one server answers for every computer of its user. Without them a request is
    served as the computer the server runs on, from its working directory.
    """

    @web.middleware
    async def middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        computer = request.query.get("computer") or None
        project = request.query.get("project") or None
        try:
            use(backlog.view(computer=computer, project=project))
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from error
        return await handler(request)

    return middleware


def application(
    backlog: Backlog,
    *,
    user: str = "",
    password: str = "",
    console: bool = True,
    trust_proxy: bool = False,
) -> web.Application:
    """The MCP endpoint at `/mcp`, the console at `/`, both behind Basic auth.

    Without a user and a password nothing is asked for, so bind such a server to localhost only.
    Every client shares the credentials and may name any computer; the server tells the
    computers apart, it does not keep them from each other.

    The endpoint refuses a browser `Origin` that is not its own, against DNS rebinding. Behind
    a TLS-terminating reverse proxy the scheme it sees is not the public one, so `trust_proxy`
    turns that check off: the proxy must validate the origin instead.
    """
    middlewares: list[Middleware] = [bind(backlog)]
    if user and password:
        middlewares.insert(0, basic_auth(user, password))
    else:
        log.warning("no credentials given: every request is served without authentication")
    app = web.Application(middlewares=middlewares)
    Endpoint(registry, trust_proxy_origin_validation=trust_proxy).setup(app, ENDPOINT_PATH)
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
        log.info("project %s (%s)", project.slug, origin.value)

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


__all__ = [
    "CONSOLE_PATH",
    "ENDPOINT_PATH",
    "application",
    "basic_auth",
    "credentials",
    "decode_pair",
    "serve",
]
