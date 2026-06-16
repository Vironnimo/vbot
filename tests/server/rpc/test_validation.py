"""Tests for the shared model/connection save-time guard.

``_ensure_model_connection_supported`` is the server-side mirror of the WebUI
dropdown filter: it rejects a saved model whose pinned connection the model's
allowlist forbids, while staying silent whenever there is nothing to check.
"""

from __future__ import annotations

import pytest

from core.models import Capabilities, Model, ReasoningCapabilities
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import _ensure_model_connection_supported


def _model(connections: tuple[str, ...]) -> Model:
    return Model(
        model_id="gpt-5.4",
        name="GPT-5.4",
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
        ),
        context_window=128000,
        max_output_tokens=16000,
        connections=connections,
    )


class _StubModels:
    """Minimal model registry: known (provider, model) pairs, else ``KeyError``."""

    def __init__(self, models: dict[tuple[str, str], Model]) -> None:
        self._models = models

    def get(self, provider_id: str, model_id: str) -> Model:
        try:
            return self._models[(provider_id, model_id)]
        except KeyError as exc:
            raise KeyError(f"Model not found: {provider_id}/{model_id}") from exc


def _models_with(connections: tuple[str, ...]) -> _StubModels:
    return _StubModels({("openai", "gpt-5.4"): _model(connections)})


def test_rejects_connection_outside_allowlist() -> None:
    models = _models_with(("subscription",))

    with pytest.raises(RpcError) as exc_info:
        _ensure_model_connection_supported(models, "model", "openai/gpt-5.4::api-key")

    error = exc_info.value
    assert error.code == RPC_ERROR_INVALID_REQUEST
    assert "openai/gpt-5.4" in error.message
    assert "api-key" in error.message
    assert "subscription" in error.message


def test_rejects_account_pinned_connection_outside_allowlist() -> None:
    """The account suffix is ignored — only the connection part is checked."""
    models = _models_with(("subscription",))

    with pytest.raises(RpcError):
        _ensure_model_connection_supported(models, "model", "openai/gpt-5.4::api-key:work")


def test_allows_connection_in_allowlist() -> None:
    models = _models_with(("subscription",))

    _ensure_model_connection_supported(models, "model", "openai/gpt-5.4::subscription")


def test_empty_allowlist_permits_any_connection() -> None:
    models = _models_with(())

    _ensure_model_connection_supported(models, "model", "openai/gpt-5.4::api-key")


def test_no_pinned_connection_is_not_checked() -> None:
    models = _models_with(("subscription",))

    _ensure_model_connection_supported(models, "model", "openai/gpt-5.4")


def test_empty_model_string_is_not_checked() -> None:
    models = _models_with(("subscription",))

    _ensure_model_connection_supported(models, "fallback_model", "")


def test_unknown_model_is_not_checked() -> None:
    """A custom/absent model has no allowlist to validate against."""
    models = _models_with(("subscription",))

    _ensure_model_connection_supported(models, "model", "openai/custom-model::api-key")


def test_malformed_model_string_is_not_checked() -> None:
    """A model string without a provider prefix is surfaced at run time, not here."""
    models = _models_with(("subscription",))

    _ensure_model_connection_supported(models, "model", "garbage::api-key")
