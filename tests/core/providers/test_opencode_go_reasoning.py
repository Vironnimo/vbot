"""OpenCode Go Adapter: per-Model Reasoning controls and Reasoning replay on each wire."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from core.chat import ChatMessage
from core.providers.reasoning import (
    DEFAULT_REASONING_REPLAY_FIDELITY,
    REASONING_REPLAY_FIDELITY_READABLE_ONLY,
    REASONING_REPLAY_FULL_HISTORY,
)
from tests.core.chat.assistant_turn_test_support import request_history

from .opencode_go_test_support import (
    CHAT_MODEL,
    CHAT_URL,
    MESSAGES_MODEL,
    MESSAGES_URL,
    RESPONSES_MODEL,
    RESPONSES_URL,
    bundled_go,
    go_adapter,
    go_request,
    profile_lookup,
    success_response,
)

EFFORTS = [None, "none", "minimal", "low", "medium", "high", "xhigh", "max"]


def _sent_payload(route: respx.Route) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(route.calls.last.request.content)
    return payload


# ---------------------------------------------------------------------------
# Reasoning controls
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_id", "effort", "thinking", "wire_effort", "intent"),
    [
        # DeepSeek V4.1 Flash and its latest-Flash alias: a toggle independent of the ladder.
        ("deepseek-v4.1-flash", None, {"type": "enabled"}, None, ("default", None)),
        ("deepseek-v4.1-flash", "none", {"type": "disabled"}, None, ("off", None)),
        ("deepseek-v4.1-flash", "medium", {"type": "enabled"}, "low", ("effort", "low")),
        ("deepseek-v4.1-flash", "max", {"type": "enabled"}, "max", ("effort", "max")),
        ("deepseek-flash", "high", {"type": "enabled"}, "high", ("effort", "high")),
        ("deepseek-flash", "none", {"type": "disabled"}, None, ("off", None)),
        # MiMo V2.6: a binary toggle without an effort ladder.
        ("mimo-v2.6-flash", None, {"type": "enabled"}, None, ("on", None)),
        ("mimo-v2.6-flash", "none", {"type": "disabled"}, None, ("off", None)),
        ("mimo-v2.6-pro", "high", {"type": "enabled"}, None, ("on", None)),
        # Space Bunny: mandatory Reasoning maps an off request to its cheapest rung.
        ("space-bunny-free", None, None, None, ("default", None)),
        ("space-bunny-free", "none", None, "low", ("effort", "low")),
        ("space-bunny-free", "high", None, "high", ("effort", "high")),
    ],
)
@pytest.mark.asyncio
async def test_bundled_chat_models_render_their_reasoning_controls(
    model_id: str,
    effort: str | None,
    thinking: dict[str, str] | None,
    wire_effort: str | None,
    intent: tuple[str, str | None],
) -> None:
    adapter, lookup, config = bundled_go()

    with respx.mock:
        route = respx.post(f"{config.base_url}/chat/completions").mock(
            return_value=success_response("chat", streaming=False)
        )
        await adapter.send(
            [{"role": "user", "content": "test"}], model_id=model_id, thinking_effort=effort
        )
    await adapter.aclose()

    payload = _sent_payload(route)
    assert payload["model"] == model_id
    assert payload.get("thinking") == thinking
    assert payload.get("reasoning_effort") == wire_effort
    description = adapter.describe_reasoning_render(
        model_lookup=lookup, model_id=model_id, effort=effort, provider_config=config
    )
    assert (description.kind, description.effort_level) == intent
    assert adapter.reasoning_replay_policy(model_id) == REASONING_REPLAY_FULL_HISTORY
    assert adapter.reasoning_replay_fidelity(model_id) == REASONING_REPLAY_FIDELITY_READABLE_ONLY


@pytest.mark.parametrize(
    ("model_id", "effort", "thinking", "intent_kind"),
    [
        pytest.param(
            "kimi-k2.6", None, {"type": "enabled", "keep": "all"}, "on", id="toggle-keeps-history"
        ),
        pytest.param("kimi-k2.6", "none", {"type": "disabled"}, "off", id="toggle-off"),
        pytest.param(
            "kimi-k2.7-code", "none", {"type": "enabled"}, "on", id="always-enabled-ignores-off"
        ),
        pytest.param(CHAT_MODEL, "none", None, "off", id="no-control-profile"),
    ],
)
@pytest.mark.asyncio
async def test_profile_thinking_controls_replace_the_effort_ladder(
    model_id: str, effort: str | None, thinking: dict[str, str] | None, intent_kind: str
) -> None:
    adapter = go_adapter()

    with respx.mock:
        route = respx.post(CHAT_URL).mock(return_value=success_response("chat", streaming=False))
        await adapter.send(
            [{"role": "user", "content": "Continue"}], model_id=model_id, thinking_effort=effort
        )

    payload = _sent_payload(route)
    assert payload.get("thinking") == thinking
    assert "reasoning_effort" not in payload
    description = adapter.describe_reasoning_render(
        model_lookup=profile_lookup, model_id=model_id, effort=effort
    )
    assert description.kind == intent_kind


@pytest.mark.parametrize(
    ("model_id", "effort", "reasoning"),
    [
        pytest.param(RESPONSES_MODEL, "none", {"effort": "none", "summary": "auto"}, id="off-rung"),
        pytest.param("muse-spark-1.3-contributor", "none", None, id="no-off-rung-omits"),
        pytest.param("grok-4.5", "none", {"effort": "low", "summary": "auto"}, id="minimum-rung"),
    ],
)
@pytest.mark.asyncio
async def test_responses_wire_never_sends_an_unsupported_off_effort(
    model_id: str, effort: str, reasoning: dict[str, str] | None
) -> None:
    """The Responses wire rejects an explicit ``none`` rung unless the Model lists it."""
    with respx.mock:
        route = respx.post(RESPONSES_URL).mock(
            return_value=success_response("responses", streaming=False)
        )
        await go_adapter().send(
            [{"role": "user", "content": "hello"}], model_id=model_id, thinking_effort=effort
        )

    assert _sent_payload(route).get("reasoning") == reasoning


@pytest.mark.parametrize("effort", EFFORTS)
@pytest.mark.asyncio
async def test_provider_default_messages_profile_preserves_native_history(
    effort: str | None,
) -> None:
    """Union Alpha's retired profile stays a test-owned Messages ``provider_default`` fixture."""
    adapter = go_adapter()
    scope = "opencode-go/union-alpha::api-key"
    # Native state protects the wire contract independently of a live catalog.
    native = {"type": "thinking", "thinking": "test-native-state", "signature": "test-signature"}
    history = [
        ChatMessage.user("First"),
        ChatMessage.assistant(
            model="opencode-go/union-alpha",
            content="Prior answer",
            reasoning="test-native-state",
            reasoning_meta={"content_blocks": [native]},
            reasoning_scope=scope,
        ),
        ChatMessage.user("Next"),
    ]
    persisted = [ChatMessage.from_dict(message.to_dict()) for message in history]
    messages = request_history(
        persisted, replay_policy=adapter.reasoning_replay_policy("union-alpha"), agent_model=scope
    )
    reply = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "Next answer"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 49, "output_tokens": 66, "cache_read_input_tokens": 0},
    }

    with respx.mock:
        route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=reply))
        raw = await adapter.send(
            messages,
            model_id="union-alpha",
            thinking_effort=effort,
            **adapter.request_context_kwargs(agent_id="audit", session_id="test-session"),
        )

    payload = _sent_payload(route)
    assert payload["model"] == "union-alpha"
    assert not (
        {"thinking", "output_config", "reasoning_effort", "thinking_effort"} & payload.keys()
    )
    assert payload["messages"][1]["content"][0] == native
    assert "reasoning" not in payload["messages"][1]
    assert adapter.reasoning_replay_policy("union-alpha") == REASONING_REPLAY_FULL_HISTORY
    description = adapter.describe_reasoning_render(
        model_lookup=profile_lookup, model_id="union-alpha", effort=effort
    )
    assert description.kind == "default"
    normalized = adapter.normalize_response(raw, model_id="union-alpha")
    assert normalized["terminal_outcome"] == "stop"
    assert not normalized.get("reasoning")
    assert not normalized.get("reasoning_meta")
    assert normalized["usage"]["input_tokens"] == 49
    assert normalized["usage"]["output_tokens"] == 66


