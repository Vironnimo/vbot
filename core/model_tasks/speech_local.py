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
import logging
import os
import re
import signal
import subprocess
import tempfile
import wave
from collections.abc import Callable, Container, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext, suppress
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
from core.model_tasks.speech_audio import ThreadAudioCallback
from core.model_tasks.speech_models import SPEECH_MODELS
from core.model_tasks.speech_setup import LocalSpeechSetup, ServerSpeechStack
from core.model_tasks.speech_types import (
    SpeechAudioCallback,
    SpeechAudioChunk,
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
# Local speech option asking the Runtime to load the engine after startup and binding changes.
PRELOAD_OPTION = "preload"
# English names of the language codes the local engines accept.
_LANGUAGE_NAMES = {
    "ar": "Arabic",
    "bg": "Bulgarian",
    "cs": "Czech",
    "da": "Danish",
    "de": "German",
    "el": "Greek",
    "en": "English",
    "es": "Spanish",
    "et": "Estonian",
    "fa": "Persian",
    "fi": "Finnish",
    "fil": "Filipino",
    "fr": "French",
    "he": "Hebrew",
    "hi": "Hindi",
    "hr": "Croatian",
    "hu": "Hungarian",
    "id": "Indonesian",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "lt": "Lithuanian",
    "lv": "Latvian",
    "mk": "Macedonian",
    "ms": "Malay",
    "nb": "Norwegian Bokmål",
    "nl": "Dutch",
    "nn": "Norwegian Nynorsk",
    "no": "Norwegian",
    "pl": "Polish",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "sk": "Slovak",
    "sl": "Slovenian",
    "sv": "Swedish",
    "sw": "Swahili",
    "th": "Thai",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "vi": "Vietnamese",
    "yue": "Cantonese",
    "zh": "Chinese",
}
# A speech worker child that has not answered within these seconds is ended.
_LOAD_DEADLINE_S = 900.0
_INFERENCE_DEADLINE_S = 600.0  # one STT chunk of at most 30 seconds of audio
# A local TTS worker that has not answered within these seconds is ended.
_SYNTHESIS_DEADLINE_S = 1800.0  # at most 5,000 characters
# A cancelled request's running engine call may still finish within these seconds.
_CANCEL_GRACE_S = 30.0
_TTS_UNAVAILABLE = (
    "Local speech synthesis is unavailable. Open Settings → Voice → Speech models, "
    "select the local text-to-speech engine under Text to speech, and choose Install. "
    "Wait for setup to finish before retrying."
)
_PROGRESS: ContextVar[SpeechProgress | None] = ContextVar("local_speech_progress", default=None)
_AUDIO: ContextVar[Callable[[SpeechAudioChunk], None] | None] = ContextVar(
    "local_speech_audio", default=None
)


class LocalSpeechError(VBotError):
    """Raised for local speech target execution errors."""


class LocalSpeechExecutionError(LocalSpeechError):
    """An available engine failed to load or transcribe."""


class LocalTranscriptionEngine(Protocol):
    """One loaded model; all calls are serialized outside the Event Loop.

    An engine that can stop a running call from another thread may also offer
    ``abort()``; the call then fails and the engine is unloaded. A managed
    worker does; an in-process engine always finishes its call.
    """

    def transcribe(self, samples: Any, options: Mapping[str, Any]) -> SpeechTranscriptionResult: ...

    def close(self) -> None: ...


class LocalSynthesisEngine(Protocol):
    """One loaded TTS model; it may offer ``abort()`` like a transcription engine."""

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


def _preload_field(first_use: str) -> TaskModelOptionField:
    return TaskModelOptionField(
        PRELOAD_OPTION,
        "boolean",
        "Load at server start",
        default=False,
        description="Load the model in the background when the vBot server starts or this "
        f"binding changes, so the first {first_use} does not wait for loading. "
        "The model then stays in memory even while unused.",
    )


def _language_choice(value: str, region: str = "") -> TaskModelOptionChoice:
    """A language choice labelled with its English name; *region* tells variants apart."""
    name = _LANGUAGE_NAMES[value.split("-")[0]]
    return TaskModelOptionChoice(value, f"{name} ({region})" if region else name)


def _stt_language_field(choices: Sequence[TaskModelOptionChoice]) -> TaskModelOptionField:
    return TaskModelOptionField(
        "language",
        "select",
        "Language",
        default="",
        description="The language of your recordings. Automatic detects it in each recording.",
        options=(
            TaskModelOptionChoice("", "Automatic"),
            *sorted(choices, key=lambda choice: choice.label),
        ),
    )


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
            server_path="directory",
        ),
        _preload_field("transcription"),
    )
    # The codes Qwen3-ASR names in its forced-language prompt.
    qwen_language = _stt_language_field(
        tuple(
            _language_choice(code)
            for code in (
                *("ar", "cs", "da", "de", "el", "en", "es", "fa", "fi", "fil", "fr", "hi"),
                *("hu", "id", "it", "ja", "ko", "mk", "ms", "nl", "pl", "pt", "ro", "ru"),
                *("sv", "th", "tr", "vi", "yue", "zh"),
            )
        )
    )
    # Nemotron's prompt dictionary keys for the locales it emits. A bare code
    # stands for the locale the dictionary maps it to (en: en-US, es: es-US,
    # fr: fr-FR, pt: pt-PT); the other variant is named by its locale.
    nemotron_language = _stt_language_field(
        (
            *(
                _language_choice(code)
                for code in (
                    *("ar", "bg", "cs", "da", "de", "el", "et", "fi", "hi", "hr", "hu", "it"),
                    *("ko", "lt", "lv", "nb", "nl", "nn", "pl", "ro", "ru", "sk", "sl", "sv"),
                    *("tr", "uk", "he-IL", "ja-JP", "th-TH", "vi-VN", "zh-CN"),
                )
            ),
            _language_choice("en", "US"),
            _language_choice("en-GB", "UK"),
            _language_choice("es", "US"),
            _language_choice("es-ES", "Spain"),
            _language_choice("fr", "France"),
            _language_choice("fr-CA", "Canada"),
            _language_choice("pt", "Portugal"),
            _language_choice("pt-BR", "Brazil"),
        )
    )
    qwen_options = (
        qwen_language,
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
            (nemotron_language, *common),
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


class _WaitingWorkers:
    """Speech worker children the parent is waiting on, so closing can end them.

    While a child loads, transcribes or synthesizes, its executor thread blocks
    until the child answers; only ending the child releases it before that.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._workers: set[_WorkerProcess] = set()
        self._closed = False

    def add(self, worker: _WorkerProcess) -> None:
        with self._lock:
            if not self._closed:
                self._workers.add(worker)
                return
        worker.end("closed")

    def discard(self, worker: _WorkerProcess) -> None:
        with self._lock:
            self._workers.discard(worker)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            workers, self._workers = self._workers, set()
        for worker in workers:
            worker.end("closed")


class _RequestCancelledError(LocalSpeechError):
    """The caller cancelled the request and receives no result."""


class _Cancellation:
    """One request's cancellation, shared by its caller and worker thread.

    The worker starts no further engine call. A call already running gets
    ``_CANCEL_GRACE_S`` seconds to finish, so a responsive engine stays loaded;
    then an engine that can ``abort`` is ended.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._requested = False
        self._abort: Callable[[], None] | None = None
        self._timer: Timer | None = None
        self.aborted = False

    def request(self) -> None:
        """Event Loop: the caller was cancelled."""
        with self._lock:
            self._requested = True
            self._arm()

    def check(self) -> None:
        if self._requested:
            raise _RequestCancelledError("Local speech request was cancelled.")

    @contextmanager
    def running(self, engine: object) -> Iterator[None]:
        """Worker thread: one engine call, which a cancellation may abort after its grace."""
        with self._lock:
            self.check()
            self._abort = getattr(engine, "abort", None)
            self._arm()
        try:
            yield
        finally:
            with self._lock:
                self._abort = None
                if self._timer is not None:
                    self._timer.cancel()
                    self._timer = None
        if self.aborted:
            # The call answered while the grace ran out; the engine is ended anyway.
            raise RuntimeError("Speech worker was ended")

    def _arm(self) -> None:
        if self._requested and self._abort is not None and self._timer is None:
            self._timer = Timer(_CANCEL_GRACE_S, self._expire)
            self._timer.daemon = True
            self._timer.start()

    def _expire(self) -> None:
        # Aborting under the lock lets ``running`` see whether its engine was ended.
        with self._lock:
            if self._abort is not None:
                self.aborted = True
                self._abort()


_CANCELLATION: ContextVar[_Cancellation | None] = ContextVar(
    "local_speech_cancellation", default=None
)


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
        self._waiting = _WaitingWorkers()
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
                            self._waiting,
                        ),
                    )
                    for entry in stt
                )
            definitions: tuple[SpeechEngineDefinition, ...] = (
                *stt,
                *_tts_definitions(add_setup, self._waiting),
            )
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
        self._audio_callbacks: set[ThreadAudioCallback] = set()

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
        return await self._run(
            state, self._transcribe_with_progress, local_id, audio, dict(options), progress
        )

    async def _run[Result](
        self,
        state: _EngineState,
        function: Callable[..., Result | None],
        *arguments: Any,
        on_cancel: Callable[[], None] | None = None,
    ) -> Result:
        """Run one request on the engine's worker, passing it a ``_Cancellation``.

        A cancelled request that is still queued never starts. A running one starts
        no further engine call and ends an unresponsive worker after its grace; the
        engine stays busy until then, so the caller waits for it.
        """
        state.pending += 1
        cancellation = _Cancellation()
        request = asyncio.ensure_future(state.workers.run(function, *arguments, cancellation))
        try:
            await asyncio.wait((request,))
        except asyncio.CancelledError:
            cancellation.request()
            if on_cancel is not None:
                on_cancel()
            request.cancel()
            await settle_before_cancelling(asyncio.wait((request,)))
            raise
        finally:
            state.pending -= 1
        result = request.result()
        assert result is not None  # Only a cancelled request ends without a result.
        return result

    def _ended_by_cancellation(self, state: _EngineState, local_id: str) -> None:
        """Unload an engine a cancellation ended because its call did not finish."""
        state.unload()
        _LOGGER.warning(
            "Ended local speech worker that kept a cancelled request "
            "(engine=%s grace_seconds=%.0f)",
            local_id,
            _CANCEL_GRACE_S,
        )

    def _transcribe_with_progress(
        self,
        local_id: str,
        audio: bytes,
        options: dict[str, Any],
        progress: SpeechProgress | None,
        cancellation: _Cancellation,
    ) -> SpeechTranscriptionResult | None:
        """Transcribe on the engine's worker; ``None`` once the caller cancelled."""
        # Scope built-in loader reporting to this worker invocation without adding
        # transport or progress requirements to third-party engine factories.
        with _PROGRESS.set(progress), _CANCELLATION.set(cancellation):
            if progress is not None:
                progress.update("preparing")
            try:
                return self._transcribe(local_id, audio, options, cancellation)
            except _RequestCancelledError:
                return None  # Not a failure: nobody waits for an error.

    def prepare(self, local_id: str, options: Mapping[str, Any]) -> str:
        """Start loading one engine in the background, before its first request.

        Returns ``loaded`` when the engine already runs with these load options,
        ``loading`` when a load started or is still running, and ``unavailable``
        when the engine cannot run here. A request that arrives meanwhile waits
        for the load. Failures are logged, never raised. Event Loop only.
        """
        state = self._states.get(local_id)
        definition = self._definitions.get(local_id)
        if state is None or definition is None:
            return "unavailable"
        try:
            _definition, merged, key = self._request(
                local_id, dict(options), definition.descriptor.task_types[0]
            )
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
            self._prepare_in_worker(state, local_id, merged),
            name=f"local-speech-prepare:{local_id}",
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
            _LOGGER.error("Local speech preload failed (engine=%s)", local_id, exc_info=True)
        finally:
            state.pending -= 1

    def _prepare(self, local_id: str, options: dict[str, Any]) -> None:
        definition = self._definitions[local_id]
        definition, options, key = self._request(
            local_id, options, definition.descriptor.task_types[0]
        )
        state = self._states[local_id]
        if key != state.key:
            state.unload()
        try:
            self._loaded_engine(state, definition, options, key)
        except Exception as error:
            failure = self._failed(state, definition, error)
            if failure is error:
                raise
            raise failure from error

    def _request(
        self, local_id: str, options: dict[str, Any], task_type: str
    ) -> tuple[SpeechEngineDefinition, dict[str, Any], tuple[Any, ...]]:
        """Validate one *task_type* request; return its definition, options and load identity."""
        definition = self._definitions.get(local_id)
        if task_type == TASK_TEXT_TO_SPEECH:
            if (
                self._closed
                or definition is None
                or task_type not in definition.descriptor.task_types
                or not definition.descriptor.can_execute()
            ):
                raise LocalSpeechError(_TTS_UNAVAILABLE)
        else:
            if self._closed:
                raise LocalSpeechError(
                    "Local speech recognition is closed. Restart the vBot server before retrying."
                )
            if definition is None or task_type not in definition.descriptor.task_types:
                raise LocalSpeechError(f"Local speech-to-text target is not available: {local_id}")
            if not definition.descriptor.can_execute():
                raise LocalSpeechError(
                    f"The local speech-to-text model {definition.descriptor.label} is not "
                    "installed. Open Settings → Voice → Speech models, select it under Speech "
                    "to text, and choose Install. Restart the server if Settings asks for it."
                )
        schema = TaskModelOptionSchema(
            task_type,
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
    ) -> LocalTranscriptionEngine | LocalSynthesisEngine:
        if state.engine is None:
            local_id = definition.descriptor.id
            task = _task_name(definition)
            if (progress := _PROGRESS.get()) is not None:
                progress.update("loading")
            _LOGGER.debug("Loading local %s model (engine=%s)", task, local_id)
            started = monotonic()
            state.engine = definition.create(self._engine_options(local_id, options))
            state.key = key
            _LOGGER.info(
                "Loaded local %s model (engine=%s seconds=%.1f)",
                task,
                local_id,
                monotonic() - started,
            )
        return state.engine

    def _failed(
        self, state: _EngineState, definition: SpeechEngineDefinition, error: Exception
    ) -> LocalSpeechError:
        """Unload after a failure and return the error the caller should see."""
        state.unload()
        task = _task_name(definition)
        _LOGGER.log(
            # Closing ends running workers; that is no failure of the engine.
            logging.DEBUG if self._closed else logging.WARNING,
            "Local %s failed (engine=%s error_type=%s)",
            task,
            definition.descriptor.id,
            type(error).__name__,
        )
        if isinstance(error, LocalSpeechError):
            return error
        work = "recognition" if task == "STT" else "synthesis"
        if isinstance(error, TimeoutError):
            return LocalSpeechExecutionError(
                f"Local speech {work} stopped responding and was ended. Retry; if it "
                "happens again, check the selected device and available memory."
            )
        checks = "device, model directory" if task == "STT" else "device"
        return LocalSpeechExecutionError(
            f"Local speech {work} failed ({type(error).__name__}). "
            f"Check the selected {checks} and available memory."
        )

    def _transcribe(
        self,
        local_id: str,
        audio: bytes,
        options: dict[str, Any],
        cancellation: _Cancellation,
    ) -> SpeechTranscriptionResult:
        definition, options, key = self._request(local_id, options, TASK_SPEECH_TO_TEXT)
        state = self._states[local_id]
        if key != state.key:
            state.unload()
        try:
            segments: list[dict[str, Any]] = []
            languages: set[str] = set()
            saw_samples = False
            for start, samples in _audio_chunks(audio):
                saw_samples = True
                cancellation.check()
                # Exact digital silence needs no model and must not invent text.
                if not samples.any():
                    continue
                engine = cast(
                    LocalTranscriptionEngine, self._loaded_engine(state, definition, options, key)
                )
                if (progress := _PROGRESS.get()) is not None:
                    progress.update("transcribing")
                with cancellation.running(engine):
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
        except _RequestCancelledError:
            raise
        except Exception as error:
            if cancellation.aborted:
                self._ended_by_cancellation(state, local_id)
                raise _RequestCancelledError("Local speech request was cancelled.") from error
            failure = self._failed(state, definition, error)
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
        for callback in tuple(self._audio_callbacks):
            callback.close()
        self._waiting.close()
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
            # An unstarted preload never starts; closing ends a waiting managed worker.
            for state in self._states.values():
                if state.preparing is not None:
                    state.preparing.cancel()
            self._close_task = asyncio.create_task(self._finish_close(), name="local-speech-close")
        # Cleanup may still be waiting for the inference worker. Cancellation
        # must never shut down its executor before the model can be unloaded.
        await settle_before_cancelling(self._close_task, on_late_failure=_log_close_failure)

    async def _finish_close(self) -> None:
        for callback in tuple(self._audio_callbacks):
            callback.close()
        # Ending loading and transcribing workers releases their worker threads.
        # Killing a process tree blocks, so it must not run on the Event Loop.
        await asyncio.to_thread(self._waiting.close)
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
        on_audio: SpeechAudioCallback | None = None,
    ) -> SpeechSynthesisResult:
        if progress is not None:
            progress.update("queued")
        state = self._states.get(local_id)
        if self._closed or state is None:
            raise LocalSpeechError(_TTS_UNAVAILABLE)
        callback = ThreadAudioCallback(on_audio) if on_audio is not None else None
        if callback is not None:
            self._audio_callbacks.add(callback)
        try:
            return await self._run(
                state,
                self._synthesize,
                local_id,
                text,
                dict(options),
                progress,
                callback,
                on_cancel=callback.close if callback is not None else None,
            )
        finally:
            if callback is not None:
                callback.close()
                self._audio_callbacks.discard(callback)

    def _synthesize(
        self,
        local_id: str,
        text: str,
        options: dict[str, Any],
        progress: SpeechProgress | None,
        on_audio: Callable[[SpeechAudioChunk], None] | None,
        cancellation: _Cancellation,
    ) -> SpeechSynthesisResult | None:
        """Synthesize on the engine's worker; ``None`` once the caller cancelled."""
        definition, options, key = self._request(local_id, options, TASK_TEXT_TO_SPEECH)
        if not 0 < len(text) <= 5000:
            raise LocalSpeechError(
                "Local speech synthesis accepts at most 5000 characters per request. "
                "Split the text into shorter requests."
            )
        state = self._states[local_id]
        with _PROGRESS.set(progress), _CANCELLATION.set(cancellation), _AUDIO.set(on_audio):
            try:
                cancellation.check()
                if key != state.key:
                    state.unload()
                engine = cast(
                    LocalSynthesisEngine, self._loaded_engine(state, definition, options, key)
                )
                if progress is not None:
                    progress.update("synthesizing")
                with cancellation.running(engine):
                    result = engine.synthesize(text, options)
                if not result.audio:
                    raise ValueError("Empty synthesis")
                return result
            except _RequestCancelledError:
                return None  # Not a failure: nobody waits for an error.
            except Exception as error:
                if cancellation.aborted:
                    self._ended_by_cancellation(state, local_id)
                    return None
                failure = self._failed(state, definition, error)
                if failure is error:
                    raise  # Closing ended the worker.
                raise failure from error


