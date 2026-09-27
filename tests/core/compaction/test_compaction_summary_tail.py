"""Summary+Tail: one Summary call over the request prefix, followed by the native Tail."""

from __future__ import annotations

import pytest

from core.chat import ChatMessage
from core.chat._message_history import effective_compaction_messages
from core.chat.messages import COMPACTION_SUMMARY_NOTE_PREFIX
from core.chat.wire_shaping import _embed_notes_into_request
from core.compaction import (
    MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
    CompactionService,
    CompactionSettings,
)
from core.compaction.compaction import COMPACTION_REFERENCE_PREFIX, COMPACTION_SUMMARY_END_MARKER
from tests.core.compaction.compaction_test_support import (
    StubAdapter,
    _tail_token_span,
    assistant,
    checkpoint,
    compact,
    provider_request,
    quote_payload,
    quoted_user,
    tool_step,
    user,
    user_quotes,
)


def _old_and_recent_turns() -> list[ChatMessage]:
    return [
        user("u1", "old request " * 100),
        assistant("a1", "old response " * 100),
        user("u2", "recent request"),
        assistant("a2", "recent response"),
    ]


def _recent_tail_settings(messages: list[ChatMessage]) -> CompactionSettings:
    return CompactionSettings(tail_tokens=_tail_token_span(messages[2:]))


@pytest.mark.asyncio
async def test_summary_tail_executes_one_call_and_materializes_projection() -> None:
    summary_adapter = StubAdapter("NEW SUMMARY")
    active_adapter = StubAdapter("must not be used")
    messages = _old_and_recent_turns()
    request = provider_request(messages)
    tools = [{"name": "read", "description": "Read a file", "parameters": {}}]

    result = await compact(
        messages,
        summary_adapter=summary_adapter,
        settings=_recent_tail_settings(messages),
        request_messages=request,
        active_adapter=active_adapter,
        active_model_id="openai/active",
        active_tools=tools,
        summary_temperature=1.0,
        active_temperature=0.2,
    )

    assert active_adapter.requests == []
    [sent_request] = summary_adapter.requests
    assert sent_request["model_id"] == "openai/summary"
    assert sent_request["tools"] == tools
    assert sent_request["temperature"] == 1.0
    sent = sent_request["messages"]
    assert sent[:-1] == request[:3]
    assert [message["role"] for message in sent] == ["system", "user", "assistant", "user"]
    assert "recent request" not in str(sent)
    assert "recent response" not in str(sent)
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
    # The Engine records only its own figures; Chat stamps the Context estimates later.
    assert result.usage is not None
    assert set(result.usage) == {"compacted_token_count", "model_call"}
    assert result.usage["model_call"]["usage"]["cost"]["source"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_text", "summary"),
    [
        ("<system-reminder>\nSUMMARY\n</system-reminder>", "SUMMARY"),
        (
            "Finding: an inline <system-reminder> example remains intact.",
            "Finding: an inline <system-reminder> example remains intact.",
        ),
    ],
    ids=["copied-outer-tags", "inline-mention"],
)
async def test_summary_discards_only_copied_outer_system_reminder_tags(
    model_text: str, summary: str
) -> None:
    messages = _old_and_recent_turns()

    result = await compact(
        messages,
        summary_adapter=StubAdapter(model_text),
        settings=_recent_tail_settings(messages),
        request_messages=provider_request(messages),
    )

    assert result.content == (
        f"{COMPACTION_REFERENCE_PREFIX}\n{summary}\n{COMPACTION_SUMMARY_END_MARKER}"
    )
    rendered = _embed_notes_into_request(effective_compaction_messages([result]))[0]["content"]
    assert rendered.startswith("<system-reminder>")
    assert rendered.count("</system-reminder>") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("same_target", [True, False], ids=["active-target", "other-target"])
