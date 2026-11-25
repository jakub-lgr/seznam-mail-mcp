"""End-to-end test of HTTP mode: real uvicorn server, OAuth flow, MCP tool call."""

import base64
from types import SimpleNamespace
import os
import hashlib
import re
import secrets
import socket
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import httpx2
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from seznam_mail_mcp import mail
from seznam_mail_mcp.app import build_http_app
from seznam_mail_mcp.config import Settings, normalize_database_url
from seznam_mail_mcp.store import Store
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from .test_mail import PLAIN, REPLY, FakeIMAP

CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


BACKENDS = ["sqlite"] + (["postgresql"] if os.environ.get("TEST_DATABASE_URL") else [])


async def _reset_postgres(url: str) -> None:
    from seznam_mail_mcp.db import metadata
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.drop_all)
    await engine.dispose()


@pytest.fixture(params=BACKENDS)
async def server(request, tmp_path: Path, monkeypatch):
    FakeIMAP.boxes = {"INBOX": {1: PLAIN}, "Odeslané": {5: REPLY}, "Koncepty": {}}
    FakeIMAP.appended = []
    FakeIMAP.flags = {}
    monkeypatch.setattr(mail, "IMAPClient", FakeIMAP)  # password "ok" logs in

    port = free_port()
    if request.param == "postgresql":
        db_url = normalize_database_url(os.environ["TEST_DATABASE_URL"], tmp_path)
        await _reset_postgres(db_url)
    else:
        db_url = normalize_database_url("", tmp_path / "data")
    settings = Settings(
        database_url=db_url,
        data_dir=tmp_path / "data",
        public_url=f"http://127.0.0.1:{port}",
        port=port,
        secret_key_file=tmp_path / "data" / "secret.key",
    )
    built = build_http_app(settings)
    uv = uvicorn.Server(uvicorn.Config(built.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if uv.started:
            break
        time.sleep(0.05)
    # test-side DB access gets its own engine: the server runs on another event loop/thread
    side_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    yield settings, SimpleNamespace(store=Store(side_engine))
    uv.should_exit = True
    thread.join(5)
    await side_engine.dispose()


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def allow(built, email):
    await built.store.set_allowed(email, True)


async def test_full_oauth_flow_and_tool_call(server):
    settings, built = server
    base = settings.public_url
    with httpx.Client(base_url=base, follow_redirects=False) as http:
        # discovery
        prm = http.get("/.well-known/oauth-protected-resource/mcp").json()
        assert prm["authorization_servers"][0].rstrip("/") == base
        asm = http.get("/.well-known/oauth-authorization-server").json()
        assert asm["registration_endpoint"].endswith("/register")

        # unknown redirect URIs are refused
        bad = http.post("/register", json={"redirect_uris": ["https://evil.example/cb"], "client_name": "Evil"})
        assert bad.status_code == 400 and bad.json()["error"] == "invalid_redirect_uri"

        # dynamic client registration (what Claude does)
        reg = http.post(
            "/register",
            json={"redirect_uris": [CLAUDE_CALLBACK], "client_name": "Claude", "token_endpoint_auth_method": "client_secret_post"},
        )
        assert reg.status_code == 201, reg.text
        client = reg.json()

        verifier, challenge = pkce()
        auth = http.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client["client_id"],
                "redirect_uri": CLAUDE_CALLBACK,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "xyz",
                "scope": "mail",
            },
        )
        assert auth.status_code == 302
        login_url = urlparse(auth.headers["location"])
        assert login_url.path == "/login"
        request_id = parse_qs(login_url.query)["request"][0]

        page = http.get(f"/login?request={request_id}")
        assert page.status_code == 200 and "Claude" in page.text and "claude.ai" in page.text
        assert page.headers["x-frame-options"] == "DENY"

        # not on the allowlist -> refused, no IMAP attempt
        r = http.post("/login", data={"request": request_id, "email": "me@seznam.cz", "password": "ok"})
        assert r.status_code == 403

        await allow(built, "Me@Seznam.cz")
        r = http.post("/login", data={"request": request_id, "email": "me@seznam.cz", "password": "wrong"})
        assert r.status_code == 401 and "Přihlášení k e-mailu selhalo" in r.text

        r = http.post("/login", data={"request": request_id, "email": "me@seznam.cz", "password": "ok"})
        assert r.status_code == 302
        back = urlparse(r.headers["location"])
        assert f"{back.scheme}://{back.netloc}{back.path}" == CLAUDE_CALLBACK
        q = parse_qs(back.query)
        assert q["state"] == ["xyz"]
        code = q["code"][0]

        # the login link is single-use
        assert http.get(f"/login?request={request_id}").status_code == 400

        token_req = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLAUDE_CALLBACK,
            "code_verifier": verifier,
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
        }
        tok = http.post("/token", data=token_req)
        assert tok.status_code == 200, tok.text
        tokens = tok.json()
        assert tokens["token_type"].lower() == "bearer" and tokens["refresh_token"]
        # the code cannot be used twice
        assert http.post("/token", data=token_req).status_code == 400

        # /mcp without a token -> 401 with a pointer to the metadata
        r = http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code == 401 and "resource_metadata" in r.headers.get("www-authenticate", "")

        # refresh token rotation
        ref = http.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
            },
        )
        assert ref.status_code == 200, ref.text
        new_tokens = ref.json()
        assert http.post(
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                  "client_id": client["client_id"], "client_secret": client["client_secret"]},
        ).status_code == 400

    # real MCP session with the bearer token
    headers = {"Authorization": f"Bearer {new_tokens['access_token']}"}
    async with httpx2.AsyncClient(headers=headers) as hc:
        async with streamable_http_client(f"{base}/mcp", http_client=hc) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool("search_emails", {"subject": "faktura"})
                assert not res.is_error, res.content
                assert res.structured_content["messages"][0]["subject"] == "Faktura za říjen"

    # the stored secret is encrypted, never plain text
    user = await built.store.get_user_by_email("me@seznam.cz")
    assert user.auth_method == "app_password" and b"ok" not in user.enc_secret

    # deny -> tokens stop working
    await built.store.set_allowed("me@seznam.cz", False)
    with httpx.Client(base_url=base) as http:
        r = http.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code == 401


