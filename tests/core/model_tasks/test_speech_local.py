"""Local STT contracts without network access or pretrained model downloads."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import wave
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from core.model_tasks.constants import TASK_SPEECH_TO_TEXT, TASK_TEXT_TO_SPEECH
from core.model_tasks.local_targets import LocalTaskTargetDescriptor
from core.model_tasks.model_files import ModelFile, PinnedModel
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
from core.model_tasks.speech_types import (
    SpeechProgress,
    SpeechSynthesisResult,
    SpeechTranscriptionResult,
)


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


def test_builtin_catalog_is_lazy_and_needs_dependencies_and_installed_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from core.model_tasks import speech_setup as speech_local

    versions = {"torch": "2.11.0+cu128", "transformers": "5.16.1", "huggingface-hub": "1.30.0"}
    monkeypatch.setattr(speech_local.metadata, "version", versions.__getitem__)
    monkeypatch.setattr(speech_local.util, "find_spec", lambda _name: None)
    assert [engine.descriptor.public_id for engine in builtin_speech_engines()] == [
        "local/qwen3-asr-1.7b",
        "local/qwen3-asr-0.6b",
        "local/parakeet",
        "local/nemotron3.5-asr",
    ]
    executor = LocalSpeechExecutor(engines_dir=tmp_path)
    try:
        engines = [
            entry.descriptor
            for entry in executor._definitions.values()
            if TASK_SPEECH_TO_TEXT in entry.descriptor.task_types
        ]
        assert all((engine.metadata or {}).get("download_bytes", 0) > 0 for engine in engines)
        assert not any(engine.can_execute() for engine in engines)
        monkeypatch.setattr(speech_local.util, "find_spec", lambda _name: object())
        # The packages alone are not enough: each target needs its own installed model.
        assert not any(engine.can_execute() for engine in engines)
        setup = executor.setup_for("local/parakeet")
        assert setup.status()["error"] == "model_missing"
        assert setup.model_directory is not None
        setup.model_directory.mkdir(parents=True)
        (setup.model_directory / "verified.json").write_text("{}", encoding="utf-8")
        assert [engine.can_execute() for engine in engines] == [False, False, True, False]
        versions["torch"] = "2.9.0"
        assert not any(engine.can_execute() for engine in engines)
        monkeypatch.setattr(speech_local.metadata, "version", lambda _name: "5.12.0")
        assert not any(engine.can_execute() for engine in engines)
        assert "prompt" not in {field.name for field in engines[2].option_fields}
    finally:
        executor.close()


@pytest.mark.asyncio
async def test_third_engine_reuses_and_switches_with_its_own_options(tmp_path: Path) -> None:
    events: list[Any] = []
    model = PinnedModel("owner/third", "a" * 40, (ModelFile("model.bin", "0" * 64, 1),))
    executor = LocalSpeechExecutor(
        engines=[definition("first", events), replace(definition("third", events), model=model)],
        engines_dir=tmp_path,
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
        # A target with a pinned model loads the installed copy.
        installed = str(tmp_path / "models" / "third" / model.revision)
        assert events[-2] == ("third", "load", {"custom": "one", "model_path": installed})
        assert [model["loaded"] for model in executor.memory_status()["models"]] == [True, True]
        await executor.release_memory("local/third")
        assert events[-1] == ("third", "close")
        assert [model["loaded"] for model in executor.memory_status()["models"]] == [True, False]
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


@pytest.mark.parametrize("engine_name", ["qwen", "parakeet", "nemotron"])
def test_native_transformers_adapter_contracts_without_weights(
    engine_name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Stand-in torch and transformers modules: the adapters import them lazily, and
    # the real native imports cost several seconds without adding adapter coverage.
    from core.model_tasks.speech_local import (
        _PROGRESS,
        _NemotronEngine,
        _ParakeetEngine,
        _QwenEngine,
    )

    engine_types: dict[str, type[_QwenEngine] | type[_ParakeetEngine] | type[_NemotronEngine]] = {
        "qwen": _QwenEngine,
        "parakeet": _ParakeetEngine,
        "nemotron": _NemotronEngine,
    }
    engine_type = engine_types[engine_name]
    torch = ModuleType("torch")
    torch.float32 = "float32"  # type: ignore[attr-defined]
    torch.cuda = SimpleNamespace(is_available=lambda: False)  # type: ignore[attr-defined]
    torch.backends = SimpleNamespace(  # type: ignore[attr-defined]
        mps=SimpleNamespace(is_available=lambda: False)
    )
    torch.inference_mode = contextlib.nullcontext  # type: ignore[attr-defined]
    torch.nn = SimpleNamespace(functional=SimpleNamespace(pad=_pad))  # type: ignore[attr-defined]
    model = MagicMock(device="cpu", dtype=torch.float32, max_symbols_per_step=10)
    model.to.return_value = model
    model.eval.return_value = model
    processor = MagicMock(num_mel_frames_first_audio_chunk=49, num_mel_frames_per_audio_chunk=56)
    inputs = _Features(
        input_ids=np.array([[1, 2]]),
        input_features=np.ones((1, 57, 8), dtype=np.float32),
        attention_mask=np.ones((1, 57), dtype=np.int64),
        prompt_ids=np.array([9]),
    )
    processor.apply_transcription_request.return_value = inputs
    processor.return_value = inputs
    model.generate.return_value = (
        np.array([[1, 2, 3]])
        if engine_name == "qwen"
        else SimpleNamespace(sequences=np.array([[3]]))
    )
    processor.decode.return_value = (
        [{"transcription": "Hallo", "language": None}] if engine_name == "qwen" else ["Hallo"]
    )
    load_model = MagicMock(return_value=model)
    load_processor = MagicMock(return_value=processor)
    transformers = ModuleType("transformers")
    transformers.AutoProcessor = SimpleNamespace(  # type: ignore[attr-defined]
        from_pretrained=load_processor
    )
    setattr(transformers, engine_type.model_class, SimpleNamespace(from_pretrained=load_model))
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    with pytest.raises(LocalSpeechExecutionError, match="model directory does not exist"):
        engine_type({"model_path": str(tmp_path / "missing")})
    options = {"device": "cpu", "language": "de", "prompt": "vBot", "model_path": str(tmp_path)}
    progress = SpeechProgress()
    with _PROGRESS.set(progress):
        engine = engine_type(options)
    try:
        result = engine.transcribe(np.ones(1600, dtype=np.float32), options)
        assert result.text == "Hallo"
        assert load_model.call_args.kwargs == {
            "dtype": "float32",
            "local_files_only": True,
            "trust_remote_code": False,
        }
        model.to.assert_called_once_with("cpu")
        model.eval.assert_called_once_with()
        assert progress.snapshot()["phase"] == "loading"
        assert load_processor.call_args.args == (str(tmp_path),)
        assert load_model.call_args.args == (str(tmp_path),)
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


class _Features(dict[str, Any]):
    """Processor output: numpy arrays stand in for tensors, so no native import is needed."""

    def to(self, *_args: Any) -> _Features:
        return self


def _pad(chunk: Any, padding: tuple[int, int, int, int]) -> Any:
    # torch.nn.functional.pad lists padding from the last dimension backwards.
    last_before, last_after, frames_before, frames_after = padding
    return np.pad(chunk, ((0, 0), (frames_before, frames_after), (last_before, last_after)))


@pytest.mark.parametrize(
    ("frames", "language"),
    [(1, ""), (48, "de-DE"), (49, ""), (50, "de-DE"), (104, ""), (105, "de-DE"), (106, "")]
    + [(3000, "de-DE")],
)
def test_nemotron_keeps_every_streaming_frame_and_language_prompt(frames, language):
    from core.model_tasks.speech_local import _NemotronEngine

    engine = _NemotronEngine.__new__(_NemotronEngine)
    engine._torch = SimpleNamespace(
        nn=SimpleNamespace(functional=SimpleNamespace(pad=_pad)),
        inference_mode=contextlib.nullcontext,
    )
    engine._model = MagicMock(max_symbols_per_step=10)
    processor = engine._processor = MagicMock(
        num_mel_frames_first_audio_chunk=49, num_mel_frames_per_audio_chunk=56
    )
    features = np.arange((frames + 3) * 8, dtype=np.float32).reshape(1, frames + 3, 8)
    mask = np.array([[1] * frames + [0] * 3])
    processor.return_value = _Features(
        input_features=features, attention_mask=mask, prompt_ids=np.array([9])
    )
    processor.decode.side_effect = lambda _tokens, *, skip_special_tokens: [
        "Hallo Welt." if skip_special_tokens else "Hallo Welt.<de-DE>"
    ]
    consumed = []

    def generate(**kwargs):
        assert kwargs["prompt_ids"].tolist() == [9]
        assert kwargs["num_lookahead_tokens"] == 6
        assert "attention_mask" not in kwargs
        consumed.extend(kwargs["input_features"])
        assert kwargs["max_new_tokens"] > len(consumed) * 7 * 10
        return SimpleNamespace(sequences=np.array([[1, 2, 3]]))

    engine._model.generate.side_effect = generate
    result = engine.transcribe(np.ones(1600, dtype=np.float32), {"language": language})
    assert result.text == "Hallo Welt."
    assert result.language == "de-DE"
    assert processor.call_args.kwargs["language"] == (language or "auto")
    processor.set_num_lookahead_tokens.assert_called_once_with(6)
    assert consumed[0].shape[1] == 49
    assert all(chunk.shape[1] == 56 for chunk in consumed[1:])
    joined = np.concatenate(consumed, axis=1)
    assert np.array_equal(joined[:, :frames], features[:, :frames])
    assert not joined[:, frames:].any()


def test_each_builtin_target_has_its_own_setup_and_memory(tmp_path: Path) -> None:
    executor = LocalSpeechExecutor(engines_dir=tmp_path)
    try:
        status = {entry["target"]: entry for entry in executor.memory_status()["models"]}
        assert set(status) == {
            "local/qwen3-asr-1.7b",
            "local/qwen3-asr-0.6b",
            "local/parakeet",
            "local/nemotron3.5-asr",
            "local/qwen3-tts-1.7b",
            "local/qwen3-tts-0.6b",
            "local/chatterbox",
        }
        assert not status["local/nemotron3.5-asr"]["loaded"]
        assert len({id(state.workers) for state in executor._states.values()}) == 7
        setups = [executor.setup_for(target) for target in status]
        assert len({id(setup) for setup in setups}) == 7
        # Sizes of one engine share its environment but keep separate models.
        large, small = (executor.setup_for(f"local/qwen3-tts-{size}") for size in ("1.7b", "0.6b"))
        assert large.directory == small.directory == tmp_path / "qwen3-tts"
        assert large.model_directory != small.model_directory
        with pytest.raises(ValueError):
            executor.setup_for("local/../../escape")
    finally:
        executor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("gpu", [False, True])
async def test_setup_installs_only_shipped_dependencies_and_verifies_before_restart(
    monkeypatch: pytest.MonkeyPatch,
    gpu: bool,
) -> None:
    from core.model_tasks import speech_setup as speech_local

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
async def test_packaged_stt_setup_installs_only_in_managed_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.model_tasks import speech_setup

    setup = LocalSpeechSetup(directory=tmp_path / "speech-engines" / "stt")
    from cli.application.state import Installation

    install = Installation(
        tmp_path / "install",
        "server",
        "127.0.0.1",
        8420,
        str((tmp_path / "data").resolve()),
    )
    runtime = install.root / "versions" / "rel_base" / "runtime"
    runtime.mkdir(parents=True)
    (install.root / "active-version").write_text("rel_base\n", encoding="ascii")
    interpreter = runtime / ("vBot.Python.exe" if os.name == "nt" else "bin/python3")
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.touch()
    if os.name == "nt":
        (runtime / "python.exe").touch()
    # An environment left from a removed installation: its base Python is gone.
    _environment(setup, home=tmp_path / "removed-runtime", version="3.13.15")
    stale = tmp_path / "speech-engines" / "stt" / "Lib" / "stale.py"
    stale.parent.mkdir(parents=True)
    stale.touch()
    assert setup.status()["error"] == "python_missing"
    commands: list[list[str]] = []

    async def command(arguments: Any, **_kwargs: Any) -> int:
        commands.append(list(arguments))
        if "venv" in arguments:
            setup.python.parent.mkdir(parents=True)
            setup.python.touch()
        return 0

    monkeypatch.setattr(setup, "_command", command)
    monkeypatch.setattr(setup, "_packaged_installation", lambda: install)
    monkeypatch.setattr(speech_setup.shutil, "which", lambda _name: None)
    setup.install()
    assert setup._task is not None
    await setup._task
    assert setup.available()
    assert not stale.exists()
    assert not any(command[:3] == [sys.executable, "-m", "pip"] for command in commands)
    installs = [command for command in commands if "install" in command]
    assert installs
    assert all(command[command.index("--python") + 1] == str(setup.python) for command in installs)
    config = setup._config()
    requirements = [
        *config["project"]["dependencies"],
        *config["project"]["optional-dependencies"]["local-speech"],
    ]
    assert installs[-1][-len(requirements) :] == requirements
    assert commands[-1][:3] == [str(setup.python), "-I", "-B"]
    assert "--verify-stt" in commands[-1]
    assert commands[0][:7] == [
        str(install.interpreter()),
        "-m",
        "uv",
        "venv",
        "--python",
        str(runtime / ("python.exe" if os.name == "nt" else "bin/python3")),
        "--seed",
    ]
    await setup.aclose()


def _environment(setup: LocalSpeechSetup, *, home: Path, version: str) -> None:
    """Give *setup* a completed environment based on the Python in *home*."""
    assert setup.directory is not None
    setup.python.parent.mkdir(parents=True)
    setup.python.touch()
    (setup.directory / "pyvenv.cfg").write_text(
        f"home = {home}\nimplementation = CPython\nversion_info = {version}\n", encoding="utf-8"
    )
    setup._write_marker(setup.directory / "verified.json")


@pytest.mark.parametrize("engine", ["", "qwen3-tts"])
@pytest.mark.parametrize("base", ["current", "gone", "other version"])
def test_managed_speech_needs_setup_again_when_its_python_cannot_serve(
    tmp_path: Path, engine: str, base: str
) -> None:
    setup = LocalSpeechSetup(engine=engine, directory=tmp_path / "speech-engines" / "stt")
    current = f"{sys.version_info.major}.{sys.version_info.minor}.0"
    home = tmp_path / "python"
    if base != "gone":
        home.mkdir()
    _environment(setup, home=home, version="3.1.0" if base == "other version" else current)

    # STT runs vBot's source and needs the server's Python; TTS keeps its recipe's.
    expected = {
        "current": "",
        "gone": "python_missing",
        "other version": "" if engine else "python_changed",
    }[base]
    assert setup.status()["error"] == expected
    assert setup.available() is (expected == "")


@pytest.mark.parametrize("engine", ["", "qwen3-tts", "chatterbox"])
def test_completed_managed_speech_setup_survives_application_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    from core.model_tasks import speech_setup

    directory = tmp_path / "speech-engines" / (engine or "stt")
    setup = LocalSpeechSetup(engine=engine, directory=directory)
    setup.python.parent.mkdir(parents=True)
    setup.python.touch()
    marker = directory / "verified.json"
    receipt = json.dumps(
        {
            "recipe": {"base": ["websockets>=14,<18"], "speech": ["torch>=2.10,<3"]},
            "sources": {
                "speech_local.py": "previous-release",
                "speech_worker.py": "previous-release",
            },
        }
    )
    marker.write_text(receipt, encoding="utf-8")
    # Neither changed requirements nor changed worker source is runtime evidence
    # that a completed environment stopped working. No metadata rewrite, import,
    # subprocess, or setup should be needed to use it after an update.
    monkeypatch.setattr(setup, "_config", MagicMock(side_effect=AssertionError("recipe read")))
    monkeypatch.setattr(speech_setup, "__file__", str(tmp_path / "new-release" / "speech_setup.py"))
    monkeypatch.setattr(
        speech_setup, "_dependencies_available", MagicMock(side_effect=AssertionError("host probe"))
    )

    assert setup.available()
    assert setup.install()["state"] == "ready"
    assert setup._task is None
    assert marker.read_text(encoding="utf-8") == receipt


def test_managed_speech_reports_concrete_failures_only_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.model_tasks import speech_setup

    logger = MagicMock()
    monkeypatch.setattr(speech_setup, "_LOGGER", logger)
    setup = LocalSpeechSetup(directory=tmp_path / "stt")
    assert not setup.available()
    assert setup.status()["error"] == "python_missing"
    logger.warning.assert_not_called()
    for _ in range(3):
        assert setup.status(log_unavailable=True)["error"] == "python_missing"
    assert logger.warning.call_count == 1
    assert logger.warning.call_args.args[1:3] == ("stt", "python_missing")
    setup.python.parent.mkdir(parents=True)
    setup.python.touch()
    assert setup.status(log_unavailable=True)["error"] == "setup_incomplete"
    assert logger.warning.call_args.args[1:3] == ("stt", "setup_incomplete")
    marker = tmp_path / "stt" / "verified.json"
    setup._write_marker(marker)
    setup._state = "ready"
    assert setup.status(log_unavailable=True)["state"] == "ready"
    assert setup.available()
    logger.info.assert_called_once()
    marker.unlink()
    assert setup.status(log_unavailable=True)["state"] == "missing"
    assert logger.warning.call_count == 3
    assert not setup.available()


@pytest.mark.parametrize("state", ["installing", "failed", "restart_required"])
def test_managed_speech_never_runs_during_incomplete_setup(tmp_path: Path, state: str) -> None:
    setup = LocalSpeechSetup(directory=tmp_path)
    setup.python.parent.mkdir(parents=True)
    setup.python.touch()
    setup._write_marker(tmp_path / "verified.json")
    setup._state = state
    assert not setup.available()


def test_managed_speech_reports_filesystem_access_failure(
    tmp_path: Path, deny_access: Callable[[Path], None]
) -> None:
    setup = LocalSpeechSetup(directory=tmp_path / "stt")
    current = f"{sys.version_info.major}.{sys.version_info.minor}.0"
    home = tmp_path / "base" / "python"
    home.mkdir(parents=True)
    _environment(setup, home=home, version=current)
    # The base Python cannot be checked, which is not the same as gone.
    deny_access(home.parent)

    assert setup.status()["error"] == "environment_unreadable"
    assert not setup.available()
    # Setup keeps an environment it cannot check instead of deleting it to start over.
    assert not setup._environment_needed()
    assert (tmp_path / "stt" / "verified.json").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["", "qwen3-tts", "chatterbox"])
async def test_failed_environment_recreation_cannot_reuse_old_completion_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    setup = LocalSpeechSetup(engine=engine, directory=tmp_path)
    setup._write_marker(tmp_path / "verified.json")

    # The interpreter is missing, but an earlier completion receipt remains.
    # A failed recreation must discard that receipt before any changes, even
    # if the failing operation leaves a new interpreter behind.
    def fail_after_creating_interpreter() -> Any:
        setup.python.parent.mkdir(parents=True)
        setup.python.touch()
        raise OSError("interrupted environment recreation")

    monkeypatch.setattr(setup, "_config", fail_after_creating_interpreter)
    setup.install()
    assert setup._task is not None
    await setup._task
    assert setup.status()["state"] == "failed"
    fresh = LocalSpeechSetup(engine=engine, directory=tmp_path)
    assert fresh.status()["error"] == "setup_incomplete"
    assert not fresh.available()
    await setup.aclose()


# Minimal STT source a managed worker child imports instead of the real engines.
_FAKE_STT_SOURCE = """
import time
from contextvars import ContextVar
from pathlib import Path

