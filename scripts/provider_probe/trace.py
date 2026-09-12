"""Provider Tool probe: trace."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _provider_tools_from_wire(raw_tools: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_tools, list):
        return []
    tools: list[dict[str, Any]] = []
    for raw_tool in raw_tools:
        if not isinstance(raw_tool, dict):
            continue
        function = raw_tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        description = function.get("description")
        parameters = function.get("parameters")
        if isinstance(name, str) and isinstance(description, str) and isinstance(parameters, dict):
            tools.append(
                {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                }
            )
    return tools


def _load_trace(path: Path) -> dict[str, Any]:
    trace = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(trace, dict):
        raise ValueError("trace root must be a JSON object")
    return trace


def _trace_request(trace: dict[str, Any]) -> dict[str, Any]:
    request = trace.get("request")
    if not isinstance(request, dict):
        raise ValueError("trace has no request object")
    body = request.get("body")
    if not isinstance(body, str):
        raise ValueError("trace request body is not text")
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError("trace request body must be a JSON object")
    return parsed


def _partial_assistant_from_trace(trace: dict[str, Any]) -> dict[str, Any]:
    response = trace.get("response")
    if not isinstance(response, dict):
        raise ValueError("trace has no response object")
    body = response.get("body")
    if not isinstance(body, str):
        raise ValueError("trace response body is not text")
    reasoning_parts: list[str] = []
    content_parts: list[str] = []
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            continue
        delta = choices[0].get("delta")
        if not isinstance(delta, dict):
            continue
        reasoning = delta.get("reasoning_content")
        if not isinstance(reasoning, str):
            reasoning = delta.get("reasoning")
        content = delta.get("content")
        if isinstance(reasoning, str):
            reasoning_parts.append(reasoning)
        if isinstance(content, str):
            content_parts.append(content)
    reasoning = "".join(reasoning_parts)
    content = "".join(content_parts)
    if not reasoning and not content:
        raise ValueError("trace response contains no partial assistant output")
    return {
        "role": "assistant",
        "content": content or None,
        "reasoning": reasoning or None,
    }


def _append_interrupted_continuation(
    messages: list[dict[str, Any]],
    trace: dict[str, Any],
) -> None:
    messages.append(_partial_assistant_from_trace(trace))
    messages.append(
        {
            "role": "user",
            "content": (
                "[Internal recovery notice: The preceding assistant response was "
                "interrupted by the Provider before a finish signal. Continue the same "
                "work from that exact point without repeating preceding text. If you "
                "announced a Tool action, perform the Tool Call now.]"
            ),
        }
    )


def _messages_from_wire(raw_messages: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_messages, list):
        raise ValueError("trace request has no messages list")
    messages: list[dict[str, Any]] = []
    for raw_message in raw_messages:
        if not isinstance(raw_message, dict):
            raise ValueError("trace request contains a non-object message")
        role = raw_message.get("role")
        message = {
            key: value
            for key, value in raw_message.items()
            if key not in {"reasoning_content", "tool_calls"}
        }
        if role == "assistant":
            reasoning = raw_message.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                message["reasoning"] = reasoning
            raw_tool_calls = raw_message.get("tool_calls")
            if isinstance(raw_tool_calls, list):
                tool_calls: list[dict[str, Any]] = []
                for raw_call in raw_tool_calls:
                    if not isinstance(raw_call, dict):
                        continue
                    function = raw_call.get("function")
                    if not isinstance(function, dict):
                        continue
                    arguments = function.get("arguments")
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError:
                            arguments = {}
                    if not isinstance(arguments, dict):
                        arguments = {}
                    tool_calls.append(
                        {
                            "id": str(raw_call.get("id", "")),
                            "name": str(function.get("name", "")),
                            "arguments": arguments,
                        }
                    )
                if tool_calls:
                    message["tool_calls"] = tool_calls
        messages.append(message)
    return messages
