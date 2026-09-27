"""Whether an automatic Compaction boundary compacts: Policy, trigger, window and deferral."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat import ChatMessage, ToolCall
from core.providers.providers import GLOBAL_CONTEXT_WINDOW_FLOOR
from core.runs import COMPACTION_STARTED_EVENT
from core.tools import tool_success
from core.utils.tokens import estimate_request_input_tokens
from tests.core.chat.chat_loop_compaction_test_support import (
    JsonObject,
    auto_compact,
    compaction_runtime,
)
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubCompactionService,
    StubModels,
    build_chat_loop,
)

_UNUSED_CHECKPOINT = ChatMessage.compaction_checkpoint(
    summary="unused", projection=[ChatMessage.user("unused")], compacted_token_count=1
)


class WireEstimateAdapter(StubAdapter):
    """Report fixed selected-wire request estimates in order, repeating the last one."""

    def __init__(self, *estimates: int) -> None:
        super().__init__([])
        self._estimates = list(estimates)

    def estimate_request_input_tokens(
        self,
        _messages: list[JsonObject],
        *,
        model_id: str,
        tools: list[JsonObject] | None = None,
    ) -> int:
        del model_id, tools
        return self._estimates.pop(0) if len(self._estimates) > 1 else self._estimates[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settings", "session_policy", "service_kwargs", "evaluated", "context_checks"),
    [
        ({"auto": False}, None, {"should_auto": True}, False, 0),
        (
            {},
            {
                "enabled": False,
                "trigger": {"type": "context_ratio", "threshold": 0.8},
                "strategy": {"type": "summary_tail", "tail_tokens": 15_000},
            },
            {"should_auto": True},
            False,
            0,
        ),
        ({}, None, {"should_auto": False}, True, 0),
        ({}, None, {"should_auto": True, "has_compactable_context": False}, True, 1),
    ],
    ids=["global-policy-disabled", "session-policy-disabled", "below-trigger", "no-new-context"],
)
async def test_automatic_compaction_needs_an_enabled_policy_a_trigger_and_new_context(
    tmp_path: Path,
    settings: JsonObject,
    session_policy: JsonObject | None,
    service_kwargs: JsonObject,
    evaluated: bool,
    context_checks: int,
) -> None:
    runtime = compaction_runtime(tmp_path, settings=settings)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Hi"))
    if session_policy is not None:
        runtime.chat_sessions.mutate_metadata(
            session.address,
            lambda metadata: metadata.__setitem__("compaction_policy", session_policy),
        )
    service = StubCompactionService(checkpoint=_UNUSED_CHECKPOINT, **service_kwargs)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, service))

    probe = await auto_compact(
        loop, runtime.agents.get("coder"), session, usage={"input_tokens": 90}
    )

    assert probe.rebuilt == probe.request
    assert service.should_auto_calls == ([(90, 100, 0.8)] if evaluated else [])
    assert len(service.compactable_context_calls) == context_checks
    assert service.compact_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("usage", "expected_tokens"),
    [({"input_tokens": 20}, 20), (None, 95)],
    ids=["measured-in-this-run", "estimated-for-a-new-run"],
)
async def test_trigger_counts_this_runs_measurement_or_the_selected_wire_estimate(
    tmp_path: Path, usage: JsonObject | None, expected_tokens: int
) -> None:
    # A measurement from this Run anchors the trigger even when the selected wire
    # estimates more; a new Run never reuses an older step's measurement.
    runtime = compaction_runtime(tmp_path, adapter=WireEstimateAdapter(95))
    agent = runtime.agents.get("coder")
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Earlier"))
    session.append(
        ChatMessage.assistant(
            model=agent.model, content="x" * 50_000, usage={"input_tokens": 20, "output_tokens": 0}
        )
    )
    session.append(ChatMessage.user("Current"))
    service = StubCompactionService(should_auto=False)

    probe = await auto_compact(
        build_chat_loop(runtime, compaction_service=cast(Any, service)),
        agent,
        session,
        usage=usage,
    )

    generic_tokens, _ = estimate_request_input_tokens(probe.request)
    assert generic_tokens > 95
    assert service.should_auto_calls == [(expected_tokens, 100, 0.8)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "model_window", "expected_window"),
    [
        ("openai/gpt-5.4", 400_000, 1_050_000),
        ("openai/gpt-5.4::subscription", 400_000, 272_000),
        ("openai/gpt-5.4::subscription", None, GLOBAL_CONTEXT_WINDOW_FLOOR),
    ],
    ids=["api-key-connection", "subscription-connection", "unknown-window-uses-floor"],
)
async def test_trigger_uses_the_selected_connections_context_window(
    tmp_path: Path, model: str, model_window: int | None, expected_window: int
) -> None:
    key = ("openai", "gpt-5.4")
    agent = StubAgent(id="coder", model=model, allowed_tools=["*"])
    runtime = compaction_runtime(
        tmp_path,
        agent=agent,
        models=StubModels(
            {key: model_window},
            connection_context_windows=(
                {key: {"api-key": 1_050_000, "subscription": 272_000}} if model_window else None
            ),
        ),
    )
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Hi"))
    service = StubCompactionService(should_auto=False)

    await auto_compact(
        build_chat_loop(runtime, compaction_service=cast(Any, service)),
        agent,
        session,
        usage={"input_tokens": 20},
    )

    assert service.should_auto_calls == [(20, expected_window, 0.8)]


@pytest.mark.asyncio
async def test_checkpoint_records_both_context_sizes_with_the_selected_wire_estimator(
    tmp_path: Path,
) -> None:
    runtime = compaction_runtime(tmp_path, adapter=WireEstimateAdapter(95, 37))
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Head"))
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted snapshot.",
        projection=[ChatMessage.user("Tail")],
        compacted_token_count=50,
    )
    service = StubCompactionService(should_auto=True, checkpoint=checkpoint)

    probe = await auto_compact(
        build_chat_loop(runtime, compaction_service=cast(Any, service)),
        runtime.agents.get("coder"),
        session,
        usage=None,
    )

    persisted = session.load()[-1]
    assert persisted.role == "compaction_checkpoint"
    assert persisted.usage is not None
    assert (persisted.usage["context_tokens_before"], persisted.usage["context_tokens_after"]) == (
        95,
        37,
    )
    started = next(event for event in probe.run.events if event.type == COMPACTION_STARTED_EVENT)
    assert started.payload["context_tokens_before"] == 95
    assert started.payload["context_usage"] == {"tokens": 95, "estimated": True}


@pytest.mark.asyncio
async def test_summary_tail_waits_until_a_loaded_skill_result_is_consumed(tmp_path: Path) -> None:
    runtime = compaction_runtime(tmp_path)
    agent = runtime.agents.get("coder")
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Use the document workflow"))
    session.append(
        ChatMessage.assistant(
            model=agent.model,
            content=None,
            tool_calls=[ToolCall(id="call-skill", name="skill", arguments={"name": "docx"})],
        )
    )
    session.append(
        ChatMessage.tool(
            tool_call_id="call-skill",
            name="skill",
            content=json.dumps(
                tool_success({"name": "docx", "status": "loaded", "content": "Instructions"}),
                separators=(",", ":"),
            ),
        )
    )
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Compacted after consumption.",
        projection=[ChatMessage.user("Tail")],
        compacted_token_count=20,
    )
    service = StubCompactionService(should_auto=True, checkpoint=checkpoint)
    loop = build_chat_loop(runtime, compaction_service=cast(Any, service))

    deferred = await auto_compact(loop, agent, session, usage={"input_tokens": 90})

    assert deferred.rebuilt == deferred.request
    assert service.compactable_context_calls == []
    assert service.compact_calls == []

    session.append(ChatMessage.assistant(model=agent.model, content="Skill result consumed"))
    await auto_compact(loop, agent, session, usage={"input_tokens": 90}, run_id="run-2")

    assert len(service.compact_calls) == 1
