"""The optional HTTP front end: the same MCP surface, plus the bundled console."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web
from aiohttp.typedefs import Middleware
from aiohttp_tiny_mcp import Authentication, Endpoint, Registry, Unauthorized
from aiohttp_tiny_mcp.console import Console

from .auth import VERIFIED, BasicAuthentication, JWTAuthentication, JWTVerifier
from .backlog import Backlog
from .server import VERSION, registry, use

log = logging.getLogger(__name__)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

ENDPOINT_PATH = "/mcp"
CONSOLE_PATH = "/"
CONSOLE_ASSETS = frozenset({CONSOLE_PATH, "/console.js", "/console.css"})

CONSOLE_ABOUT = (
    "Projects and tasks of this backlog. Tools work on one project at a time; "
    "list and search tools reach every project with all_projects, and every computer with "
    "all_computers. Connect as another computer with ?computer=<name>&project=<slug>."
)


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


def authentication(
    policy: Authentication, *, public_paths: frozenset[str] = frozenset()
) -> Middleware:
    """Reject invalid credentials before routing or body parsing; allow console assets."""

    @web.middleware
    async def middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        if request.method in {"GET", "HEAD"} and request.path in public_paths:
            return await handler(request)
        try:
            request[VERIFIED] = policy.check(await policy.authenticate(request))
        except Unauthorized as refusal:
            log.warning("refused %s %s from %s", request.method, request.path, request.remote)
            return web.json_response(
                {"error": refusal.error, "error_description": refusal.description},
                status=refusal.status,
                headers={"WWW-Authenticate": policy.challenge(refusal)},
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
    """The authenticated MCP endpoint at `/mcp` and the public static console at `/`.

    Without Basic credentials or a JWT key, requests do not require authentication.
    Every client shares the credentials and may name any computer; the server tells the
    computers apart, it does not keep them from each other.

    The endpoint refuses a browser `Origin` that is not its own, against DNS rebinding. Behind
    a TLS-terminating reverse proxy the scheme it sees is not the public one, so `trust_proxy`
    turns that check off: the proxy must validate the origin instead.
    """
    middlewares: list[Middleware] = [bind(backlog)]
    policy: Authentication | None = None
    if jwt_public_key or jwt_pre_shared_key:
        if user or password:
            raise ValueError("configure JWT or Basic authentication, not both")
        verifier = JWTVerifier.configure(
            public_key=jwt_public_key,
            pre_shared_key=jwt_pre_shared_key,
            issuer=jwt_issuer,
            audience=jwt_audience,
        )
        policy = JWTAuthentication(verifier)
    elif jwt_issuer or jwt_audience:
        raise ValueError("JWT issuer and audience require a JWT public key or pre-shared key")
    elif user and password:
        policy = BasicAuthentication(user, password)
    else:
        log.warning("no credentials given: every request is served without authentication")
    if policy is not None:
        public_paths = CONSOLE_ASSETS if console else frozenset()
        middlewares.insert(0, authentication(policy, public_paths=public_paths))
    # Share the registered surface and notification Hub, but not auth or session storage.
    configured = Registry(
        "backlog", VERSION, instructions=registry.instructions, hub=registry.hub, auth=policy
    )
    configured.tools = registry.tools
    configured.resources_fixed = registry.resources_fixed
    configured.resources_templated = registry.resources_templated
    configured.prompts = registry.prompts
    configured.completer = registry.completer
    configured.providers = registry.providers
    configured.extensions = registry.extensions
    configured.resource_aliases = registry.resource_aliases
    app = web.Application(middlewares=middlewares)
    Endpoint(configured, trust_proxy_origin_validation=trust_proxy).setup(app, ENDPOINT_PATH)
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
    "authentication",
    "serve",
]
