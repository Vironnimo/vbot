"""OpenRouter request and response helpers: routing, prompt caching, errors, newline runs."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

import httpx

from core.providers._openrouter_constants import (
    _REASONING_TRAILING_NEWLINES_STATE_KEY,
    MAX_REASONING_PARAGRAPH_NEWLINES,
    OPENROUTER_CACHE_BREAKPOINT_LIMIT,
    OPENROUTER_CACHE_CONTROL_EPHEMERAL,
    OPENROUTER_MAX_HISTORY_CACHE_BREAKPOINTS,
    REASONING_NEWLINE_RUN_PATTERN,
)


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


def _openrouter_http_error_detail(response: httpx.Response) -> str:
    reason = response.text
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
    a list content takes the marker on a copy of its last dict part, because the
    wire message may still share its content list with the caller's history.
    Empty/`None` content carries nothing (``False``), so the caller moves the
    breakpoint to an older message.
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
                marked = list(content)
                marked[index] = {**part, "cache_control": dict(OPENROUTER_CACHE_CONTROL_EPHEMERAL)}
                message["content"] = marked
                return True
    return False
