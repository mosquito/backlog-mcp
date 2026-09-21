"""JWT key selection and authentication across the HTTP transport."""

import asyncio
import hashlib
import hmac
import json
import time
from base64 import urlsafe_b64encode
from pathlib import Path
from typing import Any

import jwt
import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from aiohttp_tiny_mcp import Principal
from aiohttp_tiny_mcp.client import Client
from aiohttp_tiny_mcp.protocol.selection import AdapterSet
from aiohttp_tiny_mcp.testing import pick
from conftest import TEST_PSK, Credentials, KeyPair
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519
from yarl import URL

from backlog_mcp import cli, httpd
from backlog_mcp.auth import JWTAuthentication, JWTVerifier
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
                assert response.headers["WWW-Authenticate"] == (
                    f'{credentials.scheme} realm="backlog-mcp"'
                    + (', charset="UTF-8"' if credentials.scheme == "Basic" else "")
                )


async def test_authentication_policies(credentials: Credentials) -> None:
    request = make_mocked_request("GET", "/mcp", headers=credentials.headers)
    principal = await credentials.verifier.authenticate(request)
    assert principal is not None
    assert principal.namespace == ""
    for value in ("invalid", ""):
        request = make_mocked_request(
            "GET", "/mcp", headers={"Authorization": f"{credentials.scheme} {value}"}
        )
        assert await credentials.verifier.authenticate(request) is None


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
        url = URL("/mcp").with_query({credentials.query_parameter: "invalid"})
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
            session.headers["Authorization"] = f"{credentials.scheme} invalid"
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
        if credentials.scheme == "Bearer":
            assert await asyncio.to_thread(cli.main, [*flags, "--user", "user", "list"]) == 1
            assert "not both" in capsys.readouterr().err


@pytest.mark.parametrize(
    "claims",
    [
        {"sub": 1},
        {"iss": []},
        {"client_id": []},
        {"azp": 1},
        {"scope": {}},
        {"scp": [1]},
        {"exp": "NaN"},
    ],
)
def test_invalid_principal_claims_are_rejected(claims: dict[str, object]) -> None:
    encoded = jwt.api_jws.encode(
        json.dumps({"exp": time.time() + 300, **claims}).encode(), PSK, algorithm="HS256"
    )
    assert JWTVerifier.configure(pre_shared_key=PSK).verify(encoded) is None


@pytest.mark.parametrize(
    ("claims", "identity", "scopes"),
    [
        ({}, "|client:", frozenset()),
        (
            {"iss": "issuer", "sub": "alice", "scope": "read write"},
            "issuer|alice",
            frozenset({"read", "write"}),
        ),
        ({"client_id": "cli", "scp": ["read"]}, "|client:cli", frozenset({"read"})),
        ({"azp": "cli"}, "|client:cli", frozenset()),
    ],
)
def test_verified_claims_define_the_principal(
    claims: dict[str, object], identity: str, scopes: frozenset[str]
) -> None:
    principal = JWTVerifier.configure(pre_shared_key=PSK).verify(token(**claims))
    assert principal is not None
    assert principal.identity == identity
    assert principal.scopes == scopes
    assert principal.namespace == ""
    assert principal.expires_at is not None
    assert principal.claims.items() >= claims.items()


@pytest.mark.parametrize("adapter", ["2025-03-26", "2025-06-18", "2025-11-25"])
async def test_sessions_belong_to_the_verified_identity(backlog: Backlog, adapter: str) -> None:
    async with (
        TestServer(httpd.application(backlog, jwt_pre_shared_key=PSK)) as server,
        ClientSession(
            headers={"Authorization": f"Bearer {token(sub='alice', iss='issuer')}"}
        ) as http,
        Client(str(server.make_url("/mcp")), pick(adapter), session=http) as client,
    ):
        await client.initialize()
        assert client.session_id is not None
        for claims, status in (
            ({"sub": "bob", "iss": "issuer"}, 404),
            ({"sub": "alice", "iss": "other"}, 404),
            ({"sub": "alice", "iss": "issuer", "exp": time.time() + 600}, 200),
        ):
            headers = {
                "Authorization": f"Bearer {token(**claims)}",
                "Mcp-Session-Id": client.session_id,
                "MCP-Protocol-Version": adapter,
                "Accept": "application/json, text/event-stream",
            }
            async with http.post(
                server.make_url("/mcp"),
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 77,
                    "method": "tools/list",
                    "params": {},
                },
            ) as response:
                assert response.status == status


