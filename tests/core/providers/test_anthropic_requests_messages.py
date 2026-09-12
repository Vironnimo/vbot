"""Anthropic requests: messages behavior."""

from __future__ import annotations

from .anthropic_test_support import (
    ANTHROPIC_URL,
    SAMPLE_MESSAGES,
    SAMPLE_MESSAGES_WITH_SYSTEM,
    SUCCESS_RESPONSE,
    ProviderError,
    _strip_cache_control,
    _to_anthropic_user_content_block,
    httpx,
    json,
    pytest,
    respx,
)
from .anthropic_test_support import anthropic_adapter as anthropic_adapter
from .anthropic_test_support import custom_adapter as custom_adapter


class TestSendRequestFormat:
    "Verify that send() translates messages to the correct Anthropic format."

    # send() — request format
    @respx.mock
    @pytest.mark.asyncio
    async def test_send_includes_model_and_messages(self, anthropic_adapter):
        """The request payload contains the model ID and messages."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        assert route.called
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["model"] == "claude-sonnet-4-20250219"
        assert request_body["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": "Hello"}]}
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_maps_user_media_blocks_to_anthropic_image_source(self, anthropic_adapter):
        """Resolved media blocks map to Anthropic image base64 source blocks."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "media",
                        "base64": "iVBORw0KGgoAAAANSUhEUgAA",
                        "media_type": "image/png",
                    }
                ],
            }
        ]

        # Act
        await anthropic_adapter.send(messages, model_id="claude-sonnet-4-20250219")

        # Assert
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["messages"] == [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "iVBORw0KGgoAAAANSUhEUgAA",
                        },
                    }
                ],
            }
        ]

    @pytest.mark.parametrize(
        "invalid_block",
        [
            {"type": "media", "base64": None, "media_type": "image/png"},
            {"type": "media", "base64": "aW1n", "media_type": None},
            {"type": "media", "base64": "aW1n", "media_type": ""},
            {"type": "media"},
        ],
    )
    def test_invalid_media_block_raises_instead_of_raw_passthrough(self, invalid_block):
        """Malformed media blocks must never reach the wire as raw dicts."""
        with pytest.raises(ProviderError):
            _to_anthropic_user_content_block(invalid_block)

    @pytest.mark.parametrize("media_type", ["audio/wav", "audio/ogg", "video/mp4"])
    def test_non_image_media_block_raises_clear_error(self, media_type):
        """Anthropic's wire has no audio/video input; reject instead of mislabeling."""
        block = {"type": "media", "base64": "YXVkaW8=", "media_type": media_type}

        with pytest.raises(ProviderError):
            _to_anthropic_user_content_block(block)

    def test_document_block_maps_to_anthropic_document_part(self):
        """A canonical document block becomes an Anthropic base64 document block."""
        block = {
            "type": "document",
            "base64": "JVBERi0=",
            "media_type": "application/pdf",
            "filename": "report.pdf",
        }

        result = _to_anthropic_user_content_block(block)

        assert result == {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": "JVBERi0=",
            },
        }

    @pytest.mark.parametrize(
        "block",
        [
            {"type": "document", "base64": None, "media_type": "application/pdf"},
            {"type": "document", "base64": "JVBERi0=", "media_type": ""},
        ],
    )
    def test_invalid_document_block_raises(self, block):
        """Malformed document blocks must never reach the wire as raw dicts."""
        with pytest.raises(ProviderError):
            _to_anthropic_user_content_block(block)

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_maps_user_text_blocks_to_anthropic_text_parts(self, anthropic_adapter):
        """Resolved text blocks keep Anthropic text-part wire shape."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "First line."},
                    {"type": "text", "text": "Second line."},
                ],
            }
        ]

        # Act
        await anthropic_adapter.send(messages, model_id="claude-sonnet-4-20250219")

        # Assert
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["messages"] == [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "First line."},
                    {"type": "text", "text": "Second line."},
                ],
            }
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_maps_mixed_user_blocks_in_order(self, anthropic_adapter):
        """Mixed resolved text/media blocks preserve order after conversion."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image:"},
                    {
                        "type": "media",
                        "base64": "dGVzdC1pbWFnZS1ieXRlcw==",
                        "media_type": "image/jpeg",
                    },
                    {"type": "text", "text": "Use one sentence."},
                ],
            }
        ]

        # Act
        await anthropic_adapter.send(messages, model_id="claude-sonnet-4-20250219")

        # Assert
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["messages"] == [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image:"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": "dGVzdC1pbWFnZS1ieXRlcw==",
                        },
                    },
                    {"type": "text", "text": "Use one sentence."},
                ],
            }
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_keeps_string_user_content_behavior(self, anthropic_adapter):
        """String user content keeps the existing single text-block mapping."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        messages = [{"role": "user", "content": "Hello from plain text."}]

        # Act
        await anthropic_adapter.send(messages, model_id="claude-sonnet-4-20250219")

        # Assert
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["messages"] == [
            {
                "role": "user",
                "content": [{"type": "text", "text": "Hello from plain text."}],
            }
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_extracts_system_message(self, anthropic_adapter):
        """System-role messages are extracted to the system field."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await anthropic_adapter.send(
            SAMPLE_MESSAGES_WITH_SYSTEM,
            model_id="claude-sonnet-4-20250219",
        )

        # Assert
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["system"] == [{"type": "text", "text": "You are a helpful assistant."}]
        for msg in request_body["messages"]:
            assert msg["role"] != "system"

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_combines_multiple_system_messages(self, anthropic_adapter):
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        messages = [
            {"role": "system", "content": "Follow the project rules."},
            {"role": "system", "content": "Keep answers concise."},
            {"role": "user", "content": "Hello"},
        ]

        await anthropic_adapter.send(messages, model_id="claude-sonnet-4-20250219")

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["system"] == [
            {"type": "text", "text": "Follow the project rules.\n\nKeep answers concise."}
        ]
        for msg in request_body["messages"]:
            assert msg["role"] != "system"

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_no_system_message(self, anthropic_adapter):
        """When no system message is present, the system field is omitted."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        # Act
        await anthropic_adapter.send(SAMPLE_MESSAGES, model_id="claude-sonnet-4-20250219")

        # Assert
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert "system" not in request_body

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_system_content_blocks(self, anthropic_adapter):
        """System messages with content block arrays are extracted."""
        # Arrange
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        system_blocks = [{"type": "text", "text": "You are a helpful assistant."}]
        messages_with_system_blocks = [
            {"role": "system", "content": system_blocks},
            {"role": "user", "content": "Hello"},
        ]

        # Act
        await anthropic_adapter.send(
            messages_with_system_blocks,
            model_id="claude-sonnet-4-20250219",
        )

        # Assert
        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["system"] == system_blocks
        for msg in request_body["messages"]:
            assert msg["role"] != "system"

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_combines_multiple_system_content_block_messages(
        self,
        anthropic_adapter,
    ):
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        first_blocks = [{"type": "text", "text": "Follow the project rules."}]
        second_blocks = [{"type": "text", "text": "Keep answers concise."}]
        messages = [
            {"role": "system", "content": first_blocks},
            {"role": "system", "content": second_blocks},
            {"role": "user", "content": "Hello"},
        ]

        await anthropic_adapter.send(messages, model_id="claude-sonnet-4-20250219")

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["system"] == [*first_blocks, *second_blocks]
        for msg in request_body["messages"]:
            assert msg["role"] != "system"

    @respx.mock
    @pytest.mark.asyncio
    async def test_send_combines_mixed_system_content_messages(self, anthropic_adapter):
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        blocks = [{"type": "text", "text": "Keep answers concise."}]
        messages = [
            {"role": "system", "content": "Follow the project rules."},
            {"role": "system", "content": blocks},
            {"role": "user", "content": "Hello"},
        ]

        await anthropic_adapter.send(messages, model_id="claude-sonnet-4-20250219")

        request_body = _strip_cache_control(json.loads(route.calls.last.request.content))
        assert request_body["system"] == [
            {"type": "text", "text": "Follow the project rules."},
            *blocks,
        ]
        for msg in request_body["messages"]:
            assert msg["role"] != "system"
