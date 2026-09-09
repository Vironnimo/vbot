"""Local STT contracts without network access or pretrained model downloads."""

from __future__ import annotations

import asyncio
import io
import threading
import wave
from collections.abc import Mapping
from concurrent.futures import Future
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from core.model_tasks.constants import TASK_SPEECH_TO_TEXT
from core.model_tasks.local_targets import LocalTaskTargetDescriptor
from core.model_tasks.options import TaskModelOptionField
from core.model_tasks.speech_local import (
    LocalSpeechError,
    LocalSpeechExecutionError,
    LocalSpeechExecutor,
    LocalSpeechSetup,
    SpeechEngineDefinition,
    _audio_chunks,
    builtin_speech_engines,
)
from core.model_tasks.speech_types import SpeechProgress, SpeechTranscriptionResult


def wav(samples: Any = None, *, rate: int = 16_000, channels: int = 1) -> bytes:
    if samples is None:
        samples = np.full(1600, 1000, dtype=np.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(np.asarray(samples, dtype="<i2").tobytes())
    return buffer.getvalue()


class Engine:
    def __init__(self, name: str, events: list[Any]) -> None:
        self.name = name
        self.events = events

    def transcribe(self, samples: Any, options: Mapping[str, Any]) -> SpeechTranscriptionResult:
        self.events.append((self.name, "transcribe", len(samples), dict(options)))
        return SpeechTranscriptionResult(text=self.name, language="de")

    def close(self) -> None:
        self.events.append((self.name, "close"))


def definition(name: str, events: list[Any]) -> SpeechEngineDefinition:
    def create(options: Mapping[str, Any]) -> Engine:
        events.append((name, "load", dict(options)))
        return Engine(name, events)

    return SpeechEngineDefinition(
        LocalTaskTargetDescriptor(
            id=name,
            label=name,
            task_types=(TASK_SPEECH_TO_TEXT,),
            availability=lambda: True,
            option_fields=(TaskModelOptionField("custom", "text", "Custom", default="one"),),
        ),
        create,
    )


async def transcribe(
    executor: LocalSpeechExecutor, name: str = "first", **options: Any
) -> SpeechTranscriptionResult:
    return await executor.transcribe(
        name, wav(), filename="input.wav", media_type="audio/wav", options=options
    )


def test_builtin_catalog_is_lazy_and_reports_missing_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.model_tasks import speech_local

    versions = {"torch": "2.11.0+cu128", "transformers": "5.16.1", "huggingface-hub": "1.30.0"}
    monkeypatch.setattr(speech_local.metadata, "version", versions.__getitem__)
    monkeypatch.setattr(speech_local.util, "find_spec", lambda _name: None)
    engines = builtin_speech_engines()
    assert [engine.descriptor.public_id for engine in engines] == [
        "local/qwen3-asr",
        "local/parakeet",
    ]
    assert not any(engine.descriptor.can_execute() for engine in engines)
    monkeypatch.setattr(speech_local.util, "find_spec", lambda _name: object())
    assert all(engine.descriptor.can_execute() for engine in engines)
    versions["torch"] = "2.9.0"
    assert not any(engine.descriptor.can_execute() for engine in engines)
    monkeypatch.setattr(speech_local.metadata, "version", lambda _name: "5.12.0")
    assert not any(engine.descriptor.can_execute() for engine in engines)
    assert "prompt" not in {field.name for field in engines[1].descriptor.option_fields}


@pytest.mark.asyncio
async def test_third_engine_reuses_and_switches_with_its_own_options() -> None:
    events: list[Any] = []
    executor = LocalSpeechExecutor(
        engines=[definition("first", events), definition("third", events)]
    )
    try:
        assert (await transcribe(executor)).text == "first"
        await transcribe(executor)
        assert sum(event[1] == "load" for event in events) == 1
        await transcribe(executor, custom="two")
        assert events[3:5] == [("first", "close"), ("first", "load", {"custom": "two"})]
        result = await transcribe(executor, "third")
        assert result.text == "third"
        assert result.language == "de"
        assert result.segments == ({"start": 0.0, "end": 0.1, "text": "third"},)
        assert events[-3:-1] == [("first", "close"), ("third", "load", {"custom": "one"})]
        await executor.unload()
        assert events[-1] == ("third", "close")
    finally:
        await executor.aclose()
    with pytest.raises(LocalSpeechError, match="closed"):
        await transcribe(executor)


@pytest.mark.asyncio
async def test_validation_and_unavailable_engine_never_load() -> None:
    events: list[Any] = []
    entry = definition("first", events)
    unavailable = replace(
        entry, descriptor=replace(entry.descriptor, id="missing", availability=lambda: False)
    )
    executor = LocalSpeechExecutor(engines=[entry, unavailable])
    try:
        with pytest.raises(LocalSpeechError, match="not available"):
            await transcribe(executor, "unknown")
        with pytest.raises(LocalSpeechError):
            await transcribe(executor, "missing")
        with pytest.raises(LocalSpeechError, match="not supported"):
            await transcribe(executor, unexpected="value")
        assert not events
    finally:
        executor.close()


@pytest.mark.asyncio
async def test_failed_inference_releases_model_and_can_retry() -> None:
    events: list[Any] = []
    entry = definition("first", events)
    model = Engine("first", events)
    model.transcribe = MagicMock(side_effect=RuntimeError("private audio text"))  # type: ignore[method-assign]
    executor = LocalSpeechExecutor(
        engines=[replace(entry, create=MagicMock(side_effect=[model, Engine("retry", events)]))]
    )
    try:
        with pytest.raises(LocalSpeechExecutionError, match="RuntimeError") as error:
            await transcribe(executor)
        assert "private audio" not in str(error.value)
        assert events == [("first", "close")]
        assert (await transcribe(executor)).text == "retry"
    finally:
        await executor.aclose()
    assert events[-1] == ("retry", "close")


@pytest.mark.asyncio
async def test_digital_silence_and_invalid_audio() -> None:
    events: list[Any] = []
    executor = LocalSpeechExecutor(engines=[definition("first", events)])
    try:
        result = await executor.transcribe(
            "first", wav(np.zeros(1600)), filename="a.wav", media_type="audio/wav", options={}
        )
        assert result.text == ""
        assert not events
        with pytest.raises(LocalSpeechExecutionError):
            await executor.transcribe(
                "first", b"broken", filename="a.wav", media_type="audio/wav", options={}
            )
    finally:
        await executor.aclose()


def test_long_audio_chunks_cover_every_sample_once_and_use_quiet_boundary() -> None:
    samples = np.full(65 * 16_000, 1000, dtype=np.int16)
    samples[29 * 16_000 + 640 : 29 * 16_000 + 960] = 0
    chunks = list(_audio_chunks(wav(samples)))
    assert len(chunks) == 3
    assert len(chunks[0][1]) == 29 * 16_000 + 960
    offset = 0
    for start, chunk in chunks:
        assert start == offset
        assert len(chunk) <= 30 * 16_000
        assert chunk.ndim == 1 and chunk.dtype == np.float32
        offset += len(chunk)
    np.testing.assert_array_equal(
        np.concatenate([chunk for _, chunk in chunks]), samples.astype(np.float32) / 32768
    )


def test_stereo_48khz_is_resampled_to_mono_16khz() -> None:
    chunks = list(_audio_chunks(wav(np.full((48_000, 2), 1000), rate=48_000, channels=2)))
    assert len(chunks) == 1
    assert len(chunks[0][1]) == 16_000
    assert np.isfinite(chunks[0][1]).all()


@pytest.mark.asyncio
async def test_cancellation_waits_for_inference_then_shutdown_releases_model() -> None:
    events: list[Any] = []
    started: Future[int] = Future()
    release = threading.Event()
    entry = definition("first", events)
    model = Engine("first", events)

    def blocking(_samples: Any, _options: Mapping[str, Any]) -> SpeechTranscriptionResult:
        started.set_result(threading.get_ident())
        release.wait(timeout=5)
        return SpeechTranscriptionResult(text="done")

    model.transcribe = MagicMock(side_effect=blocking)  # type: ignore[method-assign]
    executor = LocalSpeechExecutor(engines=[replace(entry, create=lambda _options: model)])
    task = asyncio.create_task(transcribe(executor))
    try:
        assert await asyncio.wrap_future(started) != threading.get_ident()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        closing = asyncio.create_task(executor.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert events == [("first", "close")]
    finally:
        release.set()
        await executor.aclose()


@pytest.mark.parametrize("engine_name", ["qwen", "parakeet"])
def test_native_transformers_adapter_contracts_without_weights(
    engine_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    torch = pytest.importorskip("torch")
    transformers = pytest.importorskip("transformers")
    import huggingface_hub

    from core.model_tasks.speech_local import _ParakeetEngine, _QwenEngine

    engine_type = _QwenEngine if engine_name == "qwen" else _ParakeetEngine
    snapshot = MagicMock(return_value=engine_type.default_model)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot)
    # Resolve actual native auto classes: this catches unsupported extra versions.
    auto_class = getattr(transformers, engine_type.model_class)
    model = MagicMock(device=torch.device("cpu"), dtype=torch.float32)
    model.to.return_value = model
    model.eval.return_value = model
    processor = MagicMock()
    inputs = transformers.BatchFeature(
        {"input_ids": torch.tensor([[1, 2]]), "input_features": torch.ones(1, 8)}
    )
    processor.apply_transcription_request.return_value = inputs
    processor.return_value = inputs
    model.generate.return_value = (
        torch.tensor([[1, 2, 3]])
        if engine_name == "qwen"
        else SimpleNamespace(sequences=torch.tensor([[3]]))
    )
    processor.decode.return_value = (
        [{"transcription": "Hallo", "language": None}] if engine_name == "qwen" else ["Hallo"]
    )
    load_model = MagicMock(return_value=model)
    load_processor = MagicMock(return_value=processor)
    monkeypatch.setattr(auto_class, "from_pretrained", load_model)
    monkeypatch.setattr(transformers.AutoProcessor, "from_pretrained", load_processor)
    options = {"device": "cpu", "offline": True, "language": "de", "prompt": "vBot"}
    engine = engine_type(options)
    try:
        result = engine.transcribe(np.ones(1600, dtype=np.float32), options)
        assert result.text == "Hallo"
        assert load_model.call_args.kwargs["local_files_only"] is True
        assert snapshot.call_args.kwargs["local_files_only"] is True
        progress_class = snapshot.call_args.kwargs["tqdm_class"]
        with progress_class(total=10, unit="B") as bar:
            bar.update(5)
        assert load_model.call_args.kwargs["trust_remote_code"] is False
        assert load_processor.call_args.args == (engine_type.default_model,)
        assert inputs["input_ids"].dtype == torch.int64
        if engine_name == "qwen":
            assert result.language == "de"
            assert processor.apply_transcription_request.call_args.kwargs["audio_kwargs"] == {
                "sampling_rate": 16_000
            }
            assert processor.apply_transcription_request.call_args.kwargs["prompt"] == "vBot"
            assert processor.decode.call_args.args[0].tolist() == [[3]]
        else:
            assert processor.call_args.kwargs["sampling_rate"] == 16_000
    finally:
        engine.close()
    assert engine._model is None and engine._processor is None


@pytest.mark.asyncio
@pytest.mark.parametrize("gpu", [False, True])
async def test_setup_installs_only_shipped_dependencies_and_verifies_before_restart(
    monkeypatch: pytest.MonkeyPatch,
    gpu: bool,
) -> None:
    from core.model_tasks import speech_local

    monkeypatch.setattr(speech_local, "_dependencies_available", lambda: False)
    monkeypatch.setattr(speech_local.shutil, "which", lambda _name: "nvidia-smi" if gpu else None)
    commands: list[list[str]] = []
    setup = LocalSpeechSetup()

    async def command(arguments: Any, **kwargs: Any) -> int:
        commands.append(list(arguments))
        return (
            1
            if arguments[-1].startswith("import torch") and "transformers" not in arguments[-1]
            else 0
        )

    monkeypatch.setattr(setup, "_command", command)
    first = setup.install()
    task = setup._task
    assert first["state"] == "installing"
    assert setup.install()["state"] == "installing" and setup._task is task
    assert task is not None
    await task
    assert setup.status()["state"] == "restart_required"
    assert setup.blocks_execution
    assert setup.install()["state"] == "restart_required" and setup._task is task
    pip_commands = [argv for argv in commands if "install" in argv]
    assert len(pip_commands) == 2
    assert all(argv[:3] == [speech_local.sys.executable, "-m", "pip"] for argv in pip_commands)
    assert all("-e" not in argv and ".[local-speech]" not in argv for argv in pip_commands)
    assert "--no-deps" in pip_commands[0] and "--force-reinstall" in pip_commands[0]
    assert ("https://download.pytorch.org/whl/cu128" in pip_commands[0]) is gpu
    assert "AutoModelForTDT" in commands[-1][-1]
    await setup.aclose()


@pytest.mark.asyncio
async def test_failed_setup_can_retry_and_active_setup_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.model_tasks import speech_local

    monkeypatch.setattr(speech_local, "_dependencies_available", lambda: False)
    monkeypatch.setattr(speech_local.shutil, "which", lambda _name: None)
    setup = LocalSpeechSetup()
    monkeypatch.setattr(setup, "_command", AsyncMock(return_value=0))
    install = AsyncMock(return_value=1)
    monkeypatch.setattr(setup, "_pip", install)
    setup.install()
    assert setup._task is not None
    await setup._task
    assert setup.status()["state"] == "failed"
    assert setup.status()["error"] == "install_failed"
    install.return_value = 0
    setup.install()
    await setup._task
    assert setup.status()["state"] == "restart_required"
    await setup.aclose()

    setup = LocalSpeechSetup()
    entered = asyncio.Event()

    async def blocking(arguments: Any, **kwargs: Any) -> int:
        entered.set()
        await asyncio.Event().wait()
        return 0

    monkeypatch.setattr(setup, "_command", blocking)
    setup.install()
    await entered.wait()
    await setup.aclose()
    assert setup.status()["error"] == "interrupted"
    assert setup._task is not None and setup._task.done()


@pytest.mark.asyncio
async def test_setup_ready_is_a_noop_and_status_never_imports_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.model_tasks import speech_local

    monkeypatch.setattr(speech_local, "_dependencies_available", lambda: True)
    setup = LocalSpeechSetup()
    assert setup.install()["state"] == "ready"
    assert setup._task is None
    await setup.aclose()


@pytest.mark.asyncio
async def test_setup_blocks_catalog_and_inference_until_a_new_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.model_tasks import speech_local

    monkeypatch.setattr(speech_local, "_dependencies_available", lambda: False)
    monkeypatch.setattr(speech_local.shutil, "which", lambda _name: None)
    events: list[Any] = []
    executor = LocalSpeechExecutor(engines=[definition("first", events)])
    monkeypatch.setattr(executor.setup, "_command", AsyncMock(return_value=0))
    try:
        assert executor._definitions["first"].descriptor.can_execute()
        executor.setup.install()
        assert not executor._definitions["first"].descriptor.can_execute()
        assert executor.setup._task is not None
        await executor.setup._task
        assert executor.setup.status()["state"] == "restart_required"
        assert not executor._definitions["first"].descriptor.can_execute()
        with pytest.raises(LocalSpeechError):
            await transcribe(executor)
        assert events == []
    finally:
        await executor.aclose()


@pytest.mark.asyncio
async def test_request_progress_survives_worker_boundary_and_clears_between_recordings() -> None:
    from core.model_tasks.speech_local import _PROGRESS

    progress = SpeechProgress()
    observed: list[str] = []
    events: list[Any] = []
    entry = definition("first", events)

    def create(options: Mapping[str, Any]) -> Engine:
        assert _PROGRESS.get() is progress
        observed.append(progress.snapshot()["phase"])
        model = Engine("first", events)
        original = model.transcribe

        def run(samples: Any, options: Mapping[str, Any]) -> SpeechTranscriptionResult:
            current = _PROGRESS.get()
            observed.append(current.snapshot()["phase"] if current else "none")
            return original(samples, options)

        model.transcribe = run  # type: ignore[method-assign]
        return model

    executor = LocalSpeechExecutor(engines=[replace(entry, create=create)])
    try:
        await executor.transcribe(
            "first", wav(), filename="a.wav", media_type="audio/wav", options={}, progress=progress
        )
        assert observed == ["loading", "transcribing"]
        assert _PROGRESS.get() is None
        await transcribe(executor)
        assert observed[-1] == "none"
        assert len([event for event in events if event[1] == "transcribe"]) == 2
    finally:
        await executor.aclose()


@pytest.mark.asyncio
async def test_setup_command_reaps_cancelled_child_without_exposing_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = MagicMock(returncode=None)
    process.stdout = asyncio.StreamReader()
    process.stdout.feed_data(b"Downloading https://secret:password@example.invalid/pkg.whl\n")
    process.wait = AsyncMock(return_value=0)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    setup = LocalSpeechSetup()
    task = asyncio.create_task(setup._command(["python", "-m", "pip"], progress=True))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()
    assert "password" not in str(setup.status())
