"""image_generation: the profile the configured model allows, the files it returns, where
they go, source images, and what refused or failed calls say."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from core.model_tasks import ImageConfigurationError, ImageExecutionError, ImageOutcomeUnknownError
from core.tools.image import (
    IMAGE_GENERATION_TEXT_ONLY_TOOL_DESCRIPTION,
    IMAGE_GENERATION_TOOL_DESCRIPTION,
    IMAGE_GENERATION_TOOL_NAME,
)
from core.tools.tools import ToolDefinitionProfileContext, ToolRegistry
from core.utils.errors import ProviderError
from core.utils.paths import model_path
from tests.core.tools.image_test_support import (
    ImageService,
    contract_refusal,
    failure,
    generate,
    image_registry,
    make_context,
    write_image,
)
from tests.core.tools.tools_test_support import dispatch_as_executor

_TEXT_ONLY_REFUSAL = (
    "The configured image model only generates from text, so it cannot use source_images. "
    "Remove source_images, or ask the user to choose an Image generation model that accepts "
    "images in Settings → Tools → Images, video & music."
)


def _definition(registry: ToolRegistry) -> dict[str, Any]:
    [definition] = registry.provider_definitions(
        [IMAGE_GENERATION_TOOL_NAME],
        profile_context=ToolDefinitionProfileContext(agent_id="agent"),
    )
    return definition


def test_profile_offers_source_images_only_to_a_model_that_edits() -> None:
    text_only_registry = image_registry(ImageService(supports_source_images=False))
    text_only = _definition(text_only_registry)
    editing = _definition(image_registry(ImageService(supports_source_images=True)))

    assert text_only == _definition(text_only_registry)
    assert text_only["description"] == IMAGE_GENERATION_TEXT_ONLY_TOOL_DESCRIPTION
    assert editing["description"] == IMAGE_GENERATION_TOOL_DESCRIPTION
    fields = {"prompt", "aspect_ratio", "resolution", "output_dir"}
    assert set(text_only["parameters"]["properties"]) == fields
    assert set(editing["parameters"]["properties"]) == fields | {"source_images"}
    for parameters in (text_only["parameters"], editing["parameters"]):
        assert "additionalProperties" not in parameters
        assert parameters["required"] == ["prompt"]
        assert parameters["properties"]["output_dir"]["description"]


@pytest.mark.asyncio
async def test_generated_images_are_returned_as_local_file_facts(tmp_path: Path) -> None:
    service = ImageService(image_path=tmp_path / "artifact-1.png")
    registry = image_registry(service)
    context = make_context(tmp_path, IMAGE_GENERATION_TOOL_NAME)

    result = await dispatch_as_executor(registry, context, {"prompt": "a red fox"})

    # Model-facing data carries the path and useful file facts, without transport identity.
    assert result == {
        "ok": True,
        "error": None,
        "data": {
            "images": [
                {
                    "path": model_path(tmp_path / "artifact-1.png"),
                    "media_type": "image/png",
                    "size_bytes": 5,
                }
            ]
        },
        "artifacts": [],
    }
    display = registry.display_for_call(
        IMAGE_GENERATION_TOOL_NAME, {"prompt": "a red fox"}, context=context, result=result
    )
    assert display["facts"] == [{"kind": "count", "value": 1, "unit": "results", "at_least": False}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("project_id", "output_dir", "expected"),
    [
        pytest.param(None, None, "workspace/image-gen", id="identity-default"),
        pytest.param("project-1", None, "project/image-gen", id="project-default"),
        pytest.param(None, "   ", "workspace/image-gen", id="blank"),
        pytest.param(None, "assets/generated", "project/assets/generated", id="relative"),
        pytest.param(None, "<tmp>/exports", "exports", id="absolute"),
    ],
)
async def test_images_go_to_the_callers_directory(
    tmp_path: Path, project_id: str | None, output_dir: str | None, expected: str
) -> None:
    service = ImageService()
    arguments: dict[str, Any] = {"prompt": "a red fox"}
    if output_dir is not None:
        arguments["output_dir"] = output_dir.replace("<tmp>", str(tmp_path))

    result = await generate(
        tmp_path / "workspace",
        arguments,
        service,
        cwd=tmp_path / "project",
        project_id=project_id,
    )

    assert result["ok"] is True
    assert service.output_dirs == [(tmp_path / expected).resolve()]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "call_options", "sources"),
    [
        pytest.param(
            {"aspect_ratio": "16:9", "resolution": "4K", "source_images": ["photo.png"]},
            {"aspect_ratio": "16:9", "resolution": "4K"},
            ("photo.png",),
            id="canonical",
        ),
        pytest.param({"source_images": "<root>/photo.png"}, {}, ("photo.png",), id="one-path"),
        pytest.param(
            {"source_images": {"path": "photo.png"}}, {}, ("photo.png",), id="one-path-object"
        ),
        pytest.param({"input_image": "photo.png"}, {}, ("photo.png",), id="other-spelling"),
    ],
)
async def test_the_call_reaches_the_image_model(
    tmp_path: Path,
    arguments: dict[str, Any],
    call_options: dict[str, str],
    sources: tuple[str, ...],
) -> None:
    write_image(tmp_path / "photo.png")
    service = ImageService()
    arguments = {
        name: value.replace("<root>", str(tmp_path)) if isinstance(value, str) else value
        for name, value in arguments.items()
    }

    result = await generate(tmp_path, {"prompt": "make it rainy", **arguments}, service)

    assert result["ok"] is True
    assert service.generated == {
        "prompt": "make it rainy",
        "call_options": call_options,
        "source_paths": tuple((tmp_path / name).resolve() for name in sources),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        pytest.param(
            {"prompt": "   "},
            failure(
                "invalid_arguments",
                'Describe the image as prompt, for example {"prompt": "A red bicycle against '
                'a white wall, studio photograph"}.',
            ),
            id="blank-prompt",
        ),
        pytest.param(
            {"prompt": "a", "unexpected": True},
            contract_refusal(
                "image_generation was not run:\n"
                '- "unexpected" is not a parameter.\n'
                "image_generation parameters: prompt (required), source_images, aspect_ratio, "
                "resolution, output_dir."
            ),
            id="unknown-argument",
        ),
        pytest.param(
            {"prompt": "a", "aspect_ratio": "  ", "resolution": ""},
            contract_refusal(
                r'image_generation was not run: "aspect_ratio" must match the pattern .*\S.*; '
                'received "  ".'
            ),
            id="blank-options",
        ),
        pytest.param(
            {"prompt": "a", "source_images": []},
            contract_refusal('image_generation was not run: "source_images" must not be empty.'),
            id="no-source-images",
        ),
        pytest.param(
            {"prompt": "a", "source_images": {"path": "a.png", "url": "b.png"}},
            contract_refusal(
                'image_generation was not run: "source_images[0]" must be a string; received '
                "an object."
            ),
            id="two-paths-in-one-object",
        ),
        pytest.param(
            {"prompt": "a", "source_images": ["photo.jpg"]},
            failure(
                "image_not_found",
                "No image at photo.jpg (similar: photo.png).\n"
                'If you meant that file, pass {"source_images": ["photo.png"]}.',
            ),
            id="missing-source",
        ),
        *(
            pytest.param(
                {"prompt": "a", "source_images": [address]},
                failure(
                    "invalid_arguments",
                    f"source_images must be local image files; {reason} cannot be opened. Save "
                    "the image to a file first, then pass that file's path.",
                ),
                id=f"web-address-{index}",
            )
            for index, (address, reason) in enumerate(
                [
                    (
                        "https://example.com/cat.png",
                        "web addresses such as https://example.com/cat.png",
                    ),
                    (
                        "<https:/example.com/cat.png>",
                        "web addresses such as https://example.com/cat.png",
                    ),
                    ("data:image/png;base64,iVBORw0KGgo=", "data: URLs"),
                ]
            )
        ),
        pytest.param(
            {"prompt": "a", "source_images": ["file://fileserver.example/share/cat.png"]},
            failure(
                "invalid_arguments",
                "source_images must be local image files; web addresses such as "
                "file://fileserver.example/share/cat.png cannot be opened. Save the image to a "
                "file first, then pass that file's path.",
            ),
            marks=pytest.mark.skipif(sys.platform == "win32", reason="a UNC path on Windows"),
            id="file-url-of-another-computer",
        ),
    ],
)
async def test_unusable_calls_are_refused_before_generating(
    tmp_path: Path, arguments: dict[str, Any], error: dict[str, Any]
) -> None:
    write_image(tmp_path / "photo.png")
    service = ImageService()
    context = make_context(tmp_path, IMAGE_GENERATION_TOOL_NAME)

    result = await dispatch_as_executor(image_registry(service), context, arguments)

    assert result["ok"] is False
    assert result["error"] == error
    assert service.output_dirs == []
    # Only a missing local file leaves an unavailable-image placeholder in the row,
    # shown as an image the call started from.
    shown = [("photo.jpg", "source")] if error["code"] == "image_not_found" else []
    assert [(item["filename"], item.get("role")) for item in context.presentation_media] == shown


@pytest.mark.asyncio
async def test_a_text_only_model_refuses_source_images(tmp_path: Path) -> None:
    service = ImageService(supports_source_images=False)
    registry = image_registry(service)
    contract = registry.contracts_for_provider_definitions([_definition(registry)])[
        IMAGE_GENERATION_TOOL_NAME
    ]
    context = make_context(tmp_path, IMAGE_GENERATION_TOOL_NAME)
    arguments = {"prompt": "make it rainy", "source_images": ["photo.png"]}

    # The profile contract refuses the field; without it, the Tool still refuses.
    offered = await dispatch_as_executor(
        registry, replace(context, input_contract=contract), arguments
    )
    unoffered = await dispatch_as_executor(registry, context, arguments)

    assert offered["error"] == contract_refusal(
        "image_generation was not run:\n"
        '- "source_images" is not a parameter.\n'
        "image_generation parameters: prompt (required), aspect_ratio, resolution, output_dir."
    )
    assert unoffered["error"] == failure("invalid_arguments", _TEXT_ONLY_REFUSAL)
    assert service.output_dirs == []


def _provider_failure(cause: Exception, status: int) -> ImageExecutionError:
    cause.status_code = status  # type: ignore[attr-defined]
    try:
        raise ImageExecutionError(str(cause), retryable=False) from cause
    except ImageExecutionError as error:
        return error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            ImageOutcomeUnknownError(
                "provider_outcome_unknown (operation_key=image-op): request may have completed",
                operation_key="image-op",
            ),
            failure(
                "provider_outcome_unknown",
                "provider_outcome_unknown (operation_key=image-op): request may have completed",
            ),
            id="outcome-unknown",
        ),
        pytest.param(
            ImageConfigurationError("No task model configured for image_generation"),
            failure(
                "image_error",
                "Image generation is not available (No task model configured for "
                "image_generation). Tell the user to choose a working Image generation model "
                "in Settings → Tools → Images, video & music.",
            ),
            id="not-configured",
        ),
        pytest.param(
            _provider_failure(
                ProviderError(
                    'Provider error: 400 {"error":{"message":"Invalid background_hex_color '
                    '\\"\\": expected a #RRGGBB value"}}',
                    retryable=False,
                ),
                400,
            ),
            failure(
                "provider_error",
                "The image-generation provider rejected the request (HTTP 400: Invalid "
                'background_hex_color "": expected a #RRGGBB value). If the reason concerns '
                "the request, change it; otherwise tell the user, who may need to choose "
                "another Image generation model in Settings → Tools → Images, video & music.",
            ),
            id="provider-rejection",
        ),
    ],
)
async def test_generation_failures_say_what_happened_and_what_to_do(
    tmp_path: Path, error: Exception, expected: dict[str, Any]
) -> None:
    result = await generate(tmp_path, {"prompt": "a red bicycle"}, ImageService(error=error))

    assert result["error"] == expected
