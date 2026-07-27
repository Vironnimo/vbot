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
_TEXT_TO_SPEECH_ARGUMENTS = frozenset({"text"})
TEXT_TO_SPEECH_TOOL_DESCRIPTION = (
    "Convert text to spoken audio using the configured text-to-speech model. "
    "In the web chat the audio plays automatically. The result includes the "
    "audio file's path — to deliver the audio anywhere else (e.g. a channel "
    "conversation), send that file, e.g. via channel_send."
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
        unknown_arguments = set(arguments) - _TEXT_TO_SPEECH_ARGUMENTS
        if unknown_arguments:
            names = ", ".join(sorted(unknown_arguments))
            return tool_failure("invalid_arguments", f"Unknown argument(s): {names}")

        text = arguments.get("text")
        if not isinstance(text, str) or not text.strip():
            return tool_failure("invalid_arguments", "text must be a non-empty string")

        try:
            artifact = await speech_service.synthesize_artifact(text)
        except SpeechError as exc:
            return tool_failure("speech_error", str(exc))

        # The model-facing data carries the audio file's absolute path so the agent
        # can deliver it outside the web chat (e.g. channel_send); the UI-facing
        # artifacts payload stays path-free — the WebUI renders from `url`.
        artifact_payload = artifact.to_dict()
        file_path = str(artifact.file_path)
        return tool_success(
            {
                "message": (
                    f"Speech audio created at {file_path}. It plays automatically "
                    "in the web chat; to deliver it elsewhere, send this file "
                    "(e.g. via channel_send)."
                ),
                "artifact": {**artifact_payload, "path": file_path},
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
        result_schema={"type": "object", "required": ["message", "artifact"]},
        display=ToolDisplay(summary_fields=("text",)),
    )
