"""Tests for chat model identifier and modality resolution helpers."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.chat.errors import ChatError
from core.chat.model_resolution import _model_input_modalities


def _runtime_with_models_get(get: Any) -> Any:
    return cast(Any, SimpleNamespace(models=SimpleNamespace(get=get)))


def _agent(model: str) -> Any:
    return cast(Any, SimpleNamespace(model=model))


class TestModelInputModalities:
    def test_returns_model_input_modalities_on_success(self) -> None:
        model = SimpleNamespace(
            capabilities=SimpleNamespace(input_modalities=("text", "image"))
        )
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
