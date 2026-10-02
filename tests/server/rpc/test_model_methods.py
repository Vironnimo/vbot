"""Model catalog RPCs: ``model.list``, ``model.get`` and ``provider.routing_options``.

``model.refresh_db`` lives in ``test_model_methods_refresh.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.models import Capabilities, Model, ReasoningCapabilities
from core.providers._wire_profile_files import parse_wire_profile_file
from server.rpc import model_methods
from server.rpc.model_methods import shutdown_background_refresh_tasks
from tests.server.rpc_test_support import (
    JsonObject,
    StubAdapter,
    _no_models_dev_fetch,
    make_state,
    openrouter_provider,
    rpc_error,
    rpc_result,
)

__all__ = ["_no_models_dev_fetch"]


def _model(model_id: str, name: str, **fields: Any) -> Model:
    capabilities = {
        "vision": True,
        "tools": True,
        "json_mode": True,
        "reasoning": ReasoningCapabilities(supported=True),
        **fields.pop("capabilities", {}),
    }
    fields.setdefault("context_window", 256000)
    fields.setdefault("max_output_tokens", 32000)
    return Model(model_id=model_id, name=name, capabilities=Capabilities(**capabilities), **fields)


# ---------------------------------------------------------------------------
# model.list / model.get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_list_returns_all_models_across_providers_with_full_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-key")
    monkeypatch.delenv("OPENAI_OAUTH_TOKEN", raising=False)
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

    result = await rpc_result(state, "model.list")

    assert result == {
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
                    "reasoning": {
                        "supported": True,
                        "control": None,
                        "levels": [],
                        "mandatory": False,
                    },
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
                "wire_profiles": {"api-key": {"wire_status": "inferred", "verified_at": None}},
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
                    "reasoning": {
                        "supported": False,
                        "control": None,
                        "levels": [],
                        "mandatory": False,
                    },
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
                "wire_profiles": {"api-key": {"wire_status": "inferred", "verified_at": None}},
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
                    "reasoning": {
                        "supported": False,
                        "control": None,
                        "levels": [],
                        "mandatory": False,
                    },
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
                "wire_profiles": {"api-key": {"wire_status": "inferred", "verified_at": None}},
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
                    "reasoning": {
                        "supported": True,
                        "control": None,
                        "levels": [],
                        "mandatory": False,
                    },
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
                "wire_profiles": {"api-key": {"wire_status": "inferred", "verified_at": None}},
            },
        ]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "expected"),
    [
        # Ollama has no usable Connection, so its Models are never listed.
        pytest.param(
            {},
            [
                "anthropic/claude-sonnet-4-20250219",
                "openai/gpt-4.1-mini",
                "openai/gpt-5.2",
                "openai/gpt-image",
            ],
            id="usable-connections-only",
        ),
        pytest.param({"task": "image_generation"}, ["openai/gpt-image"], id="task"),
        pytest.param({"output_modality": "audio"}, [], id="output-modality"),
        pytest.param(
            {"capability": "tools", "min_context_window": 200000},
            ["anthropic/claude-sonnet-4-20250219", "openai/gpt-5.2"],
            id="capability-and-context-window",
        ),
        pytest.param(
            {"provider_id": "anthropic"},
            ["anthropic/claude-sonnet-4-20250219"],
            id="provider",
        ),
        pytest.param(
            {"provider_id": "OpenAI"},
            ["openai/gpt-4.1-mini", "openai/gpt-5.2", "openai/gpt-image"],
            id="provider-case-insensitive",
        ),
        pytest.param({"provider_id": "nonexistent"}, [], id="unknown-provider"),
    ],
)
async def test_model_list_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    params: JsonObject,
    expected: list[str],
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    state = make_state(tmp_path, StubAdapter())
    state.runtime.models._models["openai"].append(
        _model(
            "gpt-image",
            "GPT Image",
            capabilities={
                "tools": False,
                "json_mode": False,
                "reasoning": ReasoningCapabilities(supported=False),
                "input_modalities": ("text", "image"),
                "output_modalities": ("text", "image"),
            },
            context_window=128000,
        )
    )

    result = await rpc_result(state, "model.list", **params)

    assert [model["id"] for model in result["models"]] == expected


@pytest.mark.asyncio
async def test_model_list_outputs_per_model_connections_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The WebUI offers a Model only on the Connections its allowlist names; a
    Model whose allowlist matches no usable Connection is not listed at all."""

    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OPENAI_OAUTH_TOKEN", "oauth-token")
    state = make_state(tmp_path, StubAdapter())
    state.runtime.models._models["openai"] = [
        _model("gpt-5.2", "GPT-5.2", connections=("api-key",)),
        _model("gpt-5.5", "GPT-5.5", connections=("oauth",)),
        _model("gpt-ghost", "GPT Ghost", connections=("subscription",)),
    ]

    result = await rpc_result(state, "model.list")

    by_id = {model["id"]: model for model in result["models"]}
    assert by_id["openai/gpt-5.2"]["connections"] == ["api-key"]
    assert by_id["openai/gpt-5.5"]["connections"] == ["oauth"]
    assert "openai/gpt-ghost" not in by_id


