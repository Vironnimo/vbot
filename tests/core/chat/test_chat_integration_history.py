"""Tests for chat integration history."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.providers.reasoning import REASONING_REPLAY_FULL_HISTORY, ReasoningReplayPolicy
from core.runtime import Runtime
from core.utils.config import Config
from tests.core.chat.chat_integration_test_support import (
    FakeAdapter,
)
from tests.core.chat.chat_integration_test_support import (
    resources_dir as resources_dir,
)
from tests.core.chat.chat_loop_support import build_chat_loop, session_address


class FullHistoryFakeAdapter(FakeAdapter):
    """Fake adapter declaring the Anthropic-style full_history replay policy."""

    def reasoning_replay_policy(self, model_id: str) -> ReasoningReplayPolicy:
        del model_id
        return REASONING_REPLAY_FULL_HISTORY


RUN_ONE_REASONING_META = {
    "content_blocks": [
        {"type": "thinking", "thinking": "Run-one thinking", "signature": "sig-run-one"}
    ]
}


def _full_history_runtime(
    tmp_path: Path,
    resources_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter: FullHistoryFakeAdapter,
) -> Runtime:
    config = Config(data_dir=tmp_path / "data")
    config._data["RESOURCES_PATH"] = str(resources_dir)
    config._data["VBOT_VERSION"] = "test-version"
    runtime = Runtime(config)
    monkeypatch.setenv("FAKE_API_KEY", "test-key")
    monkeypatch.setattr(runtime, "get_adapter", lambda connection: adapter)
    return runtime


@pytest.mark.asyncio
async def test_full_history_adapter_replays_prior_run_reasoning_in_next_run(
    tmp_path: Path,
    resources_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FullHistoryFakeAdapter(
        [
            {
                "content": "First answer",
                "reasoning": "Run-one thinking",
                "reasoning_meta": RUN_ONE_REASONING_META,
                "tool_calls": None,
            },
            {"content": "Second answer", "tool_calls": None},
        ]
    )
    runtime = _full_history_runtime(tmp_path, resources_dir, monkeypatch, adapter)

    runtime.start()
    try:
        runtime.agents.create("coder", "Coder Agent", model="fake-provider/fake-model-v1")
        loop = build_chat_loop(runtime)

        await loop.send("coder", "Q1", session_id="session-one")
        await loop.send("coder", "Q2", session_id="session-one")

        second_request = adapter.requests[1].messages
        assert [message["role"] for message in second_request] == [
            "system",
            "user",
            "assistant",
            "user",
        ]
        prior_assistant = second_request[2]
        assert prior_assistant["reasoning"] == "Run-one thinking"
        assert prior_assistant["reasoning_meta"] == RUN_ONE_REASONING_META
        assert "usage" not in prior_assistant
        persisted = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
        assert persisted[1].reasoning_scope == "fake-provider/fake-model-v1::api-key"
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_full_history_adapter_strips_reasoning_after_model_switch(
    tmp_path: Path,
    resources_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FullHistoryFakeAdapter(
        [
            {
                "content": "First answer",
                "reasoning": "Run-one thinking",
                "reasoning_meta": RUN_ONE_REASONING_META,
                "tool_calls": None,
            },
            {"content": "Second answer", "tool_calls": None},
        ]
    )
    runtime = _full_history_runtime(tmp_path, resources_dir, monkeypatch, adapter)

    runtime.start()
    try:
        runtime.agents.create("coder", "Coder Agent", model="fake-provider/fake-model-v1")
        loop = build_chat_loop(runtime)

        await loop.send("coder", "Q1", session_id="session-one")
        runtime.agents.update("coder", model="fake-provider/fake-model-v2")
        await loop.send("coder", "Q2", session_id="session-one")

        second_request = adapter.requests[1].messages
        prior_assistant = second_request[2]
        assert prior_assistant["role"] == "assistant"
        assert prior_assistant["content"] == "First answer"
        assert "reasoning" not in prior_assistant
        assert "reasoning_meta" not in prior_assistant
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_textual_compaction_ends_full_history_reasoning_replay(
    tmp_path: Path,
    resources_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FullHistoryFakeAdapter([{"content": "Fresh answer", "tool_calls": None}])
    runtime = _full_history_runtime(tmp_path, resources_dir, monkeypatch, adapter)

    runtime.start()
    try:
        runtime.agents.create("coder", "Coder Agent", model="fake-provider/fake-model-v1")
        session = runtime.chat_sessions.create("coder", session_id="session-one")
        session.append(ChatMessage.user("Old question"))
        session.append(
            ChatMessage.assistant(model="fake-provider/fake-model-v1", content="Old answer")
        )
        tail_user = ChatMessage.user("Tail question")
        session.append(tail_user)
        tail_assistant = ChatMessage.assistant(
            model="fake-provider/fake-model-v1",
            content="Tail answer",
            reasoning="Tail thinking",
            reasoning_meta={
                "content_blocks": [
                    {"type": "thinking", "thinking": "Tail thinking", "signature": "sig-tail"}
                ]
            },
        )
        session.append(tail_assistant)
        session.append(
            ChatMessage.compaction_checkpoint(
                summary="Compacted summary",
                projection=[tail_user, tail_assistant],
                compacted_token_count=123,
            )
        )

        await build_chat_loop(runtime).send("coder", "Q3", session_id="session-one")

        request = adapter.requests[0].messages
        assert [message["role"] for message in request] == [
            "system",
            "user",
            "user",
            "assistant",
            "user",
        ]
        assert "Compacted summary" in request[1]["content"]
        request_tail_assistant = request[3]
        assert "reasoning" not in request_tail_assistant
        assert "reasoning_meta" not in request_tail_assistant
    finally:
        runtime.stop()
