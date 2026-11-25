"""OAuth 2.1 authorization server for MCP clients (Claude, ChatGPT, ...).

The MCP SDK provides the HTTP endpoints (/register, /authorize, /token, /revoke,
metadata). This provider stores everything in the database; the user proves who
they are on our own login page (see web.py) by logging in to their Seznam mailbox.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .config import Settings
from .store import Store

log = logging.getLogger(__name__)

SCOPE = "mail"


def is_loopback_redirect(uri: str) -> bool:
    u = urlparse(uri)
    return u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1")


class SeznamAuthProvider:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings

    # ------------------------------------------------------------ clients

    def redirect_allowed(self, uri: str) -> bool:
        if uri in self.settings.redirect_uris:
            return True
        return self.settings.allow_loopback_redirects and is_loopback_redirect(uri)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        info = await self.store.get_client_info(client_id)
        return OAuthClientInformationFull.model_validate_json(info) if info else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        rejected = [u for u in uris if not self.redirect_allowed(u)]
        if not uris or rejected:
            log.warning("client registration rejected: redirect_uris=%s", rejected or uris)
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="Tento server přijímá jen známé MCP klienty. Nepovolené redirect_uri: "
                + ", ".join(rejected or ["(žádné)"]),
            )
        await self.store.save_client(client_info.client_id, client_info.client_name, client_info.model_dump_json())
        log.info("registered OAuth client %s (%s)", client_info.client_id, client_info.client_name)

    # ------------------------------------------------------------ authorization

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        request_id = await self.store.create_auth_request(client.client_id, params.model_dump_json())
        return f"{self.settings.public_url}/login?request={request_id}"

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        found = await self.store.get_auth_code(authorization_code)
        if not found:
            return None
        client_id, user_id, params_json, expires_at = found
        if client_id != client.client_id:
            return None
        params = AuthorizationParams.model_validate_json(params_json)
        return AuthorizationCode(
            code=authorization_code,
            scopes=params.scopes or [SCOPE],
            expires_at=expires_at.timestamp(),
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=str(user_id),
        )

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        if not await self.store.consume_auth_code(authorization_code.code):
            raise TokenError("invalid_grant", "authorization code already used")
        return await self._issue(int(authorization_code.subject or 0), client.client_id, authorization_code.scopes, authorization_code.resource)

    async def _issue(self, user_id: int, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        s = self.settings
        access, refresh = await self.store.issue_tokens(user_id, client_id, scopes, resource, s.access_token_ttl, s.refresh_token_ttl)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=s.access_token_ttl,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    # ------------------------------------------------------------ refresh / access tokens

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        row = await self.store.get_token(refresh_token, "refresh")
        if not row or row.client_id != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row.client_id,
            scopes=row.scopes,
            expires_at=int(row.expires_at.timestamp()),
            resource=row.resource,
            subject=str(row.user_id),
        )

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        await self.store.revoke_token(refresh_token.token)  # rotation
        return await self._issue(int(refresh_token.subject or 0), client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = await self.store.get_token(token, "access")
        if not row:
            return None
        return AccessToken(
            token=token,
            client_id=row.client_id,
            scopes=row.scopes,
            expires_at=int(row.expires_at.timestamp()),
            resource=row.resource,
            subject=str(row.user_id),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # revoke the whole grant (this user + this client)
        if token.subject:
            await self.store.revoke_user_client(int(token.subject), token.client_id)
        else:
            await self.store.revoke_token(token.token)

    async def exchange_identity_assertion(self, *args, **kwargs):  # SEP-990, not supported
        raise TokenError("unsupported_grant_type", "identity assertions are not supported")

