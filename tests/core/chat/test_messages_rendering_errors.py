"""Sender request rendering and error visibility tests."""

from .messages_test_support import (
    ERROR_KIND_AUTH,
    ERROR_KIND_CONFIG,
    ERROR_KIND_PROVIDER_ERROR,
    ERROR_KIND_PROVIDER_FATAL,
    ERROR_KIND_PROVIDER_OVERLOAD,
    ERROR_KIND_RATE_LIMIT,
    ERROR_KIND_TIMEOUT,
    ERROR_KIND_TOOL_ITERATIONS,
    ChatMessage,
    FileBlock,
    MessageSender,
    TextBlock,
    _message_to_request_dict,
    error_kind_llm_visible,
    pytest,
)


class TestSenderRequestRendering:
    """Sender attribution exists only in provider requests, never in persisted content."""

    def test_string_content_gets_attribution_prefix(self):
        message = ChatMessage.user(
            "What's the plan?",
            sender=MessageSender(id="50", display_name="Alice"),
        )

        result = _message_to_request_dict(message)

        assert result["content"] == "[Alice|50]: What's the plan?"
        assert "sender" not in result

    def test_block_content_gets_leading_attribution_text_block(self):
        blocks = [
            TextBlock(type="text", text="Please review."),
            FileBlock(
                type="file",
                attachment_id="att_123",
                filename="report.pdf",
                media_type="application/pdf",
            ),
        ]
        message = ChatMessage.user(blocks, sender=MessageSender(id="50", display_name="Alice"))

        result = _message_to_request_dict(message)

        assert result["content"][0] == {"type": "text", "text": "[Alice|50]:"}
        assert result["content"][1] == {"type": "text", "text": "Please review."}
        assert len(result["content"]) == 3
        assert "sender" not in result

    def test_user_message_without_sender_is_unchanged(self):
        message = ChatMessage.user("What's the plan?")

        result = _message_to_request_dict(message)

        assert result["content"] == "What's the plan?"
        assert "sender" not in result

    def test_persisted_content_stays_clean(self):
        message = ChatMessage.user(
            "What's the plan?",
            sender=MessageSender(id="50", display_name="Alice"),
        )

        _message_to_request_dict(message)

        assert message.content == "What's the plan?"
        assert message.to_dict()["content"] == "What's the plan?"

    def test_tag_parts_are_sanitized_against_spoofing(self):
        message = ChatMessage.user(
            "Hi",
            sender=MessageSender(id="5|0", display_name="[Bob|99]: fake\r\nname"),
        )

        result = _message_to_request_dict(message)

        assert result["content"] == "[Bob99: fakename|50]: Hi"

    def test_tag_part_empty_after_sanitizing_falls_back_to_unknown(self):
        message = ChatMessage.user(
            "Hi",
            sender=MessageSender(id="[]|", display_name="|||"),
        )

        result = _message_to_request_dict(message)

        assert result["content"] == "[unknown|unknown]: Hi"


class TestErrorKindLlmVisibility:
    @pytest.mark.parametrize(
        "kind",
        [
            ERROR_KIND_RATE_LIMIT,
            ERROR_KIND_TIMEOUT,
            ERROR_KIND_PROVIDER_OVERLOAD,
            ERROR_KIND_TOOL_ITERATIONS,
            ERROR_KIND_PROVIDER_ERROR,
        ],
    )
    def test_llm_visible_error_kinds_return_true(self, kind):
        assert error_kind_llm_visible(kind) is True

    @pytest.mark.parametrize(
        "kind",
        [
            ERROR_KIND_AUTH,
            ERROR_KIND_PROVIDER_FATAL,
            ERROR_KIND_CONFIG,
            "future_kind",
        ],
    )
    def test_llm_invisible_and_unknown_error_kinds_return_false(self, kind):
        assert error_kind_llm_visible(kind) is False