def _task_name(definition: SpeechEngineDefinition) -> str:
    return "STT" if TASK_SPEECH_TO_TEXT in definition.descriptor.task_types else "TTS"


def _tts_definitions(
    setup_for: Callable[[str, str, PinnedModel], LocalSpeechSetup],
    waiting: _WaitingWorkers,
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

    common = (
        select("device", "Device", "auto", ("auto", "cuda", "cpu", "mps")),
        _preload_field("speech output"),
    )
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
    )
    instructions = TaskModelOptionField(
        "instructions",
        "textarea",
        "Speaking instructions",
        default="",
        description="Optional style instructions, such as a calm or cheerful voice.",
    )
    chatter_options = (
        TaskModelOptionField(
            "language",
            "select",
            "Language",
            default="en",
            required=True,
            options=tuple(
                sorted(
                    (
                        _language_choice(code)
                        for code in (
                            *("ar", "da", "de", "el", "en", "es", "fi", "fr", "he", "hi", "it"),
                            *("ja", "ko", "ms", "nl", "no", "pl", "pt", "ru", "sv", "sw", "tr"),
                            "zh",
                        )
                    ),
                    key=lambda choice: choice.label,
                )
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
            (*qwen_options, instructions, *common),
        ),
        ("qwen3-tts-0.6b", "qwen3-tts", "Qwen3-TTS 0.6B", "Apache-2.0", (*qwen_options, *common)),
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
                partial(_TtsEngine, setup, waiting),
                ("device",),
                model,
                environment,
            )
        )
    return tuple(definitions)


