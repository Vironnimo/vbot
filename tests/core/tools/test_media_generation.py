"""Tests for the generate_video and generate_music built-in Tools."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.model_tasks import (
    MusicExecutionError,
    VideoConfigurationError,
    VideoExecutionError,
    VideoOptionError,
    VideoOutcomeUnknownError,
    VideoRefusedError,
)
from core.model_tasks.video import VideoProfile
from core.providers.errors import ProviderRateLimitError
from core.tools.media_generation import (
    GENERATE_MUSIC_TOOL_NAME,
    GENERATE_VIDEO_TOOL_NAME,
    register_generate_music_tool,
    register_generate_video_tool,
)
from core.tools.tools import ToolDefinitionProfileContext, ToolRegistry
from core.utils.paths import model_path
from tests.core.tools.image_test_support import make_context, write_image


def _profile(*offered: str) -> VideoProfile:
    """A video Model offering the named per-call options and frame positions."""
    choices = {
        "duration": ("4", "6", "8"),
        "aspect_ratio": ("16:9", "9:16"),
        "resolution": ("720p", "1080p"),
    }
    return VideoProfile(
        call_choices={name: values for name, values in choices.items() if name in offered},
        generate_audio="generate_audio" in offered,
        frame_images=tuple(name for name in ("first_frame", "last_frame") if name in offered),
    )


@pytest.mark.parametrize(
    ("profile", "offered"),
    [
        pytest.param(
            _profile("duration", "resolution", "first_frame"),
            {
                "duration": {"type": "integer", "enum": [4, 6, 8]},
                "resolution": {"type": "string", "enum": ["720p", "1080p"]},
                "first_frame": {"type": "string"},
            },
            id="choices-and-first-frame",
        ),
        pytest.param(
            VideoProfile(
                call_choices={"duration": ("4", "5", "6")},
                generate_audio=True,
                frame_images=("first_frame", "last_frame"),
            ),
            {
                "duration": {"type": "integer", "minimum": 4, "maximum": 6},
                "generate_audio": {"type": "boolean"},
                "first_frame": {"type": "string"},
                "last_frame": {"type": "string"},
            },
            id="duration-range-audio-and-frames",
        ),
        pytest.param(VideoProfile(), {}, id="nothing-offered"),
    ],
)
def test_video_profile_only_exposes_configured_model_capabilities(
    tmp_path: Path, profile: VideoProfile, offered: dict[str, dict[str, object]]
) -> None:
    registry = ToolRegistry()
    register_generate_video_tool(registry, _VideoService(tmp_path / "video.mp4", profile))

    definition = registry.provider_definitions(
        [GENERATE_VIDEO_TOOL_NAME],
        profile_context=ToolDefinitionProfileContext(agent_id="agent"),
    )[0]

    properties = definition["parameters"]["properties"]
    assert set(properties) == {"prompt", "output_dir", *offered}
    for name, facts in offered.items():
        assert facts.items() <= properties[name].items()
    assert "billed" in definition["description"]


@pytest.mark.asyncio
async def test_video_tool_resolves_frames_and_caller_owned_default(tmp_path: Path) -> None:
    service = _VideoService(tmp_path / "video.mp4", _profile("duration", "first_frame"))
    registry = ToolRegistry()
    register_generate_video_tool(registry, service)
    context = make_context(tmp_path, GENERATE_VIDEO_TOOL_NAME)
    write_image(tmp_path / "start.png")

    result = await registry.dispatch(
        context,
        {"prompt": "A river at dawn", "duration": 8, "first_frame": "start.png"},
    )

    assert result["data"]["video"] == {
        "path": model_path(tmp_path / "video.mp4"),
        "media_type": "video/mp4",
        "size_bytes": 5,
    }
    assert service.call_options == {"duration": 8}
    assert service.frame_paths == {"first_frame": (tmp_path / "start.png").resolve()}
    assert service.output_dir == tmp_path / "video-gen"


def test_music_profile_hides_reference_images_for_text_only_model(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_generate_music_tool(
        registry,
        _MusicService(tmp_path / "music.mp3", supports_images=False),
    )

    definitions = registry.provider_definitions(
        [GENERATE_MUSIC_TOOL_NAME],
        profile_context=ToolDefinitionProfileContext(agent_id="agent"),
    )
    contract = registry.contracts_for_provider_definitions(definitions)[GENERATE_MUSIC_TOOL_NAME]

    assert "source_images" not in definitions[0]["parameters"]["properties"]
    assert contract.input_schema["properties"].get("source_images") is None


@pytest.mark.asyncio
async def test_music_tool_returns_local_artifact_facts(tmp_path: Path) -> None:
    service = _MusicService(tmp_path / "music.mp3", supports_images=True)
    registry = ToolRegistry()
    register_generate_music_tool(registry, service)
    profile_context = ToolDefinitionProfileContext(agent_id="agent")
    definitions = registry.provider_definitions(
        [GENERATE_MUSIC_TOOL_NAME], profile_context=profile_context
    )
    contract = registry.contracts_for_provider_definitions(definitions)[GENERATE_MUSIC_TOOL_NAME]
    context = make_context(tmp_path, GENERATE_MUSIC_TOOL_NAME, input_contract=contract)
    write_image(tmp_path / "cover.png")

    result = await registry.dispatch(
        context,
        {"prompt": "Dreamy synthwave", "source_images": ["cover.png"]},
    )

    assert result["data"]["music"] == {
        "path": model_path(tmp_path / "music.mp3"),
        "media_type": "audio/mpeg",
        "size_bytes": 5,
        "text": "A calm piano piece",
    }
    assert service.source_paths == ((tmp_path / "cover.png").resolve(),)
    assert service.output_dir == tmp_path / "music-gen"
    # The user sees the cover the music started from and gets a player for the music.
    display = registry.display_for_call(
        GENERATE_MUSIC_TOOL_NAME, {"prompt": "Dreamy synthwave"}, context=context, result=result
    )
    assert display["media_files"] == [
        {
            "path": str((tmp_path / "cover.png").resolve()),
            "kind": "image",
            "filename": "cover.png",
            "role": "source",
        },
        {"path": str(tmp_path / "music.mp3"), "kind": "audio", "filename": "music.mp3"},
    ]
    assert display["details"] == []


@pytest.mark.asyncio
async def test_video_tool_accepts_other_spellings_and_empty_options(tmp_path: Path) -> None:
    service = _VideoService(
        tmp_path / "video.mp4", _profile("first_frame", "last_frame", "resolution")
    )
    registry = ToolRegistry()
    register_generate_video_tool(registry, service)
    start = write_image(tmp_path / "start.png")
    end = write_image(tmp_path / "end.png")

    result = await registry.dispatch(
        make_context(tmp_path, GENERATE_VIDEO_TOOL_NAME),
        {
            "prompt": "A river at dawn",
            "start_frame": "start.png",
            "end_image": "end.png",
            "resolution": "",
            "output_directory": "clips",
        },
    )

    assert result["ok"] is True
    assert service.frame_paths == {"first_frame": start.resolve(), "last_frame": end.resolve()}
    assert service.call_options == {}
    assert service.output_dir == (tmp_path / "clips").resolve()


@pytest.mark.asyncio
async def test_missing_frame_names_the_similar_file_as_a_single_path(tmp_path: Path) -> None:
    service = _VideoService(tmp_path / "video.mp4", _profile("first_frame"))
    registry = ToolRegistry()
    register_generate_video_tool(registry, service)
    write_image(tmp_path / "start.png")

    result = await registry.dispatch(
        make_context(tmp_path, GENERATE_VIDEO_TOOL_NAME),
        {"prompt": "A river at dawn", "first_frame": "start.jpg"},
    )

    assert result["error"] == {
        "code": "image_not_found",
        "message": "No image at start.jpg (similar: start.png).\n"
        'If you meant that file, pass {"first_frame": "start.png"}.',
        "retryable": False,
    }
    assert service.frame_paths is None


@pytest.mark.asyncio
async def test_music_source_alias_and_provider_wording(tmp_path: Path) -> None:
    service = _MusicService(tmp_path / "music.mp3", supports_images=True)
    registry = ToolRegistry()
    register_generate_music_tool(registry, service)
    cover = write_image(tmp_path / "cover.png")

    result = await registry.dispatch(
        make_context(tmp_path, GENERATE_MUSIC_TOOL_NAME),
        {"prompt": "Dreamy synthwave", "reference_images": "cover.png"},
    )
    assert result["ok"] is True
    assert service.source_paths == (cover.resolve(),)

    limit = ProviderRateLimitError("Rate limited: 429 slow down")
    limit.status_code = 429  # type: ignore[attr-defined]
    try:
        raise MusicExecutionError(str(limit)) from limit
    except MusicExecutionError as error:
        service.error = error
    result = await registry.dispatch(
        make_context(tmp_path, GENERATE_MUSIC_TOOL_NAME), {"prompt": "Dreamy synthwave"}
    )
    assert result["error"]["code"] == "provider_error"
    assert result["error"]["message"] == (
        "The music-generation provider is limiting requests or its usage limit is reached "
        "(HTTP 429: slow down). Wait before trying again, and tell the user if it keeps "
        "happening."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code", "message"),
    [
        pytest.param(
            VideoOptionError(
                "duration '7' is not offered by the configured video model; choose one of: 5, 10."
            ),
            "invalid_arguments",
            "duration '7' is not offered by the configured video model; choose one of: 5, 10.",
            id="choice-not-offered",
        ),
        pytest.param(
            VideoConfigurationError("The configured provider does not support Video generation."),
            "video_error",
            "Video generation is not available (The configured provider does not support "
            "Video generation.). Tell the user to choose a working Video generation model in "
            "Settings → Tools → Images, video & music.",
            id="not-available",
        ),
        pytest.param(
            VideoExecutionError("Provider error: 400 prompt rejected by safety filter"),
            "provider_error",
            "The video-generation provider rejected the request (HTTP 400: prompt rejected by "
            "safety filter). If the reason concerns the request, change it; otherwise "
            "tell the user, who may need to choose another Video generation model in Settings "
            "→ Tools → Images, video & music.",
            id="provider-rejection",
        ),
        pytest.param(
            VideoRefusedError("Your request was rejected by the safety system."),
            "generation_refused",
            "The video-generation provider refused the request and created nothing. Its "
            "reason: Your request was rejected by the safety system. Repeating the unchanged "
            "request gets the same refusal. Change what the prompt asks for, for example an "
            "original design instead of a named character, brand or real person, or tell the "
            "user.",
            id="refused",
        ),
        pytest.param(
            VideoOutcomeUnknownError(
                "it had not finished after 20 minutes", operation_key="job-1", job_id="job-1"
            ),
            "provider_outcome_unknown",
            "The video-generation provider accepted the request as job job-1, but it had not "
            "finished after 20 minutes. The job can still finish and be charged; nothing was "
            "saved. Do not request the same video again. Tell the user, including the job id.",
            id="job-unfinished",
        ),
    ],
)
async def test_video_failures_say_what_happened_and_what_to_do(
    tmp_path: Path, error: Exception, code: str, message: str
) -> None:
    service = _VideoService(tmp_path / "video.mp4", _profile("duration"))
    service.error = error
    registry = ToolRegistry()
    register_generate_video_tool(registry, service)

    result = await registry.dispatch(
        make_context(tmp_path, GENERATE_VIDEO_TOOL_NAME), {"prompt": "A river", "duration": 7}
    )

    assert result["error"] == {"code": code, "message": message, "retryable": False}


class _VideoService:
    def __init__(self, file_path: Path, profile: VideoProfile) -> None:
        self.file_path = file_path
        self.profile = profile
        self.call_options: dict[str, object] | None = None
        self.frame_paths: dict[str, Path] | None = None
        self.output_dir: Path | None = None
        self.error: Exception | None = None

    def generation_profile(self) -> VideoProfile:
        return self.profile

    async def generate_artifact(
        self,
        _prompt: str,
        *,
        output_dir: Path,
        call_options: dict[str, object],
        frame_paths: dict[str, Path],
        usage_context: object = None,
    ) -> object:
        if self.error is not None:
            raise self.error
        self.call_options = call_options
        self.frame_paths = frame_paths
        self.usage_context = usage_context
        self.output_dir = output_dir
        return SimpleNamespace(
            file_path=self.file_path,
            media_type="video/mp4",
            size_bytes=5,
        )


class _MusicService:
    def __init__(self, file_path: Path, *, supports_images: bool) -> None:
        self.file_path = file_path
        self.supports_images = supports_images
        self.source_paths: tuple[Path, ...] | None = None
        self.output_dir: Path | None = None
        self.error: Exception | None = None

    def generation_supports_source_images(self) -> bool:
        return self.supports_images

    async def generate_artifact(
        self,
        _prompt: str,
        *,
        output_dir: Path,
        source_paths: tuple[Path, ...],
        usage_context: object = None,
    ) -> object:
        if self.error is not None:
            raise self.error
        self.source_paths = source_paths
        self.usage_context = usage_context
        self.output_dir = output_dir
        return SimpleNamespace(
            file_path=self.file_path,
            media_type="audio/mpeg",
            size_bytes=5,
            transcript="",
            text="A calm piano piece",
        )
