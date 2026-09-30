"""Tests for the shared RPC payload mappers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.chat import ChatMessage
from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.providers import LOCAL_CONTEXT_DEFAULT_CAP
from server.rpc.payloads import (
    _model_detail_response,
    _model_response,
    _resolve_context_window,
    _visible_message,
    remove_opaque_provider_metadata,
)

_LOCAL = {"ollama": {"local": True}}


def _model(
    model_id: str,
    *,
    context_window: int | None,
    metadata: dict[str, Any] | None = None,
    recommended_temperature: float | None = None,
) -> Model:
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
        ),
        context_window=context_window,
        max_output_tokens=None,
        metadata=metadata or {},
        recommended_temperature=recommended_temperature,
    )


@pytest.mark.parametrize(
    ("provider_id", "model", "local_windows", "effective"),
    [
        # Remote Models keep the raw window; an unknown window stays unknown.
        pytest.param("openai", _model("gpt-5.2", context_window=256000), None, 256000, id="remote"),
        pytest.param("custom", _model("mystery", context_window=None), None, None, id="unknown"),
        pytest.param(
            "ollama",
            _model("ministral-3:8b", context_window=262144, metadata=_LOCAL),
            None,
            LOCAL_CONTEXT_DEFAULT_CAP,
            id="local-capped",
        ),
        pytest.param(
            "ollama",
            _model("ministral-3:8b", context_window=262144, metadata=_LOCAL),
            {"ollama/ministral-3:8b": 16384},
            16384,
            id="local-user-setting",
        ),
        # A proxied cloud Model has no cap and no user knob.
        pytest.param(
            "ollama",
            _model("kimi-k2.6:cloud", context_window=262144, metadata={"ollama": {"remote": True}}),
            None,
            262144,
            id="proxied-cloud",
        ),
    ],
)
def test_model_payload_carries_raw_and_effective_context_window(
    provider_id: str, model: Model, local_windows: dict[str, int] | None, effective: int | None
) -> None:
    payload = _model_response(provider_id, model, local_context_windows=local_windows)

    assert payload["context_window"] == model.context_window
    assert payload["effective_context_window"] == effective


@pytest.mark.parametrize("temperature", [1.0, None])
def test_model_detail_projects_the_recommended_temperature(temperature: float | None) -> None:
    model = _model("glm-5.2", context_window=976000, recommended_temperature=temperature)

    assert _model_detail_response("ollama-cloud", model)["recommended_temperature"] == temperature


def _agent_window_state(model: Model, local_windows: dict[str, int]) -> SimpleNamespace:
    class _Models:
        def get(self, provider_id: str, model_id: str) -> Model:
            if (provider_id, model_id) != ("ollama", model.model_id):
                raise KeyError((provider_id, model_id))
            return model

    class _Storage:
        def load_local_models_settings(self) -> dict[str, Any]:
            return {"context_windows": dict(local_windows)}

    class _Providers:
        def get(self, provider_id: str) -> Any:
            raise KeyError(provider_id)

    return SimpleNamespace(
        runtime=SimpleNamespace(models=_Models(), storage=_Storage(), providers=_Providers())
    )


@pytest.mark.parametrize(
    ("local_windows", "expected"),
    [
        pytest.param({"ollama/ministral-3:8b": 16384}, 16384, id="user-setting"),
        pytest.param({}, LOCAL_CONTEXT_DEFAULT_CAP, id="default-cap"),
    ],
)
def test_agent_payload_window_uses_the_effective_local_resolution(
    local_windows: dict[str, int], expected: int
) -> None:
    model = _model("ministral-3:8b", context_window=262144, metadata=_LOCAL)
    state = _agent_window_state(model, local_windows)

    assert _resolve_context_window(state, "ollama/ministral-3:8b") == expected


def test_opaque_provider_metadata_and_file_references_never_reach_a_client() -> None:
    usage = {"input_tokens": 100, "output_tokens": 50, "cache_write_tokens": 10}
    value = {
        "role": "assistant",
        "content": "Hello",
        "reasoning": "thinking",
        "reasoning_meta": {"secret": "opaque"},
        "reasoning_scope": "openai/gpt-5.6-sol::api-key:work",
        "output_files": [{"path": "/srv/data/out.png"}],
        "image_files": [{"path": "/srv/data/in.png"}],
        "media_files": [{"path": "/srv/data/song.mp3", "media_type": "audio/mpeg"}],
        "usage": usage,
        "tool_calls": [
            {
                "id": "call_1",
                "name": "read",
                "arguments": {"path": "file.txt"},
                "reasoning_meta": {"secret": "nested"},
            }
        ],
    }

    assert remove_opaque_provider_metadata(value) == {
        "role": "assistant",
        "content": "Hello",
        "reasoning": "thinking",
        "usage": usage,
        "tool_calls": [{"id": "call_1", "name": "read", "arguments": {"path": "file.txt"}}],
    }


_TIMING = {
    "started_at": "2026-05-03T14:30:01+00:00",
    "completed_at": "2026-05-03T14:30:02+00:00",
    "duration_ms": 1000,
}


@pytest.mark.parametrize(
    ("message", "visible", "withheld"),
    [
        pytest.param(
            ChatMessage.assistant(
                model="openai/gpt-5.2",
                content="Hello",
                reasoning="visible thinking",
                reasoning_meta={"secret": "opaque"},
                reasoning_scope="openai/gpt-5.2::api-key:work",
                usage={"input_tokens": 100, "output_tokens": 50},
            ),
            {
                "content": "Hello",
                "reasoning": "visible thinking",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
            ("reasoning_meta", "reasoning_scope"),
            id="assistant",
        ),
        pytest.param(
            ChatMessage.assistant(model="openai/gpt-5.2", content="Hello"),
            {"content": "Hello"},
            ("usage",),
            id="assistant-without-usage",
        ),
        pytest.param(
            ChatMessage.tool(
                tool_call_id="call-one",
                name="read",
                content='{"ok":true,"error":null,"data":{},"artifacts":[]}',
                timing=_TIMING,
            ),
            {"timing": _TIMING},
            (),
            id="tool-timing",
        ),
    ],
)
def test_visible_message_keeps_the_canonical_fields(
    message: ChatMessage, visible: dict[str, Any], withheld: tuple[str, ...]
) -> None:
    result = _visible_message(message)

    assert {key: result[key] for key in visible} == visible
    assert set(withheld).isdisjoint(result)
