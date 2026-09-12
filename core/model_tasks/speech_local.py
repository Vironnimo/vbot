"""Optional local speech engines, their catalog, and serialized model lifecycle.

An engine definition owns its options, availability and factory together. The
executor knows only the synchronous transcription/close protocol; adding an
engine does not change SpeechService, the server, or any accessor.
"""

from __future__ import annotations

import asyncio
import gc
import io
import json
import os
import re
import signal
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from threading import Timer
from typing import Any, Protocol, cast

from core.model_tasks.constants import TASK_SPEECH_TO_TEXT, TASK_TEXT_TO_SPEECH
from core.model_tasks.local_targets import LocalTaskTargetDescriptor, LocalTaskTargetRegistry
from core.model_tasks.options import (
    TaskModelOptionChoice,
    TaskModelOptionField,
    TaskModelOptionSchema,
    validate_task_model_options,
)
from core.model_tasks.speech_setup import LocalSpeechSetup, _dependencies_available
from core.model_tasks.speech_types import (
    SpeechProgress,
    SpeechSynthesisResult,
    SpeechTranscriptionResult,
)
from core.model_tasks.speech_worker import resolve_snapshot
from core.utils.errors import VBotError
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

_LOGGER = get_logger("speech.local")
_SAMPLE_RATE = 16_000
_CHUNK_SAMPLES = 30 * _SAMPLE_RATE
_LOAD_OPTIONS = ("model", "model_path", "device", "dtype")
_PROGRESS: ContextVar[SpeechProgress | None] = ContextVar("local_speech_progress", default=None)


class LocalSpeechError(VBotError):
    """Raised for local speech target execution errors."""


class LocalSpeechExecutionError(LocalSpeechError):
    """An available engine failed to load or transcribe."""


class LocalTranscriptionEngine(Protocol):
    """One loaded model; all calls are serialized outside the Event Loop."""

    def transcribe(self, samples: Any, options: Mapping[str, Any]) -> SpeechTranscriptionResult: ...

    def close(self) -> None: ...


class LocalSynthesisEngine(Protocol):
    def synthesize(self, text: str, options: Mapping[str, Any]) -> SpeechSynthesisResult: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class SpeechEngineDefinition:
    descriptor: LocalTaskTargetDescriptor
    create: Callable[[Mapping[str, Any]], LocalTranscriptionEngine | LocalSynthesisEngine]
    load_options: tuple[str, ...] | None = None


