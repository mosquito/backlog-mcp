"""JWT key selection and authentication across the HTTP transport."""

import asyncio
import hashlib
import hmac
import time
from base64 import urlsafe_b64encode
from pathlib import Path

import jwt
import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer
from aiohttp_tiny_mcp.client import Client
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.testing import pick
from conftest import TEST_PSK, Credentials, KeyPair
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519
from yarl import URL

from backlog_mcp import cli, httpd
from backlog_mcp.auth import JWTVerifier
from backlog_mcp.backlog import Backlog

PSK = TEST_PSK


def token(**claims: object) -> str:
    return jwt.encode({"exp": int(time.time()) + 300, **claims}, PSK, algorithm="HS256")


def public_pem(key: object) -> str:
    return (
        key.public_key()
        .public_bytes(  # type: ignore[attr-defined]
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )


@pytest.mark.parametrize("algorithm", ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512"])
def test_rsa_algorithms_follow_the_configured_key(
    key_pair: KeyPair, jwt_verifier: JWTVerifier, algorithm: str, tmp_path: Path
) -> None:
    key_file = tmp_path / "public.pem"
    key_file.write_text(key_pair.public)
    signed = jwt.encode({"exp": time.time() + 300}, key_pair.private, algorithm=algorithm)
    assert JWTVerifier.configure(public_key=str(key_file)).verify(signed)
    verifier = jwt_verifier
    assert verifier.verify(signed)
    assert not verifier.verify(token())
    assert not verifier.verify(jwt.encode({"exp": time.time() + 300}, "", algorithm="none"))
    header, payload, signature = signed.split(".")
    changed = ("A" if signature[0] != "A" else "B") + signature[1:]
    assert not verifier.verify(f"{header}.{payload}.{changed}")
    # DER public bytes used as an HMAC secret must never bypass RSA verification.
    der = key_pair.private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    header, payload, _ = token().split(".")
    message = f"{header}.{payload}".encode()
    signature = urlsafe_b64encode(hmac.digest(der, message, hashlib.sha256)).rstrip(b"=")
    assert not verifier.verify(f"{message.decode()}.{signature.decode()}")


@pytest.mark.parametrize(
    ("curve", "algorithm"),
    [(ec.SECP256R1(), "ES256"), (ec.SECP384R1(), "ES384"), (ec.SECP521R1(), "ES512")],
)
def test_ec_curve_selects_the_algorithm(curve: ec.EllipticCurve, algorithm: str) -> None:
    key = ec.generate_private_key(curve)
    verifier = JWTVerifier.configure(public_key=public_pem(key))
    assert verifier.algorithms == (algorithm,)
    assert verifier.verify(jwt.encode({"exp": time.time() + 300}, key, algorithm=algorithm))
    assert not verifier.verify(token())


@pytest.mark.parametrize("kind", [ed25519.Ed25519PrivateKey, ed448.Ed448PrivateKey])
def test_eddsa_public_keys(kind: type) -> None:
    key = kind.generate()
    assert JWTVerifier.configure(public_key=public_pem(key)).verify(
        jwt.encode({"exp": time.time() + 300}, key, algorithm="EdDSA")
    )


@pytest.mark.parametrize(
    "claims",
    [
        {"exp": 0},
        {"exp": None},
        {"exp": "invalid"},
        {"exp": float("inf")},
        {"nbf": time.time() + 3600},
        {"iat": time.time() + 3600},
        {"iss": "wrong"},
        {"aud": "wrong"},
    ],
)
def test_invalid_claims_are_rejected(claims: dict[str, object]) -> None:
    verifier = JWTVerifier.configure(pre_shared_key=PSK, issuer="issuer", audience="backlog")
    assert not verifier.verify(token(**{"iss": "issuer", "aud": "backlog", **claims}))


def test_required_claims_and_hmac_algorithm() -> None:
    verifier = JWTVerifier.configure(pre_shared_key=PSK, issuer="issuer", audience="backlog")
    assert verifier.verify(token(iss="issuer", aud=["backlog", "other"]))
    assert not verifier.verify(token())
    assert not verifier.verify(jwt.encode({"iss": "issuer", "aud": "backlog"}, PSK))
    assert not verifier.verify(jwt.encode({"exp": time.time() + 300}, "x" * 32))
    assert not verifier.verify(jwt.encode({"exp": time.time() + 300}, PSK * 2, algorithm="HS512"))
    assert JWTVerifier.configure(pre_shared_key=PSK).verify(token(aud="unspecified"))
    assert PSK not in repr(verifier)


def test_invalid_configuration_fails_before_serving(
    backlog: Backlog, key_pair: KeyPair, tmp_path: Path
) -> None:
    public_key = key_pair.public
    for options in (
        {"jwt_public_key": public_key, "jwt_pre_shared_key": PSK},
        {"jwt_pre_shared_key": PSK, "user": "user", "password": "password"},
        {"jwt_public_key": public_key, "user": "user"},
        {"jwt_issuer": "issuer"},
        {"jwt_audience": "backlog"},
        {"jwt_pre_shared_key": "short"},
        {"jwt_pre_shared_key": public_key},
        {"jwt_public_key": "-----BEGIN PUBLIC KEY-----\ninvalid"},
    ):
        with pytest.raises(ValueError):
            httpd.application(backlog, **options)
    with pytest.raises(OSError):
        JWTVerifier.configure(public_key=str(tmp_path / "missing.pem"))
    private = key_pair.private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    with pytest.raises(ValueError):
        JWTVerifier.configure(public_key=private)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/mcp"),
        ("head", "/mcp"),
        ("post", "/mcp"),
        ("delete", "/mcp"),
        ("post", "/"),
        ("delete", "/"),
        ("get", "/console.js/extra"),
    ],
)
async def test_authentication_protects_all_routes_and_methods(
    backlog: Backlog, path: str, method: str, credentials: Credentials
) -> None:
    async with TestClient(TestServer(httpd.application(backlog, **credentials.options))) as client:
        for headers in ({}, {"Authorization": "Bearer invalid"}, {"Authorization": "Basic abc"}):
            async with client.request(method, path, headers=headers) as response:
                assert response.status == 401
                assert (
                    response.headers["WWW-Authenticate"]
                    == f'{credentials.verifier.scheme} realm="backlog-mcp"'
                )


