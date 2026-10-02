"""OpenCode Zen Adapter: catalog review, wire routing, reasoning and the Zen error policy."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from core.models.models import Model, ModelRegistry
from core.providers import OpenCodeZenAdapter
from core.providers.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
)
from core.providers.opencode_zen import _FREE_TIER_ACCESS_MESSAGE
from core.providers.wire_observations import WireObservations
from core.providers.wire_profiles import WireProfiles, bundled_wire_profile_files
from core.utils.retry import caller_owns_retries

from .opencode_zen_test_support import (
    CHAT_MODEL,
    CHAT_URL,
    GEMINI_MODEL,
    MESSAGES_MODEL,
    MESSAGES_URL,
    RESPONSES_MODEL,
    RESPONSES_URL,
    zen_adapter,
    zen_config,
    zen_model,
)

HELLO = [{"role": "user", "content": "hello"}]


def _bundled_lookup() -> Callable[[str], Model | None]:
    """Look up Zen Models in the bundled catalog resources."""
    registry = ModelRegistry.load(Path(__file__).resolve().parents[3] / "resources")
    return lambda selected: registry.get("opencode-zen", selected)


async def _request(adapter: OpenCodeZenAdapter, model_id: str, *, streaming: bool) -> None:
    if streaming:
        _ = [delta async for delta in adapter.stream(HELLO, model_id=model_id)]
    else:
        await adapter.send(HELLO, model_id=model_id)


# ---------------------------------------------------------------------------
# Catalog review
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_id", "protocol"),
    [
        ("gpt-6-sol", "responses"),
        ("gpt-6.1-sol", "responses"),
        ("claude-opus-5-5", "messages"),
        ("claude-sonnet-5-5", "messages"),
        ("glm-5.3-flash", "chat_completions"),
        ("qwen3.8-max", "chat_completions"),
        ("gemini-3.8-flash", "gemini"),
    ],
)
def test_bundled_catalog_models_are_admitted_on_their_reviewed_wire(
    model_id: str, protocol: str
) -> None:
    """The bundled protocol hints and the Chat rule route every listed Model."""
    adapter = OpenCodeZenAdapter(zen_config(), "zen-secret", model_lookup=_bundled_lookup())

    profile = adapter.wire_profile(model_id)

    assert (profile.protocol, profile.admission.state) == (protocol, "available")


@pytest.mark.parametrize(
    ("model_id", "stale_entry_of", "streaming"),
    [
        pytest.param("big-pickle", CHAT_MODEL, False, id="free-tier-send"),
        pytest.param(
            "longcat-2.5-preview-free", RESPONSES_MODEL, True, id="hinted-free-tier-stream"
        ),
        pytest.param("fledge-alpha-free", CHAT_MODEL, False, id="fledge-free-tier-send"),
        pytest.param("claude-opus-4-1", MESSAGES_MODEL, True, id="hinted-retired-stream"),
        pytest.param("future-model", CHAT_MODEL, True, id="no-protocol-info-stream"),
        pytest.param("openai/gpt-5.6-sol", None, False, id="unknown-alias-send"),
    ],
)
@pytest.mark.asyncio
async def test_unusable_selection_fails_before_network(
    model_id: str, stale_entry_of: str | None, streaming: bool
) -> None:
    """A stale catalog entry or an unknown alias never guesses a wire protocol.

    A protocol hint routes a free or retired Model but never admits it, and a
    catalog Model with neither a hint nor a reviewed rule is refused.
    """
    if stale_entry_of is not None:
        stale_model = replace(zen_model(stale_entry_of), model_id=model_id)
        adapter = OpenCodeZenAdapter(zen_config(), "key", model_lookup=lambda _: stale_model)
    else:
        adapter = zen_adapter()

    with respx.mock:
        route = respx.route(method="POST")
        with pytest.raises(ProviderError) as caught:
            await _request(adapter, model_id, streaming=streaming)
    await adapter.aclose()

    assert type(caught.value) is ProviderError
    assert caught.value.retryable is False
    assert not route.called


# ---------------------------------------------------------------------------
# Wire routing and reasoning
# ---------------------------------------------------------------------------

_RESPONSES_BODY = {
    "id": "resp_1",
    "status": "completed",
    "output": [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "done"}],
        }
    ],
}
_MESSAGES_BODY = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "done"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 2, "output_tokens": 1},
}
_CHAT_BODY = {
    "choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}]
}


@pytest.mark.parametrize(
    ("model_id", "url", "body", "auth_header", "auth_value", "absent_header"),
    [
        pytest.param(
            RESPONSES_MODEL,
            RESPONSES_URL,
            _RESPONSES_BODY,
            "authorization",
            "Bearer zen-secret",
            "x-api-key",
            id="responses",
        ),
        pytest.param(
            MESSAGES_MODEL,
            MESSAGES_URL,
            _MESSAGES_BODY,
            "x-api-key",
            "zen-secret",
            "authorization",
            id="messages",
        ),
        pytest.param(
            CHAT_MODEL,
            CHAT_URL,
            _CHAT_BODY,
            "authorization",
            "Bearer zen-secret",
            "x-api-key",
            id="chat",
        ),
    ],
)
@pytest.mark.asyncio
async def test_each_model_uses_its_reviewed_wire_and_auth_header(
    model_id: str,
    url: str,
    body: dict[str, Any],
    auth_header: str,
    auth_value: str,
    absent_header: str,
) -> None:
    adapter = zen_adapter()
    messages = [{"role": "system", "content": "Be exact"}, *HELLO]

    with respx.mock:
        route = respx.post(url).mock(return_value=httpx.Response(200, json=body))
        response = await adapter.send(messages, model_id=model_id)

    request = route.calls.last.request
    payload = json.loads(request.content)
    assert payload["model"] == model_id
    assert request.headers[auth_header] == auth_value
    assert absent_header not in request.headers
    assert ("cache_control" in json.dumps(payload)) is (model_id == MESSAGES_MODEL)
    assert adapter.normalize_response(response, model_id=model_id)["content"] == "done"


@pytest.mark.asyncio
async def test_messages_wire_reads_the_connection_binding_of_the_adapter() -> None:
    """A fact learned on the Adapter's Connection also shapes its Messages wire."""
    adapter = zen_adapter()
    observations = WireObservations(None, save_delay=None)
    profiles = WireProfiles(
        files=bundled_wire_profile_files(),
        protocol_support=lambda _provider_id: OpenCodeZenAdapter.WIRE_PROTOCOLS,
        model_resolver=lambda _provider_id, model_id: zen_model(model_id),
        report=lambda issue: None,
        observations=observations,
    )
    adapter.bind_wire_profiles(profiles.bind("opencode-zen", "api-key"))
    observations.record_rejected_parameter("opencode-zen", "api-key", MESSAGES_MODEL, "top_p")

    with respx.mock:
        route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=_MESSAGES_BODY))
        await adapter.send(HELLO, model_id=MESSAGES_MODEL, top_p=0.5)

    assert "top_p" not in json.loads(route.calls.last.request.content)


