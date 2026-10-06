"""OAuth sign-in of HTTP MCP connections.

The SDK's provider runs the authorization flow: discovery, PKCE, resource
indicators, issuer checks, registration and refresh. This module adds what it
leaves to the client.

Storage bound to the resource and its authorization server. The tokens and a
dynamically registered client live in host credentials
(``VBOT_MCP_<ID>_OAUTH_TOKENS`` and ``VBOT_MCP_<ID>_OAUTH_CLIENT``), never in
the connection configuration. Each document records the canonical resource URL
it was issued for and is ignored for any other URL, so a changed server URL
never receives the previous server's token. The token document also keeps the
authorization server's metadata and the absolute expiry: after a restart the
provider still refreshes an expiring token ahead of time, at that server's token
endpoint, instead of sending it until it fails and signing in again. A stored
registration is reused only for the redirect URI it was registered with, and
only while it works: a sign-in that does not complete, or a refresh refused for
any reason but a spent grant, deletes it with its tokens. Authorization servers
expire and revoke dynamic registrations, and the next sign-in then registers
again instead of presenting a client the server no longer knows.

Authorization servers that deviate from what the SDK expects. Every request of
the flow identifies vBot (``_FLOW_HEADERS``): some firewalls refuse requests
without a User-Agent. Issuers compare as strings except for one trailing slash
more on either side (``_issuers_match``), which several servers write in their
protected resource metadata and leave out of their own metadata; the metadata
is then read with the issuer as the resource lists it, and the issuer an
authorization response names (RFC 9207) is compared the same way. A
registration refused (HTTP 400) while it asked for ``offline_access`` is sent
once more without it; authorization still asks for it. A registration that
returns a client secret without ``token_endpoint_auth_method`` gets the RFC 7591
default ``client_secret_basic``, or ``client_secret_post`` when the server lists
only that, instead of being used as a public client that never sends its secret.
An empty scope list produces no blank scope.

Client registration, in order: a pre-registered client from the configuration
(``oauth_client_id``, an optional ``oauth_client_secret`` credential reference,
``oauth_scopes``), else dynamic registration. Client ID metadata documents are
not offered: vBot has no public HTTPS address to publish one at, and such a
document could not list each installation's loopback redirect URI. A
pre-registered client is bound to the authorization server of its first sign-in;
when the server later names another one, sign-in fails instead of presenting
the client to it.

The browser round trip is a pending input showing the authorization URL. When
the server receives OAuth redirects (``host.oauth_redirects``), the browser's
return completes it automatically; answering the input with the complete
redirected address remains the fallback.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, fields
from typing import Any, override
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import httpx2
from mcp.client.auth import OAuthClientProvider, OAuthFlowError
from mcp.client.auth.oauth2 import OAuthContext
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    union_scopes,
)
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)
from mcp.shared.auth_utils import resource_url_from_server_url
from pydantic import PrivateAttr, ValidationError

from core.extensions.oauth_redirects import DEFAULT_TTL_SECONDS
from core.extensions.operations import ExtensionHost

from .interactions import InputRequests

# The redirect URI without a server callback: nothing listens there, so the user
# pastes the address the browser shows.
PASTE_REDIRECT_URI = "http://localhost:8765/callback"
SIGN_IN_TIMEOUT_SECONDS = DEFAULT_TTL_SECONDS
# A token counts as expired this long before its expiry (at most half its
# lifetime), so it is refreshed before a request can fail with it.
REFRESH_MARGIN_SECONDS = 60.0
_GRANT_TYPES = ["authorization_code", "refresh_token"]
# Fields of the stored token and client documents that are credentials themselves.
_SECRET_FIELDS = frozenset({"access_token", "refresh_token", "id_token", "client_secret"})
_ERROR_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
# Headers of every request the sign-in flow sends (discovery, registration, token).
_FLOW_HEADERS = {"User-Agent": "vBot", "Accept": "application/json"}
_OFFLINE_ACCESS = "offline_access"


def _credential_keys(connection: str) -> tuple[str, str]:
    prefix = f"VBOT_MCP_{connection.upper()}_OAUTH"
    return f"{prefix}_TOKENS", f"{prefix}_CLIENT"


def redirect_uri(config: dict[str, Any], host: ExtensionHost) -> str:
    """The redirect URI a sign-in of *config* uses.

    The server's callback names its loopback address, or ``localhost`` with
    ``oauth_redirect_host: "localhost"``, for authorization servers that refuse
    loopback addresses.
    """
    configured = config.get("oauth_redirect_uri")
    if configured:
        return str(configured)
    redirects = host.oauth_redirects
    callback = redirects.callback_url if redirects is not None else None
    if callback is None:
        return PASTE_REDIRECT_URI
    if config.get("oauth_redirect_host") == "localhost":
        parts = urlsplit(callback)
        return urlunsplit(parts._replace(netloc=f"localhost:{parts.port}"))
    return callback


def forget_sign_in(host: ExtensionHost, connection: str) -> bool:
    """Delete the stored tokens and registered client of *connection*; whether any existed."""
    removed = False
    for key in _credential_keys(connection):
        if host.resolve_credential(key):
            host.set_credential(key, "")
            removed = True
    return removed


def sign_in_status(host: ExtensionHost, config: dict[str, Any]) -> dict[str, Any]:
    """The redirect URI of *config*'s sign-in and whether it holds tokens for its URL."""
    redirect = redirect_uri(config, host)
    return {
        "redirect_uri": redirect,
        "signed_in": OAuthStorage(host, config, redirect).signed_in(),
    }


