"""Canonical history and completed responses for streaming native Agent providers."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, override

from core.providers.adapter import (
    TOOL_CALL_ARGUMENT_SEQUENCE_INDEX_FIELD,
    TOOL_CALL_ARGUMENT_SEQUENCE_LENGTH_FIELD,
    TOOL_CALL_REJECTION_FIELD,
    ProviderAdapter,
    canonical_tool_result_is_error,
    normalize_tool_call_candidates,
    tool_result_content_blocks,
    tool_result_text,
)

JsonObject = dict[str, Any]


def native_tool_names(tools: Sequence[JsonObject], wire_limit: int) -> dict[str, str]:
    """Keep transport-compatible Tool names and shorten other names without collisions."""
    reserved = {
        tool["name"]
        for tool in tools
        if re.fullmatch(rf"[A-Za-z0-9_-]{{1,{wire_limit}}}", tool["name"])
    }
    used: set[str] = set()
    names: dict[str, str] = {}
    for tool in tools:
        name = tool["name"]
        short = name
        if name not in reserved:
            stem = re.sub(r"[^A-Za-z0-9_-]", "_", name)
            digest = hashlib.sha256(name.encode()).hexdigest()[:12]
            collision = 0
            while True:
                suffix = f"_{digest}" + (f"_{collision}" if collision else "")
                short = stem[: wire_limit - len(suffix)] + suffix
                if short not in reserved and short not in used:
                    break
                collision += 1
        used.add(short)
        names[name] = short
    return names


def calls(message: Mapping[str, Any]) -> list[JsonObject]:
    result = []
    for call in message.get("tool_calls") or []:
        function = call.get("function", call)
        arguments = function.get("arguments", call.get("arguments", {}))
        if isinstance(arguments, str):
            with contextlib.suppress(ValueError):
                arguments = json.loads(arguments)
        result.append({"id": call.get("id"), "name": function.get("name"), "arguments": arguments})
    return result


def visible_message(message: Mapping[str, Any]) -> JsonObject:
    """Keep the Model-visible state needed to detect changed canonical history."""
    role = message.get("role")
    result: JsonObject = {"role": role, "content": copy.deepcopy(message.get("content") or "")}
    if role == "assistant":
        result["tool_calls"] = calls(message)
    if role == "tool":
        result["tool_call_id"] = message.get("tool_call_id")
        result["content"] = tool_result_text(message.get("content", ""))
        result["is_error"] = canonical_tool_result_is_error(message)
        result["media"] = copy.deepcopy(tool_result_content_blocks(message))
    return result


def history(messages: Sequence[Mapping[str, Any]]) -> list[JsonObject]:
    return [visible_message(message) for message in messages if message.get("role") != "system"]


def system_prompt(messages: Sequence[Mapping[str, Any]]) -> str:
    parts = []
    for message in messages:
        if message.get("role") != "system":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(str(block.get("text", "")) for block in content if isinstance(block, dict))
    return "\n\n".join(parts)


class NativeAgentAdapter(ProviderAdapter):
    """Collect a completed response from the same stream used by Chat."""

    @override
    async def send(self, messages: list[dict], *, model_id: str, **kwargs: Any) -> dict:
        response: JsonObject = {
            "content": "",
            "reasoning": "",
            "usage": {},
            "terminal_outcome": "unknown",
        }
        calls: dict[str, JsonObject] = {}
        async for delta in self.stream(messages, model_id=model_id, **kwargs):
            delta_type = delta["type"]
            if delta_type == "content_delta":
                response["content"] += delta["text"]
            elif delta_type == "reasoning_delta":
                response["reasoning"] += delta["text"]
            elif delta_type == "tool_call_delta":
                call = calls.setdefault(
                    delta["id"], {"id": delta["id"], "name": "", "arguments": ""}
                )
                call["name"] += delta.get("name_delta", "")
                call["arguments"] += delta.get("arguments_delta", "")
            elif delta_type == "usage":
                response["usage"] = {key: value for key, value in delta.items() if key != "type"}
            elif delta_type == "finish":
                response["terminal_outcome"] = delta["reason"]
        if calls:
            response["tool_calls"] = _normalize_calls(list(calls.values()))
        return response

    @override
    def normalize_response(
        self, response: JsonObject, *, model_id: str | None = None
    ) -> JsonObject:
        del model_id
        result = dict(response)
        if result.get("tool_calls"):
            result["tool_calls"] = _normalize_calls(result["tool_calls"])
        return result


def _normalize_calls(calls: list[JsonObject]) -> list[JsonObject]:
    return [
        normalized
        for index, call in enumerate(calls)
        for normalized in normalize_tool_call_candidates(
            tool_call_id=call.get("id"),
            name=call.get("name"),
            arguments=call.get("arguments"),
            fallback_id=f"tool_call_{index}",
            rejection=call.get(TOOL_CALL_REJECTION_FIELD),
            argument_sequence_index=call.get(TOOL_CALL_ARGUMENT_SEQUENCE_INDEX_FIELD),
            argument_sequence_length=call.get(TOOL_CALL_ARGUMENT_SEQUENCE_LENGTH_FIELD),
        )
    ]
