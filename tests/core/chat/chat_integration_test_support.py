"""Shared fixtures and fakes for chat integration behavior tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, override

import pytest

from core.prompts.briefs import BRIEF_FRAGMENT_NAMES
from core.providers.adapter import (
    IMAGE_WIRE_MEDIA_TYPES,
    ProviderAdapter,
)
from core.runtime import Runtime
from core.utils.config import Config
from tests.core.providers.adapter_test_support import response_deltas

JsonObject = dict[str, Any]
StartRuntime = Callable[..., AbstractContextManager[Runtime]]


@dataclass(frozen=True)
class CapturedRequest:
    messages: list[JsonObject]
    model_id: str
    kwargs: JsonObject


class FakeAdapter(ProviderAdapter):
    """Provider adapter test double that records canonical chat requests.

    ``stream`` delivers the response ``send`` returns, as its normalized deltas.
    """

    def __init__(self, response: JsonObject | list[JsonObject]) -> None:
        self.response = response
        self.requests: list[CapturedRequest] = []

    @override
    async def aclose(self) -> None:
        return None

    @override
    async def send(self, messages: list[dict], *, model_id: str, **kwargs: Any) -> dict:
        self.requests.append(
            CapturedRequest(messages=list(messages), model_id=model_id, kwargs=kwargs)
        )
        if isinstance(self.response, list):
            return self.response.pop(0)
        return self.response

    @override
    async def stream(
        self,
        messages: list[dict],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict]:
        response = await self.send(messages, model_id=model_id, **kwargs)
        for delta in response_deltas(self.normalize_response(response, model_id=model_id)):
            yield delta

    @override
    def normalize_response(
        self, response: JsonObject, *, model_id: str | None = None
    ) -> JsonObject:
        return response

    @override
    def wire_media_support(self, model_id: str) -> frozenset[str]:
        del model_id
        return IMAGE_WIRE_MEDIA_TYPES


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


@pytest.fixture
def start_runtime(
    tmp_path: Path, resources_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> StartRuntime:
    """Start a real Runtime on the fake resources; its Model requests reach ``adapter``."""

    @contextmanager
    def start(adapter: ProviderAdapter | None = None) -> Iterator[Runtime]:
        config = Config(data_dir=tmp_path / "data")
        config._data["RESOURCES_PATH"] = str(resources_dir)
        config._data["VBOT_VERSION"] = "test-version"
        runtime = Runtime(config)
        monkeypatch.setenv("FAKE_API_KEY", "test-key")
        if adapter is not None:
            monkeypatch.setattr(runtime, "get_adapter", lambda connection: adapter)
        runtime.start()
        try:
            yield runtime
        finally:
            runtime.stop()

    return start


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
    (prompts_dir / "system_reminders.md").write_text("System reminders", encoding="utf-8")
    (prompts_dir / "subagent_role.md").write_text("Sub-Agent role", encoding="utf-8")
    # Ships disabled by default; must exist because the block definitions read it.
    (prompts_dir / "tools_list.md").write_text("Tool list\n{generated:tool_list}", encoding="utf-8")
    (prompts_dir / "channels.md").write_text("Channels\n{generated:channel_list}", encoding="utf-8")
    (prompts_dir / "skills.md").write_text("Skills\n{generated:skill_catalog}", encoding="utf-8")
    (prompts_dir / "skill_maintenance.md").write_text("Skill maintenance", encoding="utf-8")
    (prompts_dir / "compaction.md").write_text("Summarize the conversation.", encoding="utf-8")
    # Backend-only brief fragments (like compaction), read by Reflection and /learn Runs.
    for name in BRIEF_FRAGMENT_NAMES:
        (prompts_dir / name).write_text(f"Brief fragment {name}.", encoding="utf-8")


def _write_workspace_templates(resources: Path) -> None:
    # Only SOUL.md is a workspace template now; USER.md/MEMORY.md belong to the memory
    # system and are created lazily on first write, never seeded.
    templates_dir = resources / "workspace-templates"
    templates_dir.mkdir(parents=True)
    (templates_dir / "SOUL.md").write_text("Soul template for integration", encoding="utf-8")


def _json_dump(data: JsonObject) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
