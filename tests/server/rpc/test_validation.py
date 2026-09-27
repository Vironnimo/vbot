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


@pytest.mark.parametrize(
    "model", ["openai/gpt-5.4::api-key", "openai/gpt-5.4::api-key:work"], ids=["plain", "account"]
)
def test_rejects_a_pinned_connection_outside_the_allowlist(model: str) -> None:
    # An account suffix is ignored: only the Connection part is checked.
    with pytest.raises(RpcError) as exc_info:
        _ensure_model_connection_supported(_models_with(("subscription",)), "model", model)

    error = exc_info.value
    assert error.code == RPC_ERROR_INVALID_REQUEST
    assert "openai/gpt-5.4" in error.message
    assert "api-key" in error.message
    assert "subscription" in error.message


@pytest.mark.parametrize(
    ("allowlist", "model"),
    [
        pytest.param(("subscription",), "openai/gpt-5.4::subscription", id="allowed-connection"),
        pytest.param((), "openai/gpt-5.4::api-key", id="empty-allowlist-permits-any"),
        pytest.param(("subscription",), "openai/gpt-5.4", id="no-pinned-connection"),
        pytest.param(("subscription",), "", id="empty-model-string"),
        # A custom or absent Model has no allowlist to validate against.
        pytest.param(("subscription",), "openai/custom-model::api-key", id="unknown-model"),
        # A Model string without a Provider prefix is surfaced at Run time, not here.
        pytest.param(("subscription",), "garbage::api-key", id="malformed-model-string"),
    ],
)
def test_accepts_whenever_there_is_nothing_to_reject(
    allowlist: tuple[str, ...], model: str
) -> None:
    _ensure_model_connection_supported(_models_with(allowlist), "model", model)
