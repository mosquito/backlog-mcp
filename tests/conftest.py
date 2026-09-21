import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import jwt
import pytest
from aiohttp import encode_basic_auth
from aiohttp_tiny_mcp.client_base import BaseClient
from aiohttp_tiny_mcp.hub import MemoryHub
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.testing import connect
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from backlog_mcp.auth import BasicAuthVerifier, CredentialsVerifier, JWTVerifier
from backlog_mcp.backlog import Backlog
from backlog_mcp.server import ACTIVE, registry
from backlog_mcp.state import State
from backlog_mcp.store import Store


@pytest.fixture
async def backlog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Backlog]:
    # The global registry's notification condition must belong to this test's event loop.
    monkeypatch.setattr(registry, "hub", MemoryHub())
    root = tmp_path / "workspace"
    root.mkdir()
    # Keep checkout discovery inside the temporary directory.
    (root / ".git").mkdir()
    # A project root must exist: the tests register `../other` as a second project.
    (tmp_path / "other").mkdir()
    instance = Backlog(
        Store(tmp_path / "backlog.sqlite3"),
        State(tmp_path / "state.json"),
        cwd=root,
        computer="here",
    )
    token = ACTIVE.set(instance)
    try:
        yield instance
    finally:
        ACTIVE.reset(token)
        await instance.store.close()


@pytest.fixture(params=AdapterSet.default().versions)
async def client(backlog: Backlog, request: pytest.FixtureRequest) -> AsyncIterator[BaseClient]:
    """Exercise the application through each supported MCP protocol revision."""
    async with connect(registry, adapter=request.param) as connected:
        yield connected


# Authentication fixtures share generated key material across the test session.
# Tokens are created per test so their expiry does not depend on suite duration.
TEST_PSK = "test-only-pre-shared-key-32-bytes!"


class KeyPair(NamedTuple):
    private: rsa.RSAPrivateKey
    public: str


@pytest.fixture(scope="session")
def key_pair() -> KeyPair:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return KeyPair(private, public)


@pytest.fixture(scope="session")
def jwt_verifier(key_pair: KeyPair) -> JWTVerifier:
    return JWTVerifier.configure(public_key=key_pair.public)


@pytest.fixture(scope="session")
def hmac_verifier() -> JWTVerifier:
    return JWTVerifier.configure(pre_shared_key=TEST_PSK)


@pytest.fixture(scope="session")
def basic_verifier() -> BasicAuthVerifier:
    return BasicAuthVerifier("user", "password")


@dataclass
class Credentials:
    verifier: CredentialsVerifier
    options: dict[str, str]
    token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"{self.verifier.scheme} {self.token}"}

    @property
    def query(self) -> dict[str, str]:
        return {self.verifier.query_parameter: self.token}

    @property
    def cli(self) -> list[str]:
        if self.verifier.scheme == "Basic":
            return ["--user", self.options["user"], "--password", self.options["password"]]
        return ["--token", self.token]


@pytest.fixture(params=["basic", "hs256", "rs256"])
def credentials(
    request: pytest.FixtureRequest,
    key_pair: KeyPair,
    jwt_verifier: JWTVerifier,
    hmac_verifier: JWTVerifier,
    basic_verifier: BasicAuthVerifier,
) -> Credentials:
    claims = {"exp": int(time.time()) + 300}
    match request.param:
        case "basic":
            return Credentials(
                basic_verifier,
                {"user": "user", "password": "password"},
                encode_basic_auth("user", "password").split()[1],
            )
        case "hs256":
            return Credentials(
                hmac_verifier,
                {"jwt_pre_shared_key": TEST_PSK},
                jwt.encode(claims, TEST_PSK, algorithm="HS256"),
            )
        case _:
            return Credentials(
                jwt_verifier,
                {"jwt_public_key": key_pair.public},
                jwt.encode(claims, key_pair.private, algorithm="RS256"),
            )