@pytest.mark.asyncio
async def test_model_catalog_reports_wire_profiles_per_usable_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``model.list`` reports each usable Connection's wire status; ``model.get``
    adds the profile summary and the facts live traffic taught, as requests use them."""

    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OPENAI_OAUTH_TOKEN", "oauth-token")
    state = make_state(tmp_path, StubAdapter())
    runtime = state.runtime
    wire_file = parse_wire_profile_file(
        "openai",
        {
            "format_version": 1,
            "models": {
                "gpt-5.2": {
                    "verified": {"date": "2026-09-30", "connections": ["api-key"]},
                    "set": {
                        "reasoning": {
                            "dialect": "reasoning_effort",
                            "levels": ["low", "high"],
                        },
                        "replay": {"fidelity": "readable_only"},
                    },
                }
            },
        },
        source="openai.json",
        report=lambda issue: pytest.fail(issue),
    )
    assert wire_file is not None
    runtime.wire_profiles.replace_files({"openai": wire_file})
    runtime.wire_observations.record_rejected_parameter("openai", "oauth", "gpt-5.2", "top_p")
    runtime.wire_observations.record_reasoning_field(
        "openai", "oauth", "gpt-5.2", "reasoning_content"
    )

    listed = {model["id"]: model for model in (await rpc_result(state, "model.list"))["models"]}
    model = (await rpc_result(state, "model.get", model="openai/gpt-5.2"))["model"]

    assert listed["openai/gpt-5.2"]["wire_profiles"] == {
        "oauth": {"wire_status": "configured", "verified_at": None},
        "api-key": {"wire_status": "verified", "verified_at": "2026-09-30"},
    }
    assert listed["openai/gpt-4.1-mini"]["wire_profiles"] == {
        "oauth": {"wire_status": "inferred", "verified_at": None},
        "api-key": {"wire_status": "inferred", "verified_at": None},
    }
    assert model["wire_profiles"] == {
        "oauth": {
            "wire_status": "configured",
            "verified_at": None,
            "protocol": "chat_completions",
            "reasoning_dialect": "reasoning_effort",
            "reasoning_ladder": ["low", "high"],
            "replay_fidelity": "readable_only",
            "learned": {
                "reasoning_field": "reasoning_content",
                "rejected_parameters": ["top_p"],
                "rejected_efforts": [],
                "reasoning_returned": True,
            },
        },
        "api-key": {
            "wire_status": "verified",
            "verified_at": "2026-09-30",
            "protocol": "chat_completions",
            "reasoning_dialect": "reasoning_effort",
            "reasoning_ladder": ["low", "high"],
            "replay_fidelity": "readable_only",
            "learned": {
                "reasoning_field": None,
                "rejected_parameters": [],
                "rejected_efforts": [],
                "reasoning_returned": False,
            },
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
        _model(
            "gpt-4o-mini-tts",
            "GPT-4o mini TTS",
            capabilities={
                "vision": False,
                "tools": False,
                "json_mode": False,
                "reasoning": ReasoningCapabilities(supported=False),
                "input_modalities": ("text",),
                "output_modalities": ("speech",),
                "supported_parameters": ("voice", "speed"),
                "supported_voices": ("en-us-harper:mai-voice-2", "de-de-klaus:mai-voice-2"),
                "task_options": {"text_to_speech": {"codec": "mp3"}},
            },
            context_window=None,
            max_output_tokens=None,
            family="gpt-4o",
            metadata={"source": "test"},
        )
    )

    model = (await rpc_result(state, "model.get", model="openai/gpt-4o-mini-tts"))["model"]

    assert model["id"] == "openai/gpt-4o-mini-tts"
    assert model["family"] == "gpt-4o"
    assert model["metadata"] == {"source": "test"}
    assert model["capabilities"]["supported_voices"] == [
        "de-de-klaus:mai-voice-2",
        "en-us-harper:mai-voice-2",
    ]
    assert model["capabilities"]["task_options"] == {"text_to_speech": {"codec": "mp3"}}
    assert model["capabilities"]["reasoning"] == {
        "supported": False,
        "control": None,
        "levels": [],
        "budget_max": None,
        "mandatory": False,
    }
    assert model["usable_connections"] == ["api-key"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "named"),
    [
        ("model.list", {"provider_id": "openai", "extra": True}, "extra"),
        ("model.list", {"min_context_window": -1}, ""),
        ("model.get", {"model": "openai/gpt-4.1-mni"}, "did you mean: openai/gpt-4.1-mini"),
        ("model.get", {"model": "gpt-4.1-mini"}, "params.model must be '<provider>/<model-id>'"),
        ("provider.routing_options", {"provider_id": "openai"}, "does not expose routing"),
    ],
)
async def test_malformed_model_catalog_requests_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    params: JsonObject,
    named: str,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    state = make_state(tmp_path, StubAdapter())

    error = await rpc_error(state, method, **params)

    assert error["code"] == "invalid_request"
    assert named in error["message"]


# ---------------------------------------------------------------------------
# Local catalog auto-refresh budget
# ---------------------------------------------------------------------------


def _local_model() -> Model:
    return _model("local-fresh", "Local Fresh")


@pytest.mark.asyncio
@pytest.mark.parametrize("sweep_fails", [False, True])
async def test_model_list_waits_for_a_fast_local_catalog_sweep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sweep_fails: bool,
) -> None:
    """A sweep that finishes within the budget is listed; a failing one degrades
    to the last known catalog instead of failing the listing."""

    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    state = make_state(tmp_path, StubAdapter())

    async def maybe_refresh_local_catalogs() -> None:
        if sweep_fails:
            raise RuntimeError("unexpected")
        state.runtime.models._models["openai"].append(_local_model())

    state.runtime.maybe_refresh_local_catalogs = maybe_refresh_local_catalogs

    result = await rpc_result(state, "model.list", provider_id="openai")

    listed = "openai/local-fresh" in [model["id"] for model in result["models"]]
    assert listed is not sweep_fails


@pytest.mark.asyncio
async def test_model_list_serves_the_stale_catalog_while_a_slow_sweep_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setattr(model_methods, "LOCAL_CATALOG_REFRESH_WAIT_SECONDS", 0.0)
    state = make_state(tmp_path, StubAdapter())
    release = asyncio.Event()
    finished = asyncio.Event()

    async def maybe_refresh_local_catalogs() -> None:
        await release.wait()
        if not finished.is_set():
            state.runtime.models._models["openai"].append(_local_model())
            finished.set()

    state.runtime.maybe_refresh_local_catalogs = maybe_refresh_local_catalogs
    try:
        stale = await rpc_result(state, "model.list", provider_id="openai")
        # The budget expired, but the sweep was not cancelled.
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=1.0)
        fresh = await rpc_result(state, "model.list", provider_id="openai")
    finally:
        release.set()
        await shutdown_background_refresh_tasks(state.runtime)

    assert "openai/local-fresh" not in [model["id"] for model in stale["models"]]
    assert "openai/local-fresh" in [model["id"] for model in fresh["models"]]


@pytest.mark.asyncio
async def test_shutdown_cancels_and_drains_background_local_catalog_sweeps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_methods, "LOCAL_CATALOG_REFRESH_WAIT_SECONDS", 0.0)
    state = make_state(tmp_path, StubAdapter())
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def maybe_refresh_local_catalogs() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    state.runtime.maybe_refresh_local_catalogs = maybe_refresh_local_catalogs
    await rpc_result(state, "model.list")
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await shutdown_background_refresh_tasks(state.runtime)

    assert cancelled.is_set()


# ---------------------------------------------------------------------------
# provider.routing_options
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_routing_options_delegates_to_openrouter_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RoutingAdapter(StubAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.model_ids: list[str | None] = []
            self.closed = False

        async def routing_provider_options(
            self, model_id: str | None = None
        ) -> list[dict[str, str]]:
            self.model_ids.append(model_id)
            return [{"slug": "anthropic", "name": "Anthropic"}]

        async def aclose(self) -> None:
            self.closed = True

    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    adapter = RoutingAdapter()
    state = make_state(tmp_path, adapter)
    state.runtime.providers.add(openrouter_provider())

    result = await rpc_result(
        state,
        "provider.routing_options",
        provider_id="openrouter",
        model_id="anthropic/claude-sonnet-4",
    )

    assert result == {"providers": [{"slug": "anthropic", "name": "Anthropic"}]}
    assert adapter.model_ids == ["anthropic/claude-sonnet-4"]
    assert adapter.closed is True