def test_verifier_implementations(credentials: Credentials) -> None:
    assert credentials.verifier.verify(credentials=credentials.token)
    assert not credentials.verifier.verify(credentials="invalid")
    assert not credentials.verifier.verify(credentials="")


async def test_static_console_loads_without_credentials(
    backlog: Backlog, credentials: Credentials
) -> None:
    async with TestClient(TestServer(httpd.application(backlog, **credentials.options))) as client:
        page = await client.get("/")
        assert page.status == 200
        html = await page.text()
        assert 'data-endpoint="/mcp"' in html
        assert 'src="/console.js"' in html
        assert 'href="/console.css"' in html
        for path in ("/", "/console.js", "/console.css"):
            assert (await client.get(path)).status == 200
            assert (await client.head(path)).status == 200
        assert (await client.get("/mcp")).status == 401
        assert (await client.post("/mcp", json={})).status == 401


async def test_disabled_console_does_not_exempt_static_paths(
    backlog: Backlog, credentials: Credentials
) -> None:
    app = httpd.application(backlog, console=False, **credentials.options)
    async with TestClient(TestServer(app)) as client:
        for path in ("/", "/console.js", "/console.css"):
            assert (await client.get(path)).status == 401
            assert (await client.get(path, headers=credentials.headers)).status == 404


async def test_header_precedes_query(backlog: Backlog, credentials: Credentials) -> None:
    async with TestClient(TestServer(httpd.application(backlog, **credentials.options))) as client:
        url = URL("/mcp").with_query(credentials.query)
        assert (await client.get(url)).status != 401
        assert (await client.get("/mcp", headers=credentials.headers)).status != 401
        for header in ("Bearer invalid", "Basic invalid", ""):
            assert (await client.get(url, headers={"Authorization": header})).status == 401
        url = URL("/mcp").with_query({credentials.verifier.query_parameter: "invalid"})
        assert (await client.get(url, headers=credentials.headers)).status != 401


async def test_expired_query_token_is_rejected(backlog: Backlog) -> None:
    async with TestClient(TestServer(httpd.application(backlog, jwt_pre_shared_key=PSK))) as client:
        assert (await client.get(URL("/mcp").with_query(token=token(exp=0)))).status == 401


@pytest.mark.parametrize("adapter", AdapterSet.default().versions)
@pytest.mark.parametrize("query", [False, True])
async def test_authenticated_mcp_handshake_and_calls(
    backlog: Backlog, adapter: str, query: bool, credentials: Credentials
) -> None:
    async with TestServer(httpd.application(backlog, **credentials.options)) as server:
        endpoint = server.make_url("/mcp")
        if query:
            endpoint = endpoint.with_query(credentials.query)
        headers = {} if query else credentials.headers
        async with (
            ClientSession(headers=headers) as session,
            Client(str(endpoint), pick(adapter), session=session) as client,
        ):
            await client.initialize()
            result = await client.call_tool("task_add", {"title": "JWT task"})
            assert not result.is_error
            assert (await backlog.store.task(1)).title == "JWT task"
            # An existing MCP session does not bypass authentication.
            session.headers["Authorization"] = f"{credentials.verifier.scheme} invalid"
            async with session.get(endpoint) as response:
                assert response.status == 401


@pytest.mark.parametrize("query", [False, True])
async def test_cli_sends_bearer_or_query_token(
    backlog: Backlog,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    query: bool,
    credentials: Credentials,
) -> None:
    monkeypatch.setattr(cli.paths, "config_files", list)
    await backlog.view(computer="client").save(root="", name="JWT project")
    async with TestServer(httpd.application(backlog, **credentials.options)) as server:
        endpoint = server.make_url("/mcp").with_query(computer="client", project="jwt-project")
        auth = [] if query else credentials.cli
        if query:
            endpoint = endpoint.update_query(credentials.query)
        flags = ["--url", str(endpoint), "--state", str(tmp_path / "state.json"), *auth]
        assert await asyncio.to_thread(cli.main, [*flags, "add", "CLI JWT task"]) == 0
        assert "added #1 to jwt-project" in capsys.readouterr().out
        if credentials.verifier.scheme == "Bearer":
            assert await asyncio.to_thread(cli.main, [*flags, "--user", "user", "list"]) == 1
            assert "not both" in capsys.readouterr().err
