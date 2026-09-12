"""Mistral: responses behavior."""

from __future__ import annotations

from core.providers.mistral import MistralAdapter
from tests.core.providers.mistral_helpers import (
    mistral_adapter as mistral_adapter,
)
from tests.core.providers.mistral_helpers import (
    mistral_config as mistral_config,
)


def test_normalize_response_extracts_message_thinking_as_reasoning(
    mistral_adapter: MistralAdapter,
) -> None:
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Final answer",
                    "thinking": "Reasoning trace",
                }
            }
        ]
    }

    normalized = mistral_adapter.normalize_response(response)

    assert normalized["reasoning"] == "Reasoning trace"


def test_normalize_response_typed_list_extracts_text_and_reasoning(
    mistral_adapter: MistralAdapter,
) -> None:
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "ThinkA"},
                        {"type": "text", "text": "AnswerA"},
                    ],
                }
            }
        ]
    }

    normalized = mistral_adapter.normalize_response(response)

    assert normalized["content"] == "AnswerA"
    assert normalized["reasoning"] == "ThinkA"
    assert (
        normalized["reasoning_meta"]["content_chunks"]
        == response["choices"][0]["message"]["content"]
    )


def test_normalize_response_nested_thinking_chunks_flatten_to_reasoning(
    mistral_adapter: MistralAdapter,
) -> None:
    """Current reasoning models nest ``thinking`` as a list of text chunks (live shape)."""
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": [
                                {"type": "text", "text": "Step one. "},
                                {"type": "text", "text": "Step two."},
                            ],
                            "closed": True,
                        },
                        {"type": "text", "text": "391"},
                    ],
                }
            }
        ]
    }

    normalized = mistral_adapter.normalize_response(response)

    assert normalized["content"] == "391"
    assert normalized["reasoning"] == "Step one. Step two."
    assert (
        normalized["reasoning_meta"]["content_chunks"]
        == response["choices"][0]["message"]["content"]
    )


def test_format_assistant_message_replays_reasoning_as_think_chunk(
    mistral_adapter: MistralAdapter,
) -> None:
    """Replayed reasoning renders back into Mistral's content chunk-list shape."""
    wire = mistral_adapter._format_assistant_message(
        {"role": "assistant", "content": "391", "reasoning": "17*23 = 391"}
    )

    assert wire["content"] == [
        {"type": "thinking", "thinking": [{"type": "text", "text": "17*23 = 391"}]},
        {"type": "text", "text": "391"},
    ]


def test_format_assistant_message_replays_original_content_chunks_verbatim(
    mistral_adapter: MistralAdapter,
) -> None:
    chunks = [
        {
            "type": "thinking",
            "thinking": [{"type": "text", "text": "Step one."}],
            "closed": True,
        },
        {"type": "text", "text": "Answer"},
    ]

    wire = mistral_adapter._format_assistant_message(
        {
            "role": "assistant",
            "content": "A reconstructed answer",
            "reasoning": "A flattened trace",
            "reasoning_meta": {"content_chunks": chunks},
        }
    )

    assert wire["content"] == chunks


def test_format_assistant_message_without_reasoning_keeps_plain_content(
    mistral_adapter: MistralAdapter,
) -> None:
    """No reasoning → generic plain-string content is kept unchanged."""
    wire = mistral_adapter._format_assistant_message({"role": "assistant", "content": "plain"})

    assert wire["content"] == "plain"


def test_normalize_response_typed_list_multiple_text_blocks_concatenated(
    mistral_adapter: MistralAdapter,
) -> None:
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "A"},
                        {"type": "text", "text": "B"},
                    ],
                }
            }
        ]
    }

    normalized = mistral_adapter.normalize_response(response)

    assert normalized["content"] == "AB"


def test_normalize_response_string_content_delegates_to_base(
    mistral_adapter: MistralAdapter,
) -> None:
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "plain string",
                    "thinking": "Reasoning trace",
                }
            }
        ]
    }

    normalized = mistral_adapter.normalize_response(response)

    assert normalized["content"] == "plain string"
    assert normalized["reasoning"] == "Reasoning trace"
