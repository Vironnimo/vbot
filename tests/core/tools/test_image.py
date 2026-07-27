"""Tests for the image_generation built-in tool."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.model_tasks import ImageInputError
from core.tools import ToolContractError
from core.tools.image import (
    ANALYZE_IMAGE_TOOL_DESCRIPTION,
    ANALYZE_IMAGE_TOOL_NAME,
    IMAGE_GENERATION_TOOL_DESCRIPTION,
    IMAGE_GENERATION_TOOL_NAME,
    register_analyze_image_tool,
    register_image_generation_tool,
)
from core.tools.tools import ToolContext, ToolRegistry


@pytest.mark.asyncio
async def test_image_generation_tool_returns_artifact_payloads(tmp_path: Path) -> None:
    image_path = tmp_path / "artifact-1.png"
    registry = ToolRegistry()
    register_image_generation_tool(registry, _ImageService(image_path))
    context = _make_context(tmp_path)

    result = await registry.dispatch(context, {"prompt": "a red fox"})

    assert result["ok"] is True
    # The UI-facing artifacts payload stays path-free; the WebUI renders from url.
    assert result["artifacts"] == [_ARTIFACT_PAYLOAD]
    data = result["data"]
    assert isinstance(data, dict)
    # The model-facing copies carry the absolute file path for out-of-chat use.
    assert data["images"] == [{**_ARTIFACT_PAYLOAD, "path": str(image_path)}]
    assert IMAGE_GENERATION_TOOL_DESCRIPTION == (
        "Generate new images or edit local source images using the configured image "
        "generation model. Local source files are uploaded to the configured external "
        "provider. The result includes each image's file path and WebUI/Desktop Markdown "
        "for displaying it there. To send an image through a channel, use its file path "
        "with `channel_send`."
    )
    assert data["message"] == (
        "Image generation complete.\n\n"
        "WebUI/Desktop: embed this Markdown in your reply:\n"
        f"![generated image]({_ARTIFACT_PAYLOAD['url']})\n\n"
        "Channel: call `channel_send` with the image `path` in `file_paths`. "
        "Never send the Markdown to a channel."
    )


@pytest.mark.asyncio
async def test_image_generation_tool_rejects_empty_prompt(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_image_generation_tool(registry, _ImageService(tmp_path / "unused.png"))
    context = _make_context(tmp_path)

    result = await registry.dispatch(context, {"prompt": "   "})

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_image_generation_tool_rejects_unknown_arguments(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_image_generation_tool(registry, _ImageService(tmp_path / "unused.png"))

    with pytest.raises(ToolContractError, match="Additional properties"):
        await registry.dispatch(
            _make_context(tmp_path),
            {"prompt": "a red fox", "unexpected": True},
        )


@pytest.mark.asyncio
async def test_image_generation_tool_forwards_per_call_knobs(tmp_path: Path) -> None:
    service = _ImageService(tmp_path / "artifact-1.png")
    registry = ToolRegistry()
    register_image_generation_tool(registry, service)
    context = _make_context(tmp_path)

    result = await registry.dispatch(
        context,
        {"prompt": "a red fox", "aspect_ratio": "16:9", "resolution": "4K"},
    )

    assert result["ok"] is True
    assert service.received_call_options == {"aspect_ratio": "16:9", "resolution": "4K"}


@pytest.mark.asyncio
async def test_image_generation_tool_rejects_blank_knobs(tmp_path: Path) -> None:
    service = _ImageService(tmp_path / "artifact-1.png")
    registry = ToolRegistry()
    register_image_generation_tool(registry, service)
    context = _make_context(tmp_path)

    with pytest.raises(ToolContractError, match=r"aspect_ratio|resolution"):
        await registry.dispatch(
            context,
            {"prompt": "a red fox", "aspect_ratio": "  ", "resolution": ""},
        )

    assert service.received_call_options is None


@pytest.mark.asyncio
async def test_image_generation_tool_resolves_local_source_images(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "photo.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nsource")
    service = _ImageService(tmp_path / "artifact-1.png")
    registry = ToolRegistry()
    register_image_generation_tool(registry, service)
    context = _make_context(workspace)

    result = await registry.dispatch(
        context,
        {"prompt": "make it rainy", "source_images": ["photo.png"]},
    )

    assert result["ok"] is True
    assert service.received_source_paths == (source.resolve(),)


@pytest.mark.asyncio
async def test_image_generation_tool_rejects_single_source_path_string(tmp_path: Path) -> None:
    source = tmp_path / "photo.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nsource")
    service = _ImageService(tmp_path / "artifact-1.png")
    registry = ToolRegistry()
    register_image_generation_tool(registry, service)

    with pytest.raises(ToolContractError, match="is not of type 'array'"):
        await registry.dispatch(
            _make_context(tmp_path),
            {"prompt": "make it rainy", "source_images": str(source)},
        )

    assert service.received_source_paths is None


@pytest.mark.asyncio
async def test_image_generation_tool_rejects_invalid_source_paths_shape(tmp_path: Path) -> None:
    service = _ImageService(tmp_path / "artifact-1.png")
    registry = ToolRegistry()
    register_image_generation_tool(registry, service)

    with pytest.raises(ToolContractError, match="is not of type 'array'"):
        await registry.dispatch(
            _make_context(tmp_path),
            {"prompt": "make it rainy", "source_images": {"path": "photo.png"}},
        )


@pytest.mark.asyncio
async def test_analyze_image_tool_resolves_paths_and_returns_analysis(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = workspace / "first.png"
    second = workspace / "second.png"
    service = _ImageService(tmp_path / "unused.png")
    registry = ToolRegistry()
    register_analyze_image_tool(registry, service)

    result = await registry.dispatch(
        _make_context(workspace, tool_name=ANALYZE_IMAGE_TOOL_NAME),
        {
            "prompt": "Read the ingredients.",
            "images": ["first.png", str(second)],
        },
    )

    assert result["ok"] is True
    assert result["data"] == {
        "analysis": "Visible details",
        "model": "vision-model",
        "image_count": 2,
    }
    assert service.received_analysis_prompt == "Read the ingredients."
    assert service.received_analysis_paths == (first.resolve(), second.resolve())
    assert "untrusted content" in ANALYZE_IMAGE_TOOL_DESCRIPTION


@pytest.mark.asyncio
async def test_analyze_image_tool_rejects_single_path_string(tmp_path: Path) -> None:
    service = _ImageService(tmp_path / "unused.png")
    registry = ToolRegistry()
    register_analyze_image_tool(registry, service)

    with pytest.raises(ToolContractError, match="is not of type 'array'"):
        await registry.dispatch(
            _make_context(tmp_path, tool_name=ANALYZE_IMAGE_TOOL_NAME),
            {"prompt": "Describe it.", "images": "photo.png"},
        )

    assert service.received_analysis_paths is None


@pytest.mark.asyncio
async def test_analyze_image_tool_rejects_invalid_arguments_and_maps_image_error(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry()
    service = _ImageService(
        tmp_path / "unused.png",
        analysis_error=ImageInputError("bad image"),
    )
    register_analyze_image_tool(registry, service)
    context = _make_context(tmp_path, tool_name=ANALYZE_IMAGE_TOOL_NAME)

    with pytest.raises(ToolContractError, match="non-empty"):
        await registry.dispatch(context, {"prompt": "Describe it.", "images": []})
    with pytest.raises(ToolContractError, match="Additional properties"):
        await registry.dispatch(
            context,
            {"prompt": "Describe it.", "images": ["photo.png"], "extra": True},
        )
    image_error = await registry.dispatch(
        context,
        {"prompt": "Describe it.", "images": ["photo.png"]},
    )

    assert image_error["error"]["code"] == "image_understanding_error"


def _make_context(
    tmp_path: Path,
    *,
    tool_name: str = IMAGE_GENERATION_TOOL_NAME,
) -> ToolContext:
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


_ARTIFACT_PAYLOAD = {
    "id": "artifact-1",
    "kind": "image",
    "filename": "artifact-1.png",
    "media_type": "image/png",
    "size_bytes": 5,
    "url": "/api/images/artifacts/artifact-1",
    "index": 0,
}


class _ImageService:
    def __init__(
        self,
        file_path: Path,
        *,
        analysis_error: Exception | None = None,
    ) -> None:
        self._file_path = file_path
        self._analysis_error = analysis_error
        self.received_prompt: str | None = None
        self.received_call_options: dict[str, object] | None = None
        self.received_source_paths: tuple[Path, ...] | None = None
        self.received_analysis_prompt: str | None = None
        self.received_analysis_paths: tuple[Path, ...] | None = None

    async def generate_artifacts(
        self,
        prompt: str,
        *,
        call_options: dict[str, object] | None = None,
        source_paths: tuple[Path, ...] | None = None,
    ) -> tuple[object, ...]:
        self.received_prompt = prompt
        self.received_call_options = call_options
        self.received_source_paths = source_paths
        return (
            SimpleNamespace(
                file_path=self._file_path,
                to_dict=lambda: dict(_ARTIFACT_PAYLOAD),
            ),
        )

    async def analyze(
        self,
        prompt: str,
        *,
        image_paths: tuple[Path, ...],
    ) -> object:
        self.received_analysis_prompt = prompt
        self.received_analysis_paths = image_paths
        if self._analysis_error is not None:
            raise self._analysis_error
        return SimpleNamespace(
            to_dict=lambda: {
                "analysis": "Visible details",
                "model": "vision-model",
                "image_count": len(image_paths),
            }
        )
