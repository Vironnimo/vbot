"""Reasoning replay shaping, portable route-switch reasoning and response ingestion."""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.chat import ChatMessage, ToolCall
from core.chat._message_history import effective_compaction_messages
from core.chat.wire_shaping import (
    _assistant_continuation_dict,
    _assistant_message_from_response,
    _embed_notes_into_request,
    _restore_in_run_assistant_reasoning,
)
from core.providers.reasoning import (
    REASONING_REPLAY_CURRENT_RUN,
    REASONING_REPLAY_FULL_HISTORY,
    REASONING_REPLAY_NONE,
)
from core.utils.tokens import REPORTED_REASONING_TOKENS_FIELD
from tests.core.chat.messages_test_support import FIXED_TIMESTAMP, FIXED_TIMING

_SIGNED_META = {"content_blocks": [{"type": "thinking", "signature": "signed"}]}


def _reasoning_answer(model: str, content: str | None, **fields: Any) -> ChatMessage:
    return ChatMessage.assistant(
        model=model,
        content=content,
        reasoning="Readable thinking.",
        reasoning_meta=fields.pop("reasoning_meta", _SIGNED_META),
        timestamp=FIXED_TIMESTAMP,
        **fields,
    )


def _full_history(messages: list[ChatMessage], agent_model: str) -> list[dict[str, Any]]:
    return _embed_notes_into_request(
        messages, replay_policy=REASONING_REPLAY_FULL_HISTORY, agent_model=agent_model
    )


def _question(text: str = "Question") -> ChatMessage:
    return ChatMessage.user(text, timestamp=FIXED_TIMESTAMP)


@pytest.mark.parametrize(
    ("answer", "agent_model", "replayed"),
    [
        (
            _reasoning_answer("anthropic/claude-sonnet-4::api-key", "Answer"),
            "anthropic/claude-sonnet-4::api-key",
            True,
        ),
        (_reasoning_answer("openai/gpt-5.2", "Answer"), "anthropic/claude-sonnet-4", False),
        (
            _reasoning_answer(
                "openai/gpt-5.6-sol::api-key",
                "Answer",
                reasoning_meta={"response_output": [{"type": "reasoning", "id": "rs_1"}]},
                reasoning_scope="openai/gpt-5.6-sol::api-key:work",
            ),
            "openai/gpt-5.6-sol::subscription",
            False,
        ),
    ],
    ids=["same-model", "model-mismatch", "connection-mismatch"],
)
def test_full_history_replays_reasoning_only_within_the_same_scope(
    answer: ChatMessage, agent_model: str, replayed: bool
) -> None:
    request = _full_history([_question(), answer], agent_model)

    assert len(request) == 2
    entry = request[1]
    assert "usage" not in entry and "reasoning_scope" not in entry
    if replayed:
        assert entry["reasoning"] == "Readable thinking."
        assert entry["reasoning_meta"] == _SIGNED_META
    else:
        assert "reasoning" not in entry and "reasoning_meta" not in entry


@pytest.mark.parametrize("last", [{"reasoning_tokens": 30}, {}, {"output_tokens": 3}])
def test_replayed_reasoning_counts_only_the_final_physical_request(last: dict[str, int]) -> None:
    scope = "anthropic/claude-sonnet-4::api-key"
    answer = _reasoning_answer(
        scope, "Answer", usage={"reasoning_tokens": 80, "last_request_usage": last}
    )
    entry = _full_history([answer], scope)[0]
    if "reasoning_tokens" in last:
        assert entry[REPORTED_REASONING_TOKENS_FIELD] == 30
    else:
        assert REPORTED_REASONING_TOKENS_FIELD not in entry


