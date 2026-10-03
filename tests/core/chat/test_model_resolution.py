"""Model resolution rules: Model bindings, request sampling parameters, input modalities,
Provider Connections and the fallback chain."""

from __future__ import annotations

import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.chat import model_resolution
from core.chat.errors import ChatError
from core.chat.model_resolution import (
    _model_input_modalities,
    _resolve_agent_connection,
    _resolve_fallback_chain,
    parse_bare_model,
    parse_model_with_connection,
)
from core.utils.errors import ConfigError
from core.utils.log_conditions import LoggedConditions


def _agent(model: str, *, fallback_models: list[str] | None = None) -> Any:
    return cast(Any, SimpleNamespace(model=model, fallback_models=fallback_models or []))


def _runtime_for_connection(
    *, usable: set[str], models: dict[tuple[str, str], tuple[str, ...]]
) -> Any:
    """A runtime whose ``openai`` Provider lists the api-key, then the subscription Connection.

    ``usable`` holds the full ``<provider>:<connection>`` ids with credentials; ``models``
    maps ``(provider_id, model_id)`` to its Connection allowlist, and any other Model is
    unknown to the registry.
    """
    provider_config = SimpleNamespace(
        connections=[SimpleNamespace(id="api-key"), SimpleNamespace(id="subscription")]
    )

    def models_get(provider_id: str, model_id: str) -> Any:
        if (provider_id, model_id) not in models:
            raise KeyError(model_id)
        return SimpleNamespace(connections=models[(provider_id, model_id)])

    return cast(
        Any,
        SimpleNamespace(
            providers=SimpleNamespace(get=lambda _provider_id: provider_config),
            provider_credentials=SimpleNamespace(
                is_usable=lambda _provider_id, connection_id: connection_id in usable
            ),
            models=SimpleNamespace(get=models_get),
        ),
    )


CONNECTION_BOUND = {("openai", "codex-auto-review"): ("subscription",), ("openai", "gpt-5.2"): ()}
BOTH_USABLE = {"openai:api-key", "openai:subscription"}


@pytest.mark.parametrize(
    ("model", "parts"),
    [
        ("openai/gpt-5.2", ("openai", "gpt-5.2", "")),
        ("openai/gpt-5.2::oauth", ("openai", "gpt-5.2", "oauth")),
        (
            "openrouter/anthropic/claude-sonnet-4::oauth",
            ("openrouter", "anthropic/claude-sonnet-4", "oauth"),
        ),
        (
            "openrouter/poolside/laguna-xs.2:free::api-key",
            ("openrouter", "poolside/laguna-xs.2:free", "api-key"),
        ),
    ],
    ids=["no-suffix", "suffix", "slashes-in-model-id", "colon-in-model-id"],
)
def test_model_binding_splits_into_provider_model_and_connection(
    model: str, parts: tuple[str, str, str]
) -> None:
    provider_id, model_id, _connection = parts

    assert parse_model_with_connection(model) == parts
    assert parse_bare_model(model) == f"{provider_id}/{model_id}"


@pytest.mark.parametrize(
    "model", ["", "openai/gpt-5.2::", "no-slash-model"], ids=["empty", "dangling-suffix", "bare"]
)
def test_invalid_model_binding_is_a_chat_error(model: str) -> None:
    with pytest.raises(ChatError):
        parse_model_with_connection(model)


def _raising(error: BaseException) -> Callable[[str, str], Any]:
    def get(_provider_id: str, _model_id: str) -> Any:
        raise error

    return get


