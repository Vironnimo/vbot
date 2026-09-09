"""Tests for task-model RPC handlers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.model_tasks import (
    TASK_TEXT_TO_SPEECH,
    TaskModelBinding,
    TaskModelError,
    TaskModelOptionChoice,
    TaskModelOptionField,
    TaskModelOptionSchema,
)
from server.rpc.methods import dispatch_rpc


@pytest.mark.asyncio
async def test_local_speech_setup_rpc_status_install_and_guarded_restart() -> None:
    setup = MagicMock()
    setup.status.return_value = {"state": "missing"}
    setup.install.return_value = {"state": "installing", "phase": "checking", "error": ""}
    restart = MagicMock()
    state = SimpleNamespace(
        runtime=SimpleNamespace(speech=SimpleNamespace(local_setup=setup)),
        request_restart=restart,
    )

    async def invoke(action: str):
        return await dispatch_rpc(state, {"method": f"speech.local_setup_{action}", "params": {}})

    assert (await invoke("status"))["result"] == {"state": "missing", "restart_available": True}
    assert (await invoke("install"))["result"]["state"] == "installing"
    setup.install.assert_called_once_with()
    assert (await invoke("restart"))["result"]["error"] == "setup_not_finished"
    restart.assert_not_called()
    setup.status.return_value = {"state": "restart_required"}
    assert (await invoke("restart"))["result"] == {"state": "restarting"}
    restart.assert_called_once_with()
    restart.side_effect = OSError("private details")
    assert (await invoke("restart"))["result"] == {
        "state": "failed",
        "error": "restart_unavailable",
    }
    state.request_restart = None
    assert (await invoke("status"))["result"]["restart_available"] is False
    assert (await invoke("restart"))["result"]["error"] == "restart_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["status", "install", "restart"])
async def test_local_speech_setup_rpc_rejects_client_commands(action: str) -> None:
    state = MagicMock()
    result = await dispatch_rpc(
        state,
        {
            "method": f"speech.local_setup_{action}",
            "params": {"packages": ["untrusted"], "command": "shell"},
        },
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_request"
    state.runtime.speech.local_setup.install.assert_not_called()
    state.request_restart.assert_not_called()


@pytest.mark.asyncio
async def test_speech_setup_routes_exact_tts_target_and_rejects_unknown_targets() -> None:
    setup = MagicMock()
    setup.status.return_value = {"state": "ready"}
    lookup = MagicMock(return_value=setup)
    state = SimpleNamespace(
        runtime=SimpleNamespace(speech=SimpleNamespace(local_setup_for=lookup)),
        request_restart=None,
    )
    request = {"method": "speech.local_setup_status", "params": {"target": "local/chatterbox"}}
    result = await dispatch_rpc(state, request)
    lookup.assert_called_once_with("local/chatterbox")
    assert result["result"] == {"state": "ready", "restart_available": False}
    lookup.side_effect = ValueError("unknown target")
    result = await dispatch_rpc(state, request)
    assert result["error"]["code"] == "invalid_request"


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


@pytest.mark.asyncio
async def test_task_model_options_uses_current_binding_and_reports_effective_values() -> None:
    state = SimpleNamespace(runtime=SimpleNamespace(model_tasks=_ModelTasks()))

    result = await dispatch_rpc(
        state,
        {"method": "task_model.options", "params": {"task_type": TASK_TEXT_TO_SPEECH}},
    )

    assert result["ok"] is True
    schema = result["result"]["schema"]
    assert schema["target"] == "openrouter/microsoft/mai-voice-2::api-key"
    assert schema["configured_options"] == {"voice": "Harper"}
    assert schema["effective_options"] == {
        "extra_options": {},
        "response_format": "mp3",
        "voice": "Harper",
    }
    assert schema["fields"][0]["options"] == [
        {"value": "Harper", "label": "Harper"},
        {"value": "Klaus", "label": "Klaus"},
    ]


@pytest.mark.asyncio
async def test_task_model_patch_options_returns_complete_saved_binding() -> None:
    model_tasks = _ModelTasks()
    state = SimpleNamespace(runtime=SimpleNamespace(model_tasks=model_tasks))

    result = await dispatch_rpc(
        state,
        {
            "method": "task_model.patch_options",
            "params": {
                "task_type": TASK_TEXT_TO_SPEECH,
                "set": {"speed": 1.25},
            },
        },
    )

    assert result == {
        "ok": True,
        "result": {
            "model_tasks": {
                TASK_TEXT_TO_SPEECH: {
                    "target": "openrouter/microsoft/mai-voice-2::api-key",
                    "options": {"voice": "Harper", "speed": 1.25},
                }
            }
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured", "usable"),
    [
        (False, False),
        (True, False),
        (True, True),
    ],
)
async def test_task_model_status_reports_live_binding_readiness(
    configured: bool,
    usable: bool,
) -> None:
    state = SimpleNamespace(
        runtime=SimpleNamespace(model_tasks=_StatusModelTasks(configured=configured, usable=usable))
    )

    result = await dispatch_rpc(
        state,
        {"method": "task_model.status", "params": {"task_type": "speech_to_text"}},
    )

    assert result == {
        "ok": True,
        "result": {
            "task_type": "speech_to_text",
            "configured": configured,
            "usable": usable,
        },
    }


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
    def __init__(self) -> None:
        self._binding = TaskModelBinding(
            task_type=TASK_TEXT_TO_SPEECH,
            target="openrouter/microsoft/mai-voice-2::api-key",
            options={"voice": "Harper"},
        )

    def list_targets(self, _task_type: str) -> list[_Target]:
        return [_Target()]

    def update(self, model_tasks: object) -> object:
        return model_tasks

    def settings(self) -> dict[str, object]:
        return {TASK_TEXT_TO_SPEECH: self._binding.to_dict()}

    def binding_for(self, task_type: str) -> TaskModelBinding:
        assert task_type == TASK_TEXT_TO_SPEECH
        return self._binding

    def options(self, task_type: str, target: str) -> TaskModelOptionSchema:
        assert task_type == TASK_TEXT_TO_SPEECH
        assert target == self._binding.target
        return TaskModelOptionSchema(
            task_type=task_type,
            target=target,
            fields=(
                TaskModelOptionField(
                    name="voice",
                    type="select",
                    label="Voice",
                    required=True,
                    options=(
                        TaskModelOptionChoice("Harper", "Harper"),
                        TaskModelOptionChoice("Klaus", "Klaus"),
                    ),
                ),
                TaskModelOptionField(
                    name="response_format",
                    type="select",
                    label="Format",
                    default="mp3",
                ),
                TaskModelOptionField(
                    name="extra_options",
                    type="json",
                    label="Extra options",
                    default={},
                ),
            ),
        )

    def patch_options(
        self,
        task_type: str,
        *,
        set_values: dict[str, object],
        unset_names: tuple[str, ...],
    ) -> dict[str, object]:
        assert task_type == TASK_TEXT_TO_SPEECH
        assert unset_names == ()
        options = {**self._binding.options, **set_values}
        self._binding = TaskModelBinding(task_type, self._binding.target, options)
        return self.settings()


class _StatusModelTasks:
    def __init__(self, *, configured: bool, usable: bool) -> None:
        self._configured = configured
        self._usable = usable

    def binding_for(self, _task_type: str) -> object:
        if not self._configured:
            raise TaskModelError("No task model configured")
        return object()

    def binding_is_usable(self, _task_type: str) -> bool:
        return self._usable


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [False, True])
async def test_local_memory_rpc_preserves_busy_result_and_rejects_parameters(busy):
    snapshot = {
        "models": [
            {"target": "local/qwen3-tts", "label": "Qwen3-TTS", "loaded": True, "busy": busy}
        ]
    }
    released = {**snapshot, "released": not busy}
    speech = SimpleNamespace(
        local_memory_status=MagicMock(return_value=snapshot),
        unload_local=AsyncMock(return_value=released),
    )
    state = SimpleNamespace(runtime=SimpleNamespace(speech=speech))
    for method, expected in (("local_memory_status", snapshot), ("local_unload", released)):
        params = {"target": "local/qwen3-tts"} if method == "local_unload" else {}
        response = await dispatch_rpc(state, {"method": f"speech.{method}", "params": params})
        assert response == {"ok": True, "result": expected}
        response = await dispatch_rpc(
            state, {"method": f"speech.{method}", "params": {"force": True}}
        )
        assert response["error"]["code"] == "invalid_request"
    speech.local_memory_status.assert_called_once_with()
    speech.unload_local.assert_awaited_once_with("local/qwen3-tts")
    for params in ({}, {"target": 2}, {"target": ""}):
        response = await dispatch_rpc(state, {"method": "speech.local_unload", "params": params})
        assert response["error"]["code"] == "invalid_request"
    speech.unload_local.side_effect = ValueError("unknown target")
    response = await dispatch_rpc(
        state, {"method": "speech.local_unload", "params": {"target": "local/nope"}}
    )
    assert response["error"]["code"] == "invalid_request"
