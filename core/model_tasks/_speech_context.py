"""Local speech execution contracts and request-scoped cancellation/context."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock, Timer
from typing import Any, Protocol

from core.model_tasks.local_targets import LocalTaskTargetDescriptor
from core.model_tasks.model_files import PinnedModel
from core.model_tasks.speech_types import (
    SpeechAudioChunk,
    SpeechProgress,
    SpeechSynthesisResult,
    SpeechTranscriptionResult,
)
from core.utils.errors import VBotError

# A cancelled request's running engine call may still finish within these seconds.
_CANCEL_GRACE_S = 30.0
_SAMPLE_RATE = 16_000
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