# ---------------------------------------------------------------------------
# Reasoning replay
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_id", "fidelity"),
    [
        pytest.param("kimi-k3", REASONING_REPLAY_FIDELITY_READABLE_ONLY, id="chat"),
        pytest.param("unprofiled-model", REASONING_REPLAY_FIDELITY_READABLE_ONLY, id="unknown"),
        pytest.param(MESSAGES_MODEL, DEFAULT_REASONING_REPLAY_FIDELITY, id="messages"),
        pytest.param(RESPONSES_MODEL, DEFAULT_REASONING_REPLAY_FIDELITY, id="responses"),
    ],
)
def test_every_wire_replays_full_history_with_its_own_fidelity(
    model_id: str, fidelity: str
) -> None:
    adapter = go_adapter()

    assert adapter.reasoning_replay_policy(model_id) == REASONING_REPLAY_FULL_HISTORY
    assert adapter.reasoning_replay_fidelity(model_id) == fidelity


@pytest.mark.asyncio
async def test_chat_wire_replays_only_readable_reasoning_for_every_assistant() -> None:
    reasoning = "EXACT old Reasoning: äöü\nline two\n"
    history: list[dict[str, Any]] = [{"role": "user", "content": "First"}]
    # A Tool Call without Reasoning still carries an empty reasoning_content:
    # DeepSeek's thinking mode rejects the request otherwise (Sessions, 2026-09).
    for text, calls in (
        (reasoning, False),
        ("second reasoning", False),
        (None, False),
        ("", False),
        ("tool reasoning", True),
        (None, True),
        ("", True),
    ):
        call = {"id": f"call_{len(history)}", "name": "read", "arguments": {"path": "a.txt"}}
        history += [
            {
                "role": "assistant",
                "content": None if calls else "Answer",
                "reasoning": text,
                "reasoning_meta": {"reasoning_details": [{"trace": "opaque"}]} if text else None,
                "tool_calls": [call] if calls else None,
            },
            {"role": "tool", "tool_call_id": call["id"], "content": "text"}
            if calls
            else {"role": "user", "content": "Next"},
        ]

    with respx.mock:
        route = respx.post(CHAT_URL).mock(return_value=success_response("chat", streaming=False))
        await go_adapter().send(history, model_id="deepseek-v4.1-flash")

    assistants = [m for m in _sent_payload(route)["messages"] if m["role"] == "assistant"]
    assert [m.get("reasoning_content") for m in assistants] == [
        reasoning,
        "second reasoning",
        None,
        None,
        "tool reasoning",
        "",
        "",
    ]
    assert all("reasoning_details" not in m and "reasoning" not in m for m in assistants)
    assert all("<reasoning_history>" not in (m.get("content") or "") for m in assistants)


