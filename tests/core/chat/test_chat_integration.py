"""Core chat loop integration tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat import ChatLoop, ChatMessage
from core.chat.content_blocks import MediaBlock
from core.prompts import SkillPromptRegistry
from core.providers.adapter import IMAGE_WIRE_MEDIA_TYPES, ProviderAdapter
from core.providers.reasoning import REASONING_REPLAY_FULL_HISTORY, ReasoningReplayPolicy
from core.runtime import Runtime
from core.skills.skills import SkillRegistry
from core.tools import tool_success
from core.tools.memory import MEMORY_TOOL_DESCRIPTION
from core.utils.config import Config

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
    config._data["APP_VERSION"] = "test-version"
    runtime = Runtime(config)
    monkeypatch.setenv("FAKE_API_KEY", "test-key")
    monkeypatch.setattr(runtime, "get_adapter", lambda provider_id, connection_id: adapter)

    runtime.start()
    try:
        runtime.agents.create(
            "coder",
            "Coder Agent",
            model="fake-provider/fake-model-v1",
            thinking_effort="high",
        )

        assistant = await ChatLoop(runtime).send("coder", "Hello", session_id="session-one")

        messages = runtime.chat_sessions.get("coder", "session-one").load()
        assert assistant.content == "assistant response"
        assert runtime.has_provider_credentials("fake-provider") is True
        assert runtime.get_provider_credentials("fake-provider") == "test-key"
        assert [message.role for message in messages] == ["user", "assistant", "run_summary"]
        assert messages[0].content == "Hello"
        assert messages[1].model == "fake-provider/fake-model-v1"
        assert messages[1].content == "assistant response"
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
    config._data["APP_VERSION"] = "test-version"
    runtime = Runtime(config)
    monkeypatch.setenv("FAKE_API_KEY", "test-key")
    monkeypatch.setattr(runtime, "get_adapter", lambda provider_id, connection_id: adapter)

    runtime.start()
    try:
        agent = runtime.agents.create(
            "coder",
            "Coder Agent",
            model="fake-provider/fake-model-v1",
        )
        Path(agent.workspace).joinpath("note.txt").write_text("file content", encoding="utf-8")

        assistant = await ChatLoop(runtime).send("coder", "Read note", session_id="session-one")

        messages = runtime.chat_sessions.get("coder", "session-one").load()
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
        assert tool_result["ok"] is True
        assert tool_result["error"] is None
        assert tool_result["data"] == {"content": "1|file content"}
        assert tool_result["artifacts"] == []
        assert adapter.requests[1].messages[3]["content"] == messages[2].content
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
    config._data["APP_VERSION"] = "test-version"
    runtime = Runtime(config)
    monkeypatch.setenv("FAKE_API_KEY", "test-key")
    monkeypatch.setattr(runtime, "get_adapter", lambda provider_id, connection_id: adapter)

    runtime.start()
    try:
        runtime.agents.create(
            "coder",
            "Coder Agent",
            model="fake-provider/fake-model-v1",
        )

        assistant = await ChatLoop(runtime).send("coder", "Read missing", session_id="session-one")

        messages = runtime.chat_sessions.get("coder", "session-one").load()
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


def _user_media_parts(messages: list[JsonObject]) -> list[JsonObject]:
    """Return every resolved ``media`` part across user messages in a request."""
    return [
        part
        for message in messages
        if message.get("role") == "user" and isinstance(message.get("content"), list)
        for part in message["content"]
        if isinstance(part, dict) and part.get("type") == "media"
    ]


@pytest.mark.asyncio
async def test_read_image_injects_base64_for_vision_model(
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
    config._data["APP_VERSION"] = "test-version"
    runtime = Runtime(config)
    monkeypatch.setenv("FAKE_API_KEY", "test-key")
    monkeypatch.setattr(runtime, "get_adapter", lambda provider_id, connection_id: adapter)

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

        # The follow-up provider request carries the image as a base64 media part
        # in a synthetic current-turn user message.
        media_parts = _user_media_parts(adapter.requests[1].messages)
        assert len(media_parts) == 1
        assert media_parts[0]["media_type"] == "image/png"
        assert media_parts[0]["base64"]

        # The persisted session stores only a small MediaBlock reference, never base64.
        messages = runtime.chat_sessions.get("coder", "session-one").load()
        injected = next(
            message
            for message in messages
            if message.role == "user" and isinstance(message.content, list)
        )
        assert isinstance(injected.content, list)
        assert isinstance(injected.content[0], MediaBlock)
        assert injected.content[0].media_type == "image/png"
        persisted = json.dumps([message.to_dict() for message in messages])
        assert "base64" not in persisted
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
    config._data["APP_VERSION"] = "test-version"
    runtime = Runtime(config)
    monkeypatch.setenv("FAKE_API_KEY", "test-key")
    monkeypatch.setattr(runtime, "get_adapter", lambda provider_id, connection_id: adapter)

    runtime.start()
    try:
        agent = runtime.agents.create("coder", "Coder Agent", model="fake-provider/fake-model-v1")
        Path(agent.workspace).joinpath("diagram.png").write_bytes(_PNG_BYTES)

        # The run must complete without raising even though the model lacks vision.
        assistant = await runtime.chat_loop.send(
            "coder", "Look at diagram.png", session_id="session-one"
        )

        assert assistant.content == "I cannot view the image directly."

        # No base64 image part reaches the non-vision provider; a text note does.
        assert _user_media_parts(adapter.requests[1].messages) == []
        note = next(
            message
            for message in adapter.requests[1].messages
            if message.get("role") == "user"
            and isinstance(message.get("content"), str)
            and "cannot be shown" in message["content"]
        )
        assert "diagram.png" in note["content"]

        # The MediaBlock is still persisted (so a later run degrades it to a path note).
        messages = runtime.chat_sessions.get("coder", "session-one").load()
        injected = next(
            message
            for message in messages
            if message.role == "user" and isinstance(message.content, list)
        )
        assert isinstance(injected.content, list)
        assert isinstance(injected.content[0], MediaBlock)
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
    config._data["APP_VERSION"] = "test-version"
    runtime = Runtime(config)
    monkeypatch.setenv("FAKE_API_KEY", "test-key")
    monkeypatch.setattr(runtime, "get_adapter", lambda provider_id, connection_id: adapter)
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
        loop = ChatLoop(runtime)

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
        loop = ChatLoop(runtime)

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
async def test_full_history_adapter_replays_reasoning_for_compaction_tail_turns(
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
        session.append(
            ChatMessage.assistant(
                model="fake-provider/fake-model-v1",
                content="Tail answer",
                reasoning="Tail thinking",
                reasoning_meta={
                    "content_blocks": [
                        {"type": "thinking", "thinking": "Tail thinking", "signature": "sig-tail"}
                    ]
                },
            )
        )
        session.append(
            ChatMessage.compaction_checkpoint(
                summary="Compacted summary",
                tail_boundary_id=tail_user.id,
                compacted_token_count=123,
            )
        )

        await ChatLoop(runtime).send("coder", "Q3", session_id="session-one")

        request = adapter.requests[0].messages
        assert [message["role"] for message in request] == [
            "system",
            "user",
            "user",
            "assistant",
            "user",
        ]
        assert "Compacted summary" in request[1]["content"]
        tail_assistant = request[3]
        assert tail_assistant["reasoning"] == "Tail thinking"
        assert tail_assistant["reasoning_meta"] == {
            "content_blocks": [
                {"type": "thinking", "thinking": "Tail thinking", "signature": "sig-tail"}
            ]
        }
    finally:
        runtime.stop()


def test_runtime_prompt_includes_workspace_files_and_filtered_tool_skill_metadata(
    tmp_path: Path,
    resources_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config(data_dir=tmp_path / "data")
    config._data["RESOURCES_PATH"] = str(resources_dir)
    config._data["APP_VERSION"] = "test-version"
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
            allowed_tools=["read_file"],
            allowed_skills=["agent-cli"],
        )

        prompt = runtime.system_prompts.build_system_prompt(agent)
        tool_definitions = runtime.system_prompts.provider_tool_definitions(agent)

        assert "Soul template for integration" in prompt
        assert "Version test-version" in prompt
        assert "User template for integration" in prompt
        assert "- read_file: Read a workspace file." in prompt
        assert "shell" not in prompt
        assert "<name>agent-cli</name>" in prompt
        assert "Delegate coding tasks" in prompt
        assert "news" not in prompt
        assert tool_definitions == [
            {
                "name": "read_file",
                "description": "Read a workspace file.",
                "parameters": {"type": "object"},
            },
            {
                "name": "memory",
                "description": MEMORY_TOOL_DESCRIPTION,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["list", "add", "replace", "remove"],
                            "description": "Memory operation to perform.",
                        },
                        "scope": {
                            "type": "string",
                            "enum": ["user", "agent"],
                            "description": (
                                "Pinned memory file to operate on: user=USER.md, agent=MEMORY.md."
                            ),
                        },
                        "content": {
                            "type": "string",
                            "description": (
                                "Entry content for add/replace. Keep it concise and durable."
                            ),
                        },
                        "entry_id": {
                            "type": "integer",
                            "description": "1-based entry id for replace/remove.",
                        },
                    },
                    "required": ["action", "scope"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "skill",
                "description": (
                    "Load an allowed skill by name and add its instructions to session context."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "Name of the skill to activate from the available skills catalog."
                            ),
                        }
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
        ]
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
                        "context_window": 4096,
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
        "Version {app_version}\nModel {model}\nWorkspace {agent_workspace}\n"
        "Thinking {thinking_effort}\nDate {current_date}",
        encoding="utf-8",
    )
    (prompts_dir / "tools.md").write_text("Tools\n{generated:tool_list}", encoding="utf-8")
    (prompts_dir / "channels.md").write_text("Channels\n{generated:channel_list}", encoding="utf-8")
    (prompts_dir / "skills.md").write_text("Skills\n{generated:skill_list}", encoding="utf-8")
    (prompts_dir / "compaction.md").write_text("Summarize the conversation.", encoding="utf-8")


def _write_workspace_templates(resources: Path) -> None:
    templates_dir = resources / "workspace-templates"
    templates_dir.mkdir(parents=True)
    templates = {
        "SOUL.md": "Soul template for integration",
        "MEMORY.md": "Memory template for integration",
        "USER.md": "User template for integration",
    }
    for filename, content in templates.items():
        (templates_dir / filename).write_text(content, encoding="utf-8")


def _write_skill(data_dir: Path, name: str, description: str) -> None:
    skill_dir = data_dir / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n",
        encoding="utf-8",
    )


def _json_dump(data: JsonObject) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