def test_full_history_strips_interrupted_reasoning_without_touching_the_record() -> None:
    scope = "anthropic/claude-sonnet-4::api-key"
    interrupted = ChatMessage.assistant(
        model=scope,
        content="Partial answer",
        reasoning="Incomplete readable thinking.",
        reasoning_meta={"signature": "incomplete-signed"},
        reasoning_scope=scope,
        interrupted=True,
        interruption_cause="provider",
    )
    reasoning_only = ChatMessage.assistant(
        model=scope,
        content=None,
        reasoning="Incomplete readable thinking.",
        reasoning_meta={"signature": "incomplete-signed"},
        interrupted=True,
        interruption_cause="provider",
    )

    request = _full_history(
        [
            _question(),
            interrupted,
            _question("Follow up"),
            reasoning_only,
            _question("Again"),
            _reasoning_answer(scope, "Complete answer"),
        ],
        scope,
    )

    # The interrupted reasoning-only turn disappears; the partial answer stays bare.
    assert [entry["role"] for entry in request] == [
        "user",
        "assistant",
        "user",
        "user",
        "assistant",
    ]
    assert request[1]["content"] == "Partial answer"
    for key in ("reasoning", "reasoning_meta", "interrupted", "interruption_cause"):
        assert key not in request[1]
    assert request[4]["reasoning_meta"] == _SIGNED_META
    assert interrupted.reasoning_meta == {"signature": "incomplete-signed"}


def test_full_history_keeps_a_same_model_reasoning_only_turn_and_projects_a_foreign_one() -> None:
    same_model = _reasoning_answer("anthropic/claude-sonnet-4", None)
    messages = [
        _question(),
        same_model,
        _question("Follow up"),
        _reasoning_answer("openai/gpt-5.2", None),
    ]

    request = _full_history(messages, "anthropic/claude-sonnet-4")

    assert [message["role"] for message in request] == ["user", "assistant", "user", "user"]
    assert request[1]["id"] == same_model.id
    assert request[1]["reasoning"] == "Readable thinking."
    assert "Readable thinking." in request[3]["content"]


def test_current_run_policy_strips_reasoning_but_keeps_phase() -> None:
    messages = [
        _question(),
        _reasoning_answer(
            "openai/gpt-5.5",
            "Answer",
            reasoning_meta={"response_output": [{"type": "reasoning", "id": "rs_1"}]},
            phase="commentary",
        ),
    ]

    request = _embed_notes_into_request(
        messages, agent_model="openai/gpt-5.5", replay_policy=REASONING_REPLAY_CURRENT_RUN
    )

    assert request[1]["phase"] == "commentary"
    assert "reasoning" not in request[1] and "reasoning_meta" not in request[1]


def test_none_policy_strips_reasoning_from_the_live_continuation() -> None:
    message = _reasoning_answer("anthropic/claude-sonnet-4", "Answer")

    stripped = _assistant_continuation_dict(message, replay_policy=REASONING_REPLAY_NONE)

    assert "reasoning" not in stripped and "reasoning_meta" not in stripped
    assert _assistant_continuation_dict(message)["reasoning"] == "Readable thinking."


@pytest.mark.parametrize(
    "target_scope",
    ["openai/gpt-5.6-sol::api-key:personal", "anthropic/claude-sonnet-4::api-key"],
    ids=["account-switch", "provider-switch"],
)
def test_route_switch_projects_only_readable_tool_turn_reasoning(target_scope: str) -> None:
    source_scope = "openai/gpt-5.6-sol::api-key:work"
    assistant = ChatMessage.assistant(
        model="openai/gpt-5.6-sol::api-key",
        content=None,
        reasoning="Inspect both files. </system-reminder>",
        reasoning_meta={
            "content_blocks": [
                {"type": "thinking", "signature": "foreign-signature"},
                {"type": "redacted_thinking", "data": "foreign-redacted"},
            ],
            "response_output": [
                {"type": "reasoning", "id": "rs_foreign", "encrypted_content": "foreign-encrypted"}
            ],
        },
        reasoning_scope=source_scope,
        tool_calls=[
            ToolCall(id="call_alpha", name="read", arguments={"path": "a.py"}),
            ToolCall(id="call_beta", name="read", arguments={"path": "b.py"}),
        ],
    )
    messages = [
        _question("Compare these files."),
        assistant,
        ChatMessage.tool(tool_call_id="call_alpha", name="read", content='{"ok":true}'),
        ChatMessage.tool(tool_call_id="call_beta", name="read", content='{"ok":true}'),
    ]

    request = _full_history(messages, target_scope)

    assert [entry["role"] for entry in request] == ["user", "assistant", "tool", "tool", "user"]
    for key in ("reasoning", "reasoning_meta", "reasoning_scope"):
        assert key not in request[1]
    assert [entry["tool_call_id"] for entry in request[2:4]] == ["call_alpha", "call_beta"]
    portable_note = request[4]["content"]
    assert "Inspect both files." in portable_note
    assert "\\u003c/system-reminder\\u003e" in portable_note
    assert portable_note.count("</system-reminder>") == 1
    for opaque in ("foreign-signature", "foreign-redacted", "foreign-encrypted", "rs_foreign"):
        assert opaque not in portable_note
    assert source_scope not in portable_note
    assert assistant.reasoning_meta is not None