def builtin_speech_engines() -> tuple[SpeechEngineDefinition, ...]:
    """Return fresh registrations; importing this module needs no ML packages."""
    common = (
        TaskModelOptionField(
            "device",
            "select",
            "Device",
            default="auto",
            required=True,
            options=tuple(
                TaskModelOptionChoice(value, label)
                for value, label in (
                    ("auto", "Automatic"),
                    ("cuda", "CUDA / ROCm GPU"),
                    ("cpu", "CPU"),
                    ("mps", "Apple GPU"),
                )
            ),
        ),
        TaskModelOptionField(
            "dtype",
            "select",
            "Precision",
            default="auto",
            required=True,
            options=tuple(
                TaskModelOptionChoice(value, label)
                for value, label in (
                    ("auto", "Automatic"),
                    ("float32", "Float32"),
                    ("float16", "Float16"),
                    ("bfloat16", "BFloat16"),
                )
            ),
        ),
        TaskModelOptionField(
            "model_path",
            "text",
            "Model directory",
            default="",
            description="Optional directory on the vBot server containing a Transformers model. "
            "Leave empty to download and cache the selected model from Hugging Face.",
        ),
    )
    language = TaskModelOptionField(
        "language",
        "text",
        "Language",
        default="",
        description=(
            "Leave empty for automatic detection, or enter a language code such as de or en."
        ),
    )
    qwen = LocalTaskTargetDescriptor(
        id="qwen3-asr",
        label="Qwen3 ASR",
        task_types=(TASK_SPEECH_TO_TEXT,),
        availability=_dependencies_available,
        metadata={"installation_extra": "local-speech", "license": "Apache-2.0"},
        option_fields=(
            TaskModelOptionField(
                "model",
                "select",
                "Model",
                default="Qwen/Qwen3-ASR-1.7B-hf",
                required=True,
                options=(
                    TaskModelOptionChoice("Qwen/Qwen3-ASR-1.7B-hf", "Qwen3 ASR 1.7B"),
                    TaskModelOptionChoice("Qwen/Qwen3-ASR-0.6B-hf", "Qwen3 ASR 0.6B"),
                ),
            ),
            language,
            TaskModelOptionField(
                "prompt",
                "textarea",
                "Vocabulary and context",
                default="",
                description="Optional names or terminology to help recognize your recording.",
            ),
            *common,
        ),
    )
    parakeet = LocalTaskTargetDescriptor(
        id="parakeet",
        label="Parakeet TDT v3",
        task_types=(TASK_SPEECH_TO_TEXT,),
        availability=_dependencies_available,
        metadata={"installation_extra": "local-speech", "license": "CC-BY-4.0"},
        option_fields=common,
    )
    nemotron = LocalTaskTargetDescriptor(
        id="nemotron3.5-asr",
        label="Nemotron 3.5 ASR Streaming 0.6B",
        task_types=(TASK_SPEECH_TO_TEXT,),
        availability=_dependencies_available,
        metadata={"installation_extra": "local-speech", "license": "OpenMDW-1.1"},
        option_fields=(language, *common),
    )
    return (
        SpeechEngineDefinition(qwen, _QwenEngine, _LOAD_OPTIONS),
        SpeechEngineDefinition(parakeet, _ParakeetEngine, _LOAD_OPTIONS),
        SpeechEngineDefinition(nemotron, _NemotronEngine, _LOAD_OPTIONS),
    )


@dataclass
class _EngineState:
    workers: BoundedWorkerPool
    engine: LocalTranscriptionEngine | LocalSynthesisEngine | None = None
    key: tuple[Any, ...] | None = None
    pending: int = 0

    def unload(self) -> None:
        engine, self.engine = self.engine, None
        self.key = None
        if engine is not None:
            engine.close()


