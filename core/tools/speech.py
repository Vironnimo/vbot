"""Built-in text-to-speech tool."""

from __future__ import annotations

from typing import Any

from core.model_tasks import (
    SpeechConfigurationError,
    SpeechError,
    SpeechExecutionError,
    SpeechOutcomeUnknownError,
    SpeechUnsupportedTargetError,
)
from core.tools._argument_repair import normalize_call_arguments
from core.tools._media_failures import provider_failure_message, unavailable_message
from core.tools._spelling_aliases import SpellingAliases
from core.tools.contracts import compile_tool_contract
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayField,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.utils.paths import model_path

TEXT_TO_SPEECH_TOOL_NAME = "text_to_speech"
TEXT_TO_SPEECH_TOOL_DESCRIPTION = (
    "Convert text to spoken audio using the configured model. The web chat plays the "
    "returned audio artifact automatically."
)
TEXT_TO_SPEECH_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "minLength": 1,
            "description": "Text to speak.",
        }
    },
    "required": ["text"],
}

_TEXT_TO_SPEECH_CONTRACT = compile_tool_contract(
    name=TEXT_TO_SPEECH_TOOL_NAME,
    input_schema=TEXT_TO_SPEECH_TOOL_PARAMETERS,
    require_closed_input=False,
)
# Names other speech Tools use for the text to speak (OpenAI's speech API uses input).
_FIELD_ALIASES = SpellingAliases({"text": ("input", "content", "transcript")})
_SETTING = "Text to speech"


def _normalize_text_to_speech_arguments(arguments: Any) -> Any:
    return normalize_call_arguments(
        _TEXT_TO_SPEECH_CONTRACT, arguments, field_aliases=_FIELD_ALIASES
    )


def _speech_failure(error: SpeechError) -> JsonObject:
    """Project an expected speech failure with wording the Agent can act on."""
    if isinstance(error, SpeechOutcomeUnknownError):
        return tool_failure(error.code, str(error), retryable=False)
    message = str(error)
    if isinstance(error, SpeechExecutionError):
        message = provider_failure_message(error, task="text-to-speech", setting=_SETTING)
    elif isinstance(error, (SpeechConfigurationError, SpeechUnsupportedTargetError)):
        message = unavailable_message(error, setting=_SETTING)
    return tool_failure("speech_error", message, retryable=bool(getattr(error, "retryable", False)))


def make_text_to_speech_handler(speech_service: Any):
    """Create a text-to-speech tool handler bound to the runtime speech service."""

    async def handler(_context: ToolContext, arguments: JsonObject) -> JsonObject:

        text = arguments.get("text")
        if not isinstance(text, str) or not text.strip():
            return tool_failure(
                "invalid_arguments",
                'Pass the words to speak as text, for example {"text": "Your build finished."}.',
                retryable=False,
            )

        try:
            artifact = await speech_service.synthesize_artifact(text)
        except SpeechError as exc:
            return _speech_failure(exc)

        # The model-facing data carries the audio file's absolute path so the agent
        # can deliver it outside the web chat (e.g. channel_send); the UI-facing
        # artifacts payload stays path-free — the WebUI renders from `url`.
        artifact_payload = artifact.to_dict()
        file_path = model_path(artifact.file_path)
        return tool_success(
            {"artifact": {**artifact_payload, "path": file_path}},
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
        family="media",
        open_input_schema=True,
        argument_normalizer=_normalize_text_to_speech_arguments,
        result_schema={"type": "object", "required": ["artifact"]},
        display=ToolDisplay(
            primary_candidates=(ToolDisplayField("text", kind="text", quote=True),)
        ),
    )
