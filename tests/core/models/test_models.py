"""Models: Model records and the ModelRegistry load path.

Covers record normalization, how the registry projects Model DB records, its
tolerance for invalid Model DB files, the override layer as files on disk,
lookup, and the cache and reload lifecycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
from collections.abc import Callable, Mapping
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest

from core.models.models import (
    Capabilities,
    Model,
    ModelRegistry,
    ReasoningCapabilities,
    derive_model_task_types,
)
from core.models.query import ModelQuery

FIXTURES_DIR = Path(__file__).parent / "fixtures"

_PLAIN_CAPABILITIES = Capabilities(
    vision=False,
    tools=True,
    json_mode=False,
    reasoning=ReasoningCapabilities(supported=False),
)


def _record(name: str = "Model", **changes: Any) -> dict[str, Any]:
    """A complete Model DB record; ``changes`` replace top-level fields."""

    record: dict[str, Any] = {
        "name": name,
        "capabilities": {
            "vision": False,
            "tools": True,
            "json_mode": False,
            "reasoning": {"supported": False},
        },
        "context_window": 32000,
        "max_output_tokens": 4096,
    }
    record.update(changes)
    return record


def _capabilities(**changes: Any) -> dict[str, Any]:
    return {**_record()["capabilities"], **changes}


def _model(model_id: str, name: str = "Model", **changes: Any) -> Model:
    """The Model that ``_record(name, ...)`` loads as, with typed ``changes``."""

    base = Model(
        model_id=model_id,
        name=name,
        capabilities=_PLAIN_CAPABILITIES,
        context_window=32000,
        max_output_tokens=4096,
    )
    return replace(base, **changes)


def _write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    return path


def _write_catalog(resources: Path, provider_id: str, models: Mapping[str, object]) -> Path:
    return _write_json(
        resources / "models" / f"{provider_id}.json",
        {"provider_id": provider_id, "models": models},
    )


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def test_direct_construction_leaves_optional_facts_unset() -> None:
    model = Model(
        model_id="custom-model",
        name="Custom Model",
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=True),
        ),
        context_window=None,
        max_output_tokens=None,
    )

    assert model.capabilities.reasoning.control is None
    assert model.capabilities.reasoning.levels == ()
    assert model.capabilities.reasoning.budget_max is None
    assert model.capabilities.supported_parameters == ()
    assert model.capabilities.supported_voices == ()
    assert (model.context_window, model.max_output_tokens) == (None, None)
    assert model.family == ""
    assert model.metadata == {}
    assert model.connections == ()
    assert model.connection_context_windows == {}
    assert (model.recommended_temperature, model.recommended_top_p) == (None, None)
    assert model.reasoning_replay is None
    assert model.pricing is None


def _set_model_id(model: Model) -> None:
    model.model_id = "changed"  # type: ignore[misc]


def _set_vision(model: Model) -> None:
    model.capabilities.vision = False  # type: ignore[misc]


def _set_reasoning_supported(model: Model) -> None:
    model.capabilities.reasoning.supported = False  # type: ignore[misc]


def _replace_metadata_entry(model: Model) -> None:
    model.metadata["github_copilot"] = {}  # type: ignore[index]


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (_set_model_id, FrozenInstanceError),
        (_set_vision, FrozenInstanceError),
        (_set_reasoning_supported, FrozenInstanceError),
        (_replace_metadata_entry, TypeError),
    ],
    ids=["model", "capabilities", "reasoning", "metadata"],
)
def test_records_are_immutable(mutate: Callable[[Model], None], error: type[Exception]) -> None:
    model = _model(
        "gpt-5.2",
        metadata={"github_copilot": {"supported_endpoints": ["/responses"]}},
    )

    with pytest.raises(error):
        mutate(model)
    assert model.metadata["github_copilot"]["supported_endpoints"] == ("/responses",)


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        pytest.param(
            {
                "vision": True,
                "input_modalities": ("Text", "Image", "image"),
                "output_modalities": ("Text", "Audio"),
                "supported_parameters": ("tools", "response_format", "tools"),
                "supported_voices": (" af_sky ", "af_aoede", "af_sky", ""),
            },
            {
                "input_modalities": ("text", "image"),
                "output_modalities": ("text", "audio"),
                "supported_parameters": ("response_format", "tools"),
                "supported_voices": ("af_aoede", "af_sky"),
                "task_types": (
                    "chat",
                    "text_output",
                    "image_input",
                    "image_understanding",
                    "audio_generation",
                ),
            },
            id="declared-lists-are-normalized",
        ),
        pytest.param(
            {"vision": True},
            {
                "input_modalities": ("text", "image"),
                "output_modalities": ("text",),
                "supported_parameters": (),
                "supported_voices": (),
                "task_types": ("chat", "text_output", "image_input", "image_understanding"),
            },
            id="vision-flag-implies-image-input",
        ),
        pytest.param(
            {
                "input_modalities": ("audio",),
                "output_modalities": ("audio",),
                "task_types": ("live_voice",),
            },
            {
                "input_modalities": ("audio",),
                "output_modalities": ("audio",),
                "supported_parameters": (),
                "supported_voices": (),
                "task_types": ("live_voice",),
            },
            id="explicit-task-types-win",
        ),
    ],
)
def test_capabilities_normalize_declared_facts(
    declared: dict[str, Any], expected: dict[str, tuple[str, ...]]
) -> None:
    facts = {"vision": False, **declared}
    capabilities = Capabilities(
        tools=False,
        json_mode=False,
        reasoning=ReasoningCapabilities(supported=False),
        **facts,
    )

    assert {field: getattr(capabilities, field) for field in expected} == expected


@pytest.mark.parametrize(
    ("inputs", "outputs", "task_types"),
    [
        (
            ("text", "image"),
            ("text", "image"),
            ("chat", "text_output", "image_input", "image_understanding", "image_generation"),
        ),
        # Generic audio output is not speech; only "speech" output means text_to_speech.
        (("text",), ("audio",), ("audio_generation",)),
        (("text",), ("speech",), ("audio_generation", "text_to_speech")),
        (("audio",), ("transcription",), ("text_output", "audio_input", "speech_to_text")),
        (("text", "audio"), ("text",), ("chat", "text_output", "audio_input", "speech_to_text")),
        # Embedding output is a vector, not text: no chat or text_output.
        (("text",), ("embeddings",), ("text_embedding",)),
        # live_voice is never derived; only an explicit task_types entry sets it.
        (("audio",), ("audio",), ("audio_input", "audio_generation")),
    ],
)
def test_task_types_derive_from_modalities(
    inputs: tuple[str, ...], outputs: tuple[str, ...], task_types: tuple[str, ...]
) -> None:
    assert derive_model_task_types(inputs, outputs) == task_types


@pytest.mark.parametrize(
    ("connections", "connection_id", "allowed"),
    [
        ((), "api-key", True),
        (("subscription",), "subscription", True),
        (("subscription",), "api-key", False),
    ],
)
def test_connection_allowlist(
    connections: tuple[str, ...], connection_id: str, allowed: bool
) -> None:
    assert _model("gpt-5.2", connections=connections).allows_connection(connection_id) is allowed


# ---------------------------------------------------------------------------
# Registry: record projection and lookup
# ---------------------------------------------------------------------------


def test_lookup_reads_the_fixture_catalogs() -> None:
    registry = ModelRegistry.load(FIXTURES_DIR)
    alpha = Model(
        model_id="model-alpha",
        name="Model Alpha",
        capabilities=Capabilities(
            vision=True,
            tools=False,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=False),
        ),
        context_window=32000,
        max_output_tokens=4096,
    )
    beta = Model(
        model_id="model-beta",
        name="Model Beta",
        capabilities=Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=True),
        ),
        context_window=128000,
        max_output_tokens=16384,
    )
    gamma = _model("model-gamma", "Model Gamma", context_window=64000, max_output_tokens=8192)

    assert registry.get("test_provider_a", "model-alpha") == alpha
    assert registry.list_for_provider("test_provider_a") == [alpha]
    assert registry.list_for_provider("test_provider_b") == [beta, gamma]
    assert registry.list_for_provider("nonexistent_provider") == []
    for provider_id, model_id in [
        ("nonexistent_provider", "some-model"),
        ("test_provider_a", "nonexistent-model"),
        ("test_provider_b", "model-alpha"),
    ]:
        with pytest.raises(KeyError, match=f"{provider_id}/{model_id}"):
            registry.get(provider_id, model_id)


def test_load_projects_every_record_fact(tmp_path: Path) -> None:
    ladder = {"supported": True, "control": "levels", "levels": ["low", "medium", "high"]}
    records = {
        "levels": _record(capabilities=_capabilities(reasoning=ladder)),
        "on-off": _record(
            capabilities=_capabilities(reasoning={"supported": True, "control": "on_off"})
        ),
        "budget": _record(
            capabilities=_capabilities(
                reasoning={"supported": True, "control": "budget", "budget_max": 32000}
            )
        ),
        "minimal": _record(capabilities=_capabilities(reasoning={"supported": True})),
        "family": _record(family="gpt-5.2"),
        "connections": _record(connections=["api-key"]),
        "metadata": _record(
            metadata={
                "github_copilot": {
                    "vendor": "OpenAI",
                    "supported_endpoints": ["/responses", "/chat/completions"],
                }
            }
        ),
        "voices": _record(
            capabilities=_capabilities(
                output_modalities=["speech"],
                supported_voices=["af_sky", "af_aoede", "af_bella"],
            )
        ),
        "null-limits": _record(context_window=None, max_output_tokens=None),
        "absent-limits": {
            key: value
            for key, value in _record().items()
            if key not in {"context_window", "max_output_tokens"}
        },
        "sampling-upper-bounds": _record(recommended_temperature=2, recommended_top_p=1),
        "sampling-lower-bounds": _record(recommended_temperature=0.0, recommended_top_p=0.0),
    }
    _write_catalog(tmp_path, "typed", records)

    def reasoning(**facts: Any) -> Capabilities:
        return replace(_PLAIN_CAPABILITIES, reasoning=ReasoningCapabilities(**facts))

    expected = {
        "levels": _model(
            "levels",
            capabilities=reasoning(
                supported=True, control="levels", levels=("low", "medium", "high")
            ),
        ),
        "on-off": _model("on-off", capabilities=reasoning(supported=True, control="on_off")),
        "budget": _model(
            "budget", capabilities=reasoning(supported=True, control="budget", budget_max=32000)
        ),
        "minimal": _model("minimal", capabilities=reasoning(supported=True)),
        "family": _model("family", family="gpt-5.2"),
        "connections": _model("connections", connections=("api-key",)),
        "metadata": _model(
            "metadata",
            metadata={
                "github_copilot": {
                    "vendor": "OpenAI",
                    "supported_endpoints": ("/responses", "/chat/completions"),
                }
            },
        ),
        "voices": _model(
            "voices",
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=False,
                reasoning=ReasoningCapabilities(supported=False),
                output_modalities=("speech",),
                supported_voices=("af_aoede", "af_bella", "af_sky"),
            ),
        ),
        "null-limits": _model("null-limits", context_window=None, max_output_tokens=None),
        "absent-limits": _model("absent-limits", context_window=None, max_output_tokens=None),
        "sampling-upper-bounds": _model(
            "sampling-upper-bounds", recommended_temperature=2.0, recommended_top_p=1.0
        ),
        "sampling-lower-bounds": _model(
            "sampling-lower-bounds", recommended_temperature=0.0, recommended_top_p=0.0
        ),
    }

    registry = ModelRegistry.load(tmp_path)

    assert {model.model_id: model for model in registry.list_for_provider("typed")} == expected
    upper = registry.get("typed", "sampling-upper-bounds")
    assert type(upper.recommended_temperature) is float
    assert type(upper.recommended_top_p) is float


@pytest.mark.parametrize(
    ("temperature", "top_p", "warning"),
    [
        (2.1, 1.1, "outside"),
        (True, True, "not a number"),
        (math.nan, math.nan, "not finite"),
    ],
    ids=["out-of-range", "bool", "nan"],
)
def test_invalid_recommended_sampling_is_ignored_without_hiding_the_model(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    temperature: object,
    top_p: object,
    warning: str,
) -> None:
    _write_catalog(
        tmp_path,
        "p",
        {"m": _record(recommended_temperature=temperature, recommended_top_p=top_p)},
    )

    with caplog.at_level(logging.WARNING, logger="vbot.models"):
        model = ModelRegistry.load(tmp_path).get("p", "m")

    assert (model.recommended_temperature, model.recommended_top_p) == (None, None)
    warned_fields = [
        record.getMessage().split()[0]
        for record in caplog.records
        if warning in record.getMessage()
    ]
    assert warned_fields == ["recommended_temperature", "recommended_top_p"]


# ---------------------------------------------------------------------------
# Registry: tolerance for invalid Model DB files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("files", "warnings"),
    [
        pytest.param(
            {"corrupt.json": '{"provider_id": "corrupt", "models":'},
            ["corrupt.json"],
            id="corrupt-provider-file",
        ),
        pytest.param(
            {"invalid.json": {"provider_id": "invalid", "models": []}},
            ["invalid.json"],
            id="provider-models-not-an-object",
        ),
        pytest.param(
            {
                "healthy.json": {
                    "provider_id": "healthy",
                    "models": {"model-a": _record("Generated"), "broken": ["not", "an", "object"]},
                }
            },
            ["healthy.json", "healthy/broken"],
            id="provider-entry-not-an-object",
        ),
        pytest.param(
            {"healthy.overrides.json": '{"models": {"model-a":'},
            ["healthy.overrides.json"],
            id="corrupt-override-file",
        ),
        pytest.param(
            {"healthy.overrides.json": {"models": {"model-a": "not an object"}}},
            ["healthy.overrides.json", "healthy/model-a"],
            id="override-entry-not-an-object",
        ),
        pytest.param(
            {
                "healthy.overrides.json": {
                    "reasoning_replay": "conservative",
                    "models": {"model-a": {"name": "Hidden"}},
                }
            },
            ["healthy.overrides.json", "reasoning_replay must be one of"],
            id="invalid-provider-replay-rejects-the-override-file",
        ),
        pytest.param(
            {
                "healthy.overrides.json": {
                    "models": {"model-b": _record("B", reasoning_replay="conservative")}
                }
            },
            ["healthy/model-b", "reasoning_replay must be one of"],
            id="invalid-model-replay-drops-only-that-model",
        ),
        pytest.param(
            {"models.json": '{"models":'},
            ["models.json"],
            id="corrupt-canonical-file",
        ),
        pytest.param(
            {"healthy.overrides.json": {"models": {"retired": {"name": "Retired"}}}},
            ["healthy/retired", "missing required field 'capabilities'", "Override-only"],
            id="override-only-entry-missing-a-required-field",
        ),
    ],
)
def test_invalid_model_db_files_are_skipped_with_a_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    files: dict[str, object],
    warnings: list[str],
) -> None:
    _write_catalog(tmp_path, "healthy", {"model-a": _record("Generated")})
    for name, payload in files.items():
        _write_json(tmp_path / "models" / name, payload)

    with caplog.at_level(logging.WARNING, logger="vbot.models"):
        # Staged-refresh validation collects what Load ignores without logging it.
        issues = ModelRegistry.validate(tmp_path)
        assert caplog.records == []
        registry = ModelRegistry.load(tmp_path)

    assert [
        (provider_id, model.model_id, model.name)
        for provider_id, model in registry.query(ModelQuery())
    ] == [("healthy", "model-a", "Generated")]
    assert registry.provider_reasoning_replay("healthy") is None
    for warning in warnings:
        assert warning in caplog.text
    assert issues == [record.getMessage() for record in caplog.records]


def test_model_directory_scan_failure_does_not_raise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    models_dir = tmp_path / "models"
    models_dir.mkdir()

    def fail_scan(*_args: object, **_kwargs: object) -> list[Path]:
        raise OSError("scan failed")

    monkeypatch.setattr(Path, "glob", fail_scan)

    with caplog.at_level(logging.WARNING, logger="vbot.models"):
        registry = ModelRegistry.load(tmp_path)

    assert registry.query(ModelQuery()) == []
    assert str(models_dir) in caplog.text


# ---------------------------------------------------------------------------
# Registry: the override layer as files on disk
# ---------------------------------------------------------------------------


def test_override_files_apply_at_load_without_becoming_providers(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    models_dir = tmp_path / "models"
    _write_catalog(tmp_path, "openrouter", {"model-a": _record("Model A", context_window=1000)})
    _write_json(
        models_dir / "openrouter.overrides.json",
        {
            "models": {
                "model-a": {"name": "Corrected Model A"},
                "override-only": _record("Override Only"),
            }
        },
    )
    _write_json(
        models_dir / "openrouter.raw.json",
        {
            "provider_id": "openrouter",
            "fetched_at": "2026-01-01T00:00:00+00:00",
            "raw_response": {},
        },
    )
    # A hand-only Provider needs no generated file; its id comes from the file name.
    _write_json(
        models_dir / "hand-only.overrides.json", {"models": {"model-a": _record("Hand Only")}}
    )

    with caplog.at_level(logging.WARNING, logger="vbot.models"):
        registry = ModelRegistry.load(tmp_path)

    assert [
        (provider_id, model.model_id, model.name, model.context_window)
        for provider_id, model in registry.query(ModelQuery())
    ] == [
        ("hand-only", "model-a", "Hand Only", 32000),
        ("openrouter", "model-a", "Corrected Model A", 1000),
        ("openrouter", "override-only", "Override Only", 32000),
    ]
    # Neither the override nor the raw file was read as a Provider catalog.
    assert caplog.records == []


# ---------------------------------------------------------------------------
# Registry: cache and reload
# ---------------------------------------------------------------------------


def test_cache_serves_one_instance_until_invalidated(tmp_path: Path) -> None:
    """Provider and canonical edits both wait for ``invalidate`` (refresh-then-reload)."""

    def write(name: str, family: str) -> None:
        _write_catalog(tmp_path, "p", {"lab/model": {"name": name}})
        _write_json(
            tmp_path / "models" / "models.json", {"models": {"lab/model": _record(family=family)}}
        )

    write("Original", "v1")
    first = ModelRegistry.load(tmp_path)
    write("Updated", "v2")

    assert ModelRegistry.load(tmp_path) is first
    assert (first.get("p", "lab/model").name, first.get("p", "lab/model").family) == (
        "Original",
        "v1",
    )

    ModelRegistry.invalidate(tmp_path)
    second = ModelRegistry.load(tmp_path)

    assert second is not first
    assert (second.get("p", "lab/model").name, second.get("p", "lab/model").family) == (
        "Updated",
        "v2",
    )


def test_reload_swaps_contents_in_place_and_repoints_the_cache(tmp_path: Path) -> None:
    """Holders that captured the registry see the new catalog without re-wiring."""

    def write(name: str, provider_replay: str, model_replay: str) -> None:
        reasoning = _capabilities(reasoning={"supported": True})
        _write_catalog(
            tmp_path,
            "provider",
            {
                model_id: _record(name, capabilities=reasoning)
                for model_id in ("inherited", "overridden")
            },
        )
        _write_json(
            tmp_path / "models" / "provider.overrides.json",
            {
                "reasoning_replay": provider_replay,
                "models": {"overridden": {"reasoning_replay": model_replay}},
            },
        )

    write("Original", "current_run", "full_history")
    registry = ModelRegistry.load(tmp_path)

    assert registry.provider_reasoning_replay("provider") == "current_run"
    assert registry.get("provider", "inherited").reasoning_replay is None
    assert registry.get("provider", "overridden").reasoning_replay == "full_history"

    write("Updated", "full_history", "none")
    ModelRegistry.invalidate(tmp_path)
    registry.reload(tmp_path)

    assert registry.get("provider", "inherited").name == "Updated"
    assert registry.provider_reasoning_replay("provider") == "full_history"
    assert registry.get("provider", "overridden").reasoning_replay == "none"
    assert ModelRegistry.load(tmp_path) is registry


@pytest.mark.asyncio
async def test_reload_async_assembles_off_the_loop_and_swaps_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_catalog(tmp_path, "test_provider", {"model-a": _record("Original")})
    registry = ModelRegistry.load(tmp_path)
    _write_catalog(tmp_path, "test_provider", {"model-a": _record("Updated")})
    assemble = ModelRegistry._assemble_models.__func__  # type: ignore[attr-defined]
    entered = threading.Event()
    release = threading.Event()
    threads: list[int] = []

    def blocked_assemble(cls: Any, *args: Any) -> Any:
        threads.append(threading.get_ident())
        entered.set()
        release.wait(timeout=5)
        return assemble(cls, *args)

    monkeypatch.setattr(ModelRegistry, "_assemble_models", classmethod(blocked_assemble))
    reloading = asyncio.create_task(registry.reload_async(tmp_path))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        loop = asyncio.get_running_loop()
        ticked_at = loop.time()
        for _ in range(5):
            await asyncio.sleep(0.01)
        assert loop.time() - ticked_at < 1
        # Readers on the loop keep the old catalog until the swap.
        assert registry.get("test_provider", "model-a").name == "Original"
    finally:
        release.set()
    await asyncio.wait_for(reloading, timeout=5)

    assert threads and threading.get_ident() not in threads
    assert registry.get("test_provider", "model-a").name == "Updated"
    assert ModelRegistry.load(tmp_path) is registry


@pytest.mark.asyncio
@pytest.mark.parametrize("newer_async", [False, True], ids=["sync", "async"])
async def test_newer_reload_supersedes_an_in_flight_assembly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, newer_async: bool
) -> None:
    models_dir = tmp_path / "models"
    _write_catalog(tmp_path, "test_provider", {"model-a": _record("Original")})
    registry = ModelRegistry.load(tmp_path)
    _write_catalog(tmp_path, "test_provider", {"model-a": _record("Stale")})
    assemble = ModelRegistry._assemble_models.__func__  # type: ignore[attr-defined]
    stale_assembled = threading.Event()
    release_stale = threading.Event()
    latest_assembled = threading.Event()
    release_latest = threading.Event()

    def blocked_assemble(cls: Any, *args: Any) -> Any:
        result = assemble(cls, *args)
        if result[0][("test_provider", "model-a")].name == "Stale":
            stale_assembled.set()
            assert release_stale.wait(timeout=5)
        elif newer_async:
            latest_assembled.set()
            assert release_latest.wait(timeout=5)
        return result

    monkeypatch.setattr(ModelRegistry, "_assemble_models", classmethod(blocked_assemble))
    stale_reload = asyncio.create_task(registry.reload_async(tmp_path))
    latest_reload = None
    try:
        assert await asyncio.to_thread(stale_assembled.wait, 5)
        _write_catalog(tmp_path, "test_provider", {"model-a": _record("Latest")})
        _write_json(
            models_dir / "test_provider.overrides.json",
            {"reasoning_replay": "current_run", "models": {}},
        )
        if newer_async:
            latest_reload = asyncio.create_task(registry.reload_async(tmp_path))
            # The newer request enters before the old worker is released.
            await asyncio.sleep(0)
            release_stale.set()
            await asyncio.wait_for(stale_reload, timeout=5)
            assert await asyncio.to_thread(latest_assembled.wait, 5)
            # Keep the last published catalog while the latest one assembles.
            assert registry.get("test_provider", "model-a").name == "Original"
            release_latest.set()
            await asyncio.wait_for(latest_reload, timeout=5)
        else:
            registry.reload(tmp_path)
            assert registry.get("test_provider", "model-a").name == "Latest"
            release_stale.set()
            await asyncio.wait_for(stale_reload, timeout=5)

        assert registry.get("test_provider", "model-a").name == "Latest"
        assert registry.provider_reasoning_replay("test_provider") == "current_run"
        assert ModelRegistry.load(tmp_path) is registry
    finally:
        release_stale.set()
        release_latest.set()
        await asyncio.wait_for(stale_reload, timeout=5)
        if latest_reload is not None:
            await asyncio.wait_for(latest_reload, timeout=5)
