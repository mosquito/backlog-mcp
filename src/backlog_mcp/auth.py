"""Basic and JWT credentials verified against trusted server configuration."""

import asyncio
import re
from base64 import b64decode
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial

import bcrypt
from aiohttp import web
from aiohttp_tiny_mcp import Authentication, BasicAuth, Principal, Unauthorized
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
BCRYPT_COST = 10


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


def validate_account(username: str, password: str) -> None:
    """Reject values that cannot be represented by HTTP Basic credentials."""
    user_identity(username)
    if (
        not password
        or ":" in username
        or any(ord(char) < 32 or ord(char) == 127 for char in password)
    ):
        raise ValueError(
            "Basic accounts need a non-empty password without controls "
            "and a username without a colon"
        )


def validate_bcrypt_cost(value: int | str) -> int:
    """Accept the bcrypt work-factor range."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("Bcrypt cost must be an integer between 4 and 31")
    cost = int(value)
    if not 4 <= cost <= 31:
        raise ValueError("Bcrypt cost must be between 4 and 31")
    return cost


def bcrypt_account(value: str, cost: int = BCRYPT_COST) -> str:
    """Convert one plaintext entry or validate an existing bcrypt entry."""
    cost = validate_bcrypt_cost(cost)
    if value.startswith("bcrypt|") and ":" not in value:
        username, separator, hashed = value.removeprefix("bcrypt|").rpartition("|")
        if not separator or not re.fullmatch(
            r"\$2[aby]\$(?:0[4-9]|[12][0-9]|3[01])\$[./A-Za-z0-9]{53}", hashed
        ):
            raise ValueError("Use bcrypt|username|<hash> with a valid bcrypt hash")
        validate_account(username, hashed)
        return value
    username, separator, password = value.partition(":")
    if not separator:
        raise ValueError("Use username:password or bcrypt|username|<hash> for Basic accounts")
    validate_account(username, password)
    encoded = password.encode("utf-8")
    if len(encoded) > 72:
        raise ValueError("Bcrypt passwords must not exceed 72 UTF-8 bytes")
    hashed = bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=cost)).decode("ascii")
    return f"bcrypt|{username}|{hashed}"


def normalize_accounts(values: Sequence[str], cost: int = BCRYPT_COST) -> list[str]:
    """Hash configuration values in a worker while keeping existing hashes unchanged."""
    cost = validate_bcrypt_cost(cost)
    if not values:
        return []
    with ThreadPoolExecutor(max_workers=1) as executor:
        return list(executor.map(partial(bcrypt_account, cost=cost), values))


class BasicAuthentication(BasicAuth):
    """Verify bcrypt passwords over Basic headers and query credentials."""

    def __init__(
        self,
        *accounts: tuple[str, str],
        hashed_accounts: Sequence[tuple[str, str]] = (),
        cost: int = BCRYPT_COST,
    ) -> None:
        super().__init__(realm=REALM)
        if not accounts and not hashed_accounts:
            raise ValueError("At least one Basic account is required")
        for username, password in (*accounts, *hashed_accounts):
            validate_account(username, password)
        normalized = normalize_accounts(
            [f"{username}:{password}" for username, password in accounts]
            + [f"bcrypt|{username}|{hashed}" for username, hashed in hashed_accounts],
            cost=cost,
        )
        self.password_hashes: dict[str, bytes] = {}
        for value in normalized:
            username, hashed = value.removeprefix("bcrypt|").rsplit("|", 1)
            if username in self.password_hashes:
                raise ValueError("Basic account usernames must be unique")
            self.password_hashes[username] = hashed.encode("ascii")

    async def verify(self, username: str, password: str) -> Principal | None:
        hashed = self.password_hashes.get(username)
        expected = hashed if hashed is not None else next(iter(self.password_hashes.values()))
        encoded = password.encode("utf-8")
        if len(encoded) > 72:
            return None
        try:
            matched = await asyncio.to_thread(bcrypt.checkpw, encoded, expected)
        except ValueError:
            return None
        return Principal(subject=username) if hashed is not None and matched else None

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
