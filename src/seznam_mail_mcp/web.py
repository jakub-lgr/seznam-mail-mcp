"""Browser-facing pages: login (OAuth consent) and account disconnect."""

from __future__ import annotations

import html
import logging
from urllib.parse import urlparse

import anyio
from cryptography.exceptions import InvalidTag
from mcp.server.auth.provider import AuthorizationParams, construct_redirect_uri
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import mail
from .auth import SeznamAuthProvider
from .config import Settings
from .crypto import SecretBox, constant_time_equal
from .mail import MailAccount, MailError
from .store import Store, normalize_email

log = logging.getLogger(__name__)

MAX_FAILURES_PER_EMAIL = 5
MAX_FAILURES_PER_IP = 20
FAILURE_WINDOW = 15 * 60

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https: http://localhost:* http://127.0.0.1:*; base-uri 'none'; frame-ancestors 'none'",
}

_CSS = """
:root{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--fg:#1d2230;--muted:#5d6577;--line:#d9dde5;--accent:#c4161c;--err:#b42318;--ok:#067647}
@media (prefers-color-scheme:dark){:root{--bg:#12141a;--card:#1b1f27;--fg:#e8eaf0;--muted:#9aa3b5;--line:#2f3542;--accent:#ff5a5f;--err:#ff8a80;--ok:#6ce9a6}}
*{box-sizing:border-box}body{margin:0;font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg);display:flex;min-height:100vh;align-items:center;justify-content:center;padding:24px}
main{background:var(--card);border:1px solid var(--line);border-radius:14px;max-width:420px;width:100%;padding:28px}
h1{font-size:1.25rem;margin:0 0 4px}p{margin:8px 0}.muted{color:var(--muted);font-size:.9rem}
label{display:block;font-weight:600;margin:16px 0 6px}input{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:transparent;color:inherit;font:inherit}
button{margin-top:20px;width:100%;padding:11px;border:0;border-radius:8px;background:var(--accent);color:#fff;font:inherit;font-weight:600;cursor:pointer}
.err{color:var(--err);font-weight:600}.ok{color:var(--ok);font-weight:600}.box{border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:14px 0}
code{font-size:.9em}
"""


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = (
        f"<!doctype html><html lang=cs><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title>"
        f"<style>{_CSS}</style></head><body><main>{body}</main></body></html>"
    )
    return HTMLResponse(doc, status_code=status, headers=SECURITY_HEADERS)


