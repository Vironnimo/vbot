"""Anthropic requests: configuration behavior."""

from __future__ import annotations

from core.providers._http_shared import PROVIDER_NON_STREAMING_READ_TIMEOUT_SECONDS

from .anthropic_test_support import (
    ANTHROPIC_CONFIG,
    ANTHROPIC_URL,
    API_KEY,
    IMAGE_WIRE_MEDIA_TYPES,
    MINIMAL_URL,
    NO_DEFAULTS_CONFIG,
    SAMPLE_MESSAGES,
    SUCCESS_RESPONSE,
    AnthropicAdapter,
    AnthropicCompatibleAdapter,
    AsyncMock,
    _anthropic_control_model,
    _strip_cache_control,
    httpx,
    json,
    pytest,
    respx,
)
from .anthropic_test_support import anthropic_adapter as anthropic_adapter
from .anthropic_test_support import custom_adapter as custom_adapter


def test_native_anthropic_uses_reusable_compatible_adapter() -> None:
    assert issubclass(AnthropicAdapter, AnthropicCompatibleAdapter)


def test_public_package_exports_anthropic_compatible_adapter() -> None:
    from core.providers import AnthropicCompatibleAdapter as PublicAnthropicCompatibleAdapter

    assert PublicAnthropicCompatibleAdapter is AnthropicCompatibleAdapter


@pytest.mark.asyncio
async def test_compatible_defaults_do_not_leak_native_anthropic_policy() -> None:
    adapter = AnthropicCompatibleAdapter(NO_DEFAULTS_CONFIG, API_KEY)
    try:
        payload = adapter._build_payload(
            [{"role": "user", "content": "hello"}],
            model_id="compatible-model",
        )

        assert adapter.wire_media_support("compatible-model") == IMAGE_WIRE_MEDIA_TYPES
        assert "cache_control" not in payload["messages"][-1]["content"][-1]
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_compatible_borrows_transport_and_allows_version_header_opt_out() -> None:
    borrowed_client = AsyncMock()
    adapter = AnthropicCompatibleAdapter(
        NO_DEFAULTS_CONFIG,
        API_KEY,
        client=borrowed_client,
        api_version=None,
    )

    headers = await adapter._build_headers()
    await adapter.aclose()

    assert headers["x-api-key"] == API_KEY
    assert "anthropic-version" not in headers
    borrowed_client.aclose.assert_not_awaited()


def test_client_timeout_bounds_non_streaming_generation_reads(anthropic_adapter):
    timeout = anthropic_adapter._client.timeout  # noqa: SLF001 - verify adapter wiring.

    assert timeout.connect == 60.0
    assert timeout.read == PROVIDER_NON_STREAMING_READ_TIMEOUT_SECONDS
    assert timeout.write == 60.0
    assert timeout.pool == 60.0


class TestConstructorContract:
    """Verify the shared optional model_lookup constructor contract."""

    def test_constructor_defaults_model_lookup_to_none(self):
        """Constructing without model_lookup keeps _model_lookup unset (None)."""
        adapter = AnthropicAdapter(ANTHROPIC_CONFIG, API_KEY)

        assert adapter._model_lookup is None

    def test_constructor_stores_model_lookup_callable(self):
        """Constructing with model_lookup stores the callable for later adapter use."""

        def model_lookup(model_id: str):
            _ = model_id
            return None

        adapter = AnthropicAdapter(ANTHROPIC_CONFIG, API_KEY, model_lookup=model_lookup)

        assert adapter._model_lookup is model_lookup


