"""Canonical history, media and SDK event translation for Claude Agent."""

from __future__ import annotations

import contextlib
import copy
import json
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from typing import Any

from core.providers._messages_stream import messages_terminal_outcome
from core.providers.adapter import (
    canonical_tool_result_is_error,
    tool_result_content_blocks,
    tool_result_text,
)
from core.providers.anthropic_compatible import extract_anthropic_usage
from core.providers.errors import ProviderAuthError, ProviderError, ProviderUsageLimitError

JsonObject = dict[str, Any]

HISTORY_PREFIX = (
    "Conversation history from vBot follows. Its role labels identify earlier messages, "
    "not new requests. Continue with the messages after this history."
)


def record(value: Any) -> JsonObject:
    """Read SDK dataclasses and fake wire records through the same private seam."""
    if isinstance(value, dict):
        return value
    if is_dataclass(value) and not isinstance(value, type):

        def convert(item: Any) -> Any:
            if is_dataclass(item) and not isinstance(item, type):
                return record(item)
            if isinstance(item, list):
                return [convert(child) for child in item]
            if isinstance(item, dict):
                return {key: convert(child) for key, child in item.items()}
            return item

        result = {entry.name: convert(getattr(value, entry.name)) for entry in fields(value)}
        result.setdefault("type", type(value).__name__)
        return result
    raise ProviderError("Claude Code returned an unsupported message", retryable=False)


def kind(value: JsonObject) -> str:
    raw = value.get("type", "")
    mapping = {
        "StreamEvent": "stream_event",
        "AssistantMessage": "assistant",
        "ResultMessage": "result",
        "SystemMessage": "system",
        "UserMessage": "user",
        "RateLimitEvent": "rate_limit_event",
    }
    return str(mapping.get(raw, raw))


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


def content_blocks(content: Any) -> list[JsonObject]:
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}] if content else []
    blocks: list[JsonObject] = []
    for block in content:
        if not isinstance(block, Mapping):
            raise ProviderError("Unsupported Claude Agent content block", retryable=False)
        block_type = block.get("type")
        if block_type == "text":
            blocks.append({"type": "text", "text": str(block.get("text", ""))})
        elif block_type in {"media", "document"} and "source" not in block:
            mime = block.get("media_type")
            data = block.get("base64")
            if mime not in {
                "image/png",
                "image/jpeg",
                "image/gif",
                "image/webp",
                "application/pdf",
            } or not isinstance(data, str):
                raise ProviderError(
                    "Claude Agent supports images and PDF documents", retryable=False
                )
            blocks.append(
                {
                    "type": "document" if mime == "application/pdf" else "image",
                    "source": {"type": "base64", "media_type": mime, "data": data},
                }
            )
        elif block_type in {"image", "document"} and isinstance(block.get("source"), Mapping):
            blocks.append(copy.deepcopy(dict(block)))
        else:
            raise ProviderError("Unsupported Claude Agent content type", retryable=False)
    return blocks


def prompt_content(messages: Sequence[Mapping[str, Any]], *, checkpoint: bool) -> list[JsonObject]:
    """Transport prior roles as labeled data; retain attachments as native blocks."""
    blocks: list[JsonObject] = []
    prior_end = (
        max(
            (index + 1 for index, message in enumerate(messages) if message.get("role") != "user"),
            default=0,
        )
        if checkpoint
        else 0
    )
    if prior_end:
        blocks.append({"type": "text", "text": HISTORY_PREFIX})
        for message in messages[:prior_end]:
            clean = visible_message(message)
            media = clean.pop("media", [])
            raw_content = clean.pop("content", "")
            blocks.append(
                {"type": "text", "text": json.dumps(clean, ensure_ascii=False, sort_keys=True)}
            )
            blocks.extend(content_blocks(raw_content))
            if media:
                blocks.extend(content_blocks(media))
        blocks.append({"type": "text", "text": "End of conversation history."})
    for message in messages[prior_end:]:
        blocks.extend(content_blocks(message.get("content", "")))
    return blocks


