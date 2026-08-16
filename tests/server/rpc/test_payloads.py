"""Tests for shared RPC payload mappers (model payload effective window)."""

from __future__ import annotations

from types import SimpleNamespace

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.providers import LOCAL_CONTEXT_DEFAULT_CAP
from server.rpc.payloads import _model_detail_response, _model_response, _resolve_context_window


def _model(
    model_id: str,
    *,
    context_window: int | None,
    metadata: dict | None = None,
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
    )


class TestModelResponseEffectiveContextWindow:
    def test_non_local_model_carries_raw_window(self) -> None:
        """Remote models keep the raw window — null stays null (honest unknown)."""
        # Arrange / Act
        with_window = _model_response("openai", _model("gpt-5.2", context_window=256000))
        without_window = _model_response("custom", _model("mystery", context_window=None))

        # Assert
        assert with_window["effective_context_window"] == 256000
        assert without_window["effective_context_window"] is None
        assert without_window["context_window"] is None

    def test_local_model_resolves_capped_window(self) -> None:
        # Arrange
        model = _model(
            "ministral-3:8b",
            context_window=262144,
            metadata={"ollama": {"local": True}},
        )

        # Act
        payload = _model_response("ollama", model)

        # Assert — raw stays raw, effective is capped.
        assert payload["context_window"] == 262144
        assert payload["effective_context_window"] == LOCAL_CONTEXT_DEFAULT_CAP

    def test_local_model_uses_user_setting(self) -> None:
        # Arrange
        model = _model(
            "ministral-3:8b",
            context_window=262144,
            metadata={"ollama": {"local": True}},
        )

        # Act
        payload = _model_response(
            "ollama",
            model,
            local_context_windows={"ollama/ministral-3:8b": 16384},
        )

        # Assert
        assert payload["effective_context_window"] == 16384

    def test_proxied_cloud_model_trusts_reported_window(self) -> None:
        # Arrange
        model = _model(
            "kimi-k2.6:cloud",
            context_window=262144,
            metadata={"ollama": {"remote": True}},
        )

        # Act
        payload = _model_response("ollama", model)

        # Assert — no cap, no knob for cloud models.
        assert payload["effective_context_window"] == 262144


class TestModelDetailResponseRecommendedTemperature:
    def test_recommended_temperature_is_projected(self) -> None:
        model = Model(
            model_id="glm-5.2",
            name="GLM-5.2",
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=False,
                reasoning=ReasoningCapabilities(supported=True),
            ),
            context_window=976000,
            max_output_tokens=65536,
            recommended_temperature=1.0,
        )
        payload = _model_detail_response("ollama-cloud", model)
        assert payload["recommended_temperature"] == 1.0

    def test_none_recommended_temperature_is_projected_as_none(self) -> None:
        model = _model("gpt-5.2", context_window=128000)
        payload = _model_detail_response("openai", model)
        assert payload["recommended_temperature"] is None


class TestResolveContextWindowForAgentPayload:
    def _state(self, model: Model, *, local_windows: dict | None = None) -> SimpleNamespace:
        class _Models:
            def get(self, provider_id: str, model_id: str) -> Model:
                if (provider_id, model_id) != ("ollama", model.model_id):
                    raise KeyError((provider_id, model_id))
                return model

        class _Storage:
            def load_local_models_settings(self) -> dict:
                return {"context_windows": dict(local_windows or {})}

        class _Providers:
            def get(self, provider_id: str):
                raise KeyError(provider_id)

        return SimpleNamespace(
            runtime=SimpleNamespace(models=_Models(), storage=_Storage(), providers=_Providers())
        )

    def test_agent_window_uses_effective_resolution_for_local_model(self) -> None:
        # Arrange
        model = _model(
            "ministral-3:8b",
            context_window=262144,
            metadata={"ollama": {"local": True}},
        )
        state = self._state(model, local_windows={"ollama/ministral-3:8b": 16384})

        # Act / Assert
        assert _resolve_context_window(state, "ollama/ministral-3:8b") == 16384

    def test_agent_window_defaults_to_cap_for_local_model(self) -> None:
        # Arrange
        model = _model(
            "ministral-3:8b",
            context_window=262144,
            metadata={"ollama": {"local": True}},
        )
        state = self._state(model)

        # Act / Assert
        assert _resolve_context_window(state, "ollama/ministral-3:8b") == LOCAL_CONTEXT_DEFAULT_CAP
