"""OpenCode Zen multi-protocol provider adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, override

import httpx

from core.models.models import Model
from core.providers.adapter import (
    ModelLookup,
)
from core.providers.anthropic_compatible import (
    ANTHROPIC_OVERLOADED_STATUS,
    ANTHROPIC_VERSION,
    AnthropicCompatibleAdapter,
)
from core.providers.errors import (
    ProviderAuthError,
    ProviderError,
)
from core.providers.gemini_compatible import GeminiCompatibleAdapter
from core.providers.http import classify_http_status
from core.providers.openai import OpenAIAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.token_getter import TokenGetter
from core.providers.wire_profile import Protocol
from core.providers.wire_profiles import WireBinding

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder
    from core.providers.http import ProviderHTTPTransportScope

__all__ = ["OpenCodeZenAdapter"]

_FREE_TIER_ACCESS_MESSAGE = (
    "This OpenCode Zen free Model is only available inside OpenCode. "
    "Choose a supported Zen Model or an OpenCode Go Model. "
    "Another API key does not enable access to this Model from vBot."
)

# Zen returns allowance exhaustion with HTTP 429 and entitlement failures with
# HTTP 401; these markers separate them from throttling and bad credentials.
_PERMANENT_429_MARKERS = (
    "freeusagelimiterror",
    "gousagelimiterror",
    "blackusagelimiterror",
    "monthly limit",
    "weekly limit",
    "usage limit",
    "quota exceeded",
)

_NON_AUTH_401_MARKERS = (
    "creditserror",
    "monthlylimiterror",
    "userlimiterror",
    "modelerror",
)

_AUTH_401_MARKERS = ("autherror", "invalid api key", "missing api key")


def _classify_zen_status(
    status_code: int,
    *,
    detail: str,
    response_headers: httpx.Headers,
    extra_retryable: set[int] | None = None,
) -> None:
    normalized = detail.casefold()
    if status_code == 403 and "freetiererror" in normalized:
        error = ProviderError(_FREE_TIER_ACCESS_MESSAGE, retryable=False)
        error.status_code = status_code
        raise error
    if status_code == 401:
        if any(marker in normalized for marker in _NON_AUTH_401_MARKERS):
            raise ProviderError(
                f"OpenCode Zen account or Model access denied: {detail}", retryable=False
            )
        if any(marker in normalized for marker in _AUTH_401_MARKERS):
            raise ProviderAuthError(f"OpenCode Zen authentication failed: {detail}")
    if status_code == 403 and "regionerror" in normalized:
        raise ProviderError(f"OpenCode Zen region is not allowed: {detail}", retryable=False)
    if status_code == 429 and any(marker in normalized for marker in _PERMANENT_429_MARKERS):
        raise ProviderError(f"OpenCode Zen allowance exhausted: {detail}", retryable=False)
    classify_http_status(
        status_code,
        idempotent=False,
        extra_retryable=extra_retryable,
        detail=detail,
        response_headers=response_headers,
    )


class _OpenCodeZenMessagesAdapter(AnthropicCompatibleAdapter):
    """Zen's Anthropic Messages route with Zen error semantics."""

    @staticmethod
    @override
    def _build_error_detail(status_code: int, response_body: str = "") -> str:
        # Zen's error name/type determines entitlement vs authentication even
        # when the gateway omits the human-readable message.
        return f"{status_code} {response_body}".strip()

    @override
    def _classify_http_status(
        self,
        status_code: int,
        *,
        detail: str,
        response_headers: httpx.Headers,
    ) -> None:
        _classify_zen_status(
            status_code,
            detail=detail,
            response_headers=response_headers,
            extra_retryable=self._extra_retryable_statuses,
        )


class _OpenCodeZenGeminiAdapter(GeminiCompatibleAdapter):
    """Zen's Gemini route with Zen error semantics."""

    @override
    def _classify_http_status(
        self, status_code: int, *, detail: str, response_headers: httpx.Headers
    ) -> None:
        _classify_zen_status(status_code, detail=detail, response_headers=response_headers)


