"""Openrouter policy."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from core.providers._openrouter_constants import (
    _REASONING_TRAILING_NEWLINES_STATE_KEY,
    MAX_REASONING_PARAGRAPH_NEWLINES,
    OPENROUTER_CACHE_BREAKPOINT_LIMIT,
    OPENROUTER_CACHE_CONTROL_EPHEMERAL,
    OPENROUTER_MAX_HISTORY_CACHE_BREAKPOINTS,
    OPENROUTER_NONE_EFFORT,
    OPENROUTER_REASONING_OFF,
    OPENROUTER_RESPONSES_REQUEST_PARAMETERS,
    REASONING_NEWLINE_RUN_PATTERN,
)
from core.providers.reasoning import (
    REASONING_INTENT_BUDGET,
    REASONING_INTENT_EFFORT,
    REASONING_INTENT_OFF,
    REASONING_INTENT_ON,
    ReasoningIntent,
    closest_supported_effort,
    normalize_thinking_effort,
)


@dataclass(frozen=True)
class OpenRouterResponsesPolicy:
    """Request-shaping facts for an exact OpenRouter Responses-routed Model."""

    allowed_reasoning_efforts: frozenset[str]
    supports_tools: bool
    supports_parallel_tool_calls: bool
    supports_structured_outputs: bool

    @property
    def allows_any_reasoning_controls(self) -> bool:
        return bool(self.allowed_reasoning_efforts)

    @property
    def supports_explicit_none_effort(self) -> bool:
        # No Responses-routed OpenRouter Model has been live-proven to need an
        # explicit off rung yet; preserve omission until its wire is audited.
        return False

    def filter_request_kwargs(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        filtered = {key: value for key, value in kwargs.items() if value is not None}
        if not self.supports_tools:
            for name in ("tools", "tool_choice", "parallel_tool_calls"):
                filtered.pop(name, None)
        elif not self.supports_parallel_tool_calls:
            filtered.pop("parallel_tool_calls", None)

        if not self.supports_structured_outputs:
            for name in ("response_format", "structured_outputs", "json_mode", "text"):
                filtered.pop(name, None)

        if not self.allows_any_reasoning_controls:
            for name in ("thinking_effort", "reasoning_effort", "reasoning", "include_reasoning"):
                filtered.pop(name, None)
        else:
            self._normalize_effort(filtered, "thinking_effort")
            self._normalize_effort(filtered, "reasoning_effort")

        for name in ("max_tokens", "max_output_tokens", "temperature", "top_p", "top_k"):
            if name in filtered and name not in OPENROUTER_RESPONSES_REQUEST_PARAMETERS:
                filtered.pop(name, None)
        return filtered

    def closest_reasoning_effort(self, effort: Any) -> str | None:
        normalized = normalize_thinking_effort(effort)
        if not normalized:
            return None
        if normalized == OPENROUTER_NONE_EFFORT:
            return (
                OPENROUTER_NONE_EFFORT
                if OPENROUTER_NONE_EFFORT in self.allowed_reasoning_efforts
                else None
            )
        return closest_supported_effort(normalized, self.allowed_reasoning_efforts)

    def supports_request_parameter(self, parameter_name: str) -> bool:
        return parameter_name in OPENROUTER_RESPONSES_REQUEST_PARAMETERS

    def _normalize_effort(
        self,
        filtered: dict[str, Any],
        parameter_name: str,
    ) -> None:
        if parameter_name not in filtered:
            return
        safe_effort = self.closest_reasoning_effort(filtered[parameter_name])
        if safe_effort is None:
            filtered.pop(parameter_name, None)
        else:
            filtered[parameter_name] = safe_effort


def _openrouter_provider_preferences(
    routing: Mapping[str, Any],
    model_id: str,
) -> dict[str, Any]:
    """Render vBot's routing policy to OpenRouter's request ``provider`` object."""

    default_policy = routing["default"]
    model_policy = routing["models"].get(model_id)
    policy = model_policy or default_policy

    blocked: list[str] = list(default_policy["blocked"])
    if model_policy is not None:
        blocked.extend(slug for slug in model_policy["blocked"] if slug not in blocked)

    preferences: dict[str, Any] = {}
    mode = policy["mode"]
    if mode == "allowed":
        preferences["only"] = list(policy["providers"])
    elif mode == "ordered":
        preferences["order"] = list(policy["providers"])
    if blocked:
        preferences["ignore"] = blocked
    if policy["allow_fallbacks"] is False:
        preferences["allow_fallbacks"] = False
    return preferences


def _openrouter_http_error_detail(
    response: httpx.Response,
    body: str | None = None,
) -> str:
    reason = response.text if body is None else body
    return f"{response.status_code} {reason}".strip() if reason else str(response.status_code)


def _openrouter_status_error_payload(detail: str) -> dict[str, Any] | None:
    """Extract the structured ``error`` object from an HTTP error detail.

    The compatible base renders establishment failures as ``"<status> <body>"``
    and OpenRouter bodies are JSON with a documented ``error`` object. Returns
    ``None`` when the detail carries no parseable OpenRouter-shaped error, so
    the shared status policy applies unchanged.
    """

    start = detail.find("{")
    if start < 0:
        return None
    try:
        parsed = json.loads(detail[start:])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    error = parsed.get("error")
    return error if isinstance(error, dict) else None


def _openrouter_routing_options(
    payload: Mapping[str, Any],
    *,
    model_specific: bool,
) -> list[dict[str, str]]:
    """Normalize OpenRouter provider and endpoint catalogs for the Settings UI."""

    raw_data = payload.get("data")
    if model_specific:
        entries = raw_data.get("endpoints") if isinstance(raw_data, Mapping) else None
    else:
        entries = raw_data
    if not isinstance(entries, list):
        return []

    options: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        raw_slug = entry.get("tag") if model_specific else entry.get("slug")
        raw_name = entry.get("provider_name") if model_specific else entry.get("name")
        if not isinstance(raw_slug, str) or not raw_slug.strip():
            continue
        slug = raw_slug.strip().lower()
        name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else slug
        options.setdefault(slug, name)
    return [
        {"slug": slug, "name": name}
        for slug, name in sorted(
            options.items(),
            key=lambda item: (item[1].casefold(), item[0]),
        )
    ]


def _collapse_reasoning_newline_runs(text: str, state: dict[str, Any] | None) -> str:
    """Collapse newline-run noise in reasoning text, across delta boundaries.

    Interior runs of three or more newlines collapse to one paragraph break.
    A run split across consecutive deltas is bounded through the per-stream
    ``state`` mapping: it tracks how many newlines the emitted stream ends
    with so the next fragment's leading run cannot push the joined total past
    a paragraph break. Without ``state`` each fragment collapses in isolation.
    An empty result means this fragment was pure separator noise; the caller
    drops it and the trailing-run tracking keeps its previous value.
    """

    trailing = 0
    if state is not None:
        stored = state.get(_REASONING_TRAILING_NEWLINES_STATE_KEY, 0)
        trailing = stored if isinstance(stored, int) else 0
    leading = len(text) - len(text.lstrip("\n"))
    if trailing + leading > MAX_REASONING_PARAGRAPH_NEWLINES:
        excess = trailing + leading - MAX_REASONING_PARAGRAPH_NEWLINES
        text = text[excess:]
    collapsed = REASONING_NEWLINE_RUN_PATTERN.sub(
        "\n" * MAX_REASONING_PARAGRAPH_NEWLINES,
        text,
    )
    if not collapsed:
        return ""
    if state is not None:
        state[_REASONING_TRAILING_NEWLINES_STATE_KEY] = min(
            MAX_REASONING_PARAGRAPH_NEWLINES,
            len(collapsed) - len(collapsed.rstrip("\n")),
        )
    return collapsed


def _collapse_reasoning_delta_texts(
    deltas: Iterable[dict[str, Any]],
    state: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Rewrite ``reasoning_delta`` texts with newline runs collapsed.

    Fragments that collapse to nothing are dropped entirely — Chat ignores
    empty reasoning text anyway, and dropping keeps the visible delta stream
    free of no-op events. Non-reasoning deltas pass through untouched.
    """

    result: list[dict[str, Any]] = []
    for delta in deltas:
        if delta.get("type") != "reasoning_delta":
            result.append(delta)
            continue
        collapsed = _collapse_reasoning_newline_runs(str(delta.get("text", "")), state)
        if collapsed:
            result.append({"type": "reasoning_delta", "text": collapsed})
    return result


