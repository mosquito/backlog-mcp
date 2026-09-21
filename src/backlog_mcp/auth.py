"""Basic and JWT credentials verified against trusted server configuration."""

from abc import ABC, abstractmethod
from base64 import b64decode
from dataclasses import dataclass, field
from pathlib import Path
from secrets import compare_digest
from typing import ClassVar

import jwt
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


class CredentialsVerifier(ABC):
    """Verify credentials using the authentication mode configured by the server."""

    scheme: ClassVar[str]
    query_parameter: ClassVar[str]

    @abstractmethod
    def verify(self, credentials: str) -> bool:
        """Return whether the supplied credentials are valid."""
        raise NotImplementedError


@dataclass(frozen=True)
class BasicAuthVerifier(CredentialsVerifier):
    user: str
    password: str = field(repr=False)
    scheme: ClassVar[str] = "Basic"
    query_parameter: ClassVar[str] = "auth"

    def verify(self, credentials: str) -> bool:
        given = decode_pair(credentials)
        if given is None:
            return False
        matched = compare_digest(given[0].encode(), self.user.encode())
        matched &= compare_digest(given[1].encode(), self.password.encode())
        return matched


@dataclass(frozen=True)
class JWTVerifier(CredentialsVerifier):
    scheme: ClassVar[str] = "Bearer"
    query_parameter: ClassVar[str] = "token"
    key: VerificationKey = field(repr=False)
    algorithms: tuple[str, ...]
    issuer: str = ""
    audience: str = ""

    @classmethod
    def configure(
        cls, *, public_key: str = "", pre_shared_key: str = "", issuer: str = "", audience: str = ""
    ) -> JWTVerifier:
        """Load a PEM public key or an HMAC secret once, before accepting requests."""
        if bool(public_key) == bool(pre_shared_key):
            raise ValueError("configure exactly one JWT public key or pre-shared key")
        if pre_shared_key:
            hmac = jwt.algorithms.HMACAlgorithm(jwt.algorithms.HMACAlgorithm.SHA256)
            try:
                key = hmac.prepare_key(pre_shared_key)
            except jwt.InvalidKeyError as error:
                raise ValueError("the JWT pre-shared key must not be an asymmetric key") from error
            if len(key) < 32:
                raise ValueError("the JWT pre-shared key must contain at least 32 bytes")
            return cls(key, ("HS256",), issuer, audience)

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
        return cls(loaded, algorithms, issuer, audience)

    def verify(self, credentials: str) -> bool:
        """Check the signature, required expiry, and configured issuer and audience."""
        try:
            jwt.decode(
                credentials,
                self.key,
                algorithms=self.algorithms,
                issuer=self.issuer or None,
                audience=self.audience or None,
                options={"require": ["exp"], "verify_aud": bool(self.audience)},
            )
        except jwt.InvalidTokenError, ValueError, TypeError, OverflowError:
            return False
        return True
