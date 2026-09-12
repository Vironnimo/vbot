"""Tests for compaction."""

from __future__ import annotations

import json
import threading
from collections.abc import AsyncIterator
from typing import Any

import pytest

from core.chat import ChatMessage
from core.chat._message_history import effective_compaction_messages
from core.chat.messages import COMPACTION_SKILL_NOTE_PREFIX, COMPACTION_SUMMARY_NOTE_PREFIX
from core.chat.wire_shaping import _embed_notes_into_request
from core.compaction import (
    MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
    CompactionError,
    CompactionInsufficientReclaimError,
    CompactionService,
    CompactionSettings,
    is_compacted_tool_result_content,
)
from core.compaction.compaction import (
    COMPACTION_REFERENCE_PREFIX,
    COMPACTION_SUMMARY_END_MARKER,
    COMPACTION_USER_QUOTE_PREFIX,
    CompactionPlan,
    _reference_summary,
    _send_streaming_model_request,
)
from core.providers.anthropic import AnthropicAdapter
from core.providers.ollama import OllamaAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.sessions import SessionAddress
from core.sessions.history import _skill_context_note_content
from core.tools import tool_success
from tests.core.compaction.compaction_test_support import (
    StubAdapter,
    StubStorage,
    _tail_token_span,
    assistant,
    checkpoint,
    message,
    provider_request,
    user,
)


class RetainContextStrategy:
    id = "retain-context"

    def plan(self, context: Any, settings: Any) -> CompactionPlan:
        del settings
        return CompactionPlan(
            model_messages=None,
            model_target="summary",
            summary_text="RETAINED",
            after_summary=tuple(context.messages),
            compacted_token_count=1,
        )


@pytest.mark.asyncio
async def test_compaction_reports_only_the_immediately_completed_skill_epoch() -> None:
    service = CompactionService(RetainContextStrategy())
    alpha_content = '<skill_content name="alpha">Alpha instructions</skill_content>'
    alpha_note = ChatMessage.note(_skill_context_note_content("alpha", alpha_content))
    first_messages = [
        user("u1", "First task"),
        alpha_note,
        assistant("a1", "First result"),
    ]

    first = await service.compact(
        first_messages,
        session_address=SessionAddress(project_id=None, agent_id="coder", session_id="session"),
        prompt_cache_affinity_id="test-affinity",
        summary_adapter=StubAdapter(),
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(strategy="retain-context"),
    )
    first_effective = effective_compaction_messages([*first_messages, first])
    first_guidance = [
        item
        for item in first_effective
        if item.role == "note"
        and isinstance(item.content, str)
        and item.content.startswith(COMPACTION_SKILL_NOTE_PREFIX)
    ]

    assert len(first_guidance) == 1
    assert '["alpha"]' in str(first_guidance[0].content)
    assert all("Alpha instructions" not in str(item.content) for item in first_effective)

    beta_content = '<skill_content name="beta">Beta instructions</skill_content>'
    beta_note = ChatMessage.note(_skill_context_note_content("beta", beta_content))
    second_messages = [*first_messages, first, beta_note, assistant("a2", "Second result")]
    second = await service.compact(
        second_messages,
        session_address=SessionAddress(project_id=None, agent_id="coder", session_id="session"),
        prompt_cache_affinity_id="test-affinity",
        summary_adapter=StubAdapter(),
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(strategy="retain-context"),
    )
    second_effective = effective_compaction_messages([*second_messages, second])
    second_guidance = [
        item
        for item in second_effective
        if item.role == "note"
        and isinstance(item.content, str)
        and item.content.startswith(COMPACTION_SKILL_NOTE_PREFIX)
    ]

    assert len(second_guidance) == 1
    assert '["beta"]' in str(second_guidance[0].content)
    assert '["alpha"]' not in str(second_guidance[0].content)
    assert all("Beta instructions" not in str(item.content) for item in second_effective)
    rendered_second = _embed_notes_into_request(second_effective)
    assert COMPACTION_SKILL_NOTE_PREFIX not in json.dumps(rendered_second)

    third_messages = [*second_messages, second, assistant("a3", "Third result")]
    third = await service.compact(
        third_messages,
        session_address=SessionAddress(project_id=None, agent_id="coder", session_id="session"),
        prompt_cache_affinity_id="test-affinity",
        summary_adapter=StubAdapter(),
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(strategy="retain-context"),
    )
    third_effective = effective_compaction_messages([*third_messages, third])

    assert all(
        not (
            item.role == "note"
            and isinstance(item.content, str)
            and item.content.startswith(COMPACTION_SKILL_NOTE_PREFIX)
        )
        for item in third_effective
    )