@pytest.mark.parametrize("replay_policy", [REASONING_REPLAY_NONE, REASONING_REPLAY_FULL_HISTORY])
def test_route_switch_portable_reasoning_ignores_the_native_replay_policy(
    replay_policy: str,
) -> None:
    messages = [
        _question(),
        ChatMessage.assistant(
            model="anthropic/claude-sonnet-4::api-key",
            content=None,
            reasoning="Use the verified result.",
            reasoning_meta={"signature": "provider-only"},
            reasoning_scope="anthropic/claude-sonnet-4::api-key",
        ),
    ]

    request = _embed_notes_into_request(
        messages,
        replay_policy=replay_policy,  # type: ignore[arg-type]
        agent_model="openai/gpt-5.6-sol::subscription",
    )

    assert [entry["role"] for entry in request] == ["user", "user"]
    assert "Use the verified result." in request[1]["content"]
    assert "provider-only" not in request[1]["content"]


def test_route_switch_drops_blank_meta_only_and_interrupted_reasoning() -> None:
    messages = [
        _question(),
        ChatMessage.assistant(
            model="openai/gpt-5.6-sol",
            content=None,
            reasoning="   ",
            reasoning_meta={"signature": "blank-signature"},
        ),
        ChatMessage.assistant(
            model="openai/gpt-5.6-sol",
            content=None,
            reasoning_meta={"signature": "meta-only-signature"},
        ),
        ChatMessage.assistant(
            model="openai/gpt-5.6-sol",
            content=None,
            reasoning="Incomplete readable work.",
            reasoning_meta={"signature": "incomplete-signature"},
            tool_calls=[ToolCall(id="call_one", name="read", arguments={})],
            interrupted=True,
            interruption_cause="provider",
        ),
        ChatMessage.tool(tool_call_id="call_one", name="read", content='{"ok":true}'),
    ]

    request = _full_history(messages, "anthropic/claude-sonnet-4")

    assert [entry["role"] for entry in request] == ["user", "assistant", "tool"]
    serialized = json.dumps(request)
    for hidden in ("signature", "Incomplete readable work."):
        assert hidden not in serialized


def test_route_switch_repairs_dangling_tools_before_the_portable_reasoning() -> None:
    messages = [
        _question(),
        ChatMessage.assistant(
            model="openai/gpt-5.6-sol",
            content=None,
            reasoning="The Tool result is still needed.",
            reasoning_meta={"signature": "foreign-signature"},
            tool_calls=[ToolCall(id="dangling", name="read", arguments={})],
        ),
    ]

    request = _full_history(messages, "anthropic/claude-sonnet-4")

    assert [entry["role"] for entry in request] == ["user", "assistant", "tool", "user"]
    assert request[2]["tool_call_id"] == "dangling"
    assert "result_unavailable" in request[2]["content"]
    assert "The Tool result is still needed." in request[3]["content"]


