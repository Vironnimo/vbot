"""Optional local speech engines, their catalog, and serialized model lifecycle.

An engine definition owns its options, availability, factory and pinned model
together. The executor knows only the synchronous transcription/close protocol;
adding an engine does not change SpeechService, the server, or any accessor.
Every built-in target has its own installation (:class:`LocalSpeechSetup`):
its engine environment plus its pinned model files, which the executor hands
to the engine as ``model_path``. Engines never download.
"""

from __future__ import annotations

import asyncio
import base64
import gc
import importlib
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
from threading import Lock, Timer
from time import monotonic
from typing import Any, Protocol, cast

from core.model_tasks.constants import TASK_SPEECH_TO_TEXT, TASK_TEXT_TO_SPEECH
from core.model_tasks.local_targets import LocalTaskTargetDescriptor, LocalTaskTargetRegistry
from core.model_tasks.model_files import PinnedModel
from core.model_tasks.options import (
    TaskModelOptionChoice,
    TaskModelOptionField,
    TaskModelOptionSchema,
    validate_task_model_options,
)
from core.model_tasks.speech_models import SPEECH_MODELS
from core.model_tasks.speech_setup import LocalSpeechSetup, ServerSpeechStack
from core.model_tasks.speech_types import (
    SpeechProgress,
    SpeechSynthesisResult,
    SpeechTranscriptionResult,
)
from core.utils.errors import VBotError
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool, settle_before_cancelling

