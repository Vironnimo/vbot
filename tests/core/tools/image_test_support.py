"""A recording image service and registered image Tools, for generate_image and
analyze_image tests.

Calls run through ``registry.dispatch``; a call the contract refuses becomes the
``invalid_arguments`` result the Tool executor returns to the Model.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.model_tasks import ImageUnderstandingRunContext
from core.model_tasks.image_profile import ImageProfile
from core.tools.image import (
    ANALYZE_IMAGE_TOOL_NAME,
    GENERATE_IMAGE_TOOL_NAME,
    register_analyze_image_tool,
    register_generate_image_tool,
)
from core.tools.tools import ToolContext, ToolRegistry
from tests.core.tools.tools_test_support import dispatch_as_executor

PNG = b"\x89PNG\r\n\x1a\nimage"
ANALYSIS = "Visible details"
#: An editing Model offering every per-call choice.
EDITING_PROFILE = ImageProfile(
    wire="openrouter",
    call_choices={
        "aspect_ratio": ("1:1", "16:9", "9:16"),
        "resolution": ("1K", "2K", "4K"),
        "background": ("transparent", "opaque"),
    },
    max_source_images=None,
)
TEXT_ONLY_PROFILE = ImageProfile(wire="openrouter")


def write_image(path: Path, data: bytes = PNG) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def make_context(root: Path, tool_name: str, **overrides: Any) -> ToolContext:
    context = ToolContext(
        agent_id="agent",
        session_id="session",
        run_id="run",
        tool_call_id="call",
        tool_name=tool_name,
        tool_call_index=0,
        workspace=root,
        vbot_root=root,
        data_root=root,
    )
    return replace(context, **overrides)


class ImageService:
    """Records what the image Tools ask for; ``error`` fails generation and analysis."""

    def __init__(
        self,
        *,
        image_path: Path | None = None,
        error: Exception | None = None,
        profile: ImageProfile = EDITING_PROFILE,
        revised_prompt: str | None = None,
    ) -> None:
        self.image_path = image_path
        self.revised_prompt = revised_prompt
        self.error = error
        self.profile = profile
        self.generated: dict[str, Any] | None = None
        self.output_dirs: list[Path] = []
        self.analyzed: dict[str, Any] | None = None

    def generation_profile(self) -> ImageProfile:
        return self.profile

    async def generate_artifacts(
        self,
        prompt: str,
        *,
        output_dir: Path,
        call_options: dict[str, object] | None = None,
        source_paths: tuple[Path, ...] = (),
        usage_context: object = None,
    ) -> tuple[object, ...]:
        self.output_dirs.append(output_dir)
        if self.error is not None:
            raise self.error
        self.generated = {
            "prompt": prompt,
            "call_options": call_options,
            "source_paths": source_paths,
        }
        file_path = self.image_path or output_dir / "image.png"
        return (
            SimpleNamespace(
                file_path=file_path,
                media_type="image/png",
                size_bytes=5,
                width=1024,
                height=576,
                revised_prompt=self.revised_prompt,
            ),
        )

    async def analyze(
        self,
        prompt: str,
        *,
        image_paths: tuple[Path, ...],
        run_context: ImageUnderstandingRunContext,
    ) -> object:
        if self.error is not None:
            raise self.error
        self.analyzed = {
            "prompt": prompt,
            "paths": image_paths,
            "contents": [path.read_bytes() for path in image_paths],
            "run_context": run_context,
        }
        return SimpleNamespace(content=ANALYSIS)


def image_registry(service: ImageService, *, attachment_store: Any = None) -> ToolRegistry:
    """A registry holding generate_image and analyze_image for ``service``."""
    registry = ToolRegistry()
    register_generate_image_tool(registry, service)
    register_analyze_image_tool(registry, service, attachment_store=attachment_store)
    return registry


async def generate(
    root: Path, arguments: Any, service: ImageService, **context: Any
) -> dict[str, Any]:
    """Dispatch one generate_image call from an Agent whose Workspace is ``root``."""
    return await dispatch_as_executor(
        image_registry(service),
        make_context(root, GENERATE_IMAGE_TOOL_NAME, **context),
        arguments,
    )


async def analyze(
    root: Path, arguments: Any, service: ImageService, **context: Any
) -> dict[str, Any]:
    """Dispatch one analyze_image call from an Agent whose Workspace is ``root``."""
    return await dispatch_as_executor(
        image_registry(service), make_context(root, ANALYZE_IMAGE_TOOL_NAME, **context), arguments
    )


def failure(code: str, message: str, **error: Any) -> dict[str, Any]:
    """The error object of a failure result."""
    return {"code": code, "message": message, "retryable": False, **error}


def contract_refusal(message: str) -> dict[str, Any]:
    """The error object of a call the Tool's contract refused."""
    return {"code": "invalid_arguments", "message": message}
