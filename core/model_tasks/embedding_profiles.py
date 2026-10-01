"""Curated per-family facts for text embedding Models.

Embedding Models differ in how they distinguish search queries from stored
documents: some expect a Provider ``input_type`` field, some expect a text
prefix in front of each input, and the rest embed both sides the same way
(symmetric). An :class:`EmbeddingProfile` records that handling together with
a few selection facts (languages, recommendation rank, input limit, short
note) for one Model family. Profiles match the Model id, independent of the
Provider that serves the Model; an unknown Model is symmetric with no
recommendation.

Purpose handling is applied at request time only. Prefixes are never stored:
callers keep their original text, and the profile's request-shaping facts
plus :data:`EMBEDDING_PROFILE_CONTRACT_VERSION` enter the embedding space
fingerprint, so changing the handling invalidates vectors instead of mixing
incompatible spaces.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

# Bump when a change to the table alters what an existing family sends.
EMBEDDING_PROFILE_CONTRACT_VERSION = 1

EmbeddingPurpose = Literal["query", "document"]
EmbeddingHandling = Literal["symmetric", "input_type", "prefix"]
EMBEDDING_PURPOSES: frozenset[str] = frozenset({"query", "document"})

# Values OpenRouter accepts in its ``input_type`` request field.
_SEARCH_QUERY = "search_query"
_SEARCH_DOCUMENT = "search_document"
# The web-search retrieval instruction both instruction-tuned families publish.
_WEB_SEARCH_TASK = "Given a web search query, retrieve relevant passages that answer the query"


@dataclass(frozen=True)
class EmbeddingRequest:
    """Inputs and optional ``input_type`` for one embedding request."""

    inputs: list[str]
    input_type: str | None = None


@dataclass(frozen=True)
class EmbeddingProfile:
    """Purpose handling and selection facts for one embedding Model family.

    ``query``/``document`` carry the ``input_type`` values for ``input_type``
    handling and the text prefixes for ``prefix`` handling; an empty value
    leaves that side unchanged. ``multilingual`` is ``None`` when unknown,
    ``recommended_rank`` orders recommended families (1 first; ``None`` is not
    recommended), and ``max_input_tokens`` is the Model's own input limit per
    text when known.
    """

    family: str
    handling: EmbeddingHandling = "symmetric"
    query: str = ""
    document: str = ""
    multilingual: bool | None = None
    recommended_rank: int | None = None
    max_input_tokens: int | None = None
    note: str = ""

    def request(self, texts: Sequence[str], purpose: EmbeddingPurpose | None) -> EmbeddingRequest:
        """Shape *texts* for *purpose*; ``None`` sends the texts unchanged."""

        if purpose is None or self.handling == "symmetric":
            return EmbeddingRequest(list(texts))
        value = self.query if purpose == "query" else self.document
        if self.handling == "input_type":
            return EmbeddingRequest(list(texts), value or None)
        return EmbeddingRequest([f"{value}{text}" for text in texts])

    def space_identity(self) -> dict[str, Any]:
        """Return the request-shaping facts that define an embedding space."""

        return {
            "contract_version": EMBEDDING_PROFILE_CONTRACT_VERSION,
            "family": self.family,
            "handling": self.handling,
            "query": self.query,
            "document": self.document,
        }


UNKNOWN_EMBEDDING_PROFILE = EmbeddingProfile(family="")


def embedding_profile(model_id: str) -> EmbeddingProfile:
    """Return the profile for a Model id, or the symmetric unknown profile.

    Matching is case-insensitive on Model family names anywhere in the id, so
    Provider prefixes (``qwen/``), tags (``:8b``) and local catalog spellings
    (``text-embedding-qwen3-embedding-0.6b``) resolve to the same family.
    """

    normalized = model_id.strip().lower()
    for pattern, profile in _PROFILES:
        if pattern.search(normalized):
            return profile
    return UNKNOWN_EMBEDDING_PROFILE


def _family(*names: str) -> re.Pattern[str]:
    """Match any of *names* as a whole token sequence inside a Model id."""

    alternatives = "|".join(names)
    return re.compile(rf"(?<![a-z0-9])(?:{alternatives})(?![a-z0-9])")


_PROFILES: tuple[tuple[re.Pattern[str], EmbeddingProfile], ...] = (
    (
        # IBM model card: no query/document prompts, 32,768 tokens, 200+ languages.
        _family(r"granite-embedding-311m-multilingual-r2", r"granite-embedding-r2"),
        EmbeddingProfile(
            family="granite-embedding-multilingual-r2",
            multilingual=True,
            recommended_rank=1,
            max_input_tokens=32768,
            note="Compact multilingual model that runs well on a CPU.",
        ),
    ),
    (
        # Microsoft model card: queries carry the web-search instruction
        # (sentence-transformers prompt ``web_search_query``, trailing space);
        # documents need none.
        _family(r"harrier"),
        EmbeddingProfile(
            family="harrier-oss-v1",
            handling="prefix",
            query=f"Instruct: {_WEB_SEARCH_TASK}\nQuery: ",
            multilingual=True,
            recommended_rank=2,
            max_input_tokens=32768,
            note="High-quality multilingual model; needs more memory than smaller models.",
        ),
    ),
    (
        _family(r"text-embedding-3-small"),
        EmbeddingProfile(
            family="text-embedding-3-small",
            multilingual=True,
            recommended_rank=3,
            max_input_tokens=8192,
            note="Inexpensive general-purpose OpenAI model.",
        ),
    ),
    (
        # Qwen model card: the official query prompt ends in ``Query:`` with no
        # trailing space (``get_detailed_instruct`` and the sentence-transformers
        # ``query`` prompt); documents need none. 32k context, 100+ languages.
        _family(r"qwen3-embedding"),
        EmbeddingProfile(
            family="qwen3-embedding",
            handling="prefix",
            query=f"Instruct: {_WEB_SEARCH_TASK}\nQuery:",
            multilingual=True,
            recommended_rank=4,
            max_input_tokens=32768,
            note="Strong multilingual retrieval model.",
        ),
    ),
    (
        # OpenRouter honors input_type for this family (live probe 2026-10-01).
        _family(r"gemini-embedding-2"),
        EmbeddingProfile(
            family="gemini-embedding-2",
            handling="input_type",
            query=_SEARCH_QUERY,
            document=_SEARCH_DOCUMENT,
            multilingual=True,
            recommended_rank=5,
            max_input_tokens=8192,
            note="Google model with separate query and document modes.",
        ),
    ),
    (
        # Google documents a 2,048-token input limit; OpenRouter honors
        # input_type for this family (live probe 2026-10-01).
        _family(r"gemini-embedding-001"),
        EmbeddingProfile(
            family="gemini-embedding-001",
            handling="input_type",
            query=_SEARCH_QUERY,
            document=_SEARCH_DOCUMENT,
            multilingual=True,
            recommended_rank=6,
            max_input_tokens=2048,
            note="Google model with separate query and document modes; short input limit.",
        ),
    ),
    (
        _family(r"text-embedding-3-large"),
        EmbeddingProfile(
            family="text-embedding-3-large",
            multilingual=True,
            recommended_rank=7,
            max_input_tokens=8192,
            note="Higher-quality OpenAI model at a higher price.",
        ),
    ),
    (
        _family(r"bge-m3"),
        EmbeddingProfile(
            family="bge-m3",
            multilingual=True,
            recommended_rank=8,
            max_input_tokens=8192,
            note="Open multilingual model, also available for local runtimes.",
        ),
    ),
    (
        _family(r"mistral-embed"),
        EmbeddingProfile(
            family="mistral-embed",
            max_input_tokens=8192,
            note="Mistral's general-purpose embedding model.",
        ),
    ),
    (
        _family(r"codestral-embed"),
        EmbeddingProfile(
            family="codestral-embed",
            max_input_tokens=8192,
            note="Specialized for source code rather than conversations.",
        ),
    ),
    (
        # OpenRouter honors input_type for these families (live probe 2026-10-01).
        _family(r"voyage-[a-z0-9.-]+"),
        EmbeddingProfile(
            family="voyage",
            handling="input_type",
            query=_SEARCH_QUERY,
            document=_SEARCH_DOCUMENT,
            max_input_tokens=32000,
            note="Voyage AI retrieval model with separate query and document modes.",
        ),
    ),
    (
        _family(r"nemotron-3-embed"),
        EmbeddingProfile(
            family="nemotron-3-embed",
            handling="input_type",
            query=_SEARCH_QUERY,
            document=_SEARCH_DOCUMENT,
            note="NVIDIA retrieval model with separate query and document modes.",
        ),
    ),
    (
        # intfloat model cards: "query: " / "passage: " prefixes, 512 tokens.
        # The -instruct variants use instructions instead and stay unknown.
        _family(r"multilingual-e5-(?:small|base|large)(?!-instruct)"),
        EmbeddingProfile(
            family="multilingual-e5",
            handling="prefix",
            query="query: ",
            document="passage: ",
            multilingual=True,
            max_input_tokens=512,
            note="Multilingual model with a short input limit.",
        ),
    ),
    (
        _family(r"e5-(?:small|base|large)(?:-v2)?(?!-instruct)"),
        EmbeddingProfile(
            family="e5",
            handling="prefix",
            query="query: ",
            document="passage: ",
            multilingual=False,
            max_input_tokens=512,
            note="English model with a short input limit.",
        ),
    ),
    (
        # Nomic model card: v2 (MoE) is multilingual with a 512-token limit.
        _family(r"nomic-embed-text-v2(?:-moe)?"),
        EmbeddingProfile(
            family="nomic-embed-text-v2",
            handling="prefix",
            query="search_query: ",
            document="search_document: ",
            multilingual=True,
            max_input_tokens=512,
            note="Multilingual model with a short input limit.",
        ),
    ),
    (
        # Nomic model card: v1/v1.5 are English with an 8,192-token limit.
        _family(r"nomic-embed-text(?:-v1(?:\.5)?)?"),
        EmbeddingProfile(
            family="nomic-embed-text",
            handling="prefix",
            query="search_query: ",
            document="search_document: ",
            multilingual=False,
            max_input_tokens=8192,
            note="English model, popular for local runtimes.",
        ),
    ),
)
