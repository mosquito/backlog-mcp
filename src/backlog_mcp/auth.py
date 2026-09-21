"""Basic and JWT credentials verified against trusted server configuration."""

import math
from base64 import b64decode
from dataclasses import dataclass, field, replace
from pathlib import Path

import jwt
from aiohttp import web
from aiohttp_tiny_mcp import Authentication, Principal, StaticBasicAuth, Unauthorized
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, rsa

type VerificationKey = (
    bytes
    | rsa.RSAPublicKey
    | ec.EllipticCurvePublicKey
    | ed25519.Ed25519PublicKey
    | ed448.Ed448PublicKey
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


REALM = "backlog-mcp"
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

    def __init__(self, user: str, password: str) -> None:
        super().__init__(user, password, realm=REALM)

    async def authenticate(self, request: web.Request) -> Principal | None:
        verified: dict[Authentication, Principal | None] = request.setdefault(VERIFIED, {})
        if self in verified:
            return verified[self]
        given = credentials(request)
        if given is None or any(ord(char) < 32 or ord(char) == 127 for char in "".join(given)):
            verified[self] = None
            return None
        principal = await self.verify(*given)
        # Empty namespace preserves the shared Hub used by HTTP and stdio writers.
        verified[self] = replace(principal, namespace="") if principal else None
        return verified[self]


@dataclass(frozen=True)
class JWTVerifier:
    key: VerificationKey = field(repr=False)
    algorithms: tuple[str, ...]
    issuer: str = ""
    audience: str = ""

    def verify(self, credentials: str) -> Principal | None:
        """Check the signature, required expiry, and configured issuer and audience."""
        try:
            claims = jwt.decode(
                credentials,
                self.key,
                algorithms=self.algorithms,
                issuer=self.issuer or None,
                audience=self.audience or None,
                options={"require": ["exp"], "verify_aud": bool(self.audience)},
            )
            subject = claims.get("sub")
            issuer = claims.get("iss")
            client_id = claims.get("client_id", claims.get("azp", ""))
            if subject is not None and not isinstance(subject, str):
                return None
            if issuer is not None and not isinstance(issuer, str):
                return None
            if not isinstance(client_id, str):
                return None
            scope = claims.get("scope", claims.get("scp", []))
            if isinstance(scope, str):
                scopes = frozenset(scope.split())
            elif isinstance(scope, list) and all(isinstance(item, str) for item in scope):
                scopes = frozenset(scope)
            else:
                return None
            expires_at = float(claims["exp"])
            if not math.isfinite(expires_at):
                return None
            return Principal(
                subject=subject,
                issuer=issuer,
                client_id=client_id,
                scopes=scopes,
                expires_at=expires_at,
                claims=claims,
                namespace="",
            )
        except jwt.InvalidTokenError, ValueError, TypeError, OverflowError:
            return None


class HMACJWTVerifier(JWTVerifier):
    """Verify HS256 JWTs with a configured pre-shared key."""

    def __init__(self, pre_shared_key: str, *, issuer: str = "", audience: str = "") -> None:
        hmac = jwt.algorithms.HMACAlgorithm(jwt.algorithms.HMACAlgorithm.SHA256)
        try:
            key = hmac.prepare_key(pre_shared_key)
        except jwt.InvalidKeyError as error:
            raise ValueError("the JWT pre-shared key must not be an asymmetric key") from error
        if len(key) < 32:
            raise ValueError("the JWT pre-shared key must contain at least 32 bytes")
        super().__init__(key, ("HS256",), issuer, audience)


class PublicKeyJWTVerifier(JWTVerifier):
    """Verify RSA, EC, or EdDSA JWTs with a configured public key."""

    def __init__(self, public_key: str, *, issuer: str = "", audience: str = "") -> None:
        material = public_key.strip()
        if not material.startswith("-----BEGIN "):
            material = Path(material).expanduser().read_text(encoding="utf-8")
        try:
            loaded = serialization.load_pem_public_key(material.encode("utf-8"))
        except (ValueError, TypeError, UnsupportedAlgorithm) as error:
            raise ValueError("the JWT public key must be a supported PEM public key") from error
        algorithms: tuple[str, ...]
        if isinstance(loaded, rsa.RSAPublicKey):
            if loaded.key_size < 2048:
                raise ValueError("the JWT RSA public key must contain at least 2048 bits")
            algorithms = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512")
        elif isinstance(loaded, ec.EllipticCurvePublicKey):
            curves = {"secp256r1": "ES256", "secp384r1": "ES384", "secp521r1": "ES512"}
            algorithm = curves.get(loaded.curve.name)
            if algorithm is None:
                raise ValueError("unsupported JWT elliptic curve")
            algorithms = (algorithm,)
        elif isinstance(loaded, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
            algorithms = ("EdDSA",)
        else:
            raise ValueError("unsupported JWT public key type")
        super().__init__(loaded, algorithms, issuer, audience)


class JWTAuthentication(Authentication):
    """Authenticate externally issued JWTs without advertising an OAuth server."""

    def __init__(self, verifier: JWTVerifier) -> None:
        super().__init__()
        self.verifier = verifier

    async def authenticate(self, request: web.Request) -> Principal | None:
        verified: dict[Authentication, Principal | None] = request.setdefault(VERIFIED, {})
        if self in verified:
            return verified[self]
        token = request_token(request, scheme="Bearer", query_parameter="token")
        verified[self] = self.verifier.verify(token) if token else None
        return verified[self]

    def challenge(self, refusal: Unauthorized) -> str:
        return f'Bearer realm="{REALM}"'
