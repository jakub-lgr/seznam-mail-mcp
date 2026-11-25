"""Multi-user HTTP mode: Streamable HTTP MCP endpoint + OAuth + login pages."""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlparse

from cryptography.exceptions import InvalidTag
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette

from .auth import SCOPE, SeznamAuthProvider
from .config import Settings
from .crypto import SecretBox, load_or_create_key
from .db import create_engine, init_db
from .mail import MailAccount
from .server import build_server
from .store import Store, utcnow
from .web import install_routes

log = logging.getLogger(__name__)

TOUCH_INTERVAL = timedelta(minutes=5)


@dataclass
class HttpApp:
    app: Starlette
    server: MCPServer
    store: Store
    engine: AsyncEngine


def build_http_app(settings: Settings, engine: AsyncEngine | None = None) -> HttpApp:
    engine = engine or create_engine(settings)
    store = Store(engine)
    box = SecretBox(load_or_create_key(settings.secret_key_file))
    provider = SeznamAuthProvider(store, settings)

    async def resolve_account(_ctx: Context) -> MailAccount:
        token = get_access_token()
        if token is None or not token.subject:
            raise ToolError("Nepřihlášeno.")
        user = await store.get_user(int(token.subject))
        if user is None or not user.allowed or not user.enc_secret:
            raise ToolError("Účet je odpojený. Připoj e-mail v aplikaci znovu.")
        try:
            password = box.decrypt(user.enc_secret, user.email)
        except (InvalidTag, ValueError):
            log.error("cannot decrypt credentials of user %s (wrong SECRET_KEY_FILE?)", user.id)
            raise ToolError("Uložené přihlašovací údaje nejde přečíst. Připoj e-mail znovu.") from None
        if user.last_used_at is None or utcnow() - user.last_used_at > TOUCH_INTERVAL:
            await store.touch_user(user.id)
        return MailAccount(user.email, password, settings.imap_host, settings.imap_port)

    public = urlparse(settings.public_url)
    server = build_server(
        resolve_account,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=settings.public_url,
            resource_server_url=f"{settings.public_url}/mcp",
            validate_token_resource=False,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[SCOPE],
        ),
    )
    install_routes(server, provider, store, box, settings)

    local_hosts = [f"127.0.0.1:{settings.port}", f"localhost:{settings.port}"]
    app = server.streamable_http_app(
        transport_security=TransportSecuritySettings(
            allowed_hosts=[public.netloc, *local_hosts],
            allowed_origins=[f"{public.scheme}://{public.netloc}", *settings.allowed_origins],
        ),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        await init_db(engine)
        await store.purge_expired()
        async with server.session_manager.run():
            yield
        await engine.dispose()

    app.router.lifespan_context = lifespan
    return HttpApp(app=app, server=server, store=store, engine=engine)


def serve(settings: Settings) -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    built = build_http_app(settings)
    log.info("seznam-mail-mcp on %s:%s, public %s, db %s", settings.host, settings.port, settings.public_url, settings.backend)
    uvicorn.run(
        built.app,
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        server_header=False,
        log_level="info",
    )
