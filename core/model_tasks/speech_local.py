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
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata, util
from pathlib import Path
from typing import Any, Protocol

from core.model_tasks.constants import TASK_SPEECH_TO_TEXT
from core.model_tasks.local_targets import LocalTaskTargetDescriptor, LocalTaskTargetRegistry
from core.model_tasks.options import (
    TaskModelOptionChoice,
    TaskModelOptionField,
    TaskModelOptionSchema,
    validate_task_model_options,
)
from core.model_tasks.speech_types import SpeechSynthesisResult, SpeechTranscriptionResult
from core.utils.errors import VBotError
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

_LOGGER = get_logger("speech.local")
_SAMPLE_RATE = 16_000
_CHUNK_SAMPLES = 30 * _SAMPLE_RATE
_LOAD_OPTIONS = ("model", "model_path", "device", "dtype", "offline")


class LocalSpeechError(VBotError):
    """Raised for local speech target execution errors."""


class LocalSpeechExecutionError(LocalSpeechError):
    """An available engine failed to load or transcribe."""


class LocalTranscriptionEngine(Protocol):
    """One loaded model; all calls are serialized outside the Event Loop."""

    def transcribe(self, samples: Any, options: Mapping[str, Any]) -> SpeechTranscriptionResult: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class SpeechEngineDefinition:
    descriptor: LocalTaskTargetDescriptor
    create: Callable[[Mapping[str, Any]], LocalTranscriptionEngine]
    load_options: tuple[str, ...] | None = None


def _dependencies_available() -> bool:
    # Inspect metadata only: startup/status must not import torch or load models.
    try:
        version = tuple(int(part) for part in metadata.version("transformers").split(".")[:3])
        return (5, 16, 1) <= version < (6,) and all(
            util.find_spec(name) is not None for name in ("torch", "numpy", "av", "librosa")
        )
    except (metadata.PackageNotFoundError, ImportError, ValueError):
        return False


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
        TaskModelOptionField(
            "offline",
            "boolean",
            "Offline only",
            default=False,
            description="Load only already downloaded model files; never contact Hugging Face.",
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
            TaskModelOptionField(
                "language",
                "text",
                "Language",
                default="",
                description=(
                    "Leave empty for automatic detection, or enter a language code "
                    "such as de or en."
                ),
            ),
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
    return (
        SpeechEngineDefinition(qwen, _QwenEngine, _LOAD_OPTIONS),
        SpeechEngineDefinition(parakeet, _ParakeetEngine, _LOAD_OPTIONS),
    )


class LocalSpeechExecutor:
    """Own one cached engine per Runtime, with bounded and cancellation-safe work."""

    def __init__(self, *, engines: Sequence[SpeechEngineDefinition] | None = None) -> None:
        definitions = tuple(engines) if engines is not None else builtin_speech_engines()
        self._definitions = {entry.descriptor.id: entry for entry in definitions}
        if len(self._definitions) != len(definitions):
            raise ValueError("Duplicate local speech engine id")
        self.targets = LocalTaskTargetRegistry([entry.descriptor for entry in definitions])
        self._workers = BoundedWorkerPool(name="local-speech", max_workers=1)
        self._engine: LocalTranscriptionEngine | None = None
        self._engine_key: tuple[Any, ...] | None = None
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def transcribe(
        self,
        local_id: str,
        audio: bytes,
        *,
        filename: str,
        media_type: str,
        options: dict[str, Any],
    ) -> SpeechTranscriptionResult:
        if self._closed:
            raise LocalSpeechError(
                "Local speech recognition is closed. Restart the vBot server before retrying."
            )
        return await self._workers.run(self._transcribe, local_id, audio, dict(options))

    def _transcribe(
        self, local_id: str, audio: bytes, options: dict[str, Any]
    ) -> SpeechTranscriptionResult:
        if self._closed:
            raise LocalSpeechError(
                "Local speech recognition is closed. Restart the vBot server before retrying."
            )
        definition = self._definitions.get(local_id)
        if definition is None:
            raise LocalSpeechError(f"Local speech-to-text target is not available: {local_id}")
        if not definition.descriptor.can_execute():
            raise LocalSpeechError(
                "Local speech recognition requires the local-speech extra on the vBot server. "
                "In its installation directory and Python environment, run "
                "python -m pip install -e '.[local-speech]' and restart vBot."
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
        key = (local_id, json.dumps(load_options, sort_keys=True))
        if key != self._engine_key:
            self._unload()
        try:
            segments: list[dict[str, Any]] = []
            languages: set[str] = set()
            saw_samples = False
            for start, samples in _audio_chunks(audio):
                saw_samples = True
                # Exact digital silence needs no model and must not invent text.
                if not samples.any():
                    continue
                if self._engine is None:
                    _LOGGER.info("Loading local STT model (engine=%s)", local_id)
                    self._engine = definition.create(options)
                    self._engine_key = key
                    _LOGGER.info("Local STT model ready (engine=%s)", local_id)
                result = self._engine.transcribe(samples, options)
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
            self._unload()
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

    def _unload(self) -> None:
        engine, self._engine = self._engine, None
        self._engine_key = None
        if engine is not None:
            engine.close()

    async def unload(self) -> None:
        """Release the last local model before routing speech to a Provider."""
        if not self._closed:
            await self._workers.run(self._unload)

    def close(self) -> None:
        self._closed = True
        self._workers.shutdown()
        self._unload()

    async def aclose(self) -> None:
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
            await self._workers.run(self._unload)
        finally:
            self._workers.shutdown(wait=False)

    async def synthesize(
        self,
        local_id: str,
        text: str,
        *,
        options: dict[str, Any],
    ) -> SpeechSynthesisResult:
        raise LocalSpeechError(f"Local text-to-speech target is not available: {local_id}")


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
            "local_files_only": bool(options.get("offline") or options.get("model_path")),
            "trust_remote_code": False,
        }
        try:
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
