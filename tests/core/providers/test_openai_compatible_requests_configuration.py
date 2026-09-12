"""Openai compatible requests: configuration behavior."""

from __future__ import annotations

from core.providers.providers import resolve_request_output_limit

from .openai_compatible_test_support import (
    API_KEY,
    MINIMAL_URL,
    NO_DEFAULTS_CONFIG,
    OPENAI_CONFIG,
    OPENAI_MULTI_AUTH_CONFIG,
    OPENAI_URL,
    OPENROUTER_URL,
    SAMPLE_MESSAGES,
    SUCCESS_RESPONSE,
    AuthConfig,
    Capabilities,
    ConnectionConfig,
    Model,
    OpenAICompatibleAdapter,
    ReasoningCapabilities,
    httpx,
    json,
    pytest,
    replace,
    respx,
)
from .openai_compatible_test_support import openai_adapter as openai_adapter
from .openai_compatible_test_support import openrouter_adapter as openrouter_adapter


# stream() — SSE parsing
def _model_with_output_ceiling(
    model_id: str,
    ceiling: int | None,
    *,
    context_window: int = 200_000,
) -> Model:
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
        ),
        context_window=context_window,
        max_output_tokens=ceiling,
    )


# send() — headers and auth
class TestSendHeaders:
    """Verify that send() sends the correct auth and extra headers."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_bearer_auth_header(self, openai_adapter):
        """OpenAI config sends Authorization: Bearer <key>."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        # Assert
        assert route.called
        auth_header = route.calls.last.request.headers.get("authorization")
        assert auth_header == f"Bearer {API_KEY}"

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_custom_auth_header(self):
        """Config with x-api-key header sends the key without Bearer prefix."""
        # Arrange
        adapter = OpenAICompatibleAdapter(NO_DEFAULTS_CONFIG, API_KEY)
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="minimal-model")

        # Assert
        api_key_header = route.calls.last.request.headers.get("x-api-key")
        assert api_key_header == API_KEY

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_uses_selected_connection_auth_header(self):
        """Selected connection auth metadata controls the request auth header."""
        # Arrange
        selected_connection = OPENAI_MULTI_AUTH_CONFIG.get_connection("service-account")
        adapter = OpenAICompatibleAdapter(
            OPENAI_MULTI_AUTH_CONFIG,
            API_KEY,
            auth_config=selected_connection.auth,
        )
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        # Assert
        request_headers = route.calls.last.request.headers
        assert request_headers.get("x-service-token") == f"Token {API_KEY}"
        assert request_headers.get("authorization") is None

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_keyless_connection_omits_auth_header(self):
        """A none connection sends no auth header while preserving extra headers."""
        # Arrange
        keyless_config = replace(
            NO_DEFAULTS_CONFIG,
            connections=[
                ConnectionConfig(
                    id="local",
                    type="none",
                    label="Local",
                    auth=AuthConfig(header="", prefix="", credential_key=""),
                )
            ],
            extra_headers={"X-Client": "vBot"},
        )
        adapter = OpenAICompatibleAdapter(keyless_config, "")
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="minimal-model")

        # Assert
        request_headers = route.calls.last.request.headers
        assert request_headers.get("authorization") is None
        assert request_headers["x-client"] == "vBot"

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_extra_headers(self, openrouter_adapter):
        """OpenRouter config includes extra HTTP-Referer and X-Title headers."""
        # Arrange
        route = respx.post(OPENROUTER_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await openrouter_adapter.send(SAMPLE_MESSAGES, model_id="openai/gpt-5.2")

        # Assert
        request = route.calls.last.request
        assert request.headers.get("http-referer") == "https://vbot.app"
        assert request.headers.get("x-title") == "vBot"


# send() — success response
class TestSendProviderConfig:
    """Verify that provider config values are correctly used."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_uses_base_url_from_config(self, openrouter_adapter):
        """The request goes to the base_url specified in ProviderConfig."""
        # Arrange
        route = respx.post(OPENROUTER_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await openrouter_adapter.send(SAMPLE_MESSAGES, model_id="openai/gpt-5.2")

        # Assert
        assert route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_uses_auth_from_config(self):
        """Config with prefix='' sends the key directly in the auth header."""
        # Arrange
        adapter = OpenAICompatibleAdapter(NO_DEFAULTS_CONFIG, API_KEY)
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await adapter.send(SAMPLE_MESSAGES, model_id="minimal-model")

        # Assert
        api_key_header = route.calls.last.request.headers.get("x-api-key")
        assert api_key_header == API_KEY  # No "Bearer " prefix


# _build_payload() — None-valued caller kwargs
class TestBuildPayloadNoneKwargs:
    """``None``-valued caller kwargs are dropped, letting provider defaults win.

    Falsy-but-not-None values (e.g. ``0.0``) must survive. Explicit non-None
    values must still override the default. Covers both ``send()`` and
    ``stream()`` payload construction (both call ``_build_payload``).
    """

    @respx.mock
    @pytest.mark.asyncio
    async def test_none_kwarg_drops_key_and_provider_default_applies(self, openai_adapter):
        """``temperature=None`` is absent from the payload; default fills in."""
        # Arrange — OPENAI_CONFIG declares defaults.temperature=0.7
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2", temperature=None)

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert "temperature" in request_body
        assert request_body["temperature"] == 0.7  # from defaults

    @respx.mock
    @pytest.mark.asyncio
    async def test_explicit_zero_kwarg_survives_through_send(self, openai_adapter):
        """``temperature=0.0`` (falsy but not None) survives the None filter."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2", temperature=0.0)

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["temperature"] == 0.0

    @respx.mock
    @pytest.mark.asyncio
    async def test_explicit_nonzero_kwarg_overrides_default(self, openai_adapter):
        """Explicit non-None kwargs continue to override the provider default."""
        # Arrange
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

        # Act
        await openai_adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2", temperature=0.3)

        # Assert
        request_body = json.loads(route.calls.last.request.content)
        assert request_body["temperature"] == 0.3

    @respx.mock
    @pytest.mark.asyncio
    async def test_none_kwarg_drops_key_for_stream(self, openai_adapter):
        """``stream()`` also drops ``None`` caller kwargs before sending."""
        sse_body = (
            'data: {"id":"chatcmpl-1","choices":[{"delta":{"content":"Hi"}}]}\n\ndata: [DONE]\n\n'
        )
        route = respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(
                200, text=sse_body, headers={"content-type": "text/event-stream"}
            )
        )

        async for _ in openai_adapter.stream(SAMPLE_MESSAGES, model_id="gpt-5.2", temperature=None):
            pass

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["temperature"] == 0.7  # default applied
        assert "stream" in request_body  # stream() still adds stream=true


