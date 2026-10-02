"""Requests an MCP server sends this client: sampling, roots, elicitation and log messages.

Each answer belongs to the Agent invocation the server is serving. Sampling uses
that Agent's configured Model with only the context the server supplied, never
Session history or other connections; roots are the invocation's work directory;
elicitation becomes a pending input of the invocation's Session. Without an
invocation, sampling is refused and the roots are empty.

Sampling and roots are offered only as the connection's ``sampling`` and
``roots`` policies allow (the runner leaves an ``off`` capability out of the
handshake). With ``sampling: ask`` every request first becomes a pending input
the user accepts or declines. Every request is bounded: the reply is capped at
``MAX_SAMPLING_TOKENS`` and a per-connection token bucket limits how often a
server may sample. Sampling events record what was asked and decided, never
the prompt.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import mcp.types as types

from core.extensions.operations import ExtensionHost
from core.tools.tools import ToolContext

from ._events import ConnectionEvents, dump
from .interactions import InputRequests

# The most tokens one sampling reply may use, whatever the server asks for.
MAX_SAMPLING_TOKENS = 4096
# A connection may sample this many times in a row, regaining one request per
# SAMPLING_REFILL_SECONDS.
SAMPLING_BURST = 5
SAMPLING_REFILL_SECONDS = 12.0
# How much of the server's prompt the user sees when asked to approve it.
SAMPLING_PREVIEW_CHARACTERS = 1200
# The MCP error code for a sampling request the user rejected.
SAMPLING_REJECTED = -1


class _RequestBudget:
    """A token bucket: *burst* requests at once, one more every *refill* seconds."""

    def __init__(self, burst: int, refill: float, clock: Callable[[], float]) -> None:
        self._burst = burst
        self._refill = refill
        self._clock = clock
        self._tokens = float(burst)
        self._updated = clock()

    def take(self) -> bool:
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + (now - self._updated) / self._refill)
        self._updated = now
        if self._tokens < 1:
            return False
        self._tokens -= 1
        return True


class ServerRequests:
    """The client callbacks of one connection.

    *invocation* returns the Tool call the connection is currently serving, or
    ``None`` between calls; *config* returns the connection's configuration,
    whose ``sampling`` and ``roots`` policies decide what the server gets.
    """

    def __init__(
        self,
        connection: str,
        host: ExtensionHost,
        inputs: InputRequests,
        events: ConnectionEvents,
        invocation: Callable[[], ToolContext | None],
        *,
        config: Callable[[], dict[str, Any]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._connection = connection
        self._host = host
        self._inputs = inputs
        self._events = events
        self._invocation = invocation
        self._config = config
        self._budget = _RequestBudget(SAMPLING_BURST, SAMPLING_REFILL_SECONDS, clock)

    async def sample(self, context: Any, params: Any) -> Any:
        request = dump(params)
        policy = self._config().get("sampling", "off")
        max_tokens = min(request["maxTokens"], MAX_SAMPLING_TOKENS)
        record: dict[str, Any] = {
            "policy": policy,
            "model_policy": "configured_agent",
            "additional_context": "none",
            "messages": len(request["messages"]),
            "requested_max_tokens": request["maxTokens"],
            "max_tokens": max_tokens,
            "tools": [tool["name"] for tool in request.get("tools", [])],
        }
        invocation = self._invocation()
        if policy not in {"ask", "allow"}:
            return self._refuse(record, "disabled", "Sampling is turned off for this connection")
        if invocation is None:
            return self._refuse(
                record, "no_invocation", "Sampling requires an active Agent invocation"
            )
        if not self._budget.take():
            return self._refuse(
                record, "rate_limited", "Too many sampling requests; try again later"
            )
        messages = sampling_messages(request)
        if policy == "ask":
            answer = await self._inputs.request(
                self._connection,
                "sampling",
                {"message": self._approval_message(request, max_tokens)},
                invocation.session_id,
            )
            if answer.get("action") != "accept":
                return self._refuse(
                    record,
                    "declined",
                    "The user declined the sampling request",
                    code=SAMPLING_REJECTED,
                )
        self._events.record("sampling_request", {**record, "outcome": "sent"})
        sample = await self._host.sample(
            invocation,
            {
                "messages": messages,
                "max_tokens": max_tokens,
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

    def _refuse(
        self,
        record: dict[str, Any],
        outcome: str,
        message: str,
        code: int = types.INVALID_REQUEST,
    ) -> types.ErrorData:
        self._events.record("sampling_request", {**record, "outcome": outcome})
        return types.ErrorData(code=code, message=message)

    def _approval_message(self, request: dict[str, Any], max_tokens: int) -> str:
        """What the user approves: who asks, how much, and the start of the prompt."""
        tools = [tool["name"] for tool in request.get("tools", [])]
        summary = (
            f"The MCP connection {self._connection} asks to use the Agent's Model for a "
            f"reply of up to {max_tokens} tokens. It sends only the prompt below, never "
            "the Session history. Accept to send it, or decline."
        )
        if tools:
            summary += f" The reply may request these server Tools: {', '.join(tools)}."
        return f"{summary}\n\n{_preview(request)}"

    async def roots(self, context: Any) -> Any:
        roots = []
        invocation = self._invocation()
        if invocation is not None and self._config().get("roots") == "workspace":
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


def _preview(request: dict[str, Any]) -> str:
    """The server's prompt as plain text, shortened to ``SAMPLING_PREVIEW_CHARACTERS``."""
    lines = [f"system: {request['systemPrompt']}"] if request.get("systemPrompt") else []
    for message in request["messages"]:
        blocks = (
            message["content"] if isinstance(message["content"], list) else [message["content"]]
        )
        parts = [
            block["text"] if block.get("type") == "text" else f"[{block.get('type', 'content')}]"
            for block in blocks
        ]
        lines.append(f"{message['role']}: {' '.join(parts)}")
    text = "\n".join(lines)
    if len(text) > SAMPLING_PREVIEW_CHARACTERS:
        text = text[: SAMPLING_PREVIEW_CHARACTERS - 3] + "..."
    return text


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
