"""Opencode go: catalog behavior."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
import respx

from core.models.models import Capabilities, Model, ModelRegistry, ReasoningCapabilities
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.opencode_go import (
    OPENCODE_SESSION_HEADER,
    OPENCODE_SESSION_ID_KWARG,
    OpenCodeGoAdapter,
)
from core.providers.providers import ProviderConfig
from core.sessions.titles import SessionTitleService
from tests.core.providers.opencode_go_helpers import (
    ANTHROPIC_MESSAGES_MODELS,
    API_KEY,
    OPENCODE_GO_MESSAGES_URL,
    OPENCODE_GO_RESPONSES_URL,
    OPENCODE_GO_URL,
    RESPONSES_COMPLETED_RESPONSE,
)
from tests.core.providers.opencode_go_helpers import (
    opencode_go_adapter as opencode_go_adapter,
)
from tests.core.providers.opencode_go_helpers import (
    opencode_go_config as opencode_go_config,
)


def model_with_output_limit(
    model_id: str,
    max_output_tokens: int,
    *,
    context_window: int = 1_000_000,
) -> Model:
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=True),
        ),
        context_window=context_window,
        max_output_tokens=max_output_tokens,
    )


@pytest.mark.parametrize(
    ("effort", "thinking", "level"),
    [
        (None, "enabled", None),
        ("none", "disabled", None),
        ("minimal", "enabled", "low"),
        ("low", "enabled", "low"),
        ("medium", "enabled", "low"),
        ("high", "enabled", "high"),
        ("xhigh", "enabled", "high"),
        ("max", "enabled", "max"),
    ],
)
@pytest.mark.asyncio
async def test_deepseek41_preserves_effort_with_its_thinking_toggle(effort, thinking, level):
    resources = Path(__file__).resolve().parents[3] / "resources"
    registry = ModelRegistry.load(resources)
    from core.providers.providers import ProviderRegistry

    config = ProviderRegistry.load(resources).get("opencode-go")

    def lookup(model_id):
        return registry.get("opencode-go", model_id)

    adapter = OpenCodeGoAdapter(config, "test-token", model_lookup=lookup)
    try:
        payload = adapter._build_payload(
            [{"role": "user", "content": "test"}],
            "deepseek-flash",
            thinking_effort=effort,
        )
        assert payload["thinking"] == {"type": thinking}
        assert payload.get("reasoning_effort") == level
        assert adapter._model_protocol("deepseek-flash") == "openai"
        assert adapter.reasoning_replay_policy("deepseek-flash") == "full_history"
        assert adapter.reasoning_replay_fidelity("deepseek-flash") == "readable_only"
        intent = adapter.describe_reasoning_render(
            model_lookup=lookup,
            model_id="deepseek-flash",
            effort=effort,
        )
        assert intent.kind == (
            "default" if effort is None else "off" if effort == "none" else "effort"
        )
        assert intent.effort_level == level
    finally:
        await adapter.aclose()


def test_public_package_exports_opencode_go_adapter() -> None:
    from core.providers import OpenCodeGoAdapter as PublicOpenCodeGoAdapter

    assert PublicOpenCodeGoAdapter is OpenCodeGoAdapter


@respx.mock
@pytest.mark.asyncio
async def test_title_service_sends_opencode_session_header(opencode_go_adapter) -> None:
    route = respx.post(OPENCODE_GO_RESPONSES_URL).mock(
        return_value=httpx.Response(200, json=RESPONSES_COMPLETED_RESPONSE)
    )
    runtime = SimpleNamespace(
        get_adapter=lambda connection: opencode_go_adapter,
        models=SimpleNamespace(get=lambda *args: None),
        chat_sessions=SimpleNamespace(set_auto_title=Mock()),
    )
    service = SessionTitleService(runtime)
    await service._generate_title(
        agent_id="builder",
        session_id="title-session",
        project_id="project",
        model="opencode-go/muse-spark-1.3-contributor::api-key",
        title_input="Review a code change",
        run_id="title-test",
    )
    assert route.call_count == 1
    expected = opencode_go_adapter.request_context_kwargs(
        agent_id="builder", session_id="title-session", project_id="project"
    )[OPENCODE_SESSION_ID_KWARG]
    request = route.calls.last.request
    assert request.headers[OPENCODE_SESSION_HEADER] == expected
    assert request.headers["user-agent"] == "vBot"
    assert OPENCODE_SESSION_ID_KWARG not in json.loads(request.content)


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["summary_tail", "continuation"])
async def test_compaction_sends_opencode_session_header(opencode_go_adapter, strategy) -> None:
    from core.chat import ChatMessage
    from core.compaction import CompactionService, CompactionSettings
    from core.sessions import SessionAddress
    from tests.core.compaction.test_compaction import StubStorage, provider_request

    event = {"choices": [{"index": 0, "delta": {"content": "SUMMARY"}, "finish_reason": "stop"}]}
    route = respx.post(OPENCODE_GO_URL).mock(
        return_value=httpx.Response(
            200,
            text=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )
    )
    messages = [
        ChatMessage.user("old task " * 200),
        ChatMessage.assistant(model="opencode-go/deepseek-flash", content="old answer " * 200),
        ChatMessage.user("recent task"),
        ChatMessage.assistant(model="opencode-go/deepseek-flash", content="recent answer"),
    ]
    checkpoint = await CompactionService().compact(
        messages,
        session_address=SessionAddress(project_id="project", agent_id="joel", session_id="child"),
        prompt_cache_affinity_id="compaction-affinity",
        summary_adapter=opencode_go_adapter,
        summary_model_id="deepseek-flash",
        active_adapter=opencode_go_adapter,
        active_model_id="deepseek-flash",
        storage=StubStorage(),
        settings=CompactionSettings(strategy=strategy, tail_tokens=50),
        request_messages=provider_request(messages),
    )
    assert checkpoint.role == "compaction_checkpoint"
    assert route.call_count == 1
    request = route.calls.last.request
    assert request.headers[OPENCODE_SESSION_HEADER] == "vbot-compaction-affinity"
    assert request.headers["user-agent"] == "vBot"
    payload = json.loads(request.content)
    assert OPENCODE_SESSION_ID_KWARG not in payload
    assert all(key not in payload for key in ("agent_id", "session_id", "project_id"))


class TestOpenCodeGoAdapter:
    def test_request_context_uses_opaque_prompt_cache_affinity(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        source = opencode_go_adapter.request_context_kwargs(
            project_id="vbot",
            agent_id="builder",
            session_id="source",
            prompt_cache_affinity_id="shared-lineage",
        )
        fork = opencode_go_adapter.request_context_kwargs(
            project_id="vbot",
            agent_id="builder",
            session_id="fork",
            prompt_cache_affinity_id="shared-lineage",
        )
        fallback = opencode_go_adapter.request_context_kwargs(
            project_id="vbot",
            agent_id="builder",
            session_id="source",
        )

        assert source == {OPENCODE_SESSION_ID_KWARG: "vbot-shared-lineage"}
        assert fork == source
        assert fallback == opencode_go_adapter.request_context_kwargs(
            project_id="vbot",
            agent_id="builder",
            session_id="source",
        )
        assert fallback[OPENCODE_SESSION_ID_KWARG].startswith("vbot-")
        assert "source" not in fallback[OPENCODE_SESSION_ID_KWARG]

    @pytest.mark.parametrize(
        ("model_id", "url", "response_kind"),
        [
            ("deepseek-v4-flash", OPENCODE_GO_URL, "openai"),
            ("minimax-m3", OPENCODE_GO_MESSAGES_URL, "anthropic"),
            ("gpt-5.6-luna", OPENCODE_GO_RESPONSES_URL, "responses"),
        ],
    )
    @respx.mock
    @pytest.mark.asyncio
    async def test_session_header_reaches_every_non_streaming_wire(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
        model_id: str,
        url: str,
        response_kind: str,
    ) -> None:
        if response_kind == "openai":
            response = httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        elif response_kind == "anthropic":
            response = httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                },
            )
        else:
            response = httpx.Response(200, json=RESPONSES_COMPLETED_RESPONSE)
        route = respx.post(url).mock(return_value=response)
        request_context = opencode_go_adapter.request_context_kwargs(
            project_id="vbot",
            agent_id="builder",
            session_id="session",
            prompt_cache_affinity_id="cache-affinity",
        )

        await opencode_go_adapter.send(
            [{"role": "user", "content": "hello"}],
            model_id=model_id,
            **request_context,
        )

        request = route.calls.last.request
        assert request.headers[OPENCODE_SESSION_HEADER] == "vbot-cache-affinity"
        assert request.headers["user-agent"] == "vBot"
        assert OPENCODE_SESSION_ID_KWARG not in json.loads(request.content)

    @pytest.mark.parametrize(
        ("model_id", "url", "response_kind"),
        [
            ("deepseek-v4-flash", OPENCODE_GO_URL, "openai"),
            ("minimax-m3", OPENCODE_GO_MESSAGES_URL, "anthropic"),
            ("gpt-5.6-luna", OPENCODE_GO_RESPONSES_URL, "responses"),
        ],
    )
    @respx.mock
    @pytest.mark.asyncio
    async def test_session_header_reaches_every_streaming_wire(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
        model_id: str,
        url: str,
        response_kind: str,
    ) -> None:
        if response_kind == "openai":
            response_text = "data: [DONE]\n\n"
        elif response_kind == "anthropic":
            response_text = 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        else:
            completed = {
                "id": "resp_stream",
                "object": "response",
                "status": "completed",
                "output": RESPONSES_COMPLETED_RESPONSE["output"],
                "usage": RESPONSES_COMPLETED_RESPONSE["usage"],
            }
            response_text = (
                'event: response.output_text.delta\ndata: {"delta":"Done"}\n\n'
                f"event: response.completed\ndata: {json.dumps({'response': completed})}\n\n"
            )
        route = respx.post(url).mock(
            return_value=httpx.Response(
                200,
                text=response_text,
                headers={"content-type": "text/event-stream"},
            )
        )
        request_context = opencode_go_adapter.request_context_kwargs(
            project_id="vbot",
            agent_id="builder",
            session_id="session",
            prompt_cache_affinity_id="cache-affinity",
        )

        _ = [
            delta
            async for delta in opencode_go_adapter.stream(
                [{"role": "user", "content": "hello"}],
                model_id=model_id,
                **request_context,
            )
        ]

        request = route.calls.last.request
        assert request.headers[OPENCODE_SESSION_HEADER] == "vbot-cache-affinity"
        assert request.headers["user-agent"] == "vBot"
        assert OPENCODE_SESSION_ID_KWARG not in json.loads(request.content)

    @pytest.mark.parametrize(
        "model_id",
        [
            *ANTHROPIC_MESSAGES_MODELS,
            "deepseek-v4-flash",
            "deepseek-v4-flash-vision-exp",
            "deepseek/deepseek-v4-flash",
            "glm-5.3-flash",
            "kimi-k3",
            "longcat-2.0",
            "mimo-v2.5",
        ],
    )
    def test_reasoning_replay_policy_is_full_history_on_both_routes(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
        model_id: str,
    ) -> None:
        assert opencode_go_adapter.reasoning_replay_policy(model_id) == "full_history"

    def test_unknown_model_reasoning_replay_is_full_history(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        assert opencode_go_adapter.reasoning_replay_policy("new-unprofiled-model") == "full_history"

    @pytest.mark.parametrize("model_id", ["kimi-k2.5", "kimi-k2.6", "kimi-k2.7-code"])
    def test_kimi_models_inherit_full_history_replay(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
        model_id: str,
    ) -> None:
        assert opencode_go_adapter.reasoning_replay_policy(model_id) == "full_history"

    @pytest.mark.parametrize("model_id", ["glm-5.2", "glm-5.3"])
    def test_glm_reasoning_is_replayed_byte_for_byte_in_request_payload(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
        model_id: str,
    ) -> None:
        reasoning = "EXACT old Reasoning: äöü\nline two\n"

        payload = opencode_go_adapter._build_payload(
            [
                {"role": "user", "content": "First turn"},
                {
                    "role": "assistant",
                    "content": "First answer",
                    "reasoning": reasoning,
                },
                {"role": "user", "content": "Second turn"},
            ],
            model_id,
        )

        assert opencode_go_adapter.reasoning_replay_policy(model_id) == "full_history"
        assistant_message = payload["messages"][1]
        assert assistant_message["reasoning_content"] == reasoning
        assert "<reasoning_history>" not in (assistant_message.get("content") or "")

    def test_kimi_k2_6_enables_full_history_rendering(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        payload = opencode_go_adapter._build_payload(
            [{"role": "user", "content": "Continue"}],
            "kimi-k2.6",
        )

        assert payload["thinking"] == {"type": "enabled", "keep": "all"}
        assert "reasoning_effort" not in payload

    def test_kimi_k2_6_respects_explicit_thinking_off(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        payload = opencode_go_adapter._build_payload(
            [{"role": "user", "content": "Continue"}],
            "kimi-k2.6",
            thinking_effort="none",
        )

        assert payload["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in payload

    def test_kimi_k2_7_never_sends_unsupported_reasoning_effort_or_disables_thinking(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        payload = opencode_go_adapter._build_payload(
            [{"role": "user", "content": "Continue"}],
            "kimi-k2.7-code",
            thinking_effort="none",
        )

        assert payload["thinking"] == {"type": "enabled"}
        assert "reasoning_effort" not in payload

    @pytest.mark.parametrize("model_id", ["grok-4.5", "kimi-k3"])
    def test_always_reasoning_effort_models_map_none_to_lowest_supported_level(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
        model_id: str,
    ) -> None:
        payload = opencode_go_adapter._build_payload(
            [{"role": "user", "content": "Continue"}],
            model_id,
            thinking_effort="none",
        )

        assert payload["reasoning_effort"] == "low"

    def test_chat_route_declares_readable_only_reasoning_replay(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        assert opencode_go_adapter.reasoning_replay_fidelity("kimi-k3") == "readable_only"

    def test_kimi_k3_response_and_history_use_independent_reasoning_fields(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        normalized = opencode_go_adapter.normalize_response(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Answer",
                            "reasoning": "Readable trace",
                            "reasoning_details": [{"type": "reasoning.text", "text": "meta"}],
                        }
                    }
                ]
            },
            model_id="kimi-k3",
        )

        payload = opencode_go_adapter._build_payload([normalized], "kimi-k3")
        assistant = payload["messages"][0]

        assert normalized["reasoning"] == "Readable trace"
        assert normalized["reasoning_meta"] == {
            "reasoning_details": [{"type": "reasoning.text", "text": "meta"}]
        }
        assert assistant["reasoning_content"] == "Readable trace"
        assert "reasoning" not in assistant
        assert "reasoning_details" not in assistant

    def test_format_assistant_message_adds_reasoning_content(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        internal_message = {
            "role": "assistant",
            "content": "Answer",
            "reasoning": "I think...",
            "tool_calls": None,
            "reasoning_meta": None,
        }

        wire = opencode_go_adapter._format_assistant_message(
            internal_message,
            model_id="kimi-k3",
        )

        assert wire["reasoning_content"] == "I think..."
        assert wire["content"] == "Answer"
        assert "reasoning" not in wire

    def test_format_assistant_message_skips_reasoning_content_when_reasoning_is_none(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        internal_message = {
            "role": "assistant",
            "content": "Hi",
            "reasoning": None,
            "tool_calls": None,
            "reasoning_meta": None,
        }

        wire = opencode_go_adapter._format_assistant_message(
            internal_message,
            model_id="kimi-k3",
        )

        assert "reasoning_content" not in wire

    def test_format_assistant_message_skips_reasoning_content_when_reasoning_is_empty(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        internal_message = {
            "role": "assistant",
            "content": "Hi",
            "reasoning": "",
            "tool_calls": None,
            "reasoning_meta": None,
        }

        wire = opencode_go_adapter._format_assistant_message(
            internal_message,
            model_id="kimi-k3",
        )

        assert "reasoning_content" not in wire

    @respx.mock
    @pytest.mark.asyncio
    async def test_round_trip_tool_loop_payload_includes_reasoning_content(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        reasoning_text = "Need to call tool first"
        route = respx.post(OPENCODE_GO_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "reasoning_content": reasoning_text,
                                "tool_calls": [
                                    {
                                        "id": "call_weather",
                                        "type": "function",
                                        "function": {
                                            "name": "get_weather",
                                            "arguments": '{"city":"Berlin"}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            )
        )

        first_response = await opencode_go_adapter.send(
            [{"role": "user", "content": "Weather in Berlin?"}],
            model_id="deepseek/deepseek-v4-flash",
        )
        normalized_assistant = opencode_go_adapter.normalize_response(first_response)

        payload = opencode_go_adapter._build_payload(
            [
                {"role": "user", "content": "Weather in Berlin?"},
                normalized_assistant,
                {
                    "role": "tool",
                    "tool_call_id": "call_weather",
                    "name": "get_weather",
                    "content": json.dumps({"temp": 22}),
                },
            ],
            model_id="deepseek/deepseek-v4-flash",
        )

        assistant_wire = next(msg for msg in payload["messages"] if msg.get("role") == "assistant")
        assert route.called
        assert assistant_wire["reasoning_content"] == reasoning_text

    def test_base_adapter_build_payload_uses_reasoning_content_fallback(
        self,
        opencode_go_config: ProviderConfig,
    ) -> None:
        base_adapter = OpenAICompatibleAdapter(opencode_go_config, API_KEY)

        payload = base_adapter._build_payload(
            [
                {
                    "role": "assistant",
                    "content": "Answer",
                    "reasoning": "I think...",
                    "tool_calls": None,
                    "reasoning_meta": None,
                }
            ],
            model_id="deepseek/deepseek-v4-flash",
        )

        assert payload["messages"][0]["reasoning_content"] == "I think..."

    def test_build_payload_replays_reasoning_for_all_assistants_on_openai_path(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        payload = opencode_go_adapter._build_payload(
            [
                {"role": "user", "content": "First question"},
                {
                    "role": "assistant",
                    "content": "First answer",
                    "reasoning": "first reasoning",
                    "reasoning_meta": {"reasoning_details": [{"trace": "first"}]},
                    "tool_calls": None,
                },
                {"role": "user", "content": "Second question"},
                {
                    "role": "assistant",
                    "content": "Second answer",
                    "reasoning": "second reasoning",
                    "reasoning_meta": {"reasoning_details": [{"trace": "second"}]},
                    "tool_calls": None,
                },
            ],
            model_id="deepseek/deepseek-v4-flash",
        )

        assistant_messages = [
            message for message in payload["messages"] if message.get("role") == "assistant"
        ]
        assert len(assistant_messages) == 2
        assert assistant_messages[0]["reasoning_content"] == "first reasoning"
        assert "reasoning_details" not in assistant_messages[0]
        assert assistant_messages[1]["reasoning_content"] == "second reasoning"
        assert "reasoning_details" not in assistant_messages[1]

    def test_deepseek_none_thinking_effort_omits_reasoning_effort(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        payload = opencode_go_adapter._build_payload(
            [{"role": "user", "content": "Reply OK."}],
            model_id="deepseek-v4-flash",
            thinking_effort="none",
        )

        assert "reasoning_effort" not in payload

    def test_build_payload_uses_catalog_output_limit_over_provider_default(
        self,
        opencode_go_config: ProviderConfig,
    ) -> None:
        config = replace(opencode_go_config, defaults={"max_tokens": 4096})
        catalog_model = model_with_output_limit("deepseek-v4-flash", 384_000)
        adapter = OpenCodeGoAdapter(
            config,
            API_KEY,
            model_lookup=lambda model_id: (
                catalog_model if model_id == catalog_model.model_id else None
            ),
        )

        payload = adapter._build_payload(
            [{"role": "user", "content": "Write a complete HTML app."}],
            model_id="deepseek-v4-flash",
        )

        assert payload["max_tokens"] == 384_000

    def test_build_payload_uses_catalog_output_limit_for_vendor_prefixed_model_id(
        self,
        opencode_go_config: ProviderConfig,
    ) -> None:
        config = replace(opencode_go_config, defaults={"max_tokens": 4096})
        catalog_model = model_with_output_limit("deepseek-v4-flash", 384_000)
        adapter = OpenCodeGoAdapter(
            config,
            API_KEY,
            model_lookup=lambda model_id: (
                catalog_model if model_id == catalog_model.model_id else None
            ),
        )

        payload = adapter._build_payload(
            [{"role": "user", "content": "Write a complete HTML app."}],
            model_id="deepseek/deepseek-v4-flash",
        )

        assert payload["max_tokens"] == 384_000

    def test_build_payload_preserves_explicit_output_limit(
        self,
        opencode_go_config: ProviderConfig,
    ) -> None:
        config = replace(opencode_go_config, defaults={"max_tokens": 4096})
        catalog_model = model_with_output_limit("deepseek-v4-flash", 384_000)
        adapter = OpenCodeGoAdapter(
            config,
            API_KEY,
            model_lookup=lambda model_id: (
                catalog_model if model_id == catalog_model.model_id else None
            ),
        )

        payload = adapter._build_payload(
            [{"role": "user", "content": "Write a short file."}],
            model_id="deepseek-v4-flash",
            max_tokens=2048,
        )

        assert payload["max_tokens"] == 2048

    def test_build_payload_clamps_equal_context_and_output_catalog_limits(
        self,
        opencode_go_config: ProviderConfig,
    ) -> None:
        catalog_model = model_with_output_limit(
            "deepseek-v4-flash",
            384_000,
            context_window=384_000,
        )
        adapter = OpenCodeGoAdapter(
            opencode_go_config,
            API_KEY,
            model_lookup=lambda model_id: (
                catalog_model if model_id == catalog_model.model_id else None
            ),
        )

        payload = adapter._build_payload(
            [{"role": "user", "content": "x" * 8_000}],
            model_id="deepseek-v4-flash",
        )

        assert 0 < payload["max_tokens"] < 384_000