def _e(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def install_routes(server: MCPServer, provider: SeznamAuthProvider, store: Store, box: SecretBox, settings: Settings) -> None:
    async def too_many_failures(email: str | None, ip: str | None) -> bool:
        if ip and await store.recent_failures(ip=ip, window=FAILURE_WINDOW) >= MAX_FAILURES_PER_IP:
            return True
        return bool(email) and await store.recent_failures(email=email, window=FAILURE_WINDOW) >= MAX_FAILURES_PER_EMAIL

    def login_form(request_id: str, client_name: str, redirect_host: str, email: str = "", error: str = "", status: int = 200) -> HTMLResponse:
        err = f"<p class=err role=alert>{_e(error)}</p>" if error else ""
        return page(
            "Přihlášení k e-mailu",
            f"""<h1>Připojit e-mail na Seznamu</h1>
<p><strong>{_e(client_name)}</strong> chce přístup k tvé schránce.</p>
<div class=box><p class=muted>Aplikace bude moct číst e-maily a ukládat koncepty. Nemůže nic odeslat ani smazat.</p>
<p class=muted>Po přihlášení tě pošlu zpátky na <code>{_e(redirect_host)}</code>.</p></div>
{err}
<form method=post action=/login autocomplete=on>
<input type=hidden name=request value="{_e(request_id)}">
<label for=email>E-mail</label><input id=email name=email type=email required autocomplete=username value="{_e(email)}" placeholder="jmeno@seznam.cz">
<label for=password>Heslo pro aplikace</label><input id=password name=password type=password required autocomplete=current-password>
<p class=muted>Heslo pro aplikace si vytvoříš na <a href="https://ucet.seznam.cz/" target=_blank rel=noopener>ucet.seznam.cz</a> → Zabezpečení účtu → Dvoufázové ověření → Heslo pro aplikace.</p>
<button type=submit>Přihlásit a povolit přístup</button>
</form>""",
            status,
        )

    async def load_request(request_id: str):
        found = await store.get_auth_request(request_id) if request_id else None
        if not found:
            return None
        client_id, params_json = found
        client = await provider.get_client(client_id)
        if client is None:
            return None
        params = AuthorizationParams.model_validate_json(params_json)
        return client, params

    def expired() -> HTMLResponse:
        return page(
            "Platnost vypršela",
            "<h1>Odkaz už neplatí</h1><p>Přihlašovací odkaz vypršel nebo už byl použit. "
            "Spusť připojení znovu z aplikace (Claude, ChatGPT…).</p>",
            400,
        )

    @server.custom_route("/login", methods=["GET"])
    async def login_get(request: Request) -> Response:
        request_id = request.query_params.get("request", "")
        loaded = await load_request(request_id)
        if not loaded:
            return expired()
        client, params = loaded
        return login_form(request_id, client.client_name or "Aplikace", urlparse(str(params.redirect_uri)).netloc)

    @server.custom_route("/login", methods=["POST"])
    async def login_post(request: Request) -> Response:
        form = await request.form()
        request_id = str(form.get("request", ""))
        email = normalize_email(str(form.get("email", "")))
        password = str(form.get("password", ""))
        loaded = await load_request(request_id)
        if not loaded:
            return expired()
        client, params = loaded
        name, host = client.client_name or "Aplikace", urlparse(str(params.redirect_uri)).netloc
        ip = client_ip(request)

        if await too_many_failures(email, ip):
            return login_form(request_id, name, host, email, "Příliš mnoho neúspěšných pokusů. Zkus to za 15 minut.", 429)

        user = await store.get_user_by_email(email) if email else None
        if user is None or not user.allowed:
            await store.record_login_attempt(email, ip, False)
            log.info("login refused (not allowlisted)")
            return login_form(request_id, name, host, email, "Tahle adresa nemá na tomto serveru povolený přístup. Požádej správce.", 403)

        account = MailAccount(email, password, settings.imap_host, settings.imap_port)
        try:
            await anyio.to_thread.run_sync(mail.verify_login, account)
        except MailError as e:
            await store.record_login_attempt(email, ip, False)
            return login_form(request_id, name, host, email, str(e), 401)

        await store.save_login(user.id, "app_password", box.encrypt(password, user.email))
        await store.record_login_attempt(email, ip, True)
        code = await store.create_auth_code(client.client_id, user.id, params.model_dump_json())
        await store.delete_auth_request(request_id)
        log.info("user %s authorized client %s", user.id, client.client_id)
        return RedirectResponse(
            construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state),
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    # ------------------------------------------------------------ account

    def account_form(email: str = "", message: str = "", ok: bool = False, status: int = 200) -> HTMLResponse:
        msg = f"<p class={'ok' if ok else 'err'} role=alert>{_e(message)}</p>" if message else ""
        return page(
            "Můj účet",
            f"""<h1>Odpojit e-mail</h1>
<p class=muted>Smaže uložené heslo pro aplikace a odhlásí všechny připojené aplikace. Pro úplné odříznutí
smaž heslo pro aplikace i na <a href="https://ucet.seznam.cz/" target=_blank rel=noopener>ucet.seznam.cz</a>.</p>
{msg}
<form method=post action=/account>
<label for=email>E-mail</label><input id=email name=email type=email required autocomplete=username value="{_e(email)}">
<label for=password>Heslo pro aplikace</label><input id=password name=password type=password required autocomplete=current-password>
<button type=submit>Odpojit a smazat údaje</button>
</form>""",
            status,
        )

    @server.custom_route("/account", methods=["GET"])
    async def account_get(request: Request) -> Response:
        return account_form()

    @server.custom_route("/account", methods=["POST"])
    async def account_post(request: Request) -> Response:
        form = await request.form()
        email = normalize_email(str(form.get("email", "")))
        password = str(form.get("password", ""))
        ip = client_ip(request)
        if await too_many_failures(email, ip):
            return account_form(email, "Příliš mnoho neúspěšných pokusů. Zkus to za 15 minut.", status=429)
        user = await store.get_user_by_email(email) if email else None
        valid = False
        if user and user.enc_secret:
            try:
                valid = constant_time_equal(box.decrypt(user.enc_secret, user.email), password)
            except (InvalidTag, ValueError):
                valid = False
        if not valid:
            await store.record_login_attempt(email, ip, False)
            return account_form(email, "Neznámá adresa nebo špatné heslo.", status=401)
        await store.forget_credentials(user.id)  # type: ignore[union-attr]
        await store.record_login_attempt(email, ip, True)
        return account_form(message="Hotovo. Uložené heslo je smazané a všechny aplikace jsou odpojené.", ok=True)

    @server.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    @server.custom_route("/", methods=["GET"])
    async def index(request: Request) -> Response:
        return page(
            "Seznam e-mail MCP",
            f"""<h1>Seznam e-mail pro AI asistenty</h1>
<p>MCP server, který propojí Claude, ChatGPT a další asistenty s e-mailem na Seznamu.</p>
<div class=box><p class=muted>Adresa pro připojení:</p><p><code>{_e(settings.public_url)}/mcp</code></p></div>
<p class=muted>Přístup mají jen povolené adresy. <a href="/account">Odpojit svůj e-mail</a></p>""",
        )
