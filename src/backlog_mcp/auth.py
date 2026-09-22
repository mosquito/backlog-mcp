"""Basic and JWT credentials verified against trusted server configuration."""

from base64 import b64decode
from dataclasses import replace

from aiohttp import web
from aiohttp_tiny_mcp import Authentication, Principal, StaticBasicAuth, Unauthorized
from aiohttp_tiny_mcp.auth import TokenVerifier


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


REALM = "backlog-mcp"


def user_identity(username: str) -> str:
    """A username identifies one account across supported authentication methods."""
    if (
        not username
        or username != username.strip()
        or any(ord(c) < 32 or ord(c) == 127 for c in username)
    ):
        raise ValueError("principal must be a non-empty username without control characters")
    return username


# Trusted request-local result from the pre-dispatch authentication middleware.
VERIFIED: web.RequestKey[dict[Authentication, Principal | None]] = web.RequestKey(
    "backlog_verified_principals", dict
)


def request_token(request: web.Request, *, scheme: str, query_parameter: str) -> str:
    """Read query credentials only when no Authorization header is present."""
    if "Authorization" in request.headers:
        parts = request.headers["Authorization"].split()
        return parts[1] if len(parts) == 2 and parts[0].lower() == scheme.lower() else ""
    token = request.query.get(query_parameter, "")
    return token.replace(" ", "+") if scheme == "Basic" else token


def credentials(request: web.Request) -> tuple[str, str] | None:
    """Decode Basic credentials from the header or the auth query parameter."""
    return decode_pair(request_token(request, scheme="Basic", query_parameter="auth"))


class BasicAuthentication(StaticBasicAuth):
    """Use library verification with Backlog's header and query transports."""

    def __init__(self, *accounts: tuple[str, str]) -> None:
        super().__init__(*accounts, realm=REALM)

    async def authenticate(self, request: web.Request) -> Principal | None:
        verified: dict[Authentication, Principal | None] = request.setdefault(VERIFIED, {})
        if self in verified:
            return verified[self]
        given = credentials(request)
        if given is None or any(ord(char) < 32 or ord(char) == 127 for char in "".join(given)):
            verified[self] = None
            return None
        principal = await self.verify(*given)
        verified[self] = (
            replace(principal, namespace=user_identity(given[0])) if principal else None
        )
        return verified[self]


class JWTAuthentication(Authentication):
    """Authenticate externally issued JWTs without advertising an OAuth server."""

    def __init__(self, verifier: TokenVerifier) -> None:
        super().__init__()
        self.verifier = verifier

    async def authenticate(self, request: web.Request) -> Principal | None:
        verified: dict[Authentication, Principal | None] = request.setdefault(VERIFIED, {})
        if self in verified:
            return verified[self]
        token = request_token(request, scheme="Bearer", query_parameter="token")
        principal = await self.verifier.verify(token) if token else None
        if principal and principal.subject:
            try:
                identity = user_identity(principal.subject)
            except ValueError:
                verified[self] = None
            else:
                verified[self] = replace(principal, namespace=identity)
        else:
            verified[self] = None
        return verified[self]

    def challenge(self, refusal: Unauthorized) -> str:
        return f'Bearer realm="{REALM}"'
