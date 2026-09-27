"""analyze_image with local images: the calls it reads in other dialects, the images it
cannot use, what failed analyses say, and what the row keeps."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.model_tasks import (
    ImageExecutionError,
    ImageInputError,
    ImageNotFoundError,
    ImageTooLargeError,
    ImageUnderstandingRunContext,
    ImageUnderstandingUnavailableError,
    ImageUnsupportedMediaTypeError,
)
from core.providers.errors import NetworkError, ProviderAuthError, ProviderRateLimitError
from core.tools import InvalidToolResultError
from core.tools.image import ANALYZE_IMAGE_TOOL_NAME
from core.utils.errors import ProviderError
from tests.core.tools.image_test_support import (
    ANALYSIS,
    PNG,
    ImageService,
    analyze,
    contract_refusal,
    dispatch,
    failure,
    image_registry,
    make_context,
    write_image,
)


@pytest.fixture
def photos(tmp_path: Path) -> Path:
    """A Workspace with photos/cat.png, photos/garden.jpg and a text file."""
    write_image(tmp_path / "photos" / "cat.png")
    write_image(tmp_path / "photos" / "garden.jpg")
    (tmp_path / "notes.txt").write_text("text", encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_analysis_reads_the_named_images_for_this_run(photos: Path) -> None:
    service = ImageService()

    result = await analyze(
        photos,
        {
            "prompt": "Read the ingredients.",
            "images": ["photos/cat.png", str(photos / "photos" / "garden.jpg")],
        },
        service,
        iteration_number=4,
    )

    assert result == {"ok": True, "error": None, "data": {"analysis": ANALYSIS}, "artifacts": []}
    assert service.analyzed == {
        "prompt": "Read the ingredients.",
        "paths": (
            (photos / "photos" / "cat.png").resolve(),
            (photos / "photos" / "garden.jpg").resolve(),
        ),
        "contents": [PNG, PNG],
        "run_context": ImageUnderstandingRunContext(
            run_id="run", agent_id="agent", session_id="session", iteration_number=4
        ),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"prompt": "What animal?", "images": "photos/cat.png"},
        {"prompt": "What animal?", "image": "photos/cat.png"},
        {"question": "What animal?", "image_path": "photos/cat.png"},
        {"query": "What animal?", "imageUrl": "photos/cat.png"},
        {"prompt": "What animal?", "path": "photos/cat.png"},
        {"prompt": "What animal?", "file_paths": ["photos/cat.png"]},
        {"prompt": "What animal?", "images": '["photos/cat.png"]'},
        {"prompt": "What animal?", "images": [{"path": "photos/cat.png"}]},
        {
            "prompt": "What animal?",
            "images": [{"type": "image_url", "image_url": {"url": "photos/cat.png"}}],
        },
        {"prompt": "What animal?", "images": ["photos/cat.png"], "image": "photos/cat.png"},
        # A file URL keeps its drive letter on Windows.
        {"prompt": "What animal?", "image_url": "<cat.png as file URL>"},
    ],
)
async def test_other_image_dialects_analyze_the_same_file(
    photos: Path, arguments: dict[str, Any]
) -> None:
    cat = (photos / "photos" / "cat.png").resolve()
    arguments = {
        name: cat.as_uri() if value == "<cat.png as file URL>" else value
        for name, value in arguments.items()
    }
    service = ImageService()

    result = await analyze(photos, arguments, service)

    assert result["data"] == {"analysis": ANALYSIS}
    assert service.analyzed is not None
    assert (service.analyzed["prompt"], service.analyzed["paths"]) == ("What animal?", (cat,))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        pytest.param(
            {"prompt": "a", "image": "photos/cat.png", "images": ["photos/garden.jpg"]},
            contract_refusal(
                'Conflicting values for images: image is "photos/cat.png" and images is '
                '["photos/garden.jpg"]. Send only the intended one.'
            ),
            id="two-image-lists",
        ),
        pytest.param(
            {"prompt": "a", "question": "b", "images": ["photos/cat.png"]},
            contract_refusal(
                'Conflicting values for prompt: prompt is "a" and question is "b". Send only '
                "the intended one."
            ),
            id="two-prompts",
        ),
        pytest.param(
            {"images": ["photos/cat.png"]},
            contract_refusal(
                "analyze_image was not run:\n"
                '- "prompt" is required: What to inspect or extract, including the needed '
                "detail or uncertainty.\n"
                "analyze_image parameters: prompt (required), images (required)."
            ),
            id="no-prompt",
        ),
        pytest.param(
            {"prompt": "   ", "images": ["photos/cat.png"]},
            failure(
                "invalid_arguments",
                'Pass what to look for as prompt, for example {"prompt": "Read the text on '
                'the label"}.',
            ),
            id="blank-prompt",
        ),
        pytest.param(
            {"prompt": "a", "images": []},
            contract_refusal('analyze_image was not run: "images" must not be empty.'),
            id="no-images",
        ),
        pytest.param(
            {"prompt": "a", "images": ["photos/cat.png"], "extra": True},
            contract_refusal(
                "analyze_image was not run:\n"
                '- "extra" is not a parameter.\n'
                "analyze_image parameters: prompt (required), images (required)."
            ),
            id="unknown-argument",
        ),
    ],
)
async def test_unclear_calls_are_refused_before_analysis(
    photos: Path, arguments: dict[str, Any], error: dict[str, Any]
) -> None:
    service = ImageService()

    result = await analyze(photos, arguments, service)

    assert result["error"] == error
    assert service.analyzed is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("images", "error"),
    [
        pytest.param(
            ["photos/cat.jpg"],
            failure(
                "image_not_found",
                "No image at photos/cat.jpg (similar: photos/cat.png).\n"
                'If you meant that file, pass {"images": ["photos/cat.png"]}.',
            ),
            id="similar-file",
        ),
        pytest.param(
            ["photos/cat.jpg", "photos/garden.jpg", "photo/garden.jpeg"],
            failure(
                "image_not_found",
                "No image at photos/cat.jpg (similar: photos/cat.png).\n"
                "No image at photo/garden.jpeg (similar: photos/garden.jpg).\n"
                'If you meant those files, pass {"images": ["photos/cat.png", '
                '"photos/garden.jpg", "photos/garden.jpg"]}.',
            ),
            id="similar-files",
        ),
        pytest.param(
            ["photos/cat.jpg", "photos/zebra.png"],
            failure(
                "image_not_found",
                "No image at photos/cat.jpg (similar: photos/cat.png).\n"
                "No image at photos/zebra.png, and no similar file is beside it.",
            ),
            id="no-similar-file",
        ),
        pytest.param(
            ["photos"],
            failure(
                "image_read_error",
                "photos is a folder, not an image. Pass image files from it, for example "
                '{"images": ["photos/cat.png", "photos/garden.jpg"]}.',
            ),
            id="folder",
        ),
    ],
)
async def test_unusable_local_images_name_the_files_meant_without_substituting(
    photos: Path, images: list[str], error: dict[str, Any]
) -> None:
    service = ImageService()
    context = make_context(photos, ANALYZE_IMAGE_TOOL_NAME)

    result = await dispatch(
        image_registry(service), context, {"prompt": "Describe", "images": images}
    )

    assert result["error"] == error
    assert service.analyzed is None
    # The row still shows an unavailable-image placeholder for each requested path.
    assert [item["filename"] for item in context.presentation_images] == [
        Path(item).name for item in images
    ]


def _provider_failure(
    cause: Exception, status: int | None = None, *, retryable: bool = False
) -> ImageExecutionError:
    if status is not None:
        cause.status_code = status  # type: ignore[attr-defined]
    try:
        raise ImageExecutionError(str(cause), retryable=retryable) from cause
    except ImageExecutionError as error:
        return error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            ImageNotFoundError("missing"), failure("image_not_found", "missing"), id="not-found"
        ),
        pytest.param(
            ImageInputError("bad image"), failure("image_read_error", "bad image"), id="bad-input"
        ),
        pytest.param(
            ImageTooLargeError("too large"), failure("image_too_large", "too large"), id="too-large"
        ),
        pytest.param(
            ImageUnsupportedMediaTypeError("unsupported"),
            failure("unsupported_image_type", "unsupported"),
            id="unsupported-type",
        ),
        pytest.param(
            ImageUnderstandingUnavailableError("not configured"),
            failure(
                "image_understanding_unavailable",
                "Image understanding is not available (not configured). Tell the user to "
                "choose a working Image understanding model in Settings under Specialized "
                "Models.",
            ),
            id="unavailable",
        ),
        pytest.param(
            ImageExecutionError("rate limited", retryable=True, attempts_made=4),
            failure(
                "provider_error",
                "The image-understanding provider failed (rate limited). Try again later.",
                retryable=True,
                attempts_made=4,
            ),
            id="retries-spent",
        ),
        pytest.param(
            _provider_failure(
                ProviderAuthError(
                    'Authentication error: 401 {"error": {"message": "Provided '
                    'authentication token is expired."}}'
                ),
                401,
            ),
            failure(
                "provider_error",
                "The image-understanding provider rejected its credentials (HTTP 401: Provided "
                "authentication token is expired.). Tell the user to check that provider's API "
                "key or sign-in in Settings.",
            ),
            id="credentials",
        ),
        pytest.param(
            _provider_failure(
                ProviderRateLimitError(
                    'Rate limited: 429 {"error":{"type":"usage_limit_reached",'
                    '"message":"The usage limit has been reached"}}'
                ),
                429,
                retryable=True,
            ),
            failure(
                "provider_error",
                "The image-understanding provider is limiting requests or its usage limit is "
                "reached (HTTP 429: The usage limit has been reached). Wait before trying "
                "again, and tell the user if it keeps happening.",
                retryable=True,
            ),
            id="rate-limit",
        ),
        pytest.param(
            _provider_failure(NetworkError("Connection reset by peer"), retryable=True),
            failure(
                "provider_error",
                "The image-understanding provider did not answer (Connection reset by peer). "
                "Try again later.",
                retryable=True,
            ),
            id="no-answer",
        ),
        pytest.param(
            _provider_failure(
                ProviderError(
                    "Provider error: 400 <html><head><title>Bad Request</title></head>"
                    "<body>...</body></html>",
                    retryable=False,
                ),
                400,
            ),
            failure(
                "provider_error",
                "The image-understanding provider rejected the request (HTTP 400: Bad "
                "Request). If the reason concerns the request, change it; otherwise tell the "
                "user, who may need to choose another Image understanding model in Settings "
                "under Specialized Models.",
            ),
            id="request-rejected",
        ),
    ],
)
async def test_failed_analyses_say_what_happened_and_what_to_do(
    photos: Path, error: Exception, expected: dict[str, Any]
) -> None:
    result = await analyze(
        photos, {"prompt": "Describe", "images": ["photos/cat.png"]}, ImageService(error=error)
    )

    assert result["error"] == expected


@pytest.mark.asyncio
async def test_an_unexpected_failure_is_not_masked(photos: Path) -> None:
    service = ImageService(error=RuntimeError("implementation defect"))

    with pytest.raises(RuntimeError, match="implementation defect"):
        await analyze(photos, {"prompt": "Describe", "images": ["photos/cat.png"]}, service)


def test_results_carry_only_the_analysis() -> None:
    registry = image_registry(ImageService())

    with pytest.raises(InvalidToolResultError):
        registry.validate_result(
            ANALYZE_IMAGE_TOOL_NAME,
            {
                "ok": True,
                "error": None,
                "data": {"analysis": ANALYSIS, "model": "vision-model"},
                "artifacts": [],
            },
        )


def test_display_shows_the_request_under_any_spelling() -> None:
    registry = image_registry(ImageService())

    display = registry.display_for_call(
        ANALYZE_IMAGE_TOOL_NAME, {"question": "What animal?", "image": "cat.png"}
    )

    assert [part["value"] for part in display["primary"]] == ["What animal?"]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_the_row_keeps_only_the_original_paths_without_writing_files(
    tmp_path: Path, missing: bool
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    image = project / "original image.png"
    if not missing:
        write_image(image)
    before = set(tmp_path.rglob("*"))
    registry = image_registry(ImageService())
    context = make_context(tmp_path, ANALYZE_IMAGE_TOOL_NAME, cwd=project)
    arguments = {"prompt": "Inspect", "images": [image.name]}

    result = await dispatch(registry, context, arguments)
    display = registry.display_for_call(
        ANALYZE_IMAGE_TOOL_NAME, arguments, context=context, result=result
    )
    restored = ChatMessage.from_dict(
        ChatMessage.tool(
            tool_call_id="call",
            name=ANALYZE_IMAGE_TOOL_NAME,
            content=json.dumps(result),
            tool_display=display,
        ).to_dict()
    )

    assert result["ok"] is not missing
    assert restored.tool_display is not None
    assert restored.tool_display["image_files"] == [{"path": str(image), "filename": image.name}]
    assert result["artifacts"] == []
    assert set(tmp_path.rglob("*")) == before
