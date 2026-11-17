"""NVIDIA API Catalog (build.nvidia.com) on the shared Chat Completions wire.

Per-Model reasoning controls and media live in ``resources/wire/nvidia.json``;
Model facts come from NVIDIA's public ``/models`` listing and its models.dev
section. This Adapter owns which listed entries are chat Models."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.providers import ConnectionConfig

# The listing mixes chat Models with endpoints that cannot hold a conversation:
# embedding and reranking retrievers (``nv-embedqa-*``, ``*-embed-*``,
# ``nvclip``), guardrail and safety classifiers (``nemoguard``,
# ``*-safety-guard-*``, ``*-content-safety``) and detectors. It carries no task
# field, so the id families decide; every other entry is a chat Model.
_NON_CHAT_ID_PART = re.compile(
    r"(?:^|[-_./])(?:embed|embedqa|rerank|reranker|nvclip|clip|guard|nemoguard|safety|"
    r"detector|detect|pii)(?:$|[-_./])"
)


class NvidiaAdapter(OpenAICompatibleAdapter):
    """NVIDIA's chat-Model catalog filter."""

    @classmethod
    def accepts_discovered_model(
        cls,
        raw: Mapping[str, Any],
        connection: ConnectionConfig | None,
    ) -> bool:
        """Keep the listing's chat Models; skip retrievers, classifiers and detectors."""

        del cls, connection
        model_id = raw.get("id")
        if not isinstance(model_id, str):
            return True
        model_name = model_id.rsplit("/", 1)[-1].lower()
        return _NON_CHAT_ID_PART.search(model_name) is None
