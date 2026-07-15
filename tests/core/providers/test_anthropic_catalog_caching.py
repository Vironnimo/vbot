"""Anthropic catalog, sampling, and prompt-caching tests."""

from __future__ import annotations

from .anthropic_test_support import (
    ANTHROPIC_CONFIG,
    ANTHROPIC_METADATA_KEY,
    ANTHROPIC_URL,
    API_KEY,
    CANONICAL_MESSAGES_WITH_TOOL_LOOP,
    REASONING_CONTROL_BUDGET,
    REASONING_CONTROL_LEVELS,
    SAMPLE_MESSAGES,
    SAMPLE_MESSAGES_WITH_SYSTEM,
    SAMPLE_TOOLS,
    SUCCESS_RESPONSE,
    SUPPORTS_TEMPERATURE_METADATA_FIELD,
    AnthropicAdapter,
    Capabilities,
    Model,
    ReasoningCapabilities,
    _strip_cache_control,
    httpx,
    json,
    pytest,
    respx,
)
from .anthropic_test_support import anthropic_adapter as anthropic_adapter


def _anthropic_catalog_entry(
    model_id: str,
    *,
    adaptive: bool,
    enabled: bool,
    efforts: tuple[str, ...] = (),
    thinking: bool = True,
    image: bool = True,
    pdf: bool = True,
    structured: bool = True,
    context_window: int = 200000,
    max_output_tokens: int = 64000,
) -> dict:
    """Build a raw ``/models`` entry mirroring the live Anthropic catalog shape."""

    capabilities: dict = {
        "image_input": {"supported": image},
        "pdf_input": {"supported": pdf},
        "structured_outputs": {"supported": structured},
        "thinking": {
            "supported": thinking,
            "types": {
                "enabled": {"supported": enabled},
                "adaptive": {"supported": adaptive},
            },
        },
        "effort": {
            "supported": bool(efforts),
            **{level: {"supported": True} for level in efforts},
        },
    }
    return {
        "type": "model",
        "id": model_id,
        "display_name": f"Display {model_id}",
        "max_input_tokens": context_window,
        "max_tokens": max_output_tokens,
        "capabilities": capabilities,
    }


class TestCatalogDiscovery:
    """The discovery normalizer maps the live ``/models`` caps onto a vBot Model."""

    def test_adaptive_only_model_maps_to_levels_and_drops_sampling(self):
        """An adaptive-only model (Opus 4.7+/Fable) → levels control, no temperature."""
        model = AnthropicAdapter.normalize_catalog_entry(
            _anthropic_catalog_entry(
                "claude-opus-4-8",
                adaptive=True,
                enabled=False,
                efforts=("low", "medium", "high", "xhigh", "max"),
                context_window=1000000,
                max_output_tokens=128000,
            )
        )

        assert model.model_id == "claude-opus-4-8"
        assert model.name == "Display claude-opus-4-8"
        assert model.context_window == 1000000
        assert model.max_output_tokens == 128000
        assert model.capabilities.vision is True
        assert model.capabilities.tools is True
        assert model.capabilities.json_mode is True
        assert model.capabilities.input_modalities == ("text", "image", "pdf")
        assert model.capabilities.reasoning == ReasoningCapabilities(
            supported=True,
            control=REASONING_CONTROL_LEVELS,
            levels=("low", "medium", "high", "xhigh", "max"),
        )
        assert model.metadata[ANTHROPIC_METADATA_KEY][SUPPORTS_TEMPERATURE_METADATA_FIELD] is False

    def test_budget_model_maps_to_budget_control_and_keeps_sampling(self):
        """A native-thinking model with no adaptive (Haiku 4.5) → budget control."""
        model = AnthropicAdapter.normalize_catalog_entry(
            _anthropic_catalog_entry("claude-haiku-4-5", adaptive=False, enabled=True)
        )

        assert model.capabilities.reasoning == ReasoningCapabilities(
            supported=True,
            control=REASONING_CONTROL_BUDGET,
            levels=(),
        )
        assert model.metadata[ANTHROPIC_METADATA_KEY][SUPPORTS_TEMPERATURE_METADATA_FIELD] is True

    def test_effort_ladder_without_adaptive_maps_to_budget(self):
        """Opus 4.5 exposes an effort ladder but no adaptive thinking → budget (not levels).

        Sending adaptive thinking to such a model is a wire error; budget control
        renders the native ``thinking: {type: enabled, budget_tokens}`` it accepts.
        """
        model = AnthropicAdapter.normalize_catalog_entry(
            _anthropic_catalog_entry(
                "claude-opus-4-5",
                adaptive=False,
                enabled=True,
                efforts=("low", "medium", "high"),
            )
        )

        assert model.capabilities.reasoning.control == REASONING_CONTROL_BUDGET
        assert model.capabilities.reasoning.levels == ()
        assert model.metadata[ANTHROPIC_METADATA_KEY][SUPPORTS_TEMPERATURE_METADATA_FIELD] is True

    def test_non_reasoning_model_has_no_control_and_keeps_sampling(self):
        """A model the catalog marks reasoning-unsupported carries no control."""
        model = AnthropicAdapter.normalize_catalog_entry(
            _anthropic_catalog_entry("claude-legacy", adaptive=False, enabled=False, thinking=False)
        )

        assert model.capabilities.reasoning == ReasoningCapabilities(supported=False)
        assert model.metadata[ANTHROPIC_METADATA_KEY][SUPPORTS_TEMPERATURE_METADATA_FIELD] is True

    def test_display_name_falls_back_to_id(self):
        entry = _anthropic_catalog_entry("claude-x", adaptive=True, enabled=False)
        del entry["display_name"]

        model = AnthropicAdapter.normalize_catalog_entry(entry)

        assert model.name == "claude-x"

    def test_discovery_headers_add_version_and_keep_auth(self):
        headers = AnthropicAdapter.discovery_headers(
            ANTHROPIC_CONFIG, API_KEY, {"x-api-key": "secret"}
        )

        assert headers["anthropic-version"] == "2023-06-01"
        assert headers["x-api-key"] == "secret"

    def test_discovery_params_pages_the_listing(self):
        assert AnthropicAdapter.discovery_params() == {"limit": "1000"}


