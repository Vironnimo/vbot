"""Tests for image analysis."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PIL import Image

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
)
from core.model_tasks import image as image_module
from core.model_tasks.image import (
    DEFAULT_IMAGE_ANALYSIS_MAX_IMAGES,
    DEFAULT_IMAGE_ANALYSIS_MAX_TOTAL_BYTES,
    _ensure_analysis_total_size,
    _load_image_inputs,
)
from core.providers.accounts import ConnectionRef
from core.providers.errors import ProviderError
from core.utils.errors import ConfigError
from tests.core.model_tasks.image_test_support import (
    _MissingModelTasks,
)


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


class _UnderstandingAdapter:
    def __init__(
        self,
        response: object | None = None,
        *,
        wire_media_types: frozenset[str] = frozenset({"image/png"}),
        close_error: Exception | None = None,
    ) -> None:
        self.response = response or {
            "content": "Visible ingredients: flour and salt.",
            "usage": {"input_tokens": 12, "output_tokens": 7},
        }
        self.wire_media_types = wire_media_types
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

    async def send(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.requests.append({"messages": messages, "model_id": model_id, "kwargs": kwargs})
        if isinstance(self.response, Exception):
            raise self.response
        return cast(dict[str, Any], self.response)

    def normalize_response(
        self,
        response: dict[str, Any],
        *,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        del model_id
        return response

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

    async def send(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.active_requests += 1
        self.max_active_requests = max(self.max_active_requests, self.active_requests)
        self.started.set()
        try:
            await self.release.wait()
            return await super().send(messages, model_id=model_id, **kwargs)
        finally:
            self.active_requests -= 1


class _UnderstandingModels:
    def __init__(self, recommended: dict[tuple[str, str], float] | None = None) -> None:
        self._recommended = recommended or {}

    def get(self, provider_id: str, model_id: str) -> Any:
        recommended = self._recommended.get((provider_id, model_id))
        if recommended is None:
            raise KeyError(model_id)
        return SimpleNamespace(recommended_temperature=recommended)


class _UnderstandingRuntime:
    def __init__(
        self,
        adapter: _UnderstandingAdapter | Exception,
        models: _UnderstandingModels | None = None,
    ) -> None:
        self.adapter = adapter
        self.models = models or _UnderstandingModels()
        self.calls: list[tuple[str, str]] = []

    def get_adapter(self, connection: ConnectionRef) -> _UnderstandingAdapter:
        self.calls.append((connection.provider_id, connection.connection_id))
        if isinstance(self.adapter, Exception):
            raise self.adapter
        return self.adapter


def _png(path: Path, suffix: bytes = b"pixels") -> Path:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + suffix)
    return path


@pytest.mark.asyncio
async def test_analysis_availability_requires_adapter_image_wire_support() -> None:
    supported_adapter = _UnderstandingAdapter(
        wire_media_types=frozenset({"application/pdf", "image/png"})
    )
    unsupported_adapter = _UnderstandingAdapter(
        wire_media_types=frozenset({"application/pdf", "audio/wav"})
    )
    supported_runtime = _UnderstandingRuntime(supported_adapter)
    unsupported_runtime = _UnderstandingRuntime(unsupported_adapter)

    supported = await ImageService(
        _UnderstandingModelTasks(), cast(Any, supported_runtime)
    ).analysis_is_available()
    unsupported = await ImageService(
        _UnderstandingModelTasks(), cast(Any, unsupported_runtime)
    ).analysis_is_available()

    assert supported is True
    assert unsupported is False
    assert supported_runtime.calls == [("openrouter", "openrouter:api-key")]
    assert unsupported_runtime.calls == [("openrouter", "openrouter:api-key")]
    assert supported_adapter.wire_media_models == ["vision-model"]
    assert unsupported_adapter.wire_media_models == ["vision-model"]
    assert supported_adapter.closed is True
    assert unsupported_adapter.closed is True


@pytest.mark.asyncio
async def test_analysis_availability_rejects_unusable_binding_before_adapter_resolution() -> None:
    runtime = _UnderstandingRuntime(RuntimeError("adapter must not be resolved"))
    service = ImageService(
        _UnderstandingModelTasks(binding_usable=False),
        cast(Any, runtime),
    )

    assert await service.analysis_is_available() is False
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_analysis_availability_maps_expected_adapter_resolution_failure_to_false() -> None:
    runtime = _UnderstandingRuntime(RuntimeError("runtime unavailable"))
    service = ImageService(_UnderstandingModelTasks(), cast(Any, runtime))

    assert await service.analysis_is_available() is False
    assert runtime.calls == [("openrouter", "openrouter:api-key")]


@pytest.mark.asyncio
async def test_analysis_availability_ignores_adapter_cleanup_failure(
    caplog: Any,
) -> None:
    adapter = _UnderstandingAdapter(close_error=RuntimeError("cleanup failed"))
    service = ImageService(
        _UnderstandingModelTasks(),
        cast(Any, _UnderstandingRuntime(adapter)),
    )

    with caplog.at_level(logging.WARNING, logger="vbot.image"):
        available = await service.analysis_is_available()

    assert available is True
    assert adapter.closed is True
    assert "adapter cleanup failed" in caplog.text


@pytest.mark.asyncio
async def test_analyze_sends_fixed_isolated_prompt_and_ordered_images(
    tmp_path: Path,
) -> None:
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
    assert request["kwargs"] == {"temperature": None, "tools": []}
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
            streaming=False,
            iteration_number=3,
        )
    ]


@pytest.mark.asyncio
async def test_analysis_converts_for_the_actual_target_without_changing_original(tmp_path: Path):
    source = tmp_path / "diagram.bmp"
    Image.new("RGB", (12, 8), "blue").save(source)
    original = source.read_bytes()
    adapter = _UnderstandingAdapter()
    service = ImageService(_UnderstandingModelTasks(), cast(Any, _UnderstandingRuntime(adapter)))
    for target in ("image/png", "image/jpeg"):
        adapter.wire_media_types = frozenset({target})
        await service.analyze("Describe it", image_paths=[source])
        part = adapter.requests[-1]["messages"][1]["content"][1]
        assert part["media_type"] == target
        with Image.open(io.BytesIO(base64.b64decode(part["base64"]))) as converted:
            assert converted.size == (12, 8)
    assert source.read_bytes() == original


@pytest.mark.asyncio
async def test_analyze_uses_model_recommended_temperature(tmp_path: Path) -> None:
    image = _png(tmp_path / "image.png")
    adapter = _UnderstandingAdapter()
    runtime = _UnderstandingRuntime(
        adapter,
        models=_UnderstandingModels({("openrouter", "vision-model"): 1.0}),
    )
    service = ImageService(
        _UnderstandingModelTasks(task_types=("chat", "text_output")),
        cast(Any, runtime),
    )

    await service.analyze("List the ingredients.", image_paths=[image])

    assert adapter.requests[0]["kwargs"]["temperature"] == 1.0


@pytest.mark.asyncio
async def test_analyze_rejects_more_than_six_images_before_reading_files(
    tmp_path: Path,
) -> None:
    runtime = _UnderstandingRuntime(_UnderstandingAdapter())
    service = ImageService(_UnderstandingModelTasks(), cast(Any, runtime))
    image_paths = [tmp_path / f"image-{index}.png" for index in range(7)]

    with pytest.raises(ImageTooLargeError) as error:
        await service.analyze("Compare them", image_paths=image_paths)

    assert DEFAULT_IMAGE_ANALYSIS_MAX_IMAGES == 6
    assert error.value.code == "image_too_large"
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_analyze_rejects_inputs_above_the_total_byte_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _png(tmp_path / "first.png", b"first")
    second = _png(tmp_path / "second.png", b"second")
    total_bytes = first.stat().st_size + second.stat().st_size
    test_limit = total_bytes - 1
    runtime = _UnderstandingRuntime(_UnderstandingAdapter())
    service = ImageService(_UnderstandingModelTasks(), cast(Any, runtime))
    monkeypatch.setattr(
        image_module,
        "DEFAULT_IMAGE_ANALYSIS_MAX_TOTAL_BYTES",
        test_limit,
    )

    with pytest.raises(ImageTooLargeError) as error:
        await service.analyze("Compare them", image_paths=[first, second])

    assert error.value.code == "image_too_large"
    assert runtime.calls == []


def test_default_analysis_total_limit_has_actionable_error() -> None:
    with pytest.raises(ImageTooLargeError) as error:
        _ensure_analysis_total_size(
            DEFAULT_IMAGE_ANALYSIS_MAX_TOTAL_BYTES + 1,
            DEFAULT_IMAGE_ANALYSIS_MAX_TOTAL_BYTES,
        )

    assert DEFAULT_IMAGE_ANALYSIS_MAX_TOTAL_BYTES == 100 * 1024 * 1024
    assert error.value.code == "image_too_large"


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
    original_content = image_module._analysis_content

    def tracked_load(*args: Any, **kwargs: Any) -> Any:
        load_threads.append(threading.get_ident())
        return _load_image_inputs(*args, **kwargs)

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


@pytest.mark.asyncio
async def test_analyze_rejects_missing_non_image_and_oversize_input(
    tmp_path: Path,
) -> None:
    runtime = _UnderstandingRuntime(_UnderstandingAdapter())
    service = ImageService(
        _UnderstandingModelTasks(),
        cast(Any, runtime),
        max_input_bytes=12,
    )
    text_file = tmp_path / "notes.txt"
    text_file.write_text("plain text", encoding="utf-8")
    oversize = _png(tmp_path / "large.png", b"too-many-pixels")
    directory = tmp_path / "directory"
    directory.mkdir()

    with pytest.raises(ImageNotFoundError):
        await service.analyze("Describe it", image_paths=[tmp_path / "missing.png"])
    with pytest.raises(ImageReadError):
        await service.analyze("Describe it", image_paths=[directory])
    with pytest.raises(ImageUnsupportedMediaTypeError):
        await service.analyze("Describe it", image_paths=[text_file])
    with pytest.raises(ImageTooLargeError):
        await service.analyze("Describe it", image_paths=[oversize])

    assert runtime.calls == []


@pytest.mark.asyncio
async def test_analyze_maps_provider_failure_and_empty_output_and_closes_adapter(
    tmp_path: Path,
) -> None:
    source = _png(tmp_path / "source.png")
    provider_error = ProviderError("rate limited", retryable=True)
    provider_error.attempts_made = 4
    failing_adapter = _UnderstandingAdapter(provider_error)
    failing = ImageService(
        _UnderstandingModelTasks(),
        cast(Any, _UnderstandingRuntime(failing_adapter)),
    )
    empty_adapter = _UnderstandingAdapter({"content": "   "})
    empty = ImageService(
        _UnderstandingModelTasks(),
        cast(Any, _UnderstandingRuntime(empty_adapter)),
    )

    with pytest.raises(ImageExecutionError, match="rate limited") as error:
        await failing.analyze("Describe it", image_paths=[source])
    with pytest.raises(ImageExecutionError):
        await empty.analyze("Describe it", image_paths=[source])

    assert failing_adapter.closed is True
    assert empty_adapter.closed is True
    assert error.value.code == "provider_error"
    assert error.value.retryable is True
    assert error.value.attempts_made == 4


@pytest.mark.asyncio
async def test_analyze_preserves_success_when_adapter_cleanup_fails(
    tmp_path: Path,
    caplog: Any,
) -> None:
    source = _png(tmp_path / "source.png")
    adapter = _UnderstandingAdapter(close_error=RuntimeError("cleanup failed for private_account"))
    service = ImageService(
        _UnderstandingModelTasks(target="openrouter/vision-model::api-key:private_account"),
        cast(Any, _UnderstandingRuntime(adapter)),
    )

    with caplog.at_level(logging.WARNING, logger="vbot.image"):
        result = await service.analyze("Describe it", image_paths=[source])

    assert result.content == "Visible ingredients: flour and salt."
    assert adapter.closed is True
    assert "adapter cleanup failed" in caplog.text
    assert "private_account" not in caplog.text
    assert "[REDACTED]" in caplog.text


@pytest.mark.asyncio
async def test_analyze_preserves_primary_error_when_adapter_cleanup_fails(
    tmp_path: Path,
    caplog: Any,
) -> None:
    source = _png(tmp_path / "source.png")
    adapter = _UnderstandingAdapter(
        ProviderError("primary provider failure"),
        close_error=RuntimeError("secondary cleanup failure"),
    )
    service = ImageService(
        _UnderstandingModelTasks(),
        cast(Any, _UnderstandingRuntime(adapter)),
    )

    with (
        caplog.at_level(logging.WARNING, logger="vbot.image"),
        pytest.raises(ImageExecutionError, match="primary provider failure") as error,
    ):
        await service.analyze("Describe it", image_paths=[source])

    assert "secondary cleanup failure" not in str(error.value)
    assert "secondary cleanup failure" in caplog.text
    assert adapter.closed is True


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


@pytest.mark.asyncio
async def test_analyze_does_not_mask_unexpected_adapter_failure(tmp_path: Path) -> None:
    source = _png(tmp_path / "source.png")
    adapter = _UnderstandingAdapter(RuntimeError("adapter bug"))
    service = ImageService(
        _UnderstandingModelTasks(),
        cast(Any, _UnderstandingRuntime(adapter)),
    )

    with pytest.raises(RuntimeError, match="adapter bug"):
        await service.analyze("Describe it", image_paths=[source])

    assert adapter.closed is True


@pytest.mark.asyncio
async def test_analyze_requires_binding_prompt_and_images(tmp_path: Path) -> None:
    source = _png(tmp_path / "source.png")
    missing = ImageService(_MissingModelTasks(), cast(Any, object()))
    configured = ImageService(
        _UnderstandingModelTasks(),
        cast(Any, _UnderstandingRuntime(_UnderstandingAdapter())),
    )

    with pytest.raises(ImageUnderstandingUnavailableError):
        await missing.analyze("Describe it", image_paths=[source])
    with pytest.raises(ImageConfigurationError):
        await configured.analyze("  ", image_paths=[source])
    with pytest.raises(ImageInputError):
        await configured.analyze("Describe it", image_paths=[])


def test_image_file_ids_retry_collisions_without_overwriting(tmp_path, monkeypatch):
    from core.model_tasks.image import _write_image_artifact
    from core.utils import ids

    existing = tmp_path / "img_000000000001.png"
    existing.write_bytes(b"keep")
    values = iter((1, 2))
    monkeypatch.setattr(ids.secrets, "randbits", lambda _bits: next(values))
    result = _write_image_artifact(
        b"new", output_dir=tmp_path, extension="png", media_type="image/png", index=0
    )
    assert result.id == "img_000000000002"
    assert existing.read_bytes() == b"keep"
    assert result.file_path.read_bytes() == b"new"
