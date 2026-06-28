"""Tests for chat model identifier and modality resolution helpers."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.chat.errors import ChatError
from core.chat.model_resolution import (
    _first_usable_connection_id,
    _model_connection_allowlist,
    _model_input_modalities,
    _resolve_agent_connection,
    _resolve_fallback,
)


def _runtime_with_models_get(get: Any) -> Any:
    return cast(Any, SimpleNamespace(models=SimpleNamespace(get=get)))


def _agent(model: str, *, fallback_model: str = "") -> Any:
    return cast(Any, SimpleNamespace(model=model, fallback_model=fallback_model))


def _runtime_for_connection(
    *,
    provider_connections: list[str],
    usable: set[str],
    models: dict[tuple[str, str], tuple[str, ...]] | None = None,
) -> Any:
    """Build a runtime stub for connection resolution.

    ``provider_connections`` is the provider's connection ids in config order;
    ``usable`` is the set of full ``<provider>:<connection>`` ids with credentials;
    ``models`` maps ``(provider_id, model_id)`` to its connection allowlist
    (a missing entry raises ``KeyError``, i.e. an unknown/custom model).
    """
    provider_config = SimpleNamespace(
        connections=[SimpleNamespace(id=connection_id) for connection_id in provider_connections]
    )

    def models_get(provider_id: str, model_id: str) -> Any:
        if models is None or (provider_id, model_id) not in models:
            raise KeyError(model_id)
        return SimpleNamespace(connections=models[(provider_id, model_id)])

    return cast(
        Any,
        SimpleNamespace(
            providers=SimpleNamespace(get=lambda _provider_id: provider_config),
            provider_credentials=SimpleNamespace(
                has_credentials=lambda _provider_id, connection_id: connection_id in usable
            ),
            models=SimpleNamespace(get=models_get),
        ),
    )


class TestModelInputModalities:
    def test_returns_model_input_modalities_on_success(self) -> None:
        model = SimpleNamespace(capabilities=SimpleNamespace(input_modalities=("text", "image")))
        runtime = _runtime_with_models_get(lambda _provider, _model: model)

        modalities = _model_input_modalities(runtime, _agent("openai/gpt-5.2"))

        assert modalities == frozenset({"text", "image"})

    def test_unknown_model_logs_warning_and_returns_empty(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def raise_key_error(_provider: str, _model: str) -> Any:
            raise KeyError("Model not found: openai/ghost")

        runtime = _runtime_with_models_get(raise_key_error)

        caplog.set_level(logging.WARNING, logger="vbot.chat")
        modalities = _model_input_modalities(runtime, _agent("openai/ghost"))

        assert modalities == frozenset()
        warning_records = [
            record
            for record in caplog.records
            if record.name == "vbot.chat" and record.levelno == logging.WARNING
        ]
        assert len(warning_records) == 1
        assert "openai/ghost" in warning_records[0].getMessage()

    def test_malformed_agent_model_logs_warning_and_returns_empty(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A bare model with no "<provider>/<model-id>" makes _split_agent_model
        # raise ChatError before the registry is consulted.
        def unreachable_get(_provider: str, _model: str) -> Any:
            raise AssertionError("registry should not be consulted for a malformed model")

        runtime = _runtime_with_models_get(unreachable_get)

        caplog.set_level(logging.WARNING, logger="vbot.chat")
        modalities = _model_input_modalities(runtime, _agent("no-slash-model"))

        assert modalities == frozenset()
        warning_records = [
            record
            for record in caplog.records
            if record.name == "vbot.chat" and record.levelno == logging.WARNING
        ]
        assert len(warning_records) == 1
        assert "no-slash-model" in warning_records[0].getMessage()

    def test_chat_error_is_caught_and_does_not_propagate(self) -> None:
        def raise_chat_error(_provider: str, _model: str) -> Any:
            raise ChatError("boom")

        runtime = _runtime_with_models_get(raise_chat_error)

        assert _model_input_modalities(runtime, _agent("openai/gpt-5.2")) == frozenset()


class TestModelConnectionAllowlist:
    def test_returns_model_connections(self) -> None:
        runtime = _runtime_with_models_get(
            lambda _provider, _model: SimpleNamespace(connections=("subscription",))
        )

        assert _model_connection_allowlist(runtime, "openai", "codex-auto-review") == (
            "subscription",
        )

    def test_unknown_model_is_unrestricted(self) -> None:
        def raise_key_error(_provider: str, _model: str) -> Any:
            raise KeyError("Model not found")

        runtime = _runtime_with_models_get(raise_key_error)

        assert _model_connection_allowlist(runtime, "openai", "ghost") == ()


class TestFirstUsableConnectionId:
    def test_allowlist_skips_a_forbidden_first_connection(self) -> None:
        # api-key is usable and listed first, but the allowlist permits only
        # subscription, so the choke point must skip past api-key.
        runtime = _runtime_for_connection(
            provider_connections=["api-key", "subscription"],
            usable={"openai:api-key", "openai:subscription"},
        )

        assert (
            _first_usable_connection_id(runtime, "openai", ("subscription",))
            == "openai:subscription"
        )

    def test_empty_allowlist_picks_first_usable(self) -> None:
        runtime = _runtime_for_connection(
            provider_connections=["api-key", "subscription"],
            usable={"openai:api-key", "openai:subscription"},
        )

        assert _first_usable_connection_id(runtime, "openai", ()) == "openai:api-key"

    def test_raises_clear_error_when_no_allowed_connection_is_usable(self) -> None:
        runtime = _runtime_for_connection(
            provider_connections=["api-key", "subscription"],
            usable={"openai:api-key"},
        )

        with pytest.raises(ChatError, match=r"allowlist \[subscription\]"):
            _first_usable_connection_id(runtime, "openai", ("subscription",))


class TestResolveAgentConnection:
    def test_bare_connection_bound_model_resolves_to_allowed_connection(self) -> None:
        # The reported bug: a bare subscription-only model must not land on the
        # api-key connection even though it is configured and listed first.
        runtime = _runtime_for_connection(
            provider_connections=["api-key", "subscription"],
            usable={"openai:api-key", "openai:subscription"},
            models={("openai", "codex-auto-review"): ("subscription",)},
        )

        provider_id, connection_id = _resolve_agent_connection(
            runtime, _agent("openai/codex-auto-review")
        )

        assert provider_id == "openai"
        assert connection_id == "openai:subscription"

    def test_bare_unrestricted_model_picks_first_usable_connection(self) -> None:
        runtime = _runtime_for_connection(
            provider_connections=["api-key", "subscription"],
            usable={"openai:api-key", "openai:subscription"},
            models={("openai", "gpt-5.2"): ()},
        )

        _provider_id, connection_id = _resolve_agent_connection(runtime, _agent("openai/gpt-5.2"))

        assert connection_id == "openai:api-key"

    def test_explicit_connection_suffix_is_used_verbatim(self) -> None:
        runtime = _runtime_for_connection(
            provider_connections=["api-key", "subscription"],
            usable={"openai:api-key", "openai:subscription"},
            models={("openai", "codex-auto-review"): ("subscription",)},
        )

        _provider_id, connection_id = _resolve_agent_connection(
            runtime, _agent("openai/codex-auto-review::subscription")
        )

        assert connection_id == "openai:subscription"

    def test_bare_model_errors_when_no_allowed_connection_is_usable(self) -> None:
        runtime = _runtime_for_connection(
            provider_connections=["api-key", "subscription"],
            usable={"openai:api-key"},
            models={("openai", "codex-auto-review"): ("subscription",)},
        )

        with pytest.raises(ChatError, match="allowlist"):
            _resolve_agent_connection(runtime, _agent("openai/codex-auto-review"))

    def test_unknown_bare_model_is_unrestricted(self) -> None:
        runtime = _runtime_for_connection(
            provider_connections=["api-key", "subscription"],
            usable={"openai:api-key", "openai:subscription"},
            models={},
        )

        _provider_id, connection_id = _resolve_agent_connection(
            runtime, _agent("openai/custom-thing")
        )

        assert connection_id == "openai:api-key"


class TestResolveFallback:
    def test_bare_connection_bound_fallback_resolves_to_allowed_connection(self) -> None:
        runtime = _runtime_for_connection(
            provider_connections=["api-key", "subscription"],
            usable={"openai:api-key", "openai:subscription"},
            models={("openai", "codex-auto-review"): ("subscription",)},
        )
        agent = _agent("openai/gpt-5.2::api-key", fallback_model="openai/codex-auto-review")

        assert _resolve_fallback(runtime, agent) == (
            "openai/codex-auto-review",
            "openai",
            "openai:subscription",
        )

    def test_returns_none_when_no_allowed_connection_is_usable(self) -> None:
        runtime = _runtime_for_connection(
            provider_connections=["api-key", "subscription"],
            usable={"openai:api-key"},
            models={("openai", "codex-auto-review"): ("subscription",)},
        )
        agent = _agent("openai/gpt-5.2::api-key", fallback_model="openai/codex-auto-review")

        assert _resolve_fallback(runtime, agent) is None
