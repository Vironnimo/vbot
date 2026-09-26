"""Tests for the generate_video and generate_music built-in Tools."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.model_tasks import MusicExecutionError, VideoConfigurationError, VideoExecutionError
from core.providers.errors import ProviderRateLimitError
from core.tools.media_generation import (
    GENERATE_MUSIC_TOOL_NAME,
    GENERATE_VIDEO_TOOL_NAME,
    register_generate_music_tool,
    register_generate_video_tool,
)
from core.tools.tools import ToolContext, ToolDefinitionProfileContext, ToolRegistry
from core.utils.paths import model_path


def test_video_profile_only_exposes_configured_model_capabilities(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_generate_video_tool(
        registry,
        _VideoService(tmp_path / "video.mp4", {"duration", "resolution", "first_frame"}),
    )

    definition = registry.provider_definitions(
        [GENERATE_VIDEO_TOOL_NAME],
        profile_context=ToolDefinitionProfileContext(agent_id="agent"),
    )[0]

    assert set(definition["parameters"]["properties"]) == {
        "prompt",
        "duration",
        "resolution",
        "first_frame",
        "output_dir",
    }
    assert isinstance(definition["description"], str)
    assert definition["description"]


@pytest.mark.asyncio
async def test_video_tool_resolves_frames_and_caller_owned_default(tmp_path: Path) -> None:
    service = _VideoService(tmp_path / "video.mp4", {"duration", "first_frame"})
    registry = ToolRegistry()
    register_generate_video_tool(registry, service)
    context = _context(tmp_path, GENERATE_VIDEO_TOOL_NAME)
    _image(tmp_path / "start.png")

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
    context = replace(_context(tmp_path, GENERATE_MUSIC_TOOL_NAME), input_contract=contract)
    _image(tmp_path / "cover.png")

    result = await registry.dispatch(
        context,
        {"prompt": "Dreamy synthwave", "source_images": ["cover.png"]},
    )

    assert result["data"]["music"] == {
        "path": model_path(tmp_path / "music.mp3"),
        "media_type": "audio/mpeg",
        "size_bytes": 5,
    }
    assert service.source_paths == ((tmp_path / "cover.png").resolve(),)
    assert service.output_dir == tmp_path / "music-gen"


@pytest.mark.asyncio
async def test_video_tool_accepts_other_spellings_and_empty_options(tmp_path: Path) -> None:
    service = _VideoService(tmp_path / "video.mp4", {"first_frame", "last_frame", "resolution"})
    registry = ToolRegistry()
    register_generate_video_tool(registry, service)
    start = _image(tmp_path / "start.png")
    end = _image(tmp_path / "end.png")

    result = await registry.dispatch(
        _context(tmp_path, GENERATE_VIDEO_TOOL_NAME),
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
    service = _VideoService(tmp_path / "video.mp4", {"first_frame"})
    registry = ToolRegistry()
    register_generate_video_tool(registry, service)
    _image(tmp_path / "start.png")

    result = await registry.dispatch(
        _context(tmp_path, GENERATE_VIDEO_TOOL_NAME),
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
    cover = _image(tmp_path / "cover.png")

    result = await registry.dispatch(
        _context(tmp_path, GENERATE_MUSIC_TOOL_NAME),
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
        _context(tmp_path, GENERATE_MUSIC_TOOL_NAME), {"prompt": "Dreamy synthwave"}
    )
    assert result["error"]["code"] == "provider_error"
    assert result["error"]["message"] == (
        "The music-generation provider is limiting requests or its usage limit is reached "
        "(HTTP 429: slow down). Wait before trying again, and tell the user if it keeps "
        "happening."
    )


@pytest.mark.asyncio
async def test_video_failures_keep_request_fixes_and_reword_provider_refusals(
    tmp_path: Path,
) -> None:
    service = _VideoService(tmp_path / "video.mp4", {"duration"})
    registry = ToolRegistry()
    register_generate_video_tool(registry, service)
    context = _context(tmp_path, GENERATE_VIDEO_TOOL_NAME)

    service.error = VideoConfigurationError(
        "duration must be one of the values supported by the configured model: 5, 10."
    )
    result = await registry.dispatch(context, {"prompt": "A river", "duration": 7})
    assert result["error"]["message"] == (
        "duration must be one of the values supported by the configured model: 5, 10."
    )

    service.error = VideoExecutionError("Provider error: 400 prompt rejected by safety filter")
    result = await registry.dispatch(context, {"prompt": "A river"})
    assert result["error"]["message"] == (
        "The video-generation provider rejected the request (HTTP 400: prompt rejected by "
        "safety filter). If the reason concerns the request, change it; otherwise "
        "tell the user, who may need to choose another Video generation model in Settings "
        "under Specialized Models."
    )


def _image(path: Path) -> Path:
    path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    return path


def _context(tmp_path: Path, tool_name: str) -> ToolContext:
    return ToolContext(
        agent_id="agent",
        session_id="session",
        run_id="run",
        tool_call_id="tool-call",
        tool_name=tool_name,
        tool_call_index=0,
        workspace=tmp_path,
        vbot_root=tmp_path,
        data_root=tmp_path,
    )


class _VideoService:
    def __init__(self, file_path: Path, capabilities: set[str]) -> None:
        self.file_path = file_path
        self.capabilities = capabilities
        self.call_options: dict[str, object] | None = None
        self.frame_paths: dict[str, Path] | None = None
        self.output_dir: Path | None = None
        self.error: Exception | None = None

    def generation_capabilities(self) -> frozenset[str]:
        return frozenset(self.capabilities)

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
        )
