"""Configuration loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_SQLITE_FILE = "seznam-mail-mcp.db"


@dataclass(frozen=True)
class Settings:
    database_url: str
    """SQLAlchemy async URL (postgresql+asyncpg://... or sqlite+aiosqlite:///...)."""

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


def load_settings(env_file: str | os.PathLike | None = None) -> Settings:
    load_dotenv(env_file or ".env", override=False)
    data_dir = Path(os.environ.get("DATA_DIR", "data"))
    return Settings(database_url=normalize_database_url(os.environ.get("DATABASE_URL"), data_dir))
