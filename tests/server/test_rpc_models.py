"""Tests for rpc models."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.models import Capabilities, Model, ModelQuery, ReasoningCapabilities
from server.rpc.methods import dispatch_rpc
from server.rpc.payloads import _model_response
from server.rpc.provider_access import _provider_has_credentials
from tests.server.rpc_test_support import (
    StubAdapter,
    _no_models_dev_fetch,
    make_state,
)

__all__ = ["_no_models_dev_fetch"]


@pytest.mark.asyncio
async def test_model_list_returns_all_models_across_providers_with_full_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-key")
    state = make_state(tmp_path, StubAdapter())
    monkeypatch.setattr(
        state.runtime.providers,
        "list_ids",
        lambda: ["openai", "anthropic", "ollama"],
    )
    state.runtime.models._models["openai"] = [
        state.runtime.models._models["openai"][1],
        state.runtime.models._models["openai"][0],
    ]

    response = await dispatch_rpc(state, {"method": "model.list", "params": {}})

    assert response == {
        "ok": True,
        "result": {
            "models": [
                {
                    "id": "anthropic/claude-sonnet-4-20250219",
                    "provider_id": "anthropic",
                    "model_id": "claude-sonnet-4-20250219",
                    "name": "Claude Sonnet 4",
                    "capabilities": {
                        "vision": True,
                        "tools": True,
                        "json_mode": False,
                        "reasoning": {"supported": True, "control": None, "levels": []},
                        "input_modalities": ["text", "image"],
                        "output_modalities": ["text"],
                        "supported_parameters": [],
                        "task_types": [
                            "chat",
                            "text_output",
                            "image_input",
                            "image_understanding",
                        ],
                    },
                    "context_window": 200000,
                    "effective_context_window": 200000,
                    "local": False,
                    "max_output_tokens": 64000,
                    "connections": [],
                },
                {
                    "id": "ollama/llama3.2",
                    "provider_id": "ollama",
                    "model_id": "llama3.2",
                    "name": "Llama 3.2",
                    "capabilities": {
                        "vision": False,
                        "tools": True,
                        "json_mode": False,
                        "reasoning": {"supported": False, "control": None, "levels": []},
                        "input_modalities": ["text"],
                        "output_modalities": ["text"],
                        "supported_parameters": [],
                        "task_types": ["chat", "text_output"],
                    },
                    "context_window": 128000,
                    "effective_context_window": 128000,
                    "local": False,
                    "max_output_tokens": 8192,
                    "connections": [],
                },
                {
                    "id": "openai/gpt-4.1-mini",
                    "provider_id": "openai",
                    "model_id": "gpt-4.1-mini",
                    "name": "GPT-4.1 mini",
                    "capabilities": {
                        "vision": False,
                        "tools": True,
                        "json_mode": True,
                        "reasoning": {"supported": False, "control": None, "levels": []},
                        "input_modalities": ["text"],
                        "output_modalities": ["text"],
                        "supported_parameters": [],
                        "task_types": ["chat", "text_output"],
                    },
                    "context_window": 128000,
                    "effective_context_window": 128000,
                    "local": False,
                    "max_output_tokens": 16000,
                    "connections": [],
                },
                {
                    "id": "openai/gpt-5.2",
                    "provider_id": "openai",
                    "model_id": "gpt-5.2",
                    "name": "GPT-5.2",
                    "capabilities": {
                        "vision": True,
                        "tools": True,
                        "json_mode": True,
                        "reasoning": {"supported": True, "control": None, "levels": []},
                        "input_modalities": ["text", "image"],
                        "output_modalities": ["text"],
                        "supported_parameters": [],
                        "task_types": [
                            "chat",
                            "text_output",
                            "image_input",
                            "image_understanding",
                        ],
                    },
                    "context_window": 256000,
                    "effective_context_window": 256000,
                    "local": False,
                    "max_output_tokens": 32000,
                    "connections": [],
                },
            ]
        },
    }


@pytest.mark.asyncio
async def test_model_get_returns_complete_model_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    state = make_state(tmp_path, StubAdapter())
    state.runtime.models._models["openai"].append(
        Model(
            model_id="gpt-4o-mini-tts",
            name="GPT-4o mini TTS",
            capabilities=Capabilities(
                vision=False,
                tools=False,
                json_mode=False,
                reasoning=ReasoningCapabilities(supported=False),
                input_modalities=("text",),
                output_modalities=("speech",),
                supported_parameters=("voice", "speed"),
                supported_voices=("en-us-harper:mai-voice-2", "de-de-klaus:mai-voice-2"),
                task_options={"text_to_speech": {"codec": "mp3"}},
            ),
            context_window=None,
            max_output_tokens=None,
            family="gpt-4o",
            metadata={"source": "test"},
        )
    )

    response = await dispatch_rpc(
        state,
        {"method": "model.get", "params": {"model": "openai/gpt-4o-mini-tts"}},
    )

    assert response["ok"] is True
    model = response["result"]["model"]
    assert model["id"] == "openai/gpt-4o-mini-tts"
    assert model["family"] == "gpt-4o"
    assert model["metadata"] == {"source": "test"}
    assert model["capabilities"]["supported_voices"] == [
        "de-de-klaus:mai-voice-2",
        "en-us-harper:mai-voice-2",
    ]
    assert model["capabilities"]["task_options"] == {"text_to_speech": {"codec": "mp3"}}
    assert model["usable_connections"] == ["api-key"]


@pytest.mark.asyncio
async def test_model_get_unknown_model_returns_suggestions(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "model.get", "params": {"model": "openai/gpt-4.1-mni"}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert "did you mean: openai/gpt-4.1-mini" in response["error"]["message"]


@pytest.mark.asyncio
async def test_model_list_filters_by_connection_usability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.delenv("OPENAI_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "model.list", "params": {}})

    assert response == {
        "ok": True,
        "result": {
            "models": [
                {
                    "id": "openai/gpt-4.1-mini",
                    "provider_id": "openai",
                    "model_id": "gpt-4.1-mini",
                    "name": "GPT-4.1 mini",
                    "capabilities": {
                        "vision": False,
                        "tools": True,
                        "json_mode": True,
                        "reasoning": {"supported": False, "control": None, "levels": []},
                        "input_modalities": ["text"],
                        "output_modalities": ["text"],
                        "supported_parameters": [],
                        "task_types": ["chat", "text_output"],
                    },
                    "context_window": 128000,
                    "effective_context_window": 128000,
                    "local": False,
                    "max_output_tokens": 16000,
                    "connections": [],
                },
                {
                    "id": "openai/gpt-5.2",
                    "provider_id": "openai",
                    "model_id": "gpt-5.2",
                    "name": "GPT-5.2",
                    "capabilities": {
                        "vision": True,
                        "tools": True,
                        "json_mode": True,
                        "reasoning": {"supported": True, "control": None, "levels": []},
                        "input_modalities": ["text", "image"],
                        "output_modalities": ["text"],
                        "supported_parameters": [],
                        "task_types": [
                            "chat",
                            "text_output",
                            "image_input",
                            "image_understanding",
                        ],
                    },
                    "context_window": 256000,
                    "effective_context_window": 256000,
                    "local": False,
                    "max_output_tokens": 32000,
                    "connections": [],
                },
            ]
        },
    }


@pytest.mark.asyncio
async def test_model_list_outputs_per_model_connections_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``model.list`` propagates the per-model ``connections`` allowlist
    from the registry into the RPC payload. The WebUI uses this list to
    decide which provider connections to offer for a given model — a
    model tagged ``["oauth"]`` is not offered on ``api-key``. A model whose
    allowlist matches no usable connection is not listed at all."""

    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OPENAI_OAUTH_TOKEN", "oauth-token")
    state = make_state(tmp_path, StubAdapter())
    state.runtime.models._models["openai"] = [
        Model(
            model_id="gpt-5.2",
            name="GPT-5.2",
            capabilities=Capabilities(
                vision=True,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=True),
            ),
            context_window=256000,
            max_output_tokens=32000,
            connections=("api-key",),
        ),
        Model(
            model_id="gpt-5.5",
            name="GPT-5.5",
            capabilities=Capabilities(
                vision=True,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=True),
            ),
            context_window=256000,
            max_output_tokens=32000,
            connections=("oauth",),
        ),
        Model(
            model_id="gpt-ghost",
            name="GPT Ghost",
            capabilities=Capabilities(
                vision=True,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=True),
            ),
            context_window=256000,
            max_output_tokens=32000,
            connections=("subscription",),
        ),
    ]

    response = await dispatch_rpc(state, {"method": "model.list", "params": {}})

    assert response["ok"] is True
    by_id = {model["id"]: model for model in response["result"]["models"]}
    assert by_id["openai/gpt-5.2"]["connections"] == ["api-key"]
    assert by_id["openai/gpt-5.5"]["connections"] == ["oauth"]
    # No usable "subscription" connection exists on the stub provider, so the
    # allowlist-bound model is dropped from the listing entirely.
    assert "openai/gpt-ghost" not in by_id


