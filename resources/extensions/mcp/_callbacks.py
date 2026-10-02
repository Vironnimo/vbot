"""Requests an MCP server sends this client: sampling, roots, elicitation and log messages.

Each answer belongs to the Agent invocation the server is serving. Sampling uses
that Agent's configured Model with only the context the server supplied, never
Session history or other connections; roots are the invocation's work directory;
elicitation becomes a pending input of the invocation's Session. Without an
invocation, sampling is refused and the roots are empty.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import mcp.types as types

from core.extensions.operations import ExtensionHost
from core.tools.tools import ToolContext

from ._events import ConnectionEvents, dump
from .interactions import InputRequests


class ServerRequests:
    """The client callbacks of one connection.

    *invocation* returns the Tool call the connection is currently serving, or
    ``None`` between calls.
    """

    def __init__(
        self,
        connection: str,
        host: ExtensionHost,
        inputs: InputRequests,
        events: ConnectionEvents,
        invocation: Callable[[], ToolContext | None],
    ) -> None:
        self._connection = connection
        self._host = host
        self._inputs = inputs
        self._events = events
        self._invocation = invocation

    async def sample(self, context: Any, params: Any) -> Any:
        invocation = self._invocation()
        if invocation is None:
            return types.ErrorData(
                code=types.INVALID_REQUEST, message="Sampling requires an active Agent invocation"
            )
        request = dump(params)
        self._events.record(
            "sampling_request",
            {"request": request, "model_policy": "configured_agent", "additional_context": "none"},
        )
        messages = sampling_messages(request)
        sample = await self._host.sample(
            invocation,
            {
                "messages": messages,
                "max_tokens": request["maxTokens"],
                "temperature": request.get("temperature"),
                "stop_sequences": request.get("stopSequences"),
                "tool_choice": request.get("toolChoice", {}).get("mode"),
                "tools": [
                    {
                        "name": tool["name"],
                        "description": tool.get("description", tool["name"]),
                        "parameters": tool["inputSchema"],
                    }
                    for tool in request.get("tools", [])
                ],
            },
        )
        self._events.record("sampling_usage", sample.get("usage", {}))
        content = []
        if sample.get("content"):
            content.append({"type": "text", "text": sample["content"]})
        for call in sample.get("tool_calls", []):
            content.append(
                {
                    "type": "tool_use",
                    "id": call["id"],
                    "name": call["name"],
                    "input": call["arguments"],
                }
            )
        stop_reason = "toolUse" if sample.get("tool_calls") else "endTurn"
        if sample.get("terminal_outcome") == "output_truncated":
            stop_reason = "maxTokens"
        result = {
            "role": "assistant",
            "model": sample["model"],
            "content": content,
            "stopReason": stop_reason,
        }
        if request.get("tools"):
            return types.CreateMessageResultWithTools.model_validate(result)
        result["content"] = content[0] if content else {"type": "text", "text": ""}
        return types.CreateMessageResult.model_validate(result)

    async def roots(self, context: Any) -> Any:
        roots = []
        invocation = self._invocation()
        if invocation is not None:
            roots.append(
                types.Root.model_validate({"uri": Path(invocation.effective_cwd).as_uri()})
            )
        return types.ListRootsResult(roots=roots)

    async def elicit(self, context: Any, params: Any) -> Any:
        invocation = self._invocation()
        session_id = invocation.session_id if invocation is not None else None
        response = await self._inputs.request(
            self._connection, "elicitation", dump(params), session_id
        )
        return types.ElicitResult.model_validate(response)

    async def log(self, params: Any) -> None:
        self._events.record("log", dump(params))


def sampling_messages(request: dict[str, Any]) -> list[dict[str, Any]]:
    """Map only the server's explicit sampling context, never Session history."""
    messages: list[dict[str, Any]] = []
    if request.get("systemPrompt"):
        messages.append({"role": "system", "content": request["systemPrompt"]})
    for message in request["messages"]:
        blocks = (
            message["content"] if isinstance(message["content"], list) else [message["content"]]
        )
        content = []
        calls = []
        results = []
        for block in blocks:
            kind = block["type"]
            if kind == "text":
                content.append({"type": "text", "text": block["text"]})
            elif kind in {"image", "audio"}:
                content.append(
                    {"type": kind, "base64": block["data"], "media_type": block["mimeType"]}
                )
            elif kind == "tool_use":
                calls.append(
                    {"id": block["id"], "name": block["name"], "arguments": block["input"]}
                )
            elif kind == "tool_result":
                results.append(
                    {
                        "role": "tool",
                        "tool_call_id": block["toolUseId"],
                        "content": json.dumps(block, ensure_ascii=False),
                    }
                )
            else:
                raise ValueError(f"Unsupported sampling content type: {kind}")
        if content or calls:
            entry = {"role": message["role"], "content": content}
            if calls:
                entry["tool_calls"] = calls
            messages.append(entry)
        messages.extend(results)
    return messages
