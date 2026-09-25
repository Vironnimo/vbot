"""The image Tools run clear calls in other dialects and explain unusable images."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.model_tasks import (
    ImageConfigurationError,
    ImageExecutionError,
    ImageUnderstandingRunContext,
)
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderRateLimitError,
)
from core.tools import ToolContractError
from core.tools.image import (
    ANALYZE_IMAGE_TOOL_NAME,
    IMAGE_GENERATION_TOOL_NAME,
    register_analyze_image_tool,
    register_image_generation_tool,
)
from core.tools.tools import ToolContext, ToolRegistry
from core.utils.errors import ProviderError


class _Service:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.analyzed: tuple[str, tuple[Path, ...]] | None = None
        self.generated: tuple[str, tuple[Path, ...]] | None = None

    def generation_supports_source_images(self) -> bool:
        return True

    async def analyze(
        self,
        prompt: str,
        *,
        image_paths: tuple[Path, ...],
        run_context: ImageUnderstandingRunContext,
    ) -> object:
        self.analyzed = (prompt, image_paths)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(content="A cat on a sofa.")

    async def generate_artifacts(
        self,
        prompt: str,
        *,
        output_dir: Path,
        call_options: dict[str, object] | None = None,
        source_paths: tuple[Path, ...] = (),
    ) -> tuple[object, ...]:
        self.generated = (prompt, source_paths)
        if self.error is not None:
            raise self.error
        return (
            SimpleNamespace(file_path=output_dir / "a.png", media_type="image/png", size_bytes=3),
        )


def _context(root: Path, tool_name: str = ANALYZE_IMAGE_TOOL_NAME) -> ToolContext:
    return ToolContext(
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


@pytest.fixture
def photos(tmp_path: Path) -> Path:
    (tmp_path / "photos").mkdir()
    for name in ("cat.png", "garden.jpg"):
        (tmp_path / "photos" / name).write_bytes(b"\x89PNG\r\n\x1a\nimage")
    (tmp_path / "notes.txt").write_text("text", encoding="utf-8")
    return tmp_path


async def analyze(root: Path, arguments: Any, service: _Service | None = None) -> dict[str, Any]:
    registry = ToolRegistry()
    register_analyze_image_tool(registry, service or _Service())
    return await registry.dispatch(_context(root), arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
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
    ],
)
async def test_other_image_dialects_analyze_the_same_file(
    photos: Path, arguments: dict[str, Any]
) -> None:
    service = _Service()

    result = await analyze(photos, arguments, service)

    assert result["data"] == {"analysis": "A cat on a sofa."}
    assert service.analyzed == ("What animal?", ((photos / "photos" / "cat.png").resolve(),))


@pytest.mark.asyncio
async def test_a_file_url_names_the_local_file(photos: Path) -> None:
    service = _Service()
    url = (photos / "photos" / "cat.png").resolve().as_uri()

    result = await analyze(photos, {"prompt": "What animal?", "image_url": url}, service)

    assert result["ok"] is True
    assert service.analyzed is not None
    assert service.analyzed[1] == ((photos / "photos" / "cat.png").resolve(),)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {"prompt": "a", "image": "photos/cat.png", "images": ["photos/garden.jpg"]},
            "Conflicting values for images",
        ),
        ({"prompt": "a", "question": "b", "images": ["photos/cat.png"]}, "Conflicting values"),
        ({"images": ["photos/cat.png"]}, '"prompt" is required'),
    ],
)
async def test_unclear_calls_are_refused_before_analysis(
    photos: Path, arguments: dict[str, Any], message: str
) -> None:
    service = _Service()

    with pytest.raises(ToolContractError) as error:
        await analyze(photos, arguments, service)

    assert message in str(error.value)
    assert service.analyzed is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("images", "message"),
    [
        (
            ["photos/cat.jpg"],
            "No image at photos/cat.jpg (similar: photos/cat.png).\n"
            'If you meant that file, pass {"images": ["photos/cat.png"]}.',
        ),
        (
            ["photos/cat.jpg", "photos/garden.jpg", "photo/garden.jpeg"],
            "No image at photos/cat.jpg (similar: photos/cat.png).\n"
            "No image at photo/garden.jpeg (similar: photos/garden.jpg).\n"
            'If you meant those files, pass {"images": ["photos/cat.png", '
            '"photos/garden.jpg", "photos/garden.jpg"]}.',
        ),
        (
            ["photos/cat.jpg", "photos/zebra.png"],
            "No image at photos/cat.jpg (similar: photos/cat.png).\n"
            "No image at photos/zebra.png, and no similar file is beside it.",
        ),
    ],
)
async def test_missing_images_name_similar_files_without_substituting(
    photos: Path, images: list[str], message: str
) -> None:
    service = _Service()
    registry = ToolRegistry()
    register_analyze_image_tool(registry, service)
    context = _context(photos)

    result = await registry.dispatch(context, {"prompt": "Describe", "images": images})

    assert result["error"] == {"code": "image_not_found", "message": message, "retryable": False}
    assert service.analyzed is None
    # The row still shows an unavailable-image placeholder for each requested path.
    assert [item["filename"] for item in context.presentation_images] == [
        Path(item).name for item in images
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("image", "message"),
    [
        (
            "https://example.com/cat.png",
            "images must be local image files; web addresses such as "
            "https://example.com/cat.png cannot be opened. Save the image to a file first, "
            "then pass that file's path.",
        ),
        (
            "data:image/png;base64,iVBORw0KGgo=",
            "images must be local image files; data: URLs cannot be opened. Save the image "
            "to a file first, then pass that file's path.",
        ),
    ],
)
async def test_web_and_data_addresses_are_refused_with_the_reason(
    photos: Path, image: str, message: str
) -> None:
    service = _Service()
    registry = ToolRegistry()
    register_analyze_image_tool(registry, service)
    context = _context(photos)

    result = await registry.dispatch(context, {"prompt": "Describe", "images": [image]})

    assert result["error"] == {"code": "invalid_arguments", "message": message, "retryable": False}
    assert service.analyzed is None
    assert context.presentation_images == []


@pytest.mark.asyncio
async def test_a_folder_names_the_images_inside_it(photos: Path) -> None:
    result = await analyze(photos, {"prompt": "Describe", "images": ["photos"]})

    assert result["error"]["code"] == "image_read_error"
    assert result["error"]["message"] == (
        "photos is a folder, not an image. Pass image files from it, for example "
        '{"images": ["photos/cat.png", "photos/garden.jpg"]}.'
    )


def _failure(cause: Exception, *, retryable: bool = False) -> ImageExecutionError:
    try:
        raise cause
    except Exception as error:
        try:
            raise ImageExecutionError(str(error), retryable=retryable) from error
        except ImageExecutionError as wrapped:
            return wrapped


def _with_status(error: Exception, status: int) -> Exception:
    error.status_code = status  # type: ignore[attr-defined]
    return error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "message"),
    [
        (
            _failure(
                _with_status(
                    ProviderAuthError(
                        'Authentication error: 401 {"error": {"message": "Provided '
                        'authentication token is expired."}}'
                    ),
                    401,
                )
            ),
            "The image-understanding provider rejected its credentials (HTTP 401: Provided "
            "authentication token is expired.). Tell the user to check that provider's API "
            "key or sign-in in Settings.",
        ),
        (
            _failure(
                _with_status(
                    ProviderRateLimitError(
                        'Rate limited: 429 {"error":{"type":"usage_limit_reached",'
                        '"message":"The usage limit has been reached"}}'
                    ),
                    429,
                ),
                retryable=True,
            ),
            "The image-understanding provider is limiting requests or its usage limit is "
            "reached (HTTP 429: The usage limit has been reached). Wait before trying again, "
            "and tell the user if it keeps happening.",
        ),
        (
            _failure(NetworkError("Connection reset by peer"), retryable=True),
            "The image-understanding provider did not answer (Connection reset by peer). Try "
            "again later.",
        ),
        (
            _failure(
                _with_status(
                    ProviderError(
                        "Provider error: 400 <html><head><title>Bad Request</title></head>"
                        "<body>...</body></html>",
                        retryable=False,
                    ),
                    400,
                )
            ),
            "The image-understanding provider rejected the request (HTTP 400: Bad Request). "
            "If the reason concerns the request, change it; otherwise tell the user, who may "
            "need to choose another Image understanding model in Settings under Specialized "
            "Models.",
        ),
    ],
)
async def test_provider_failures_say_what_happened_and_what_to_do(
    photos: Path, error: ImageExecutionError, message: str
) -> None:
    result = await analyze(
        photos, {"prompt": "Describe", "images": ["photos/cat.png"]}, _Service(error)
    )

    assert result["error"]["code"] == "provider_error"
    assert result["error"]["message"] == message
    assert result["error"]["retryable"] is error.retryable


async def generate(root: Path, arguments: Any, service: _Service) -> dict[str, Any]:
    registry = ToolRegistry()
    register_image_generation_tool(registry, service)
    return await registry.dispatch(_context(root, IMAGE_GENERATION_TOOL_NAME), arguments)


@pytest.mark.asyncio
async def test_generation_source_aliases_and_missing_sources(photos: Path) -> None:
    service = _Service()

    result = await generate(
        photos, {"prompt": "make it rainy", "input_image": "photos/cat.png"}, service
    )
    assert result["ok"] is True
    assert service.generated == ("make it rainy", ((photos / "photos" / "cat.png").resolve(),))

    service = _Service()
    result = await generate(
        photos, {"prompt": "make it rainy", "source_images": ["photos/cat.jpg"]}, service
    )
    assert result["error"] == {
        "code": "image_not_found",
        "message": "No image at photos/cat.jpg (similar: photos/cat.png).\n"
        'If you meant that file, pass {"source_images": ["photos/cat.png"]}.',
        "retryable": False,
    }
    assert service.generated is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code", "message"),
    [
        (
            ImageConfigurationError("No task model configured for image_generation"),
            "image_error",
            "Image generation is not available (No task model configured for "
            "image_generation). Tell the user to choose a working Image generation model in "
            "Settings under Specialized Models.",
        ),
        (
            _failure(
                _with_status(
                    ProviderError(
                        'Provider error: 400 {"error":{"message":"Invalid background_hex_color '
                        '\\"\\": expected a #RRGGBB value"}}',
                        retryable=False,
                    ),
                    400,
                )
            ),
            "provider_error",
            "The image-generation provider rejected the request (HTTP 400: Invalid "
            'background_hex_color "": expected a #RRGGBB value). If the reason concerns the '
            "request, change it; otherwise tell the user, who may need to choose another "
            "Image generation model in Settings under Specialized Models.",
        ),
    ],
)
async def test_generation_failures_keep_their_code_and_name_the_fix(
    tmp_path: Path, error: Exception, code: str, message: str
) -> None:
    result = await generate(tmp_path, {"prompt": "a red bicycle"}, _Service(error))

    assert result["error"]["code"] == code
    assert result["error"]["message"] == message


def test_display_shows_the_request_under_any_spelling(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_analyze_image_tool(registry, _Service())

    display = registry.display_for_call(
        ANALYZE_IMAGE_TOOL_NAME, {"question": "What animal?", "image": "cat.png"}
    )

    assert [part["value"] for part in display["primary"]] == ["What animal?"]


@pytest.mark.skipif(os.name != "nt", reason="drive letters exist only on Windows")
@pytest.mark.asyncio
async def test_a_windows_file_url_keeps_its_drive(photos: Path) -> None:
    service = _Service()
    target = (photos / "photos" / "cat.png").resolve()

    result = await analyze(
        photos, {"prompt": "What animal?", "images": [f"file:///{target.as_posix()}"]}, service
    )

    assert result["ok"] is True
    assert service.analyzed is not None
    assert service.analyzed[1] == (target,)
