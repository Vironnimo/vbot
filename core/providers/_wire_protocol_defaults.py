"""Code defaults per wire protocol: resolution layer 1 of every wire profile.

Each table describes what the shared protocol codec does when no Provider data
says otherwise. Provider files only state deviations from these values.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.providers._wire_profile_files import PartialProfile, validate_partial_profile
from core.providers.wire_profile import PROTOCOLS

_IMAGE_TYPES = ["image/jpeg", "image/png", "image/gif", "image/webp"]

_RAW_DEFAULTS: dict[str, dict[str, Any]] = {
    "chat_completions": {
        "request": {
            "output_limit_field": "max_tokens",
            "tool_schema": "omit_strict",
            "tool_call_ids": "none",
        },
        "reasoning": {"dialect": "reasoning_effort", "floor": ["low", "medium", "high"]},
        "response": {
            "reasoning_fields": ["reasoning", "reasoning_content", "reasoning_text", "thinking"]
        },
        "replay": {"fidelity": "meta_preferred", "history_field": "reasoning_content"},
        "media": {"types": [*_IMAGE_TYPES, "audio/wav", "audio/mpeg"]},
    },
    "messages": {
        "request": {
            "output_limit_field": "max_tokens",
            "tool_schema": "omit_strict",
            "tool_call_ids": "anthropic",
            "parameters": {
                "temperature": {"mode": "drop_while_thinking"},
                "top_p": {"mode": "drop_while_thinking"},
                "top_k": {"mode": "drop_while_thinking"},
            },
        },
        "reasoning": {
            "dialect": "anthropic_thinking",
            "floor": ["minimal", "low", "medium", "high", "xhigh", "max"],
        },
        "replay": {"fidelity": "meta_only", "strip_when_off": True},
        "media": {"types": _IMAGE_TYPES},
    },
    "responses": {
        "request": {
            "output_limit_field": "max_output_tokens",
            "tool_schema": "explicit_non_strict",
            "tool_call_ids": "responses",
        },
        "reasoning": {"dialect": "responses_reasoning"},
        "replay": {"fidelity": "meta_only"},
        "media": {"types": _IMAGE_TYPES},
    },
    "gemini": {
        "request": {"output_limit_field": None, "tool_schema": "omit_strict"},
        "reasoning": {"dialect": "gemini_thinking"},
        "replay": {"fidelity": "meta_only"},
        "media": {"types": _IMAGE_TYPES},
    },
    "ollama_chat": {
        "request": {"output_limit_field": None, "tool_schema": "omit_strict"},
        "reasoning": {"dialect": "ollama_think", "floor": ["low", "medium", "high"]},
        "response": {"reasoning_fields": ["thinking"]},
        "replay": {"fidelity": "readable_only", "history_field": "thinking"},
        "media": {"types": _IMAGE_TYPES},
    },
}


def _strict_report(message: str) -> None:
    raise ValueError(f"invalid protocol default: {message}")


PROTOCOL_DEFAULTS: Mapping[str, PartialProfile] = {
    protocol: validate_partial_profile(
        _RAW_DEFAULTS[protocol],
        where=f"protocol_defaults.{protocol}",
        report=_strict_report,
        allow_protocol=False,
    )
    for protocol in PROTOCOLS
}
