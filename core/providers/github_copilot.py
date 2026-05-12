"""GitHub Copilot provider adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.openai_compatible import (
    OpenAICompatibleAdapter,
    _read_int,
    _read_mapping,
    _read_string,
)


class GitHubCopilotAdapter(OpenAICompatibleAdapter):
    """OpenAI-compatible adapter for GitHub Copilot."""

    @classmethod
    def normalize_catalog_entry(
        cls,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> Model:
        """Normalize one captured GitHub Copilot ``/models`` entry."""

        del defaults
        capabilities = _read_mapping(raw, "capabilities")
        limits = _read_mapping(capabilities, "limits")
        supports = _read_mapping(capabilities, "supports")

        return Model(
            model_id=_read_string(raw, "id"),
            name=_read_string(raw, "name"),
            capabilities=Capabilities(
                vision=supports.get("vision") is True,
                tools=supports.get("tool_calls") is True,
                json_mode=supports.get("structured_outputs") is True,
                reasoning=ReasoningCapabilities(supported=_copilot_supports_reasoning(supports)),
            ),
            context_window=_read_int(limits, "max_context_window_tokens"),
            max_output_tokens=_read_int(limits, "max_output_tokens"),
        )


def _copilot_supports_reasoning(supports: Mapping[str, Any]) -> bool:
    reasoning_effort = supports.get("reasoning_effort")
    if isinstance(reasoning_effort, list) and reasoning_effort:
        return True
    return "min_thinking_budget" in supports or "max_thinking_budget" in supports
