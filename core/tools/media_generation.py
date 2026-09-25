"""Built-in Video and Music generation Tools."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from core.model_tasks import (
    MusicError,
    MusicExecutionError,
    MusicOutcomeUnknownError,
    TaskUsageContext,
    VideoError,
    VideoExecutionError,
    VideoOutcomeUnknownError,
)
from core.tools._image_inputs import (
    UnusableImageError,
    normalize_generate_music_arguments,
    normalize_generate_video_arguments,
    resolve_local_image,
    resolve_local_images,
)
from core.tools._media_failures import provider_failure_message
from core.tools.arguments import optional_bool, optional_int, optional_string
from core.tools.contracts import compile_tool_contract
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDefinitionProfile,
    ToolDefinitionProfileContext,
    ToolDisplay,
    ToolDisplayField,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.utils.paths import model_path

GENERATE_VIDEO_TOOL_NAME = "generate_video"
GENERATE_MUSIC_TOOL_NAME = "generate_music"
_VIDEO_DIRECTORY_NAME = "video-gen"
_MUSIC_DIRECTORY_NAME = "music-gen"

GENERATE_VIDEO_TEXT_ONLY_DESCRIPTION = (
    "Generate a video from a text prompt using the configured model. Returns the "
    "generated video file's local path."
)
GENERATE_VIDEO_FIRST_FRAME_DESCRIPTION = (
    "Generate a video from a text prompt, optionally starting from a local first-frame "
    "image. The local image is uploaded to the configured external provider. Returns "
    "the generated video file's local path."
)
GENERATE_VIDEO_FRAME_RANGE_DESCRIPTION = (
    "Generate a video from a text prompt, optionally using local first- and last-frame "
    "images. Local images are uploaded to the configured external provider. Returns the "
    "generated video file's local path."
)
GENERATE_VIDEO_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "minLength": 1,
            "description": "Describe the video to generate.",
        },
        "duration": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "Requested video duration in seconds. Must be supported by the configured model."
            ),
        },
        "resolution": {
            "type": "string",
            "pattern": r".*\S.*",
            "description": (
                "Requested output resolution. Must be supported by the configured model."
            ),
        },
        "aspect_ratio": {
            "type": "string",
            "pattern": r".*\S.*",
            "description": (
                "Requested output aspect ratio. Must be supported by the configured model."
            ),
        },
        "size": {
            "type": "string",
            "pattern": r".*\S.*",
            "description": "Requested output size. Must be supported by the configured model.",
        },
        "generate_audio": {
            "type": "boolean",
            "description": "Whether the generated video should include audio.",
        },
        "first_frame": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Local image path for the video's first frame. Relative paths start at the "
                "working directory."
            ),
        },
        "last_frame": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Local image path for the video's last frame. Relative paths start at the "
                "working directory."
            ),
        },
        "output_dir": {
            "type": "string",
            "description": (
                "Folder for the generated video, created if missing; relative paths start at "
                "the working directory. Omit to use the default video-gen folder."
            ),
        },
    },
    "required": ["prompt"],
}

GENERATE_MUSIC_DESCRIPTION = (
    "Generate music from a text prompt or local reference images using the configured model. "
    "Local images are uploaded to the configured external provider. Returns the generated "
    "audio file's local path."
)
GENERATE_MUSIC_TEXT_ONLY_DESCRIPTION = (
    "Generate music from a text prompt using the configured model. Returns the generated audio "
    "file's local path."
)
GENERATE_MUSIC_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Describe the music to generate, including any desired lyrics, style, mood, "
                "instrumentation, or structure."
            ),
        },
        "source_images": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "description": (
                "Optional local image paths to use as visual references. Relative paths start "
                "at the working directory."
            ),
        },
        "output_dir": {
            "type": "string",
            "description": (
                "Folder for the generated music, created if missing; relative paths start at "
                "the working directory. Omit to use the default music-gen folder."
            ),
        },
    },
    "required": ["prompt"],
}


_VIDEO_CONTRACT = compile_tool_contract(
    name=GENERATE_VIDEO_TOOL_NAME,
    input_schema=GENERATE_VIDEO_PARAMETERS,
    require_closed_input=False,
)
_MUSIC_CONTRACT = compile_tool_contract(
    name=GENERATE_MUSIC_TOOL_NAME,
    input_schema=GENERATE_MUSIC_PARAMETERS,
    require_closed_input=False,
)


def _normalize_video_arguments(arguments: Any) -> Any:
    return normalize_generate_video_arguments(_VIDEO_CONTRACT, arguments)


def _normalize_music_arguments(arguments: Any) -> Any:
    return normalize_generate_music_arguments(_MUSIC_CONTRACT, arguments)


def _invalid(message: str) -> JsonObject:
    return tool_failure("invalid_arguments", message, retryable=False)


def _media_failure(error: VideoError | MusicError, task: str, setting: str) -> JsonObject:
    """Project an expected media failure; provider refusals say what to do next."""
    message = str(error)
    if isinstance(error, (VideoExecutionError, MusicExecutionError)) and not isinstance(
        error, (VideoOutcomeUnknownError, MusicOutcomeUnknownError)
    ):
        message = provider_failure_message(error, task=task, setting=setting)
    return tool_failure(error.code, message, retryable=bool(getattr(error, "retryable", False)))


def _video_profile_resolver(video_service: Any):
    def resolve(_context: ToolDefinitionProfileContext) -> ToolDefinitionProfile:
        capabilities = set(video_service.generation_capabilities())
        parameters = copy.deepcopy(GENERATE_VIDEO_PARAMETERS)
        properties = parameters["properties"]
        for name in (
            "duration",
            "resolution",
            "aspect_ratio",
            "size",
            "generate_audio",
            "first_frame",
            "last_frame",
        ):
            if name not in capabilities:
                properties.pop(name, None)
        if "last_frame" in capabilities:
            description = GENERATE_VIDEO_FRAME_RANGE_DESCRIPTION
        elif "first_frame" in capabilities:
            description = GENERATE_VIDEO_FIRST_FRAME_DESCRIPTION
        else:
            description = GENERATE_VIDEO_TEXT_ONLY_DESCRIPTION
        return ToolDefinitionProfile(
            key="-".join(sorted(capabilities)) or "text-only",
            description=description,
            parameters=parameters,
        )

    return resolve


def _music_profile_resolver(music_service: Any):
    def resolve(_context: ToolDefinitionProfileContext) -> ToolDefinitionProfile:
        if music_service.generation_supports_source_images():
            return ToolDefinitionProfile(
                key="text-and-reference-images",
                description=GENERATE_MUSIC_DESCRIPTION,
                parameters=GENERATE_MUSIC_PARAMETERS,
            )
        parameters = copy.deepcopy(GENERATE_MUSIC_PARAMETERS)
        parameters["properties"].pop("source_images", None)
        return ToolDefinitionProfile(
            key="text-only",
            description=GENERATE_MUSIC_TEXT_ONLY_DESCRIPTION,
            parameters=parameters,
        )

    return resolve


def make_generate_video_handler(video_service: Any):
    """Create a Video generation handler bound to the runtime service."""

    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return _invalid(
                'Describe the video as prompt, for example {"prompt": "A paper boat drifting '
                'down a rainy street, slow tracking shot"}.'
            )
        try:
            call_options = _video_call_options(arguments)
            output_dir = _output_dir(
                context,
                arguments,
                default_name=_VIDEO_DIRECTORY_NAME,
            )
        except ValueError as exc:
            return _invalid(str(exc))
        try:
            frame_paths = _video_frame_paths(context, arguments)
        except UnusableImageError as problem:
            return tool_failure(problem.code, str(problem), retryable=False)

        try:
            artifact = await video_service.generate_artifact(
                prompt,
                output_dir=output_dir,
                call_options=call_options,
                frame_paths=frame_paths,
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
        except VideoError as exc:
            return _media_failure(exc, "video-generation", "Video generation")
        return tool_success({"video": _artifact_payload(artifact)})

    return handler


def make_generate_music_handler(music_service: Any):
    """Create a Music generation handler bound to the runtime service."""

    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return _invalid(
                'Describe the music as prompt, for example {"prompt": "Calm lo-fi piano, '
                '80 bpm, no vocals"}.'
            )
        try:
            output_dir = _output_dir(
                context,
                arguments,
                default_name=_MUSIC_DIRECTORY_NAME,
            )
        except ValueError as exc:
            return _invalid(str(exc))
        source_paths: tuple[Path, ...] = ()
        if "source_images" in arguments:
            try:
                source_paths = tuple(
                    resolve_local_images(context, arguments["source_images"], "source_images")
                )
            except UnusableImageError as problem:
                return tool_failure(problem.code, str(problem), retryable=False)

        try:
            artifact = await music_service.generate_artifact(
                prompt,
                output_dir=output_dir,
                source_paths=source_paths,
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
        except MusicError as exc:
            return _media_failure(exc, "music-generation", "Music generation")
        return tool_success({"music": _artifact_payload(artifact)})

    return handler


def _video_call_options(arguments: JsonObject) -> JsonObject:
    options: JsonObject = {}
    duration = optional_int(arguments.get("duration"), field_name="duration", minimum=1)
    if duration is not None:
        options["duration"] = duration
    for name in ("resolution", "aspect_ratio", "size"):
        value = optional_string(arguments.get(name), field_name=name)
        if value == "":
            raise ValueError(f"{name} must be a non-empty string when provided")
        if value is not None:
            options[name] = value
    if "generate_audio" in arguments:
        options["generate_audio"] = optional_bool(
            arguments.get("generate_audio"),
            field_name="generate_audio",
            default=False,
        )
    return options


def _video_frame_paths(context: ToolContext, arguments: JsonObject) -> dict[str, Path]:
    return {
        name: resolve_local_image(context, arguments[name], name)
        for name in ("first_frame", "last_frame")
        if arguments.get(name) is not None
    }


def _output_dir(context: ToolContext, arguments: JsonObject, *, default_name: str) -> Path:
    value = optional_string(arguments.get("output_dir"), field_name="output_dir")
    if value:
        return context.resolve_path(value)
    root = context.workspace if context.project_id is None else context.effective_cwd
    return root / default_name


def _artifact_payload(artifact: Any) -> JsonObject:
    return {
        "path": model_path(artifact.file_path),
        "media_type": artifact.media_type,
        "size_bytes": artifact.size_bytes,
    }


def register_generate_video_tool(registry: ToolRegistry, video_service: Any) -> None:
    """Register the Video generation Tool."""

    registry.register(
        GENERATE_VIDEO_TOOL_NAME,
        GENERATE_VIDEO_TEXT_ONLY_DESCRIPTION,
        GENERATE_VIDEO_PARAMETERS,
        make_generate_video_handler(video_service),
        family="media",
        open_input_schema=True,
        argument_normalizer=_normalize_video_arguments,
        result_schema={
            "type": "object",
            "properties": {
                "video": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "minLength": 1},
                        "media_type": {"type": "string", "minLength": 1},
                        "size_bytes": {"type": "integer", "minimum": 1},
                    },
                    "required": ["path", "media_type", "size_bytes"],
                    "additionalProperties": False,
                }
            },
            "required": ["video"],
            "additionalProperties": False,
        },
        display=ToolDisplay(
            primary_candidates=(ToolDisplayField("prompt", kind="text", quote=True),),
            secondary_fields=(
                ToolDisplayField("duration"),
                ToolDisplayField("aspect_ratio"),
                ToolDisplayField("resolution"),
            ),
        ),
        definition_profile_resolver=_video_profile_resolver(video_service),
    )


def register_generate_music_tool(registry: ToolRegistry, music_service: Any) -> None:
    """Register the Music generation Tool."""

    registry.register(
        GENERATE_MUSIC_TOOL_NAME,
        GENERATE_MUSIC_DESCRIPTION,
        GENERATE_MUSIC_PARAMETERS,
        make_generate_music_handler(music_service),
        family="media",
        open_input_schema=True,
        argument_normalizer=_normalize_music_arguments,
        result_schema={
            "type": "object",
            "properties": {
                "music": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "minLength": 1},
                        "media_type": {"type": "string", "minLength": 1},
                        "size_bytes": {"type": "integer", "minimum": 1},
                    },
                    "required": ["path", "media_type", "size_bytes"],
                    "additionalProperties": False,
                }
            },
            "required": ["music"],
            "additionalProperties": False,
        },
        display=ToolDisplay(
            primary_candidates=(ToolDisplayField("prompt", kind="text", quote=True),)
        ),
        definition_profile_resolver=_music_profile_resolver(music_service),
    )
