"""Tests for chat integration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from core.prompts import SkillPromptRegistry
from core.runs import RUN_CHANGE_STATS_EVENT
from core.runtime import Runtime
from core.skills.skills import SkillRegistry
from core.tools import tool_success
from core.tools.memory import MEMORY_TOOL_DESCRIPTION, MEMORY_TOOL_PARAMETERS
from core.utils.config import Config
from tests.core.chat.chat_integration_test_support import (
    FakeAdapter,
    JsonObject,
)
from tests.core.chat.chat_integration_test_support import (
    resources_dir as resources_dir,
)
from tests.core.chat.chat_loop_support import RecordingReflection, build_chat_loop, session_address


def _ok_tool_handler(_context: Any, _arguments: JsonObject) -> JsonObject:
    return tool_success({"content": "ok"})


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


def _write_skill(data_dir: Path, name: str, description: str) -> None:
    skill_dir = data_dir / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n",
        encoding="utf-8",
    )
