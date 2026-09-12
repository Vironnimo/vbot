"""Anthropic requests: tools behavior."""

from __future__ import annotations

from core.providers.adapter import TOOL_RESULT_CONTENT_BLOCKS_FIELD
from core.providers.tool_schema import render_tool_definitions

from .anthropic_test_support import (
    ANTHROPIC_URL,
    CANONICAL_MESSAGES_WITH_TOOL_LOOP,
    HISTORY_TOOL_DESCRIPTION,
    HISTORY_TOOL_NAME,
    HISTORY_TOOL_PARAMETERS,
    READ_TOOL_DEFINITION,
    SAMPLE_MESSAGES,
    SAMPLE_TOOLS,
    SUCCESS_RESPONSE,
    _strip_cache_control,
    httpx,
    json,
    pytest,
    respx,
)
from .anthropic_test_support import anthropic_adapter as anthropic_adapter
from .anthropic_test_support import custom_adapter as custom_adapter


class TestSendRequestFormat:
    "Verify that send() translates messages to the correct Anthropic format."

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_tool_use_content_blocks(self, anthropic_adapter):
        """Tool use content blocks are passed through correctly."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        tool_messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "What's the weather?"}],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me check."},
                    {
                        "type": "tool_use",
                        "id": "toolu_01A",
                        "name": "get_weather",
                        "input": {"location": "San Francisco"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_01A",
                        "content": "72°F and sunny",
                    }
                ],
            },
        ]

        # Act
        await anthropic_adapter.send(tool_messages, model_id="claude-sonnet-4-20250219")

        # Assert
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert len(request_body["messages"]) == 3
        assistant_msg = request_body["messages"][1]
        assert assistant_msg["role"] == "assistant"
        assert any(block["type"] == "tool_use" for block in assistant_msg["content"])
        user_msg = request_body["messages"][2]
        assert user_msg["role"] == "user"
        assert any(block["type"] == "tool_result" for block in user_msg["content"])

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_renders_image_inside_native_tool_result(self, anthropic_adapter):
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        messages = [
            {
                "role": "tool",
                "tool_call_id": "toolu_image",
                "content": '{"ok":true}',
                TOOL_RESULT_CONTENT_BLOCKS_FIELD: [
                    {
                        "type": "media",
                        "base64": "aW1hZ2U=",
                        "media_type": "image/png",
                    },
                    {"type": "text", "text": "[Image path: C:/diagram.png]"},
                ],
            }
        ]

        await anthropic_adapter.send(
            messages,
            model_id="claude-sonnet-4-20250219",
        )

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        tool_result = request_body["messages"][0]["content"][0]
        assert tool_result == {
            "type": "tool_result",
            "tool_use_id": "toolu_image",
            "content": [
                {"type": "text", "text": '{"ok":true}'},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "aW1hZ2U=",
                    },
                },
                {"type": "text", "text": "[Image path: C:/diagram.png]"},
            ],
        }

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_endpoint_is_messages(self, anthropic_adapter):
        """The request goes to /messages, not /chat/completions."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        assert route.called
        request = route.calls.last.request
        assert "/messages" in str(request.url)

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_maps_canonical_messages_tools_and_reasoning(self, anthropic_adapter):
        """Canonical messages, tool definitions, and effort map to Anthropic wire format."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        await anthropic_adapter.send(
            CANONICAL_MESSAGES_WITH_TOOL_LOOP,
            model_id="claude-sonnet-4-20250219",
            tools=SAMPLE_TOOLS,
            thinking_effort="high",
        )

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["system"] == [{"type": "text", "text": "You are helpful."}]
        assert request_body["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": "Weather?"}]},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "Need weather.",
                        "signature": "opaque-current-turn",
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_abc",
                        "name": "get_weather",
                        "input": {"city": "Berlin"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_abc",
                        "content": '{"temp":22}',
                    }
                ],
            },
        ]
        assert request_body["tools"] == [
            {
                "name": "get_weather",
                "description": "Get current weather",
                "input_schema": SAMPLE_TOOLS[0]["parameters"],
            }
        ]
        assert request_body["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert request_body["output_config"] == {"effort": "high"}

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_maps_read_definition_to_input_schema(self, anthropic_adapter):
        """The compact read definition maps to Anthropic input_schema tools."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        await anthropic_adapter.send(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4-20250219",
            tools=[READ_TOOL_DEFINITION],
        )

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        rendered = render_tool_definitions(
            [READ_TOOL_DEFINITION],
            profile="omit_strict",
        )[0]
        assert request_body["tools"] == [
            {
                "name": "read",
                "description": READ_TOOL_DEFINITION["description"],
                "input_schema": rendered["parameters"],
            }
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_maps_history_definition_to_input_schema(self, anthropic_adapter):
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        await anthropic_adapter.send(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4-20250219",
            tools=[
                {
                    "name": HISTORY_TOOL_NAME,
                    "description": HISTORY_TOOL_DESCRIPTION,
                    "parameters": HISTORY_TOOL_PARAMETERS,
                }
            ],
        )

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        definition = {
            "name": HISTORY_TOOL_NAME,
            "description": HISTORY_TOOL_DESCRIPTION,
            "parameters": HISTORY_TOOL_PARAMETERS,
        }
        rendered = render_tool_definitions([definition], profile="omit_strict")[0]
        assert request_body["tools"] == [
            {
                "name": HISTORY_TOOL_NAME,
                "description": HISTORY_TOOL_DESCRIPTION,
                "input_schema": rendered["parameters"],
            }
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_preserves_tool_input_schema(self, anthropic_adapter):
        """A nullable-union schema keeps its canonical meaning on the wire."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        tool = {
            "name": "search",
            "description": "Search records",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "tag": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["query"],
            },
        }

        await anthropic_adapter.send(
            SAMPLE_MESSAGES,
            model_id="claude-sonnet-4-20250219",
            tools=[tool],
        )

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["tools"][0]["input_schema"]["properties"]["tag"] == {
            "anyOf": [{"type": "string"}, {"type": "null"}]
        }

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_groups_multiple_tool_results_in_one_user_message(self, anthropic_adapter):
        """Consecutive canonical tool messages become one Anthropic user message."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        messages = [
            {"role": "user", "content": "Check two cities."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "toolu_a", "name": "get_weather", "arguments": {"city": "Berlin"}},
                    {"id": "toolu_b", "name": "get_weather", "arguments": {"city": "Paris"}},
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_a",
                "name": "get_weather",
                "content": '{"temp":22}',
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_b",
                "name": "get_weather",
                "content": '{"temp":19}',
            },
        ]

        await anthropic_adapter.send(messages, model_id="claude-sonnet-4-20250219")

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["messages"][2] == {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_a", "content": '{"temp":22}'},
                {"type": "tool_result", "tool_use_id": "toolu_b", "content": '{"temp":19}'},
            ],
        }

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_marks_only_failed_canonical_tool_results_as_native_errors(
        self,
        anthropic_adapter,
    ):
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        success = json.dumps(
            {"ok": True, "error": None, "data": {"value": 1}, "artifacts": []},
            separators=(",", ":"),
        )
        failure = json.dumps(
            {
                "ok": False,
                "error": {"code": "lookup_failed", "message": "Lookup failed."},
                "data": None,
                "artifacts": [],
            },
            separators=(",", ":"),
        )
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "toolu_ok", "name": "lookup", "arguments": {}},
                    {"id": "toolu_failed", "name": "lookup", "arguments": {}},
                ],
            },
            {"role": "tool", "tool_call_id": "toolu_ok", "content": success},
            {"role": "tool", "tool_call_id": "toolu_failed", "content": failure},
        ]

        await anthropic_adapter.send(messages, model_id="claude-sonnet-4-20250219")

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        result_blocks = request_body["messages"][1]["content"]
        assert result_blocks == [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_ok",
                "content": success,
            },
            {
                "type": "tool_result",
                "tool_use_id": "toolu_failed",
                "content": failure,
                "is_error": True,
            },
        ]