# ---------------------------------------------------------------------------
# Sampling-parameter dropping for adaptive-only models
# ---------------------------------------------------------------------------


def _anthropic_sampling_model(model_id: str, *, supports_temperature: bool) -> Model:
    """A reasoning Claude carrying the discovery-derived temperature-support flag."""
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(
                supported=True,
                control=REASONING_CONTROL_LEVELS,
                levels=("low", "medium", "high"),
            ),
        ),
        context_window=1000000,
        max_output_tokens=128000,
        metadata={
            ANTHROPIC_METADATA_KEY: {SUPPORTS_TEMPERATURE_METADATA_FIELD: supports_temperature}
        },
    )


class TestSamplingParameterDropping:
    """Sampling params are dropped for models that reject them, even without thinking."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_drops_sampling_for_unsupported_model_without_thinking(self):
        """An adaptive-only model with no effort still must not receive temperature."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        adapter = AnthropicAdapter(
            ANTHROPIC_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _anthropic_sampling_model(
                model_id, supports_temperature=False
            ),
        )

        await adapter.send(
            SAMPLE_MESSAGES,
            model_id="claude-opus-4-8",
            temperature=0.5,
            top_p=0.9,
            top_k=10,
        )

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert "temperature" not in request_body
        assert "top_p" not in request_body
        assert "top_k" not in request_body
        assert "thinking" not in request_body

    @respx.mock
    @pytest.mark.asyncio
    async def test_keeps_sampling_for_supported_model(self):
        """A sampling-capable model keeps temperature when thinking is inactive."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        adapter = AnthropicAdapter(
            ANTHROPIC_CONFIG,
            API_KEY,
            model_lookup=lambda model_id: _anthropic_sampling_model(
                model_id, supports_temperature=True
            ),
        )

        await adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-6", temperature=0.5)

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["temperature"] == 0.5

    @respx.mock
    @pytest.mark.asyncio
    async def test_keeps_sampling_when_lookup_absent(self):
        """With no catalog lookup the flag is unknown, so sampling is left alone."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        adapter = AnthropicAdapter(ANTHROPIC_CONFIG, API_KEY)

        await adapter.send(SAMPLE_MESSAGES, model_id="claude-opus-4-8", temperature=0.5)

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["temperature"] == 0.5