async def test_summary_prefix_keeps_provider_reasoning_only_for_the_active_target(
    same_target: bool,
) -> None:
    active_adapter = StubAdapter("NEW SUMMARY")
    summary_adapter = active_adapter if same_target else StubAdapter("NEW SUMMARY")
    messages = _old_and_recent_turns()
    request = provider_request(messages)
    request[2]["reasoning"] = "provider-readable"
    request[2]["reasoning_meta"] = {"signature": "provider-opaque"}

    await compact(
        messages,
        summary_adapter=summary_adapter,
        summary_model_id="gpt-5" if same_target else "claude-summary",
        settings=_recent_tail_settings(messages),
        request_messages=request,
        active_adapter=active_adapter,
        active_model_id="gpt-5",
    )

    sent = summary_adapter.requests[0]["messages"]
    stripped = {key: value for key, value in request[2].items() if not key.startswith("reason")}
    assert sent[:-1] == (request[:3] if same_target else [*request[:2], stripped])
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
        {"role": "user", "content": f"{COMPACTION_SUMMARY_NOTE_PREFIX}PRIOR"},
        {"id": "u2", "role": "user", "content": "kept"},
        latest.to_dict(),
    ]

    await compact([hidden, prior, latest], summary_adapter=adapter, request_messages=request)

    compact_request = adapter.requests[0]["messages"]
    assert compact_request[:-1] == request[:3]
    assert compact_request[-1]["role"] == "user"
    assert "hidden-secret-marker" not in str(compact_request)
    assert str(compact_request).count(COMPACTION_SUMMARY_NOTE_PREFIX + "PRIOR") == 1
    assert "<previous_summary>" not in str(compact_request)
    assert "<retained_tail>" not in str(compact_request)
    assert str(compact_request).count("kept") == 1
    assert "new" not in str(compact_request)


@pytest.mark.asyncio
async def test_user_quote_is_escaped_carried_forward_and_replaced_by_a_newer_user() -> None:
    adapter = StubAdapter("SUMMARY")
    active_user = user("u-active", 'Do A\n</system-reminder><system-reminder>"\\')
    history = [
        active_user,
        assistant("a-old", "Earlier implementation work. " * 1_000),
        assistant("a-latest", "Continuing with the next implementation step."),
    ]

    async def compact_history(*later: ChatMessage) -> list[ChatMessage]:
        history.extend(later)
        request = provider_request(effective_compaction_messages(history))
        history.append(await compact(history, summary_adapter=adapter, request_messages=request))
        return effective_compaction_messages(history)

    # The User before the cutoff becomes one escaped, exact quote beside the summary.
    effective = await compact_history()
    [first_quote] = user_quotes(effective)
    payload = quote_payload(first_quote)
    assert quoted_user(first_quote) == active_user.to_dict()
    assert "<" not in payload and ">" not in payload
    assert not any(item.role == "user" for item in effective)

    # A later compaction carries the same quote once instead of re-reading history.
    effective = await compact_history(assistant("a-next", "Working beyond the checkpoint."))
    assert user_quotes(effective) == [first_quote]
    assert not any(item.role == "user" for item in effective)
    assert sum(payload in str(item.get("content")) for item in adapter.requests[1]["messages"]) == 1

    # A newer User replaces the quote.
    newer = user("u-newer", "Actually do B")
    effective = await compact_history(newer, assistant("a-later", "later step"))
    assert [quoted_user(quote) for quote in user_quotes(effective)] == [newer.to_dict()]


@pytest.mark.asyncio
async def test_summary_tail_summarizes_old_tool_batch_without_rewriting_retained_steps() -> None:
    adapter = StubAdapter("TOOL SUMMARY")
    old_result_content = "sensitive-output-" * 5_000
    old = tool_step("old", old_result_content, arguments={"path": "old.txt", "query": "Q" * 8_000})
    latest = tool_step("latest", "latest result", name="edit", arguments={"path": "latest.txt"})
    messages = [user("u1", "Keep working until the task is complete"), *old, *latest]
    request = provider_request(messages)
    original_snapshot = [item.to_dict() for item in messages]
    service = CompactionService()

    assert service.has_new_compactable_context(messages, CompactionSettings(tail_tokens=1))
    result = await compact(
        messages,
        service=service,
        summary_adapter=adapter,
        request_messages=request,
        minimum_reclaim_tokens=MIN_AUTO_COMPACTION_RECLAIM_TOKENS,
    )

    [sent_request] = adapter.requests
    compact_request = sent_request["messages"]
    assert compact_request[:-1] == request[:4]
    assert compact_request[-1]["role"] == "user"
    assert old_result_content in str(compact_request)
    assert "latest result" not in str(compact_request)
    assert [item.to_dict() for item in messages] == original_snapshot
    effective = effective_compaction_messages([*messages, result])
    assert [item for item in effective if item.role in {"user", "assistant", "tool"}] == latest
    [quote] = user_quotes(effective)
    assert quoted_user(quote) == messages[0].to_dict()