@pytest.fixture
def fresh_modality_conditions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start from a process that has logged no modality lookup failure yet."""
    monkeypatch.setattr(model_resolution, "_MODALITY_CONDITIONS", LoggedConditions())


def _chat_records(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "vbot.chat" and record.levelno >= logging.INFO
    ]


@pytest.mark.parametrize(
    ("model", "models_get", "modalities"),
    [
        (
            "openai/gpt-5.2",
            lambda _provider_id, _model_id: SimpleNamespace(
                capabilities=SimpleNamespace(input_modalities=("text", "image"))
            ),
            {"text", "image"},
        ),
        ("openai/ghost", _raising(KeyError("Model not found: openai/ghost")), set()),
        ("openai/gpt-5.2", _raising(ChatError("boom")), set()),
        ("no-slash-model", _raising(AssertionError("registry must not be consulted")), set()),
    ],
    ids=["known-model", "unknown-model", "registry-error", "malformed-binding"],
)
@pytest.mark.usefixtures("fresh_modality_conditions")
def test_input_modalities_degrade_visibly_to_none(
    caplog: pytest.LogCaptureFixture,
    model: str,
    models_get: Callable[[str, str], Any],
    modalities: set[str],
) -> None:
    caplog.set_level(logging.WARNING, logger="vbot.chat")
    runtime = cast(Any, SimpleNamespace(models=SimpleNamespace(get=models_get)))

    assert _model_input_modalities(runtime, _agent(model)) == frozenset(modalities)

    # No modalities silently drop image and audio attachments, so the degrade warns.
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "vbot.chat" and record.levelno == logging.WARNING
    ]
    assert len(warnings) == (0 if modalities else 1)
    assert all(model in warning for warning in warnings)


@pytest.mark.usefixtures("fresh_modality_conditions")
def test_input_modality_lookup_failure_logs_on_transitions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="vbot.chat")
    lookup: dict[str, Callable[[str, str], Any]] = {"get": _raising(KeyError("openai/ghost"))}
    runtime = cast(
        Any,
        SimpleNamespace(
            models=SimpleNamespace(get=lambda provider, model: lookup["get"](provider, model))
        ),
    )
    agent = _agent("openai/ghost")

    # Every Run resolves the modalities; a persisting failure warns once.
    _model_input_modalities(runtime, agent)
    _model_input_modalities(runtime, agent)
    assert [level for level, _ in _chat_records(caplog)] == [logging.WARNING]

    # Another failure kind is a changed condition and warns again.
    lookup["get"] = _raising(ChatError("registry unavailable"))
    _model_input_modalities(runtime, agent)
    _model_input_modalities(runtime, agent)
    assert [level for level, _ in _chat_records(caplog)] == [logging.WARNING] * 2

    # Resolving again logs the recovery once; a later failure is a new condition.
    lookup["get"] = lambda _provider, _model: SimpleNamespace(
        capabilities=SimpleNamespace(input_modalities=("text",))
    )
    _model_input_modalities(runtime, agent)
    _model_input_modalities(runtime, agent)
    lookup["get"] = _raising(KeyError("openai/ghost"))
    _model_input_modalities(runtime, agent)
    levels = [level for level, _ in _chat_records(caplog)]
    assert levels == [logging.WARNING, logging.WARNING, logging.INFO, logging.WARNING]
    assert all("openai/ghost" in message for _, message in _chat_records(caplog))


@pytest.mark.parametrize(
    ("model", "usable", "connection"),
    [
        ("openai/codex-auto-review", BOTH_USABLE, "openai:subscription"),
        ("openai/gpt-5.2", BOTH_USABLE, "openai:api-key"),
        ("openai/custom-thing", BOTH_USABLE, "openai:api-key"),
        ("openai/codex-auto-review::subscription", BOTH_USABLE, "openai:subscription"),
        ("openai/codex-auto-review", {"openai:api-key"}, None),
    ],
    ids=[
        "connection-bound-model",
        "unrestricted-model",
        "unknown-model",
        "explicit-suffix",
        "no-allowed-connection-usable",
    ],
)
def test_agent_connection_is_the_first_usable_one_the_model_allows(
    model: str, usable: set[str], connection: str | None
) -> None:
    # A connection-bound Model never lands on the api-key Connection just because it
    # is configured first; an unknown Model is unrestricted.
    runtime = _runtime_for_connection(usable=usable, models=CONNECTION_BOUND)

    if connection is None:
        with pytest.raises(ChatError):
            _resolve_agent_connection(runtime, _agent(model))
    else:
        assert _resolve_agent_connection(runtime, _agent(model)) == ("openai", connection)


def test_fallback_chain_keeps_only_resolvable_distinct_candidates_in_order() -> None:
    runtime = _runtime_for_connection(
        usable=BOTH_USABLE,
        models={**CONNECTION_BOUND, ("openai", "enterprise-only"): ("enterprise",)},
    )

    def is_usable(_provider_id: str, connection_id: str) -> bool:
        if connection_id == "openai:missing":
            raise ConfigError("Unknown connection: openai:missing")
        return connection_id in BOTH_USABLE

    runtime.provider_credentials.is_usable = is_usable
    agent = _agent(
        "openai/gpt-5.2::api-key",
        fallback_models=[
            "ghost/ghost-model::api-key",
            "openai/codex-auto-review",
            "openai/gpt-5.2::api-key",
            "openai/dead::missing",
            "openai/enterprise-only",
            "not-a-binding",
            "openai/working::subscription",
            "openai/codex-auto-review",
        ],
    )

    # Uncredentialed, unknown, disallowed and malformed candidates are skipped, as are
    # the primary binding and repeats; one broken entry never drops the rest.
    assert _resolve_fallback_chain(runtime, agent) == [
        ("openai/codex-auto-review", "openai", "openai:subscription"),
        ("openai/working::subscription", "openai", "openai:subscription"),
    ]
    assert _resolve_fallback_chain(runtime, _agent("openai/gpt-5.2")) == []