async def test_account_disconnect(server):
    settings, built = server
    store = built.store
    await allow(built, "me@seznam.cz")
    user = await store.get_user_by_email("me@seznam.cz")
    from seznam_mail_mcp.crypto import SecretBox, load_or_create_key

    box = SecretBox(load_or_create_key(settings.secret_key_file))
    await store.save_login(user.id, "app_password", box.encrypt("ok", user.email))
    with httpx.Client(base_url=settings.public_url) as http:
        assert http.post("/account", data={"email": "me@seznam.cz", "password": "nope"}).status_code == 401
        r = http.post("/account", data={"email": "me@seznam.cz", "password": "ok"})
        assert r.status_code == 200 and "Hotovo" in r.text
    user = await store.get_user_by_email("me@seznam.cz")
    assert user.enc_secret is None and user.allowed


async def test_login_rate_limit(server):
    settings, built = server
    await allow(built, "me@seznam.cz")
    with httpx.Client(base_url=settings.public_url) as http:
        codes = [http.post("/account", data={"email": "me@seznam.cz", "password": "x"}).status_code for _ in range(7)]
    assert codes[:5] == [401] * 5 and codes[5] == 429


async def test_host_header_checked(server):
    """DNS-rebinding protection: a valid token sent with a foreign Host header is refused."""
    from mcp.shared.auth import OAuthClientInformationFull

    settings, built = server
    info = OAuthClientInformationFull(client_id="c1", redirect_uris=[CLAUDE_CALLBACK])
    await built.store.save_client("c1", "test", info.model_dump_json())
    user = await built.store.set_allowed("me@seznam.cz", True)
    access, _ = await built.store.issue_tokens(user.id, "c1", ["mail"], None, 3600, 3600)
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    with httpx.Client(base_url=settings.public_url, headers={"Authorization": f"Bearer {access}"}) as http:
        assert http.post("/mcp", headers={"Host": "evil.example"}, json=body).status_code == 421
