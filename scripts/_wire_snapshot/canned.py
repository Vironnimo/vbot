"""Wire protocol detection and canned Provider responses for the offline snapshot.

The mock transport answers a chat request with the canned response of the wire
protocol its URL addresses, as JSON or as a stream when the request asks for
one. Every canned answer carries readable reasoning, opaque reasoning state, a
Tool Call, usage and a terminal reason, so response normalization is exercised
end to end. Chat Completions answers carry a *different* text in each readable
reasoning field so a record shows which field an adapter reads.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

PROTOCOL_CHAT = "chat_completions"
PROTOCOL_MESSAGES = "messages"
PROTOCOL_RESPONSES = "responses"
PROTOCOL_GEMINI = "gemini"
PROTOCOL_OLLAMA = "ollama_native"

CANNED_CONTENT = "Here is the answer."
CANNED_TOOL_CALL_ID = "call_canned_1"
CANNED_TOOL_NAME = "web_search"
CANNED_TOOL_ARGUMENTS = {"query": "vBot release"}
_CANNED_ARGUMENTS_JSON = json.dumps(CANNED_TOOL_ARGUMENTS, separators=(",", ":"))
_CREATED = 1767225600  # 2026-01-01T00:00:00Z


def protocol_for_path(path: str) -> str | None:
    """Return the chat wire protocol a request path addresses, if any."""

    if path.endswith("/chat/completions"):
        return PROTOCOL_CHAT
    if path.endswith("/messages"):
        return PROTOCOL_MESSAGES
    if path.endswith("/responses"):
        return PROTOCOL_RESPONSES
    if ":generateContent" in path or ":streamGenerateContent" in path:
        return PROTOCOL_GEMINI
    if path.endswith("/api/chat"):
        return PROTOCOL_OLLAMA
    return None


def canned_chat_response(request: httpx.Request) -> httpx.Response:
    """Answer one chat request with the canned response of its protocol."""

    protocol = protocol_for_path(request.url.path)
    body = _json_body(request)
    raw_model = body.get("model")
    model = raw_model if isinstance(raw_model, str) else "snapshot-model"
    streaming = body.get("stream") is True or ":streamGenerateContent" in request.url.path
    if protocol == PROTOCOL_CHAT:
        return _sse(_chat_stream(model)) if streaming else _json(_chat_json(model))
    if protocol == PROTOCOL_MESSAGES:
        return _sse(_messages_stream(model)) if streaming else _json(_messages_json(model))
    if protocol == PROTOCOL_RESPONSES:
        return _sse(_responses_stream(model)) if streaming else _json(_responses_json(model))
    if protocol == PROTOCOL_GEMINI:
        return _sse(_gemini_stream(model)) if streaming else _json(_gemini_json(model))
    if protocol == PROTOCOL_OLLAMA:
        return _ndjson(_ollama_stream(model)) if streaming else _json(_ollama_json(model))
    return httpx.Response(404, json={"error": {"message": "snapshot: unknown chat endpoint"}})


def _json_body(request: httpx.Request) -> dict[str, Any]:
    try:
        body = json.loads(request.content or b"{}")
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _json(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _sse(events: list[tuple[str | None, Any]]) -> httpx.Response:
    lines: list[str] = []
    for event_name, data in events:
        if event_name is not None:
            lines.append(f"event: {event_name}")
        lines.append(f"data: {data if isinstance(data, str) else json.dumps(data)}")
        lines.append("")
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=("\n".join(lines) + "\n").encode("utf-8"),
    )


def _ndjson(chunks: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/x-ndjson"},
        content="".join(json.dumps(chunk) + "\n" for chunk in chunks).encode("utf-8"),
    )


# ---------------------------------------------------------------------------
# Chat Completions
# ---------------------------------------------------------------------------


def _chat_usage() -> dict[str, Any]:
    return {
        "prompt_tokens": 1200,
        "completion_tokens": 340,
        "total_tokens": 1540,
        "prompt_tokens_details": {"cached_tokens": 800},
        "completion_tokens_details": {"reasoning_tokens": 120},
    }


def _chat_reasoning_details(source: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "reasoning.text",
            "text": f"Detail text from reasoning_details ({source}).",
            "signature": f"sig-chat-{source}",
            "format": "unknown",
            "index": 0,
        },
        {
            "type": "reasoning.encrypted",
            "data": f"ENC-CHAT-{source.upper()}",
            "format": "unknown",
            "index": 1,
        },
    ]


def _chat_json(model: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-snapshot",
        "object": "chat.completion",
        "created": _CREATED,
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": CANNED_CONTENT,
                    "reasoning": "Text from the reasoning field.",
                    "reasoning_content": "Text from the reasoning_content field.",
                    "reasoning_text": "Text from the reasoning_text field.",
                    "thinking": "Text from the thinking field.",
                    "reasoning_details": _chat_reasoning_details("response"),
                    "tool_calls": [
                        {
                            "id": CANNED_TOOL_CALL_ID,
                            "type": "function",
                            "function": {
                                "name": CANNED_TOOL_NAME,
                                "arguments": _CANNED_ARGUMENTS_JSON,
                            },
                        }
                    ],
                },
            }
        ],
        "usage": _chat_usage(),
    }


def _chat_chunk(model: str, delta: dict[str, Any], finish_reason: str | None = None) -> Any:
    return {
        "id": "chatcmpl-snapshot",
        "object": "chat.completion.chunk",
        "created": _CREATED,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _chat_stream(model: str) -> list[tuple[str | None, Any]]:
    return [
        (None, _chat_chunk(model, {"role": "assistant"})),
        (
            None,
            _chat_chunk(
                model,
                {
                    "reasoning": "Stream text from the reasoning field. ",
                    "reasoning_content": "Stream text from the reasoning_content field. ",
                    "reasoning_text": "Stream text from the reasoning_text field. ",
                    "thinking": "Stream text from the thinking field. ",
                },
            ),
        ),
        (None, _chat_chunk(model, {"reasoning_details": _chat_reasoning_details("stream")})),
        (None, _chat_chunk(model, {"content": "Here is "})),
        (None, _chat_chunk(model, {"content": "the answer."})),
        (
            None,
            _chat_chunk(
                model,
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": CANNED_TOOL_CALL_ID,
                            "type": "function",
                            "function": {"name": CANNED_TOOL_NAME, "arguments": ""},
                        }
                    ]
                },
            ),
        ),
        (
            None,
            _chat_chunk(
                model,
                {"tool_calls": [{"index": 0, "function": {"arguments": _CANNED_ARGUMENTS_JSON}}]},
            ),
        ),
        (None, _chat_chunk(model, {}, finish_reason="tool_calls")),
        (
            None,
            {
                "id": "chatcmpl-snapshot",
                "object": "chat.completion.chunk",
                "created": _CREATED,
                "model": model,
                "choices": [],
                "usage": _chat_usage(),
            },
        ),
        (None, "[DONE]"),
    ]


# ---------------------------------------------------------------------------
# Anthropic Messages
# ---------------------------------------------------------------------------


def _messages_usage() -> dict[str, Any]:
    return {
        "input_tokens": 400,
        "output_tokens": 340,
        "cache_read_input_tokens": 800,
        "cache_creation_input_tokens": 100,
    }


def _messages_json(model: str) -> dict[str, Any]:
    return {
        "id": "msg_snapshot",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [
            {
                "type": "thinking",
                "thinking": "Messages thinking text.",
                "signature": "sig-messages-response",
            },
            {"type": "redacted_thinking", "data": "REDACTED-MESSAGES-RESPONSE"},
            {"type": "text", "text": CANNED_CONTENT},
            {
                "type": "tool_use",
                "id": CANNED_TOOL_CALL_ID,
                "name": CANNED_TOOL_NAME,
                "input": CANNED_TOOL_ARGUMENTS,
            },
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": _messages_usage(),
    }


def _messages_stream(model: str) -> list[tuple[str | None, Any]]:
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_snapshot",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {**_messages_usage(), "output_tokens": 1},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "Messages stream thinking text."},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "sig-messages-stream"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "redacted_thinking", "data": "REDACTED-MESSAGES-STREAM"},
        },
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "text_delta", "text": CANNED_CONTENT},
        },
        {"type": "content_block_stop", "index": 2},
        {
            "type": "content_block_start",
            "index": 3,
            "content_block": {
                "type": "tool_use",
                "id": CANNED_TOOL_CALL_ID,
                "name": CANNED_TOOL_NAME,
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 3,
            "delta": {"type": "input_json_delta", "partial_json": _CANNED_ARGUMENTS_JSON},
        },
        {"type": "content_block_stop", "index": 3},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 340},
        },
        {"type": "message_stop"},
    ]
    return [(event["type"], event) for event in events]


# ---------------------------------------------------------------------------
# OpenAI Responses
# ---------------------------------------------------------------------------


def _responses_items(source: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "reasoning",
            "id": "rs_canned",
            "summary": [{"type": "summary_text", "text": f"Responses summary text ({source})."}],
            "encrypted_content": f"ENC-RESPONSES-{source.upper()}",
        },
        {
            "type": "message",
            "id": "msg_canned",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": CANNED_CONTENT, "annotations": []}],
        },
        {
            "type": "function_call",
            "id": "fc_canned",
            "call_id": CANNED_TOOL_CALL_ID,
            "name": CANNED_TOOL_NAME,
            "arguments": _CANNED_ARGUMENTS_JSON,
            "status": "completed",
        },
    ]


def _responses_usage() -> dict[str, Any]:
    return {
        "input_tokens": 1200,
        "input_tokens_details": {"cached_tokens": 800},
        "output_tokens": 340,
        "output_tokens_details": {"reasoning_tokens": 120},
        "total_tokens": 1540,
    }


def _responses_json(model: str, source: str = "response") -> dict[str, Any]:
    return {
        "id": "resp_snapshot",
        "object": "response",
        "created_at": _CREATED,
        "status": "completed",
        "model": model,
        "output": _responses_items(source),
        "usage": _responses_usage(),
    }


def _responses_stream(model: str) -> list[tuple[str | None, Any]]:
    reasoning, message, function_call = _responses_items("stream")
    events: list[dict[str, Any]] = [
        {
            "type": "response.created",
            "response": {
                "id": "resp_snapshot",
                "object": "response",
                "status": "in_progress",
                "model": model,
                "output": [],
            },
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "reasoning", "id": "rs_canned", "summary": []},
        },
        {
            "type": "response.reasoning_summary_text.delta",
            "output_index": 0,
            "item_id": "rs_canned",
            "summary_index": 0,
            "delta": "Responses summary text (stream).",
        },
        {"type": "response.output_item.done", "output_index": 0, "item": reasoning},
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {**message, "status": "in_progress", "content": []},
        },
        {
            "type": "response.output_text.delta",
            "output_index": 1,
            "item_id": "msg_canned",
            "content_index": 0,
            "delta": CANNED_CONTENT,
        },
        {"type": "response.output_item.done", "output_index": 1, "item": message},
        {
            "type": "response.output_item.added",
            "output_index": 2,
            "item": {**function_call, "status": "in_progress", "arguments": ""},
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 2,
            "item_id": "fc_canned",
            "delta": _CANNED_ARGUMENTS_JSON,
        },
        {"type": "response.output_item.done", "output_index": 2, "item": function_call},
        {"type": "response.completed", "response": _responses_json(model, "stream")},
    ]
    return [(event["type"], event) for event in events]


# ---------------------------------------------------------------------------
# Gemini (OpenCode Zen)
# ---------------------------------------------------------------------------


def _gemini_usage() -> dict[str, Any]:
    return {
        "promptTokenCount": 1200,
        "candidatesTokenCount": 220,
        "thoughtsTokenCount": 120,
        "cachedContentTokenCount": 800,
        "totalTokenCount": 1540,
    }


def _gemini_parts(source: str) -> list[dict[str, Any]]:
    return [
        {
            "text": f"Gemini thought text ({source}).",
            "thought": True,
            "thoughtSignature": f"sig-gemini-thought-{source}",
        },
        {"text": CANNED_CONTENT},
        {
            "functionCall": {
                "id": CANNED_TOOL_CALL_ID,
                "name": CANNED_TOOL_NAME,
                "args": CANNED_TOOL_ARGUMENTS,
            },
            "thoughtSignature": f"sig-gemini-call-{source}",
        },
    ]


def _gemini_json(model: str) -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": _gemini_parts("response")},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": _gemini_usage(),
        "modelVersion": model,
        "responseId": "resp-gemini-snapshot",
    }


def _gemini_stream(model: str) -> list[tuple[str | None, Any]]:
    thought, text, call = _gemini_parts("stream")
    chunks: list[dict[str, Any]] = [
        {"candidates": [{"content": {"role": "model", "parts": [thought]}, "index": 0}]},
        {"candidates": [{"content": {"role": "model", "parts": [text]}, "index": 0}]},
        {
            "candidates": [
                {"content": {"role": "model", "parts": [call]}, "finishReason": "STOP", "index": 0}
            ],
            "usageMetadata": _gemini_usage(),
        },
    ]
    for chunk in chunks:
        chunk["modelVersion"] = model
        chunk["responseId"] = "resp-gemini-snapshot"
    return [(None, chunk) for chunk in chunks]


# ---------------------------------------------------------------------------
# Ollama native /api/chat
# ---------------------------------------------------------------------------


def _ollama_tool_calls() -> list[dict[str, Any]]:
    return [
        {
            "id": CANNED_TOOL_CALL_ID,
            "function": {"name": CANNED_TOOL_NAME, "arguments": CANNED_TOOL_ARGUMENTS},
        }
    ]


def _ollama_json(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "created_at": "2026-01-01T00:00:00Z",
        "message": {
            "role": "assistant",
            "content": CANNED_CONTENT,
            "thinking": "Ollama thinking text.",
            "tool_calls": _ollama_tool_calls(),
        },
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 1200,
        "eval_count": 340,
    }


def _ollama_stream(model: str) -> list[dict[str, Any]]:
    base = {"model": model, "created_at": "2026-01-01T00:00:00Z", "done": False}
    return [
        {**base, "message": {"role": "assistant", "content": "", "thinking": "Ollama stream "}},
        {**base, "message": {"role": "assistant", "content": "", "thinking": "thinking text."}},
        {**base, "message": {"role": "assistant", "content": CANNED_CONTENT}},
        {
            **base,
            "message": {"role": "assistant", "content": "", "tool_calls": _ollama_tool_calls()},
        },
        {
            **base,
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 1200,
            "eval_count": 340,
        },
    ]
