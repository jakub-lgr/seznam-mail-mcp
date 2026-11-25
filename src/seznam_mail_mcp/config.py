"""Configuration loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_SQLITE_FILE = "seznam-mail-mcp.db"

# OAuth callbacks of well-known MCP clients. Loopback URIs (http://localhost:<port>/...,
# http://127.0.0.1:<port>/...) used by desktop/CLI clients are allowed separately.
DEFAULT_REDIRECT_URIS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "https://chatgpt.com/connector_platform_oauth_redirect",
)


@dataclass(frozen=True)
class ToolOptions:
    """Which write tools are offered and their limits (same for stdio and HTTP mode)."""

    enable_organize: bool = True
    """mark_emails, move_emails, trash_emails"""
    enable_send: bool = True
    """send_email, send_draft"""
    send_daily_limit: int = 50
    """sent e-mails per user in any 24 hours (0 = no limit)"""
    save_sent_copy: bool = True
    """append a copy of each sent e-mail to the Sent folder"""


@dataclass(frozen=True)
class Settings:
    database_url: str
    """SQLAlchemy async URL (postgresql+asyncpg://... or sqlite+aiosqlite:///...)."""
    data_dir: Path = Path("data")
    public_url: str = "http://127.0.0.1:3856"
    """External base URL, e.g. https://mail.mcp.example.com (OAuth issuer)."""
    host: str = "127.0.0.1"
    port: int = 3856
    secret_key_file: Path = Path("data/secret.key")
    redirect_uris: tuple[str, ...] = DEFAULT_REDIRECT_URIS
    allow_loopback_redirects: bool = True
    access_token_ttl: int = 3600
    refresh_token_ttl: int = 180 * 24 * 3600  # sliding: renewed on every refresh
    imap_host: str = "imap.seznam.cz"
    imap_port: int = 993
    smtp_host: str = "smtp.seznam.cz"
    smtp_port: int = 465
    tools: ToolOptions = field(default_factory=ToolOptions)
    allowed_origins: tuple[str, ...] = ()
    """Extra Origin header values accepted on /mcp (browser-based clients)."""

    @property
    def backend(self) -> str:
        return "postgresql" if self.database_url.startswith("postgresql") else "sqlite"


def normalize_database_url(raw: str | None, data_dir: Path) -> str:
    """Turn the user-facing DATABASE_URL into an async SQLAlchemy URL.

    - empty / unset            -> SQLite file in DATA_DIR (created if missing)
    - postgres:// postgresql:// -> postgresql+asyncpg://
    - sqlite:///path            -> sqlite+aiosqlite:///path
    """
    raw = (raw or "").strip()
    if not raw:
        data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{(data_dir / DEFAULT_SQLITE_FILE).resolve()}"

    scheme, sep, rest = raw.partition("://")
    if not sep:
        raise ValueError("DATABASE_URL must look like postgresql://... or sqlite:///...")
    scheme = scheme.lower()

    if scheme in ("postgres", "postgresql", "postgresql+asyncpg"):
        return f"postgresql+asyncpg://{rest}"
    if scheme in ("sqlite", "sqlite+aiosqlite"):
        path = rest[1:] if rest.startswith("/") else rest  # sqlite:///rel or sqlite:////abs
        if path and path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{path}"
    raise ValueError(f"Unsupported database scheme '{scheme}' (use postgresql or sqlite)")


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _list(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None or value.strip() == "":
        return default
    return tuple(v.strip() for v in value.split(",") if v.strip())


def load_tool_options(env: os._Environ | dict = os.environ) -> ToolOptions:
    return ToolOptions(
        enable_organize=_bool(env.get("ENABLE_ORGANIZE"), True),
        enable_send=_bool(env.get("ENABLE_SEND"), True),
        send_daily_limit=int(env.get("SEND_DAILY_LIMIT") or 50),
        save_sent_copy=_bool(env.get("SAVE_SENT_COPY"), True),
    )


def load_settings(env_file: str | os.PathLike | None = None) -> Settings:
    load_dotenv(env_file or ".env", override=False)
    env = os.environ
    data_dir = Path(env.get("DATA_DIR", "data"))
    port = int(env.get("PORT", "3856"))
    return Settings(
        database_url=normalize_database_url(env.get("DATABASE_URL"), data_dir),
        data_dir=data_dir,
        public_url=env.get("PUBLIC_URL", f"http://127.0.0.1:{port}").rstrip("/"),
        host=env.get("HOST", "127.0.0.1"),
        port=port,
        secret_key_file=Path(env.get("SECRET_KEY_FILE", str(data_dir / "secret.key"))),
        redirect_uris=_list(env.get("ALLOWED_REDIRECT_URIS"), DEFAULT_REDIRECT_URIS),
        allow_loopback_redirects=_bool(env.get("ALLOW_LOOPBACK_REDIRECTS"), True),
        access_token_ttl=int(env.get("ACCESS_TOKEN_TTL", "3600")),
        refresh_token_ttl=int(env.get("REFRESH_TOKEN_TTL", str(180 * 24 * 3600))),
        imap_host=env.get("IMAP_HOST", "imap.seznam.cz"),
        imap_port=int(env.get("IMAP_PORT", "993")),
        smtp_host=env.get("SMTP_HOST", "smtp.seznam.cz"),
        smtp_port=int(env.get("SMTP_PORT", "465")),
        tools=load_tool_options(env),
        allowed_origins=_list(env.get("ALLOWED_ORIGINS"), ()),
    )
