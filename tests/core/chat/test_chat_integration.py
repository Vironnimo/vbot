"""The chat loop on a real Runtime: Provider wiring, the read Tool, change statistics and prompts.

These tests start the production Runtime on fake Provider resources and route its Model
requests to a recording adapter; the chat-loop behavior itself is covered by the
``test_chat_loop_*`` suites on stub runtimes.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, cast

import pytest

from core.prompts import SkillPromptRegistry
from core.runs import MODEL_STEP_USAGE_EVENT, RUN_CHANGE_STATS_EVENT
from core.skills.skills import SkillRegistry
from core.tools import tool_success
from core.tools.memory import MEMORY_TOOL_DESCRIPTION, MEMORY_TOOL_PARAMETERS
from tests.core.chat.chat_integration_test_support import FakeAdapter, JsonObject, StartRuntime
from tests.core.chat.chat_integration_test_support import resources_dir as resources_dir
from tests.core.chat.chat_integration_test_support import start_runtime as start_runtime
from tests.core.chat.chat_loop_support import RecordingReflection, build_chat_loop, history


def _ok_tool_handler(_context: Any, _arguments: JsonObject) -> JsonObject:
    return tool_success({"content": "ok"})


@pytest.mark.asyncio
async def test_agent_sends_message_and_persists_assistant_response(
    start_runtime: StartRuntime,
) -> None:
    adapter = FakeAdapter({"content": "assistant response", "reasoning": None, "tool_calls": None})

    with start_runtime(adapter) as runtime:
        runtime.agents.create(
            "coder", "Coder Agent", model="fake-provider/fake-model-v1", thinking_effort="high"
        )

        assistant = await build_chat_loop(runtime).send("coder", "Hello", session_id="session-one")

        messages = history(runtime)
        assert assistant.content == "assistant response"
        assert runtime.has_provider_credentials("fake-provider") is True
        assert runtime.get_provider_credentials("fake-provider") == "test-key"
        assert [message.role for message in messages] == ["user", "assistant", "run_summary"]
        assert messages[0].content == "Hello"
        assert messages[1].model == "fake-provider/fake-model-v1"
        assert messages[-1].iteration_count == 1
        request = adapter.requests[0]
        assert request.model_id == "fake-model-v1"
        assert (request.kwargs["thinking_effort"], request.kwargs["temperature"]) == ("high", None)
        assert [message["role"] for message in request.messages] == ["system", "user"]


@pytest.mark.asyncio
async def test_read_tool_batch_persists_each_result_and_counts_one_iteration_per_response(
    start_runtime: StartRuntime,
) -> None:
    adapter = FakeAdapter(
        [
            {
                "content": None,
                "reasoning": "Read both files together.",
                "tool_calls": [
                    {"id": "call_note", "name": "read", "arguments": {"path": "note.txt"}},
                    {"id": "call_missing", "name": "read", "arguments": {"path": "missing.txt"}},
                ],
            },
            {"content": "One file was missing.", "reasoning": "Compare.", "tool_calls": None},
        ]
    )

    with start_runtime(adapter) as runtime:
        reflection = RecordingReflection()
        agent = runtime.agents.create("coder", "Coder Agent", model="fake-provider/fake-model-v1")
        Path(agent.workspace).joinpath("note.txt").write_text("file content", encoding="utf-8")

        assistant = await build_chat_loop(runtime, reflection_service=reflection).send(
            "coder", "Read both", session_id="session-one"
        )

        messages = history(runtime)
        run = runtime.chat_run_manager.get(str(messages[-1].run_id))
        assert assistant.content == "One file was missing."
        assert [message.role for message in messages] == [
            "user",
            "assistant",
            "tool",
            "tool",
            "assistant",
            "run_summary",
        ]
        found, missing = (json.loads(str(message.content)) for message in messages[2:4])
        assert (found["ok"], found["data"], found["error"], found["artifacts"]) == (
            True,
            {"content": "1| file content"},
            None,
            [],
        )
        assert (missing["ok"], missing["data"], missing["artifacts"]) == (False, None, [])
        assert missing["error"]["code"] == "file_not_found"
        assert "missing.txt" in missing["error"]["message"]
        # The next request replays exactly the persisted Tool results.
        assert [m["content"] for m in adapter.requests[1].messages if m["role"] == "tool"] == [
            messages[2].content,
            messages[3].content,
        ]
        # Two Tool calls in one Model response still count as one iteration.
        assert (run.iteration_count, run.tool_call_count) == (2, 2)
        assert [
            event.payload["iteration_count"]
            for event in run.events
            if event.type == MODEL_STEP_USAGE_EVENT
        ] == [1, 2]
        assert messages[-1].status == "completed" and messages[-1].timing is not None
        assert messages[-1].iteration_count == 2
        assert reflection.calls[0]["iteration_count"] == 2


@pytest.mark.asyncio
async def test_change_stats_stream_after_each_tool_round_and_match_terminal(
    start_runtime: StartRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = FakeAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_write_a",
                        "name": "apply_patch",
                        "arguments": {"patch": "*** Add File: a.txt\n+one\n+two"},
                    }
                ],
            },
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_write_b",
                        "name": "apply_patch",
                        "arguments": {"patch": "*** Add File: b.txt\n+x"},
                    }
                ],
            },
            {"content": "Both files written.", "tool_calls": None},
        ]
    )

    with start_runtime(adapter) as runtime:
        agent = runtime.agents.create("coder", "Coder Agent", model="fake-provider/fake-model-v1")
        workspace = Path(agent.workspace)
        tracker = runtime.change_tracker
        threads: dict[str, list[int]] = {"peek_run_stats": [], "take_run_stats": []}
        for name, calls in threads.items():
            original = getattr(tracker, name)

            def recording(run_key: Any, original: Any = original, calls: list[int] = calls) -> Any:
                calls.append(threading.get_ident())
                return original(run_key)

            monkeypatch.setattr(tracker, name, recording)

        await build_chat_loop(runtime).send("coder", "Write files", session_id="session-one")

        # Live and terminal diffs run off the Event Loop; finalization computes once.
        assert [len(calls) for calls in threads.values()] == [2, 1]
        assert threading.get_ident() not in [*threads["peek_run_stats"], *threads["take_run_stats"]]
        messages = history(runtime)
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


def test_runtime_prompt_includes_workspace_files_and_filtered_tool_skill_metadata(
    start_runtime: StartRuntime,
) -> None:
    with start_runtime() as runtime:
        _write_skill(runtime.storage.data_dir, "agent-cli", "Delegate coding tasks")
        _write_skill(runtime.storage.data_dir, "news", "Fetch news")
        runtime._skills = SkillRegistry.load(runtime.storage.data_dir / "skills")
        runtime.system_prompts._skill_registry = cast(SkillPromptRegistry, runtime.skills)
        runtime.tools.register(
            "read_file", "Read a workspace file.", {"type": "object"}, _ok_tool_handler
        )
        runtime.tools.register(
            "shell", "Run a shell command.", {"type": "object"}, _ok_tool_handler
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
        assert "- shell:" not in prompt
        assert "- agent-cli: Delegate coding tasks" in prompt
        assert "news" not in prompt
        # skill and skill_manage are ordinary Tools that this Agent's selection filters out.
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


def _write_skill(data_dir: Path, name: str, description: str) -> None:
    skill_dir = data_dir / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n",
        encoding="utf-8",
    )
