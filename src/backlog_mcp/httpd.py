"""The optional HTTP front end: the same MCP surface, plus the bundled console."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web
from aiohttp.typedefs import Middleware
from aiohttp_tiny_mcp import Endpoint
from aiohttp_tiny_mcp.console import Console

from .auth import BasicAuthVerifier, CredentialsVerifier, JWTVerifier, decode_pair
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


def request_token(request: web.Request, *, scheme: str, query_parameter: str) -> str:
    """Read credentials from the header, or from the URL when no header is present."""
    if "Authorization" in request.headers:
        parts = request.headers["Authorization"].split()
        return parts[1] if len(parts) == 2 and parts[0].lower() == scheme.lower() else ""
    token = request.query.get(query_parameter, "")
    # Restore a raw '+' in legacy Basic query credentials.
    return token.replace(" ", "+") if scheme == "Basic" else token


def credentials(request: web.Request) -> tuple[str, str] | None:
    """Decode Basic credentials from the header or the auth query parameter."""
    return decode_pair(request_token(request, scheme="Basic", query_parameter="auth"))


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


def authentication(verifier: CredentialsVerifier) -> Middleware:
    """Verify every request before handling MCP sessions or serving the console."""

    @web.middleware
    async def middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        token = request_token(
            request, scheme=verifier.scheme, query_parameter=verifier.query_parameter
        )
        if not token or not verifier.verify(token):
            log.warning("refused %s %s from %s", request.method, request.path, request.remote)
            raise web.HTTPUnauthorized(
                headers={"WWW-Authenticate": f'{verifier.scheme} realm="{REALM}"'}
            )
        return await handler(request)

    return middleware


def application(
    backlog: Backlog,
    *,
    user: str = "",
    password: str = "",
    jwt_public_key: str = "",
    jwt_pre_shared_key: str = "",
    jwt_issuer: str = "",
    jwt_audience: str = "",
    console: bool = True,
    trust_proxy: bool = False,
) -> web.Application:
    """The MCP endpoint at `/mcp` and console at `/`, with Basic or JWT authentication.

    Without Basic credentials or a JWT key, requests do not require authentication.
    Every client shares the credentials and may name any computer; the server tells the
    computers apart, it does not keep them from each other.

    The endpoint refuses a browser `Origin` that is not its own, against DNS rebinding. Behind
    a TLS-terminating reverse proxy the scheme it sees is not the public one, so `trust_proxy`
    turns that check off: the proxy must validate the origin instead.
    """
    middlewares: list[Middleware] = [bind(backlog)]
    if jwt_public_key or jwt_pre_shared_key:
        if user or password:
            raise ValueError("configure JWT or Basic authentication, not both")
        verifier = JWTVerifier.configure(
            public_key=jwt_public_key,
            pre_shared_key=jwt_pre_shared_key,
            issuer=jwt_issuer,
            audience=jwt_audience,
        )
        middlewares.insert(0, authentication(verifier))
    elif jwt_issuer or jwt_audience:
        raise ValueError("JWT issuer and audience require a JWT public key or pre-shared key")
    elif user and password:
        middlewares.insert(0, authentication(BasicAuthVerifier(user, password)))
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
    "BasicAuthVerifier",
    "CredentialsVerifier",
    "authentication",
    "credentials",
    "decode_pair",
    "serve",
]