@pytest.mark.parametrize(
    ("model_id", "reply_message", "reasoning_meta"),
    [
        pytest.param(
            "kimi-k3",
            {
                "role": "assistant",
                "content": "Answer",
                "reasoning": "Readable trace",
                "reasoning_details": [{"type": "reasoning.text", "text": "meta"}],
            },
            {"reasoning_details": [{"type": "reasoning.text", "text": "meta"}]},
            id="profiled-reasoning-carrier",
        ),
        pytest.param(
            "deepseek/deepseek-v4-flash",
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "Readable trace",
                "tool_calls": [
                    {
                        "id": "call_weather",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"Berlin"}'},
                    }
                ],
            },
            None,
            id="default-carrier-with-tool-call",
        ),
    ],
)
@pytest.mark.asyncio
async def test_chat_reasoning_round_trips_from_its_response_carrier_to_reasoning_content(
    model_id: str, reply_message: dict[str, Any], reasoning_meta: dict[str, Any] | None
) -> None:
    """Kimi K3 answers with ``reasoning``; every Chat Model replays ``reasoning_content``."""
    adapter = go_adapter()
    question = {"role": "user", "content": "Weather in Berlin?"}

    with respx.mock:
        route = respx.post(CHAT_URL).mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": reply_message, "finish_reason": "stop"}]}
            )
        )
        raw = await adapter.send([question], model_id=model_id)
        normalized = adapter.normalize_response(raw, model_id=model_id)
        follow_up: list[dict[str, Any]] = [question, normalized]
        if normalized.get("tool_calls"):
            follow_up.append(
                {"role": "tool", "tool_call_id": "call_weather", "content": '{"temp": 22}'}
            )
        await adapter.send(follow_up, model_id=model_id)

    assert normalized["reasoning"] == "Readable trace"
    assert normalized.get("reasoning_meta") == reasoning_meta
    assistant = next(m for m in _sent_payload(route)["messages"] if m["role"] == "assistant")
    assert assistant["reasoning_content"] == "Readable trace"
    assert "reasoning" not in assistant
    assert "reasoning_details" not in assistant


@pytest.mark.parametrize("streaming", [False, True], ids=["send", "stream"])
@pytest.mark.asyncio
async def test_messages_wire_replays_signed_thinking_for_every_assistant(streaming: bool) -> None:
    """Every earlier Assistant keeps its own signed block; none is synthesized for the others."""

    def assistant(call_id: str, signature: str | None) -> list[dict[str, Any]]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": f"Assistant {call_id}",
            "tool_calls": [{"id": call_id, "name": "probe", "arguments": {}}],
        }
        if signature is not None:
            block = {"type": "thinking", "thinking": f"{call_id} thinking", "signature": signature}
            message.update(reasoning=block["thinking"], reasoning_meta={"content_blocks": [block]})
        result = {
            "role": "tool",
            "tool_call_id": call_id,
            "name": "probe",
            "content": '{"ok":true}',
        }
        return [message, result]

    history = [
        {"role": "user", "content": "First"},
        *assistant("call_old", "sig-old"),
        *assistant("call_mid", "sig-mid"),
        *assistant("call_latest", None),
    ]

    with respx.mock:
        route = respx.post(MESSAGES_URL).mock(
            return_value=success_response("messages", streaming=streaming)
        )
        await go_request(go_adapter(), "minimax-m2.7", streaming=streaming, messages=history)

    assistants = [m for m in _sent_payload(route)["messages"] if m["role"] == "assistant"]
    thinking = [[b for b in m["content"] if b.get("type") == "thinking"] for m in assistants]
    assert thinking == [
        [{"type": "thinking", "thinking": "call_old thinking", "signature": "sig-old"}],
        [{"type": "thinking", "thinking": "call_mid thinking", "signature": "sig-mid"}],
        [],
    ]
    assert any(block.get("type") == "tool_use" for block in assistants[-1]["content"])
