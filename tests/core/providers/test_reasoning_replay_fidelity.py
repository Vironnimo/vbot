"""Fidelity tests for reasoning replay serialization.

The replay *scope* (how far back reasoning may go) is chat-layer policy; these
tests pin the *fidelity* dimension: each wire declares which class of persisted
reasoning state it carries back, and the base OpenAI-compatible serializer emits
exactly one class — opaque meta when present and allowed, otherwise readable
text, never both.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.providers.adapter import ProviderAdapter
from core.providers.kimi import KimiAdapter
from core.providers.minimax import MiniMaxAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.openrouter import OpenRouterAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.reasoning import (
    DEFAULT_REASONING_REPLAY_FIDELITY,
    REASONING_REPLAY_FIDELITY_META_ONLY,
    REASONING_REPLAY_FIDELITY_READABLE_ONLY,
)

API_KEY = "test-api-key-12345"

BOTH_CLASSES_MESSAGE: dict[str, Any] = {
    "role": "assistant",
    "content": "Answer",
    "reasoning": "I think...",
    "reasoning_meta": {"reasoning_details": [{"trace": "opaque"}]},
    "tool_calls": None,
}
READABLE_ONLY_MESSAGE: dict[str, Any] = {
    "role": "assistant",
    "content": "Answer",
    "reasoning": "I think...",
    "tool_calls": None,
}


def _config(provider_id: str) -> ProviderConfig:
    return ProviderConfig(
        id=provider_id,
        name=provider_id,
        adapter="openai_compatible",
        base_url=f"https://{provider_id}.example/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key=f"{provider_id.upper()}_API_KEY",
                ),
            )
        ],
    )


class TestAbcDefault:
    def test_default_fidelity_is_meta_preferred(self) -> None:
        class _MinimalAdapter(ProviderAdapter):
            async def send(self, messages, *, model_id, **kwargs):  # pragma: no cover
                raise NotImplementedError

            async def stream(self, messages, *, model_id, **kwargs):  # pragma: no cover
                raise NotImplementedError
                yield  # pragma: no cover

            async def aclose(self) -> None:  # pragma: no cover
                return None

        adapter = _MinimalAdapter()

        assert adapter.reasoning_replay_fidelity("any-model") == DEFAULT_REASONING_REPLAY_FIDELITY

    def test_openrouter_inherits_meta_preferred(self) -> None:
        adapter = OpenRouterAdapter(_config("openrouter"), API_KEY)

        assert adapter.reasoning_replay_fidelity("anthropic/claude") == "meta_preferred"


class TestMetaPreferredBaseRule:
    @pytest.fixture()
    def adapter(self) -> OpenAICompatibleAdapter:
        return OpenAICompatibleAdapter(_config("minimal"), API_KEY)

    def test_meta_supersedes_readable_when_both_captured(
        self, adapter: OpenAICompatibleAdapter
    ) -> None:
        wire = adapter._format_assistant_message(dict(BOTH_CLASSES_MESSAGE), model_id="m")

        assert wire["reasoning_details"] == [{"trace": "opaque"}]
        assert "reasoning_content" not in wire

    def test_encrypted_content_alone_counts_as_meta(self, adapter: OpenAICompatibleAdapter) -> None:
        message = {
            **READABLE_ONLY_MESSAGE,
            "reasoning_meta": {"encrypted_content": "opaque-bytes"},
        }

        wire = adapter._format_assistant_message(message, model_id="m")

        assert wire["encrypted_content"] == "opaque-bytes"
        assert "reasoning_content" not in wire

    def test_readable_only_turn_still_replays_readably(
        self, adapter: OpenAICompatibleAdapter
    ) -> None:
        wire = adapter._format_assistant_message(dict(READABLE_ONLY_MESSAGE), model_id="m")

        assert wire["reasoning_content"] == "I think..."
        assert "reasoning_details" not in wire
        assert "encrypted_content" not in wire

    def test_turn_without_reasoning_carries_nothing(self, adapter: OpenAICompatibleAdapter) -> None:
        wire = adapter._format_assistant_message(
            {"role": "assistant", "content": "Answer", "tool_calls": None},
            model_id="m",
        )

        assert "reasoning_content" not in wire
        assert "reasoning_details" not in wire
        assert "encrypted_content" not in wire


class TestReadableOnlyDeclaration:
    @pytest.fixture()
    def adapter(self) -> KimiAdapter:
        return KimiAdapter(_config("kimi"), API_KEY)

    def test_kimi_declares_readable_only(self, adapter: KimiAdapter) -> None:
        assert (
            adapter.reasoning_replay_fidelity("kimi-k3") == REASONING_REPLAY_FIDELITY_READABLE_ONLY
        )

    def test_stray_meta_is_stripped_and_readable_kept(self, adapter: KimiAdapter) -> None:
        wire = adapter._format_assistant_message(dict(BOTH_CLASSES_MESSAGE), model_id="k3")

        assert wire["reasoning_content"] == "I think..."
        assert "reasoning_details" not in wire
        assert "encrypted_content" not in wire

    def test_minimax_key_wire_declares_nothing_narrower(self) -> None:
        adapter = MiniMaxAdapter(_config("minimax"), API_KEY)

        # The M2.x key-wire captures reasoning_details; meta must keep winning.
        assert adapter.reasoning_replay_fidelity("minimax-m2.5") == "meta_preferred"

    def test_minimax_payload_replays_details_not_duplicated_text(
        self,
    ) -> None:
        adapter = MiniMaxAdapter(_config("minimax"), API_KEY)
        payload = adapter._build_payload([dict(BOTH_CLASSES_MESSAGE)], "minimax-m2.5")

        assistant = payload["messages"][0]
        assert assistant["reasoning_details"] == [{"trace": "opaque"}]
        assert "reasoning_content" not in assistant


class TestOpenRouterRegression:
    def test_payload_sends_details_without_duplicated_readable_text(self) -> None:
        adapter = OpenRouterAdapter(_config("openrouter"), API_KEY)

        formatted_both = adapter._format_assistant_message(
            dict(BOTH_CLASSES_MESSAGE), model_id="anthropic/claude"
        )
        formatted_readable = adapter._format_assistant_message(
            dict(READABLE_ONLY_MESSAGE), model_id="anthropic/claude"
        )

        # Both classes captured: details go back, plaintext is not duplicated.
        assert formatted_both["reasoning_details"] == [{"trace": "opaque"}]
        assert "reasoning_content" not in formatted_both
        # Raw-string turns keep their readable continuity.
        assert formatted_readable["reasoning_content"] == "I think..."


class TestMetaOnlyDeclaration:
    def test_mistral_declares_meta_only(self) -> None:
        from core.providers.mistral import MistralAdapter

        adapter = MistralAdapter(_config("mistral"), API_KEY)

        assert (
            adapter.reasoning_replay_fidelity("mistral-large")
            == REASONING_REPLAY_FIDELITY_META_ONLY
        )

    def test_meta_only_never_emits_readable_field(self) -> None:
        from core.providers.mistral import MistralAdapter

        adapter = MistralAdapter(_config("mistral"), API_KEY)

        wire = adapter._format_assistant_message(dict(READABLE_ONLY_MESSAGE), model_id="m")

        assert "reasoning_content" not in wire
