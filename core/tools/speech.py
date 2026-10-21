"""Built-in text-to-speech tool."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

from core.model_tasks import (
    SpeechConfigurationError,
    SpeechError,
    SpeechExecutionError,
    SpeechOutcomeUnknownError,
    SpeechUnsupportedTargetError,
    TaskUsageContext,
)
from core.model_tasks.speech_playback import SpeechPlayback
from core.model_tasks.speech_types import SpeechAudioChunk
from core.storage.layout import DataDirectoryLayout
from core.storage.temp_files import artifact_owner
from core.tools._media_failures import provider_failure_message, unavailable_message
from core.tools.call_syntax import SpellingAliases, normalize_call_arguments
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

GENERATE_SPEECH_TOOL_NAME = "generate_speech"
GENERATE_SPEECH_TOOL_DESCRIPTION = (
    "Convert text to spoken audio. The web chat plays the returned audio artifact automatically."
)
GENERATE_SPEECH_TOOL_PARAMETERS: JsonObject = {
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

_GENERATE_SPEECH_CONTRACT = compile_tool_contract(
    name=GENERATE_SPEECH_TOOL_NAME,
    input_schema=GENERATE_SPEECH_TOOL_PARAMETERS,
    require_closed_input=False,
)
# Names other speech Tools use for the text to speak (OpenAI's speech API uses input).
_FIELD_ALIASES = SpellingAliases({"text": ("input", "content", "transcript")})
_SETTING = "Text to speech"
# The temporary-file category of the audio a restricted Session receives.
_SESSION_CATEGORY = "speech"


def _normalize_generate_speech_arguments(arguments: Any) -> Any:
    return normalize_call_arguments(
        _GENERATE_SPEECH_CONTRACT, arguments, field_aliases=_FIELD_ALIASES
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


def make_generate_speech_handler(speech_service: Any):
    """Create a text-to-speech tool handler bound to the runtime speech service."""

    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:

        text = arguments.get("text")
        if not isinstance(text, str) or not text.strip():
            return tool_failure(
                "invalid_arguments",
                'Pass the words to speak as text, for example {"text": "Your build finished."}.',
                retryable=False,
            )

        playback: SpeechPlayback | None = None
        announced = False

        async def publish_audio(chunk: SpeechAudioChunk) -> None:
            nonlocal playback, announced
            if playback is None:
                playback = speech_service.playbacks.create()
            if playback is None:
                return
            await playback.append(chunk.audio, chunk.sample_rate_hz)
            if not announced:
                announced = True
                await context.emit(
                    "speech_playback",
                    {"tool_call_id": context.tool_call_id, "url": playback.url},
                )

        try:
            artifact = await speech_service.synthesize_artifact(
                text,
                on_audio=publish_audio,
                usage_context=TaskUsageContext(
                    agent_id=context.agent_id,
                    project_id=context.project_id,
                    session_id=context.session_id,
                    run_id=context.run_id,
                    owner_name=context.execution_owner.extension
                    if context.execution_owner
                    else None,
                    group_id=context.execution_owner.group_id if context.execution_owner else None,
                ),
            )
        except SpeechError as exc:
            if playback is not None:
                playback.finish("failed")
            return _speech_failure(exc)
        except BaseException as exc:
            if playback is not None:
                playback.finish(
                    "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
                )
            raise
        else:
            if playback is not None:
                playback.finish()

        # The model-facing data carries the audio file's absolute path so the agent
        # can deliver it outside the web chat (e.g. channel_send); the UI-facing
        # artifacts payload stays path-free — the WebUI renders from `url`.
        artifact_payload = artifact.to_dict()
        audio = Path(artifact.file_path)
        if context.permissions.restricted:
            # The shared audio store is no place a restricted Session may read.
            owner = artifact_owner(context.project_id, context.agent_id, context.session_id)
            audio = await asyncio.to_thread(_session_audio, audio, context.data_root, owner)
        file_path = model_path(audio)
        return tool_success(
            {"artifact": {**artifact_payload, "path": file_path}},
            artifacts=[artifact_payload],
        )

    return handler


def _session_audio(audio: Path, data_root: Path, owner: str) -> Path:
    """Blocking: *audio* in the Session's own temporary-file folder, linked or copied.

    The folder keeps the file for the ``speech`` retention; the shared audio
    store keeps its own. Where neither a link nor a copy can be made, the
    original path is returned.
    """
    target = DataDirectoryLayout(data_root).temporary / _SESSION_CATEGORY / owner / audio.name
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            try:
                os.link(audio, target)
            except OSError:
                shutil.copyfile(audio, target)
    except OSError:
        return audio
    return target


def register_generate_speech_tool(registry: ToolRegistry, speech_service: Any) -> None:
    """Register the text-to-speech tool with a vBot tool registry."""

    registry.register(
        GENERATE_SPEECH_TOOL_NAME,
        GENERATE_SPEECH_TOOL_DESCRIPTION,
        GENERATE_SPEECH_TOOL_PARAMETERS,
        make_generate_speech_handler(speech_service),
        summary="Convert text to spoken audio.",
        family="media",
        open_input_schema=True,
        argument_normalizer=_normalize_generate_speech_arguments,
        result_schema={"type": "object", "required": ["artifact"]},
        display=ToolDisplay(
            primary_candidates=(ToolDisplayField("text", kind="text", quote=True),)
        ),
    )
