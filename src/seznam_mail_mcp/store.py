"""Persistence for users, OAuth clients, authorization requests/codes and tokens.

Only hashes of tokens/codes are stored. Expiry checks happen in Python (UTC) so the
same code works on PostgreSQL and SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from .crypto import new_token, token_hash
from .db import auth_codes, auth_requests, login_attempts, oauth_clients, tokens, users


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)  # SQLite returns naive UTC


def normalize_email(email: str) -> str:
    return email.strip().lower()


@dataclass(frozen=True)
class User:
    id: int
    email: str
    allowed: bool
    auth_method: str | None
    enc_secret: bytes | None
    created_at: datetime | None
    last_login_at: datetime | None
    last_used_at: datetime | None

    @classmethod
    def from_row(cls, row: Any) -> "User":
        return cls(
            id=row.id,
            email=row.email,
            allowed=bool(row.allowed),
            auth_method=row.auth_method,
            enc_secret=row.enc_secret,
            created_at=_aware(row.created_at),
            last_login_at=_aware(row.last_login_at),
            last_used_at=_aware(row.last_used_at),
        )


@dataclass(frozen=True)
class TokenRow:
    user_id: int
    client_id: str
    kind: str
    scopes: list[str]
    resource: str | None
    expires_at: datetime


class Store:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    # ------------------------------------------------------------ users

    async def get_user(self, user_id: int) -> User | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(sa.select(users).where(users.c.id == user_id))).first()
        return User.from_row(row) if row else None

    async def get_user_by_email(self, email: str) -> User | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(sa.select(users).where(users.c.email == normalize_email(email)))).first()
        return User.from_row(row) if row else None

    async def list_users(self) -> list[User]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(sa.select(users).order_by(users.c.email))).all()
        return [User.from_row(r) for r in rows]

    async def set_allowed(self, email: str, allowed: bool) -> User:
        email = normalize_email(email)
        async with self.engine.begin() as conn:
            updated = await conn.execute(sa.update(users).where(users.c.email == email).values(allowed=allowed))
            if updated.rowcount == 0:
                await conn.execute(sa.insert(users).values(email=email, allowed=allowed))
            if not allowed:
                await conn.execute(
                    sa.update(tokens)
                    .where(tokens.c.user_id == sa.select(users.c.id).where(users.c.email == email).scalar_subquery())
                    .values(revoked=True)
                )
        user = await self.get_user_by_email(email)
        assert user is not None
        return user

    async def save_login(self, user_id: int, auth_method: str, enc_secret: bytes) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                sa.update(users)
                .where(users.c.id == user_id)
                .values(auth_method=auth_method, enc_secret=enc_secret, last_login_at=utcnow())
            )

    async def forget_credentials(self, user_id: int) -> None:
        """Drop the stored mailbox secret and revoke every token (keeps the allowlist entry)."""
        async with self.engine.begin() as conn:
            await conn.execute(sa.update(users).where(users.c.id == user_id).values(enc_secret=None, auth_method=None))
            await conn.execute(sa.update(tokens).where(tokens.c.user_id == user_id).values(revoked=True))

    async def touch_user(self, user_id: int) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(sa.update(users).where(users.c.id == user_id).values(last_used_at=utcnow()))

    async def delete_user(self, email: str) -> bool:
        async with self.engine.begin() as conn:
            uid = (await conn.execute(sa.select(users.c.id).where(users.c.email == normalize_email(email)))).scalar()
            if uid is None:
                return False
            # explicit deletes so SQLite without FK enforcement behaves the same
            for table in (tokens, auth_codes):
                await conn.execute(sa.delete(table).where(table.c.user_id == uid))
            await conn.execute(sa.delete(users).where(users.c.id == uid))
        return True

    # ------------------------------------------------------------ OAuth clients

    async def save_client(self, client_id: str, client_name: str | None, client_info_json: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                sa.insert(oauth_clients).values(client_id=client_id, client_name=client_name, client_info=client_info_json)
            )

    async def get_client_info(self, client_id: str) -> str | None:
        async with self.engine.connect() as conn:
            return (
                await conn.execute(sa.select(oauth_clients.c.client_info).where(oauth_clients.c.client_id == client_id))
            ).scalar()

    # ------------------------------------------------------------ authorization requests (login form)

    async def create_auth_request(self, client_id: str, params_json: str, ttl: int = 900) -> str:
        request_id = new_token()
        async with self.engine.begin() as conn:
            await conn.execute(
                sa.insert(auth_requests).values(
                    request_hash=token_hash(request_id),
                    client_id=client_id,
                    params=params_json,
                    expires_at=utcnow() + timedelta(seconds=ttl),
                )
            )
        return request_id

    async def get_auth_request(self, request_id: str) -> tuple[str, str] | None:
        """Return (client_id, params_json) of a pending, unexpired request."""
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(sa.select(auth_requests).where(auth_requests.c.request_hash == token_hash(request_id)))
            ).first()
        if not row or _aware(row.expires_at) < utcnow():
            return None
        return row.client_id, row.params

    async def delete_auth_request(self, request_id: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(sa.delete(auth_requests).where(auth_requests.c.request_hash == token_hash(request_id)))

    # ------------------------------------------------------------ authorization codes

    async def create_auth_code(self, client_id: str, user_id: int, params_json: str, ttl: int = 300) -> str:
        code = new_token()
        async with self.engine.begin() as conn:
            await conn.execute(
                sa.insert(auth_codes).values(
                    code_hash=token_hash(code),
                    client_id=client_id,
                    user_id=user_id,
                    params=params_json,
                    expires_at=utcnow() + timedelta(seconds=ttl),
                )
            )
        return code

    async def get_auth_code(self, code: str) -> tuple[str, int, str, datetime] | None:
        """(client_id, user_id, params_json, expires_at) or None."""
        async with self.engine.connect() as conn:
            row = (await conn.execute(sa.select(auth_codes).where(auth_codes.c.code_hash == token_hash(code)))).first()
        if not row:
            return None
        return row.client_id, row.user_id, row.params, _aware(row.expires_at)

    async def consume_auth_code(self, code: str) -> bool:
        """Delete the code; False if it was already used (single use)."""
        async with self.engine.begin() as conn:
            res = await conn.execute(sa.delete(auth_codes).where(auth_codes.c.code_hash == token_hash(code)))
        return res.rowcount == 1

    # ------------------------------------------------------------ tokens

    async def issue_tokens(
        self, user_id: int, client_id: str, scopes: list[str], resource: str | None, access_ttl: int, refresh_ttl: int
    ) -> tuple[str, str]:
        access, refresh = new_token(), new_token()
        now = utcnow()
        common = {"user_id": user_id, "client_id": client_id, "scopes": " ".join(scopes), "resource": resource}
        async with self.engine.begin() as conn:
            await conn.execute(
                sa.insert(tokens),
                [
                    {**common, "token_hash": token_hash(access), "kind": "access", "expires_at": now + timedelta(seconds=access_ttl)},
                    {**common, "token_hash": token_hash(refresh), "kind": "refresh", "expires_at": now + timedelta(seconds=refresh_ttl)},
                ],
            )
        return access, refresh

    async def get_token(self, token: str, kind: str) -> TokenRow | None:
        """A valid (unrevoked, unexpired) token whose user is still allowed."""
        q = (
            sa.select(tokens, users.c.allowed)
            .join(users, users.c.id == tokens.c.user_id)
            .where(tokens.c.token_hash == token_hash(token), tokens.c.kind == kind)
        )
        async with self.engine.connect() as conn:
            row = (await conn.execute(q)).first()
        if not row or row.revoked or not row.allowed:
            return None
        expires_at = _aware(row.expires_at)
        if expires_at <= utcnow():
            return None
        return TokenRow(
            user_id=row.user_id,
            client_id=row.client_id,
            kind=row.kind,
            scopes=row.scopes.split() if row.scopes else [],
            resource=row.resource,
            expires_at=expires_at,
        )

    async def revoke_token(self, token: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(sa.update(tokens).where(tokens.c.token_hash == token_hash(token)).values(revoked=True))

    async def revoke_user_client(self, user_id: int, client_id: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                sa.update(tokens).where(tokens.c.user_id == user_id, tokens.c.client_id == client_id).values(revoked=True)
            )

    # ------------------------------------------------------------ login rate limiting

    async def record_login_attempt(self, email: str | None, ip: str | None, success: bool) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                sa.insert(login_attempts).values(
                    email=normalize_email(email) if email else None, ip=ip, success=success, created_at=utcnow()
                )
            )

    async def recent_failures(self, *, email: str | None = None, ip: str | None = None, window: int = 900) -> int:
        since = utcnow() - timedelta(seconds=window)
        q = sa.select(sa.func.count()).select_from(login_attempts).where(
            login_attempts.c.success == sa.false(), login_attempts.c.created_at >= since
        )
        if email is not None:
            q = q.where(login_attempts.c.email == normalize_email(email))
        if ip is not None:
            q = q.where(login_attempts.c.ip == ip)
        async with self.engine.connect() as conn:
            return int((await conn.execute(q)).scalar() or 0)

    # ------------------------------------------------------------ housekeeping

    async def purge_expired(self) -> None:
        now = utcnow()
        async with self.engine.begin() as conn:
            await conn.execute(sa.delete(auth_requests).where(auth_requests.c.expires_at < now))
            await conn.execute(sa.delete(auth_codes).where(auth_codes.c.expires_at < now))
            await conn.execute(sa.delete(tokens).where(tokens.c.expires_at < now - timedelta(days=1)))
            await conn.execute(sa.delete(login_attempts).where(login_attempts.c.created_at < now - timedelta(days=30)))