@pytest.mark.asyncio
async def test_model_list_outputs_empty_connections_for_unrestricted_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model with no ``connections`` allowlist surfaces ``connections``
    as an empty list — the WebUI treats that as "valid for every
    connection of the provider"."""

    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": "model.list", "params": {}})

    assert response["ok"] is True
    for model in response["result"]["models"]:
        assert model["connections"] == []


@pytest.mark.asyncio
async def test_model_list_filters_by_task_and_modality(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    state = make_state(tmp_path, StubAdapter())
    state.runtime.models._models["openai"].append(
        Model(
            model_id="gpt-image",
            name="GPT Image",
            capabilities=Capabilities(
                vision=True,
                tools=False,
                json_mode=False,
                reasoning=ReasoningCapabilities(supported=False),
                input_modalities=("text", "image"),
                output_modalities=("text", "image"),
            ),
            context_window=128000,
            max_output_tokens=32000,
        )
    )

    image_response = await dispatch_rpc(
        state,
        {"method": "model.list", "params": {"task": "image_generation"}},
    )
    audio_response = await dispatch_rpc(
        state,
        {"method": "model.list", "params": {"output_modality": "audio"}},
    )
    context_response = await dispatch_rpc(
        state,
        {
            "method": "model.list",
            "params": {"capability": "tools", "min_context_window": 200000},
        },
    )

    assert [model["id"] for model in image_response["result"]["models"]] == ["openai/gpt-image"]
    assert audio_response["result"]["models"] == []
    assert [model["id"] for model in context_response["result"]["models"]] == ["openai/gpt-5.2"]


@pytest.mark.asyncio
async def test_model_list_filters_by_provider_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    state = make_state(tmp_path, StubAdapter())

    openai_response = await dispatch_rpc(
        state,
        {"method": "model.list", "params": {"provider_id": "openai"}},
    )
    anthropic_response = await dispatch_rpc(
        state,
        {"method": "model.list", "params": {"provider_id": "anthropic"}},
    )
    uppercase_response = await dispatch_rpc(
        state,
        {"method": "model.list", "params": {"provider_id": "OpenAI"}},
    )
    unknown_response = await dispatch_rpc(
        state,
        {"method": "model.list", "params": {"provider_id": "nonexistent"}},
    )

    assert [model["id"] for model in openai_response["result"]["models"]] == [
        "openai/gpt-4.1-mini",
        "openai/gpt-5.2",
    ]
    assert [model["id"] for model in anthropic_response["result"]["models"]] == [
        "anthropic/claude-sonnet-4-20250219"
    ]
    assert [model["id"] for model in uppercase_response["result"]["models"]] == [
        "openai/gpt-4.1-mini",
        "openai/gpt-5.2",
    ]
    assert unknown_response["result"]["models"] == []


@pytest.mark.asyncio
async def test_model_list_rejects_unsupported_fields(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "model.list", "params": {"provider_id": "openai", "extra": True}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert "extra" in response["error"]["message"]


@pytest.mark.asyncio
async def test_model_list_rejects_invalid_filter_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {"method": "model.list", "params": {"min_context_window": -1}},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_model_list_delegates_filtering_to_model_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The RPC result must match what ``ModelQuery.from_filters`` + ``query`` produce.

    This locks in the byte-identical contract while routing filtering through
    the core query instead of duplicating it in the RPC layer.
    """

    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-key")
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "model.list",
            "params": {"task": "image_generation", "min_context_window": 1000},
        },
    )

    # Cross-check the RPC result against the core query path directly. If
    # either path diverges, this test fails — making the "delegate to the
    # core query" contract enforced.
    expected = sorted(
        (
            (
                provider_id,
                _model_response(provider_id, model),
            )
            for provider_id, model in state.runtime.models.query(
                ModelQuery.from_filters({"task": "image_generation", "min_context_window": 1000})
            )
            if _provider_has_credentials(state.runtime, provider_id)
        ),
        key=lambda item: (item[1]["provider_id"], item[1]["model_id"]),
    )
    expected_models = [item[1] for item in expected]

    assert response["result"]["models"] == expected_models
