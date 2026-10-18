"""Resources and bounded lifetime of one Voice listener generation.

Audio imports happen only while building a real listener. The controller admits
it under its lock before starting any capture, and accepts its generation-tagged
events; this unit owns construction and shutdown of the resources themselves.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from desktop.speech.server_client import DEFAULT_UPLOAD_BUDGET_BYTES, ServerAuthorization
from desktop.wakeword import contracts as voice
from desktop.wakeword._mock import MockEvents, MockListener
from desktop.wakeword.config import ERROR_NO_SERVER, PhraseConfig, VoiceConfig
from desktop.wakeword.engine import WakewordEngine, WakewordModelCatalog, WakewordModelError
from desktop.wakeword.readiness import ReadinessWorker
from desktop.wakeword.server_client import VoiceServerClient

if TYPE_CHECKING:
    from desktop.speech.capture import AudioCapture, CaptureStatus
    from desktop.speech.microphone import MicrophoneService
    from desktop.wakeword.commands import CommandOutcome, CommandPipeline, CommandRecorder
    from desktop.wakeword.detection import Detection, DetectionLoop
logger = logging.getLogger("vbot.desktop.wakeword.controller")
JOIN_TIMEOUT_SECONDS = 2.0

EngineFactory = Callable[
    [Sequence[PhraseConfig], Callable[[dict[str, float]], None]], WakewordEngine
]


@dataclass(frozen=True)
class VoiceRuntime:
    """Replaceable runtime dependencies (tests inject doubles; ``None`` uses the real one).

    The microphone, its device list and the echo canceller come from the
    :class:`~desktop.speech.microphone.MicrophoneService` the controller is
    given, which has its own seams.
    """

    engine_factory: EngineFactory | None = None
    speech_detector_factory: Callable[[], Any] | None = None
    transport: Any = None
    join_timeout: float = JOIN_TIMEOUT_SECONDS
    mock_frame_seconds: float = 0.1
    mock_stage_seconds: float = 0.8


class ListenerSession:
    """The threads and resources of one listener generation."""

    def __init__(
        self, generation: int, stop_event: threading.Event, client: VoiceServerClient | None
    ) -> None:
        self.generation = generation
        self.stop_event = stop_event
        self.client = client
        self.capture: AudioCapture | None = None
        self.detection: DetectionLoop | None = None
        self.recorder: CommandRecorder | None = None
        self.pipeline: CommandPipeline | None = None
        self.threads: list[threading.Thread] = []
        self.budget_bytes = DEFAULT_UPLOAD_BUDGET_BYTES
        self.readiness: ReadinessWorker | None = None

    def signal(self) -> None:
        """Ask every thread of the session to stop (non-blocking)."""
        self.stop_event.set()

    def shutdown(self, timeout: float) -> None:
        """Stop the session with bounded joins; threads still running are abandoned."""
        self.stop_event.set()
        deadline = time.monotonic() + timeout

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        stopped = True
        for component in (self.capture, self.detection, self.recorder):
            if component is not None and not component.join(remaining()):
                stopped = False
        if self.client is not None:
            # Aborts requests still in flight so their workers end promptly.
            self.client.close()
        if self.pipeline is not None and not self.pipeline.close(remaining()):
            stopped = False
        if self.readiness is not None and not self.readiness.join(remaining()):
            stopped = False
        for thread in self.threads:
            thread.join(remaining())
            stopped = stopped and not thread.is_alive()
        if not stopped:
            logger.warning(
                "Voice listener %s did not stop in time; abandoning its threads", self.generation
            )

    def prepare_transcription(self) -> None:
        """Let the server load its speech-to-text model while the command is spoken."""
        client = self.client
        if client is None or self.stop_event.is_set():
            return
        thread = threading.Thread(
            target=client.prepare_transcription, name="vbot-voice-prepare", daemon=True
        )
        self.threads = [known for known in self.threads if known.is_alive()]
        self.threads.append(thread)
        thread.start()


class ListenerEvents(MockEvents, Protocol):
    """Generation-tagged callbacks; detection callbacks never block."""

    def _on_scores(self, generation: int, scores: dict[str, float]) -> None: ...
    def _on_capture_status(self, generation: int, status: CaptureStatus) -> None: ...
    def _on_detection(self, generation: int, detection: Detection) -> None: ...
    def _on_listener_started(self, generation: int) -> None: ...
    def _on_listener_failed(self, generation: int, error_code: str) -> None: ...
    def _calibrating(self, generation: int) -> bool: ...
    def _on_command_stage(self, generation: int, command_id: str, stage: str) -> None: ...
    def _on_command_outcome(self, generation: int, outcome: CommandOutcome) -> None: ...


def _build_listener(
    generation: int,
    server_url: str,
    microphone: MicrophoneService,
    engine: WakewordEngine,
    runtime: VoiceRuntime,
    authorization: ServerAuthorization | None,
    events: ListenerEvents,
) -> ListenerSession:
    """Construct an unstarted listener; generation admission belongs to the controller."""
    from desktop.wakeword import _speech_detection
    from desktop.wakeword.commands import CommandPipeline, CommandRecorder
    from desktop.wakeword.detection import SUBSCRIPTION_SECONDS, DetectionLoop

    speech_detector_factory = (
        runtime.speech_detector_factory or _speech_detection.SpeechDetector.create
    )
    stop_event = threading.Event()
    client = VoiceServerClient(
        server_url, cancel=stop_event, authorization=authorization, transport=runtime.transport
    )
    session = ListenerSession(generation, stop_event, client)
    capture = microphone.create_capture(
        on_status=lambda status: events._on_capture_status(generation, status),
        stop_event=stop_event,
    )
    session.capture = capture
    session.detection = DetectionLoop(
        subscription=capture.subscribe(max_seconds=SUBSCRIPTION_SECONDS),
        engine=engine,
        stop_event=stop_event,
        on_detection=lambda detection: events._on_detection(generation, detection),
        on_started=lambda: events._on_listener_started(generation),
        on_failed=lambda code: events._on_listener_failed(generation, code),
        calibrating=lambda: events._calibrating(generation),
        speech_detector_factory=speech_detector_factory,
    )
    session.recorder = CommandRecorder(
        stop_event=stop_event,
        speech_detector_factory=speech_detector_factory,
        budget_bytes=lambda: session.budget_bytes,
    )
    session.pipeline = CommandPipeline(
        client,
        stop_event=stop_event,
        on_stage=lambda command_id, stage: events._on_command_stage(generation, command_id, stage),
        on_outcome=lambda outcome: events._on_command_outcome(generation, outcome),
    )
    return session


class ListenerStartError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class ListenerFactory:
    """Lazy stack selection and listener construction, including the explicit mock driver."""

    def __init__(
        self,
        microphone: MicrophoneService,
        catalog: WakewordModelCatalog,
        catalog_lock: threading.Lock,
        runtime: VoiceRuntime,
        authorization: ServerAuthorization | None,
        stack_available: Callable[[], bool],
        *,
        mock: bool,
    ) -> None:
        self._microphone = microphone
        self._catalog = catalog
        self._catalog_lock = catalog_lock
        self._runtime = runtime
        self._authorization = authorization
        self._stack_available = stack_available
        self._lock = threading.Lock()
        self._mode = voice.MODE_MOCK if mock else voice.MODE_REAL
        self._stack_checked = False

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    def retry(self) -> None:
        with self._lock:
            if self._mode == voice.MODE_UNAVAILABLE:
                self._mode = voice.MODE_REAL
                self._stack_checked = False

    def build(
        self,
        generation: int,
        config: VoiceConfig,
        server_url: str,
        events: ListenerEvents,
        *,
        refresh_devices: bool = False,
    ) -> ListenerSession:
        mode = self._resolve_mode()
        if mode == voice.MODE_UNAVAILABLE:
            raise ListenerStartError(voice.ERROR_VOICE_STACK_UNAVAILABLE)
        if mode == voice.MODE_MOCK:
            session = ListenerSession(generation, threading.Event(), None)
            session.threads.append(
                MockListener(session, config.active_model_ids[0], self._runtime, events)
            )
            return session
        if not server_url:
            raise ListenerStartError(ERROR_NO_SERVER)
        if refresh_devices:
            self._microphone.refresh_devices()

        def scores(values: dict[str, float]) -> None:
            events._on_scores(generation, values)

        try:
            factory = self._runtime.engine_factory
            if factory is not None:
                engine = factory(config.phrases, scores)
            else:
                with self._catalog_lock:
                    engine = self._catalog.create_engine(config.phrases, score_listener=scores)
        except WakewordModelError as exc:
            logger.warning("Wakeword engine unavailable: %s", exc)
            raise ListenerStartError(exc.error_code) from exc
        except Exception as exc:
            logger.warning("Wakeword engine could not be created", exc_info=True)
            raise ListenerStartError(voice.ERROR_ENGINE_START_FAILED) from exc
        return _build_listener(
            generation,
            server_url,
            self._microphone,
            engine,
            self._runtime,
            self._authorization,
            events,
        )

    def _resolve_mode(self) -> str:
        with self._lock:
            if self._mode != voice.MODE_REAL or self._stack_checked:
                return self._mode
        try:
            available = bool(self._stack_available())
        except Exception:
            logger.warning("Voice stack probe failed", exc_info=True)
            available = False
        with self._lock:
            if not self._stack_checked:
                self._stack_checked = True
                if not available:
                    logger.warning("The on-device Voice stack is unavailable")
                    self._mode = voice.MODE_UNAVAILABLE
            return self._mode