class _WorkerProcess:
    """A ``speech_worker.py`` child the parent talks to in JSON lines.

    The child is ended with its process tree when a request passes its deadline,
    a cancellation's grace runs out (``abort``) or the executor closes; the
    request waiting for it then fails.
    """

    _closed_message: str  # The error of a request whose child closing ended.

    def __init__(self, python: Path, arguments: Sequence[str], waiting: _WaitingWorkers) -> None:
        from core.utils.processes import subprocess_creation_flags

        self._waiting = waiting
        self._ending = Lock()
        self._ended = ""  # Why the parent ended the child, if it did.
        self._process = subprocess.Popen(
            [
                str(python),
                "-I",
                "-B",
                str(Path(__file__).with_name("speech_worker.py")),
                *arguments,
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

    def abort(self) -> None:
        """End the child from another thread; the running request fails."""
        self.end("aborted")

    def end(self, reason: str) -> None:
        """Kill the child's process tree; the first reason names the failure."""
        with self._ending:
            self._ended = self._ended or reason
        _kill_speech_process(self._process)

    def _exchange(
        self,
        request: Mapping[str, Any],
        answer: str,
        deadline: float,
        phases: Container[str] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> Any:
        """Send one request line and return the child's ``answer`` field.

        Progress forwards the child's phases (only *phases* when given). The child
        is ended once *deadline* seconds pass without that answer.
        """
        process = self._process
        assert process.stdin is not None and process.stdout is not None
        timer = Timer(deadline, self.end, ("deadline",))
        timer.daemon = True
        self._waiting.add(self)
        timer.start()
        try:
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
            while line := process.stdout.readline(4096):
                event = json.loads(line)
                if event.get("error"):
                    raise RuntimeError(event["error"])
                if on_event is not None:
                    on_event(event)
                if (
                    (phase := event.get("phase"))
                    and (phases is None or phase in phases)
                    and (progress := _PROGRESS.get()) is not None
                ):
                    progress.update(phase)
                if (payload := event.get(answer)) is not None:
                    return payload
        except OSError, ValueError:
            if not self._ended:
                raise  # A broken pipe or line of a child ended here is reported below.
        finally:
            timer.cancel()
            self._waiting.discard(self)
        if self._ended == "deadline":
            raise TimeoutError(f"Speech worker did not answer within {deadline:.0f} seconds")
        if self._ended == "closed":
            raise LocalSpeechError(self._closed_message)
        raise RuntimeError("Speech worker exited")

    def _load(self, options: Mapping[str, Any]) -> None:
        """Have the child load its model; a child that fails to is closed."""
        try:
            cancellation = _CANCELLATION.get()
            with cancellation.running(self) if cancellation is not None else nullcontext():
                self._exchange({"load": True, "options": dict(options)}, "loaded", _LOAD_DEADLINE_S)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        _close_speech_process(self._process)


class _TtsEngine(_WorkerProcess):
    """A cached SDK process with fixed entry point and parent-owned output paths.

    Construction returns once the child reports the model loaded, so the
    executor's load boundary (logs, progress, preloading) covers the real load.
    """

    _closed_message = "Local speech synthesis is closed. Restart the vBot server before retrying."

    def __init__(
        self, setup: LocalSpeechSetup, waiting: _WaitingWorkers, options: Mapping[str, Any]
    ) -> None:
        super().__init__(setup.python, [setup.engine], waiting)
        self._load(options)

    def synthesize(self, text: str, options: Mapping[str, Any]) -> SpeechSynthesisResult:
        with tempfile.TemporaryDirectory(prefix="vbot-tts-") as directory:
            output = Path(directory) / "speech.wav"
            callback = _AUDIO.get()
            next_chunk = 0
            total_bytes = 0

            def chunk_ready(event: dict[str, Any]) -> None:
                nonlocal next_chunk, total_bytes
                if "audio_chunk" not in event:
                    return
                index = event["audio_chunk"]
                if callback is None or type(index) is not int or index != next_chunk:
                    raise ValueError("Invalid speech audio chunk")
                path = output.with_name(f"chunk-{index}.wav")
                size = path.stat().st_size
                total_bytes += size
                if not size > 44 or total_bytes > 64 * 1024 * 1024:
                    raise ValueError("Invalid speech audio chunk size")
                with wave.open(str(path), "rb") as wav:
                    if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                        raise ValueError("Invalid speech audio chunk format")
                    chunk = SpeechAudioChunk(wav.readframes(wav.getnframes()), wav.getframerate())
                path.unlink()
                callback(chunk)
                next_chunk += 1

            try:
                self._exchange(
                    {
                        "text": text,
                        "options": dict(options),
                        "output": str(output),
                        "stream_audio": callback is not None,
                    },
                    "done",
                    _SYNTHESIS_DEADLINE_S,
                    ("loading", "synthesizing"),
                    chunk_ready,
                )
            except BaseException:
                # A failed/cancelled playback consumer can leave the child still
                # generating into its WAV. Reap it before Windows removes files.
                self.close()
                raise
            if not 44 < output.stat().st_size <= 64 * 1024 * 1024:
                raise ValueError("Invalid output size")
            return SpeechSynthesisResult(output.read_bytes(), "audio/wav", "wav")


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


class _ManagedSttEngine(_WorkerProcess):
    """Keep the optional ML stack in a managed child interpreter.

    Construction returns once the child reports the model loaded, so the
    executor's load boundary (logs, progress, preloading) covers the real load.
    """

    _closed_message = "Local speech recognition is closed. Restart the vBot server before retrying."

    def __init__(
        self,
        setup: LocalSpeechSetup,
        app_root: Path,
        engine: str,
        waiting: _WaitingWorkers,
        options: Mapping[str, Any],
    ) -> None:
        super().__init__(setup.python, ["--stt", engine, str(app_root)], waiting)
        self._load(options)

    def transcribe(self, samples: Any, options: Mapping[str, Any]) -> SpeechTranscriptionResult:
        payload = self._exchange(
            {
                "samples": base64.b64encode(samples.astype("<f4").tobytes()).decode("ascii"),
                "options": dict(options),
            },
            "result",
            _INFERENCE_DEADLINE_S,
        )
        return SpeechTranscriptionResult(
            text=payload["text"],
            language=payload.get("language"),
            segments=tuple(payload.get("segments", ())),
            usage=payload.get("usage"),
        )


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