@pytest.mark.parametrize(
    ("model_id", "selected_effort", "wire_effort"),
    [
        ("gpt-6-sol", "none", "none"),
        ("gpt-6.1-sol", "none", "low"),
        ("gpt-6.1-sol", "minimal", "low"),
        ("gpt-6.1-sol", "max", "max"),
    ],
)
@pytest.mark.asyncio
async def test_bundled_gpt6_model_uses_the_responses_wire_with_its_effort(
    model_id: str, selected_effort: str, wire_effort: str
) -> None:
    adapter = OpenCodeZenAdapter(zen_config(), "zen-secret", model_lookup=_bundled_lookup())
    response_format = {"type": "json_schema", "name": "answer", "schema": {"type": "object"}}

    with respx.mock:
        route = respx.post(RESPONSES_URL).mock(
            return_value=httpx.Response(200, json=_RESPONSES_BODY)
        )
        response = await adapter.send(
            HELLO,
            model_id=model_id,
            thinking_effort=selected_effort,
            response_format=response_format,
        )
    await adapter.aclose()

    payload = json.loads(route.calls.last.request.content)
    assert payload["model"] == model_id
    assert payload["reasoning"]["effort"] == wire_effort
    if model_id == "gpt-6.1-sol":
        assert payload["text"]["format"] == response_format
        assert adapter.reasoning_replay_policy(model_id) == "none"
    assert adapter.normalize_response(response, model_id=model_id)["content"] == "done"
    intent = adapter.describe_reasoning_render(model_id, selected_effort)
    assert intent.effort_level == wire_effort


@pytest.mark.parametrize(
    ("model_id", "selected_effort", "wire_effort", "thinking", "intent"),
    [
        ("space-bunny-free", "none", "low", None, ("effort", "low")),
        ("space-bunny-free", "high", "high", None, ("effort", "high")),
        ("qwen3.8-max", None, None, {"type": "enabled"}, ("on", None)),
        ("qwen3.8-max", "none", None, {"type": "disabled"}, ("off", None)),
        ("qwen3.8-max", "high", None, {"type": "enabled"}, ("on", None)),
    ],
)
@pytest.mark.asyncio
async def test_bundled_chat_models_render_their_reasoning_controls(
    model_id: str,
    selected_effort: str | None,
    wire_effort: str | None,
    thinking: dict[str, str] | None,
    intent: tuple[str, str | None],
) -> None:
    lookup = _bundled_lookup()
    adapter = OpenCodeZenAdapter(zen_config(), "zen-secret", model_lookup=lookup)
    body = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "done", "reasoning_content": "worked"},
                "finish_reason": "stop",
            }
        ]
    }

    with respx.mock:
        route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=body))
        response = await adapter.send(HELLO, model_id=model_id, thinking_effort=selected_effort)
    await adapter.aclose()

    payload = json.loads(route.calls.last.request.content)
    assert payload["model"] == model_id
    assert payload.get("reasoning_effort") == wire_effort
    assert payload.get("thinking") == thinking
    normalized = adapter.normalize_response(response, model_id=model_id)
    assert normalized["reasoning"] == "worked"
    description = adapter.describe_reasoning_render(model_id, selected_effort)
    assert (description.kind, description.effort_level) == intent