class LocalSpeechExecutor:
    """Own independently cached engines with bounded, cancellation-safe work per engine."""

    def __init__(
        self,
        *,
        engines: Sequence[SpeechEngineDefinition] | None = None,
        engines_dir: Path | None = None,
    ) -> None:
        install_lock = asyncio.Lock()
        self.setup = LocalSpeechSetup(install_lock=install_lock)
        self.tts_setups = {
            name: LocalSpeechSetup(
                engine=name,
                directory=engines_dir / name if engines_dir else None,
                install_lock=install_lock,
            )
            for name in ("qwen3-tts", "chatterbox")
        }
        definitions = (
            tuple(engines)
            if engines is not None
            else (*builtin_speech_engines(), *_tts_definitions(self.tts_setups))
        )
        definitions = tuple(
            replace(
                entry,
                descriptor=replace(
                    entry.descriptor,
                    availability=partial(self._can_execute, entry.descriptor),
                ),
            )
            for entry in definitions
        )
        self._definitions = {entry.descriptor.id: entry for entry in definitions}
        if len(self._definitions) != len(definitions):
            raise ValueError("Duplicate local speech engine id")
        self.targets = LocalTaskTargetRegistry([entry.descriptor for entry in definitions])
        self._states = {
            name: _EngineState(BoundedWorkerPool(name=f"speech-{name}", max_workers=1))
            for name in self._definitions
        }
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    def _can_execute(self, descriptor: LocalTaskTargetDescriptor) -> bool:
        setup = self.tts_setups.get(descriptor.id)
        if setup is None and TASK_SPEECH_TO_TEXT in descriptor.task_types:
            setup = self.setup
        return not (setup and setup.blocks_execution) and descriptor.can_execute()

    def setup_for(self, target: str) -> LocalSpeechSetup:
        if target in ("", "local/qwen3-asr", "local/parakeet", "local/nemotron3.5-asr"):
            return self.setup
        if target.startswith("local/") and target[6:] in self.tts_setups:
            return self.tts_setups[target[6:]]
        raise ValueError("Unknown local speech target")

    async def transcribe(
        self,
        local_id: str,
        audio: bytes,
        *,
        filename: str,
        media_type: str,
        options: dict[str, Any],
        progress: SpeechProgress | None = None,
    ) -> SpeechTranscriptionResult:
        if self._closed:
            raise LocalSpeechError(
                "Local speech recognition is closed. Restart the vBot server before retrying."
            )
        if progress is not None:
            progress.update("queued")
        state = self._states.get(local_id)
        if state is None:
            raise LocalSpeechError(f"Local speech-to-text target is not available: {local_id}")
        state.pending += 1
        try:
            return await state.workers.run(
                self._transcribe_with_progress, local_id, audio, dict(options), progress
            )
        finally:
            state.pending -= 1

    def _transcribe_with_progress(
        self,
        local_id: str,
        audio: bytes,
        options: dict[str, Any],
        progress: SpeechProgress | None,
    ) -> SpeechTranscriptionResult:
        # Scope built-in loader reporting to this worker invocation without adding
        # transport or progress requirements to third-party engine factories.
        token = _PROGRESS.set(progress)
        try:
            if progress is not None:
                progress.update("preparing")
            return self._transcribe(local_id, audio, options)
        finally:
            _PROGRESS.reset(token)

    def _transcribe(
        self, local_id: str, audio: bytes, options: dict[str, Any]
    ) -> SpeechTranscriptionResult:
        if self._closed:
            raise LocalSpeechError(
                "Local speech recognition is closed. Restart the vBot server before retrying."
            )
        definition = self._definitions.get(local_id)
        if definition is None or TASK_SPEECH_TO_TEXT not in definition.descriptor.task_types:
            raise LocalSpeechError(f"Local speech-to-text target is not available: {local_id}")
        if not definition.descriptor.can_execute():
            raise LocalSpeechError(
                "Local speech recognition is not installed. "
                "Open Settings → Tools & Media → Specialized Models, select a local "
                "speech-to-text engine, and choose Install. "
                "Restart the server when setup has finished."
            )
        schema = TaskModelOptionSchema(
            TASK_SPEECH_TO_TEXT,
            definition.descriptor.public_id,
            definition.descriptor.option_fields,
        )
        try:
            validate_task_model_options(schema, options)
        except ValueError as error:
            raise LocalSpeechError(str(error)) from error
        options = {**schema.default_options(), **options}
        load_options = (
            options
            if definition.load_options is None
            else {name: options.get(name) for name in definition.load_options}
        )
        state = self._states[local_id]
        key = (local_id, json.dumps(load_options, sort_keys=True))
        if key != state.key:
            state.unload()
        try:
            segments: list[dict[str, Any]] = []
            languages: set[str] = set()
            saw_samples = False
            for start, samples in _audio_chunks(audio):
                saw_samples = True
                # Exact digital silence needs no model and must not invent text.
                if not samples.any():
                    continue
                if state.engine is None:
                    if (progress := _PROGRESS.get()) is not None:
                        progress.update("loading")
                    _LOGGER.info("Loading local STT model (engine=%s)", local_id)
                    state.engine = definition.create(options)
                    state.key = key
                    _LOGGER.info("Local STT model ready (engine=%s)", local_id)
                if (progress := _PROGRESS.get()) is not None:
                    progress.update("transcribing")
                result = cast(LocalTranscriptionEngine, state.engine).transcribe(samples, options)
                if not isinstance(result.text, str):
                    raise ValueError("Local engine returned a non-text transcription")
                if result.text.strip():
                    segments.append(
                        {
                            "start": start / _SAMPLE_RATE,
                            "end": (start + len(samples)) / _SAMPLE_RATE,
                            "text": result.text.strip(),
                        }
                    )
                    if result.language:
                        languages.add(result.language)
            if not saw_samples:
                raise ValueError("Audio contains no samples")
            return SpeechTranscriptionResult(
                text=" ".join(segment["text"] for segment in segments),
                language=next(iter(languages)) if len(languages) == 1 else None,
                segments=tuple(segments),
            )
        except Exception as error:
            state.unload()
            _LOGGER.warning(
                "Local STT failed (engine=%s, error_type=%s)", local_id, type(error).__name__
            )
            if isinstance(error, LocalSpeechError):
                raise
            raise LocalSpeechExecutionError(
                f"Local speech recognition failed ({type(error).__name__}). "
                "Check the selected device, model directory, available memory, "
                "and model download access."
            ) from error

    def memory_status(self) -> dict[str, Any]:
        """Read all engine identities without importing ML packages or loading models."""
        return {
            "models": [
                {
                    "target": definition.descriptor.public_id,
                    "label": definition.descriptor.label,
                    "loaded": self._states[name].key is not None,
                    "busy": self._states[name].pending > 0 or self._closed,
                }
                for name, definition in self._definitions.items()
            ]
        }

    async def release_memory(self, target: str) -> dict[str, Any]:
        """Release exactly one idle engine; other engines keep serving their requests."""
        local_id = target.removeprefix("local/")
        if not target.startswith("local/") or local_id not in self._states:
            raise ValueError("Unknown local speech target")
        state = self._states[local_id]
        if state.pending or self._closed or state.key is None:
            return {**self.memory_status(), "released": False}
        state.pending += 1
        try:
            await state.workers.run(state.unload)
            _LOGGER.info("Unloaded local speech model (target=%s)", target)
        finally:
            state.pending -= 1
        return {**self.memory_status(), "released": True}

    def close(self) -> None:
        self.setup.close()
        for setup in self.tts_setups.values():
            setup.close()
        self._closed = True
        for state in self._states.values():
            state.workers.shutdown()
            state.unload()

    async def aclose(self) -> None:
        await self.setup.aclose()
        for setup in self.tts_setups.values():
            await setup.aclose()
        if self._close_task is None:
            if self._closed:
                return
            self._closed = True
            self._close_task = asyncio.create_task(self._finish_close())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            # Cleanup may still be waiting for the inference worker. Cancellation
            # must never shut down its executor before the model can be unloaded.
            while not self._close_task.done():
                try:
                    await asyncio.shield(self._close_task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            self._close_task.exception()
            raise

    async def _finish_close(self) -> None:
        try:
            outcomes = await asyncio.gather(
                *(state.workers.run(state.unload) for state in self._states.values()),
                return_exceptions=True,
            )
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    raise outcome
        finally:
            for state in self._states.values():
                state.workers.shutdown(wait=False)

    async def synthesize(
        self,
        local_id: str,
        text: str,
        *,
        options: dict[str, Any],
        progress: SpeechProgress | None = None,
    ) -> SpeechSynthesisResult:
        if progress is not None:
            progress.update("queued")
        state = self._states.get(local_id)
        if self._closed or state is None:
            raise LocalSpeechError(
                "Local speech synthesis is unavailable. Open Settings → Tools & Media → "
                "Specialized Models, select the local text-to-speech engine, and choose Install. "
                "Wait for setup to finish before retrying."
            )
        state.pending += 1
        try:
            return await state.workers.run(
                self._synthesize, local_id, text, dict(options), progress
            )
        finally:
            state.pending -= 1

    def _synthesize(
        self, local_id: str, text: str, options: dict[str, Any], progress: SpeechProgress | None
    ) -> SpeechSynthesisResult:
        definition = self._definitions.get(local_id)
        if (
            self._closed
            or definition is None
            or (TASK_TEXT_TO_SPEECH not in definition.descriptor.task_types)
            or not definition.descriptor.can_execute()
        ):
            raise LocalSpeechError(
                "Local speech synthesis is unavailable. Open Settings → Tools & Media → "
                "Specialized Models, select the local text-to-speech engine, and choose Install. "
                "Wait for setup to finish before retrying."
            )
        if not 0 < len(text) <= 5000:
            raise LocalSpeechError(
                "Local speech synthesis accepts at most 5000 characters per request. "
                "Split the text into shorter requests."
            )
        schema = TaskModelOptionSchema(
            TASK_TEXT_TO_SPEECH,
            definition.descriptor.public_id,
            definition.descriptor.option_fields,
        )
        try:
            validate_task_model_options(schema, options)
        except ValueError as error:
            raise LocalSpeechError(str(error)) from error
        options = {**schema.default_options(), **options}
        load_options = (
            options
            if definition.load_options is None
            else {name: options.get(name) for name in definition.load_options}
        )
        state = self._states[local_id]
        key = (local_id, json.dumps(load_options, sort_keys=True))
        token = _PROGRESS.set(progress)
        try:
            if key != state.key:
                state.unload()
            if state.engine is None:
                if progress is not None:
                    progress.update("loading")
                state.engine = definition.create(options)
                state.key = key
            if progress is not None:
                progress.update("synthesizing")
            result = cast(LocalSynthesisEngine, state.engine).synthesize(text, options)
            if not result.audio:
                raise ValueError("Empty synthesis")
            return result
        except Exception as error:
            state.unload()
            _LOGGER.warning(
                "Local TTS failed (engine=%s, error_type=%s)", local_id, type(error).__name__
            )
            raise LocalSpeechExecutionError(
                "Local speech synthesis failed. Check the selected device, available memory "
                "and model download access, then retry."
            ) from error
        finally:
            _PROGRESS.reset(token)


def _tts_definitions(setups: Mapping[str, LocalSpeechSetup]) -> tuple[SpeechEngineDefinition, ...]:
    def select(name: str, label: str, default: str, choices: Sequence[str]) -> TaskModelOptionField:
        return TaskModelOptionField(
            name,
            "select",
            label,
            default=default,
            required=True,
            options=tuple(TaskModelOptionChoice(x, x) for x in choices),
        )

    common = (select("device", "Device", "auto", ("auto", "cuda", "cpu", "mps")),)
    qwen_options = (
        select(
            "model",
            "Model",
            "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
            ("Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"),
        ),
        select(
            "voice",
            "Voice",
            "Ryan",
            ("Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric", "Ryan", "Aiden", "Ono_Anna", "Sohee"),
        ),
        select(
            "language",
            "Language",
            "Auto",
            (
                "Auto",
                "German",
                "English",
                "Chinese",
                "Japanese",
                "Korean",
                "French",
                "Russian",
                "Portuguese",
                "Spanish",
                "Italian",
            ),
        ),
        TaskModelOptionField(
            "instructions",
            "textarea",
            "Speaking instructions",
            default="",
            description="Optional style instructions for the 1.7B model.",
        ),
        *common,
    )
    chatter_options = (
        select(
            "language",
            "Language",
            "en",
            (
                "ar",
                "da",
                "de",
                "el",
                "en",
                "es",
                "fi",
                "fr",
                "he",
                "hi",
                "it",
                "ja",
                "ko",
                "ms",
                "nl",
                "no",
                "pl",
                "pt",
                "ru",
                "sv",
                "sw",
                "tr",
                "zh",
            ),
        ),
        TaskModelOptionField(
            "exaggeration",
            "number",
            "Expressiveness",
            default=0.5,
            min_value=0,
            max_value=1,
            step=0.05,
        ),
        TaskModelOptionField(
            "cfg_weight", "number", "Guidance", default=0.5, min_value=0, max_value=1, step=0.05
        ),
        *common,
    )
    return tuple(
        SpeechEngineDefinition(
            LocalTaskTargetDescriptor(
                id=name,
                label=label,
                task_types=(TASK_TEXT_TO_SPEECH,),
                availability=setups[name].available,
                metadata={"installation_extra": "local-tts", "license": license_name},
                option_fields=fields,
            ),
            partial(_TtsEngine, setups[name]),
            ("model", "device"),
        )
        for name, label, license_name, fields in (
            ("qwen3-tts", "Qwen3-TTS", "Apache-2.0", qwen_options),
            ("chatterbox", "Chatterbox Multilingual V3", "MIT", chatter_options),
        )
    )


class _TtsEngine:
    """A cached SDK process with fixed entry point and parent-owned output paths."""

    def __init__(self, setup: LocalSpeechSetup, options: Mapping[str, Any]) -> None:
        from core.utils.processes import subprocess_creation_flags

        self._process = subprocess.Popen(
            [
                str(setup.python),
                "-I",
                str(Path(__file__).with_name("speech_worker.py")),
                setup.engine,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            creationflags=subprocess_creation_flags(),
            start_new_session=os.name != "nt",
            env={**os.environ, "PYTHONUTF8": "1", "TOKENIZERS_PARALLELISM": "false"},
        )

    def synthesize(self, text: str, options: Mapping[str, Any]) -> SpeechSynthesisResult:
        process = self._process
        assert process.stdin is not None and process.stdout is not None
        timer = Timer(1800, self.close)
        timer.daemon = True
        timer.start()
        try:
            with tempfile.TemporaryDirectory(prefix="vbot-tts-") as directory:
                output = Path(directory) / "speech.wav"
                process.stdin.write(
                    json.dumps({"text": text, "options": dict(options), "output": str(output)})
                    + "\n"
                )
                process.stdin.flush()
                while line := process.stdout.readline(4096):
                    event = json.loads(line)
                    if event.get("error"):
                        raise RuntimeError(event["error"])
                    if (
                        event.get("phase")
                        in {"checking_model", "downloading", "loading", "synthesizing"}
                        and (progress := _PROGRESS.get()) is not None
                    ):
                        progress.update(event["phase"])
                    if event.get("done"):
                        if not 44 < output.stat().st_size <= 64 * 1024 * 1024:
                            raise ValueError("Invalid output size")
                        return SpeechSynthesisResult(output.read_bytes(), "audio/wav", "wav")
                raise RuntimeError("Speech worker exited")
        finally:
            timer.cancel()

    def close(self) -> None:
        from core.utils.processes import windows_taskkill_tree

        process = self._process
        if process.poll() is None:
            if os.name == "nt":
                if not windows_taskkill_tree(process.pid):
                    with suppress(ProcessLookupError):
                        process.kill()
            else:
                with suppress(ProcessLookupError):
                    cast(Any, os).killpg(process.pid, cast(Any, signal).SIGKILL)
            process.wait()
        if process.stdin:
            process.stdin.close()
        if process.stdout:
            process.stdout.close()


def _audio_chunks(audio: bytes) -> Iterator[tuple[int, Any]]:
    """Decode to bounded mono 16 kHz float32 chunks without dropping samples."""
    import av
    import numpy as np

    pending = np.empty(0, dtype=np.float32)
    offset = 0
    with av.open(io.BytesIO(audio), mode="r") as container:
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=_SAMPLE_RATE)

        def frames() -> Iterator[Any]:
            for frame in container.decode(audio=0):
                yield from resampler.resample(frame)
            yield from resampler.resample(None)

        for frame in frames():
            pending = np.concatenate((pending, frame.to_ndarray().reshape(-1)))
            while len(pending) >= _CHUNK_SAMPLES:
                # Prefer a quiet boundary in the final second. Every sample is
                # retained exactly once, including speech with no useful pause.
                window = 320
                tail = pending[_CHUNK_SAMPLES - _SAMPLE_RATE : _CHUNK_SAMPLES].reshape(-1, window)
                quietest = int(np.argmin(np.mean(tail * tail, axis=1)))
                cut = _CHUNK_SAMPLES - _SAMPLE_RATE + (quietest + 1) * window
                yield offset, pending[:cut]
                offset += cut
                pending = pending[cut:]
        if len(pending):
            yield offset, pending


class _TransformersEngine:
    """Shared model loading; model-specific preparation and decoding stay below."""

    model_class = ""
    default_model = ""

    def __init__(self, options: Mapping[str, Any]) -> None:
        import torch
        import transformers

        self._torch = torch
        self._model: Any = None
        self._processor: Any = None
        device = options.get("device") or "auto"
        if device == "auto":
            device = (
                "cuda"
                if torch.cuda.is_available()
                else ("mps" if torch.backends.mps.is_available() else "cpu")
            )
        if device == "cuda" and not torch.cuda.is_available():
            raise LocalSpeechExecutionError(
                "The selected GPU is unavailable to PyTorch. "
                "Select CPU or install a GPU-enabled PyTorch build."
            )
        if device == "mps" and not torch.backends.mps.is_available():
            raise LocalSpeechExecutionError(
                "The selected Apple GPU is unavailable to PyTorch. "
                "Select CPU or a supported device in the speech-to-text options."
            )
        precision = options.get("dtype") or "auto"
        if precision == "auto":
            precision = "float32"
            if device == "cuda":
                precision = "bfloat16" if torch.cuda.is_bf16_supported() else "float16"
        source = (options.get("model_path") or "").strip()
        if source:
            path = Path(source).expanduser()
            if not path.is_dir():
                raise LocalSpeechExecutionError(
                    "The model directory does not exist on the vBot server. "
                    "Correct Model directory in the speech-to-text options or leave it empty "
                    "to use the selected downloadable model."
                )
            source = str(path)
        else:
            source = options.get("model") or self.default_model
        load_options = {
            "local_files_only": True,
            "trust_remote_code": False,
        }
        try:
            progress = _PROGRESS.get()
            if not options.get("model_path"):
                from tqdm.auto import tqdm  # type: ignore[import-untyped]

                class DownloadProgress(tqdm):
                    def __init__(self, *args: Any, **kwargs: Any) -> None:
                        self._bytes = kwargs.get("unit") == "B"
                        kwargs["file"] = io.StringIO()
                        super().__init__(*args, **kwargs)

                    def update(self, n: float | None = 1) -> bool | None:
                        if progress is not None and self._bytes and (n or 0) > 0:
                            progress.update("downloading")
                        return cast(bool | None, super().update(n))

                if progress is not None:
                    progress.update("checking_model")
                files = [
                    "config.json",
                    "generation_config.json",
                    "model.safetensors",
                    "processor_config.json",
                    "tokenizer.json",
                    "tokenizer_config.json",
                ]
                if self.model_class == "AutoModelForMultimodalLM":
                    files.append("chat_template.jinja")
                source = resolve_snapshot(source, files, DownloadProgress)
            if progress is not None:
                progress.update("loading")
            self._processor = transformers.AutoProcessor.from_pretrained(source, **load_options)
            self._model = (
                getattr(transformers, self.model_class)
                .from_pretrained(source, dtype=getattr(torch, precision), **load_options)
                .to(device)
                .eval()
            )
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self._model = None
        self._processor = None
        gc.collect()
        if self._torch.cuda.is_available():
            self._torch.cuda.empty_cache()
        if self._torch.backends.mps.is_available():
            self._torch.mps.empty_cache()


class _QwenEngine(_TransformersEngine):
    model_class = "AutoModelForMultimodalLM"
    default_model = "Qwen/Qwen3-ASR-1.7B-hf"

    def transcribe(self, samples: Any, options: Mapping[str, Any]) -> SpeechTranscriptionResult:
        language = (options.get("language") or "").strip() or None
        if language == "auto":
            language = None
        inputs = self._processor.apply_transcription_request(
            audio=samples,
            language=language,
            prompt=options.get("prompt") or None,
            audio_kwargs={"sampling_rate": _SAMPLE_RATE},
        ).to(self._model.device, self._model.dtype)
        with self._torch.inference_mode():
            output = self._model.generate(**inputs, max_new_tokens=1024, do_sample=False)
        generated = output[:, inputs["input_ids"].shape[1] :]
        if generated.shape[1] >= 1024:
            raise LocalSpeechExecutionError(
                "The transcription reached the model output limit. Retry with a shorter recording."
            )
        parsed = self._processor.decode(generated, return_format="parsed")[0]
        return SpeechTranscriptionResult(
            text=parsed["transcription"], language=language or parsed.get("language")
        )


class _ParakeetEngine(_TransformersEngine):
    model_class = "AutoModelForTDT"
    default_model = "nvidia/parakeet-tdt-0.6b-v3"

    def transcribe(self, samples: Any, options: Mapping[str, Any]) -> SpeechTranscriptionResult:
        inputs = self._processor(samples, sampling_rate=_SAMPLE_RATE, return_tensors="pt").to(
            self._model.device, self._model.dtype
        )
        with self._torch.inference_mode():
            output = self._model.generate(**inputs, return_dict_in_generate=True)
        texts = self._processor.decode(output.sequences, skip_special_tokens=True)
        return SpeechTranscriptionResult(text=texts[0])


class _NemotronEngine(_TransformersEngine):
    model_class = "AutoModelForRNNT"
    default_model = "nvidia/nemotron-3.5-asr-streaming-0.6b"

    def transcribe(self, samples: Any, options: Mapping[str, Any]) -> SpeechTranscriptionResult:
        language = (options.get("language") or "").strip() or "auto"
        inputs = self._processor(
            samples, sampling_rate=_SAMPLE_RATE, language=language, return_tensors="pt"
        ).to(self._model.device, self._model.dtype)
        # Compute features once, then let native streaming generation retain the
        # encoder/decoder caches across fixed-size mel chunks within this recording.
        lookahead = 6
        self._processor.set_num_lookahead_tokens(lookahead)
        first = self._processor.num_mel_frames_first_audio_chunk
        subsequent = self._processor.num_mel_frames_per_audio_chunk
        frames = int(inputs["attention_mask"].sum().item())
        features = inputs["input_features"][:, :frames]
        chunk_count = 1 + max(0, frames - first + subsequent - 1) // subsequent

        def chunks() -> Iterator[Any]:
            start = 0
            for index in range(chunk_count):
                size = first if index == 0 else subsequent
                chunk = features[:, start : start + size]
                # The final short chunk must be padded, never discarded. Native
                # streaming rejects any chunk that does not have its exact size.
                yield self._torch.nn.functional.pad(chunk, (0, 0, 0, size - chunk.shape[1]))
                start += size

        # RNNT emits blanks as well as text; bound by the maximum emissions per
        # encoder frame so the final audio cannot be truncated by a text-token cap.
        limit = chunk_count * (lookahead + 1) * self._model.max_symbols_per_step + 1
        with self._torch.inference_mode():
            output = self._model.generate(
                input_features=chunks(),
                prompt_ids=inputs["prompt_ids"],
                num_lookahead_tokens=lookahead,
                max_new_tokens=limit,
                return_dict_in_generate=True,
            )
        text = self._processor.decode(output.sequences, skip_special_tokens=True)[0]
        raw = self._processor.decode(output.sequences, skip_special_tokens=False)[0]
        detected = re.findall(r"<([a-z]{2,3}-[A-Z]{2})>", raw)
        return SpeechTranscriptionResult(
            text=text,
            language=language if language != "auto" else (detected[-1] if detected else None),
        )
