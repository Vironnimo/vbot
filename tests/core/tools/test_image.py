"""Tests for the image_generation built-in tool."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.model_tasks import (
    ImageExecutionError,
    ImageInputError,
    ImageNotFoundError,
    ImageOutcomeUnknownError,
    ImageReadError,
    ImageTooLargeError,
    ImageUnderstandingRunContext,
    ImageUnderstandingUnavailableError,
    ImageUnsupportedMediaTypeError,
)
from core.tools import InvalidToolResultError, ToolContractError
from core.tools.image import (
    ANALYZE_IMAGE_TOOL_DESCRIPTION,
    ANALYZE_IMAGE_TOOL_NAME,
    ANALYZE_IMAGE_TOOL_PARAMETERS,
    IMAGE_GENERATION_TEXT_ONLY_TOOL_DESCRIPTION,
    IMAGE_GENERATION_TEXT_ONLY_TOOL_PARAMETERS,
    IMAGE_GENERATION_TOOL_DESCRIPTION,
    IMAGE_GENERATION_TOOL_NAME,
    make_image_generation_handler,
    register_analyze_image_tool,
    register_image_generation_tool,
)
from core.tools.tools import ToolContext, ToolDefinitionProfileContext, ToolRegistry
from core.utils.paths import model_path


def test_image_generation_profile_matches_configured_model_capability(tmp_path: Path) -> None:
    text_only_service = _ImageService(
        tmp_path / "text.png",
        supports_source_images=False,
    )
    editing_service = _ImageService(
        tmp_path / "editing.png",
        supports_source_images=True,
    )
    text_only_registry = ToolRegistry()
    editing_registry = ToolRegistry()
    register_image_generation_tool(text_only_registry, text_only_service)
    register_image_generation_tool(editing_registry, editing_service)
    profile_context = ToolDefinitionProfileContext(agent_id="agent-1")

    text_only = text_only_registry.provider_definitions(
        [IMAGE_GENERATION_TOOL_NAME],
        profile_context=profile_context,
    )
    editing = editing_registry.provider_definitions(
        [IMAGE_GENERATION_TOOL_NAME],
        profile_context=profile_context,
    )

    text_only_properties = text_only[0]["parameters"]["properties"]
    editing_properties = editing[0]["parameters"]["properties"]
    assert text_only[0]["description"] == IMAGE_GENERATION_TEXT_ONLY_TOOL_DESCRIPTION
    assert "additionalProperties" not in text_only[0]["parameters"]
    assert "additionalProperties" not in editing[0]["parameters"]
    assert text_only_registry.get(IMAGE_GENERATION_TOOL_NAME).open_input_schema is True
    assert editing_registry.get(IMAGE_GENERATION_TOOL_NAME).open_input_schema is True
    assert "source_images" not in text_only_properties
    assert {"prompt", "aspect_ratio", "resolution", "output_dir"} == set(text_only_properties)
    assert editing[0]["description"] == IMAGE_GENERATION_TOOL_DESCRIPTION
    assert "source_images" in editing_properties
    assert text_only == text_only_registry.provider_definitions(
        [IMAGE_GENERATION_TOOL_NAME],
        profile_context=profile_context,
    )


@pytest.mark.asyncio
async def test_image_generation_text_only_profile_rejects_source_images_in_handler(
    tmp_path: Path,
) -> None:
    service = _ImageService(
        tmp_path / "unused.png",
        supports_source_images=False,
    )
    registry = ToolRegistry()
    register_image_generation_tool(registry, service)
    definitions = registry.provider_definitions(
        [IMAGE_GENERATION_TOOL_NAME],
        profile_context=ToolDefinitionProfileContext(agent_id="agent-1"),
    )
    contract = registry.contracts_for_provider_definitions(definitions)[IMAGE_GENERATION_TOOL_NAME]

    result = await registry.dispatch(
        replace(_make_context(tmp_path), input_contract=contract),
        {"prompt": "make it rainy", "source_images": ["photo.png"]},
    )

    assert result["error"] == {
        "code": "invalid_arguments",
        "message": "source_images is unavailable for the configured image generation model",
    }
    assert service.received_prompt is None


@pytest.mark.asyncio
async def test_image_generation_tool_returns_model_file_facts(tmp_path: Path) -> None:
    image_path = tmp_path / "artifact-1.png"
    service = _ImageService(image_path)
    registry = ToolRegistry()
    register_image_generation_tool(registry, service)
    tool = registry.get(IMAGE_GENERATION_TOOL_NAME)
    assert "additionalProperties" not in tool.parameters
    assert "additionalProperties" not in IMAGE_GENERATION_TEXT_ONLY_TOOL_PARAMETERS
    assert tool.open_input_schema is True
    output_dir_parameter = tool.parameters["properties"]["output_dir"]
    assert output_dir_parameter == {
        "type": "string",
        "description": (
            "Directory to save generated images. Relative paths resolve from the working "
            "directory; missing directories are created. Omit when no specific destination "
            "is given."
        ),
    }
    assert "output_dir" not in tool.parameters["required"]
    context = _make_context(tmp_path)

    result = await registry.dispatch(context, {"prompt": "a red fox"})

    assert result["ok"] is True
    assert result["artifacts"] == []
    data = result["data"]
    assert isinstance(data, dict)
    # Model-facing data carries the path and useful file facts, without transport identity.
    assert data == {
        "images": [
            {
                "path": model_path(image_path),
                "media_type": "image/png",
                "size_bytes": 5,
            }
        ]
    }
    assert "configured external provider" in IMAGE_GENERATION_TOOL_DESCRIPTION
    assert "chat display links" not in IMAGE_GENERATION_TOOL_DESCRIPTION
    assert "chat display links" not in IMAGE_GENERATION_TEXT_ONLY_TOOL_DESCRIPTION
    display = registry.display_for_call(
        IMAGE_GENERATION_TOOL_NAME,
        {"prompt": "a red fox"},
        context=context,
        result=result,
    )
    assert display["facts"] == [{"kind": "count", "value": 1, "unit": "results", "at_least": False}]
    assert service.received_output_dirs == [tmp_path / "image-gen"]


@pytest.mark.asyncio
async def test_image_generation_output_directory_follows_agent_kind(tmp_path: Path) -> None:
    workspace = tmp_path / "identity-workspace"
    project_cwd = tmp_path / "project"
    service = _ImageService(tmp_path / "artifact.png")
    registry = ToolRegistry()
    register_image_generation_tool(registry, service)
    identity_context = replace(
        _make_context(tmp_path),
        workspace=workspace,
        cwd=project_cwd,
    )
    config_context = replace(identity_context, project_id="project-1")

    identity_result = await registry.dispatch(identity_context, {"prompt": "identity image"})
    config_result = await registry.dispatch(config_context, {"prompt": "project image"})

    assert identity_result["ok"] is True
    assert config_result["ok"] is True
    assert service.received_output_dirs == [
        workspace / "image-gen",
        project_cwd / "image-gen",
    ]


@pytest.mark.asyncio
async def test_image_generation_resolves_explicit_output_directory_from_working_directory(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "identity-workspace"
    project_cwd = tmp_path / "project"
    service = _ImageService(tmp_path / "artifact.png")
    registry = ToolRegistry()
    register_image_generation_tool(registry, service)
    context = replace(
        _make_context(tmp_path),
        workspace=workspace,
        cwd=project_cwd,
    )

    result = await registry.dispatch(
        context,
        {"prompt": "identity image", "output_dir": "assets/generated"},
    )

    assert result["ok"] is True
    assert service.received_output_dirs == [(project_cwd / "assets" / "generated").resolve()]


@pytest.mark.asyncio
async def test_image_generation_accepts_absolute_output_directory(tmp_path: Path) -> None:
    output_dir = tmp_path / "exports"
    service = _ImageService(tmp_path / "artifact.png")
    registry = ToolRegistry()
    register_image_generation_tool(registry, service)

    result = await registry.dispatch(
        _make_context(tmp_path),
        {"prompt": "export image", "output_dir": str(output_dir)},
    )

    assert result["ok"] is True
    assert service.received_output_dirs == [output_dir.resolve()]


@pytest.mark.asyncio
async def test_image_generation_rejects_blank_output_directory(tmp_path: Path) -> None:
    service = _ImageService(tmp_path / "artifact.png")
    registry = ToolRegistry()
    register_image_generation_tool(registry, service)

    result = await registry.dispatch(
        _make_context(tmp_path),
        {"prompt": "export image", "output_dir": "   "},
    )

    assert result["error"] == {
        "code": "invalid_arguments",
        "message": "output_dir must be a non-empty string when provided",
    }
    assert service.received_output_dirs == []


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

    assert result["error"] == {
        "code": "invalid_arguments",
        "message": "Unknown argument(s): unexpected",
    }


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
async def test_image_generation_tool_exposes_unknown_provider_outcome(tmp_path: Path) -> None:
    service = _ImageService(
        tmp_path / "unused.png",
        generation_error=ImageOutcomeUnknownError(
            "provider_outcome_unknown (operation_key=image-op): request may have completed",
            operation_key="image-op",
        ),
    )
    registry = ToolRegistry()
    register_image_generation_tool(registry, service)

    result = await registry.dispatch(_make_context(tmp_path), {"prompt": "a red fox"})

    assert result["error"]["code"] == "provider_outcome_unknown"
    assert result["error"]["retryable"] is False
    assert "operation_key=image-op" in result["error"]["message"]


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
async def test_image_generation_tool_rejects_empty_source_images(tmp_path: Path) -> None:
    service = _ImageService(tmp_path / "artifact-1.png")
    registry = ToolRegistry()
    register_image_generation_tool(registry, service)

    with pytest.raises(ToolContractError, match="non-empty"):
        await registry.dispatch(
            _make_context(tmp_path),
            {"prompt": "make it rainy", "source_images": []},
        )

    handler_result = await make_image_generation_handler(service)(
        _make_context(tmp_path),
        {"prompt": "make it rainy", "source_images": []},
    )
    assert handler_result["error"]["code"] == "invalid_arguments"
    assert "at least one" in handler_result["error"]["message"]
    assert service.received_source_paths is None


@pytest.mark.asyncio
async def test_image_generation_tool_rejects_invalid_source_paths_shape(tmp_path: Path) -> None:
    service = _ImageService(tmp_path / "artifact-1.png")
    registry = ToolRegistry()
    register_image_generation_tool(registry, service)

    with pytest.raises(ToolContractError, match="expected JSON string"):
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
    tool = registry.get(ANALYZE_IMAGE_TOOL_NAME)
    assert tool.parameters == ANALYZE_IMAGE_TOOL_PARAMETERS
    assert "additionalProperties" not in tool.parameters
    assert tool.open_input_schema is True

    context = replace(
        _make_context(workspace, tool_name=ANALYZE_IMAGE_TOOL_NAME),
        iteration_number=4,
    )
    result = await registry.dispatch(
        context,
        {
            "prompt": "Read the ingredients.",
            "images": ["first.png", str(second)],
        },
    )

    assert result["ok"] is True
    assert result["data"] == {
        "analysis": "Visible details",
        "image_count": 2,
    }
    assert service.received_analysis_prompt == "Read the ingredients."
    assert service.received_analysis_paths == (first.resolve(), second.resolve())
    assert service.received_analysis_run_context == ImageUnderstandingRunContext(
        run_id="run",
        agent_id="agent",
        session_id="session",
        iteration_number=4,
    )
    assert "untrusted content" in ANALYZE_IMAGE_TOOL_DESCRIPTION


@pytest.mark.parametrize(
    "invalid_data",
    [
        {},
        {
            "analysis": "Visible details",
            "image_count": 1,
            "model": "vision-model",
        },
    ],
)
def test_analyze_image_tool_has_closed_result_contract(
    tmp_path: Path,
    invalid_data: dict[str, object],
) -> None:
    registry = ToolRegistry()
    register_analyze_image_tool(registry, _ImageService(tmp_path / "unused.png"))
    tool = registry.get(ANALYZE_IMAGE_TOOL_NAME)

    assert tool.result_schema == {
        "type": "object",
        "properties": {
            "analysis": {"type": "string", "minLength": 1},
            "image_count": {"type": "integer", "minimum": 1},
        },
        "required": ["analysis", "image_count"],
        "additionalProperties": False,
    }
    with pytest.raises(InvalidToolResultError, match="violates its contract"):
        registry.validate_result(
            ANALYZE_IMAGE_TOOL_NAME,
            {"ok": True, "error": None, "data": invalid_data, "artifacts": []},
        )


@pytest.mark.asyncio
async def test_analyze_image_tool_accepts_single_path_string(tmp_path: Path) -> None:
    image = tmp_path / "photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nsource")
    service = _ImageService(tmp_path / "unused.png")
    registry = ToolRegistry()
    register_analyze_image_tool(registry, service)

    result = await registry.dispatch(
        _make_context(tmp_path, tool_name=ANALYZE_IMAGE_TOOL_NAME),
        {"prompt": "Describe it.", "images": "photo.png"},
    )

    assert result["ok"] is True
    assert service.received_analysis_paths == (image.resolve(),)


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
    unknown = await registry.dispatch(
        context,
        {"prompt": "Describe it.", "images": ["photo.png"], "extra": True},
    )
    assert unknown["error"] == {
        "code": "invalid_arguments",
        "message": "Unknown argument(s): extra",
    }
    image_error = await registry.dispatch(
        context,
        {"prompt": "Describe it.", "images": ["photo.png"]},
    )

    assert image_error["error"] == {
        "code": "image_read_error",
        "message": "bad image",
        "retryable": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("analysis_error", "expected_code"),
    [
        (ImageNotFoundError("missing"), "image_not_found"),
        (ImageReadError("unreadable"), "image_read_error"),
        (ImageTooLargeError("too large"), "image_too_large"),
        (ImageUnsupportedMediaTypeError("unsupported"), "unsupported_image_type"),
        (ImageUnderstandingUnavailableError("not configured"), "image_understanding_unavailable"),
    ],
)
async def test_analyze_image_tool_projects_stable_expected_error_codes(
    tmp_path: Path,
    analysis_error: ImageInputError | ImageUnderstandingUnavailableError,
    expected_code: str,
) -> None:
    registry = ToolRegistry()
    register_analyze_image_tool(
        registry,
        _ImageService(tmp_path / "unused.png", analysis_error=analysis_error),
    )

    result = await registry.dispatch(
        _make_context(tmp_path, tool_name=ANALYZE_IMAGE_TOOL_NAME),
        {"prompt": "Describe it.", "images": ["photo.png"]},
    )

    assert result["error"] == {
        "code": expected_code,
        "message": str(analysis_error),
        "retryable": False,
    }


@pytest.mark.asyncio
async def test_analyze_image_tool_preserves_provider_retry_metadata(tmp_path: Path) -> None:
    provider_error = ImageExecutionError(
        "rate limited",
        retryable=True,
        attempts_made=4,
    )
    registry = ToolRegistry()
    register_analyze_image_tool(
        registry,
        _ImageService(tmp_path / "unused.png", analysis_error=provider_error),
    )

    result = await registry.dispatch(
        _make_context(tmp_path, tool_name=ANALYZE_IMAGE_TOOL_NAME),
        {"prompt": "Describe it.", "images": ["photo.png"]},
    )

    assert result["error"] == {
        "code": "provider_error",
        "message": "rate limited",
        "retryable": True,
        "attempts_made": 4,
    }


@pytest.mark.asyncio
async def test_analyze_image_tool_does_not_mask_unexpected_failure(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_analyze_image_tool(
        registry,
        _ImageService(
            tmp_path / "unused.png",
            analysis_error=RuntimeError("implementation defect"),
        ),
    )

    with pytest.raises(RuntimeError, match="implementation defect"):
        await registry.dispatch(
            _make_context(tmp_path, tool_name=ANALYZE_IMAGE_TOOL_NAME),
            {"prompt": "Describe it.", "images": ["photo.png"]},
        )


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


class _ImageService:
    def __init__(
        self,
        file_path: Path,
        *,
        generation_error: Exception | None = None,
        analysis_error: Exception | None = None,
        supports_source_images: bool = True,
    ) -> None:
        self._file_path = file_path
        self._generation_error = generation_error
        self._analysis_error = analysis_error
        self._supports_source_images = supports_source_images
        self.received_prompt: str | None = None
        self.received_call_options: dict[str, object] | None = None
        self.received_source_paths: tuple[Path, ...] | None = None
        self.received_output_dirs: list[Path] = []
        self.received_analysis_prompt: str | None = None
        self.received_analysis_paths: tuple[Path, ...] | None = None
        self.received_analysis_run_context: ImageUnderstandingRunContext | None = None

    def generation_supports_source_images(self) -> bool:
        return self._supports_source_images

    async def generate_artifacts(
        self,
        prompt: str,
        *,
        output_dir: Path,
        call_options: dict[str, object] | None = None,
        source_paths: tuple[Path, ...] | None = None,
    ) -> tuple[object, ...]:
        self.received_prompt = prompt
        self.received_output_dirs.append(output_dir)
        self.received_call_options = call_options
        self.received_source_paths = source_paths
        if self._generation_error is not None:
            raise self._generation_error
        return (
            SimpleNamespace(
                file_path=self._file_path,
                media_type="image/png",
                size_bytes=5,
            ),
        )

    async def analyze(
        self,
        prompt: str,
        *,
        image_paths: tuple[Path, ...],
        run_context: ImageUnderstandingRunContext,
    ) -> object:
        self.received_analysis_prompt = prompt
        self.received_analysis_paths = image_paths
        self.received_analysis_run_context = run_context
        if self._analysis_error is not None:
            raise self._analysis_error
        return SimpleNamespace(
            content="Visible details",
            model="vision-model",
            image_count=len(image_paths),
            usage={"input_tokens": 12, "output_tokens": 7},
        )
