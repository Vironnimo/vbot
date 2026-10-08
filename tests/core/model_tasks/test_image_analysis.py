"""Tests for image analysis."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import random
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, override

import pytest
from PIL import Image

import core.attachments.images as conversion_module
from core.debug import DebugContext
from core.model_tasks import (
    TASK_IMAGE_UNDERSTANDING,
    ImageConfigurationError,
    ImageExecutionError,
    ImageInputError,
    ImageNotFoundError,
    ImageReadError,
    ImageService,
    ImageTooLargeError,
    ImageUnderstandingRunContext,
    ImageUnderstandingUnavailableError,
    ImageUnsupportedMediaTypeError,
    TaskModelError,
)
from core.model_tasks import image as image_module
from core.model_tasks.image import DEFAULT_IMAGE_ANALYSIS_MAX_TOTAL_BYTES
from core.providers.accounts import ConnectionRef
from core.providers.errors import ProviderError
from core.usage import UsageRecorder
from core.utils.errors import ConfigError
from tests.core.model_tasks.image_test_support import (
    _MissingModelTasks,
)
from tests.core.providers.adapter_test_support import response_deltas
from tests.core.usage.usage_test_support import read_ledger


# ---------------------------------------------------------------------------
# ImageService.analyze — isolated image-understanding execution
# ---------------------------------------------------------------------------
class _UnderstandingModelTasks:
    def __init__(
        self,
        *,
        target: str = "openrouter/vision-model::api-key",
        input_modalities: tuple[str, ...] = ("text", "image"),
        output_modalities: tuple[str, ...] = ("text",),
        task_types: tuple[str, ...] = (TASK_IMAGE_UNDERSTANDING,),
        binding_usable: bool = True,
    ) -> None:
        self._target = target
        self._binding_usable = binding_usable
        self._model = SimpleNamespace(
            capabilities=SimpleNamespace(
                input_modalities=input_modalities,
                output_modalities=output_modalities,
                task_types=task_types,
            )
        )

    def binding_for(self, task_type: str) -> object:
        assert task_type == TASK_IMAGE_UNDERSTANDING
        return SimpleNamespace(task_type=task_type, target=self._target, options={})

    def binding_is_usable(self, task_type: str) -> bool:
        assert task_type == TASK_IMAGE_UNDERSTANDING
        return self._binding_usable

    def model_for_target(self, _target_ref: object) -> Any:
        return self._model

    def validate_execution_target(self, binding: Any) -> None:
        assert binding.target == self._target
        if not self._binding_usable:
            raise TaskModelError("The selected target is no longer usable")

    def options_with_defaults(self, binding: Any) -> dict[str, Any]:
        return dict(binding.options)


class _UnderstandingAdapter:
    def __init__(
        self,
        response: object | None = None,
        *,
        wire_media_types: frozenset[str] = frozenset({"image/png"}),
        max_image_bytes: int | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.response = response or {
            "content": "Visible ingredients: flour and salt.",
            "usage": {"input_tokens": 12, "output_tokens": 7},
        }
        self.wire_media_types = wire_media_types
        self.max_image_bytes = max_image_bytes
        self.wire_media_models: list[str] = []
        self.requests: list[dict[str, Any]] = []
        self.debug_contexts: list[DebugContext] = []
        self.closed = False
        self.close_error = close_error

    def set_debug_context(self, context: DebugContext) -> None:
        self.debug_contexts.append(context)

    def wire_media_support(self, model_id: str) -> frozenset[str]:
        self.wire_media_models.append(model_id)
        return self.wire_media_types

    def image_size_limit(self, model_id: str) -> int | None:
        return self.max_image_bytes

    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        self.requests.append({"messages": messages, "model_id": model_id, "kwargs": kwargs})
        return self._deltas()

    async def _deltas(self) -> AsyncIterator[dict[str, Any]]:
        if isinstance(self.response, Exception):
            raise self.response
        for delta in response_deltas(cast(dict[str, Any], self.response)):
            yield delta

    async def aclose(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class _BlockingUnderstandingAdapter(_UnderstandingAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.active_requests = 0
        self.max_active_requests = 0

    @override
    async def _deltas(self) -> AsyncIterator[dict[str, Any]]:
        self.active_requests += 1
        self.max_active_requests = max(self.max_active_requests, self.active_requests)
        self.started.set()
        try:
            await self.release.wait()
            async for delta in super()._deltas():
                yield delta
        finally:
            self.active_requests -= 1


class _BrokenAfterUsageAdapter(_UnderstandingAdapter):
    @override
    async def _deltas(self) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "usage", "input_tokens": 9, "output_tokens": 1}
        raise ProviderError("stream dropped", retryable=True)


class _UnderstandingRuntime:
    def __init__(
        self,
        adapter: _UnderstandingAdapter | Exception,
    ) -> None:
        self.adapter = adapter
        self.calls: list[tuple[str, str]] = []

    def get_adapter(self, connection: ConnectionRef) -> _UnderstandingAdapter:
        self.calls.append((connection.provider_id, connection.connection_id))
        if isinstance(self.adapter, Exception):
            raise self.adapter
        return self.adapter


def _png(path: Path, suffix: bytes = b"pixels") -> Path:
    stream = io.BytesIO()
    Image.new("RGB", (12, 8), "blue").save(stream, format="PNG")
    path.write_bytes(stream.getvalue() + suffix)
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["empty-analysis", "stream-breaks"])
async def test_failed_analysis_preserves_billed_usage_and_caller_scope(
    recorder: UsageRecorder,
    tmp_path: Path,
    failure: str,
) -> None:
    adapter = (
        _UnderstandingAdapter({"content": "", "usage": {"input_tokens": 9, "output_tokens": 1}})
        if failure == "empty-analysis"
        else _BrokenAfterUsageAdapter()
    )
    service = ImageService(
        _UnderstandingModelTasks(),
        cast(Any, _UnderstandingRuntime(adapter)),
        usage_recorder=recorder,
    )
    with pytest.raises(ImageExecutionError):
        await service.analyze(
            "Describe",
            image_paths=[_png(tmp_path / "test.png")],
            run_context=ImageUnderstandingRunContext(
                agent_id="agent",
                project_id="project",
                session_id="session",
                run_id="run",
                iteration_number=1,
                owner_name="extension",
                group_id="group",
            ),
        )
    _, records = read_ledger(recorder)
    assert len(records) == 1
    record = records[0]
    assert (record.kind, record.status, record.usage["input_tokens"]) == (
        "image_understanding",
        "failed",
        9,
    )
    assert (record.agent_id, record.project_id, record.session_id, record.run_id) == (
        "agent",
        "project",
        "session",
        "run",
    )
    assert (record.owner_name, record.group_id) == ("extension", "group")


@pytest.mark.parametrize(
    ("binding_usable", "wire_media_types", "resolve_error", "close_error", "available"),
    [
        pytest.param(True, {"application/pdf", "image/png"}, None, None, True, id="image-wire"),
        pytest.param(True, {"application/pdf", "audio/wav"}, None, None, False, id="no-image-wire"),
        # An unusable binding is decided before any adapter is resolved.
        pytest.param(
            False,
            {"image/png"},
            RuntimeError("adapter must not be resolved"),
            None,
            False,
            id="unusable-binding",
        ),
        pytest.param(
            True, {"image/png"}, RuntimeError("runtime unavailable"), None, False, id="no-adapter"
        ),
        pytest.param(
            True, {"image/png"}, None, RuntimeError("cleanup failed"), True, id="cleanup-failure"
        ),
    ],
)
@pytest.mark.asyncio
async def test_analysis_availability_requires_an_adapter_with_an_image_wire(
    caplog: pytest.LogCaptureFixture,
    binding_usable: bool,
    wire_media_types: set[str],
    resolve_error: Exception | None,
    close_error: Exception | None,
    available: bool,
) -> None:
    adapter = _UnderstandingAdapter(
        wire_media_types=frozenset(wire_media_types), close_error=close_error
    )
    runtime = _UnderstandingRuntime(resolve_error or adapter)
    service = ImageService(
        _UnderstandingModelTasks(binding_usable=binding_usable), cast(Any, runtime)
    )

    with caplog.at_level(logging.WARNING, logger="vbot.image"):
        assert await service.analysis_is_available() is available

    assert runtime.calls == ([("openrouter", "openrouter:api-key")] if binding_usable else [])
    if resolve_error is None:
        assert adapter.wire_media_models == ["vision-model"]
        assert adapter.closed is True
    assert ("adapter cleanup failed" in caplog.text) is (close_error is not None)


@pytest.mark.asyncio
async def test_analyze_sends_fixed_isolated_prompt_and_ordered_images(tmp_path: Path) -> None:
    first = _png(tmp_path / "first.png", b"first")
    second = _png(tmp_path / "second.png", b"second")
    adapter = _UnderstandingAdapter()
    runtime = _UnderstandingRuntime(adapter)
    service = ImageService(
        _UnderstandingModelTasks(task_types=("chat", "text_output")),
        cast(Any, runtime),
    )

    run_context = ImageUnderstandingRunContext(
        run_id="run-1",
        agent_id="agent-1",
        session_id="session-1",
        iteration_number=3,
    )
    result = await service.analyze(
        "List the recipe ingredients exactly.",
        image_paths=[first, second],
        run_context=run_context,
    )

    assert result.to_dict() == {
        "analysis": "Visible ingredients: flour and salt.",
        "model": "vision-model",
        "image_count": 2,
        "usage": {"input_tokens": 12, "output_tokens": 7},
    }
    assert runtime.calls == [("openrouter", "openrouter:api-key")]
    request = adapter.requests[0]
    assert request["model_id"] == "vision-model"
    # No sampling parameters: the Provider's own defaults apply.
    assert request["kwargs"] == {"tools": []}
    assert request["messages"][0]["role"] == "system"
    assert request["messages"][0]["content"]
    user_content = request["messages"][1]["content"]
    assert user_content[0] == {
        "type": "text",
        "text": "List the recipe ingredients exactly.",
    }
    assert [block["media_type"] for block in user_content[1:]] == [
        "image/png",
        "image/png",
    ]
    assert user_content[1]["base64"] != user_content[2]["base64"]
    assert adapter.closed is True
    assert adapter.debug_contexts == [
        DebugContext(
            run_id="run-1",
            agent_id="agent-1",
            session_id="session-1",
            provider_id="openrouter",
            connection_id="openrouter:api-key",
            model_id="vision-model",
            streaming=True,
            iteration_number=3,
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_outcome", "complete"),
    [("stop", True), ("output_truncated", False), ("content_filtered", False)],
)
async def test_an_analysis_the_model_did_not_finish_says_so(
    tmp_path: Path, terminal_outcome: str, complete: bool
) -> None:
    partial = "Visible ingredients: flour and"
    adapter = _UnderstandingAdapter(
        response={"content": partial, "terminal_outcome": terminal_outcome}
    )
    service = ImageService(
        _UnderstandingModelTasks(task_types=("chat", "text_output")),
        cast(Any, _UnderstandingRuntime(adapter)),
    )

    result = await service.analyze("List the ingredients.", image_paths=[_png(tmp_path / "a.png")])

    assert result.content.startswith(partial)
    assert (result.content == partial) is complete


@pytest.mark.asyncio
@pytest.mark.parametrize("format", ["BMP", "HEIF"])
async def test_analysis_converts_for_the_actual_target_without_changing_original(
    tmp_path: Path, format
):
    source = tmp_path / "diagram.input"
    Image.new("RGB", (12, 8), "blue").save(source, format=format)
    original = source.read_bytes()
    adapter = _UnderstandingAdapter()
    service = ImageService(_UnderstandingModelTasks(), cast(Any, _UnderstandingRuntime(adapter)))
    for target in ("image/png", "image/jpeg"):
        adapter.wire_media_types = frozenset({target})
        result = await service.analyze("Describe it", image_paths=[source])
        part = adapter.requests[-1]["messages"][1]["content"][1]
        assert part["media_type"] == target
        with Image.open(io.BytesIO(base64.b64decode(part["base64"]))) as converted:
            assert converted.size == (12, 8)
        assert "converted copy" in result.content
        assert "converted copy" in adapter.requests[-1]["messages"][1]["content"][0]["text"]
        assert ("lossy compression" in result.content) == (target == "image/jpeg")
    assert source.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,code,advice",
    [
        ("damaged", "image_read_error", "fresh copy"),
        ("pixels", "image_too_large", "changing the file format alone will not help"),
        ("bytes", "image_too_large", "1 byte limit"),
    ],
)
async def test_analysis_preparation_failures_are_actionable_before_provider_send(
    tmp_path, monkeypatch, failure, code, advice
):
    source = _png(tmp_path / "source.png")
    if failure == "damaged":
        source.write_bytes(source.read_bytes()[:25])
    if failure == "pixels":
        monkeypatch.setattr(conversion_module, "_PIXEL_LIMIT", 10)
    adapter = _UnderstandingAdapter(max_image_bytes=1 if failure == "bytes" else None)
    service = ImageService(_UnderstandingModelTasks(), cast(Any, _UnderstandingRuntime(adapter)))
    with pytest.raises(ImageInputError) as error:
        await service.analyze("Describe it", image_paths=[source])
    assert error.value.code == code
    assert advice in str(error.value)
    assert not adapter.requests
    assert adapter.closed


@pytest.mark.asyncio
async def test_analysis_byte_limit_preserves_original_and_reports_the_sent_copy(tmp_path):
    source = tmp_path / "large.png"
    Image.frombytes("RGB", (128, 64), random.Random(3).randbytes(128 * 64 * 3)).save(source)
    original = source.read_bytes()
    adapter = _UnderstandingAdapter(max_image_bytes=512)
    service = ImageService(_UnderstandingModelTasks(), cast(Any, _UnderstandingRuntime(adapter)))
    result = await service.analyze("Read its small text", image_paths=[source])
    content = adapter.requests[0]["messages"][1]["content"]
    assert len(base64.b64decode(content[1]["base64"])) <= 512
    assert "resized from 128x64" in content[0]["text"]
    assert "resized from 128x64" in result.content
    assert source.read_bytes() == original


@pytest.mark.asyncio
async def test_analysis_does_not_split_total_budget_when_uneven_native_images_fit(
    tmp_path, monkeypatch
):
    small = _png(tmp_path / "small.png")
    large = _png(tmp_path / "large.png", b"padding" * 100)
    originals = [path.read_bytes() for path in (small, large)]
    monkeypatch.setattr(
        image_module, "DEFAULT_IMAGE_ANALYSIS_MAX_TOTAL_BYTES", sum(map(len, originals))
    )
    adapter = _UnderstandingAdapter()
    service = ImageService(_UnderstandingModelTasks(), cast(Any, _UnderstandingRuntime(adapter)))
    await service.analyze("Compare", image_paths=[small, large])
    content = adapter.requests[0]["messages"][1]["content"]
    assert [base64.b64decode(part["base64"]) for part in content[1:]] == originals


@pytest.mark.asyncio
async def test_analysis_conversion_growth_fits_actual_total_budget(tmp_path, monkeypatch):
    source = tmp_path / "small.webp"
    # A small palette gives lossless WebP a substantial size advantage over PNG.
    values = random.Random(5).choices(range(0, 256, 32), k=64 * 64 * 3)
    image = Image.frombytes("RGB", (64, 64), bytes(values))
    image.save(source, format="WEBP", lossless=True)
    original = source.read_bytes()
    png = io.BytesIO()
    image.save(png, format="PNG")
    assert len(original) < len(png.getvalue())
    ceiling = len(original) * 2
    monkeypatch.setattr(image_module, "DEFAULT_IMAGE_ANALYSIS_MAX_TOTAL_BYTES", ceiling)
    adapter = _UnderstandingAdapter()
    service = ImageService(_UnderstandingModelTasks(), cast(Any, _UnderstandingRuntime(adapter)))
    result = await service.analyze("Compare", image_paths=[source, source])
    parts = adapter.requests[0]["messages"][1]["content"][1:]
    assert sum(len(base64.b64decode(part["base64"])) for part in parts) <= ceiling
    assert "resized" in result.content
    assert source.read_bytes() == original


@pytest.mark.asyncio
async def test_analyze_rejects_inputs_above_the_total_byte_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The documented cumulative ceiling is 100 MiB; a 1 MiB stand-in keeps the files small.
    assert DEFAULT_IMAGE_ANALYSIS_MAX_TOTAL_BYTES == 100 * 1024 * 1024
    monkeypatch.setattr(image_module, "DEFAULT_IMAGE_ANALYSIS_MAX_TOTAL_BYTES", 1024 * 1024)
    first = _png(tmp_path / "first.png", b"x" * 600_000)
    second = _png(tmp_path / "second.png", b"x" * 600_000)
    runtime = _UnderstandingRuntime(_UnderstandingAdapter())
    service = ImageService(_UnderstandingModelTasks(), cast(Any, runtime))

    with pytest.raises(
        ImageTooLargeError,
        match=r"exceeding the 1 MiB \(1048576 bytes\) limit\. Pass fewer or smaller images",
    ) as error:
        await service.analyze("Compare them", image_paths=[first, second])

    assert error.value.code == "image_too_large"
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_analyze_offloads_file_loading_and_base64_encoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _png(tmp_path / "source.png")
    runtime = _UnderstandingRuntime(_UnderstandingAdapter())
    service = ImageService(_UnderstandingModelTasks(), cast(Any, runtime))
    event_loop_thread = threading.get_ident()
    load_threads: list[int] = []
    content_threads: list[int] = []
    original_load = image_module._load_image_inputs
    original_content = image_module._analysis_content

    def tracked_load(*args: Any, **kwargs: Any) -> Any:
        load_threads.append(threading.get_ident())
        return original_load(*args, **kwargs)

    def tracked_content(*args: Any, **kwargs: Any) -> Any:
        content_threads.append(threading.get_ident())
        return original_content(*args, **kwargs)

    monkeypatch.setattr(image_module, "_load_image_inputs", tracked_load)
    monkeypatch.setattr(image_module, "_analysis_content", tracked_content)

    await service.analyze("Describe it", image_paths=[source])

    assert load_threads and all(thread_id != event_loop_thread for thread_id in load_threads)
    assert content_threads and all(thread_id != event_loop_thread for thread_id in content_threads)


@pytest.mark.asyncio
async def test_analyze_serializes_concurrent_requests(tmp_path: Path) -> None:
    source = _png(tmp_path / "source.png")
    adapter = _BlockingUnderstandingAdapter()
    runtime = _UnderstandingRuntime(adapter)
    service = ImageService(_UnderstandingModelTasks(), cast(Any, runtime))

    first = asyncio.create_task(service.analyze("First", image_paths=[source]))
    await adapter.started.wait()
    second = asyncio.create_task(service.analyze("Second", image_paths=[source]))
    await asyncio.sleep(0)

    assert runtime.calls == [("openrouter", "openrouter:api-key")]

    adapter.release.set()
    await asyncio.gather(first, second)

    assert adapter.max_active_requests == 1
    assert runtime.calls == [
        ("openrouter", "openrouter:api-key"),
        ("openrouter", "openrouter:api-key"),
    ]


@pytest.mark.asyncio
async def test_analyze_rejects_non_understanding_model_and_unsupported_wire(
    tmp_path: Path,
) -> None:
    source = _png(tmp_path / "source.png")
    pinned_target = "openrouter/vision-model::api-key:private_account"
    text_only = ImageService(
        _UnderstandingModelTasks(
            target=pinned_target,
            input_modalities=("text",),
        ),
        cast(Any, _UnderstandingRuntime(_UnderstandingAdapter())),
    )
    image_only = ImageService(
        _UnderstandingModelTasks(input_modalities=("image",)),
        cast(Any, _UnderstandingRuntime(_UnderstandingAdapter())),
    )
    adapter = _UnderstandingAdapter(wire_media_types=frozenset())
    unsupported_wire = ImageService(
        _UnderstandingModelTasks(),
        cast(Any, _UnderstandingRuntime(adapter)),
    )

    with pytest.raises(
        ImageUnderstandingUnavailableError,
        match="openrouter/vision-model",
    ) as text_only_error:
        await text_only.analyze("Describe it", image_paths=[source])
    with pytest.raises(ImageUnderstandingUnavailableError):
        await image_only.analyze("Describe it", image_paths=[source])
    with pytest.raises(ImageUnsupportedMediaTypeError):
        await unsupported_wire.analyze("Describe it", image_paths=[source])

    assert adapter.closed is True
    assert "private_account" not in str(text_only_error.value)
    assert pinned_target not in str(text_only_error.value)


def _one_png(tmp_path: Path) -> list[Path]:
    return [_png(tmp_path / "source.png")]


def _unread(tmp_path: Path) -> list[Path]:
    return [tmp_path / f"image-{index}.png" for index in range(7)]


def _directory(tmp_path: Path) -> list[Path]:
    directory = tmp_path / "directory"
    directory.mkdir()
    return [directory]


def _text_file(tmp_path: Path) -> list[Path]:
    text_file = tmp_path / "notes.txt"
    text_file.write_text("plain text", encoding="utf-8")
    return [text_file]


@pytest.mark.parametrize(
    ("binding", "prompt", "images", "error", "match"),
    [
        pytest.param(
            "missing",
            "Describe it",
            _one_png,
            ImageUnderstandingUnavailableError,
            None,
            id="missing-binding",
        ),
        # The target is revalidated before any file is read.
        pytest.param(
            "unusable",
            "Describe it",
            lambda tmp_path: _unread(tmp_path)[:1],
            ImageUnderstandingUnavailableError,
            None,
            id="unusable-target",
        ),
        pytest.param("usable", "  ", _one_png, ImageConfigurationError, None, id="blank-prompt"),
        pytest.param("usable", "Describe it", lambda _: [], ImageInputError, None, id="no-images"),
        # The documented six-image limit applies before any file is read.
        pytest.param(
            "usable",
            "Compare them",
            _unread,
            ImageTooLargeError,
            "at most 6 images per call, but received 7. Pass fewer images",
            id="seven-images",
        ),
        pytest.param(
            "usable",
            "Describe it",
            lambda tmp_path: [tmp_path / "missing.png"],
            ImageNotFoundError,
            None,
            id="missing-file",
        ),
        pytest.param("usable", "Describe it", _directory, ImageReadError, None, id="directory"),
        pytest.param(
            "usable",
            "Describe it",
            _text_file,
            ImageUnsupportedMediaTypeError,
            None,
            id="not-an-image",
        ),
        # Any PNG exceeds the 12-byte input ceiling of this service.
        pytest.param("usable", "Describe it", _one_png, ImageTooLargeError, None, id="oversize"),
    ],
)
@pytest.mark.asyncio
async def test_analyze_rejects_unusable_requests_before_resolving_an_adapter(
    tmp_path: Path,
    binding: str,
    prompt: str,
    images: Callable[[Path], list[Path]],
    error: type[Exception],
    match: str | None,
) -> None:
    runtime = _UnderstandingRuntime(_UnderstandingAdapter())
    model_tasks = (
        _MissingModelTasks()
        if binding == "missing"
        else _UnderstandingModelTasks(binding_usable=binding == "usable")
    )
    service = ImageService(model_tasks, cast(Any, runtime), max_input_bytes=12)

    with pytest.raises(error, match=match) as caught:
        await service.analyze(prompt, image_paths=images(tmp_path))

    assert type(caught.value) is error
    assert runtime.calls == []


def _retried_provider_error() -> ProviderError:
    error = ProviderError("rate limited", retryable=True)
    error.attempts_made = 4
    return error


@pytest.mark.parametrize(
    ("response", "error", "match", "attributes"),
    [
        pytest.param(
            _retried_provider_error,
            ImageExecutionError,
            "rate limited",
            {"code": "provider_error", "retryable": True, "attempts_made": 4},
            id="provider-error",
        ),
        pytest.param(lambda: {"content": "   "}, ImageExecutionError, None, {}, id="empty-output"),
        # An unexpected adapter failure is a bug and is not masked.
        pytest.param(
            lambda: RuntimeError("adapter bug"), RuntimeError, "adapter bug", {}, id="adapter-bug"
        ),
    ],
)
@pytest.mark.asyncio
async def test_analyze_maps_send_failures_and_closes_the_adapter(
    tmp_path: Path,
    response: Callable[[], object],
    error: type[Exception],
    match: str | None,
    attributes: dict[str, object],
) -> None:
    adapter = _UnderstandingAdapter(response())
    service = ImageService(_UnderstandingModelTasks(), cast(Any, _UnderstandingRuntime(adapter)))

    with pytest.raises(error, match=match) as caught:
        await service.analyze("Describe it", image_paths=[_png(tmp_path / "source.png")])

    assert {name: getattr(caught.value, name) for name in attributes} == attributes
    assert adapter.closed is True


@pytest.mark.parametrize("send_error", [None, ProviderError("primary provider failure")])
@pytest.mark.asyncio
async def test_adapter_cleanup_failure_never_replaces_the_outcome(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    send_error: ProviderError | None,
) -> None:
    adapter = _UnderstandingAdapter(
        send_error, close_error=RuntimeError("cleanup failed for private_account")
    )
    service = ImageService(
        _UnderstandingModelTasks(target="openrouter/vision-model::api-key:private_account"),
        cast(Any, _UnderstandingRuntime(adapter)),
    )
    source = _png(tmp_path / "source.png")

    with caplog.at_level(logging.WARNING, logger="vbot.image"):
        if send_error is None:
            result = await service.analyze("Describe it", image_paths=[source])
            assert result.content == "Visible ingredients: flour and salt."
        else:
            with pytest.raises(ImageExecutionError, match="primary provider failure") as caught:
                await service.analyze("Describe it", image_paths=[source])
            assert "cleanup failed" not in str(caught.value)

    assert adapter.closed is True
    assert "adapter cleanup failed" in caplog.text
    assert "cleanup failed for [REDACTED]" in caplog.text
    assert "private_account" not in caplog.text


@pytest.mark.asyncio
async def test_analyze_redacts_pinned_account_from_provider_error_and_log(
    tmp_path: Path,
    caplog: Any,
) -> None:
    source = _png(tmp_path / "source.png")
    account_id = "private_account"
    target = f"openrouter/vision-model::api-key:{account_id}"
    adapter = _UnderstandingAdapter(ProviderError(f"request for {target} via {account_id} failed"))
    service = ImageService(
        _UnderstandingModelTasks(target=target),
        cast(Any, _UnderstandingRuntime(adapter)),
    )

    with (
        caplog.at_level(logging.WARNING, logger="vbot.image"),
        pytest.raises(ImageExecutionError) as error,
    ):
        await service.analyze("Describe it", image_paths=[source])

    assert "openrouter/vision-model" in str(error.value)
    assert "[REDACTED]" in str(error.value)
    assert account_id not in str(error.value)
    assert account_id not in caplog.text
    assert target not in caplog.text
    assert "target=openrouter/vision-model" in caplog.text


@pytest.mark.asyncio
async def test_analyze_maps_adapter_configuration_failure_to_unavailable(
    tmp_path: Path,
) -> None:
    source = _png(tmp_path / "source.png")
    service = ImageService(
        _UnderstandingModelTasks(),
        cast(Any, _UnderstandingRuntime(ConfigError("connection disabled"))),
    )

    with pytest.raises(ImageUnderstandingUnavailableError, match="connection disabled") as error:
        await service.analyze("Describe it", image_paths=[source])

    assert error.value.code == "image_understanding_unavailable"
    assert error.value.retryable is False