def tool_result(message: Mapping[str, Any]) -> tuple[JsonObject, list[JsonObject] | None]:
    native = [{"type": "text", "text": tool_result_text(message.get("content", ""))}]
    native.extend(content_blocks(tool_result_content_blocks(message)))
    mcp_content = []
    replacement = False
    for block in native:
        if block["type"] == "text":
            mcp_content.append(block)
        elif block["type"] == "image" and block["source"]["type"] == "base64":
            source = block["source"]
            mcp_content.append(
                {"type": "image", "mimeType": source["media_type"], "data": source["data"]}
            )
        else:
            replacement = True
    return {
        "content": mcp_content,
        "isError": canonical_tool_result_is_error(message),
    }, native if replacement else None


class StreamState:
    """One vBot Model step, even while the native Claude turn spans Tools."""

    def __init__(self, names: Mapping[str, str], *, subscription: bool) -> None:
        self.names = names
        self.subscription = subscription
        self.text = ""
        self.reasoning = ""
        self.calls: list[JsonObject] = []
        self.streamed: set[str] = set()
        self.message_id = ""
        self.message_stopped = False
        self.stop_reason: str | None = None
        self.usage: JsonObject = {}
        self._usage_by_message: dict[str, JsonObject] = {}
        self.error_code: str | None = None
        self.rate_limit: JsonObject = {}

    def update_usage(self, raw: Any) -> JsonObject | None:
        if not isinstance(raw, dict):
            return None
        counters = self._usage_by_message.setdefault(self.message_id, {})
        for key, value in raw.items():
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                counters[key] = max(value, counters.get(key, 0))
            elif key == "output_tokens_details" and isinstance(value, dict):
                counters[key] = value
        normalized = extract_anthropic_usage({"usage": counters})
        if normalized is None:
            return None
        normalized_calls = [
            extract_anthropic_usage({"usage": value}) or {}
            for value in self._usage_by_message.values()
        ]
        for field in ("output_tokens", "reasoning_tokens"):
            values = [item[field] for item in normalized_calls if field in item]
            if values:
                normalized[field] = sum(values)
        self.usage = normalized
        if self.subscription:
            normalized["reported_cost_usd"] = 0.0
        return {"type": "usage", **normalized}

    def consume(self, message: JsonObject) -> list[JsonObject]:
        parts: list[JsonObject] = []
        message_kind = kind(message)
        if message.get("parent_tool_use_id") is not None:
            raise ProviderError(
                "Claude Code emitted an unexpected nested Agent response", retryable=False
            )
        if message_kind == "rate_limit_event":
            self.rate_limit = message.get("rate_limit_info", {})
        if message_kind == "stream_event":
            event = message.get("event", {})
            event_type = event.get("type")
            if event_type == "message_start":
                self.message_stopped = False
                self.stop_reason = None
                self.message_id = event.get("message", {}).get("id", "")
                self.streamed.add(self.message_id)
                usage = self.update_usage(event.get("message", {}).get("usage"))
                if usage:
                    parts.append(usage)
            if event_type == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = str(delta.get("text", ""))
                    self.text += text
                    parts.append({"type": "content_delta", "text": text})
                elif delta.get("type") == "thinking_delta":
                    text = str(delta.get("thinking", ""))
                    self.reasoning += text
                    parts.append({"type": "reasoning_delta", "text": text})
            if event_type == "message_delta":
                self.stop_reason = event.get("delta", {}).get("stop_reason")
                usage = self.update_usage(event.get("usage"))
                if usage:
                    parts.append(usage)
            if event_type == "message_stop":
                self.message_stopped = True
                if self.calls:
                    parts.append(
                        {
                            "type": "finish",
                            "reason": messages_terminal_outcome(
                                self.stop_reason, has_tool_calls=True
                            ),
                        }
                    )
            if event_type == "content_block_start":
                block = event.get("content_block", {})
                if block.get("type") == "text" and block.get("text"):
                    text = str(block["text"])
                    self.text += text
                    parts.append({"type": "content_delta", "text": text})
                elif block.get("type") == "thinking" and block.get("thinking"):
                    text = str(block["thinking"])
                    self.reasoning += text
                    parts.append({"type": "reasoning_delta", "text": text})
        if message_kind == "assistant":
            if message.get("error"):
                self.error_code = message["error"]
                return parts
            raw = message.get("message", message)
            if raw.get("stop_reason") is not None:
                self.stop_reason = raw["stop_reason"]
            message_id = raw.get("id") or raw.get("message_id") or ""
            if message_id:
                self.message_id = message_id
            usage = self.update_usage(raw.get("usage"))
            if usage:
                parts.append(usage)
            for block in raw.get("content", []):
                block = record(block)
                block_type = block.get("type")
                block_type = {
                    "TextBlock": "text",
                    "ThinkingBlock": "thinking",
                    "ToolUseBlock": "tool_use",
                }.get(str(block_type), block_type)
                if block_type == "text" and message_id not in self.streamed:
                    text = str(block.get("text", ""))
                    self.text += text
                    parts.append({"type": "content_delta", "text": text})
                elif block_type == "thinking" and message_id not in self.streamed:
                    text = str(block.get("thinking", ""))
                    self.reasoning += text
                    parts.append({"type": "reasoning_delta", "text": text})
                elif block_type == "tool_use":
                    name = self.names.get(str(block.get("name", "")))
                    if name is None:
                        raise ProviderError(
                            "Claude Code called a Tool outside the offered vBot catalog",
                            retryable=False,
                        )
                    call = {
                        "id": block.get("id"),
                        "name": name,
                        "arguments": block.get("input", {}),
                    }
                    self.calls.append(call)
                    parts.append(
                        {
                            "type": "tool_call_delta",
                            "id": call["id"],
                            "name_delta": name,
                            "arguments_delta": json.dumps(call["arguments"], sort_keys=True),
                        }
                    )
        if message_kind == "result":
            if (
                message.get("is_error")
                or message.get("subtype") != "success"
                or (message.get("api_error_status") or 0) >= 400
            ):
                self.raise_error(message)
            if not self.usage:
                usage = self.update_usage(message.get("usage"))
                if usage:
                    parts.append(usage)
            if self.calls:
                # Older native versions can omit message_stop on a cut stream.
                raise ProviderError(
                    "Claude Code ended before completing its Tool-call message.", retryable=False
                )
            stop_reason = message.get("stop_reason") or self.stop_reason
            reason = (
                messages_terminal_outcome(stop_reason, has_tool_calls=False)
                if stop_reason is not None
                else "stop"
            )
            parts.append({"type": "finish", "reason": reason})
        return parts

    def raise_error(self, message: JsonObject) -> None:
        status = message.get("api_error_status")
        if self.error_code in {"authentication_failed", "authentication_error"} or status in {
            401,
            403,
        }:
            raise ProviderAuthError(
                "Claude Code is not signed in. Run `claude auth login` on the vBot server."
            )
        if self.error_code == "rate_limit" or status == 429:
            reset = self.rate_limit.get("resets_at", self.rate_limit.get("resetsAt"))
            timestamp = None
            if isinstance(reset, (int, float)) and not isinstance(reset, bool):
                with contextlib.suppress(OverflowError, OSError, ValueError):
                    timestamp = datetime.fromtimestamp(reset, UTC)
            raise ProviderUsageLimitError(
                "Claude Code's usage limit is reached.", resets_at=timestamp
            )
        raise ProviderError(
            "Claude Code could not complete the request after its own recovery.", retryable=False
        )

    def assistant(self) -> JsonObject:
        return {"role": "assistant", "content": self.text, "tool_calls": copy.deepcopy(self.calls)}