# ---------------------------------------------------------------------------
# Zen error policy
# ---------------------------------------------------------------------------


class _RecordingRefresher:
    """OAuth-style token getter that records every refresh request."""

    def __init__(self) -> None:
        self.refreshes: list[int] = []

    async def __call__(self) -> str:
        return "test-token"

    async def refresh_after_rejection(
        self, _rejected: str, *, status_code: int, response_body: str
    ) -> str | None:
        self.refreshes.append(status_code)
        return None


@pytest.mark.parametrize("streaming", [False, True], ids=["send", "stream"])
@pytest.mark.parametrize(
    ("model_id", "status", "error"),
    [
        pytest.param(RESPONSES_MODEL, 401, {"name": "CreditsError"}, id="responses-credits"),
        pytest.param(MESSAGES_MODEL, 401, {"type": "MonthlyLimitError"}, id="messages-monthly"),
        pytest.param(CHAT_MODEL, 401, {"name": "UserLimitError"}, id="chat-user-limit"),
        pytest.param(GEMINI_MODEL, 401, {"type": "ModelError"}, id="gemini-model"),
        pytest.param(RESPONSES_MODEL, 403, {"type": "FreeTierError"}, id="responses-free-tier"),
        pytest.param(MESSAGES_MODEL, 403, {"name": "FreeTierError"}, id="messages-free-tier"),
        pytest.param(CHAT_MODEL, 403, {"type": "FreeTierError"}, id="chat-free-tier"),
        pytest.param(GEMINI_MODEL, 403, {"name": "FreeTierError"}, id="gemini-free-tier"),
    ],
)
@pytest.mark.asyncio
async def test_every_wire_reports_zen_entitlement_errors_without_refreshing_credentials(
    model_id: str, status: int, error: dict[str, str], streaming: bool
) -> None:
    """Each wire hands Zen's error name or type to the classifier before OAuth recovery."""
    getter = _RecordingRefresher()
    adapter = zen_adapter(getter)

    with respx.mock:
        route = respx.route(method="POST").mock(
            return_value=httpx.Response(status, json={"error": error})
        )
        with pytest.raises(ProviderError) as caught:
            await _request(adapter, model_id, streaming=streaming)
    await adapter.aclose()

    assert type(caught.value) is ProviderError
    assert caught.value.retryable is False
    assert route.call_count == 1
    assert getter.refreshes == []
    if status == 403:
        # FreeTierError keeps its status and explains that another key cannot help.
        assert caught.value.status_code == 403
        assert str(caught.value) == _FREE_TIER_ACCESS_MESSAGE


@pytest.mark.parametrize(
    ("model_id", "status", "body", "error_type", "retryable"),
    [
        pytest.param(
            CHAT_MODEL, 401, "AuthError: invalid api key", ProviderAuthError, False, id="auth"
        ),
        pytest.param(
            CHAT_MODEL,
            403,
            "RegionError: unsupported country",
            ProviderError,
            False,
            id="region",
        ),
        pytest.param(
            CHAT_MODEL,
            429,
            "FreeUsageLimitError: daily allowance exhausted",
            ProviderError,
            False,
            id="allowance-exhausted",
        ),
        pytest.param(
            CHAT_MODEL,
            429,
            "RateLimitError: burst limit",
            ProviderRateLimitError,
            True,
            id="rate-limit",
        ),
        pytest.param(CHAT_MODEL, 529, "overloaded", ProviderError, False, id="chat-529"),
        pytest.param(
            MESSAGES_MODEL,
            529,
            '{"type":"error","error":{"type":"overloaded_error"}}',
            ProviderError,
            True,
            id="messages-529",
        ),
    ],
)
@pytest.mark.asyncio
async def test_zen_error_policy_separates_auth_region_allowance_and_retryable_failures(
    model_id: str, status: int, body: str, error_type: type[ProviderError], retryable: bool
) -> None:
    adapter = zen_adapter()

    with respx.mock, caller_owns_retries():
        respx.route(method="POST").mock(return_value=httpx.Response(status, text=body))
        with pytest.raises(ProviderError) as caught:
            await adapter.send(HELLO, model_id=model_id)
    await adapter.aclose()

    assert type(caught.value) is error_type
    assert caught.value.retryable is retryable
