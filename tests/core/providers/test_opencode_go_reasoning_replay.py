"""Opencode go: reasoning replay behavior."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from core.providers.opencode_go import (
    OpenCodeGoAdapter,
)
from tests.core.providers.opencode_go_helpers import (
    ANTHROPIC_MESSAGES_MODELS,
    OPENCODE_GO_MESSAGES_URL,
    OPENCODE_GO_URL,
)
from tests.core.providers.opencode_go_helpers import (
    opencode_go_adapter as opencode_go_adapter,
)
from tests.core.providers.opencode_go_helpers import (
    opencode_go_config as opencode_go_config,
)


class TestOpenCodeGoAdapterMinimaxRouting:
    @respx.mock
    @pytest.mark.asyncio
    async def test_minimax_send_replays_reasoning_meta_for_all_assistants(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        captured_payload: dict[str, Any] = {}

        def _capture_messages_request(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                },
            )

        respx.post(OPENCODE_GO_MESSAGES_URL).mock(side_effect=_capture_messages_request)

        await opencode_go_adapter.send(
            [
                {"role": "user", "content": "First"},
                {
                    "role": "assistant",
                    "content": "Older assistant",
                    "reasoning": "old thinking",
                    "reasoning_meta": {
                        "content_blocks": [
                            {"type": "thinking", "thinking": "old thinking", "signature": "sig-old"}
                        ]
                    },
                    "tool_calls": [{"id": "call_old", "name": "old_tool", "arguments": {}}],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_old",
                    "name": "old_tool",
                    "content": json.dumps({"ok": True}),
                },
                {
                    "role": "assistant",
                    "content": "Latest assistant",
                    "reasoning": "latest thinking",
                    "reasoning_meta": {
                        "content_blocks": [
                            {
                                "type": "thinking",
                                "thinking": "latest thinking",
                                "signature": "sig-latest",
                            }
                        ]
                    },
                    "tool_calls": [{"id": "call_latest", "name": "latest_tool", "arguments": {}}],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_latest",
                    "name": "latest_tool",
                    "content": json.dumps({"ok": True}),
                },
            ],
            model_id="minimax-m2.7",
        )

        assistant_messages = [
            message
            for message in captured_payload.get("messages", [])
            if isinstance(message, dict) and message.get("role") == "assistant"
        ]
        assert len(assistant_messages) == 2
        older_blocks = assistant_messages[0].get("content", [])
        latest_blocks = assistant_messages[1].get("content", [])
        assert isinstance(older_blocks, list)
        assert isinstance(latest_blocks, list)
        older_thinking = [
            block
            for block in older_blocks
            if isinstance(block, dict) and block.get("type") == "thinking"
        ]
        latest_thinking = [
            block
            for block in latest_blocks
            if isinstance(block, dict) and block.get("type") == "thinking"
        ]
        assert older_thinking == [
            {"type": "thinking", "thinking": "old thinking", "signature": "sig-old"}
        ]
        assert latest_thinking == [
            {"type": "thinking", "thinking": "latest thinking", "signature": "sig-latest"}
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_minimax_send_passes_assistant_reasoning_through_unchanged(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        captured_payload: dict[str, Any] = {}

        def _capture_messages_request(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                },
            )

        respx.post(OPENCODE_GO_MESSAGES_URL).mock(side_effect=_capture_messages_request)

        await opencode_go_adapter.send(
            [
                {"role": "user", "content": "First"},
                {
                    "role": "assistant",
                    "content": "Older assistant",
                    "reasoning": "old thinking",
                    "reasoning_meta": {
                        "content_blocks": [
                            {"type": "thinking", "thinking": "old thinking", "signature": "sig-old"}
                        ]
                    },
                    "tool_calls": [{"id": "call_old", "name": "old_tool", "arguments": {}}],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_old",
                    "name": "old_tool",
                    "content": json.dumps({"ok": True}),
                },
                {
                    "role": "assistant",
                    "content": "Latest assistant",
                    "tool_calls": [{"id": "call_latest", "name": "latest_tool", "arguments": {}}],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_latest",
                    "name": "latest_tool",
                    "content": json.dumps({"ok": True}),
                },
            ],
            model_id="minimax-m2.7",
        )

        assistant_messages = [
            message
            for message in captured_payload.get("messages", [])
            if isinstance(message, dict) and message.get("role") == "assistant"
        ]
        assert len(assistant_messages) == 2
        older_blocks = assistant_messages[0].get("content", [])
        latest_blocks = assistant_messages[1].get("content", [])
        assert isinstance(older_blocks, list)
        assert isinstance(latest_blocks, list)
        assert any(
            isinstance(block, dict) and block.get("type") == "thinking" for block in older_blocks
        )
        assert not any(
            isinstance(block, dict) and block.get("type") == "thinking" for block in latest_blocks
        )
        assert any(
            isinstance(block, dict) and block.get("type") == "tool_use" for block in latest_blocks
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_non_minimax_send_uses_openai_path(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        captured_payload: dict[str, Any] = {}

        def _capture_chat_request(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )

        chat_route = respx.post(OPENCODE_GO_URL).mock(side_effect=_capture_chat_request)
        messages_route = respx.post(OPENCODE_GO_MESSAGES_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "unused"}],
                    "stop_reason": "end_turn",
                },
            )
        )

        await opencode_go_adapter.send(
            [
                {"role": "user", "content": "First"},
                {
                    "role": "assistant",
                    "content": "Older assistant",
                    "reasoning": "old thinking",
                    "reasoning_meta": {
                        "reasoning_details": [{"trace": "old"}],
                    },
                    "tool_calls": None,
                },
                {"role": "user", "content": "Second"},
                {
                    "role": "assistant",
                    "content": "Latest assistant",
                    "reasoning": "latest thinking",
                    "reasoning_meta": {
                        "reasoning_details": [{"trace": "latest"}],
                    },
                    "tool_calls": None,
                },
                {"role": "user", "content": "Continue"},
            ],
            model_id="deepseek/deepseek-v4-flash",
        )

        assert chat_route.called
        assert not messages_route.called
        assistant_messages = [
            message
            for message in captured_payload.get("messages", [])
            if isinstance(message, dict) and message.get("role") == "assistant"
        ]
        assert len(assistant_messages) == 2
        assert assistant_messages[0]["reasoning_content"] == "old thinking"
        assert "reasoning_details" not in assistant_messages[0]
        assert assistant_messages[1]["reasoning_content"] == "latest thinking"
        assert "reasoning_details" not in assistant_messages[1]

    @pytest.mark.parametrize("model_id", ANTHROPIC_MESSAGES_MODELS)
    @respx.mock
    @pytest.mark.asyncio
    async def test_messages_model_stream_uses_anthropic_path(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
        model_id: str,
    ) -> None:
        messages_route = respx.post(OPENCODE_GO_MESSAGES_URL).mock(
            return_value=httpx.Response(
                200,
                text='event: message_stop\ndata: {"type":"message_stop"}\n\n',
                headers={"content-type": "text/event-stream"},
            )
        )
        chat_route = respx.post(OPENCODE_GO_URL).mock(
            return_value=httpx.Response(
                200,
                text="data: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )
        )

        chunks: list[dict[str, str]] = []
        async for chunk in opencode_go_adapter.stream(
            [{"role": "user", "content": "hello"}],
            model_id=model_id,
        ):
            chunks.append(chunk)

        assert chunks == []
        assert messages_route.called
        assert not chat_route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_non_minimax_stream_uses_openai_path(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        chat_route = respx.post(OPENCODE_GO_URL).mock(
            return_value=httpx.Response(
                200,
                text="data: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )
        )
        messages_route = respx.post(OPENCODE_GO_MESSAGES_URL).mock(
            return_value=httpx.Response(
                200,
                text='event: message_stop\ndata: {"type":"message_stop"}\n\n',
                headers={"content-type": "text/event-stream"},
            )
        )

        chunks: list[dict[str, str]] = []
        async for chunk in opencode_go_adapter.stream(
            [{"role": "user", "content": "hello"}],
            model_id="deepseek/deepseek-v4-flash",
        ):
            chunks.append(chunk)

        assert chunks == []
        assert chat_route.called
        assert not messages_route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_minimax_stream_replays_reasoning_meta_for_all_assistants(
        self,
        opencode_go_adapter: OpenCodeGoAdapter,
    ) -> None:
        captured_payload: dict[str, Any] = {}

        def _capture_messages_request(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                text='event: message_stop\ndata: {"type":"message_stop"}\n\n',
                headers={"content-type": "text/event-stream"},
            )

        respx.post(OPENCODE_GO_MESSAGES_URL).mock(side_effect=_capture_messages_request)

        chunks: list[dict[str, str]] = []
        async for chunk in opencode_go_adapter.stream(
            [
                {"role": "user", "content": "First"},
                {
                    "role": "assistant",
                    "content": "Older assistant",
                    "reasoning": "old thinking",
                    "reasoning_meta": {
                        "content_blocks": [
                            {"type": "thinking", "thinking": "old thinking", "signature": "sig-old"}
                        ]
                    },
                    "tool_calls": [{"id": "call_old", "name": "old_tool", "arguments": {}}],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_old",
                    "name": "old_tool",
                    "content": json.dumps({"ok": True}),
                },
                {
                    "role": "assistant",
                    "content": "Latest assistant",
                    "reasoning": "latest thinking",
                    "reasoning_meta": {
                        "content_blocks": [
                            {
                                "type": "thinking",
                                "thinking": "latest thinking",
                                "signature": "sig-latest",
                            }
                        ]
                    },
                    "tool_calls": [{"id": "call_latest", "name": "latest_tool", "arguments": {}}],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_latest",
                    "name": "latest_tool",
                    "content": json.dumps({"ok": True}),
                },
            ],
            model_id="minimax-m2.7",
        ):
            chunks.append(chunk)

        assert chunks == []
        assistant_messages = [
            message
            for message in captured_payload.get("messages", [])
            if isinstance(message, dict) and message.get("role") == "assistant"
        ]
        assert len(assistant_messages) == 2
        older_blocks = assistant_messages[0].get("content", [])
        latest_blocks = assistant_messages[1].get("content", [])
        assert isinstance(older_blocks, list)
        assert isinstance(latest_blocks, list)
        assert any(
            isinstance(block, dict) and block.get("type") == "thinking" for block in older_blocks
        )
        assert any(
            isinstance(block, dict) and block.get("type") == "thinking" for block in latest_blocks
        )
