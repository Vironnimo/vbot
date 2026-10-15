"""OpenCode Zen Adapter: catalog defaults, wire routing, reasoning and error policy."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from core.models.models import Model, ModelRegistry, ReasoningCapabilities
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

    def lookup(model_id: str) -> Model | None:
        try:
            return registry.get("opencode-zen", model_id)
        except KeyError:
            return None

    return lookup


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
        ("mistral-large-4", "chat_completions"),
        ("gemini-4-argon", "gemini"),
        ("gemini-3.8-flash", "gemini"),
    ],
)
def test_bundled_catalog_models_are_admitted_on_their_reviewed_wire(
    model_id: str, protocol: str
) -> None:
    """Catalog hints, family defaults and exact corrections select each wire."""
    adapter = OpenCodeZenAdapter(zen_config(), "zen-secret", model_lookup=_bundled_lookup())

    profile = adapter.wire_profile(model_id)

    assert (profile.protocol, profile.admission.state) == (protocol, "available")


@pytest.mark.parametrize(
    ("model_id", "stale_entry_of", "streaming"),
    [
        pytest.param("mimo-v2.6-flash-free", CHAT_MODEL, False, id="free-tier-send"),
        pytest.param(
            "longcat-2.5-preview-free", RESPONSES_MODEL, True, id="hinted-free-tier-stream"
        ),
        pytest.param("fledge-alpha-free", CHAT_MODEL, False, id="fledge-free-tier-send"),
        pytest.param("claude-opus-4-1", MESSAGES_MODEL, True, id="hinted-retired-stream"),
        pytest.param("jev-1.13", CHAT_MODEL, False, id="unsupported-task-route"),
    ],
)
@pytest.mark.asyncio
async def test_unusable_selection_fails_before_network(
    model_id: str, stale_entry_of: str | None, streaming: bool
) -> None:
    """Concrete access, retirement and unsupported-task exceptions survive catalog hints."""
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
    assert str(caught.value).startswith(f"Model '{model_id}' is ")
    assert not route.called


# ---------------------------------------------------------------------------
# Wire routing and reasoning
# ---------------------------------------------------------------------------


def test_messages_estimation_uses_native_replay_without_duplicate_canonical_reasoning() -> None:
    adapter = zen_adapter()
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": "Native answer",
            "reasoning_meta": {
                "content_blocks": [
                    {"type": "thinking", "thinking": "Native thought", "signature": "opaque"},
                ]
            },
        },
        {"role": "user", "content": "continue"},
    ]
    native = adapter.estimate_request_input_tokens(messages, model_id=MESSAGES_MODEL)
    assert native > 0
    messages[0]["reasoning"] = "redundant canonical thought" * 1000
    assert adapter.estimate_request_input_tokens(messages, model_id=MESSAGES_MODEL) == native


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
        *[
            pytest.param(
                model_id,
                RESPONSES_URL,
                _RESPONSES_BODY,
                "authorization",
                "Bearer zen-secret",
                "x-api-key",
                id=model_id,
            )
            for model_id in ("gpt-future", "grok-future", "muse-future-free")
        ],
        *[
            pytest.param(
                model_id,
                MESSAGES_URL,
                _MESSAGES_BODY,
                "x-api-key",
                "zen-secret",
                "authorization",
                id=model_id,
            )
            for model_id in ("claude-future", "qwen99")
        ],
        *[
            pytest.param(
                model_id,
                CHAT_URL,
                _CHAT_BODY,
                "authorization",
                "Bearer zen-secret",
                "x-api-key",
                id=model_id,
            )
            for model_id in (
                "future-model",
                "future-model-free",
                "big-pickle",
                "minimax-m99",
                "qwen99-max-free",
            )
        ],
    ],
)
@pytest.mark.asyncio
async def test_each_model_uses_its_resolved_wire_and_auth_header(
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
    assert ("cache_control" in json.dumps(payload)) is (url == MESSAGES_URL)
    assert adapter.normalize_response(response, model_id=model_id)["content"] == "done"
    if model_id not in (RESPONSES_MODEL, MESSAGES_MODEL, CHAT_MODEL):
        assert adapter.wire_profile(model_id).status == "inferred"
    await adapter.aclose()


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
        ("gpt-future", "none", "low"),
        ("gpt-future", "max", "max"),
        ("gpt-6-sol", "none", "none"),
        ("gpt-6.1-sol", "none", "low"),
        ("gpt-6.1-sol", "minimal", "low"),
        ("gpt-6.1-sol", "max", "max"),
    ],
)
@pytest.mark.asyncio
async def test_current_and_future_gpt_models_use_their_resolved_effort(
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
    if model_id == "gpt-future":
        assert adapter.wire_profile(model_id).replay.scope == "full_history"
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
        ("qwen99-max", "none", None, {"type": "disabled"}, ("off", None)),
        ("qwen99-max", "high", None, {"type": "enabled"}, ("on", None)),
        ("deepseek-v99", "medium", "low", None, ("effort", "low")),
        ("deepseek-v99", "max", "max", None, ("effort", "max")),
        ("glm-99", "medium", "low", None, ("effort", "low")),
        ("glm-99", "max", "max", None, ("effort", "max")),
        ("kimi-k99", "medium", "max", None, ("effort", "max")),
        ("mistral-large-99", "none", "none", None, ("off", "none")),
        ("mistral-large-99", "max", "high", None, ("effort", "high")),
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
    if model_id == "mistral-large-99":
        assert adapter.reasoning_replay_fidelity(model_id) == "meta_only"


@pytest.mark.asyncio
async def test_inferred_gpt_minimum_effort_follows_the_new_models_reported_ladder() -> None:
    model = zen_model(RESPONSES_MODEL)
    model = replace(
        model,
        model_id="gpt-future",
        capabilities=replace(
            model.capabilities,
            reasoning=ReasoningCapabilities(
                supported=True, control="levels", levels=("medium", "high")
            ),
        ),
    )
    adapter = OpenCodeZenAdapter(zen_config(), "zen-secret", model_lookup=lambda _: model)

    with respx.mock:
        route = respx.post(RESPONSES_URL).mock(
            return_value=httpx.Response(200, json=_RESPONSES_BODY)
        )
        await adapter.send(HELLO, model_id=model.model_id, thinking_effort="none")
    await adapter.aclose()

    assert json.loads(route.calls.last.request.content)["reasoning"]["effort"] == "medium"


def test_new_chat_family_defaults_yield_to_a_reported_responses_protocol() -> None:
    model = replace(
        zen_model(RESPONSES_MODEL),
        model_id="mistral-large-99",
        capabilities=replace(
            zen_model(RESPONSES_MODEL).capabilities,
            reasoning=ReasoningCapabilities(supported=None),
        ),
    )
    adapter = OpenCodeZenAdapter(zen_config(), "zen-secret", model_lookup=lambda _: model)

    profile = adapter.wire_profile(model.model_id)

    assert profile.protocol == "responses"
    assert profile.reasoning.ladder == ("low", "medium", "high", "xhigh")
    assert profile.reasoning.supported is None


@pytest.mark.parametrize(
    ("model_id", "effort", "wire_effort"),
    [
        ("claude-future", "minimal", "low"),
        ("gemini-future", "none", "low"),
        ("muse-future", "minimal", "minimal"),
    ],
)
def test_future_native_families_use_the_latest_gateway_effort_floor(
    model_id: str, effort: str, wire_effort: str
) -> None:
    adapter = zen_adapter()

    assert adapter.describe_reasoning_render(model_id, effort).effort_level == wire_effort


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