_PROGRESS = ContextVar("progress", default=None)


class _Result:
    def __init__(self, text):
        self.text = text

    def to_dict(self):
        return {"text": self.text, "language": "en"}


class _Model:
    def __init__(self, options):
        _PROGRESS.get().update("loading")
        if options.get("marker"):
            Path(options["marker"]).touch()
            time.sleep(60)

    def transcribe(self, samples, options):
        return _Result(f"{len(samples)} samples in {options['language']}")


class _Definition:
    descriptor = type("Descriptor", (), {"id": "fake"})
    create = _Model


def builtin_speech_engines():
    return (_Definition(),)
"""


def _fake_stt_app(root: Path) -> tuple[Any, Path]:
    """Return a setup running this interpreter and an app root with a fake engine."""
    package = root / "app" / "core" / "model_tasks"
    package.mkdir(parents=True)
    (root / "app" / "core" / "__init__.py").touch()
    (package / "__init__.py").touch()
    # The isolated child still needs this interpreter's packages, such as numpy.
    source = f"import sys\nsys.path.extend({sys.path!r})\n{_FAKE_STT_SOURCE}"
    (package / "speech_local.py").write_text(source, encoding="utf-8")
    return SimpleNamespace(python=Path(sys.executable)), root / "app"


def test_managed_stt_worker_loads_before_transcribing_and_forwards_progress(
    tmp_path: Path,
) -> None:
    from core.model_tasks.speech_local import _PROGRESS, _LoadingProcesses, _ManagedSttEngine

    setup, app = _fake_stt_app(tmp_path)
    progress = SpeechProgress()
    token = _PROGRESS.set(progress)
    engine = None
    try:
        # Construction returns only once the child reports the model loaded.
        engine = _ManagedSttEngine(setup, app, "fake", _LoadingProcesses(), {"language": "en"})
        assert progress.snapshot()["phase"] == "loading"
        result = engine.transcribe(np.asarray([0.25, -0.5], dtype=np.float32), {"language": "en"})
    finally:
        _PROGRESS.reset(token)
        if engine is not None:
            engine.close()
    assert isinstance(result, SpeechTranscriptionResult)
    assert (result.text, result.language) == ("2 samples in en", "en")
    assert progress.snapshot()["phase"] == "transcribing"


@pytest.mark.asyncio
async def test_shutdown_during_managed_preload_ends_the_loading_worker(tmp_path: Path) -> None:
    from core.model_tasks.speech_local import _ManagedSttEngine

    setup, app = _fake_stt_app(tmp_path)
    marker = tmp_path / "loading"
    entry = definition("fake", [])
    executor = LocalSpeechExecutor(
        engines=[
            replace(
                entry,
                create=lambda _options: _ManagedSttEngine(
                    setup, app, "fake", executor._loading, {"marker": str(marker)}
                ),
            )
        ]
    )
    try:
        assert executor.prepare("fake", {}) == "loading"
        async with asyncio.timeout(10):
            while not marker.exists():
                await asyncio.sleep(0.02)
        # The child would load for a minute; shutdown must end it instead of waiting.
        await asyncio.wait_for(executor.aclose(), 10)
        assert executor.memory_status()["models"][0]["loaded"] is False
        assert executor.prepare("fake", {}) == "unavailable"
    finally:
        await executor.aclose()


@pytest.mark.asyncio
async def test_prepare_loads_once_in_background_and_transcription_waits_for_it() -> None:
    events: list[Any] = []
    gate = threading.Event()
    gate.set()
    failures = iter([RuntimeError("private load failure")])

    def create(options: Mapping[str, Any]) -> Engine:
        events.append(("first", "load", dict(options)))
        if (failure := next(failures, None)) is not None:
            raise failure
        assert gate.wait(5)
        return Engine("first", events)

    executor = LocalSpeechExecutor(engines=[replace(definition("first", events), create=create)])
    try:
        assert executor.prepare("unknown", {}) == "unavailable"
        assert executor.prepare("first", {"unexpected": "value"}) == "unavailable"
        # A failed preload is only logged; the next transcription loads again.
        assert executor.prepare("first", {}) == "loading"
        await asyncio.sleep(0)
        assert (await transcribe(executor)).text == "first"
        assert [event[1] for event in events] == ["load", "load", "transcribe"]
        assert executor.prepare("first", {}) == "loaded"

        gate.clear()
        events.clear()
        assert executor.prepare("first", {"custom": "two"}) == "loading"
        assert executor.prepare("first", {"custom": "two"}) == "loading"
        request = asyncio.create_task(transcribe(executor, custom="two"))
        await asyncio.sleep(0)
        assert not request.done()
        gate.set()
        assert (await request).text == "first"
        assert [event[1] for event in events] == ["close", "load", "transcribe"]
    finally:
        gate.set()
        await executor.aclose()
    assert executor.prepare("first", {}) == "unavailable"


def test_managed_stt_worker_starts_without_unrelated_image_dependencies(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    worker = root / "core" / "model_tasks" / "speech_worker.py"
    startup = (
        "import runpy, sys\n"
        "sys.modules['pillow_heif'] = None\n"
        "sys.argv = sys.argv[1:]\n"
        "runpy.run_path(sys.argv[0], run_name='__main__')\n"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", startup, str(worker), "--stt", "nemotron3.5-asr", str(root)],
        input="",
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_packaged_detection_requires_release_and_shipped_app_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.model_tasks import speech_local

    installed = tmp_path / "roles" / "Server" / "Lib" / "site-packages" / "core" / "model_tasks"
    installed.mkdir(parents=True)
    monkeypatch.setattr(speech_local, "__file__", str(installed / "speech_local.py"))
    assert speech_local._packaged_app_root() is None
    (tmp_path / "release.json").write_text("{}", encoding="utf-8")
    shipped = tmp_path / "app" / "core" / "model_tasks"
    shipped.mkdir(parents=True)
    (shipped / "speech_local.py").touch()
    assert speech_local._packaged_app_root() == tmp_path / "app"


@pytest.mark.asyncio
async def test_failed_setup_can_retry_and_active_setup_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.model_tasks import speech_setup as speech_local

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
    from core.model_tasks import speech_setup as speech_local

    monkeypatch.setattr(speech_local, "_dependencies_available", lambda: True)
    setup = LocalSpeechSetup()
    assert setup.install()["state"] == "ready"
    assert setup._task is None
    await setup.aclose()


@pytest.mark.asyncio
async def test_setup_blocks_catalog_and_inference_until_a_new_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.model_tasks import speech_setup as speech_local

    monkeypatch.setattr(speech_local, "_dependencies_available", lambda: False)
    monkeypatch.setattr(speech_local.shutil, "which", lambda _name: None)
    events: list[Any] = []
    executor = LocalSpeechExecutor(
        engines=[definition("first", events), definition("second", events)]
    )
    setup = executor.setup_for("local/first")
    monkeypatch.setattr(setup, "_command", AsyncMock(return_value=0))
    try:
        assert executor._definitions["first"].descriptor.can_execute()
        setup.install()
        assert not executor._definitions["first"].descriptor.can_execute()
        assert setup._task is not None
        await setup._task
        assert setup.status()["state"] == "restart_required"
        # The development checkout's targets share the server's packages.
        assert executor.setup_for("local/second").status()["state"] == "restart_required"
        for name in ("first", "second"):
            assert not executor._definitions[name].descriptor.can_execute()
            with pytest.raises(LocalSpeechError):
                await transcribe(executor, name)
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
    kill_tree = AsyncMock()
    monkeypatch.setattr("core.utils.processes.kill_process_tree_async", kill_tree)
    setup = LocalSpeechSetup()
    task = asyncio.create_task(setup._command(["python", "-m", "pip"], progress=True))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    kill_tree.assert_awaited_once_with(process)
    process.wait.assert_awaited_once()
    assert "password" not in str(setup.status())


@pytest.mark.asyncio
@pytest.mark.parametrize("task_type", [TASK_SPEECH_TO_TEXT, TASK_TEXT_TO_SPEECH])
async def test_manual_memory_release_guards_work_and_reloads_both_speech_tasks(task_type):
    started = asyncio.Event()
    finish = threading.Event()
    loop = asyncio.get_running_loop()
    events = []

    class BlockingEngine:
        def __init__(self, _options):
            events.append("load")

        def work(self):
            loop.call_soon_threadsafe(started.set)
            assert finish.wait(5)

        def transcribe(self, _samples, _options):
            self.work()
            return SpeechTranscriptionResult(text="result")

        def synthesize(self, _text, _options):
            self.work()
            return SpeechSynthesisResult(b"audio", "audio/wav", "wav")

        def close(self):
            events.append("close")

    entry = SpeechEngineDefinition(
        LocalTaskTargetDescriptor(
            id="test", label="Test voice", task_types=(task_type,), availability=lambda: True
        ),
        BlockingEngine,
    )
    executor = LocalSpeechExecutor(engines=[entry])

    async def request():
        if task_type == TASK_SPEECH_TO_TEXT:
            return await transcribe(executor, "test")
        return await executor.synthesize("test", "hello", options={})

    pending = None
    try:
        empty = {"target": "local/test", "label": "Test voice", "loaded": False, "busy": False}
        assert executor.memory_status() == {"models": [empty]}
        assert not (await executor.release_memory("local/test"))["released"]
        pending = asyncio.create_task(request())
        await asyncio.wait_for(started.wait(), 2)
        status = executor.memory_status()
        assert status == {"models": [{**empty, "loaded": True, "busy": True}]}
        assert await asyncio.wait_for(executor.release_memory("local/test"), 0.5) == {
            **status,
            "released": False,
        }
        # A cancelled queued request and cancellation of admitted work must not
        # make the UI offer release while the native worker is still running.
        queued = asyncio.create_task(request())
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        pending.cancel()
        await asyncio.sleep(0)
        assert executor.memory_status()["models"][0]["busy"]
        assert not (await executor.release_memory("local/test"))["released"]
        assert events == ["load"]
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not executor.memory_status()["models"][0]["busy"]
        released = await executor.release_memory("local/test")
        assert released == {"models": [empty], "released": True}
        assert events == ["load", "close"]
        assert not (await executor.release_memory("local/test"))["released"]
        await request()
        assert events == ["load", "close", "load"]
        assert executor.memory_status()["models"][0]["target"] == "local/test"
    finally:
        finish.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await executor.aclose()
    assert events[-1] == "close"


@pytest.mark.asyncio
async def test_unloading_stt_does_not_wait_for_or_close_busy_tts():
    events = []
    started = asyncio.Event()
    finish = threading.Event()
    loop = asyncio.get_running_loop()

    class Voice:
        def synthesize(self, _text, _options):
            loop.call_soon_threadsafe(started.set)
            assert finish.wait(5)
            return SpeechSynthesisResult(b"audio", "audio/wav", "wav")

        def close(self):
            events.append(("tts", "close"))

    executor = LocalSpeechExecutor(
        engines=[
            definition("stt", events),
            SpeechEngineDefinition(
                LocalTaskTargetDescriptor(
                    id="tts",
                    label="TTS",
                    task_types=(TASK_TEXT_TO_SPEECH,),
                    availability=lambda: True,
                ),
                lambda _options: Voice(),
            ),
        ]
    )
    task = None
    try:
        await transcribe(executor, "stt")
        task = asyncio.create_task(executor.synthesize("tts", "hello", options={}))
        await asyncio.wait_for(started.wait(), 2)
        result = await asyncio.wait_for(executor.release_memory("local/stt"), 0.5)
        assert result["released"]
        stt, tts = result["models"]
        assert not stt["loaded"] and not stt["busy"]
        assert tts["loaded"] and tts["busy"]
        assert events[-1] == ("stt", "close")
        assert ("tts", "close") not in events
        finish.set()
        assert (await task).audio == b"audio"
        await transcribe(executor, "stt")
        assert sum(event[1] == "load" for event in events) == 2
        assert all(model["loaded"] for model in executor.memory_status()["models"])
        with pytest.raises(ValueError):
            await executor.release_memory("local/unknown")
        assert ("tts", "close") not in events
    finally:
        finish.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await executor.aclose()


@pytest.mark.asyncio
async def test_shutdown_waits_for_other_engines_even_when_one_close_fails():
    events = []
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    first = Engine("first", events)
    second = Engine("second", events)

    def close_second():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        events.append("second closed")

    first.close = MagicMock(side_effect=RuntimeError("test close failure"))
    second.close = MagicMock(side_effect=close_second)
    executor = LocalSpeechExecutor(
        engines=[
            replace(definition("first", events), create=lambda _options: first),
            replace(definition("second", events), create=lambda _options: second),
        ]
    )
    await transcribe(executor, "first")
    await transcribe(executor, "second")
    closing = asyncio.create_task(executor.aclose())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert not closing.done()
        release.set()
        with pytest.raises(RuntimeError):
            await closing
        assert events[-1] == "second closed"
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)


@pytest.mark.parametrize("task_type", [TASK_SPEECH_TO_TEXT, TASK_TEXT_TO_SPEECH])
def test_local_speech_options_do_not_expose_an_offline_switch(task_type):
    executor = LocalSpeechExecutor()
    try:
        definitions = [
            entry
            for entry in executor._definitions.values()
            if task_type in entry.descriptor.task_types
        ]
        assert len(definitions) == (4 if task_type == TASK_SPEECH_TO_TEXT else 3)
        for entry in definitions:
            assert "offline" not in {field.name for field in entry.descriptor.option_fields}
            assert "offline" not in entry.load_options
    finally:
        executor.close()
