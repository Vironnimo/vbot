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
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any, cast

from core.model_tasks import _speech_context
from core.model_tasks._speech_catalog import PRELOAD_OPTION as PRELOAD_OPTION
from core.model_tasks._speech_catalog import _tts_definitions
from core.model_tasks._speech_catalog import builtin_speech_engines as builtin_speech_engines
from core.model_tasks._speech_context import (
    _AUDIO,
    _CANCELLATION,
    _PROGRESS,
    _SAMPLE_RATE,
    _Cancellation,
    _RequestCancelledError,
)
from core.model_tasks._speech_context import (
    LocalSpeechError as LocalSpeechError,
)
from core.model_tasks._speech_context import (
    LocalSpeechExecutionError as LocalSpeechExecutionError,
)
from core.model_tasks._speech_context import (
    LocalSynthesisEngine as LocalSynthesisEngine,
)
from core.model_tasks._speech_context import (
    LocalTranscriptionEngine as LocalTranscriptionEngine,
)
from core.model_tasks._speech_context import (
    SpeechEngineDefinition as SpeechEngineDefinition,
)
from core.model_tasks._speech_engines import _audio_chunks, _ManagedSttEngine, _WaitingWorkers
from core.model_tasks.constants import TASK_SPEECH_TO_TEXT, TASK_TEXT_TO_SPEECH
from core.model_tasks.local_targets import LocalTaskTargetDescriptor, LocalTaskTargetRegistry
from core.model_tasks.model_files import PinnedModel
from core.model_tasks.options import (
    TaskModelOptionSchema,
    validate_task_model_options,
)
from core.model_tasks.speech_audio import ThreadAudioCallback
from core.model_tasks.speech_input import SpeechInputError
from core.model_tasks.speech_setup import LocalSpeechSetup, ServerSpeechStack
from core.model_tasks.speech_types import (
    SpeechAudioCallback,
    SpeechAudioChunk,
    SpeechProgress,
    SpeechSynthesisResult,
    SpeechTranscriptionResult,
)
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool, settle_before_cancelling

_LOGGER = get_logger("speech.local")
_TTS_UNAVAILABLE = (
    "Local speech synthesis is unavailable. Open Settings → Voice → Speech models, "
    "select the local text-to-speech engine under Text to speech, and choose Install. "
    "Wait for setup to finish before retrying."
)


@dataclass
class _EngineState:
    workers: BoundedWorkerPool
    lifecycle_lock: Lock = field(default_factory=Lock)
    engine: LocalTranscriptionEngine | LocalSynthesisEngine | None = None
    key: tuple[Any, ...] | None = None
    pending: int = 0
    preparing: asyncio.Task[None] | None = None
    preparing_key: tuple[Any, ...] | None = None
    preparation_generation: int = 0
    retired: bool = False
    releasing: asyncio.Task[None] | None = None

    def unload(self, *, only_if_retired: bool = False) -> None:
        with self.lifecycle_lock:
            if only_if_retired and (not self.retired or self.pending != 1):
                return
            engine, self.engine = self.engine, None
            self.key = None
        # Detachment is atomic with reselection/prepare; process-tree cleanup
        # may block, so it stays outside the lock and on the engine's worker.
        if engine is not None:
            engine.close()


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
            self._release_retired_when_idle(state)
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
            _speech_context._CANCEL_GRACE_S,
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
        if state is None or definition is None or state.retired:
            return "unavailable"
        try:
            _definition, merged, key = self._request(
                local_id, dict(options), definition.descriptor.task_types[0]
            )
        except LocalSpeechError:
            return "unavailable"
        with state.lifecycle_lock:
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
                self._prepare_in_worker(state, local_id, merged, state.preparation_generation),
                name=f"local-speech-prepare:{local_id}",
            )
        return "loading"

    async def _prepare_in_worker(
        self, state: _EngineState, local_id: str, options: dict[str, Any], generation: int
    ) -> None:
        state.pending += 1
        try:
            await state.workers.run(self._prepare, local_id, options, generation)
        except LocalSpeechError:
            pass  # Logged by the worker; the next transcription reports it to its caller.
        except Exception:
            _LOGGER.error("Local speech preload failed (engine=%s)", local_id, exc_info=True)
        finally:
            state.pending -= 1
            self._release_retired_when_idle(state)

    def _prepare(self, local_id: str, options: dict[str, Any], generation: int) -> None:
        state = self._states[local_id]
        # Binding changes obsolete queued preloads, never an admitted request.
        if state.retired or generation != state.preparation_generation:
            return
        definition = self._definitions[local_id]
        definition, options, key = self._request(
            local_id, options, definition.descriptor.task_types[0]
        )
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
            engine = definition.create(self._engine_options(local_id, options))
            with state.lifecycle_lock:
                state.engine = engine
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
            with closing(_audio_chunks(audio, check_cancel=cancellation.check)) as chunks:
                for start, samples in chunks:
                    saw_samples = True
                    cancellation.check()
                    # Exact digital silence needs no model and must not invent text.
                    if not samples.any():
                        continue
                    engine = cast(
                        LocalTranscriptionEngine,
                        self._loaded_engine(state, definition, options, key),
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
        except SpeechInputError:
            # Rejected recordings do not invalidate a healthy cached model.
            raise
        except Exception as error:
            if cancellation.aborted:
                self._ended_by_cancellation(state, local_id)
                raise _RequestCancelledError("Local speech request was cancelled.") from error
            failure = self._failed(state, definition, error)
            if failure is error:
                raise
            raise failure from error

    def retain_transcription_model(self, local_id: str | None) -> None:
        """Keep the selected STT model warm and retire others after accepted work.

        A Provider or cleared binding passes ``None``. This changes residency,
        not request admission; already accepted transcriptions retain their
        binding. TTS-capable engines keep their independent lifecycle. Loop only.
        """
        if self._closed:
            return
        for name, definition in self._definitions.items():
            if definition.descriptor.task_types != (TASK_SPEECH_TO_TEXT,):
                continue
            state = self._states[name]
            with state.lifecycle_lock:
                if name != local_id and not state.retired:
                    state.preparation_generation += 1
                    state.preparing_key = None
                state.retired = name != local_id
            self._release_retired_when_idle(state)

    def _release_retired_when_idle(self, state: _EngineState) -> None:
        if (
            self._closed
            or not state.retired
            or state.pending
            or state.engine is None
            or (state.releasing is not None and not state.releasing.done())
        ):
            return
        state.releasing = asyncio.get_running_loop().create_task(
            self._release_retired(state), name="local-speech-release"
        )

    async def _release_retired(self, state: _EngineState) -> None:
        state.pending += 1
        try:
            await state.workers.run(self._unload_retired, state)
        except Exception:
            _LOGGER.error("Retired local speech model cleanup failed", exc_info=True)
        finally:
            state.pending -= 1
            state.releasing = None
            self._release_retired_when_idle(state)

    def _unload_retired(self, state: _EngineState) -> None:
        # Reselection or another accepted recording may precede worker admission.
        # That request's completion schedules cleanup again if still needed.
        state.unload(only_if_retired=True)

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
            if state.releasing is not None:
                state.releasing.cancel()
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
            *(
                task
                for state in self._states.values()
                for task in (state.preparing, state.releasing)
                if task is not None
            ),
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


def _log_close_failure(error: BaseException) -> None:
    """Report a shutdown failure that its cancelled caller no longer receives."""
    _LOGGER.error("Local speech shutdown failed after its caller was cancelled", exc_info=error)
