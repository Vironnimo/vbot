"""Tests for task-model RPC handlers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.rpc.methods import dispatch_rpc


@pytest.mark.asyncio
async def test_task_model_list_targets_rpc_returns_targets() -> None:
    state = SimpleNamespace(runtime=SimpleNamespace(model_tasks=_ModelTasks()))

    result = await dispatch_rpc(
        state,
        {"method": "task_model.list_targets", "params": {"task_type": "speech_to_text"}},
    )

    assert result == {
        "ok": True,
        "result": {
            "targets": [
                {
                    "id": "openrouter/openai/gpt-4o-transcribe::api-key",
                    "kind": "provider",
                    "provider_id": "openrouter",
                    "model_id": "openai/gpt-4o-transcribe",
                    "connection_id": "openrouter:api-key",
                    "connection_label": "API Key",
                    "label": "OpenRouter / GPT-4o Transcribe",
                    "task_types": ["speech_to_text"],
                    "usable": True,
                    "metadata": {},
                }
            ]
        },
    }


@pytest.mark.asyncio
async def test_task_model_update_validates_payload() -> None:
    state = SimpleNamespace(runtime=SimpleNamespace(model_tasks=_ModelTasks()))

    result = await dispatch_rpc(
        state,
        {"method": "task_model.update", "params": {"model_tasks": {"bad": {}}}},
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_request"


class _Target:
    def to_dict(self) -> dict[str, object]:
        return {
            "id": "openrouter/openai/gpt-4o-transcribe::api-key",
            "kind": "provider",
            "provider_id": "openrouter",
            "model_id": "openai/gpt-4o-transcribe",
            "connection_id": "openrouter:api-key",
            "connection_label": "API Key",
            "label": "OpenRouter / GPT-4o Transcribe",
            "task_types": ["speech_to_text"],
            "usable": True,
            "metadata": {},
        }


class _ModelTasks:
    def list_targets(self, _task_type: str) -> list[_Target]:
        return [_Target()]

    def update(self, model_tasks: object) -> object:
        return model_tasks
