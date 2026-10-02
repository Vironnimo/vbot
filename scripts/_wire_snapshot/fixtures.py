"""The canonical conversation, Tools and request context every snapshot render sends.

The conversation is one Run: a system prompt, the user's request, an Assistant
turn with readable reasoning, protocol-specific opaque reasoning state and a
Tool Call, its Tool Result, and a follow-up user message queued into the same
Run. Chat's own projection helpers turn it into Provider request messages, so
the snapshot sends what Chat would send for that history.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from core.chat.messages import ChatMessage, ToolCall
from core.chat.wire_shaping import (
    _assistant_continuation_dict,
    _message_to_request_dict,
    model_facing_request,
)
from core.providers.reasoning import ReasoningReplayPolicy
from scripts._wire_snapshot.canned import (
    PROTOCOL_CHAT,
    PROTOCOL_GEMINI,
    PROTOCOL_MESSAGES,
    PROTOCOL_RESPONSES,
)

AGENT_ID = "snapshot-agent"
SESSION_ID = "snapshot-session"
PROMPT_CACHE_AFFINITY_ID = "snapshot-prompt-cache-affinity"

SYSTEM_PROMPT = (
    "You are the wire snapshot Agent. Answer briefly and use the available Tools when needed."
)
USER_REQUEST = "Read README.md and summarize it in one sentence."
ASSISTANT_CONTENT = "I will read the file first."
ASSISTANT_REASONING = "The user wants a summary, so I need the file contents before answering."
TOOL_CALL_ID = "call_snapshot_read"
TOOL_CALL_ARGUMENTS = {"path": "README.md"}
TOOL_RESULT = "# vBot\nA local-first Agent runtime with Tools, Sessions and Skills."
FOLLOW_UP = "Thanks. Also search the web for the latest vBot release."
IMAGE_PROMPT = "What does the attached image show?"
# A valid 1x1 PNG; Adapters never decode attachment bytes.
IMAGE_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
IMAGE_NOTE = "[Image 1: snapshot.png (image/png), saved at attachments/snapshot.png]"
_IMAGE_MEDIA_PREFERENCE = ("image/png", "image/jpeg", "image/webp", "image/gif")

_TIMESTAMP = datetime(2026, 1, 1, tzinfo=UTC)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "read",
        "description": "Read a text file from the workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path."},
                "offset": {"type": "integer", "minimum": 0},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "web_search",
        "description": "Search the web and return result snippets.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
        },
    },
]


def reasoning_meta_for(protocol: str | None) -> dict[str, Any] | None:
    """Return the opaque reasoning state a *protocol* stores on an Assistant turn."""

    if protocol == PROTOCOL_CHAT:
        return {
            "reasoning_details": [
                {
                    "type": "reasoning.text",
                    "text": ASSISTANT_REASONING,
                    "signature": "sig-chat-history",
                    "format": "unknown",
                    "index": 0,
                },
                {
                    "type": "reasoning.encrypted",
                    "data": "ENC-CHAT-HISTORY",
                    "format": "unknown",
                    "index": 1,
                },
            ]
        }
    if protocol == PROTOCOL_MESSAGES:
        return {
            "content_blocks": [
                {
                    "type": "thinking",
                    "thinking": ASSISTANT_REASONING,
                    "signature": "sig-messages-history",
                },
                {"type": "redacted_thinking", "data": "REDACTED-MESSAGES-HISTORY"},
            ]
        }
    if protocol == PROTOCOL_RESPONSES:
        return {
            "response_output": [
                {
                    "type": "reasoning",
                    "id": "rs_history",
                    "summary": [{"type": "summary_text", "text": ASSISTANT_REASONING}],
                    "encrypted_content": "ENC-RESPONSES-HISTORY",
                },
                {
                    "type": "message",
                    "id": "msg_history",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": ASSISTANT_CONTENT, "annotations": []}
                    ],
                },
                {
                    "type": "function_call",
                    "id": "fc_history",
                    "call_id": TOOL_CALL_ID,
                    "name": "read",
                    "arguments": '{"path":"README.md"}',
                    "status": "completed",
                },
            ]
        }
    if protocol == PROTOCOL_GEMINI:
        return {
            "gemini_parts": [
                {
                    "text": ASSISTANT_REASONING,
                    "thought": True,
                    "thoughtSignature": "sig-gemini-history-thought",
                },
                {"text": ASSISTANT_CONTENT},
                {
                    "functionCall": {
                        "id": TOOL_CALL_ID,
                        "name": "read",
                        "args": TOOL_CALL_ARGUMENTS,
                    },
                    "thoughtSignature": "sig-gemini-history-call",
                },
            ]
        }
    return None


def image_media_type(wire_media_types: frozenset[str]) -> str | None:
    """Return the image type an attachment is sent as, or ``None`` without image support."""

    for media_type in _IMAGE_MEDIA_PREFERENCE:
        if media_type in wire_media_types:
            return media_type
    return None


def request_messages(
    *,
    agent_model: str,
    protocol: str | None,
    replay_policy: ReasoningReplayPolicy,
    image_media: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return the Provider request ``(messages, tools)`` Chat builds for the fixture Run.

    ``protocol`` selects the opaque reasoning state on the Assistant turn;
    ``image_media`` turns the follow-up into an image attachment as Chat
    resolves it (native media block plus a path note).
    """

    system = replace(
        ChatMessage.system(SYSTEM_PROMPT, agent_model, timestamp=_TIMESTAMP),
        id="snapshot-system",
    )
    user = _message("snapshot-user", "user", content=USER_REQUEST)
    assistant = _message(
        "snapshot-assistant",
        "assistant",
        content=ASSISTANT_CONTENT,
        model=agent_model,
        reasoning=ASSISTANT_REASONING,
        reasoning_meta=reasoning_meta_for(protocol),
        tool_calls=[ToolCall(id=TOOL_CALL_ID, name="read", arguments=dict(TOOL_CALL_ARGUMENTS))],
    )
    tool_result = _message(
        "snapshot-tool-result",
        "tool",
        content=TOOL_RESULT,
        tool_call_id=TOOL_CALL_ID,
        name="read",
    )
    follow_up = _message("snapshot-follow-up", "user", content=FOLLOW_UP)

    follow_up_dict = _message_to_request_dict(follow_up)
    if image_media is not None:
        follow_up_dict["content"] = [
            {"type": "text", "text": IMAGE_PROMPT},
            {"type": "media", "base64": IMAGE_BASE64, "media_type": image_media},
            {"type": "text", "text": IMAGE_NOTE},
        ]
    messages = [
        system.to_dict(),
        _message_to_request_dict(user),
        _assistant_continuation_dict(assistant, replay_policy=replay_policy),
        _message_to_request_dict(tool_result),
        follow_up_dict,
    ]
    return model_facing_request(messages, copy.deepcopy(TOOLS))


def _message(message_id: str, role: Any, **fields: Any) -> ChatMessage:
    return ChatMessage(
        id=message_id,
        timestamp=_TIMESTAMP.isoformat(),
        role=role,
        **fields,
    )
