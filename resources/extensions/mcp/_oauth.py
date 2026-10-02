"""OAuth sign-in of HTTP MCP connections.

The SDK's provider runs the authorization flow. Its tokens and the client it
registered live in host credentials under ``VBOT_MCP_<ID>_OAUTH_TOKENS`` and
``VBOT_MCP_<ID>_OAUTH_CLIENT``, never in the connection configuration. The
browser round trip is a pending input: the user opens the authorization URL and
answers with the complete redirected URL, which carries the code.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

from core.extensions.operations import ExtensionHost

from .interactions import InputRequests

DEFAULT_REDIRECT_URI = "http://localhost:8765/callback"
# Fields of the stored token and client documents that are credentials themselves.
_SECRET_FIELDS = frozenset({"access_token", "refresh_token", "id_token", "client_secret"})


def _credential_prefix(connection: str) -> str:
    return f"VBOT_MCP_{connection.upper()}_OAUTH"


class OAuthStorage:
    """The SDK's token storage, kept in the host credential store."""

    def __init__(self, host: ExtensionHost, identifier: str) -> None:
        self.host = host
        self.prefix = _credential_prefix(identifier)

    async def get_tokens(self) -> OAuthToken | None:
        raw = self.host.resolve_credential(f"{self.prefix}_TOKENS")
        return OAuthToken.model_validate_json(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.host.set_credential(f"{self.prefix}_TOKENS", tokens.model_dump_json(by_alias=True))

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self.host.resolve_credential(f"{self.prefix}_CLIENT")
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.host.set_credential(
            f"{self.prefix}_CLIENT", client_info.model_dump_json(by_alias=True)
        )


def oauth_secrets(host: ExtensionHost, connection: str) -> list[str]:
    """The stored OAuth documents of *connection* and the credentials inside them."""
    prefix = _credential_prefix(connection)
    secrets: list[str] = []
    for suffix in ("TOKENS", "CLIENT"):
        raw = host.resolve_credential(f"{prefix}_{suffix}")
        if not raw:
            continue
        secrets.append(raw)
        values = json.loads(raw)
        secrets.extend(
            str(value) for key, value in values.items() if key in _SECRET_FIELDS and value
        )
    return secrets


class ConnectionOAuth:
    """One connection's sign-in: the SDK provider and its browser round trip."""

    def __init__(self, config: dict[str, Any], host: ExtensionHost, inputs: InputRequests) -> None:
        self._config = config
        self._host = host
        self._inputs = inputs
        self._authorization_url: str | None = None

    def provider(self) -> OAuthClientProvider:
        return OAuthClientProvider(
            self._config["url"],
            OAuthClientMetadata(
                client_name="vBot",
                redirect_uris=[self._config.get("oauth_redirect_uri", DEFAULT_REDIRECT_URI)],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none",
            ),
            OAuthStorage(self._host, self._config["id"]),
            redirect_handler=self.redirect,
            callback_handler=self.callback,
        )

    async def redirect(self, url: str) -> None:
        self._authorization_url = url

    async def callback(self) -> AuthorizationCodeResult:
        """Ask the user to sign in and answer with the redirected URL."""
        response = await self._inputs.request(
            self._config["id"], "oauth", {"url": self._authorization_url}
        )
        query = parse_qs(urlsplit(response.get("redirect_url", "")).query)
        if "code" not in query:
            raise ValueError(
                "OAuth response requires the complete redirected URL containing the code"
            )
        return AuthorizationCodeResult(
            code=query["code"][0],
            state=query.get("state", [None])[0],
            iss=query.get("iss", [None])[0],
        )