@pytest.mark.asyncio
async def test_compaction_compacts_skill_tool_carrier_without_breaking_its_cycle() -> None:
    skill_result_content = json.dumps(
        tool_success(
            {
                "name": "docx",
                "status": "loaded",
                "content": "Full document instructions",
                "environment_access": "Use DOCX_KEY through bash env_keys.",
            }
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    messages = [
        user("u1", "Create a document"),
        message(
            "a-tools",
            "assistant",
            "",
            model="openai/gpt-5",
            tool_calls=[{"id": "call-skill", "name": "skill", "arguments": {"name": "docx"}}],
        ),
        message(
            "t-skill",
            "tool",
            skill_result_content,
            tool_call_id="call-skill",
            name="skill",
        ),
        assistant("a2", "Used the Skill"),
    ]

    result = await CompactionService(RetainContextStrategy()).compact(
        messages,
        session_address=SessionAddress(project_id=None, agent_id="coder", session_id="session"),
        prompt_cache_affinity_id="test-affinity",
        summary_adapter=StubAdapter(),
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(strategy="retain-context"),
    )
    effective = effective_compaction_messages([*messages, result])
    projected_result = next(item for item in effective if item.id == "t-skill")
    projected_payload = json.loads(str(projected_result.content))

    assert is_compacted_tool_result_content(projected_result.content)
    assert projected_payload["outcome"] == {
        "name": "docx",
        "status": "loaded",
        "compacted": True,
    }
    assert "Full document instructions" not in str(projected_result.content)
    assert "DOCX_KEY" not in str(projected_result.content)


@pytest.mark.asyncio
async def test_summary_tail_executes_one_call_and_materializes_projection() -> None:
    summary_adapter = StubAdapter("NEW SUMMARY")
    active_adapter = StubAdapter("must not be used")
    messages = [
        user("u1", "old request " * 100),
        assistant("a1", "old response " * 100),
        user("u2", "recent request"),
        assistant("a2", "recent response"),
    ]
    request = provider_request(messages)
    tools = [{"name": "read", "description": "Read a file", "parameters": {}}]
    storage = StubStorage()

    result = await CompactionService().compact(
        messages,
        session_address=SessionAddress(project_id=None, agent_id="coder", session_id="session"),
        prompt_cache_affinity_id="test-affinity",
        summary_adapter=summary_adapter,
        summary_model_id="openai/summary",
        storage=storage,
        settings=CompactionSettings(tail_tokens=_tail_token_span(messages[2:])),
        request_messages=request,
        active_adapter=active_adapter,
        active_model_id="openai/active",
        active_tools=tools,
    )

    assert len(summary_adapter.requests) == 1
    assert active_adapter.requests == []
    assert summary_adapter.requests[0]["model_id"] == "openai/summary"
    sent = summary_adapter.requests[0]["messages"]
    assert sent[:-1] == request[:3]
    assert [message["role"] for message in sent] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert str(sent[-1]["content"]).startswith("<system-reminder>\n")
    assert str(sent[-1]["content"]).endswith("\n</system-reminder>")
    reminder_content = str(sent[-1]["content"])
    assert "Preserve decisions and unfinished work." in reminder_content
    assert "<retained_tail>" not in reminder_content
    assert "recent request" not in str(sent)
    assert "recent response" not in str(sent)
    assert storage.read_names == ["compaction.md"]
    assert summary_adapter.requests[0]["tools"] == tools
    assert summary_adapter.requests[0]["temperature"] is None
    assert (
        str(summary_adapter.requests[0]["messages"][-1]["content"]).count(
            "Preserve decisions and unfinished work."
        )
        == 1
    )
    effective = effective_compaction_messages([*messages, result])
    assert effective[0].role == "note"
    assert effective[0].content == (
        f"{COMPACTION_SUMMARY_NOTE_PREFIX}{COMPACTION_REFERENCE_PREFIX}\n"
        f"NEW SUMMARY\n{COMPACTION_SUMMARY_END_MARKER}"
    )
    assert [item.id for item in effective[1:]] == ["u2", "a2"]
    request_projection = _embed_notes_into_request(effective)
    assert COMPACTION_REFERENCE_PREFIX in request_projection[0]["content"]
    assert COMPACTION_SUMMARY_END_MARKER in request_projection[0]["content"]
    assert request_projection[1]["content"] == "recent request"


@pytest.mark.asyncio
async def test_summary_tail_discards_copied_outer_system_reminder_tags() -> None:
    summary_adapter = StubAdapter("<system-reminder>\nSUMMARY\n</system-reminder>")
    messages = [
        user("u1", "old request " * 100),
        assistant("a1", "old response " * 100),
        user("u2", "recent request"),
        assistant("a2", "recent response"),
    ]

    result = await CompactionService().compact(
        messages,
        session_address=SessionAddress(project_id=None, agent_id="coder", session_id="session"),
        prompt_cache_affinity_id="test-affinity",
        summary_adapter=summary_adapter,
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(tail_tokens=_tail_token_span(messages[2:])),
        request_messages=provider_request(messages),
    )

    assert result.content == (
        f"{COMPACTION_REFERENCE_PREFIX}\nSUMMARY\n{COMPACTION_SUMMARY_END_MARKER}"
    )
    rendered = _embed_notes_into_request(effective_compaction_messages([result]))[0]["content"]
    assert rendered.count("<system-reminder>") == 1
    assert rendered.count("</system-reminder>") == 1


def test_reference_summary_keeps_non_boundary_reminder_tag_mentions() -> None:
    summary = "Finding: an inline <system-reminder> example remains intact."

    referenced = _reference_summary(summary)

    assert summary in referenced


@pytest.mark.asyncio
async def test_manual_summary_tail_uses_manual_prompt_before_tail() -> None:
    adapter = StubAdapter("NEW SUMMARY")
    storage = StubStorage()
    messages = [user("u1", "old request"), assistant("a1", "recent response")]

    await CompactionService().compact(
        messages,
        session_address=SessionAddress(project_id=None, agent_id="coder", session_id="session"),
        prompt_cache_affinity_id="test-affinity",
        summary_adapter=adapter,
        summary_model_id="openai/summary",
        storage=storage,
        settings=CompactionSettings(tail_tokens=1),
        request_messages=provider_request(messages),
        trigger="manual",
    )

    reminder_content = str(adapter.requests[0]["messages"][-1]["content"])
    assert storage.read_names == ["compaction-manual.md"]
    assert "Preserve decisions and unfinished work." in reminder_content
    assert "<retained_tail>" not in reminder_content


@pytest.mark.asyncio
async def test_compaction_keeps_sync_transforms_off_loop_and_model_io_on_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.compaction import compaction

    loop_thread = threading.get_ident()
    strategy_threads: list[int] = []
    stream_threads: list[int] = []
    finalization_threads: list[int] = []
    original_extract = compaction._extract_summary_text

    def recording_extract(response: dict[str, Any]) -> str:
        finalization_threads.append(threading.get_ident())
        return original_extract(response)

    monkeypatch.setattr(compaction, "_extract_summary_text", recording_extract)

    class RecordingStrategy:
        id = "recording"

        def plan(self, context: Any, settings: Any) -> CompactionPlan:
            strategy_threads.append(threading.get_ident())
            return CompactionPlan(
                model_messages=({"role": "user", "content": "compact"},),
                model_target="summary",
                compacted_token_count=1,
            )

    class RecordingAdapter:
        async def stream(
            self, messages: list[dict], **kwargs: Any
        ) -> AsyncIterator[dict[str, Any]]:
            stream_threads.append(threading.get_ident())
            yield {"type": "content_delta", "text": "summary"}
            yield {"type": "finish", "reason": "stop"}

    adapter = RecordingAdapter()
    await CompactionService(RecordingStrategy()).compact(
        [user("u1", "old context")],
        session_address=SessionAddress(project_id=None, agent_id="coder", session_id="session"),
        prompt_cache_affinity_id="test-affinity",
        summary_adapter=adapter,
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(strategy="recording"),
    )

    assert strategy_threads and strategy_threads != [loop_thread]
    assert stream_threads == [loop_thread]
    assert finalization_threads and finalization_threads != [loop_thread]


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["summary_tail", "continuation"])
@pytest.mark.parametrize(
    "adapter_class", [OpenAICompatibleAdapter, AnthropicAdapter, OllamaAdapter]
)
async def test_compaction_consumes_canonical_stream_without_raw_wire_normalization(
    strategy: str, adapter_class: type[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = object.__new__(adapter_class)
    adapter._model_lookup = None
    requests: list[list[dict[str, Any]]] = []

    async def stream(
        messages: list[dict[str, Any]], **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        requests.append(messages)
        yield {"type": "heartbeat"}
        yield {"type": "content_delta", "text": "SUMMARY SENTINEL"}
        yield {"type": "finish", "reason": "stop"}

    monkeypatch.setattr(adapter, "stream", stream)
    messages = [
        user("u1", "old request"),
        assistant("a1", "old answer"),
        user("u2", "current request"),
        assistant("a2", "current answer"),
    ]

    result = await CompactionService().compact(
        messages,
        session_address=SessionAddress(project_id=None, agent_id="coder", session_id="session"),
        prompt_cache_affinity_id="test-affinity",
        summary_adapter=adapter,
        summary_model_id="summary",
        storage=StubStorage(),
        settings=CompactionSettings(strategy=strategy, tail_tokens=1),
        request_messages=provider_request(messages),
        active_adapter=adapter,
        active_model_id="summary",
    )

    assert result.role == "compaction_checkpoint"
    assert "SUMMARY SENTINEL" in str(result.content)
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["summary_tail", "continuation"])
@pytest.mark.parametrize(
    "finish", ["output_truncated", "content_filtered", "error", "unknown", "tool_calls", None]
)
async def test_compaction_rejects_partial_summary_without_successful_finish(
    strategy: str, finish: str | None
) -> None:
    class IncompleteAdapter(StubAdapter):
        async def stream(
            self, messages: list[dict], **kwargs: Any
        ) -> AsyncIterator[dict[str, Any]]:
            self.requests.append({"messages": messages, **kwargs})
            yield {"type": "content_delta", "text": "Partial summary"}
            if finish is not None:
                yield {"type": "finish", "reason": finish}

    adapter = IncompleteAdapter()
    messages = [user("u1", "request"), assistant("a1", "answer")]
    original_messages = [message.to_dict() for message in messages]

    with pytest.raises(CompactionError):
        await CompactionService().compact(
            messages,
            session_address=SessionAddress(project_id=None, agent_id="coder", session_id="session"),
            prompt_cache_affinity_id="test-affinity",
            summary_adapter=adapter,
            summary_model_id="summary",
            storage=StubStorage(),
            settings=CompactionSettings(strategy=strategy),
            request_messages=provider_request(messages),
            active_adapter=adapter,
            active_model_id="summary",
        )

    assert len(adapter.requests) == 1
    assert [message.to_dict() for message in messages] == original_messages


@pytest.mark.asyncio
async def test_compaction_rejects_tool_attempt_even_when_finish_claims_stop() -> None:
    class ToolAdapter:
        async def stream(
            self, messages: list[dict], **kwargs: Any
        ) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "content_delta", "text": "I will inspect the context"}
            yield {
                "type": "tool_call_delta",
                "id": "call-1",
                "name_delta": "read",
                "arguments_delta": '{"path":"notes.txt"}',
            }
            yield {"type": "finish", "reason": "stop"}

    with pytest.raises(CompactionError):
        await _send_streaming_model_request(ToolAdapter(), [], {})


@pytest.mark.asyncio
async def test_compaction_stream_preserves_split_usage_and_post_finish_details() -> None:
    class UsageAdapter:
        async def stream(
            self, messages: list[dict], **kwargs: Any
        ) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "content_delta", "text": "summary"}
            yield {"type": "usage", "input_tokens": 100, "cache_read_tokens": 20}
            yield {"type": "finish", "reason": "stop"}
            yield {"type": "usage", "output_tokens": 30, "reasoning_tokens": 10}

    response = await _send_streaming_model_request(UsageAdapter(), [], {})

    assert response["usage"] == {
        "input_tokens": 100,
        "output_tokens": 30,
        "cache_read_tokens": 20,
        "reasoning_tokens": 10,
    }


@pytest.mark.asyncio
async def test_summary_tail_preserves_exact_active_model_prefix_with_reasoning() -> None:
    adapter = StubAdapter("NEW SUMMARY")
    messages = [
        user("u1", "old request " * 100),
        assistant("a1", "old response " * 100),
        user("u2", "recent request"),
        assistant("a2", "recent response"),
    ]
    request = provider_request(messages)
    request[2]["reasoning"] = "provider-readable"
    request[2]["reasoning_meta"] = {"signature": "provider-opaque"}

    await CompactionService().compact(
        messages,
        session_address=SessionAddress(project_id=None, agent_id="coder", session_id="session"),
        prompt_cache_affinity_id="test-affinity",
        summary_adapter=adapter,
        summary_model_id="gpt-5",
        storage=StubStorage(),
        settings=CompactionSettings(tail_tokens=_tail_token_span(messages[2:])),
        request_messages=request,
        active_adapter=adapter,
        active_model_id="gpt-5",
    )

    sent = adapter.requests[0]["messages"]
    assert sent[:-1] == request[:3]
    assert sent[2]["reasoning"] == "provider-readable"
    assert sent[2]["reasoning_meta"] == {"signature": "provider-opaque"}
    assert "recent request" not in str(sent)
    assert "recent response" not in str(sent)


@pytest.mark.asyncio
async def test_custom_summary_model_drops_active_provider_reasoning_state() -> None:
    summary_adapter = StubAdapter("NEW SUMMARY")
    active_adapter = StubAdapter("must not be used")
    messages = [
        user("u1", "old request " * 100),
        assistant("a1", "old response " * 100),
        user("u2", "recent request"),
        assistant("a2", "recent response"),
    ]
    request = provider_request(messages)
    request[2]["reasoning"] = "provider-readable"
    request[2]["reasoning_meta"] = {"signature": "provider-opaque"}

    await CompactionService().compact(
        messages,
        session_address=SessionAddress(project_id=None, agent_id="coder", session_id="session"),
        prompt_cache_affinity_id="test-affinity",
        summary_adapter=summary_adapter,
        summary_model_id="claude-summary",
        storage=StubStorage(),
        settings=CompactionSettings(tail_tokens=_tail_token_span(messages[2:])),
        request_messages=request,
        active_adapter=active_adapter,
        active_model_id="gpt-5",
    )

    sent = summary_adapter.requests[0]["messages"]
    assert sent[:2] == request[:2]
    assert sent[2]["content"] == request[2]["content"]
    assert "reasoning" not in sent[2]
    assert "reasoning_meta" not in sent[2]
    assert len(sent) == 4
    assert "recent request" not in str(sent)
    assert "recent response" not in str(sent)


@pytest.mark.asyncio
async def test_next_compaction_consumes_previous_projection_not_hidden_history() -> None:
    adapter = StubAdapter("NEXT")
    prior = checkpoint(
        [ChatMessage.note(f"{COMPACTION_SUMMARY_NOTE_PREFIX}PRIOR"), user("u2", "kept")]
    )
    hidden = user("u1", "hidden-secret-marker")

    latest = assistant("a2", "new")
    request = [
        {"id": "system-1", "role": "system", "content": "system"},
        {
            "role": "user",
            "content": f"{COMPACTION_SUMMARY_NOTE_PREFIX}PRIOR",
        },
        {"id": "u2", "role": "user", "content": "kept"},
        latest.to_dict(),
    ]

    await CompactionService().compact(
        [hidden, prior, latest],
        session_address=SessionAddress(project_id=None, agent_id="coder", session_id="session"),
        prompt_cache_affinity_id="test-affinity",
        summary_adapter=adapter,
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(tail_tokens=1),
        request_messages=request,
    )

    compact_request = adapter.requests[0]["messages"]
    assert compact_request[:-1] == request[:3]
    assert compact_request[-1]["role"] == "user"
    assert "hidden-secret-marker" not in str(compact_request)
    assert str(compact_request).count(COMPACTION_SUMMARY_NOTE_PREFIX + "PRIOR") == 1
    assert "<previous_summary>" not in str(compact_request)
    assert "<retained_tail>" not in str(compact_request)
    assert str(compact_request).count("kept") == 1
    assert "new" not in str(compact_request)


def test_summary_tail_auto_compaction_advances_inside_retained_user_turn() -> None:
    current_user = user("u1", "One long-running user turn")
    old_carrier = message(
        "a1",
        "assistant",
        "",
        model="openai/gpt-5",
        tool_calls=[{"id": "call-1", "name": "read", "arguments": {"path": "one"}}],
    )
    old_result = message(
        "t1",
        "tool",
        "result one",
        tool_call_id="call-1",
        name="read",
    )
    prior = checkpoint([current_user, old_carrier, old_result])
    next_carrier = message(
        "a2",
        "assistant",
        "",
        model="openai/gpt-5",
        tool_calls=[{"id": "call-2", "name": "edit", "arguments": {"path": "one"}}],
    )
    next_result = message(
        "t2",
        "tool",
        "result two",
        tool_call_id="call-2",
        name="edit",
    )

    can_compact = CompactionService().has_new_compactable_context(
        [current_user, old_carrier, old_result, prior, next_carrier, next_result],
        CompactionSettings(tail_tokens=1),
    )

    assert can_compact is True


def test_summary_tail_auto_compaction_waits_when_only_prior_summary_is_in_head() -> None:
    retained_user = user("u1", "Previously retained turn " * 100)
    prior = checkpoint([retained_user])

    can_compact = CompactionService().has_new_compactable_context(
        [retained_user, prior],
        CompactionSettings(tail_tokens=1),
    )

    assert can_compact is False


@pytest.mark.asyncio
async def test_historical_user_quote_survives_repeated_compaction() -> None:
    adapter = StubAdapter("FIRST")
    active_user_text = "Complete the whole task; do not stop after one checkpoint."
    active_user = user("u-active", active_user_text)
    messages = [
        active_user,
        assistant("a-old", "Earlier implementation work. " * 1_000),
        assistant("a-latest", "Continuing with the next implementation step."),
    ]
    service = CompactionService()

    first = await service.compact(
        messages,
        session_address=SessionAddress(project_id=None, agent_id="coder", session_id="session"),
        prompt_cache_affinity_id="test-affinity",
        summary_adapter=adapter,
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(tail_tokens=10),
        request_messages=provider_request(messages),
    )
    after_first = effective_compaction_messages([*messages, first])
    first_quote = next(
        item for item in after_first if str(item.content).startswith(COMPACTION_USER_QUOTE_PREFIX)
    )
    assert (
        json.loads(str(first_quote.content).removeprefix(COMPACTION_USER_QUOTE_PREFIX))
        == active_user.to_dict()
    )
    assert not any(item.role == "user" for item in after_first)
    continued = assistant("a-next", "Working beyond the first checkpoint.")
    adapter.text = "SECOND"

    second = await service.compact(
        [*messages, first, continued],
        session_address=SessionAddress(project_id=None, agent_id="coder", session_id="session"),
        prompt_cache_affinity_id="test-affinity",
        summary_adapter=adapter,
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(tail_tokens=10),
        request_messages=provider_request([*after_first, continued]),
    )
    after_second = effective_compaction_messages([*messages, first, continued, second])

    retained_users = [item for item in after_second if item.role == "user"]
    assert retained_users == []
    quotes = [
        item for item in after_second if str(item.content).startswith(COMPACTION_USER_QUOTE_PREFIX)
    ]
    assert quotes == [first_quote]
    assert not service.has_new_compactable_context([second], CompactionSettings(tail_tokens=10))
    second_request = adapter.requests[1]["messages"]
    assert sum(active_user_text in str(item.get("content")) for item in second_request) == 1


@pytest.mark.asyncio
async def test_summary_tail_summarizes_old_tool_batch_without_rewriting_retained_steps() -> None:
    adapter = StubAdapter("TOOL SUMMARY")
    old_arguments = {"path": "old.txt", "query": "Q" * 8_000}
    old_result_content = "sensitive-output-" * 5_000
    old_carrier = message(
        "a-old",
        "assistant",
        "",
        model="openai/gpt-5",
        tool_calls=[{"id": "call-old", "name": "read", "arguments": old_arguments}],
    )
    old_result = message(
        "t-old",
        "tool",
        old_result_content,
        tool_call_id="call-old",
        name="read",
    )
    latest_carrier = message(
        "a-latest",
        "assistant",
        "",
        model="openai/gpt-5",
        tool_calls=[{"id": "call-latest", "name": "edit", "arguments": {"path": "latest.txt"}}],
    )
    latest_result = message(
        "t-latest",
        "tool",
        "latest result",
        tool_call_id="call-latest",
        name="edit",
    )
    messages = [
        user("u1", "Keep working until the task is complete"),
        old_carrier,
        old_result,
        latest_carrier,
        latest_result,
    ]
    request = provider_request(messages)
    original_snapshot = [item.to_dict() for item in messages]
    service = CompactionService()

    assert service.has_new_compactable_context(
        messages,
        CompactionSettings(tail_tokens=1),
    )

    result = await service.compact(
        messages,
        session_address=SessionAddress(project_id=None, agent_id="coder", session_id="session"),
        prompt_cache_affinity_id="test-affinity",
        summary_adapter=adapter,
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(tail_tokens=1),
        request_messages=request,
        minimum_reclaim_tokens=MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
    )

    compact_request = adapter.requests[0]["messages"]
    assert len(adapter.requests) == 1
    assert compact_request[:-1] == request[:4]
    assert compact_request[-1]["role"] == "user"
    assert old_result_content in str(compact_request)
    assert "latest result" not in str(compact_request)
    assert "<retained_tail>" not in str(compact_request)
    assert [item.to_dict() for item in messages] == original_snapshot
    effective = effective_compaction_messages([*messages, result])
    assert [item.id for item in effective if item.role in {"user", "assistant", "tool"}] == [
        "a-latest",
        "t-latest",
    ]
    assert (
        next(item for item in effective if item.id == "a-latest").tool_calls
        == latest_carrier.tool_calls
    )
    assert (
        next(item for item in effective if item.id == "t-latest").content == latest_result.content
    )
    quote = next(
        item for item in effective if str(item.content).startswith(COMPACTION_USER_QUOTE_PREFIX)
    )
    assert (
        json.loads(str(quote.content).removeprefix(COMPACTION_USER_QUOTE_PREFIX))
        == messages[0].to_dict()
    )


@pytest.mark.asyncio
async def test_summary_tail_requires_active_request_context() -> None:
    with pytest.raises(CompactionError):
        await CompactionService().compact(
            [user("u1", "old"), assistant("a1", "tail")],
            session_address=SessionAddress(project_id=None, agent_id="coder", session_id="session"),
            prompt_cache_affinity_id="test-affinity",
            summary_adapter=StubAdapter(),
            summary_model_id="openai/summary",
            storage=StubStorage(),
            settings=CompactionSettings(tail_tokens=1),
        )


@pytest.mark.asyncio
async def test_automatic_compaction_rejects_projection_below_minimum_reclaim() -> None:
    adapter = StubAdapter("A summary that is intentionally much larger " * 500)
    messages = [
        user("u1", "old"),
        assistant("a1", "short"),
        user("u2", "tail"),
    ]

    with pytest.raises(
        CompactionInsufficientReclaimError,
    ):
        await CompactionService().compact(
            messages,
            session_address=SessionAddress(project_id=None, agent_id="coder", session_id="session"),
            prompt_cache_affinity_id="test-affinity",
            summary_adapter=adapter,
            summary_model_id="openai/summary",
            storage=StubStorage(),
            settings=CompactionSettings(tail_tokens=1),
            request_messages=provider_request(messages),
            minimum_reclaim_tokens=MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
        )

    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_compaction_engine_leaves_context_projection_for_chat() -> None:
    service = CompactionService()
    messages = [
        user("u1", "old context " * 2_000),
        assistant("a1", "old answer " * 2_000),
        user("u2", "tail"),
        assistant("a2", "tail answer"),
    ]

    result = await service.compact(
        messages,
        session_address=SessionAddress(project_id=None, agent_id="coder", session_id="session"),
        prompt_cache_affinity_id="test-affinity",
        summary_adapter=StubAdapter("Short retained summary."),
        summary_model_id="openai/summary",
        storage=StubStorage(),
        settings=CompactionSettings(tail_tokens=1),
        request_messages=provider_request(messages),
    )

    assert result.usage is not None
    assert set(result.usage) == {"compacted_token_count"}
