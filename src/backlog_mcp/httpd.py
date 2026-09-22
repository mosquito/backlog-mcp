"""The optional HTTP front end: the same MCP surface, plus the bundled console."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any

from aiohttp import web
from aiohttp.typedefs import Middleware
from aiohttp_tiny_mcp import Authentication, Endpoint, Principal, Registry, Unauthorized
from aiohttp_tiny_mcp.auth.jwt import HMACJWTVerifier, PublicKeyJWTVerifier
from aiohttp_tiny_mcp.console import Console
from aiohttp_tiny_mcp.storage.namespaces import namespace
from aiohttp_tiny_mcp.storage.sqlite import SqliteStorage

from .auth import BasicAuthentication, JWTAuthentication, basic_namespace
from .backlog import Backlog
from .server import ACTIVE_REGISTRY, VERSION, persistent_registry, use

log = logging.getLogger(__name__)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

ENDPOINT_PATH = "/mcp"
CONSOLE_PATH = "/"
CONSOLE_ASSETS = frozenset({CONSOLE_PATH, "/console.js", "/console.css"})
PRINCIPAL = web.RequestKey("backlog_principal", Principal)
STORAGE = web.AppKey("backlog_storage", SqliteStorage)

CONSOLE_ABOUT = (
    "Projects and tasks of this backlog. Tools work on one project at a time; "
    "list and search tools reach every project with all_projects, and every computer with "
    "all_computers. Connect as another computer with ?computer=<name>&project=<slug>."
)


def bind(backlog: Backlog, configured: Registry | None = None) -> Middleware:
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
            identity = None
            if PRINCIPAL in request:
                principal = request[PRINCIPAL]
                identity = principal.namespace
                assert identity, "authenticated users must have a storage identity"
                await backlog.store.register(
                    identity, principal.subject or principal.client_id or identity
                )
            use(backlog.view(computer=computer, project=project, principal=identity))
            if configured is not None:
                ACTIVE_REGISTRY.set(configured)
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error)) from error
        return await handler(request)

    return middleware


def authentication(endpoint: Endpoint, *, public_paths: frozenset[str] = frozenset()) -> Middleware:
    """Reject invalid credentials before routing or body parsing; allow console assets."""

    @web.middleware
    async def middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        if request.method in {"GET", "HEAD"} and request.path in public_paths:
            return await handler(request)
        token = namespace.set(None)
        try:
            try:
                principal = await endpoint.verified(request)
            except Unauthorized as refusal:
                log.warning("refused %s %s from %s", request.method, request.path, request.remote)
                return endpoint.refuse(refusal)
            if principal is not None:
                request[PRINCIPAL] = principal
            return await handler(request)
        finally:
            namespace.reset(token)

    return middleware


def application(
    backlog: Backlog,
    *,
    basic: Sequence[str] = (),
    jwt_public_key: str = "",
    jwt_pre_shared_key: str = "",
    jwt_issuer: str = "",
    jwt_audience: str = "",
    console: bool = True,
    trust_proxy: bool = False,
) -> web.Application:
    """The authenticated MCP endpoint at `/mcp` and the public static console at `/`.

    Without Basic credentials or a JWT key, requests do not require authentication.
    Project memberships control data access. Sessions remain principal-scoped.

    The endpoint refuses a browser `Origin` that is not its own, against DNS rebinding. Behind
    a TLS-terminating reverse proxy the scheme it sees is not the public one, so `trust_proxy`
    turns that check off: the proxy must validate the origin instead.
    """
    policies: list[Authentication] = []
    accounts: list[tuple[str, str]] = []
    if basic:
        for account in basic:
            user, separator, password = account.partition(":")
            if not separator or not user or not password:
                raise ValueError("Basic accounts must use username:password with non-empty values")
            accounts.append((user, password))
        policies.append(BasicAuthentication(*accounts))
    if jwt_pre_shared_key:
        policies.append(
            JWTAuthentication(
                HMACJWTVerifier(jwt_pre_shared_key, issuer=jwt_issuer, audience=jwt_audience)
            )
        )
    if jwt_public_key:
        policies.append(
            JWTAuthentication(
                PublicKeyJWTVerifier(jwt_public_key, issuer=jwt_issuer, audience=jwt_audience)
            )
        )
    if (jwt_issuer or jwt_audience) and not (jwt_public_key or jwt_pre_shared_key):
        raise ValueError("JWT issuer and audience require a JWT public key or pre-shared key")
    if not policies:
        log.warning("no credentials given: every request is served without authentication")
    configured, storage = persistent_registry(backlog, policies)
    middlewares: list[Middleware] = [bind(backlog, configured)]
    endpoint = Endpoint(configured, trust_proxy_origin_validation=trust_proxy)
    if policies:
        public_paths = CONSOLE_ASSETS if console else frozenset()
        middlewares.insert(0, authentication(endpoint, public_paths=public_paths))
    app = web.Application(middlewares=middlewares)
    app[STORAGE] = storage
    endpoint.setup(app, ENDPOINT_PATH)
    if console:
        page = Console(
            endpoint_path=ENDPOINT_PATH,
            title=f"backlog {VERSION}",
            description=CONSOLE_ABOUT,
        )
        page.setup(app, CONSOLE_PATH)

    async def lifespan(app: web.Application) -> AsyncIterator[None]:
        await backlog.store.open()
        try:
            for user, _password in accounts:
                await backlog.store.register(basic_namespace(user), user)
            log.info("database %s", backlog.store.path)
            async for _ in storage.cleanup_ctx(app):
                yield
        finally:
            await backlog.store.close()

    app.cleanup_ctx.append(lifespan)
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