def oauth_secrets(host: ExtensionHost, connection: str) -> list[str]:
    """The stored OAuth documents of *connection* and the credentials inside them."""
    secrets: list[str] = []
    for key in _credential_keys(connection):
        raw = host.resolve_credential(key)
        if not raw:
            continue
        secrets.append(raw)
        try:
            values = json.loads(raw)
        except ValueError:
            continue
        if isinstance(values, dict):
            secrets.extend(
                str(value) for key, value in values.items() if key in _SECRET_FIELDS and value
            )
    return secrets


def _issuers_match(first: str, second: str) -> bool:
    """Whether two issuer identifiers name the same authorization server.

    RFC 8414 compares them as strings; one trailing slash more on either side is
    accepted too, and nothing else.
    """
    return first == second or first == f"{second}/" or second == f"{first}/"


def _json_object(response: httpx2.Response) -> dict[str, Any] | None:
    try:
        body = json.loads(response.content)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _json_response(response: httpx2.Response, body: dict[str, Any]) -> httpx2.Response:
    """*response* with *body* in place of its own."""
    return httpx2.Response(
        response.status_code,
        headers={"Content-Type": "application/json"},
        content=json.dumps(body).encode(),
        request=response.request,
    )


def _registration_without_offline_access(
    request: httpx2.Request, response: httpx2.Response
) -> httpx2.Request | None:
    """*request* again without ``offline_access``, when the server refused it with that scope."""
    if response.status_code != 400:
        return None
    try:
        body = json.loads(request.content)
    except ValueError:
        return None
    scope = body.get("scope") if isinstance(body, dict) else None
    if not isinstance(scope, str) or _OFFLINE_ACCESS not in scope.split():
        return None
    remaining = " ".join(item for item in scope.split() if item != _OFFLINE_ACCESS)
    retry = {key: value for key, value in body.items() if key != "scope"}
    if remaining:
        retry["scope"] = remaining
    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in {"content-length", "host"}
    }
    return httpx2.Request(request.method, request.url, json=retry, headers=headers)


def _refresh_due(expires_at: float, lifetime: float) -> float:
    return expires_at - min(REFRESH_MARGIN_SECONDS, lifetime / 2)