_LOGGER = get_logger("speech.local")
_SAMPLE_RATE = 16_000
_CHUNK_SAMPLES = 30 * _SAMPLE_RATE
_LOAD_OPTIONS = ("model_path", "device", "dtype")
# Local STT option asking the Runtime to load the engine after startup and binding changes.
PRELOAD_OPTION = "preload"
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
    """One local speech target.

    *model* is the pinned model its installation fetches; the executor passes
    that model's directory to *create* as the ``model_path`` option unless the
    binding names its own. *environment* is the TTS environment the target
    runs in (``[tool.vbot.local-tts.<environment>]``); empty means the STT stack.
    """

    descriptor: LocalTaskTargetDescriptor
    create: Callable[[Mapping[str, Any]], LocalTranscriptionEngine | LocalSynthesisEngine]
    load_options: tuple[str, ...] | None = None
    model: PinnedModel | None = None
    environment: str = ""


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
            description="Optional directory on the vBot server with a Transformers model of "
            "this engine's architecture, loaded instead of the installed model. "
            "Leave empty to use the installed model.",
        ),
        TaskModelOptionField(
            PRELOAD_OPTION,
            "boolean",
            "Load at server start",
            default=False,
            description="Load the model in the background when the vBot server starts or this "
            "binding changes, so the first transcription does not wait for loading. "
            "The model then stays in memory even while unused.",
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
    qwen_options = (
        language,
        TaskModelOptionField(
            "prompt",
            "textarea",
            "Vocabulary and context",
            default="",
            description="Optional names or terminology to help recognize your recording.",
        ),
        *common,
    )

    def definition(
        local_id: str,
        label: str,
        license_name: str,
        options: tuple[TaskModelOptionField, ...],
        engine: Callable[[Mapping[str, Any]], LocalTranscriptionEngine],
    ) -> SpeechEngineDefinition:
        model = SPEECH_MODELS[local_id]
        return SpeechEngineDefinition(
            LocalTaskTargetDescriptor(
                id=local_id,
                label=label,
                task_types=(TASK_SPEECH_TO_TEXT,),
                metadata={"license": license_name, "download_bytes": model.download_bytes},
                option_fields=options,
            ),
            engine,
            _LOAD_OPTIONS,
            model,
        )

    return (
        definition("qwen3-asr-1.7b", "Qwen3 ASR 1.7B", "Apache-2.0", qwen_options, _QwenEngine),
        definition("qwen3-asr-0.6b", "Qwen3 ASR 0.6B", "Apache-2.0", qwen_options, _QwenEngine),
        definition("parakeet", "Parakeet TDT v3", "CC-BY-4.0", common, _ParakeetEngine),
        definition(
            "nemotron3.5-asr",
            "Nemotron 3.5 ASR Streaming 0.6B",
            "OpenMDW-1.1",
            (language, *common),
            _NemotronEngine,
        ),
    )


@dataclass
class _EngineState:
    workers: BoundedWorkerPool
    engine: LocalTranscriptionEngine | LocalSynthesisEngine | None = None
    key: tuple[Any, ...] | None = None
    pending: int = 0
    preparing: asyncio.Task[None] | None = None
    preparing_key: tuple[Any, ...] | None = None

    def unload(self) -> None:
        engine, self.engine = self.engine, None
        self.key = None
        if engine is not None:
            engine.close()


class _LoadingProcesses:
    """Managed STT workers still loading, so closing can abort a long load.

    A loading worker blocks its executor thread until the child answers; only
    killing the child ends that wait before the load finishes.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._processes: set[subprocess.Popen[str]] = set()
        self._closed = False

    def add(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            if not self._closed:
                self._processes.add(process)
                return
        _kill_speech_process(process)

    def discard(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._processes.discard(process)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            processes, self._processes = self._processes, set()
        for process in processes:
            _kill_speech_process(process)


class LocalSpeechExecutor:
    """Own independently cached engines with bounded, cancellation-safe work per engine."""

    def __init__(
        self,
        *,
        engines: Sequence[SpeechEngineDefinition] | None = None,
        engines_dir: Path | None = None,
        managed_worker: bool = False,
    ) -> None:
        install_lock = asyncio.Lock()
        self._loading = _LoadingProcesses()
        packaged_app = None if managed_worker else _packaged_app_root()
        # A development checkout's STT targets share the server interpreter's packages.
        server_stack = ServerSpeechStack()

        def new_setup(
            local_id: str, environment: str, model: PinnedModel | None
        ) -> LocalSpeechSetup:
            if environment:
                directory = engines_dir / environment if engines_dir else None
            else:
                directory = engines_dir / "stt" if engines_dir and packaged_app else None
            return LocalSpeechSetup(
                name=local_id,
                engine=environment,
                directory=directory,
                install_lock=install_lock,
                model=model,
                models_dir=engines_dir / "models" if engines_dir else None,
                server_stack=server_stack,
            )

        self.setups: dict[str, LocalSpeechSetup] = {}

        def add_setup(
            local_id: str, environment: str, model: PinnedModel | None
        ) -> LocalSpeechSetup:
            setup = self.setups[local_id] = new_setup(local_id, environment, model)
            return setup

        if engines is None:
            stt = tuple(
                replace(
                    entry,
                    descriptor=replace(
                        entry.descriptor,
                        availability=add_setup(entry.descriptor.id, "", entry.model).available,
                    ),
                )
                for entry in builtin_speech_engines()
            )
            if packaged_app is not None:
                stt = tuple(
                    replace(
                        entry,
                        create=partial(
                            _ManagedSttEngine,
                            self.setups[entry.descriptor.id],
                            packaged_app,
                            entry.descriptor.id,
                            self._loading,
                        ),
                    )
                    for entry in stt
                )
            definitions: tuple[SpeechEngineDefinition, ...] = (*stt, *_tts_definitions(add_setup))
        else:
            definitions = tuple(engines)
            for entry in definitions:
                add_setup(entry.descriptor.id, entry.environment, entry.model)
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
        return not self.setups[descriptor.id].blocks_execution and descriptor.can_execute()

    def setup_for(self, target: str) -> LocalSpeechSetup:
        local_id = target.removeprefix("local/")
        if not target.startswith("local/") or local_id not in self.setups:
            raise ValueError("Unknown local speech target")
        return self.setups[local_id]

    def activities(self) -> list[dict[str, Any]]:
        """Each local speech installation that has something to show (``LocalSetup.activity``)."""
        result = []
        for local_id, setup in self.setups.items():
            activity = setup.activity()
            if activity is not None:
                descriptor = self._definitions[local_id].descriptor
                result.append(
                    {
                        "target": descriptor.public_id,
                        "label": descriptor.label,
                        "task_type": descriptor.task_types[0],
                        **activity,
                    }
                )
        return result

    def _engine_options(self, local_id: str, options: dict[str, Any]) -> dict[str, Any]:
        """The options an engine loads with: the installed model unless the binding names one."""
        directory = self.setups[local_id].model_directory
        if directory is None or options.get("model_path"):
            return options
        return {**options, "model_path": str(directory)}

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

    def prepare(self, local_id: str, options: Mapping[str, Any]) -> str:
        """Start loading one STT engine in the background, before its first request.

        Returns ``loaded`` when the engine already runs with these load options,
        ``loading`` when a load started or is still running, and ``unavailable``
        when the engine cannot run here. A transcription that arrives meanwhile
        waits for the load. Failures are logged, never raised. Event Loop only.
        """
        state = self._states.get(local_id)
        try:
            if state is None:
                raise LocalSpeechError(f"Local speech-to-text target is not available: {local_id}")
            _definition, merged, key = self._stt_request(local_id, dict(options))
        except LocalSpeechError:
            return "unavailable"
        if state.engine is not None and state.key == key:
            return "loaded"
        if (
            state.preparing is not None
            and not state.preparing.done()
            and state.preparing_key == key
        ):
            return "loading"
        state.preparing_key = key
        state.preparing = asyncio.get_running_loop().create_task(
            self._prepare_in_worker(state, local_id, merged)
        )
        return "loading"

    async def _prepare_in_worker(
        self, state: _EngineState, local_id: str, options: dict[str, Any]
    ) -> None:
        state.pending += 1
        try:
            await state.workers.run(self._prepare, local_id, options)
        except LocalSpeechError:
            pass  # Logged by the worker; the next transcription reports it to its caller.
        except Exception:
            _LOGGER.error("Local STT preload failed (engine=%s)", local_id, exc_info=True)
        finally:
            state.pending -= 1

    def _prepare(self, local_id: str, options: dict[str, Any]) -> None:
        definition, options, key = self._stt_request(local_id, options)
        state = self._states[local_id]
        if key != state.key:
            state.unload()
        try:
            self._loaded_engine(state, definition, options, key)
        except Exception as error:
            failure = self._failed(state, local_id, error)
            if failure is error:
                raise
            raise failure from error

    def _stt_request(
        self, local_id: str, options: dict[str, Any]
    ) -> tuple[SpeechEngineDefinition, dict[str, Any], tuple[Any, ...]]:
        """Validate one STT request; return its definition, options and load identity."""
        if self._closed:
            raise LocalSpeechError(
                "Local speech recognition is closed. Restart the vBot server before retrying."
            )
        definition = self._definitions.get(local_id)
        if definition is None or TASK_SPEECH_TO_TEXT not in definition.descriptor.task_types:
            raise LocalSpeechError(f"Local speech-to-text target is not available: {local_id}")
        if not definition.descriptor.can_execute():
            raise LocalSpeechError(
                f"The local speech-to-text model {definition.descriptor.label} is not "
                "installed. Open Settings → Voice → Speech models, select it under Speech "
                "to text, and choose Install. Restart the server if Settings asks for it."
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
        return definition, options, (local_id, json.dumps(load_options, sort_keys=True))

    def _loaded_engine(
        self,
        state: _EngineState,
        definition: SpeechEngineDefinition,
        options: dict[str, Any],
        key: tuple[Any, ...],
    ) -> LocalTranscriptionEngine:
        if state.engine is None:
            local_id = definition.descriptor.id
            if (progress := _PROGRESS.get()) is not None:
                progress.update("loading")
            _LOGGER.debug("Loading local STT model (engine=%s)", local_id)
            started = monotonic()
            state.engine = definition.create(self._engine_options(local_id, options))
            state.key = key
            _LOGGER.info(
                "Loaded local STT model (engine=%s seconds=%.1f)", local_id, monotonic() - started
            )
        return cast(LocalTranscriptionEngine, state.engine)

    def _failed(self, state: _EngineState, local_id: str, error: Exception) -> LocalSpeechError:
        """Unload after a failure and return the error the caller should see."""
        state.unload()
        _LOGGER.warning(
            "Local STT failed (engine=%s error_type=%s)", local_id, type(error).__name__
        )
        if isinstance(error, LocalSpeechError):
            return error
        return LocalSpeechExecutionError(
            f"Local speech recognition failed ({type(error).__name__}). "
            "Check the selected device, model directory and available memory."
        )

    def _transcribe(
        self, local_id: str, audio: bytes, options: dict[str, Any]
    ) -> SpeechTranscriptionResult:
        definition, options, key = self._stt_request(local_id, options)
        state = self._states[local_id]
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
                engine = self._loaded_engine(state, definition, options, key)
                if (progress := _PROGRESS.get()) is not None:
                    progress.update("transcribing")
                result = engine.transcribe(samples, options)
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
            failure = self._failed(state, local_id, error)
            if failure is error:
                raise
            raise failure from error

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
        for setup in self.setups.values():
            setup.close()
        self._closed = True
        self._loading.close()
        for state in self._states.values():
            if state.preparing is not None:
                state.preparing.cancel()
            state.workers.shutdown()
            state.unload()

    async def aclose(self) -> None:
        for setup in self.setups.values():
            await setup.aclose()
        if self._close_task is None:
            if self._closed:
                return
            self._closed = True
            # An unstarted preload never starts; closing aborts a started managed load.
            for state in self._states.values():
                if state.preparing is not None:
                    state.preparing.cancel()
            self._close_task = asyncio.create_task(self._finish_close())
        # Cleanup may still be waiting for the inference worker. Cancellation
        # must never shut down its executor before the model can be unloaded.
        await settle_before_cancelling(self._close_task, on_late_failure=_log_close_failure)

    async def _finish_close(self) -> None:
        # Killing a process tree blocks, so it must not run on the Event Loop.
        await asyncio.to_thread(self._loading.close)
        await asyncio.gather(
            *(state.preparing for state in self._states.values() if state.preparing is not None),
            return_exceptions=True,
        )
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
                "Local speech synthesis is unavailable. Open Settings → Voice → Speech models, "
                "select the local text-to-speech engine under Text to speech, and choose Install. "
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
                "Local speech synthesis is unavailable. Open Settings → Voice → Speech models, "
                "select the local text-to-speech engine under Text to speech, and choose Install. "
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
                state.engine = definition.create(self._engine_options(local_id, options))
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
                "Local speech synthesis failed. Check the selected device and available "
                "memory, then retry."
            ) from error
        finally:
            _PROGRESS.reset(token)


def _tts_definitions(
    setup_for: Callable[[str, str, PinnedModel], LocalSpeechSetup],
) -> tuple[SpeechEngineDefinition, ...]:
    """The local TTS targets; *setup_for(id, environment, model)* supplies each installation."""

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
        *common,
    )
    instructions = TaskModelOptionField(
        "instructions",
        "textarea",
        "Speaking instructions",
        default="",
        description="Optional style instructions, such as a calm or cheerful voice.",
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
    definitions = []
    for local_id, environment, label, license_name, fields in (
        (
            "qwen3-tts-1.7b",
            "qwen3-tts",
            "Qwen3-TTS 1.7B",
            "Apache-2.0",
            (*qwen_options, instructions),
        ),
        ("qwen3-tts-0.6b", "qwen3-tts", "Qwen3-TTS 0.6B", "Apache-2.0", qwen_options),
        ("chatterbox", "chatterbox", "Chatterbox Multilingual V3", "MIT", chatter_options),
    ):
        model = SPEECH_MODELS[local_id]
        setup = setup_for(local_id, environment, model)
        definitions.append(
            SpeechEngineDefinition(
                LocalTaskTargetDescriptor(
                    id=local_id,
                    label=label,
                    task_types=(TASK_TEXT_TO_SPEECH,),
                    availability=setup.available,
                    metadata={"license": license_name, "download_bytes": model.download_bytes},
                    option_fields=fields,
                ),
                partial(_TtsEngine, setup),
                ("device",),
                model,
                environment,
            )
        )
    return tuple(definitions)


class _TtsEngine:
    """A cached SDK process with fixed entry point and parent-owned output paths."""

    def __init__(self, setup: LocalSpeechSetup, options: Mapping[str, Any]) -> None:
        from core.utils.processes import subprocess_creation_flags

        # The worker loads this model directory with the first request.
        self._model_path = options["model_path"]
        self._process = subprocess.Popen(
            [
                str(setup.python),
                "-I",
                "-B",
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
                    json.dumps(
                        {
                            "text": text,
                            "options": {**options, "model_path": self._model_path},
                            "output": str(output),
                        }
                    )
                    + "\n"
                )
                process.stdin.flush()
                while line := process.stdout.readline(4096):
                    event = json.loads(line)
                    if event.get("error"):
                        raise RuntimeError(event["error"])
                    if (
                        event.get("phase") in {"loading", "synthesizing"}
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


def _packaged_app_root() -> Path | None:
    """Locate an actual packaged release from this module, without ambient flags."""
    source = Path(__file__).resolve()
    for parent in source.parents:
        app = parent / "app"
        if (parent / "release.json").is_file() and (
            app / "core" / "model_tasks" / "speech_local.py"
        ).is_file():
            return app
    return None


class _ManagedSttEngine:
    """Keep the optional ML stack in a managed child interpreter.

    Construction returns once the child reports the model loaded, so the
    executor's load boundary (logs, progress, preloading) covers the real load.
    """

    def __init__(
        self,
        setup: LocalSpeechSetup,
        app_root: Path,
        engine: str,
        loading: _LoadingProcesses,
        options: Mapping[str, Any],
    ) -> None:
        from core.utils.processes import subprocess_creation_flags

        self._process = subprocess.Popen(
            [
                str(setup.python),
                "-I",
                "-B",
                str(Path(__file__).with_name("speech_worker.py")),
                "--stt",
                engine,
                str(app_root),
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
        loading.add(self._process)
        try:
            self._exchange({"load": True, "options": dict(options)}, "loaded")
        except BaseException:
            self.close()
            raise
        finally:
            loading.discard(self._process)

    def transcribe(self, samples: Any, options: Mapping[str, Any]) -> SpeechTranscriptionResult:
        payload = self._exchange(
            {
                "samples": base64.b64encode(samples.astype("<f4").tobytes()).decode("ascii"),
                "options": dict(options),
            },
            "result",
        )
        return SpeechTranscriptionResult(
            text=payload["text"],
            language=payload.get("language"),
            segments=tuple(payload.get("segments", ())),
            usage=payload.get("usage"),
        )

    def _exchange(self, request: Mapping[str, Any], answer: str) -> Any:
        """Send one request line and return the child's ``answer`` field."""
        process = self._process
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        while line := process.stdout.readline(4096):
            event = json.loads(line)
            if event.get("error"):
                raise RuntimeError(event["error"])
            if event.get("phase") and (progress := _PROGRESS.get()) is not None:
                progress.update(event["phase"])
            if (payload := event.get(answer)) is not None:
                return payload
        raise RuntimeError("Speech worker exited")

    def close(self) -> None:
        _close_speech_process(self._process)


def _kill_speech_process(process: subprocess.Popen[str]) -> None:
    """Kill a speech child's whole process tree; its pipes stay with their owner."""
    from core.utils.processes import windows_taskkill_tree

    if process.poll() is None:
        if os.name == "nt":
            if not windows_taskkill_tree(process.pid):
                with suppress(ProcessLookupError):
                    process.kill()
        else:
            with suppress(ProcessLookupError):
                cast(Any, os).killpg(process.pid, cast(Any, signal).SIGKILL)
        process.wait()


def _close_speech_process(process: subprocess.Popen[str]) -> None:
    _kill_speech_process(process)
    if process.stdin:
        process.stdin.close()
    if process.stdout:
        process.stdout.close()


def _log_close_failure(error: BaseException) -> None:
    """Report a shutdown failure that its cancelled caller no longer receives."""
    _LOGGER.error("Local speech shutdown failed after its caller was cancelled", exc_info=error)


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

    def __init__(self, options: Mapping[str, Any]) -> None:
        torch = importlib.import_module("torch")
        transformers = importlib.import_module("transformers")

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
        # The executor supplies the installed model's directory unless the binding names one.
        path = Path((options.get("model_path") or "").strip()).expanduser()
        if not str(path) or str(path) == "." or not path.is_dir():
            raise LocalSpeechExecutionError(
                "The model directory does not exist on the vBot server. "
                "Correct Model directory in the speech-to-text options or leave it empty "
                "to use the installed model."
            )
        source = str(path)
        load_options = {
            "local_files_only": True,
            "trust_remote_code": False,
        }
        try:
            if (progress := _PROGRESS.get()) is not None:
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
