"""Chat-loop tests grouped by streaming."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from core.chat import (
    ChatMessage,
)
from core.chat.messages import HISTORY_COMPACTION_GUIDANCE
from core.providers.reasoning import (
    REASONING_REPLAY_FULL_HISTORY,
    REASONING_REPLAY_NONE,
)
from core.tools import (
    ToolRegistry,
    tool_success,
)
from tests.core.chat.chat_loop_support import (
    PolicyStubAdapter,
    StubAdapter,
    StubAgent,
    StubModels,
    StubRuntime,
    StubStorage,
    build_chat_loop,
    persisted_roles,
)

JsonObject = dict[str, Any]


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_fresh_follow_up_omits_old_reasoning_and_reasoning_meta_from_request(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="anthropic/claude-sonnet-4", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Fresh answer", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Previous question"))
    session.append(
        ChatMessage.assistant(
            model="anthropic/claude-sonnet-4",
            content="Previous answer",
            reasoning="Old readable reasoning",
            reasoning_meta={
                "content_blocks": [{"type": "thinking", "thinking": "Old readable reasoning"}]
            },
        )
    )

    await build_chat_loop(runtime).send("coder", "Follow up", session_id="session-one")

    assistant_history = adapter.requests[0]["messages"][2]
    persisted = [message.to_dict() for message in session.load()]
    assert assistant_history == {
        "id": persisted[1]["id"],
        "timestamp": persisted[1]["timestamp"],
        "role": "assistant",
        "model": "anthropic/claude-sonnet-4",
        "content": "Previous answer",
    }
    assert persisted[1]["reasoning"] == "Old readable reasoning"
    assert persisted[1]["reasoning_meta"] == {
        "content_blocks": [{"type": "thinking", "thinking": "Old readable reasoning"}]
    }


@pytest.mark.asyncio
async def test_fresh_follow_up_skips_reasoning_only_assistant_history_message(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="anthropic/claude-sonnet-4", allowed_tools=["*"])
    adapter = StubAdapter([{"content": "Fresh answer", "tool_calls": None}])
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Previous question"))
    session.append(
        ChatMessage.assistant(
            model="anthropic/claude-sonnet-4",
            content=None,
            reasoning="Old readable reasoning",
            reasoning_meta={"opaque": "provider-signed"},
        )
    )

    await build_chat_loop(runtime).send("coder", "Follow up", session_id="session-one")

    request_messages = adapter.requests[0]["messages"]
    persisted = session.load()
    assert [message["role"] for message in request_messages] == ["system", "user", "user"]
    assert request_messages[1]["content"] == "Previous question"
    assert request_messages[2]["content"] == "Follow up"
    assert persisted_roles(persisted) == ["user", "assistant", "user", "assistant"]
    assert persisted[1].content is None
    assert persisted[1].reasoning == "Old readable reasoning"
    assert persisted[1].reasoning_meta == {"opaque": "provider-signed"}


@pytest.mark.asyncio
async def test_full_history_policy_replays_same_model_reasoning_across_runs(
    tmp_path: Path,
) -> None:
    # Arrange: a prior-run same-model assistant turn (persisted with a
    # connection suffix) and a model-mismatched turn, both carrying reasoning.
    agent = StubAgent(id="coder", model="anthropic/claude-sonnet-4", allowed_tools=["*"])
    adapter = PolicyStubAdapter(
        [{"content": "Fresh answer", "tool_calls": None}],
        policy=REASONING_REPLAY_FULL_HISTORY,
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Q1"))
    session.append(
        ChatMessage.assistant(
            model="anthropic/claude-sonnet-4::api-key",
            content="A1",
            reasoning="Prior-run thinking",
            reasoning_meta={"content_blocks": [{"type": "thinking", "signature": "signed"}]},
        )
    )
    session.append(ChatMessage.user("Q2"))
    session.append(
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="A2",
            reasoning="Foreign model thinking",
            reasoning_meta={"opaque": "foreign"},
        )
    )

    await build_chat_loop(runtime).send("coder", "Q3", session_id="session-one")

    request = adapter.requests[0]["messages"]
    assert [message["role"] for message in request] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    # The policy hook is queried with the provider-local model id.
    assert adapter.policy_queries[0] == "claude-sonnet-4"
    same_model_entry = request[2]
    assert same_model_entry["reasoning"] == "Prior-run thinking"
    assert same_model_entry["reasoning_meta"] == {
        "content_blocks": [{"type": "thinking", "signature": "signed"}]
    }
    assert "usage" not in same_model_entry
    mismatched_entry = request[4]
    assert "reasoning" not in mismatched_entry
    assert "reasoning_meta" not in mismatched_entry


@pytest.mark.asyncio
async def test_none_policy_strips_reasoning_from_live_tool_continuation(
    tmp_path: Path,
) -> None:
    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["get_weather"])
    adapter = PolicyStubAdapter(
        [
            {
                "content": None,
                "reasoning": "Need weather.",
                "reasoning_meta": {"encrypted_content": "opaque-current-turn"},
                "tool_calls": [
                    {"id": "call_abc", "name": "get_weather", "arguments": {"city": "Berlin"}}
                ],
            },
            {"content": "Sunny", "tool_calls": None},
        ],
        policy=REASONING_REPLAY_NONE,
    )
    tools = ToolRegistry()
    tools.register(
        "get_weather",
        "Get weather.",
        {"type": "object"},
        lambda _context, arguments: tool_success({"temp": 22, "city": arguments["city"]}),
    )
    runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=adapter, tools=tools)

    assistant = await build_chat_loop(runtime).send("coder", "Weather?", session_id="session-one")

    # The live tool-continuation entry never carries reasoning fields.
    continuation = adapter.requests[1]["messages"]
    assert [message["role"] for message in continuation] == ["system", "user", "assistant", "tool"]
    assert "reasoning" not in continuation[2]
    assert "reasoning_meta" not in continuation[2]
    # Persistence is unaffected by request shaping.
    persisted = runtime.chat_sessions.get("coder", "session-one").load()
    assert persisted[1].reasoning == "Need weather."
    assert persisted[1].reasoning_meta == {"encrypted_content": "opaque-current-turn"}
    assert assistant.content == "Sunny"


@pytest.mark.asyncio
async def test_auto_compaction_preserves_reasoning_for_all_current_run_turns(
    tmp_path: Path,
) -> None:
    # Regression: the mid-run rebuild used to restore reasoning only for the
    # latest tool-continuation turn; earlier current-run turns lost theirs.
    class SecondCycleCompactionService:
        def __init__(self) -> None:
            self.checks = 0
            self.compacted = False

        def estimate_messages_tokens(self, _messages: list[JsonObject]) -> int:
            return 90

        def should_auto_compact(
            self,
            _input_tokens: int,
            _context_window: int,
            _threshold: float,
        ) -> bool:
            if self.compacted:
                return False
            self.checks += 1
            return self.checks == 2

        async def compact(
            self,
            messages: list[ChatMessage],
            *,
            agent: Any,
            summary_adapter: Any,
            summary_model_id: str,
            storage: Any,
            settings: Any,
            **kwargs: Any,
        ) -> ChatMessage:
            del agent, summary_adapter, summary_model_id, storage, settings

            self.compacted = True
            tail_user = next(
                message
                for message in messages
                if message.role == "user" and message.content == "Weather?"
            )
            return ChatMessage.compaction_checkpoint(
                summary="Compacted prior context.",
                projection=messages[messages.index(tail_user) :],
                compacted_token_count=42,
            )

    agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["get_weather"])
    adapter = StubAdapter(
        [
            {
                "content": None,
                "reasoning": "First step.",
                "reasoning_meta": {"signature": "one"},
                "usage": {"input_tokens": 11, "output_tokens": 7},
                "tool_calls": [
                    {"id": "call_one", "name": "get_weather", "arguments": {"city": "Berlin"}}
                ],
            },
            {
                "content": None,
                "reasoning": "Second step.",
                "reasoning_meta": {"signature": "two"},
                "usage": {"input_tokens": 13, "output_tokens": 9},
                "tool_calls": [
                    {"id": "call_two", "name": "get_weather", "arguments": {"city": "Hamburg"}}
                ],
            },
            {"content": "Sunny in both.", "tool_calls": None},
        ]
    )
    tools = ToolRegistry()
    tools.register(
        "get_weather",
        "Get weather.",
        {"type": "object"},
        lambda _context, arguments: tool_success({"temp": 22, "city": arguments["city"]}),
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        tools=tools,
        storage=StubStorage(
            {
                "auto": True,
                "threshold": 0.8,
                "tail_tokens": 15_000,
                "summary_model": None,
            }
        ),
        models=StubModels({("openai", "gpt-5.2"): 100}),
    )
    compaction_service = SecondCycleCompactionService()

    assistant = await build_chat_loop(
        runtime,
        compaction_service=cast(Any, compaction_service),
    ).send("coder", "Weather?", session_id="session-one")

    assert assistant.content == "Sunny in both."
    rebuilt = adapter.requests[2]["messages"]
    assert [message["role"] for message in rebuilt] == [
        "system",
        "user",
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
    ]
    assert rebuilt[1]["content"] == (
        "<system-reminder>\nCompacted prior context.\n\n"
        f"{HISTORY_COMPACTION_GUIDANCE.format(ordinal=1)}\n</system-reminder>"
    )
    # Both current-run assistant turns keep their reasoning after the rebuild,
    # not just the latest tool continuation.
    assert rebuilt[3]["reasoning"] == "First step."
    assert rebuilt[3]["reasoning_meta"] == {"signature": "one"}
    assert rebuilt[5]["reasoning"] == "Second step."
    assert rebuilt[5]["reasoning_meta"] == {"signature": "two"}
    assert "usage" not in rebuilt[3]
    assert "usage" not in rebuilt[5]
