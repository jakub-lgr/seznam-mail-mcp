import os
from pathlib import Path

import pytest
import sqlalchemy as sa

from seznam_mail_mcp.config import Settings, normalize_database_url
from seznam_mail_mcp.db import SCHEMA_DIR, DIALECTS, create_engine, init_db, schema_sql, users


def test_empty_url_falls_back_to_sqlite_file(tmp_path: Path):
    url = normalize_database_url("", tmp_path / "data")
    assert url == f"sqlite+aiosqlite:///{(tmp_path / 'data' / 'seznam-mail-mcp.db').resolve()}"
    assert (tmp_path / "data").is_dir()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgres://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
        ("postgresql://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
        ("sqlite:///:memory:", "sqlite+aiosqlite:///:memory:"),
    ],
)
def test_url_normalization(raw, expected, tmp_path):
    assert normalize_database_url(raw, tmp_path) == expected


def test_unknown_scheme_rejected(tmp_path):
    with pytest.raises(ValueError):
        normalize_database_url("mysql://u:p@h/db", tmp_path)


@pytest.mark.parametrize("dialect", list(DIALECTS))
def test_schema_files_up_to_date(dialect):
    assert (SCHEMA_DIR / f"{dialect}.sql").read_text() == schema_sql(dialect), (
        "run: python -m seznam_mail_mcp.db dump-schema --write"
    )


async def _roundtrip(settings: Settings):
    engine = create_engine(settings)
    try:
        await init_db(engine)
        await init_db(engine)  # idempotent
        async with engine.begin() as conn:
            await conn.execute(sa.delete(users).where(users.c.email == "test@example.invalid"))
            await conn.execute(sa.insert(users).values(email="test@example.invalid", enc_secret=b"\x00\x01"))
            row = (await conn.execute(sa.select(users).where(users.c.email == "test@example.invalid"))).one()
            assert row.enc_secret == b"\x00\x01"
            assert row.allowed is False
            assert row.created_at is not None
            await conn.execute(sa.delete(users).where(users.c.id == row.id))
    finally:
        await engine.dispose()


async def test_sqlite_roundtrip(tmp_path):
    await _roundtrip(Settings(database_url=normalize_database_url("", tmp_path / "data")))


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")
async def test_postgresql_roundtrip(tmp_path):
    await _roundtrip(Settings(database_url=normalize_database_url(os.environ["TEST_DATABASE_URL"], tmp_path)))


def test_refresh_token_default_is_180_days(tmp_path, monkeypatch):
    from seznam_mail_mcp.config import load_settings

    monkeypatch.delenv("REFRESH_TOKEN_TTL", raising=False)
    env = tmp_path / ".env"
    env.write_text("")
    assert load_settings(str(env)).refresh_token_ttl == 180 * 24 * 3600


async def test_purge_runs_periodically(monkeypatch):
    import asyncio

    from seznam_mail_mcp import app

    calls = []

    class FakeStore:
        async def purge_expired(self):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("db down")  # a failure must not stop the loop

    monkeypatch.setattr(app, "PURGE_INTERVAL", 0.01)
    task = asyncio.create_task(app._purge_daily(FakeStore()))
    await asyncio.sleep(0.1)
    task.cancel()
    assert len(calls) >= 2