class TestSendRequestFormat:
    "Verify that send() translates messages to the correct Anthropic format."

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_applies_defaults_from_config(self, anthropic_adapter):
        """Defaults from ProviderConfig are included when not overridden."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["max_tokens"] == 4096

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_kwargs_override_defaults_with_context_safety(self, anthropic_adapter):
        """Caller kwargs win, then clamp against the conservative unknown-model window."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await anthropic_adapter.send(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4-20250219",
            max_tokens=8192,
        )

        # Assert
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert 4096 < request_body["max_tokens"] < 8192

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_without_defaults(self):
        """When config has no defaults, only model and messages are sent."""
        # Arrange
        adapter = AnthropicAdapter(NO_DEFAULTS_CONFIG, API_KEY)
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert "model" in request_body
        assert "messages" in request_body
        assert "max_tokens" not in request_body

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_thinking_kwargs_pass_through(self, anthropic_adapter):
        """Thinking and output_config kwargs are passed through."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        thinking = {"type": "enabled", "budget_tokens": 10000}
        output_config = {"effort": "high"}

        # Act
        await anthropic_adapter.send(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4-20250219",
            thinking=thinking,
            output_config=output_config,
        )

        # Assert
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["thinking"] == thinking
        assert request_body["output_config"] == output_config


class TestMaxTokensResolution:
    """The output ``max_tokens`` defaults to the model's catalog ceiling."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_defaults_max_tokens_to_model_ceiling(self):
        """With no caller value, ``max_tokens`` is the model's output ceiling."""
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        adapter = AnthropicAdapter(
            NO_DEFAULTS_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _anthropic_control_model(model_id, control="budget"),
        )

        await adapter.send(SAMPLE_MESSAGES, model_id="claude-opus-4-1")

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["max_tokens"] == 64000

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_explicit_max_tokens_wins_over_ceiling(self):
        """An explicit positive caller value overrides the model ceiling."""
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        adapter = AnthropicAdapter(
            NO_DEFAULTS_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _anthropic_control_model(model_id, control="budget"),
        )

        await adapter.send(SAMPLE_MESSAGES, model_id="claude-opus-4-1", max_tokens=1234)

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["max_tokens"] == 1234

    @respx.mock
    @pytest.mark.asyncio
    async def test_context_clamp_also_bounds_reasoning_budget(self):
        """A context-clamped output allowance remains the reasoning budget's hard bound."""
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        adapter = AnthropicAdapter(
            NO_DEFAULTS_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _anthropic_control_model(
                model_id,
                control="budget",
                context_window=10_000,
                max_output_tokens=10_000,
            ),
        )
        messages = [{"role": "user", "content": "x" * 8_000}]

        await adapter.send(
            messages, model_id="claude-context-equals-output", thinking_effort="high"
        )

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert 0 < request_body["max_tokens"] < 10_000
        assert request_body["thinking"] == {
            "type": "enabled",
            "budget_tokens": request_body["max_tokens"] - 1,
        }

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_non_positive_max_tokens_falls_back_to_ceiling(self):
        """A non-positive caller value is ignored (it would 400) — ceiling wins."""
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        adapter = AnthropicAdapter(
            NO_DEFAULTS_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _anthropic_control_model(model_id, control="budget"),
        )

        await adapter.send(SAMPLE_MESSAGES, model_id="claude-opus-4-1", max_tokens=0)

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["max_tokens"] == 64000

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_max_tokens_falls_back_to_config_default_without_ceiling(self):
        """When the ceiling is unknown (no lookup), the config default is used."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        adapter = AnthropicAdapter(ANTHROPIC_CONFIG, API_KEY)  # default max_tokens=4096, no lookup

        await adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["max_tokens"] == 4096

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_budget_effort_keeps_output_headroom_under_ceiling(self):
        """Regression: a mid-effort budget no longer consumes the whole allowance.

        Under a flat 8K cap the ``medium`` budget (8192) was clamped to ~8191,
        leaving ~1 token for the answer. Defaulting ``max_tokens`` to the model's
        real ceiling leaves the budget intact with ample output headroom.
        """
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        adapter = AnthropicAdapter(
            NO_DEFAULTS_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _anthropic_control_model(model_id, control="budget"),
        )

        await adapter.send(SAMPLE_MESSAGES, model_id="claude-opus-4-1", thinking_effort="medium")

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["max_tokens"] == 64000
        assert request_body["thinking"] == {"type": "enabled", "budget_tokens": 8192}


def test_request_image_estimate_receives_active_model(anthropic_adapter):
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "media",
                    "media_type": "image/png",
                    "base64": (
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lE"
                        "QVR42mP8/x8AAwMCAO+aXfcAAAAASUVORK5CYII="
                    ),
                }
            ],
        }
    ]
    known = anthropic_adapter.estimate_request_input_tokens(messages, model_id="claude-sonnet-4-6")
    fallback = anthropic_adapter.estimate_request_input_tokens(messages, model_id="unknown")
    assert fallback - known == 4096 - 1
