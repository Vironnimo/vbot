"""Tests for the image_generation built-in tool."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.tools.image import (
    IMAGE_GENERATION_TOOL_DESCRIPTION,
    IMAGE_GENERATION_TOOL_NAME,
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

    result = await registry.dispatch(
        _make_context(tmp_path),
        {"prompt": "a red fox", "unexpected": True},
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert "unexpected" in result["error"]["message"]


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
async def test_image_generation_tool_omits_blank_knobs(tmp_path: Path) -> None:
    service = _ImageService(tmp_path / "artifact-1.png")
    registry = ToolRegistry()
    register_image_generation_tool(registry, service)
    context = _make_context(tmp_path)

    result = await registry.dispatch(
        context,
        {"prompt": "a red fox", "aspect_ratio": "  ", "resolution": ""},
    )

    assert result["ok"] is True
    assert service.received_call_options == {}


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
async def test_image_generation_tool_accepts_single_source_path_string(tmp_path: Path) -> None:
    source = tmp_path / "photo.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nsource")
    service = _ImageService(tmp_path / "artifact-1.png")
    registry = ToolRegistry()
    register_image_generation_tool(registry, service)

    result = await registry.dispatch(
        _make_context(tmp_path),
        {"prompt": "make it rainy", "source_images": str(source)},
    )

    assert result["ok"] is True
    assert service.received_source_paths == (source.resolve(),)


@pytest.mark.asyncio
async def test_image_generation_tool_rejects_invalid_source_paths_shape(tmp_path: Path) -> None:
    service = _ImageService(tmp_path / "artifact-1.png")
    registry = ToolRegistry()
    register_image_generation_tool(registry, service)

    result = await registry.dispatch(
        _make_context(tmp_path),
        {"prompt": "make it rainy", "source_images": {"path": "photo.png"}},
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


def _make_context(tmp_path: Path) -> ToolContext:
    return ToolContext(
        agent_id="agent",
        session_id="session",
        run_id="run",
        tool_call_id="tool-call",
        tool_name=IMAGE_GENERATION_TOOL_NAME,
        tool_call_index=0,
        workspace=tmp_path,
        app_root=tmp_path,
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
    def __init__(self, file_path: Path) -> None:
        self._file_path = file_path
        self.received_prompt: str | None = None
        self.received_call_options: dict[str, object] | None = None
        self.received_source_paths: tuple[Path, ...] | None = None

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
