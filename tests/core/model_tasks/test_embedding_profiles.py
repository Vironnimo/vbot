"""Embedding profiles: Model id spellings resolve to one provider-independent family."""

from __future__ import annotations

import pytest

from core.model_tasks.embedding_profiles import UNKNOWN_EMBEDDING_PROFILE, embedding_profile


@pytest.mark.parametrize(
    ("model_id", "family"),
    [
        # Provider prefixes, tags and local catalog spellings name the same family.
        pytest.param("qwen/qwen3-embedding-8b", "qwen3-embedding", id="openrouter-id"),
        pytest.param("qwen3-embedding:0.6b", "qwen3-embedding", id="ollama-tag"),
        pytest.param("text-embedding-qwen3-embedding-0.6b", "qwen3-embedding", id="lmstudio-key"),
        pytest.param("Mistral-Embed-2312", "mistral-embed", id="case-and-version"),
        pytest.param("granite-embedding-r2", "granite-embedding-multilingual-r2", id="local-id"),
        # Version suffixes keep the closest family apart.
        pytest.param("nomic-embed-text-v2-moe", "nomic-embed-text-v2", id="newer-generation"),
        pytest.param("nomic-embed-text:latest", "nomic-embed-text", id="older-generation"),
        pytest.param("intfloat/multilingual-e5-large", "multilingual-e5", id="multilingual"),
        pytest.param("intfloat/e5-large-v2", "e5", id="english"),
        # Instruction-tuned variants use different conventions and stay unknown.
        pytest.param("intfloat/multilingual-e5-large-instruct", "", id="instruct-variant"),
        pytest.param("openai/text-embedding-ada-002", "", id="unknown-model"),
    ],
)
def test_model_id_spellings_resolve_to_their_family(model_id: str, family: str) -> None:
    profile = embedding_profile(model_id)

    assert profile.family == family
    if not family:
        assert profile == UNKNOWN_EMBEDDING_PROFILE
        assert (profile.handling, profile.recommended_rank) == ("symmetric", None)
