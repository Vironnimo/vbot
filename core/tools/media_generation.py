"""Built-in Video and Music generation Tools."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from core.model_tasks import MusicError, VideoError, VideoOutcomeUnknownError
from core.tools.arguments import optional_bool, optional_int, optional_string
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
_VIDEO_ARGUMENTS = frozenset(
    {
        "prompt",
        "duration",
        "resolution",
        "aspect_ratio",
        "size",
        "generate_audio",
        "first_frame",
        "last_frame",
        "output_dir",
    }
)
_MUSIC_ARGUMENTS = frozenset({"prompt", "source_images", "output_dir"})

GENERATE_VIDEO_TEXT_ONLY_DESCRIPTION = (
    "Generate a video from a text prompt using the configured model. Returns the "
    "generated video file’s local path."
)
GENERATE_VIDEO_FIRST_FRAME_DESCRIPTION = (
    "Generate a video from a text prompt, optionally starting from a local first-frame "
    "image. The local image is uploaded to the configured external provider. Returns "
    "the generated video file’s local path."
)
GENERATE_VIDEO_FRAME_RANGE_DESCRIPTION = (
    "Generate a video from a text prompt, optionally using local first- and last-frame "
    "images. Local images are uploaded to the configured external provider. Returns the "
    "generated video file’s local path."
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
                "Local image path for the video’s first frame. Relative paths resolve against "
                "the effective working directory."
            ),
        },
        "last_frame": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Local image path for the video’s last frame. Relative paths resolve against "
                "the effective working directory."
            ),
        },
        "output_dir": {
            "type": "string",
            "description": (
                "Directory for the generated video. Relative paths resolve against the "
                "effective working directory."
            ),
        },
    },
    "required": ["prompt"],
}

GENERATE_MUSIC_DESCRIPTION = (
    "Generate music from a text prompt or local reference images using the configured model. "
    "Local images are uploaded to the configured external provider. Returns the generated "
    "audio file’s local path."
)
GENERATE_MUSIC_TEXT_ONLY_DESCRIPTION = (
    "Generate music from a text prompt using the configured model. Returns the generated audio "
    "file’s local path."
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
                "Optional local image paths to use as visual references. Relative paths resolve "
                "against the effective working directory."
            ),
        },
        "output_dir": {
            "type": "string",
            "description": (
                "Directory for the generated music. Relative paths resolve against the effective "
                "working directory."
            ),
        },
    },
    "required": ["prompt"],
}


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
        unknown = set(arguments) - _VIDEO_ARGUMENTS
        if unknown:
            return tool_failure(
                "invalid_arguments",
                f"Unknown argument(s): {', '.join(sorted(unknown))}",
            )
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return tool_failure("invalid_arguments", "prompt must be a non-empty string")
        try:
            call_options = _video_call_options(arguments)
            frame_paths = _video_frame_paths(context, arguments)
            output_dir = _output_dir(
                context,
                arguments,
                default_name=_VIDEO_DIRECTORY_NAME,
            )
        except ValueError as exc:
            return tool_failure("invalid_arguments", str(exc))

        try:
            artifact = await video_service.generate_artifact(
                prompt,
                output_dir=output_dir,
                call_options=call_options,
                frame_paths=frame_paths,
            )
        except VideoOutcomeUnknownError as exc:
            return tool_failure(exc.code, str(exc), retryable=False)
        except VideoError as exc:
            return tool_failure(exc.code, str(exc), retryable=exc.retryable)
        return tool_success({"video": _artifact_payload(artifact)})

    return handler


def make_generate_music_handler(music_service: Any):
    """Create a Music generation handler bound to the runtime service."""

    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        unknown = set(arguments) - _MUSIC_ARGUMENTS
        if unknown:
            return tool_failure(
                "invalid_arguments",
                f"Unknown argument(s): {', '.join(sorted(unknown))}",
            )
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return tool_failure("invalid_arguments", "prompt must be a non-empty string")
        try:
            source_paths = _source_paths(context, arguments)
            output_dir = _output_dir(
                context,
                arguments,
                default_name=_MUSIC_DIRECTORY_NAME,
            )
        except ValueError as exc:
            return tool_failure("invalid_arguments", str(exc))

        try:
            artifact = await music_service.generate_artifact(
                prompt,
                output_dir=output_dir,
                source_paths=source_paths,
            )
        except MusicError as exc:
            return tool_failure(exc.code, str(exc), retryable=exc.retryable)
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
    paths: dict[str, Path] = {}
    for name in ("first_frame", "last_frame"):
        value = optional_string(arguments.get(name), field_name=name)
        if value == "":
            raise ValueError(f"{name} must be a non-empty string when provided")
        if value is not None:
            paths[name] = context.resolve_path(value)
    return paths


def _source_paths(context: ToolContext, arguments: JsonObject) -> tuple[Path, ...]:
    raw_paths = arguments.get("source_images")
    if raw_paths is None:
        return ()
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ValueError("source_images must contain at least one local image path")
    paths: list[Path] = []
    for index, raw_path in enumerate(raw_paths):
        value = optional_string(raw_path, field_name=f"source_images[{index}]")
        if not value:
            raise ValueError(f"source_images[{index}] must be a non-empty string")
        paths.append(context.resolve_path(value))
    return tuple(paths)


def _output_dir(context: ToolContext, arguments: JsonObject, *, default_name: str) -> Path:
    value = optional_string(arguments.get("output_dir"), field_name="output_dir")
    if value == "":
        raise ValueError("output_dir must be a non-empty string when provided")
    if value is not None:
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