class TestOutputLimitDefault:
    """The output allowance defaults to the model's catalog ceiling.

    Sibling of the Anthropic adapter's ceiling-aware ``max_tokens``: the flat
    provider-config ``max_tokens`` default (e.g. 4096/8192) truncates any model
    whose real ceiling is higher, so an unspecified allowance defaults to the
    catalog ``max_output_tokens`` instead.
    """

    @respx.mock
    @pytest.mark.asyncio
    async def test_defaults_max_tokens_to_model_ceiling_over_config_default(self):
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
        adapter = OpenAICompatibleAdapter(
            OPENAI_CONFIG,  # config default max_tokens=4096
            API_KEY,
            model_lookup=lambda model_id: _model_with_output_ceiling(model_id, 128_000),
        )

        await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["max_tokens"] == 128_000

    @respx.mock
    @pytest.mark.asyncio
    async def test_explicit_caller_limit_wins_over_ceiling(self):
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
        adapter = OpenAICompatibleAdapter(
            OPENAI_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _model_with_output_ceiling(model_id, 128_000),
        )

        await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2", max_tokens=512)

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["max_tokens"] == 512

    @respx.mock
    @pytest.mark.asyncio
    async def test_explicit_max_completion_tokens_suppresses_ceiling_default(self):
        """A caller output limit under any accepted key suppresses the ceiling inject."""
        adapter = OpenAICompatibleAdapter(
            NO_DEFAULTS_CONFIG,  # no config max_tokens fallback to muddy the assertion
            API_KEY,
            model_lookup=lambda model_id: _model_with_output_ceiling(model_id, 128_000),
        )
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        await adapter.send(SAMPLE_MESSAGES, model_id="minimal-model", max_completion_tokens=777)

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["max_completion_tokens"] == 777
        assert "max_tokens" not in request_body

    @respx.mock
    @pytest.mark.asyncio
    async def test_unknown_ceiling_keeps_config_default(self):
        route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
        adapter = OpenAICompatibleAdapter(
            OPENAI_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _model_with_output_ceiling(model_id, None),
        )

        await adapter.send(SAMPLE_MESSAGES, model_id="gpt-5.2")

        request_body = json.loads(route.calls.last.request.content)
        assert request_body["max_tokens"] == 4096

    @respx.mock
    @pytest.mark.asyncio
    async def test_equal_context_and_output_ceiling_leaves_room_for_nemo_request_input(self):
        """Regression: Nemo's 256K output ceiling must not consume its whole context."""
        route = respx.post(MINIMAL_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        messages = [
            {"role": "system", "content": "You are a concise assistant."},
            {"role": "user", "content": "x" * 8_000},
        ]
        tools = [
            {
                "name": "large_tool",
                "description": "y" * 24_000,
                "parameters": {"type": "object", "properties": {}},
            }
        ]
        adapter = OpenAICompatibleAdapter(
            NO_DEFAULTS_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _model_with_output_ceiling(
                model_id,
                256_000,
                context_window=256_000,
            ),
        )

        await adapter.send(messages, model_id="nvidia/nemotron-nano-9b-v2:free", tools=tools)

        request_body = json.loads(route.calls.last.request.content)
        estimated_input = adapter.estimate_request_input_tokens(
            messages, model_id="nvidia/nemotron-nano-9b-v2:free", tools=tools
        )
        expected = resolve_request_output_limit(
            explicit_limit=None,
            model_output_limit=256_000,
            provider_default=None,
            effective_context_window=256_000,
            estimated_input_tokens=estimated_input,
        )
        assert request_body["max_tokens"] == expected
        assert 0 < request_body["max_tokens"] < 256_000


@pytest.mark.asyncio
@respx.mock
async def test_output_capacity_uses_scoped_input_projection_and_separate_reserve():
    from core.providers.adapter import request_input_budget

    route = respx.post(MINIMAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
    adapter = OpenAICompatibleAdapter(
        NO_DEFAULTS_CONFIG,
        API_KEY,
        model_lookup=lambda model_id: _model_with_output_ceiling(
            model_id, 256_000, context_window=256_000
        ),
    )
    messages = [{"role": "user", "content": "x" * 8_000}]
    model_id = "nvidia/nemotron-nano-9b-v2:free"
    with request_input_budget(model_id, 150_000):
        await adapter.send(messages, model_id=model_id)
    body = json.loads(route.calls.last.request.content)
    # 256k window minus measured input minus the existing 25% output reserve.
    assert body["max_tokens"] == 68_500
    assert set(body) <= {"model", "messages", "max_tokens", "stream"}


def test_request_image_estimate_receives_active_model(openai_adapter):
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
    known = openai_adapter.estimate_request_input_tokens(messages, model_id="gpt-4o")
    fallback = openai_adapter.estimate_request_input_tokens(messages, model_id="unknown")
    assert fallback - known == 4096 - 255