@dataclass
class _Context(OAuthContext):
    """The SDK context, counting a token as expired shortly before it expires."""

    @override
    def update_token_expiry(self, token: OAuthToken) -> None:
        if token.expires_in is None:
            self.token_expiry_time = None
        else:
            self.token_expiry_time = _refresh_due(time.time() + token.expires_in, token.expires_in)


class _ClientMetadata(OAuthClientMetadata):
    """Client metadata whose requested scope always includes the configured scopes.

    The SDK replaces ``scope`` with the scopes the server challenges for before
    each authorization; the configured ones are added to whatever it sets. From
    an empty scope list the SDK builds a blank scope or one that starts with a
    space; a blank scope becomes none, and the space is dropped.
    """

    _configured_scope: str | None = PrivateAttr(default=None)

    @override
    def __setattr__(self, name: str, value: Any) -> None:
        if name == "scope":
            value = union_scopes(value, self._configured_scope)
            value = (" ".join(value.split()) or None) if value is not None else None
        super().__setattr__(name, value)


class OAuthStorage:
    """The SDK's token storage: host credentials bound to the resource and its server."""

    def __init__(
        self,
        host: ExtensionHost,
        config: dict[str, Any],
        redirect: str,
        client_secret: str | None = None,
    ) -> None:
        self._host = host
        self._config = config
        self._tokens_key, self._client_key = _credential_keys(config["id"])
        self._resource = resource_url_from_server_url(config["url"])
        self._redirect = redirect
        self._client_secret = client_secret
        self.context: OAuthContext | None = None

    def _document(self, key: str) -> dict[str, Any] | None:
        raw = self._host.resolve_credential(key)
        if not raw:
            return None
        try:
            document = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(document, dict) or document.get("resource") != self._resource:
            return None
        return document

    def _tokens(self) -> dict[str, Any] | None:
        document = self._document(self._tokens_key)
        if document is None or self._config.get("oauth_client_id"):
            return document
        client = self._document(self._client_key)
        issuer = self.bound_issuer(document)
        registered = client.get("issuer") if client is not None else None
        if issuer and registered and not _issuers_match(issuer, registered):
            return None
        return document

    def signed_in(self) -> bool:
        """Whether tokens for this connection's URL are stored."""
        return self._tokens() is not None

    def bound_issuer(self, document: dict[str, Any] | None = None) -> str | None:
        """The issuer of the authorization server the stored tokens came from."""
        if document is None:
            document = self._document(self._tokens_key)
        server = (document or {}).get("authorization_server")
        issuer = server.get("issuer") if isinstance(server, dict) else None
        return issuer if isinstance(issuer, str) else None

    def restore(self, context: OAuthContext) -> None:
        """Give *context* the expiry and server metadata stored with its tokens."""
        document = self._tokens()
        if document is None or context.current_tokens is None:
            return
        expires_at = document.get("expires_at")
        if isinstance(expires_at, int | float):
            lifetime = context.current_tokens.expires_in or REFRESH_MARGIN_SECONDS * 2
            context.token_expiry_time = _refresh_due(float(expires_at), lifetime)
        try:
            if context.oauth_metadata is None and document.get("authorization_server"):
                context.oauth_metadata = OAuthMetadata.model_validate(
                    document["authorization_server"]
                )
            if context.protected_resource_metadata is None and document.get("protected_resource"):
                metadata = ProtectedResourceMetadata.model_validate(document["protected_resource"])
                context.protected_resource_metadata = metadata
                if metadata.authorization_servers:
                    context.auth_server_url = str(metadata.authorization_servers[0])
        except ValidationError:
            context.oauth_metadata = None
            context.protected_resource_metadata = None

    def forget_tokens(self) -> None:
        self._host.set_credential(self._tokens_key, "")

    def forget_client(self) -> None:
        """Delete the dynamically registered client and the tokens it obtained."""
        self._host.set_credential(self._client_key, "")
        self.forget_tokens()

    async def get_tokens(self) -> OAuthToken | None:
        document = self._tokens()
        if document is None:
            return None
        try:
            return OAuthToken.model_validate(document)
        except ValidationError:
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        context = self.context
        previous = self._document(self._tokens_key) or {}
        document: dict[str, Any] = {
            **tokens.model_dump(mode="json", exclude_none=True),
            "resource": self._resource,
        }
        if tokens.expires_in is not None:
            document["expires_at"] = time.time() + tokens.expires_in
        server = context.oauth_metadata if context is not None else None
        resource = context.protected_resource_metadata if context is not None else None
        if server is not None:
            document["authorization_server"] = server.model_dump(mode="json", exclude_none=True)
        elif previous.get("authorization_server"):
            document["authorization_server"] = previous["authorization_server"]
        if resource is not None:
            document["protected_resource"] = resource.model_dump(mode="json", exclude_none=True)
        elif previous.get("protected_resource"):
            document["protected_resource"] = previous["protected_resource"]
        self._host.set_credential(self._tokens_key, json.dumps(document, separators=(",", ":")))

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        client_id = self._config.get("oauth_client_id")
        if client_id:
            return OAuthClientInformationFull(
                client_id=client_id,
                client_secret=self._client_secret,
                # RFC 6749 section 2.3.1: every server supports Basic for a client secret.
                token_endpoint_auth_method="client_secret_basic" if self._client_secret else "none",
                redirect_uris=[self._redirect],  # type: ignore[list-item]
                grant_types=list(_GRANT_TYPES),
                response_types=["code"],
            )
        document = self._document(self._client_key)
        if document is None:
            return None
        try:
            client = OAuthClientInformationFull.model_validate(document)
        except ValidationError:
            return None
        if self._redirect not in {str(uri) for uri in client.redirect_uris or []}:
            return None
        return client

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        if self._config.get("oauth_client_id"):
            raise OAuthFlowError(
                "The configured OAuth client cannot be replaced by a new registration"
            )
        document = {
            **client_info.model_dump(mode="json", exclude_none=True),
            "resource": self._resource,
        }
        self._host.set_credential(self._client_key, json.dumps(document, separators=(",", ":")))
        # Tokens belong to the client that obtained them.
        self.forget_tokens()