def _render_openrouter_reasoning(payload: dict[str, Any], intent: ReasoningIntent) -> None:
    """Render a reasoning intent onto an OpenRouter payload.

    OpenRouter speaks ``reasoning: {effort}`` / ``{enabled}``. An ``effort``
    intent maps straight through; ``budget`` also renders as an effort (OpenRouter
    maps effort→budget internally, so no token budget is sent); ``on`` toggles
    ``enabled: true``. ``off`` keeps the byte-identical ``{"effort": "none"}`` for
    an effort-spelled-off wire (``effort_level == "none"``) and falls back to the
    documented ``{"enabled": false}`` toggle otherwise; ``default`` omits the
    field entirely.
    """

    if intent.kind == REASONING_INTENT_ON:
        payload["reasoning"] = {"enabled": True}
        payload["include_reasoning"] = True
    elif intent.kind in (REASONING_INTENT_EFFORT, REASONING_INTENT_BUDGET):
        if intent.effort_level is not None:
            payload["reasoning"] = {"effort": intent.effort_level}
            payload["include_reasoning"] = True
    elif intent.kind == REASONING_INTENT_OFF:
        if intent.effort_level == OPENROUTER_NONE_EFFORT:
            payload["reasoning"] = {"effort": OPENROUTER_NONE_EFFORT}
        else:
            payload["reasoning"] = dict(OPENROUTER_REASONING_OFF)
        # Some upstreams honor the output toggle even when they ignore the
        # requested effort. Never ask one to return reasoning for an off intent.
        payload.pop("include_reasoning", None)