async def test_applications_keep_auth_and_sessions_separate(backlog: Backlog) -> None:
    from backlog_mcp.server import registry

    async with (
        TestServer(httpd.application(backlog, jwt_pre_shared_key=PSK)) as jwt_server,
        TestServer(httpd.application(backlog, user="user", password="password")) as basic_server,
        TestServer(httpd.application(backlog)) as open_server,
        ClientSession(headers={"Authorization": f"Bearer {token(sub='alice')}"}) as http,
        Client(str(jwt_server.make_url("/mcp")), pick("2025-03-26"), session=http) as client,
    ):
        await client.initialize()
        assert registry.auth is None
        assert client.session_id is not None
        for server, authorization, status in (
            (jwt_server, "", 401),
            (basic_server, f"Bearer {token(sub='alice')}", 401),
            (basic_server, "Basic dXNlcjpwYXNzd29yZA==", 404),
            (open_server, "", 404),
        ):
            async with http.post(
                server.make_url("/mcp"),
                headers={
                    "Authorization": authorization,
                    "Mcp-Session-Id": client.session_id,
                    "MCP-Protocol-Version": "2025-03-26",
                    "Accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "id": 77, "method": "tools/list", "params": {}},
            ) as response:
                assert response.status == status


async def test_http_request_verifies_jwt_once(
    backlog: Backlog, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = JWTVerifier.verify
    calls = 0

    def counted(self: JWTVerifier, credentials: str) -> Principal | None:
        nonlocal calls
        calls += 1
        return original(self, credentials)

    monkeypatch.setattr(JWTVerifier, "verify", counted)
    async with TestClient(TestServer(httpd.application(backlog, jwt_pre_shared_key=PSK))) as client:
        response = await client.post(
            "/mcp", headers={"Authorization": f"Bearer {token()}"}, json={}
        )
        assert response.status != 401
    assert calls == 1


async def test_authenticated_users_share_http_and_stdio_notifications(backlog: Backlog) -> None:
    from aiohttp_tiny_mcp.testing import connect

    from backlog_mcp.server import TASK_CHANGED, registry

    async with (
        TestServer(httpd.application(backlog, jwt_pre_shared_key=PSK)) as server,
        ClientSession(headers={"Authorization": f"Bearer {token(sub='alice')}"}) as alice_http,
        ClientSession(headers={"Authorization": f"Bearer {token(sub='bob')}"}) as bob_http,
        Client(str(server.make_url("/mcp")), pick("2026-07-28"), session=alice_http) as listener,
        Client(str(server.make_url("/mcp")), pick("2026-07-28"), session=bob_http) as writer,
        connect(registry, adapter="2026-07-28") as stdio,
    ):
        await listener.initialize()
        await writer.initialize()
        frames: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def collect() -> None:
            async for frame in listener.listen(methods=[TASK_CHANGED]):
                await frames.put(frame)

        collecting = asyncio.create_task(collect())
        try:
            async with asyncio.timeout(5):
                while not listener.accepted:
                    await asyncio.sleep(0.01)
            for source, title in ((writer, "From Bob"), (stdio, "From stdio")):
                result = await source.call_tool("task_add", {"title": title})
                assert not result.is_error
                frame = await asyncio.wait_for(frames.get(), 5)
                assert frame["method"] == TASK_CHANGED
                assert frame["params"]["title"] == title
        finally:
            collecting.cancel()
            await asyncio.gather(collecting, return_exceptions=True)


async def test_middleware_preserves_policy_refusal_status() -> None:
    policy = JWTAuthentication(JWTVerifier.configure(pre_shared_key=PSK))
    policy.required_scopes = ("read",)
    app = web.Application(middlewares=[httpd.authentication(policy)])
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/mcp", headers={"Authorization": f"Bearer {token()}"})
        assert response.status == 403
        assert (await response.json())["error"] == "insufficient_scope"
