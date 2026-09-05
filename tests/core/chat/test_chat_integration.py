"""Core chat loop integration tests."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat import ChatMessage
from core.prompts import SkillPromptRegistry
from core.providers.adapter import (
    IMAGE_WIRE_MEDIA_TYPES,
    TOOL_RESULT_CONTENT_BLOCKS_FIELD,
    ProviderAdapter,
)
from core.providers.reasoning import REASONING_REPLAY_FULL_HISTORY, ReasoningReplayPolicy
from core.runs import RUN_CHANGE_STATS_EVENT
from core.runtime import Runtime
from core.skills.skills import SkillRegistry
from core.tools import read_media_artifact, tool_success
from core.tools.memory import MEMORY_TOOL_DESCRIPTION, MEMORY_TOOL_PARAMETERS
from core.utils.config import Config
from tests.core.chat.chat_loop_support import RecordingReflection, build_chat_loop, session_address

JsonObject = dict[str, Any]


def _ok_tool_handler(_context: Any, _arguments: JsonObject) -> JsonObject:
    return tool_success({"content": "ok"})


@dataclass(frozen=True)
class CapturedRequest:
    messages: list[JsonObject]
    model_id: str
    kwargs: JsonObject


class FakeAdapter(ProviderAdapter):
    """Provider adapter test double that records canonical chat requests."""

    def __init__(self, response: JsonObject | list[JsonObject]) -> None:
        self.response = response
        self.requests: list[CapturedRequest] = []

    async def aclose(self) -> None:
        return None

    async def send(self, messages: list[dict], *, model_id: str, **kwargs: Any) -> dict:
        self.requests.append(
            CapturedRequest(messages=list(messages), model_id=model_id, kwargs=kwargs)
        )
        if isinstance(self.response, list):
            return self.response.pop(0)
        return self.response

    async def stream(
        self,
        messages: list[dict],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict]:
        raise NotImplementedError("streaming not implemented in this stub")
        yield {}

    def normalize_response(
        self, response: JsonObject, *, model_id: str | None = None
    ) -> JsonObject:
        return response

    def wire_media_support(self, model_id: str) -> frozenset[str]:
        del model_id
        return IMAGE_WIRE_MEDIA_TYPES


class FullHistoryFakeAdapter(FakeAdapter):
    """Fake adapter declaring the Anthropic-style full_history replay policy."""

    def reasoning_replay_policy(self, model_id: str) -> ReasoningReplayPolicy:
        del model_id
        return REASONING_REPLAY_FULL_HISTORY


@pytest.fixture
def resources_dir(tmp_path: Path) -> Path:
    resources = tmp_path / "resources"
    environment_template = resources / "data-dir" / ".env.example"
    environment_template.parent.mkdir(parents=True)
    environment_template.write_text("# Integration-test environment\n", encoding="utf-8")
    _write_provider_resource(resources)
    _write_model_resource(resources)
    _write_prompt_resources(resources)
    _write_workspace_templates(resources)
    return resources


@pytest.mark.asyncio
async def test_agent_sends_message_and_persists_assistant_response(
    tmp_path: Path,
    resources_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeAdapter({"content": "assistant response", "reasoning": None, "tool_calls": None})
    config = Config(data_dir=tmp_path / "data")
    config._data["RESOURCES_PATH"] = str(resources_dir)
    config._data["VBOT_VERSION"] = "test-version"
    runtime = Runtime(config)
    monkeypatch.setenv("FAKE_API_KEY", "test-key")
    monkeypatch.setattr(runtime, "get_adapter", lambda connection: adapter)

    runtime.start()
    try:
        runtime.agents.create(
            "coder",
            "Coder Agent",
            model="fake-provider/fake-model-v1",
            thinking_effort="high",
        )

        assistant = await build_chat_loop(runtime).send("coder", "Hello", session_id="session-one")

        messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
        assert assistant.content == "assistant response"
        assert runtime.has_provider_credentials("fake-provider") is True
        assert runtime.get_provider_credentials("fake-provider") == "test-key"
        assert [message.role for message in messages] == ["user", "assistant", "run_summary"]
        assert messages[0].content == "Hello"
        assert messages[1].model == "fake-provider/fake-model-v1"
        assert messages[1].content == "assistant response"
        assert messages[-1].iteration_count == 1
        assert adapter.requests[0].model_id == "fake-model-v1"
        assert adapter.requests[0].kwargs["thinking_effort"] == "high"
        assert adapter.requests[0].kwargs["temperature"] is None
        assert [message["role"] for message in adapter.requests[0].messages] == ["system", "user"]
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_read_tool_success_persists_result_and_final_response_uses_content(
    tmp_path: Path,
    resources_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_read", "name": "read", "arguments": {"path": "note.txt"}}
                ],
            },
            {"content": "I read: file content", "tool_calls": None},
        ]
    )
    config = Config(data_dir=tmp_path / "data")
    config._data["RESOURCES_PATH"] = str(resources_dir)
    config._data["VBOT_VERSION"] = "test-version"
    runtime = Runtime(config)
    monkeypatch.setenv("FAKE_API_KEY", "test-key")
    monkeypatch.setattr(runtime, "get_adapter", lambda connection: adapter)

    runtime.start()
    try:
        agent = runtime.agents.create(
            "coder",
            "Coder Agent",
            model="fake-provider/fake-model-v1",
        )
        Path(agent.workspace).joinpath("note.txt").write_text("file content", encoding="utf-8")

        assistant = await build_chat_loop(runtime).send(
            "coder", "Read note", session_id="session-one"
        )

        messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
        tool_message_content = messages[2].content
        assert isinstance(tool_message_content, str)
        tool_result = json.loads(tool_message_content)
        assert assistant.content == "I read: file content"
        assert [message.role for message in messages] == [
            "user",
            "assistant",
            "tool",
            "assistant",
            "run_summary",
        ]
        assert messages[-1].status == "completed"
        assert messages[-1].timing is not None
        assert messages[-1].iteration_count == 2
        assert tool_result["ok"] is True
        assert tool_result["error"] is None
        assert tool_result["data"] == {"content": "1| file content"}
        assert tool_result["artifacts"] == []
        assert adapter.requests[1].messages[3]["content"] == messages[2].content
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_parallel_tool_calls_count_one_iteration_per_model_response(
    tmp_path: Path,
    resources_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeAdapter(
        [
            {
                "content": None,
                "reasoning": "Read all five files together.",
                "tool_calls": [
                    {
                        "id": f"call_read_{index}",
                        "name": "read",
                        "arguments": {"path": "note.txt"},
                    }
                    for index in range(5)
                ],
            },
            {
                "content": "All five reads completed.",
                "reasoning": "The results agree.",
                "tool_calls": None,
            },
        ]
    )
    config = Config(data_dir=tmp_path / "data")
    config._data["RESOURCES_PATH"] = str(resources_dir)
    config._data["VBOT_VERSION"] = "test-version"
    runtime = Runtime(config)
    monkeypatch.setenv("FAKE_API_KEY", "test-key")
    monkeypatch.setattr(runtime, "get_adapter", lambda connection: adapter)

    runtime.start()
    try:
        reflection = RecordingReflection()
        agent = runtime.agents.create(
            "coder",
            "Coder Agent",
            model="fake-provider/fake-model-v1",
        )
        Path(agent.workspace).joinpath("note.txt").write_text("same", encoding="utf-8")

        await build_chat_loop(runtime, reflection_service=reflection).send(
            "coder", "Read this five times", session_id="session-one"
        )

        messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
        run = runtime.chat_run_manager.get(str(messages[-1].run_id))
        live_counts = [
            event.payload["iteration_count"]
            for event in run.events
            if event.type == "model_step_usage"
        ]
        assert len(adapter.requests) == 2
        assert run.iteration_count == 2
        assert run.tool_call_count == 5
        assert messages[-1].iteration_count == 2
        assert live_counts == [1, 2]
        assert run.events[-1].payload["iteration_count"] == 2
        assert reflection.calls[0]["iteration_count"] == 2
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_change_stats_stream_after_each_tool_round_and_match_terminal(
    tmp_path: Path,
    resources_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_write_a",
                        "name": "write",
                        "arguments": {"path": "a.txt", "content": "one\ntwo\n"},
                    }
                ],
            },
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_write_b",
                        "name": "write",
                        "arguments": {"path": "b.txt", "content": "x\n"},
                    }
                ],
            },
            {"content": "Both files written.", "tool_calls": None},
        ]
    )
    config = Config(data_dir=tmp_path / "data")
    config._data["RESOURCES_PATH"] = str(resources_dir)
    config._data["VBOT_VERSION"] = "test-version"
    runtime = Runtime(config)
    monkeypatch.setenv("FAKE_API_KEY", "test-key")
    monkeypatch.setattr(runtime, "get_adapter", lambda connection: adapter)

    runtime.start()
    try:
        agent = runtime.agents.create(
            "coder",
            "Coder Agent",
            model="fake-provider/fake-model-v1",
        )
        workspace = Path(agent.workspace)

        await build_chat_loop(runtime).send("coder", "Write files", session_id="session-one")

        messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
        run = runtime.chat_run_manager.get(str(messages[-1].run_id))
        live_stats = [
            event.payload["change_stats"]
            for event in run.events
            if event.type == RUN_CHANGE_STATS_EVENT
        ]

        assert live_stats == [
            {"files": 1, "added": 2, "removed": 0, "paths": [str(workspace / "a.txt")]},
            {
                "files": 2,
                "added": 3,
                "removed": 0,
                "paths": [str(workspace / "a.txt"), str(workspace / "b.txt")],
            },
        ]
        assert run.terminal_payload_extras["change_stats"] == live_stats[-1]
        assert messages[-1].change_stats == live_stats[-1]
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_reasoning_only_response_requests_visible_continuation(
    tmp_path: Path,
    resources_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeAdapter(
        [
            {"content": None, "reasoning": "Only thinking this time.", "tool_calls": None},
            {"content": "Visible answer.", "reasoning": None, "tool_calls": None},
        ]
    )
    config = Config(data_dir=tmp_path / "data")
    config._data["RESOURCES_PATH"] = str(resources_dir)
    config._data["VBOT_VERSION"] = "test-version"
    runtime = Runtime(config)
    monkeypatch.setenv("FAKE_API_KEY", "test-key")
    monkeypatch.setattr(runtime, "get_adapter", lambda connection: adapter)

    runtime.start()
    try:
        runtime.agents.create(
            "coder",
            "Coder Agent",
            model="fake-provider/fake-model-v1",
        )

        await build_chat_loop(runtime).send(
            "coder", "Think without answering", session_id="session-one"
        )

        messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
        run = runtime.chat_run_manager.get(str(messages[-1].run_id))
        assert len(adapter.requests) == 2
        assert messages[1].reasoning == "Only thinking this time."
        assert messages[1].content is None
        assert messages[1].interrupted is True
        assistant_messages = [message for message in messages if message.role == "assistant"]
        assert assistant_messages[-1].content == "Visible answer."
        assert run.iteration_count == 2
        assert messages[-1].iteration_count == 2
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_read_tool_missing_file_persists_failure_and_run_recovers(
    tmp_path: Path,
    resources_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_missing", "name": "read", "arguments": {"path": "missing.txt"}}
                ],
            },
            {"content": "The file was missing, so I recovered.", "tool_calls": None},
        ]
    )
    config = Config(data_dir=tmp_path / "data")
    config._data["RESOURCES_PATH"] = str(resources_dir)
    config._data["VBOT_VERSION"] = "test-version"
    runtime = Runtime(config)
    monkeypatch.setenv("FAKE_API_KEY", "test-key")
    monkeypatch.setattr(runtime, "get_adapter", lambda connection: adapter)

    runtime.start()
    try:
        runtime.agents.create(
            "coder",
            "Coder Agent",
            model="fake-provider/fake-model-v1",
        )

        assistant = await build_chat_loop(runtime).send(
            "coder", "Read missing", session_id="session-one"
        )

        messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
        tool_message_content = messages[2].content
        assert isinstance(tool_message_content, str)
        tool_result = json.loads(tool_message_content)
        assert assistant.content == "The file was missing, so I recovered."
        assert [message.role for message in messages] == [
            "user",
            "assistant",
            "tool",
            "assistant",
            "run_summary",
        ]
        assert messages[-1].status == "completed"
        assert messages[-1].timing is not None
        assert tool_result["ok"] is False
        assert tool_result["error"]["code"] == "file_not_found"
        assert "missing.txt" in tool_result["error"]["message"]
        assert tool_result["data"] is None
        assert tool_result["artifacts"] == []
        assert adapter.requests[1].messages[3]["content"] == messages[2].content
    finally:
        runtime.stop()


_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _tool_result_content_parts(messages: list[JsonObject]) -> list[JsonObject]:
    """Return every Run-local rich content part across Tool Results."""
    return [
        part
        for message in messages
        if message.get("role") == "tool"
        and isinstance(message.get(TOOL_RESULT_CONTENT_BLOCKS_FIELD), list)
        for part in message[TOOL_RESULT_CONTENT_BLOCKS_FIELD]
        if isinstance(part, dict)
    ]


@pytest.mark.asyncio
async def test_read_image_returns_run_local_base64_in_tool_result_for_vision_model(
    tmp_path: Path,
    resources_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_read", "name": "read", "arguments": {"path": "diagram.png"}}
                ],
            },
            {"content": "I can see the diagram.", "tool_calls": None},
        ]
    )
    config = Config(data_dir=tmp_path / "data")
    config._data["RESOURCES_PATH"] = str(resources_dir)
    config._data["VBOT_VERSION"] = "test-version"
    runtime = Runtime(config)
    monkeypatch.setenv("FAKE_API_KEY", "test-key")
    monkeypatch.setattr(runtime, "get_adapter", lambda connection: adapter)

    runtime.start()
    try:
        agent = runtime.agents.create(
            "coder", "Coder Agent", model="fake-provider/fake-model-vision"
        )
        Path(agent.workspace).joinpath("diagram.png").write_bytes(_PNG_BYTES)

        assistant = await runtime.chat_loop.send(
            "coder", "Look at diagram.png", session_id="session-one"
        )

        assert assistant.content == "I can see the diagram."

        # The follow-up provider request carries the image on its correlated
        # Tool Result, without fabricating another user turn.
        tool_result_parts = _tool_result_content_parts(adapter.requests[1].messages)
        media_parts = [part for part in tool_result_parts if part.get("type") == "media"]
        assert len(media_parts) == 1
        assert media_parts[0]["media_type"] == "image/png"
        assert media_parts[0]["base64"]

        # The canonical Session persists only the original user turn and the
        # compact Tool envelope; request-only base64 never reaches history.
        messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
        assert [message.role for message in messages] == [
            "user",
            "assistant",
            "tool",
            "assistant",
            "run_summary",
        ]
        assert all(
            not isinstance(message.content, list) for message in messages if message.role == "user"
        )
        persisted = json.dumps([message.to_dict() for message in messages])
        assert "base64" not in persisted

        assert isinstance(adapter.response, list)
        adapter.response.append({"content": "The image is no longer active.", "tool_calls": None})
        await runtime.chat_loop.send(
            "coder",
            "Continue without reopening it.",
            session_id="session-one",
        )
        next_run_messages = adapter.requests[2].messages
        assert all(TOOL_RESULT_CONTENT_BLOCKS_FIELD not in message for message in next_run_messages)
        assert "base64" not in json.dumps(next_run_messages)
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_long_mixed_image_run_is_bounded_and_can_reopen_old_images(
    tmp_path: Path,
    resources_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames = [_PNG_BYTES + bytes([index]) * 1_400_000 for index in range(14)]
    responses: list[JsonObject] = [
        {
            "content": f"inspection-{index}",
            "tool_calls": [
                {
                    "id": f"call-{index}",
                    "name": "read" if index % 2 == 0 else "mcp_capture",
                    "arguments": {"path": f"frame-{index}.png"}
                    if index % 2 == 0
                    else {"index": index},
                }
            ],
        }
        for index in range(14)
    ]
    responses.extend(
        [
            {
                "content": "compare original",
                "tool_calls": [
                    {
                        "id": "reopen",
                        "name": "read",
                        "arguments": {"path": "frame-0.png"},
                    }
                ],
            },
            {"content": "done", "tool_calls": None},
        ]
    )
    rebuilt_requests: list[list[JsonObject]] = []

    class RebuildingAdapter(FakeAdapter):
        async def send(self, messages: list[dict], *, model_id: str, **kwargs: Any) -> dict:
            if len(self.requests) == 10:
                session = runtime.chat_sessions.get(session_address("coder", "session-one"))
                rebuilt_requests.append(
                    await runtime.chat_loop._build_request_messages(
                        agent,
                        session,
                        input_modalities=frozenset({"text", "image"}),
                        wire_media_types=IMAGE_WIRE_MEDIA_TYPES,
                    )
                )
            return await super().send(messages, model_id=model_id, **kwargs)

    adapter = RebuildingAdapter(responses)
    config = Config(data_dir=tmp_path / "data")
    config._data["RESOURCES_PATH"] = str(resources_dir)
    config._data["VBOT_VERSION"] = "test-version"
    runtime = Runtime(config)
    monkeypatch.setenv("FAKE_API_KEY", "test-key")
    monkeypatch.setattr(runtime, "get_adapter", lambda connection: adapter)
    runtime.start()
    try:
        # Exercise the exact shared artifact contract used by MCP binary results,
        # alternating with the real read Tool; no external Blender process needed.
        def capture(_context: Any, arguments: JsonObject) -> JsonObject:
            index = arguments["index"]
            record = runtime.attachment_store.store(f"frame-{index}.png", frames[index])
            return tool_success(
                {"frame": index},
                artifacts=[
                    read_media_artifact(
                        attachment_id=record.id,
                        filename=record.filename,
                        media_type=record.media_type,
                    )
                ],
            )

        runtime.tools.register(
            "mcp_capture",
            "Capture a test frame.",
            {
                "type": "object",
                "properties": {"index": {"type": "integer"}},
                "required": ["index"],
                "additionalProperties": False,
            },
            capture,
        )
        agent = runtime.agents.create("coder", "Coder", model="fake-provider/fake-model-vision")
        for index, frame in enumerate(frames):
            Path(agent.workspace).joinpath(f"frame-{index}.png").write_bytes(frame)
        assistant = await runtime.chat_loop.send(
            "coder", "Inspect successive frames", session_id="session-one"
        )
        assert assistant.content == "done"
        assert len(adapter.requests) == 16
        for iteration, request in enumerate(adapter.requests):
            images = [
                part
                for part in _tool_result_content_parts(request.messages)
                if part["type"] == "media"
            ]
            expected_indices = (
                list(range(max(0, iteration - 3), iteration)) if iteration <= 14 else [12, 13, 0]
            )
            assert [base64.b64decode(part["base64"]) for part in images] == [
                frames[index] for index in expected_indices
            ]
            assert sum(len(part["base64"]) for part in images) <= 14 * 1024 * 1024
            assert len(json.dumps(request.messages).encode()) < 14 * 1024 * 1024
            for index in range(iteration):
                if index < 14:
                    assert any(
                        message.get("content") == f"inspection-{index}"
                        for message in request.messages
                    )
        rebuilt_images = [
            part
            for part in _tool_result_content_parts(rebuilt_requests[0])
            if part["type"] == "media"
        ]
        assert [base64.b64decode(part["base64"]) for part in rebuilt_images] == frames[7:10]
        session = runtime.chat_sessions.get(session_address("coder", "session-one"))
        persisted = session.load()
        assert "base64" not in json.dumps([message.to_dict() for message in persisted])
        assert len([message for message in persisted if message.role == "tool"]) == 15
        for message in persisted:
            if message.role == "tool":
                assert isinstance(message.content, str)
                artifact = json.loads(message.content)["artifacts"][0]
                record = runtime.attachment_store.get(artifact["attachment_id"])
                assert Path(record.file_path).read_bytes() in frames
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_read_image_degrades_to_note_for_non_vision_model(
    tmp_path: Path,
    resources_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_read", "name": "read", "arguments": {"path": "diagram.png"}}
                ],
            },
            {"content": "I cannot view the image directly.", "tool_calls": None},
        ]
    )
    config = Config(data_dir=tmp_path / "data")
    config._data["RESOURCES_PATH"] = str(resources_dir)
    config._data["VBOT_VERSION"] = "test-version"
    runtime = Runtime(config)
    monkeypatch.setenv("FAKE_API_KEY", "test-key")
    monkeypatch.setattr(runtime, "get_adapter", lambda connection: adapter)

    runtime.start()
    try:
        agent = runtime.agents.create(
            "coder",
            "Coder Agent",
            model="fake-provider/fake-model-v1",
            tool_access={"mode": "selected", "allowed": ["read"]},
        )
        Path(agent.workspace).joinpath("diagram.png").write_bytes(_PNG_BYTES)

        # The run must complete without raising even though the model lacks vision.
        assistant = await runtime.chat_loop.send(
            "coder", "Look at diagram.png", session_id="session-one"
        )

        assert assistant.content == "I cannot view the image directly."

        # No base64 image part reaches the non-vision provider; the correlated
        # Tool Result receives a path-bearing capability note instead.
        tool_result_parts = _tool_result_content_parts(adapter.requests[1].messages)
        assert all(part.get("type") != "media" for part in tool_result_parts)
        note = next(
            part
            for part in tool_result_parts
            if part.get("type") == "text" and "no vision capability" in str(part.get("text"))
        )
        assert "diagram.png" in note["text"]

        # No synthetic user message is persisted for the fallback either.
        messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
        assert [message.role for message in messages] == [
            "user",
            "assistant",
            "tool",
            "assistant",
            "run_summary",
        ]
        assert all(
            not isinstance(message.content, list) for message in messages if message.role == "user"
        )
    finally:
        runtime.stop()


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


def test_runtime_prompt_includes_workspace_files_and_filtered_tool_skill_metadata(
    tmp_path: Path,
    resources_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config(data_dir=tmp_path / "data")
    config._data["RESOURCES_PATH"] = str(resources_dir)
    config._data["VBOT_VERSION"] = "test-version"
    runtime = Runtime(config)

    runtime.start()
    try:
        _write_skill(runtime.storage.data_dir, "agent-cli", "Delegate coding tasks")
        _write_skill(runtime.storage.data_dir, "news", "Fetch news")
        runtime._skills = SkillRegistry.load(runtime.storage.data_dir / "skills")
        runtime.system_prompts._skill_registry = cast(SkillPromptRegistry, runtime.skills)
        runtime.tools.register(
            "read_file",
            "Read a workspace file.",
            {"type": "object"},
            _ok_tool_handler,
        )
        runtime.tools.register(
            "shell",
            "Run a shell command.",
            {"type": "object"},
            _ok_tool_handler,
        )
        agent = runtime.agents.create(
            "coder",
            "Coder Agent",
            model="fake-provider/fake-model-v1",
            tool_access={"mode": "selected", "allowed": ["read_file"]},
            allowed_skills=["agent-cli"],
        )

        prompt = runtime.system_prompts.build_system_prompt(agent)
        tool_definitions = runtime.system_prompts.provider_tool_definitions(agent)

        assert "Soul template for integration" in prompt
        assert "Version test-version" in prompt
        # Memory files are lazy: with nothing written yet, the user scope renders its
        # heading label and the empty-scope placeholder rather than a seeded template.
        assert "# User Profile" in prompt
        assert "No entries yet." in prompt
        assert "- read_file: Read a workspace file." in prompt
        assert "shell" not in prompt
        assert "<name>agent-cli</name>" in prompt
        assert "Delegate coding tasks" in prompt
        assert "news" not in prompt
        assert tool_definitions == [
            {
                "name": "memory",
                "description": MEMORY_TOOL_DESCRIPTION,
                "parameters": MEMORY_TOOL_PARAMETERS,
            },
            {
                "name": "read_file",
                "description": "Read a workspace file.",
                "parameters": {"type": "object"},
            },
        ]
        # skill/skill_manage are ordinary tools now: this agent allows only read_file,
        # so neither is offered — the per-agent toggle filters them like any tool.
        offered_names = {definition["name"] for definition in tool_definitions}
        assert "skill" not in offered_names
        assert "skill_manage" not in offered_names
    finally:
        runtime.stop()


def _write_provider_resource(resources: Path) -> None:
    providers_dir = resources / "providers"
    providers_dir.mkdir(parents=True)
    (providers_dir / "fake.json").write_text(
        _json_dump(
            {
                "id": "fake-provider",
                "name": "Fake Provider",
                "adapter": "openai_compatible",
                "base_url": "https://fake-provider.example/v1",
                "connections": [
                    {
                        "id": "api-key",
                        "type": "api_key",
                        "label": "API Key",
                        "auth": {
                            "header": "Authorization",
                            "prefix": "Bearer ",
                            "credential_key": "FAKE_API_KEY",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _write_model_resource(resources: Path) -> None:
    models_dir = resources / "models"
    models_dir.mkdir(parents=True)
    (models_dir / "fake-provider.json").write_text(
        _json_dump(
            {
                "provider_id": "fake-provider",
                "models": {
                    "fake-model-v1": {
                        "name": "Fake Model",
                        "capabilities": {
                            "vision": False,
                            "tools": True,
                            "json_mode": True,
                            "reasoning": {"supported": True},
                        },
                        "context_window": 4096,
                        "max_output_tokens": 1024,
                    },
                    "fake-model-v2": {
                        "name": "Fake Model Two",
                        "capabilities": {
                            "vision": False,
                            "tools": True,
                            "json_mode": True,
                            "reasoning": {"supported": True},
                        },
                        "context_window": 4096,
                        "max_output_tokens": 1024,
                    },
                    "fake-model-vision": {
                        "name": "Fake Vision Model",
                        "capabilities": {
                            "vision": True,
                            "tools": True,
                            "json_mode": True,
                            "reasoning": {"supported": True},
                        },
                        "context_window": 16_384,
                        "max_output_tokens": 1024,
                    },
                },
            }
        ),
        encoding="utf-8",
    )


def _write_prompt_resources(resources: Path) -> None:
    # Block-model resources: the core text blocks read their default text from these
    # files (the tool/channel/skill lists are {generated:…} producers now); SOUL and
    # memory render through their own blocks. The per-scope layout is the assembly
    # driver — there is no root fragment.
    prompts_dir = resources / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "runtime.md").write_text(
        "OS {operating_system}\nModel {model}\nThinking {thinking_effort}\n"
        "Date {current_local_date}\nZone {timezone}",
        encoding="utf-8",
    )
    (prompts_dir / "identity_runtime.md").write_text(
        "Version {vbot_version}\nIdentity Workspace {identity_workspace}\n"
        "Host {server_hostname}\nRoot {vbot_root}\nData {data_root}",
        encoding="utf-8",
    )
    (prompts_dir / "working_project.md").write_text(
        "Project {project_name} ({project_id})\nWorkspace {project_workspace}\n{project_files}",
        encoding="utf-8",
    )
    (prompts_dir / "tools.md").write_text("Tools\n{generated:tool_list}", encoding="utf-8")
    # Ships disabled by default; must exist because the block definitions read it.
    (prompts_dir / "tools_list.md").write_text("Tool list\n{generated:tool_list}", encoding="utf-8")
    (prompts_dir / "channels.md").write_text("Channels\n{generated:channel_list}", encoding="utf-8")
    (prompts_dir / "skills.md").write_text("Skills\n{generated:skill_catalog}", encoding="utf-8")
    (prompts_dir / "skill_maintenance.md").write_text("Skill maintenance", encoding="utf-8")
    (prompts_dir / "compaction.md").write_text("Summarize the conversation.", encoding="utf-8")
    # Backend-only fragments (like compaction), read by Reflection Runs.
    (prompts_dir / "reflect-memory.md").write_text(
        "Review this session for memory updates.", encoding="utf-8"
    )
    (prompts_dir / "reflect-skill.md").write_text(
        "Review this session for skill updates.", encoding="utf-8"
    )
    (prompts_dir / "reflect.md").write_text("Review this session.", encoding="utf-8")


def _write_workspace_templates(resources: Path) -> None:
    # Only SOUL.md is a workspace template now; USER.md/MEMORY.md belong to the memory
    # system and are created lazily on first write, never seeded.
    templates_dir = resources / "workspace-templates"
    templates_dir.mkdir(parents=True)
    (templates_dir / "SOUL.md").write_text("Soul template for integration", encoding="utf-8")


def _write_skill(data_dir: Path, name: str, description: str) -> None:
    skill_dir = data_dir / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n",
        encoding="utf-8",
    )


def _json_dump(data: JsonObject) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
