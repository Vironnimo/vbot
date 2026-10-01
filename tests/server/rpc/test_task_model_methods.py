"""Tests for task-model RPC handlers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.model_tasks import (
    TASK_TEXT_TO_SPEECH,
    TaskModelBinding,
    TaskModelError,
    TaskModelOptionChoice,
    TaskModelOptionField,
    TaskModelOptionSchema,
    TaskModelService,
)
from core.model_tasks.speech_setup import LocalSpeechSetup
from server.rpc.methods import dispatch_rpc
from tests.core.model_tasks.model_tasks_test_support import (
    _Credentials,
    _model,
    _Models,
    _Providers,
    _Storage,
)


def _settings_state(model_tasks: Any) -> SimpleNamespace:
    """RPC state whose Runtime persists Task Model bindings in its Settings."""
    return SimpleNamespace(
        runtime=SimpleNamespace(
            model_tasks=model_tasks,
            storage=SimpleNamespace(load_settings=lambda: {"model_tasks": model_tasks.settings()}),
            apply_settings_change=AsyncMock(return_value=False),
        )
    )


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


def _local_owner(target: str, label: str) -> SimpleNamespace:
    """A service running one local target, as the generic local RPCs see it."""
    setup = MagicMock()
    setup.status.return_value = {"state": "missing", "phase": "checking", "error": "python_missing"}
    setup.install.return_value = {"state": "installing", "phase": "queued", "error": ""}
    models = [{"target": target, "label": label, "loaded": True, "busy": False}]

    def setup_for(requested: str) -> Any:
        if requested != target:
            raise ValueError("Unknown local target")
        return setup

    async def unload(requested: str) -> dict[str, Any]:
        setup_for(requested)
        models[0]["loaded"] = False
        return {"models": models, "released": True}

    return SimpleNamespace(
        setup=setup,
        local_setup_for=setup_for,
        local_memory_status=lambda: {"models": models},
        unload_local=unload,
    )


@pytest.mark.asyncio
async def test_generic_local_rpcs_route_each_target_to_the_service_that_runs_it() -> None:
    speech = _local_owner("local/qwen3-tts", "Qwen3-TTS")
    embeddings = _local_owner("local/granite-embedding-r2", "Granite")
    state = SimpleNamespace(
        runtime=SimpleNamespace(
            speech=speech,
            embeddings=embeddings,
            model_tasks=SimpleNamespace(
                binding_for=lambda _: TaskModelBinding(
                    task_type="text_embedding", target="local/granite-embedding-r2"
                ),
                binding_is_usable=lambda _: False,
            ),
        ),
        request_restart=None,
    )

    async def call(method: str, **params: Any) -> dict[str, Any]:
        return await dispatch_rpc(state, {"method": f"task_model.{method}", "params": params})

    status = await call("local_setup_status", target="local/granite-embedding-r2")
    assert status["result"] == {
        "state": "missing",
        "phase": "checking",
        "error": "python_missing",
        "restart_available": False,
    }
    install = await call("local_setup_install", target="local/granite-embedding-r2")
    assert install["result"]["state"] == "installing"
    embeddings.setup.install.assert_called_once_with()
    speech.setup.install.assert_not_called()
    assert [
        model["target"] for model in (await call("local_memory_status"))["result"]["models"]
    ] == [
        "local/qwen3-tts",
        "local/granite-embedding-r2",
    ]
    unloaded = (await call("local_unload", target="local/granite-embedding-r2"))["result"]
    assert unloaded["released"] is True
    assert [model["loaded"] for model in unloaded["models"]] == [True, False]
    # The selected local engine's readiness is checked (and logged) by its owner.
    await call("status", task_type="text_embedding")
    embeddings.setup.status.assert_called_with(log_unavailable=True)
    for method, params in (
        ("local_setup_status", {"target": "local/unknown"}),
        ("local_setup_install", {"target": "openrouter/x/y::api-key"}),
        ("local_setup_install", {"target": "local/qwen3-tts", "packages": ["untrusted"]}),
        ("local_unload", {"target": "local/unknown"}),
        ("local_memory_status", {"force": True}),
    ):
        assert (await call(method, **params))["error"]["code"] == "invalid_request"
    speech.setup.install.assert_not_called()


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
@pytest.mark.parametrize("method", ["update", "options"])
async def test_unknown_local_task_target_is_an_invalid_request(method: str) -> None:
    service = TaskModelService(_Providers(), _Models(), _Credentials(), _Storage())
    state = _settings_state(service)
    binding = {"target": "local/missing"}
    params = (
        {"model_tasks": {TASK_TEXT_TO_SPEECH: binding}}
        if method == "update"
        else {"task_type": TASK_TEXT_TO_SPEECH, **binding}
    )

    result = await dispatch_rpc(state, {"method": f"task_model.{method}", "params": params})

    assert result["error"]["code"] == "invalid_request"
    assert service.settings() == {}
    state.runtime.apply_settings_change.assert_not_awaited()


@pytest.mark.asyncio
async def test_overflowing_task_option_is_an_invalid_request() -> None:
    service = TaskModelService(
        _Providers(),
        _Models([_model("tts", (TASK_TEXT_TO_SPEECH,))]),
        _Credentials(),
        _Storage(),
    )
    state = _settings_state(service)

    result = await dispatch_rpc(
        state,
        {
            "method": "task_model.update",
            "params": {
                "model_tasks": {
                    TASK_TEXT_TO_SPEECH: {
                        "target": "openrouter/tts::api-key",
                        "options": {"speed": 10**400},
                    }
                }
            },
        },
    )

    assert result["error"]["code"] == "invalid_request"
    assert service.settings() == {}


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
    state = _settings_state(model_tasks)
    previous = {"model_tasks": model_tasks.settings()}

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
    # Live consumers, such as a local speech model preload, see the saved change.
    state.runtime.apply_settings_change.assert_awaited_once_with(
        previous, {"model_tasks": result["result"]["model_tasks"]}
    )


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_type", "target", "engine"),
    [
        ("speech_to_text", "local/nemotron3.5-asr", ""),
        ("text_to_speech", "local/qwen3-tts", "qwen3-tts"),
    ],
)
async def test_local_speech_readiness_preserves_setup_and_logs_missing_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task_type: str, target: str, engine: str
) -> None:
    logger = MagicMock()
    monkeypatch.setattr("core.model_tasks.speech_setup._LOGGER", logger)
    setup = LocalSpeechSetup(engine=engine, directory=tmp_path)
    setup.python.parent.mkdir(parents=True)
    setup.python.touch()
    # A receipt from an earlier application release is still a completed setup.
    receipt = tmp_path / "verified.json"
    receipt.write_text('{"recipe": "previous", "sources": {}}', encoding="utf-8")
    lookup = MagicMock(return_value=setup)
    state = SimpleNamespace(
        runtime=SimpleNamespace(
            model_tasks=SimpleNamespace(
                binding_for=lambda _: TaskModelBinding(task_type=task_type, target=target),
                binding_is_usable=lambda _: setup.available(),
            ),
            speech=SimpleNamespace(local_setup_for=lookup),
        )
    )
    request = {"method": "task_model.status", "params": {"task_type": task_type}}
    result = await dispatch_rpc(state, request)
    assert result["result"]["usable"] is True
    lookup.assert_called_once_with(target)
    logger.warning.assert_not_called()
    setup.python.unlink()
    for _ in range(3):
        result = await dispatch_rpc(state, request)
        assert result["result"] == {"task_type": task_type, "configured": True, "usable": False}
    assert logger.warning.call_count == 1
    assert logger.warning.call_args.args[1:3] == (engine or "stt", "python_missing")
    assert receipt.read_text(encoding="utf-8") == '{"recipe": "previous", "sources": {}}'


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

    def update(self, model_tasks: object, *, base: object = None) -> object:
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
        return TaskModelBinding(
            task_type=_task_type,
            target="openrouter/openai/gpt-4o-transcribe::api-key",
        )

    def binding_is_usable(self, _task_type: str) -> bool:
        return self._usable


@pytest.mark.asyncio
async def test_prepare_transcription_rpc_reports_state_and_rejects_parameters() -> None:
    speech = SimpleNamespace(prepare_transcription=MagicMock(return_value="loading"))
    state = SimpleNamespace(runtime=SimpleNamespace(speech=speech))
    request = {"method": "speech.prepare_transcription", "params": {}}
    assert await dispatch_rpc(state, request) == {"ok": True, "result": {"state": "loading"}}
    request["params"] = {"target": "local/parakeet"}
    assert (await dispatch_rpc(state, request))["error"]["code"] == "invalid_request"
    speech.prepare_transcription.assert_called_once_with()


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