class _Provider(OAuthClientProvider):
    """The SDK provider with vBot's storage binding and pre-registered client checks."""

    def __init__(
        self,
        server_url: str,
        client_metadata: OAuthClientMetadata,
        *,
        storage: OAuthStorage,
        preregistered: bool,
        redirect_handler: Callable[[str], Awaitable[None]],
        callback_handler: Callable[[], Awaitable[AuthorizationCodeResult]],
    ) -> None:
        super().__init__(
            server_url,
            client_metadata,
            storage,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )
        self.context = _Context(
            **{field.name: getattr(self.context, field.name) for field in fields(self.context)}
        )
        self._storage = storage
        self._preregistered = preregistered
        storage.context = self.context

    @override
    async def _initialize(self) -> None:
        await super()._initialize()
        self._storage.restore(self.context)

    @override
    async def _auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        """The SDK flow, tolerating the authorization server deviations the module names."""
        flow = super()._auth_flow(request)
        try:
            outgoing = await anext(flow)
            while True:
                if outgoing is request:
                    response = yield outgoing
                else:
                    for name, value in _FLOW_HEADERS.items():
                        outgoing.headers.setdefault(name, value)
                    response = yield outgoing
                    if self._registers(outgoing):
                        retry = _registration_without_offline_access(outgoing, response)
                        if retry is not None:
                            response = yield retry
                        response = self._with_secret_auth_method(response)
                    elif self._discovers_server(outgoing):
                        response = self._with_listed_issuer(response)
                outgoing = await flow.asend(response)
        except StopAsyncIteration:
            return
        finally:
            await flow.aclose()

    def _registers(self, request: httpx2.Request) -> bool:
        """Whether *request* is the flow's dynamic client registration."""
        if request.method != "POST" or self.context.client_info is not None:
            return False
        metadata = self.context.oauth_metadata
        if metadata is not None and metadata.registration_endpoint is not None:
            endpoint = str(metadata.registration_endpoint)
        else:
            base = self.context.get_authorization_base_url(self.context.server_url)
            endpoint = urljoin(base, "/register")
        return request.url == httpx2.URL(endpoint)

    def _discovers_server(self, request: httpx2.Request) -> bool:
        """Whether *request* asks for the listed authorization server's metadata."""
        listed = self.context.auth_server_url
        if request.method != "GET" or listed is None:
            return False
        urls = build_oauth_authorization_server_metadata_discovery_urls(
            listed, self.context.server_url
        )
        return any(request.url == httpx2.URL(url) for url in urls)

    def _with_listed_issuer(self, response: httpx2.Response) -> httpx2.Response:
        """The metadata *response* naming its issuer as the resource lists it, if they match."""
        listed = self.context.auth_server_url
        body = _json_object(response)
        if response.status_code != 200 or listed is None or body is None:
            return response
        issuer = body.get("issuer")
        if not isinstance(issuer, str) or issuer == listed or not _issuers_match(issuer, listed):
            return response
        return _json_response(response, {**body, "issuer": listed})

    def _with_secret_auth_method(self, response: httpx2.Response) -> httpx2.Response:
        """The registration *response* naming how its client secret is sent, if it does not."""
        body = _json_object(response)
        if (
            response.status_code not in {200, 201}
            or body is None
            or not body.get("client_secret")
            or body.get("token_endpoint_auth_method") is not None
        ):
            return response
        metadata = self.context.oauth_metadata
        supported = metadata.token_endpoint_auth_methods_supported if metadata else None
        # RFC 7591 section 2 and RFC 8414 section 2: client_secret_basic is the default.
        method = "client_secret_basic"
        if supported and method not in supported and "client_secret_post" in supported:
            method = "client_secret_post"
        return _json_response(response, {**body, "token_endpoint_auth_method": method})

    @override
    async def _handle_refresh_response(self, response: httpx2.Response) -> bool:
        refreshed = await super()._handle_refresh_response(response)
        if not refreshed:
            # The refresh token is spent or revoked; never offer it again.
            self._storage.forget_tokens()
            # Any other refusal may mean the server no longer knows the client.
            if await _error_code(response) != "invalid_grant":
                self._forget_registration()
        return refreshed

    @override
    async def _perform_authorization(self) -> httpx2.Request:
        try:
            return await super()._perform_authorization()
        except Exception:
            # Cancelled, refused or timed out: the authorization page may have
            # rejected the client.
            self._forget_registration()
            raise

    @override
    async def _handle_token_response(self, response: httpx2.Response) -> None:
        try:
            await super()._handle_token_response(response)
        except Exception:
            self._forget_registration()
            raise

    def _forget_registration(self) -> None:
        """Register again at the next sign-in instead of reusing the stored registration."""
        if self._preregistered:
            return
        self._storage.forget_client()
        self.context.client_info = None

    @override
    def _expected_issuer(self) -> str:
        issuer = super()._expected_issuer()
        bound = self._storage.bound_issuer()
        if self._preregistered and bound is not None and not _issuers_match(bound, issuer):
            raise OAuthFlowError(
                f"The MCP server now uses the authorization server {issuer} instead of "
                f"{bound}. The configured OAuth client ID belongs to {bound}: register vBot "
                "with the new server and update the client ID, or sign in again to use "
                "the configured client with it"
            )
        return issuer


