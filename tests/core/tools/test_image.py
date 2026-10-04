"""image_generation: the profile the configured model allows, the files it returns, where
they go, source images, and what refused or failed calls say."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from core.model_tasks import (
    ImageConfigurationError,
    ImageExecutionError,
    ImageOptionError,
    ImageOutcomeUnknownError,
    ImageRefusedError,
)
from core.model_tasks.artifacts import OutputDirectoryError, OutputWriteError
from core.model_tasks.image_profile import ImageProfile
from core.tools.image import (
    IMAGE_GENERATION_TEXT_ONLY_TOOL_DESCRIPTION,
    IMAGE_GENERATION_TOOL_DESCRIPTION,
    IMAGE_GENERATION_TOOL_NAME,
)
from core.tools.tools import ToolDefinitionProfileContext, ToolRegistry
from core.utils.errors import ProviderError
from core.utils.paths import model_path
from tests.core.tools.image_test_support import (
    TEXT_ONLY_PROFILE,
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


@pytest.mark.parametrize(
    ("profile", "description", "offered"),
    [
        pytest.param(TEXT_ONLY_PROFILE, IMAGE_GENERATION_TEXT_ONLY_TOOL_DESCRIPTION, {}, id="text"),
        pytest.param(
            ImageProfile(
                wire="openai_images",
                call_choices={"aspect_ratio": ("1:1", "16:9"), "background": ("transparent",)},
                max_source_images=16,
            ),
            IMAGE_GENERATION_TOOL_DESCRIPTION,
            {
                "source_images": {"maxItems": 16},
                "aspect_ratio": {"enum": ["1:1", "16:9"]},
                "background": {"enum": ["transparent"]},
            },
            id="editing-with-choices",
        ),
    ],
)
def test_profile_offers_what_the_configured_model_takes(
    profile: ImageProfile, description: str, offered: dict[str, dict[str, Any]]
) -> None:
    registry = image_registry(ImageService(profile=profile))
    definition = _definition(registry)
    parameters = definition["parameters"]

    assert definition == _definition(registry)
    assert definition["description"] == description
    assert set(parameters["properties"]) == {"prompt", "output_dir", *offered}
    for name, facts in offered.items():
        assert facts.items() <= parameters["properties"][name].items()
    assert "additionalProperties" not in parameters
    assert parameters["required"] == ["prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("revised_prompt", [None, "An original red fox in deep snow"])
async def test_generated_images_are_returned_as_local_file_facts(
    tmp_path: Path, revised_prompt: str | None
) -> None:
    service = ImageService(image_path=tmp_path / "artifact-1.png", revised_prompt=revised_prompt)
    registry = image_registry(service)
    context = make_context(tmp_path, IMAGE_GENERATION_TOOL_NAME)

    result = await dispatch_as_executor(registry, context, {"prompt": "a red fox"})

    # Model-facing data carries the path and useful file facts, without transport identity,
    # plus the prompt the provider actually rendered when it rewrote the request.
    image: dict[str, Any] = {
        "path": model_path(tmp_path / "artifact-1.png"),
        "media_type": "image/png",
        "size_bytes": 5,
        "width": 1024,
        "height": 576,
    }
    if revised_prompt is not None:
        image["revised_prompt"] = revised_prompt
    assert result == {"ok": True, "error": None, "data": {"images": [image]}, "artifacts": []}
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
            {
                "aspect_ratio": "16:9",
                "resolution": "4K",
                "background": "transparent",
                "source_images": ["photo.png"],
            },
            {"aspect_ratio": "16:9", "resolution": "4K", "background": "transparent"},
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
                "resolution, background, output_dir."
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
    service = ImageService(profile=TEXT_ONLY_PROFILE)
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
        "image_generation parameters: prompt (required), output_dir."
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
                "The request to the image-generation provider ended without a usable answer, "
                "so it is unknown whether it created the image. Nothing was saved. Repeating "
                "the request can create and charge a second image. If you still need it, "
                "repeat the call once, and tell the user if that fails too.",
            ),
            id="outcome-unknown",
        ),
        pytest.param(
            ImageRefusedError("Your request was rejected by the safety system"),
            failure(
                "generation_refused",
                "The image-generation provider refused the request and created nothing. Its "
                "reason: Your request was rejected by the safety system. Repeating the "
                "unchanged request gets the same refusal. Change what the prompt asks for, for "
                "example an original design instead of a named character, brand or real "
                "person, or tell the user.",
            ),
            id="refused",
        ),
        pytest.param(
            ImageRefusedError(None),
            failure(
                "generation_refused",
                "The image-generation provider refused the request and created nothing. It "
                "gave no reason, which most often means its content policy blocked the "
                "request. Repeating the unchanged request gets the same refusal. Change what "
                "the prompt asks for, for example an original design instead of a named "
                "character, brand or real person, or tell the user.",
            ),
            id="refused-without-reason",
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
        pytest.param(
            ImageOptionError(
                "aspect_ratio '5:4' is not offered by the configured image model; choose one "
                "of: 1:1, 16:9."
            ),
            failure(
                "invalid_arguments",
                "aspect_ratio '5:4' is not offered by the configured image model; choose one "
                "of: 1:1, 16:9.",
            ),
            id="option-not-offered",
        ),
        pytest.param(
            OutputDirectoryError(Path("notes.txt"), "a file with that name exists"),
            failure(
                "output_dir_unusable",
                "Cannot use notes.txt as the output folder: a file with that name exists. "
                "Nothing was generated. Pass another output_dir, or omit output_dir to use the "
                "default folder.",
            ),
            id="unusable-folder",
        ),
        pytest.param(
            OutputWriteError(Path("full-disk"), "No space left on device"),
            failure(
                "output_write_failed",
                "Generation succeeded, but the images could not be saved in full-disk: No "
                "space left on device. The provider charged for this request. Tell the user; "
                "repeating the call generates and charges again.",
            ),
            id="save-failed",
        ),
    ],
)
async def test_generation_failures_say_what_happened_and_what_to_do(
    tmp_path: Path, error: Exception, expected: dict[str, Any]
) -> None:
    result = await generate(tmp_path, {"prompt": "a red bicycle"}, ImageService(error=error))

    assert result["error"] == expected
