"""Tests for GitHubCopilotAdapter."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from core.models.models import Model
from core.providers.github_copilot import (
    GitHubCopilotAdapter,
)
from core.providers.github_copilot_policy import CHAT_COMPLETIONS_ENDPOINT
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

FIXTURE_PATH = Path("tests/core/models/fixtures/github_copilot_models_raw.json")
API_KEY = "test-api-key-12345"
COPILOT_CONFIG = ProviderConfig(
    id="github-copilot",
    name="GitHub Copilot",
    adapter="github_copilot",
    base_url="https://api.githubcopilot.com",
    connections=[
        ConnectionConfig(
            id="oauth",
            type="oauth",
            label="Sign in with GitHub",
            auth=AuthConfig(
                header="Authorization",
                prefix="Bearer ",
                credential_key="",
            ),
        )
    ],
    defaults={"max_tokens": 4096},
)
COPILOT_URL = "https://api.githubcopilot.com/chat/completions"
RESPONSES_URL = "https://api.githubcopilot.com/responses"
MESSAGES_URL = "https://api.githubcopilot.com/v1/messages"
SUCCESS_RESPONSE = {
    "id": "chatcmpl-abc123",
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello!"},
            "finish_reason": "stop",
        }
    ],
}
SAMPLE_MESSAGES = [{"role": "user", "content": "Hello"}]
SYNTHETIC_COPILOT_METADATA_BY_MODEL_ID = {
    "claude-haiku-4.5": {
        "github_copilot": {
            "vendor": "Anthropic",
            "family": "claude-haiku-4.5",
            "version": "claude-haiku-4.5",
            "supported_endpoints": [CHAT_COMPLETIONS_ENDPOINT, "/v1/messages"],
            "adaptive_thinking": True,
            "parallel_tool_calls": True,
            "streaming": True,
            "structured_outputs": True,
            "tool_calls": True,
        }
    },
    "gemini-3.1-pro-preview": {
        "github_copilot": {
            "vendor": "Google",
            "family": "gemini-3.1-pro-preview",
            "supported_endpoints": [CHAT_COMPLETIONS_ENDPOINT],
            "tool_calls": True,
            "streaming": True,
        }
    },
    "gpt-5.4": {
        "github_copilot": {
            "vendor": "OpenAI",
            "family": "gpt-5.4",
            "version": "gpt-5.4",
            "supported_endpoints": [CHAT_COMPLETIONS_ENDPOINT, "/responses", "ws:/responses"],
            "reasoning_efforts": ["low", "medium", "high"],
            "parallel_tool_calls": True,
            "streaming": True,
            "structured_outputs": True,
            "tool_calls": True,
        }
    },
    "gpt-5.4-mini": {
        "github_copilot": {
            "vendor": "OpenAI",
            "family": "gpt-5.4-mini",
            "version": "gpt-5.4-mini",
            "supported_endpoints": [CHAT_COMPLETIONS_ENDPOINT, "/responses", "ws:/responses"],
            "reasoning_efforts": ["low", "medium", "high"],
            "parallel_tool_calls": True,
            "streaming": True,
            "structured_outputs": True,
            "tool_calls": True,
        }
    },
    "gpt-5.4-partial": {
        "github_copilot": {
            "vendor": "OpenAI",
            "family": "gpt-5.4",
            "version": "gpt-5.4",
            "supported_endpoints": ["/responses"],
            "streaming": True,
            "tool_calls": True,
        }
    },
}


def _raw_copilot_models() -> dict[str, dict]:
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["data"]
    return {entry["id"]: entry for entry in data}


def _copilot_model(model_id: str) -> Model:
    raw_models = _raw_copilot_models()
    return GitHubCopilotAdapter.normalize_catalog_entry(raw_models[model_id], {})


def _copilot_model_with_metadata(model_id: str, metadata: dict) -> Model:
    base_model = GitHubCopilotAdapter.normalize_catalog_entry(
        {
            "id": model_id,
            "name": model_id,
            "capabilities": {"supports": {}},
        },
        {},
    )
    return replace(base_model, metadata=metadata)


def _copilot_metadata_lookup(model_id: str) -> Model | None:
    synthetic_metadata = SYNTHETIC_COPILOT_METADATA_BY_MODEL_ID.get(model_id)
    if synthetic_metadata is not None:
        return _copilot_model_with_metadata(model_id, synthetic_metadata)
    raw_models = _raw_copilot_models()
    if model_id not in raw_models:
        return None
    return _copilot_model(model_id)


class _BrokenLineIterator:
    def __init__(self, first_line: str, error: Exception) -> None:
        self._first_line = first_line
        self._error = error
        self._emitted_first_line = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if not self._emitted_first_line:
            self._emitted_first_line = True
            return self._first_line
        raise self._error


class _BrokenStreamResponse:
    status_code = 200

    def __init__(self, first_line: str, error: Exception) -> None:
        self._iterator = _BrokenLineIterator(first_line, error)
        self.closed = False

    def aiter_lines(self):
        return self._iterator

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture()
def copilot_adapter() -> GitHubCopilotAdapter:
    return GitHubCopilotAdapter(COPILOT_CONFIG, API_KEY)


@pytest.fixture()
def metadata_copilot_adapter() -> GitHubCopilotAdapter:
    return GitHubCopilotAdapter(
        COPILOT_CONFIG,
        API_KEY,
        model_lookup=_copilot_metadata_lookup,
    )


class _RotatingTokenGetter:
    """Async token getter that yields a fresh token on each call."""

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self.calls = 0

    async def __call__(self) -> str:
        token = self._tokens[min(self.calls, len(self._tokens) - 1)]
        self.calls += 1
        return token