class ConnectionOAuth:
    """One connection's sign-in: the SDK provider and its browser round trip."""

    def __init__(self, config: dict[str, Any], host: ExtensionHost, inputs: InputRequests) -> None:
        self._config = config
        self._host = host
        self._inputs = inputs
        self._authorization_url: str | None = None
        self._state: str | None = None
        self._provider: _Provider | None = None

    def provider(self) -> OAuthClientProvider:
        secret_key = self._config.get("oauth_client_secret")
        secret = self._host.resolve_credential(secret_key) if secret_key else None
        if secret_key and not secret:
            raise ValueError(f"Missing MCP credential: {secret_key}")
        redirect = redirect_uri(self._config, self._host)
        configured_scope = " ".join(self._config.get("oauth_scopes", [])) or None
        metadata = _ClientMetadata(
            client_name="vBot",
            redirect_uris=[redirect],  # type: ignore[list-item]
            grant_types=list(_GRANT_TYPES),
            response_types=["code"],
            token_endpoint_auth_method="none",
            scope=configured_scope,
        )
        metadata._configured_scope = configured_scope
        # The URL as the SDK sends it, which is how a registration records it.
        sent_redirect = str(metadata.redirect_uris[0]) if metadata.redirect_uris else redirect
        storage = OAuthStorage(self._host, self._config, sent_redirect, secret)
        self._provider = _Provider(
            self._config["url"],
            metadata,
            storage=storage,
            preregistered=bool(self._config.get("oauth_client_id")),
            redirect_handler=self.redirect,
            callback_handler=self.callback,
        )
        return self._provider

    async def redirect(self, url: str) -> None:
        self._authorization_url = url
        self._state = parse_qs(urlsplit(url).query).get("state", [None])[0]

    async def callback(self) -> AuthorizationCodeResult:
        """Wait for the browser to return, or for the user to paste where it went."""
        return self._result(await self._response())

    async def _response(self) -> dict[str, str]:
        redirects = self._host.oauth_redirects
        browser = redirects.expect(self._state) if redirects is not None and self._state else None
        # Started eagerly: the pending input exists once the sign-in waits.
        pasted = asyncio.create_task(
            self._inputs.request(
                self._config["id"],
                "oauth",
                {"url": self._authorization_url},
                expires_in=SIGN_IN_TIMEOUT_SECONDS,
            ),
            eager_start=True,
        )
        waiting: set[asyncio.Future[Any]] = {pasted} if browser is None else {pasted, browser}
        try:
            done, _ = await asyncio.wait(
                waiting, timeout=SIGN_IN_TIMEOUT_SECONDS, return_when=asyncio.FIRST_COMPLETED
            )
            if browser is not None and browser in done and not browser.cancelled():
                return browser.result()
            if pasted in done:
                return _redirect_query(pasted.result().get("redirect_url", ""))
            raise OAuthFlowError(
                "MCP sign-in did not finish in time; connect again to start a new sign-in"
            )
        finally:
            pasted.cancel()
            if browser is not None and redirects is not None and self._state:
                redirects.discard(self._state)
            await asyncio.gather(pasted, return_exceptions=True)

    def _result(self, params: dict[str, str]) -> AuthorizationCodeResult:
        server = self._provider.context.oauth_metadata if self._provider else None
        iss = params.get("iss")
        # RFC 9207: the SDK compares the issuer as a string; one that matches
        # the server's is handed on in the server's form.
        if iss is not None and server is not None and _issuers_match(iss, str(server.issuer)):
            iss = str(server.issuer)
        if "error" in params:
            # RFC 9207: an error from another issuer is not acted on or shown.
            if iss is not None and (server is None or iss != str(server.issuer)):
                raise OAuthFlowError("MCP sign-in failed: the response came from another server")
            code = params["error"]
            detail = f" ({code})" if _ERROR_CODE.fullmatch(code) else ""
            raise OAuthFlowError(f"The authorization server refused the MCP sign-in{detail}")
        if not params.get("code"):
            raise ValueError(
                "OAuth response requires the complete redirected URL containing the code"
            )
        return AuthorizationCodeResult(code=params["code"], state=params.get("state"), iss=iss)


async def _error_code(response: httpx2.Response) -> str | None:
    """The OAuth ``error`` code of a refused token request, if its body names one."""
    try:
        body = json.loads(await response.aread())
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, str) else None


def _redirect_query(url: str) -> dict[str, str]:
    """The parameters of a pasted redirect address, the first of each."""
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
