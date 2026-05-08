"""Phase 2 backend integration tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from core.agents.agents import SkillPromptRegistry
from core.chat import ChatLoop
from core.providers.adapter import ProviderAdapter
from core.runtime import Runtime
from core.skills.skills import SkillRegistry
from core.tools import tool_success
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
        raise NotImplementedError("streaming is outside Phase 2 integration scope")
        yield {}

    def normalize_response(self, response: JsonObject) -> JsonObject:
        return response


@pytest.fixture
def resources_dir(tmp_path: Path) -> Path:
    resources = tmp_path / "resources"
    _write_provider_resource(resources)
    _write_model_resource(resources)
    _write_prompt_resources(resources)
    _write_workspace_templates(resources)
    return resources


@pytest.mark.asyncio
async def test_phase2_agent_sends_message_and_persists_assistant_response(
    tmp_path: Path,
    resources_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeAdapter({"content": "Phase 2 works", "reasoning": None, "tool_calls": None})
    config = Config(data_dir=tmp_path / "data")
    config._data["RESOURCES_PATH"] = str(resources_dir)
    config._data["APP_VERSION"] = "test-version"
    runtime = Runtime(config)
    monkeypatch.setenv("FAKE_API_KEY", "test-key")
    monkeypatch.setattr(runtime, "get_adapter", lambda provider_id: adapter)

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
        assert assistant.content == "Phase 2 works"
        assert [message.role for message in messages] == ["user", "assistant"]
        assert messages[0].content == "Hello"
        assert messages[1].model == "fake-provider/fake-model-v1"
        assert messages[1].content == "Phase 2 works"
        assert adapter.requests[0].model_id == "fake-model-v1"
        assert adapter.requests[0].kwargs["thinking_effort"] == "high"
        assert adapter.requests[0].kwargs["temperature"] == 0.1
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
    monkeypatch.setattr(runtime, "get_adapter", lambda provider_id: adapter)

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
        tool_result = json.loads(messages[2].content or "{}")
        assert assistant.content == "I read: file content"
        assert [message.role for message in messages] == ["user", "assistant", "tool", "assistant"]
        assert tool_result["ok"] is True
        assert tool_result["error"] is None
        assert tool_result["data"] == {"content": "file content"}
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
    monkeypatch.setattr(runtime, "get_adapter", lambda provider_id: adapter)

    runtime.start()
    try:
        runtime.agents.create("coder", "Coder Agent", model="fake-provider/fake-model-v1")

        assistant = await ChatLoop(runtime).send("coder", "Read missing", session_id="session-one")

        messages = runtime.chat_sessions.get("coder", "session-one").load()
        tool_result = json.loads(messages[2].content or "{}")
        assert assistant.content == "The file was missing, so I recovered."
        assert [message.role for message in messages] == ["user", "assistant", "tool", "assistant"]
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
        assert "Identity template for integration" in prompt
        assert "Agents template for integration" in prompt
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
            }
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
                "auth": {
                    "header": "Authorization",
                    "prefix": "Bearer ",
                    "credential_key": "FAKE_API_KEY",
                },
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
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _write_prompt_resources(resources: Path) -> None:
    prompts_dir = resources / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "system.md").write_text(
        "App {app_version}\n{runtime}\n{tools}\n{skills}\n"
        "{include:SOUL.md}\n{include:IDENTITY.md}\n{include:AGENTS.md}\n{include:USER.md}",
        encoding="utf-8",
    )
    (prompts_dir / "runtime.md").write_text(
        "Model {model}\nWorkspace {agent_workspace}\n"
        "Thinking {thinking_effort}\nDate {current_date}",
        encoding="utf-8",
    )
    (prompts_dir / "tools.md").write_text("Tools\n{tool_list}", encoding="utf-8")
    (prompts_dir / "skills.md").write_text("Skills\n{skill_list}", encoding="utf-8")


def _write_workspace_templates(resources: Path) -> None:
    templates_dir = resources / "workspace-templates"
    templates_dir.mkdir(parents=True)
    templates = {
        "SOUL.md": "Soul template for integration",
        "IDENTITY.md": "Identity template for integration",
        "AGENTS.md": "Agents template for integration",
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