class OpenCodeZenAdapter(OpenAIAdapter):
    """Route OpenCode Zen Models across its four official wire protocols.

    The wire profile (``resources/wire/opencode-zen.json``) routes each Model
    to Chat Completions or Responses (the inherited codecs), Anthropic Messages
    (an inner Messages adapter sharing this Adapter's client and wire
    profiles) or Gemini ``generateContent``. New Models inherit Provider and
    family defaults; only concrete profile restrictions and retirements are
    refused before any request.
    """

    WIRE_PROTOCOLS: ClassVar[tuple[Protocol, ...]] = (
        "chat_completions",
        "messages",
        "responses",
        "gemini",
    )

    @classmethod
    @override
    def accepts_discovered_model(
        cls,
        raw: Mapping[str, Any],
        connection: ConnectionConfig | None,
    ) -> bool:
        """Keep every entry of Zen's Model listing."""

        del cls, raw, connection
        return True

    def __init__(
        self,
        config: ProviderConfig,
        token_getter: TokenGetter | str,
        base_url: str | None = None,
        auth_config: AuthConfig | None = None,
        model_lookup: ModelLookup | None = None,
        debug_recorder: ProviderDebugRecorder | None = None,
        *,
        connection_mode: str | None = None,
        http_transport: ProviderHTTPTransportScope | None = None,
    ) -> None:
        super().__init__(
            config,
            token_getter,
            base_url,
            auth_config,
            model_lookup,
            debug_recorder,
            connection_mode=connection_mode,
            http_transport=http_transport,
        )
        selected_auth = auth_config or config.connections[0].auth
        self._messages = _OpenCodeZenMessagesAdapter(
            config,
            self._token_getter,
            base_url=base_url,
            auth_config=AuthConfig(
                header="x-api-key",
                prefix="",
                credential_key=selected_auth.credential_key,
            ),
            model_lookup=model_lookup,
            debug_recorder=debug_recorder,
            client=self._client,
            api_version=ANTHROPIC_VERSION,
            extra_retryable_statuses=frozenset({ANTHROPIC_OVERLOADED_STATUS}),
        )
        self._messages.bind_wire_profiles(self.wire)
        self._gemini = _OpenCodeZenGeminiAdapter(
            config,
            self._token_getter,
            base_url=base_url,
            auth_config=AuthConfig(header="x-goog-api-key", prefix=""),
            model_lookup=model_lookup,
            debug_recorder=debug_recorder,
            client=self._client,
        )
        self._gemini.bind_wire_profiles(self.wire)

    @override
    def bind_wire_profiles(self, binding: WireBinding) -> None:
        super().bind_wire_profiles(binding)
        self._messages.bind_wire_profiles(binding)
        self._gemini.bind_wire_profiles(binding)

    @override
    async def aclose(self) -> None:
        await self._messages.aclose()
        await self._gemini.aclose()
        await super().aclose()

    @classmethod
    @override
    def normalize_catalog_entry(
        cls,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> Model:
        """Normalize one listed Model as a plain OpenAI-compatible entry.

        Discovery drops a Model the wire profile does not admit once the
        models.dev protocol hint is projected.
        """

        return OpenAICompatibleAdapter.normalize_catalog_entry(raw, defaults)

    @override
    def request_context_kwargs(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None = None,
        prompt_cache_affinity_id: str | None = None,
    ) -> dict[str, Any]:
        del agent_id, session_id, project_id, prompt_cache_affinity_id
        return {}

    @override
    def estimate_request_input_tokens(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model_id: str,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> int:
        """Estimate the same native input that the selected Zen wire sends."""
        profile = self.wire_profile(model_id)
        if profile.protocol == "messages":
            return self._messages.estimate_request_input_tokens(
                messages,
                model_id=model_id,
                tools=tools,
            )
        if profile.protocol == "gemini":
            return self._gemini.estimate_request_input_tokens(
                messages, model_id=model_id, tools=tools
            )
        return super().estimate_request_input_tokens(messages, model_id=model_id, tools=tools)

    @override
    async def send(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        protocol = self.wire_profile(model_id).protocol
        if protocol == "messages":
            return await self._messages.send(messages, model_id=model_id, **kwargs)
        if protocol == "gemini":
            return await self._gemini.send(messages, model_id=model_id, **kwargs)
        return await super().send(messages, model_id=model_id, **kwargs)

    @override
    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        protocol = self.wire_profile(model_id).protocol
        if protocol == "messages":
            return self._messages.stream(messages, model_id=model_id, **kwargs)
        if protocol == "gemini":
            return self._gemini.stream(messages, model_id=model_id, **kwargs)
        return super().stream(messages, model_id=model_id, **kwargs)

    @override
    def normalize_response(
        self,
        response: dict[str, Any],
        *,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        if model_id is not None:
            protocol = self.wire_profile(model_id).protocol
            if protocol == "messages":
                return self._messages.normalize_response(response, model_id=model_id)
            if protocol == "gemini":
                return self._gemini.normalize_response(response, model_id=model_id)
            return super().normalize_response(response, model_id=model_id)
        if "candidates" in response or "promptFeedback" in response:
            return self._gemini.normalize_response(response, model_id=model_id)
        if response.get("type") == "message":
            return self._messages.normalize_response(response)
        return super().normalize_response(response)

    @override
    def _classify_http_status(
        self,
        status_code: int,
        *,
        detail: str,
        response_headers: httpx.Headers,
    ) -> None:
        _classify_zen_status(
            status_code,
            detail=detail,
            response_headers=response_headers,
        )