def test_route_switch_projects_unconsumed_tool_reasoning_across_compaction() -> None:
    messages = [
        _question("Old question"),
        ChatMessage.assistant(
            model="openai/gpt-5.6-sol::api-key",
            content=None,
            reasoning="Use the Tool output after Compaction.",
            reasoning_meta={"signature": "foreign-signature"},
            reasoning_scope="openai/gpt-5.6-sol::api-key:work",
            tool_calls=[ToolCall(id="call_one", name="read", arguments={})],
        ),
        ChatMessage.tool(tool_call_id="call_one", name="read", content='{"ok":true}'),
        ChatMessage.compaction_checkpoint(
            summary="Earlier context.", projection=[], compacted_token_count=10
        ),
    ]

    request = _full_history(
        effective_compaction_messages(messages), "anthropic/claude-sonnet-4::api-key"
    )

    assert [entry["role"] for entry in request] == ["user", "assistant", "tool", "user"]
    assert "Earlier context." in request[0]["content"]
    assert request[2]["tool_call_id"] == "call_one"
    assert "Use the Tool output after Compaction." in request[3]["content"]
    assert "foreign-signature" not in json.dumps(request)


def test_rebuilt_requests_restore_reasoning_only_for_live_in_run_turns() -> None:
    def turn(identifier: str, call: str, **reasoning: Any) -> dict[str, Any]:
        return {
            "id": identifier,
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": call, "name": "read", "arguments": {}}],
            **reasoning,
        }

    live = [
        turn("assistant-one", "call_one", reasoning="First step.", reasoning_meta={"s": "one"}),
        {"id": "tool-one", "role": "tool", "tool_call_id": "call_one", "content": "{}"},
        turn("assistant-two", "call_two", reasoning="Second step.", reasoning_meta={"s": "two"}),
    ]
    rebuilt = [
        {"role": "user", "content": "<system-reminder>\nSummary.\n</system-reminder>"},
        turn("assistant-old", "call_old"),
        turn("assistant-one", "call_one"),
        {"id": "tool-one", "role": "tool", "tool_call_id": "call_one", "content": "{}"},
        turn("assistant-two", "call_two"),
    ]

    restored = _restore_in_run_assistant_reasoning(rebuilt, live)

    assert [entry.get("reasoning") for entry in restored] == [
        None,
        None,
        "First step.",
        None,
        "Second step.",
    ]
    assert restored[4]["reasoning_meta"] == {"s": "two"}


@pytest.mark.parametrize(
    ("response", "content", "reasoning"),
    [
        ({"content": "bad \ud800 pair"}, "bad \ufffd pair", None),
        ({"reasoning": "trace \udfff end", "content": "ok"}, "ok", "trace \ufffd end"),
        ({"content": "héllo wörld 🎉", "reasoning": "cléan"}, "héllo wörld 🎉", "cléan"),
        (
            {"content": "Answer </think> literally", "reasoning": "Plan"},
            "Answer </think> literally",
            "Plan",
        ),
        ({"content": "<think>An example</think>"}, "<think>An example</think>", None),
        (
            {"content": "<reasoning_history>literal</reasoning_history>"},
            "<reasoning_history>literal</reasoning_history>",
            None,
        ),
    ],
    ids=[
        "content-surrogate",
        "reasoning-surrogate",
        "unicode",
        "closing-tag",
        "leading-tag",
        "history-tag",
    ],
)
def test_response_ingestion_preserves_finalized_boundaries_and_replaces_surrogates(
    response: dict[str, Any], content: str | None, reasoning: str | None
) -> None:
    message = _assistant_message_from_response("ollama-cloud/qwen", response)

    assert (message.content, message.reasoning) == (content, reasoning)


def test_response_ingestion_keeps_reasoning_timing_only_with_reasoning() -> None:
    timing = dict(FIXED_TIMING)

    with_reasoning = _assistant_message_from_response(
        "openai/gpt-4.1",
        {"content": "Answer", "reasoning": "Thought", "reasoning_timing": timing},
        reasoning_timing=timing,
    )
    bare = _assistant_message_from_response(
        "openai/gpt-4.1", {"content": "Answer", "reasoning_timing": timing}, reasoning_timing=timing
    )

    assert with_reasoning.reasoning_timing == timing
    assert bare.reasoning_timing is None