# ---------------------------------------------------------------------------
# Prompt caching — cache_control breakpoint placement
# ---------------------------------------------------------------------------

EPHEMERAL = {"type": "ephemeral"}


def _cache_marked_blocks(payload: dict) -> list[dict]:
    """All content blocks in the payload (system + messages) carrying a marker."""
    marked: list[dict] = []
    system = payload.get("system")
    if isinstance(system, list):
        marked += [b for b in system if isinstance(b, dict) and "cache_control" in b]
    for message in payload.get("messages", []):
        content = message.get("content")
        if isinstance(content, list):
            marked += [b for b in content if isinstance(b, dict) and "cache_control" in b]
    return marked


def _long_text_history(turns: int) -> list[dict]:
    """A user/assistant text history of ``turns`` messages (no system)."""
    history: list[dict] = []
    for index in range(turns):
        role = "user" if index % 2 == 0 else "assistant"
        history.append({"role": role, "content": [{"type": "text", "text": f"m{index}"}]})
    return history


class TestPromptCaching:
    @respx.mock
    @pytest.mark.asyncio
    async def test_marks_last_system_block(self, anthropic_adapter):
        """A string system prompt becomes a block list with a marker on the last block."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        await anthropic_adapter.send(
            SAMPLE_MESSAGES_WITH_SYSTEM, model_id="claude-sonnet-4-20250219"
        )

        body = json.loads(route.calls.last.request.content)
        assert body["system"] == [
            {
                "type": "text",
                "text": "You are a helpful assistant.",
                "cache_control": EPHEMERAL,
            }
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_tools_are_cached_via_the_system_marker(self, anthropic_adapter):
        """Tools render before system, so the system marker caches them — tools carry none."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        await anthropic_adapter.send(
            SAMPLE_MESSAGES_WITH_SYSTEM,
            model_id="claude-sonnet-4-20250219",
            tools=SAMPLE_TOOLS,
        )

        body = json.loads(route.calls.last.request.content)
        assert all("cache_control" not in tool for tool in body["tools"])
        assert body["system"][-1]["cache_control"] == EPHEMERAL

    @respx.mock
    @pytest.mark.asyncio
    async def test_rolling_markers_on_three_most_recent_messages(self, anthropic_adapter):
        """Only the last three messages are marked; earlier history stays unmarked."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        await anthropic_adapter.send(_long_text_history(6), model_id="claude-sonnet-4-20250219")

        messages = json.loads(route.calls.last.request.content)["messages"]
        marked = ["cache_control" in message["content"][-1] for message in messages]
        assert marked == [False, False, False, True, True, True]

    @respx.mock
    @pytest.mark.asyncio
    async def test_never_exceeds_four_breakpoints(self, anthropic_adapter):
        """System + rolling markers stay within Anthropic's four-breakpoint limit."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        history = [{"role": "system", "content": "Sys"}, *_long_text_history(8)]

        await anthropic_adapter.send(history, model_id="claude-sonnet-4-20250219")

        assert len(_cache_marked_blocks(json.loads(route.calls.last.request.content))) == 4

    @respx.mock
    @pytest.mark.asyncio
    async def test_caches_messages_without_a_system_prompt(self, anthropic_adapter):
        """No system prompt still caches the recent conversation tail."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        await anthropic_adapter.send(_long_text_history(2), model_id="claude-sonnet-4-20250219")

        body = json.loads(route.calls.last.request.content)
        assert "system" not in body
        assert [m["content"][-1].get("cache_control") for m in body["messages"]] == [
            EPHEMERAL,
            EPHEMERAL,
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_marker_skips_reasoning_block(self, anthropic_adapter):
        """The marker rides the tool_use block, never a replayed thinking block."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        await anthropic_adapter.send(
            CANONICAL_MESSAGES_WITH_TOOL_LOOP,
            model_id="claude-sonnet-4-20250219",
            thinking_effort="high",
        )

        assistant = json.loads(route.calls.last.request.content)["messages"][1]
        blocks_by_type = {block["type"]: block for block in assistant["content"]}
        assert "cache_control" not in blocks_by_type["thinking"]
        assert blocks_by_type["tool_use"]["cache_control"] == EPHEMERAL