def _describe_openrouter_intent(intent: ReasoningIntent) -> ReasoningIntent:
    """Map a resolved intent onto OpenRouter's render (``/status`` description).

    Mirrors :func:`_render_openrouter_reasoning`: an ``on`` intent toggles
    ``enabled`` rather than sending an effort, and a ``budget`` intent renders
    as the effort OpenRouter maps internally.
    """

    if intent.kind == REASONING_INTENT_BUDGET and intent.effort_level is not None:
        return ReasoningIntent(REASONING_INTENT_EFFORT, effort_level=intent.effort_level)
    return intent


def _is_claude_family(model_id: str) -> bool:
    """True for Anthropic Claude models on OpenRouter (``anthropic/claude-*``).

    Matching on the ``claude`` substring covers the vendor-prefixed slug, the
    tilde auto-router form (``~anthropic/claude-haiku-latest``), and any dated
    variant, while never matching a non-Claude model. Only these need explicit
    ``cache_control`` — every other family caches implicitly upstream.
    """

    return "claude" in model_id.lower()


def _apply_openrouter_prompt_caching(payload: dict[str, Any]) -> None:
    """Place ``cache_control`` breakpoints on the OpenAI-wire message array.

    Envelope layout (marker inside a content part): one marker on the last
    system message (caches tools + system) and up to
    :data:`OPENROUTER_MAX_HISTORY_CACHE_BREAKPOINTS` rolling markers on the most
    recent non-system messages, capped at :data:`OPENROUTER_CACHE_BREAKPOINT_LIMIT`.
    A message whose content cannot carry a marker (empty string, or a pure
    tool-call assistant turn with ``None`` content) is skipped so a breakpoint is
    never wasted on a part OpenRouter would ignore.
    """

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return

    remaining = OPENROUTER_CACHE_BREAKPOINT_LIMIT
    last_system = _last_index(messages, role="system")
    if last_system is not None and _mark_openrouter_message(messages[last_system]):
        remaining -= 1

    history_budget = min(remaining, OPENROUTER_MAX_HISTORY_CACHE_BREAKPOINTS)
    marked = 0
    for index in range(len(messages) - 1, -1, -1):
        if marked >= history_budget:
            break
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") == "system":
            continue
        if _mark_openrouter_message(message):
            marked += 1


def _last_index(messages: list[Any], *, role: str) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and message.get("role") == role:
            return index
    return None


def _mark_openrouter_message(message: dict[str, Any]) -> bool:
    """Add ``cache_control`` to a message's last content part; return whether it did.

    A string content is wrapped into a single ``text`` part to carry the marker;
    a list content takes the marker on its last dict part. Empty/`None` content
    carries nothing (``False``), so the caller moves the breakpoint to an older
    message.
    """

    content = message.get("content")
    if isinstance(content, str):
        if not content.strip():
            return False
        message["content"] = [
            {
                "type": "text",
                "text": content,
                "cache_control": dict(OPENROUTER_CACHE_CONTROL_EPHEMERAL),
            }
        ]
        return True
    if isinstance(content, list):
        for index in range(len(content) - 1, -1, -1):
            part = content[index]
            if isinstance(part, dict):
                part["cache_control"] = dict(OPENROUTER_CACHE_CONTROL_EPHEMERAL)
                return True
    return False
