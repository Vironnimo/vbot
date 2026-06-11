"""Built-in text-to-speech tool."""

from __future__ import annotations

from typing import Any

from core.model_tasks import SpeechError
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)

TEXT_TO_SPEECH_TOOL_NAME = "text_to_speech"
TEXT_TO_SPEECH_TOOL_DESCRIPTION = (
    "Create a speech audio artifact from text using the configured text-to-speech model."
)
TEXT_TO_SPEECH_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "minLength": 1,
            "description": "The text to synthesize.",
        }
    },
    "required": ["text"],
    "additionalProperties": False,
}


def make_text_to_speech_handler(speech_service: Any):
    """Create a text-to-speech tool handler bound to the runtime speech service."""

    async def handler(_context: ToolContext, arguments: JsonObject) -> JsonObject:
        text = arguments.get("text")
        if not isinstance(text, str) or not text.strip():
            return tool_failure("invalid_arguments", "text must be a non-empty string")

        try:
            artifact = await speech_service.synthesize_artifact(text)
        except SpeechError as exc:
            return tool_failure("speech_error", str(exc))

        artifact_payload = artifact.to_dict()
        return tool_success(
            {
                "message": "Speech artifact created.",
                "artifact": artifact_payload,
            },
            artifacts=[artifact_payload],
        )

    return handler


def register_text_to_speech_tool(registry: ToolRegistry, speech_service: Any) -> None:
    """Register the text-to-speech tool with a vBot tool registry."""

    registry.register(
        TEXT_TO_SPEECH_TOOL_NAME,
        TEXT_TO_SPEECH_TOOL_DESCRIPTION,
        TEXT_TO_SPEECH_TOOL_PARAMETERS,
        make_text_to_speech_handler(speech_service),
        display=ToolDisplay(summary_fields=("text",)),
    )
