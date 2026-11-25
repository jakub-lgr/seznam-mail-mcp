"""Database layer: one schema definition, two backends (PostgreSQL or SQLite).

The tables below are the single source of truth. `schema/postgresql.sql` and
`schema/sqlite.sql` are generated from them (`python -m seznam_mail_mcp.db dump-schema --write`)
so anyone can create the database by hand; the server also creates missing tables on start.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.schema import CreateIndex, CreateTable

from .config import Settings, load_settings

# BIGINT identity on PostgreSQL, INTEGER PRIMARY KEY (rowid alias) on SQLite.
Id = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
Timestamp = sa.DateTime(timezone=True)

metadata = sa.MetaData()

users = sa.Table(
    "users",
    metadata,
    sa.Column("id", Id, primary_key=True, autoincrement=True),
    sa.Column("email", sa.String(320), nullable=False, unique=True),
    # NULL until the user logs in for the first time (an admin may allowlist the address first).
    sa.Column("enc_password", sa.LargeBinary, nullable=True),
    sa.Column("allowed", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("created_at", Timestamp, nullable=False, server_default=sa.func.now()),
    sa.Column("last_used_at", Timestamp, nullable=True),
    comment="Seznam mailboxes; allowed=true is the allowlist",
)

oauth_clients = sa.Table(
    "oauth_clients",
    metadata,
    sa.Column("client_id", sa.String(64), primary_key=True),
    sa.Column("client_name", sa.String(200), nullable=True),
    sa.Column("redirect_uris", sa.Text, nullable=False, comment="JSON array"),
    sa.Column("created_at", Timestamp, nullable=False, server_default=sa.func.now()),
    comment="OAuth clients registered via dynamic client registration",
)

auth_codes = sa.Table(
    "auth_codes",
    metadata,
    sa.Column("code_hash", sa.String(64), primary_key=True, comment="SHA-256 hex"),
    sa.Column("client_id", sa.String(64), sa.ForeignKey("oauth_clients.client_id", ondelete="CASCADE"), nullable=False),
    sa.Column("user_id", Id, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    sa.Column("redirect_uri", sa.Text, nullable=False),
    sa.Column("code_challenge", sa.String(128), nullable=False),
    sa.Column("scope", sa.Text, nullable=True),
    sa.Column("expires_at", Timestamp, nullable=False),
    comment="Short-lived OAuth authorization codes (PKCE)",
)

tokens = sa.Table(
    "tokens",
    metadata,
    sa.Column("token_hash", sa.String(64), primary_key=True, comment="SHA-256 hex"),
    sa.Column("kind", sa.String(16), nullable=False, comment="access | refresh"),
    sa.Column("user_id", Id, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    sa.Column("client_id", sa.String(64), sa.ForeignKey("oauth_clients.client_id", ondelete="CASCADE"), nullable=False),
    sa.Column("scope", sa.Text, nullable=True),
    sa.Column("created_at", Timestamp, nullable=False, server_default=sa.func.now()),
    sa.Column("expires_at", Timestamp, nullable=False),
    sa.Column("revoked", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Index("ix_tokens_user_id", "user_id"),
    comment="OAuth access and refresh tokens (hashes only)",
)

login_attempts = sa.Table(
    "login_attempts",
    metadata,
    sa.Column("id", Id, primary_key=True, autoincrement=True),
    sa.Column("email", sa.String(320), nullable=True),
    sa.Column("ip", sa.String(64), nullable=True),
    sa.Column("success", sa.Boolean, nullable=False),
    sa.Column("created_at", Timestamp, nullable=False, server_default=sa.func.now()),
    sa.Index("ix_login_attempts_email_created", "email", "created_at"),
    sa.Index("ix_login_attempts_ip_created", "ip", "created_at"),
    comment="Login attempts for rate limiting",
)

DIALECTS = {"postgresql": postgresql.dialect(), "sqlite": sqlite.dialect()}


def create_engine(settings: Settings) -> AsyncEngine:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    if settings.backend == "sqlite":

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

    return engine


async def init_db(engine: AsyncEngine) -> None:
    """Create missing tables (idempotent)."""
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)


def schema_sql(dialect_name: str) -> str:
    dialect = DIALECTS[dialect_name]
    parts = [
        f"-- seznam-mail-mcp database schema ({dialect_name})\n"
        "-- Generated from src/seznam_mail_mcp/db.py; do not edit by hand.\n"
        "-- Regenerate: python -m seznam_mail_mcp.db dump-schema --write\n"
    ]
    for table in metadata.sorted_tables:
        parts.append(str(CreateTable(table, if_not_exists=True).compile(dialect=dialect)).strip() + ";\n")
        for index in sorted(table.indexes, key=lambda i: i.name or ""):
            parts.append(str(CreateIndex(index, if_not_exists=True).compile(dialect=dialect)).strip() + ";\n")
    return "\n".join(parts)


SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schema"


def cli() -> None:
    parser = argparse.ArgumentParser(prog="seznam-mail-mcp-db", description="Database utilities")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="show which database backend is configured")
    sub.add_parser("init", help="create missing tables in the configured database")
    dump = sub.add_parser("dump-schema", help="print SQL schema for both backends")
    dump.add_argument("--write", action="store_true", help=f"write files into {SCHEMA_DIR}")
    args = parser.parse_args()

    if args.cmd == "dump-schema":
        for name in DIALECTS:
            sql = schema_sql(name)
            if args.write:
                SCHEMA_DIR.mkdir(exist_ok=True)
                (SCHEMA_DIR / f"{name}.sql").write_text(sql)
                print(f"wrote schema/{name}.sql")
            else:
                print(sql)
        return

    settings = load_settings()
    safe_url = sa.make_url(settings.database_url).render_as_string(hide_password=True)
    if args.cmd == "info":
        print(f"backend: {settings.backend}\nurl:     {safe_url}")
        return

    async def _init() -> None:
        engine = create_engine(settings)
        try:
            await init_db(engine)
        finally:
            await engine.dispose()

    asyncio.run(_init())
    print(f"tables ready ({settings.backend}: {safe_url})")


if __name__ == "__main__":
    cli()
