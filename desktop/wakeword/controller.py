"""Desktop Voice: the controller behind the Voice bridge methods.

:class:`VoiceController` is the public Voice API of the Desktop facade. It owns

- the persisted Voice config (read and written through the ``wakeword``
  settings section, validated by :mod:`desktop.wakeword.config`) and the
  immutable :class:`VoiceConfig` snapshot the running listener uses;
- the listener lifecycle: a *session* (capture, detection, command recorder,
  command pipeline, readiness checks) built from one config snapshot and one
  server URL, tagged with a generation number. A config change of the
  listener settings, a server switch, a retry or a disable stops the old
  session (signal, bounded joins on a background thread; a thread that does
  not stop in time is abandoned) and starts a new one. Changes that only
  affect action routing swap the snapshot without restarting capture;
- dispatching detections: ``live_voice`` phrases ask the page for a Live voice
  call, ``command`` phrases record a command and hand it to the pipeline;
- the status snapshot and the event stream, published through a
  :class:`VoiceEventSink` with one shared, increasing sequence. Callbacks from
  a stopped session carry an old generation and are dropped;
- phrase calibration, the mock mode (``--mock-wakeword``: a simulated command
  cycle, no audio or network) and the unavailable mode (the on-device stack
  cannot be imported).

Threading: bridge calls arrive on pywebview threads, callbacks on the session
threads. ``_mutation_lock`` serializes bridge mutations (settings write, then
apply); ``_catalog_lock`` serializes catalog file access; ``_lock`` guards the
runtime state and is never held across network calls, thread joins or
PortAudio calls. Publications happen under ``_lock`` so their order matches
the sequence; the sink must never block or call back.

Import cost: the numpy-based audio modules are imported only when a real
listener starts, so an ordinary Desktop launch with Voice disabled never loads
them.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from desktop import settings as desktop_settings
from desktop.wakeword.calibration import PhraseCalibration
from desktop.wakeword.config import (
    ERROR_NO_SERVER,
    MAX_ACTIVE_PHRASES,
    CommandAction,
    LiveVoiceAction,
    PhraseConfig,
    VoiceConfig,
    apply_voice_changes,
    config_status,
    forget_model,
    parse_voice_config,
    set_enabled,
)
from desktop.wakeword.engine import (
    MockWakewordEngine,
    WakewordEngine,
    WakewordModelCatalog,
    WakewordModelDescriptor,
    WakewordModelError,
)
from desktop.wakeword.server_client import (
    DEFAULT_UPLOAD_BUDGET_BYTES,
    ERROR_TARGET_AGENT_UNAVAILABLE,
    VoiceRequestCancelled,
    VoiceServerClient,
    VoiceServerError,
)

if TYPE_CHECKING:
    from desktop.wakeword.capture import (
        AudioCapture,
        CaptureStatus,
        EchoStageFactory,
        EchoStagePool,
    )
    from desktop.wakeword.commands import (
        CommandOutcome,
        CommandPipeline,
        CommandRecorder,
        RecordingResult,
    )
    from desktop.wakeword.detection import Detection, DetectionLoop

logger = logging.getLogger("vbot.desktop.wakeword.controller")

MODE_REAL = "real"
MODE_MOCK = "mock"
MODE_UNAVAILABLE = "unavailable"

STATE_OFF = "off"
STATE_STARTING = "starting"
STATE_LISTENING = "listening"
STATE_MICROPHONE_DISCONNECTED = "microphone_disconnected"
STATE_ERROR = "error"

ERROR_VOICE_STACK_UNAVAILABLE = "voice_stack_unavailable"
ERROR_MISSING_TARGET_AGENT = "missing_target_agent"
ERROR_ENGINE_START_FAILED = "engine_start_failed"
ERROR_PIPELINE_FAILED = "pipeline_failed"
ERROR_CALIBRATION_UNAVAILABLE = "calibration_unavailable"
ERROR_CALIBRATION_INACTIVE = "calibration_inactive"
ERROR_WAKEWORD_MODEL_ACTIVE = "wakeword_model_active"

EVENT_DETECTED = "detected"
EVENT_RECORDING_STARTED = "recording_started"
EVENT_RECORDING_ENDED = "recording_ended"
EVENT_NO_SPEECH = "no_speech"
EVENT_COMMAND_FAILED = "command_failed"
EVENT_SENT = "sent"
EVENT_LIVE_REQUESTED = "live_requested"
EVENT_MICROPHONE_DISCONNECTED = "microphone_disconnected"
EVENT_MICROPHONE_RECONNECTED = "microphone_reconnected"
EVENT_ERROR = "error"

LIVE_REQUEST_SOURCE = "wakeword"

READINESS_STALE_SECONDS = 300.0
"""A successful readiness check older than this is repeated on the next detection."""

CALIBRATION_PUSH_INTERVAL_SECONDS = 0.2
"""Calibration score pushes are limited to five per second."""

JOIN_TIMEOUT_SECONDS = 2.0

_MOCK_SCORES = [0.0] * 25 + [1.0]
_CAPTURE_CAPTURING = "capturing"
_CAPTURE_DISCONNECTED = "disconnected"
_CAPTURE_FAILED = "failed"
_CAPTURE_STOPPED = "stopped"
_STAGE_TRANSCRIBING = "transcribing"
_STAGE_SENDING = "sending"


class VoiceEventSink(Protocol):
    """Receives Voice status snapshots and events for the page; never blocks."""

    def publish_status(self, status: Mapping[str, Any]) -> None: ...

    def publish_event(self, event: Mapping[str, Any]) -> None: ...


class VoiceControlError(RuntimeError):
    """A Voice request that cannot run in the current state, with a stable code."""

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


EngineFactory = Callable[
    [Sequence[PhraseConfig], Callable[[dict[str, float]], None]], WakewordEngine
]


@dataclass(frozen=True)
class VoiceRuntime:
    """Replaceable runtime dependencies (tests inject doubles; ``None`` uses the real one).

    ``echo_stage_factory`` creates the echo stage (default:
    :func:`desktop.wakeword.echo.create_echo_stage`). It runs at most once per
    process, on the capture thread of the first listener with echo
    cancellation enabled; later listeners reuse the stage.
    """

    audio_backend: Any = None
    engine_factory: EngineFactory | None = None
    speech_detector_factory: Callable[[], Any] | None = None
    fallback_vad_factory: Callable[[], Any] | None = None
    transport: Any = None
    echo_stage_factory: EchoStageFactory | None = None
    reconnect_interval: float = 30.0
    join_timeout: float = JOIN_TIMEOUT_SECONDS
    mock_frame_seconds: float = 0.1
    mock_stage_seconds: float = 0.8


@dataclass(frozen=True)
class _Recording:
    command_id: str
    model_id: str
    agent_id: str | None
    session_behavior: str


@dataclass
class _Readiness:
    """Server-side readiness of the command phrases, from the last check."""

    speech_problem: str | None = None
    agent_problems: dict[str, str | None] = field(default_factory=dict)
    checked_at: float | None = None
    running: bool = False
    again: bool = False


class _Session:
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
        for thread in self.threads:
            thread.join(remaining())
            stopped = stopped and not thread.is_alive()
        if not stopped:
            logger.warning(
                "Voice listener %s did not stop in time; abandoning its threads", self.generation
            )


class VoiceController:
    """Voice API of the Desktop facade: config, lifecycle, dispatch, status and events.

    ``live_requests(action, source)`` asks the page for a Live voice call and
    must not block. ``stack_available()`` reports whether the on-device stack
    imports; it runs once, lazily, on a background thread when a real
    listener first starts. The listener starts with :meth:`start` (after the
    window is shown) and ends with :meth:`close`.
    """

    def __init__(
        self,
        *,
        settings_path: Path | None,
        server_url: str,
        sink: VoiceEventSink,
        live_requests: Callable[[str, str], None],
        mock: bool = False,
        stack_available: Callable[[], bool] = lambda: True,
        catalog: WakewordModelCatalog | None = None,
        runtime: VoiceRuntime | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings_path = settings_path
        self._sink = sink
        self._live_requests = live_requests
        self._stack_available = stack_available
        self._catalog = catalog if catalog is not None else WakewordModelCatalog(settings_path)
        self._runtime = runtime if runtime is not None else VoiceRuntime()
        self._clock = clock

        self._mutation_lock = threading.Lock()
        self._catalog_lock = threading.Lock()
        self._lock = threading.Lock()

        self._config = parse_voice_config(
            desktop_settings.read_section(desktop_settings.WAKEWORD_KEY, settings_path)
        )
        self._server_url = _normalized_url(server_url)
        self._mode = MODE_MOCK if mock else MODE_REAL
        self._stack_checked = False
        self._started = False
        self._closed = False
        self._generation = 0
        self._session: _Session | None = None
        self._transition_thread: threading.Thread | None = None
        self._routing_version = 0
        self._sequence = 0
        self._reported_state: tuple[str, str | None] = (STATE_OFF, None)
        self._labels: dict[str, str] = {}
        self._labels_loaded = False
        self._command_ids = itertools.count(1)
        self._echo_stages: EchoStagePool | None = None  # created with the first real listener
        # Runtime state of the current generation (see _reset_runtime_locked).
        self._recording: _Recording | None = None
        self._commands: dict[str, dict[str, Any]] = {}
        self._fatal_error: str | None = None
        self._capture_state: str | None = None
        self._capture_error: str | None = None
        self._active_microphone: dict[str, Any] | None = None
        self._echo_state = "off"
        self._listener_ready = False
        self._readiness = _Readiness()
        self._calibration: PhraseCalibration | None = None
        self._calibration_pushed_at = 0.0
        self._reported_state = self._derive_state_locked()

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start listening when Voice is enabled (call once the window is shown)."""
        with self._mutation_lock, self._lock:
            if self._started or self._closed:
                return
            self._started = True
            if self._config.enabled:
                self._transition_locked()
                self._commit_locked()

    def close(self) -> None:
        """Stop listening for good; waits a bounded time for the threads to end."""
        with self._mutation_lock:
            with self._lock:
                if self._closed:
                    return
                self._closed = True
                self._generation += 1
                session, self._session = self._session, None
                if session is not None:
                    session.signal()
                transition, self._transition_thread = self._transition_thread, None
            timeout = self._runtime.join_timeout
            if transition is not None:
                transition.join(timeout)
            if session is not None:
                session.shutdown(timeout)

    def set_server_url(self, server_url: str) -> None:
        """Follow the window's server: a different server rebuilds the listener."""
        url = _normalized_url(server_url)
        with self._mutation_lock, self._lock:
            if url == self._server_url:
                return
            logger.info("Voice follows the server %s", url or "(none)")
            self._server_url = url
            self._transition_locked()
            self._commit_locked()

    # -- Status --------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Return the current status snapshot."""
        self._ensure_labels()
        with self._lock:
            return self._status_locked()

    # -- Config --------------------------------------------------------------

    def set_enabled(self, enabled: object) -> dict[str, Any]:
        """Persist and apply the enable switch; returns ``{enabled, error_code}``.

        Readiness problems are reported through the status, never by refusing
        the switch, so ``error_code`` is always ``None``.
        """
        with self._mutation_lock:
            section = desktop_settings.update_section(
                desktop_settings.WAKEWORD_KEY,
                lambda raw: set_enabled(raw, enabled),
                self._settings_path,
            )
            config = parse_voice_config(section)
            logger.info("Voice %s", "enabled" if config.enabled else "disabled")
            self._apply_config(config)
        return {"enabled": config.enabled, "error_code": None}

    def update_config(self, changes: object) -> dict[str, Any]:
        """Validate, persist and apply one partial config change; returns the status."""
        known_model_ids = self._known_model_ids()
        with self._mutation_lock:
            with self._lock:
                server_url = self._server_url
            section = desktop_settings.update_section(
                desktop_settings.WAKEWORD_KEY,
                lambda raw: apply_voice_changes(
                    raw,
                    changes,
                    server_url=server_url,
                    known_model_ids=known_model_ids.__contains__,
                ),
                self._settings_path,
            )
            self._apply_config(parse_voice_config(section))
        return self.status()

    def retry(self) -> dict[str, Any]:
        """Rebuild the listener from the stored config; returns the status."""
        with self._mutation_lock:
            config = parse_voice_config(
                desktop_settings.read_section(desktop_settings.WAKEWORD_KEY, self._settings_path)
            )
            with self._lock:
                logger.info("Voice listener retry requested")
                if self._mode == MODE_UNAVAILABLE:
                    self._mode = MODE_REAL
                    self._stack_checked = False
                self._config = config
                self._transition_locked()
                self._commit_locked()
        return self.status()

    # -- Recording -----------------------------------------------------------

    def stop_recording(self) -> dict[str, Any]:
        """End the current command recording and send what was captured; returns the status."""
        with self._lock:
            session = self._session
            recording = self._recording
        if session is not None and session.recorder is not None and recording is not None:
            session.recorder.stop()
        return self.status()

    # -- Models and devices --------------------------------------------------

    def list_microphones(self) -> list[dict[str, Any]]:
        """Return the shared-mode input devices and whether Voice can use them."""
        from desktop.wakeword._microphones import list_microphones

        return list_microphones(self._runtime.audio_backend)

    def list_models(self) -> list[dict[str, Any]]:
        """Return the curated built-ins and the imported models."""
        with self._catalog_lock:
            models = self._catalog.list_models()
        self._store_labels(models)
        return [model.to_dict() for model in models]

    def import_model(self, filename: str, content: bytes) -> dict[str, Any]:
        """Install one model; activates it while fewer than the maximum phrases are active."""
        with self._catalog_lock:
            descriptor = self._catalog.import_model(filename, content)
        self._ensure_labels(force=True)
        activated = False
        with self._mutation_lock:
            known_model_ids = self._known_model_ids()
            with self._lock:
                server_url = self._server_url

            def activate(raw: dict[str, Any]) -> dict[str, Any]:
                nonlocal activated
                active = parse_voice_config(raw).active_model_ids
                if len(active) >= MAX_ACTIVE_PHRASES or descriptor.id in active:
                    return raw
                activated = True
                return apply_voice_changes(
                    raw,
                    {"active_model_ids": [*active, descriptor.id]},
                    server_url=server_url,
                    known_model_ids=known_model_ids.__contains__,
                )

            section = desktop_settings.update_section(
                desktop_settings.WAKEWORD_KEY, activate, self._settings_path
            )
            logger.info(
                "Wakeword model imported (model=%s, activated=%s)", descriptor.id, activated
            )
            self._apply_config(parse_voice_config(section))
        return {**descriptor.to_dict(), "activated": activated}

    def delete_model(self, model_id: object) -> dict[str, bool]:
        """Remove one inactive imported model and its per-phrase settings."""
        if not isinstance(model_id, str) or not model_id.strip():
            raise WakewordModelError("Wakeword model id must be a non-empty string")
        model_id = model_id.strip()
        with self._mutation_lock:
            with self._lock:
                active = model_id in self._config.active_model_ids
            if active:
                raise WakewordModelError(
                    "An active wake phrase cannot be deleted. Deactivate it first.",
                    error_code=ERROR_WAKEWORD_MODEL_ACTIVE,
                )
            with self._catalog_lock:
                self._catalog.delete_model(model_id)
            section = desktop_settings.update_section(
                desktop_settings.WAKEWORD_KEY,
                lambda raw: forget_model(raw, model_id),
                self._settings_path,
            )
            logger.info("Wakeword model deleted (model=%s)", model_id)
            self._ensure_labels(force=True)
            self._apply_config(parse_voice_config(section))
        return {"deleted": True}

    # -- Calibration ---------------------------------------------------------

    def start_calibration(self, model_id: object) -> dict[str, Any]:
        """Calibrate one active phrase; commands pause while it runs. Returns the status."""
        self._ensure_labels()
        with self._lock:
            state, _ = self._derive_state_locked()
            if (
                self._mode != MODE_REAL
                or state != STATE_LISTENING
                or not isinstance(model_id, str)
                or self._config.phrase(model_id) is None
            ):
                raise VoiceControlError(
                    "Calibration needs Voice listening with this wake phrase active.",
                    error_code=ERROR_CALIBRATION_UNAVAILABLE,
                )
            now = self._clock()
            self._calibration = PhraseCalibration(model_id, now=now)
            self._calibration_pushed_at = now
            logger.info("Voice calibration started (model=%s)", model_id)
            self._commit_locked()
            return self._status_locked()

    def restart_calibration(self) -> dict[str, Any]:
        """Discard the measurements and start again from room noise. Returns the status."""
        self._ensure_labels()
        with self._lock:
            calibration = self._active_calibration_locked()
            if calibration is None:
                raise VoiceControlError(
                    "No calibration is running.", error_code=ERROR_CALIBRATION_INACTIVE
                )
            calibration.restart(self._clock())
            logger.info("Voice calibration restarted (model=%s)", calibration.model_id)
            self._commit_locked()
            return self._status_locked()

    def stop_calibration(self) -> dict[str, Any]:
        """Leave calibration without changing any sensitivity. Returns the status."""
        self._ensure_labels()
        with self._lock:
            if self._calibration is not None:
                logger.info("Voice calibration stopped (model=%s)", self._calibration.model_id)
                self._calibration = None
                self._commit_locked()
            return self._status_locked()

    # -- Transitions (under _lock) ---------------------------------------------

    def _apply_config(self, config: VoiceConfig) -> None:
        with self._lock:
            previous = self._config
            self._config = config
            if previous.enabled != config.enabled or previous.listener_settings_changed(config):
                self._transition_locked()
            else:
                self._routing_version += 1
                if self._session is not None:
                    self._request_readiness_locked(self._session)
            self._commit_locked()

    def _transition_locked(self) -> None:
        """Stop the running session and start the next one on a background thread."""
        self._generation += 1
        generation = self._generation
        session, self._session = self._session, None
        if session is not None:
            session.signal()
        self._reset_runtime_locked()
        should_run = self._started and not self._closed and self._config.enabled
        if session is None and not should_run:
            return
        previous = self._transition_thread
        thread = threading.Thread(
            target=self._run_transition,
            args=(generation, previous, session, should_run),
            name="vbot-voice-start" if should_run else "vbot-voice-stop",
            daemon=True,
        )
        self._transition_thread = thread
        thread.start()

    def _reset_runtime_locked(self) -> None:
        """Forget the runtime state of a stopped session; a recording ends for the page."""
        if self._recording is not None:
            self._emit_recording_locked(EVENT_RECORDING_ENDED, self._recording)
        self._recording = None
        self._commands = {}
        self._fatal_error = None
        self._capture_state = None
        self._capture_error = None
        self._active_microphone = None
        self._echo_state = "off"
        self._listener_ready = False
        self._readiness = _Readiness()
        self._calibration = None
        self._calibration_pushed_at = 0.0

    def _run_transition(
        self,
        generation: int,
        previous: threading.Thread | None,
        session: _Session | None,
        should_run: bool,
    ) -> None:
        timeout = self._runtime.join_timeout
        try:
            if previous is not None:
                previous.join(timeout)
            if session is not None:
                session.shutdown(timeout)
            if should_run:
                self._build_session(generation)
        except Exception:
            logger.exception("Voice listener could not start")
            self._fail_start(generation, ERROR_PIPELINE_FAILED)

    def _build_session(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            config = self._config
            server_url = self._server_url
        mode = self._resolve_mode()
        self._ensure_labels()
        if mode == MODE_UNAVAILABLE:
            self._fail_start(generation, ERROR_VOICE_STACK_UNAVAILABLE)
            return
        if mode == MODE_MOCK:
            self._start_mock_session(generation, config)
            return
        if not server_url:
            self._fail_start(generation, ERROR_NO_SERVER)
            return
        try:
            engine = self._create_engine(config.phrases, generation)
        except WakewordModelError as exc:
            logger.warning("Wakeword engine unavailable: %s", exc)
            self._fail_start(generation, exc.error_code)
            return
        except Exception:
            logger.warning("Wakeword engine could not be created", exc_info=True)
            self._fail_start(generation, ERROR_ENGINE_START_FAILED)
            return

        from desktop.wakeword import _speech_detection
        from desktop.wakeword.capture import AudioCapture, EchoStagePool
        from desktop.wakeword.commands import CommandPipeline, CommandRecorder
        from desktop.wakeword.detection import SUBSCRIPTION_SECONDS, DetectionLoop

        runtime = self._runtime
        with self._lock:
            if self._echo_stages is None:
                self._echo_stages = EchoStagePool(runtime.echo_stage_factory or _create_echo_stage)
            echo_stages = self._echo_stages
        speech_detector_factory = (
            runtime.speech_detector_factory or _speech_detection.SpeechDetector.create
        )
        fallback_vad_factory = runtime.fallback_vad_factory or _speech_detection.create_fallback_vad
        stop_event = threading.Event()
        client = VoiceServerClient(server_url, cancel=stop_event, transport=runtime.transport)
        session = _Session(generation, stop_event, client)
        capture = AudioCapture(
            microphone=config.microphone,
            echo_cancellation=config.echo_cancellation,
            echo_stages=echo_stages,
            on_status=lambda status: self._on_capture_status(generation, status),
            stop_event=stop_event,
            backend=runtime.audio_backend,
            reconnect_interval=runtime.reconnect_interval,
        )
        session.capture = capture
        session.detection = DetectionLoop(
            subscription=capture.subscribe(max_seconds=SUBSCRIPTION_SECONDS),
            engine=engine,
            stop_event=stop_event,
            on_detection=lambda detection: self._on_detection(generation, detection),
            on_started=lambda: self._on_listener_started(generation),
            on_failed=lambda code: self._on_listener_failed(generation, code),
            calibrating=lambda: self._calibrating(generation),
            speech_detector_factory=speech_detector_factory,
            fallback_vad_factory=fallback_vad_factory,
        )
        session.recorder = CommandRecorder(
            stop_event=stop_event,
            speech_detector_factory=speech_detector_factory,
            fallback_vad_factory=fallback_vad_factory,
            budget_bytes=lambda: session.budget_bytes,
        )
        session.pipeline = CommandPipeline(
            client,
            stop_event=stop_event,
            on_stage=lambda command_id, stage: self._on_command_stage(
                generation, command_id, stage
            ),
            on_outcome=lambda outcome: self._on_command_outcome(generation, outcome),
        )
        with self._lock:
            if generation == self._generation:
                self._session = session
                capture.start()
                session.detection.start()
                self._request_readiness_locked(session)
                logger.info(
                    "Voice listener starting (phrases=%s, server=%s)",
                    ",".join(config.active_model_ids),
                    server_url,
                )
                return
        client.close()

    def _start_mock_session(self, generation: int, config: VoiceConfig) -> None:
        session = _Session(generation, threading.Event(), None)
        thread = threading.Thread(
            target=self._run_mock,
            args=(session, config.active_model_ids[0]),
            name="vbot-voice-mock",
            daemon=True,
        )
        session.threads.append(thread)
        with self._lock:
            if generation != self._generation:
                return
            self._session = session
            self._listener_ready = True
            logger.info("Voice mock listener started")
            self._commit_locked()
            thread.start()

    def _resolve_mode(self) -> str:
        with self._lock:
            if self._mode != MODE_REAL or self._stack_checked:
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
                    self._mode = MODE_UNAVAILABLE
            return self._mode

    def _create_engine(self, phrases: Sequence[PhraseConfig], generation: int) -> WakewordEngine:
        def score_listener(scores: dict[str, float]) -> None:
            self._on_scores(generation, scores)

        factory = self._runtime.engine_factory
        if factory is not None:
            return factory(phrases, score_listener)
        with self._catalog_lock:
            return self._catalog.create_engine(phrases, score_listener=score_listener)

    def _fail_start(self, generation: int, error_code: str) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._fatal_error = error_code
            self._commit_locked()

    def _fail_session_locked(self, error_code: str) -> None:
        """Stop the current session after a fatal listener failure."""
        self._fatal_error = error_code
        if self._session is not None:
            self._session.signal()
        if self._recording is not None:
            self._emit_recording_locked(EVENT_RECORDING_ENDED, self._recording)
            self._recording = None
        self._commands.clear()
        self._calibration = None
        self._listener_ready = False

    # -- Session callbacks ---------------------------------------------------

    def _on_capture_status(self, generation: int, status: CaptureStatus) -> None:
        with self._lock:
            if generation != self._generation or status.state == _CAPTURE_STOPPED:
                return
            self._capture_state = status.state
            self._capture_error = status.error_code
            self._active_microphone = (
                status.microphone.to_status() if status.microphone is not None else None
            )
            self._echo_state = status.echo_state
            if status.state == _CAPTURE_FAILED:
                self._fail_session_locked(status.error_code or ERROR_PIPELINE_FAILED)
            self._commit_locked()

    def _on_listener_started(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._listener_ready = True
            self._commit_locked()

    def _on_listener_failed(self, generation: int, error_code: str) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._fail_session_locked(error_code)
            self._commit_locked()

    def _calibrating(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation and self._calibration is not None

    def _on_scores(self, generation: int, scores: dict[str, float]) -> None:
        with self._lock:
            calibration = self._calibration
            if generation != self._generation or calibration is None:
                return
            now = self._clock()
            calibration.feed(scores, now)
            if calibration.expired(now):
                logger.info("Voice calibration timed out (model=%s)", calibration.model_id)
                self._calibration = None
                self._commit_locked()
            elif now - self._calibration_pushed_at >= CALIBRATION_PUSH_INTERVAL_SECONDS:
                self._calibration_pushed_at = now
                self._push_status_locked()

    def _on_detection(self, generation: int, detection: Detection) -> None:
        live_mode: str | None = None
        recording: _Recording | None = None
        with self._lock:
            session = self._session
            if generation != self._generation or session is None:
                return
            model_id = detection.model_id
            action = self._config.effective_action(model_id, self._server_url)
            if isinstance(action, LiveVoiceAction):
                self._emit_locked(EVENT_DETECTED, model_id=model_id)
                self._emit_locked(EVENT_LIVE_REQUESTED, model_id=model_id)
                live_mode = action.mode
            elif self._recording is not None:
                logger.info("Wake phrase ignored during a command recording (model=%s)", model_id)
                return
            else:
                self._emit_locked(EVENT_DETECTED, model_id=model_id)
                problem = self._command_problem_locked(action)
                if problem is not None:
                    logger.info(
                        "Voice command not recorded (model=%s, problem=%s)", model_id, problem
                    )
                    self._emit_locked(
                        EVENT_COMMAND_FAILED,
                        model_id=model_id,
                        agent_id=action.agent_id,
                        error_code=problem,
                    )
                    if problem != ERROR_MISSING_TARGET_AGENT:
                        self._request_readiness_locked(session)
                    self._commit_locked()
                    return
                if self._readiness_stale_locked():
                    self._request_readiness_locked(session)
                recording = _Recording(
                    command_id=f"c{next(self._command_ids)}",
                    model_id=model_id,
                    agent_id=action.agent_id,
                    session_behavior=action.session_behavior or "active",
                )
                self._recording = recording
                self._emit_recording_locked(EVENT_RECORDING_STARTED, recording)
            self._commit_locked()
        if live_mode is not None:
            self._live_requests(live_mode, LIVE_REQUEST_SOURCE)
        elif recording is not None:
            self._start_recording(generation, session, recording, detection)

    def _start_recording(
        self,
        generation: int,
        session: _Session,
        recording: _Recording,
        detection: Detection,
    ) -> None:
        from desktop.wakeword.commands import (
            OUTCOME_FAILED,
            SUBSCRIPTION_SECONDS,
            RecordingResult,
        )

        assert session.capture is not None and session.recorder is not None
        after_index = detection.pre_roll[-1].index if detection.pre_roll else None
        subscription = session.capture.subscribe(
            max_seconds=SUBSCRIPTION_SECONDS, after_index=after_index
        )
        started = session.recorder.start(
            detection.pre_roll,
            subscription,
            lambda result: self._on_recording_done(generation, recording, result),
        )
        if not started:
            subscription.close()
            self._on_recording_done(
                generation,
                recording,
                RecordingResult(OUTCOME_FAILED, error_code=ERROR_PIPELINE_FAILED),
            )

    def _on_recording_done(
        self, generation: int, recording: _Recording, result: RecordingResult
    ) -> None:
        from desktop.wakeword.commands import (
            OUTCOME_AUDIO,
            OUTCOME_FAILED,
            OUTCOME_NO_SPEECH,
            Command,
        )

        command: Command | None = None
        with self._lock:
            session = self._session
            if generation != self._generation or self._recording is not recording:
                return
            self._recording = None
            self._emit_recording_locked(EVENT_RECORDING_ENDED, recording)
            if result.outcome == OUTCOME_NO_SPEECH:
                self._emit_locked(
                    EVENT_NO_SPEECH, command_id=recording.command_id, model_id=recording.model_id
                )
            elif result.outcome == OUTCOME_FAILED:
                self._emit_locked(
                    EVENT_COMMAND_FAILED,
                    command_id=recording.command_id,
                    model_id=recording.model_id,
                    agent_id=recording.agent_id,
                    error_code=result.error_code or ERROR_PIPELINE_FAILED,
                )
            elif (
                result.outcome == OUTCOME_AUDIO
                and result.wav is not None
                and recording.agent_id is not None
            ):
                self._commands[recording.command_id] = {
                    "command_id": recording.command_id,
                    "model_id": recording.model_id,
                    "stage": _STAGE_TRANSCRIBING,
                }
                command = Command(
                    command_id=recording.command_id,
                    model_id=recording.model_id,
                    agent_id=recording.agent_id,
                    session_behavior=recording.session_behavior,
                    wav=result.wav,
                )
            self._commit_locked()
        if command is None or session is None or session.pipeline is None:
            return
        if not session.pipeline.submit(command):
            with self._lock:
                dropped = self._commands.pop(command.command_id, None)
                if generation == self._generation and dropped is not None:
                    self._commit_locked()

    def _on_command_stage(self, generation: int, command_id: str, stage: str) -> None:
        with self._lock:
            entry = self._commands.get(command_id)
            if generation != self._generation or entry is None or entry["stage"] == stage:
                return
            entry["stage"] = stage
            self._commit_locked()

    def _on_command_outcome(self, generation: int, outcome: CommandOutcome) -> None:
        with self._lock:
            if generation != self._generation:
                return
            if self._commands.pop(outcome.command_id, None) is None:
                return
            self._emit_locked(
                outcome.kind,
                command_id=outcome.command_id,
                model_id=outcome.model_id,
                agent_id=outcome.agent_id,
                session_id=outcome.session_id,
                error_code=outcome.error_code,
            )
            if outcome.error_code == ERROR_TARGET_AGENT_UNAVAILABLE and outcome.agent_id:
                self._readiness.agent_problems[outcome.agent_id] = outcome.error_code
            self._commit_locked()

    # -- Readiness -----------------------------------------------------------

    def _command_problem_locked(self, action: CommandAction) -> str | None:
        if action.agent_id is None:
            return ERROR_MISSING_TARGET_AGENT
        if self._readiness.speech_problem is not None:
            return self._readiness.speech_problem
        return self._readiness.agent_problems.get(action.agent_id)

    def _phrase_problem_locked(self, model_id: str) -> str | None:
        action = self._config.effective_action(model_id, self._server_url)
        if isinstance(action, LiveVoiceAction):
            return None
        return self._command_problem_locked(action)

    def _readiness_stale_locked(self) -> bool:
        checked_at = self._readiness.checked_at
        return checked_at is None or self._clock() - checked_at > READINESS_STALE_SECONDS

    def _request_readiness_locked(self, session: _Session) -> None:
        if session.client is None or session.stop_event.is_set():
            return
        readiness = self._readiness
        if readiness.running:
            readiness.again = True
            return
        readiness.running = True
        thread = threading.Thread(
            target=self._run_readiness,
            args=(session,),
            name="vbot-voice-readiness",
            daemon=True,
        )
        session.threads = [known for known in session.threads if known.is_alive()]
        session.threads.append(thread)
        thread.start()

    def _run_readiness(self, session: _Session) -> None:
        client = session.client
        assert client is not None
        while True:
            with self._lock:
                if session.generation != self._generation:
                    return
                version = self._routing_version
                self._readiness.again = False
                actions = [
                    action
                    for phrase in self._config.phrases
                    if isinstance(
                        action := self._config.effective_action(phrase.model_id, self._server_url),
                        CommandAction,
                    )
                ]
            agent_ids = sorted({action.agent_id for action in actions if action.agent_id})
            try:
                speech_problem = client.speech_readiness() if actions else None
                agent_problems = {
                    agent_id: _agent_problem(client, agent_id) for agent_id in agent_ids
                }
                budget = client.upload_budget_bytes() if actions else None
            except VoiceRequestCancelled:
                return
            except Exception:
                if not session.stop_event.is_set():
                    logger.exception("Voice readiness check failed")
                    with self._lock:
                        if session.generation == self._generation:
                            self._readiness.running = False
                return
            with self._lock:
                if session.generation != self._generation:
                    return
                readiness = self._readiness
                if (speech_problem, agent_problems) != (
                    readiness.speech_problem,
                    readiness.agent_problems,
                ):
                    logger.info(
                        "Voice readiness: speech_to_text=%s, agents=%s",
                        speech_problem or "ready",
                        {agent: problem or "ready" for agent, problem in agent_problems.items()},
                    )
                readiness.speech_problem = speech_problem
                readiness.agent_problems = agent_problems
                readiness.checked_at = self._clock()
                if budget is not None:
                    session.budget_bytes = budget
                again = readiness.again or version != self._routing_version
                readiness.running = again
                self._commit_locked()
            if not again:
                return

    # -- Mock mode -----------------------------------------------------------

    def _run_mock(self, session: _Session, model_id: str) -> None:
        runtime = self._runtime
        engine = MockWakewordEngine(_MOCK_SCORES, model_id=model_id)
        engine.start()
        try:
            while not session.stop_event.wait(runtime.mock_frame_seconds):
                if engine.detect(b"") is not None:
                    self._simulate_mock_command(session, model_id)
        finally:
            engine.stop()

    def _simulate_mock_command(self, session: _Session, model_id: str) -> None:
        dwell = self._runtime.mock_stage_seconds
        with self._lock:
            if session.generation != self._generation or self._recording is not None:
                return
            action = self._config.effective_action(model_id, self._server_url)
            agent_id = action.agent_id if isinstance(action, CommandAction) else None
            recording = _Recording(f"c{next(self._command_ids)}", model_id, agent_id, "active")
            self._emit_locked(EVENT_DETECTED, model_id=model_id)
            self._recording = recording
            self._emit_recording_locked(EVENT_RECORDING_STARTED, recording)
            self._commit_locked()
        steps: list[str | None] = [_STAGE_TRANSCRIBING, _STAGE_SENDING, None]
        for stage in steps:
            if session.stop_event.wait(dwell):
                return
            with self._lock:
                if session.generation != self._generation:
                    return
                if self._recording is recording:
                    self._recording = None
                    self._emit_recording_locked(EVENT_RECORDING_ENDED, recording)
                if stage is not None:
                    self._commands[recording.command_id] = {
                        "command_id": recording.command_id,
                        "model_id": model_id,
                        "stage": stage,
                    }
                elif self._commands.pop(recording.command_id, None) is not None:
                    self._emit_locked(
                        EVENT_SENT,
                        command_id=recording.command_id,
                        model_id=model_id,
                        agent_id=agent_id,
                    )
                self._commit_locked()

    # -- Status and publication (under _lock) ------------------------------------

    def _derive_state_locked(self) -> tuple[str, str | None]:
        if not self._config.enabled:
            return STATE_OFF, None
        if self._fatal_error is not None:
            return STATE_ERROR, self._fatal_error
        if self._capture_state == _CAPTURE_DISCONNECTED:
            return STATE_MICROPHONE_DISCONNECTED, self._capture_error
        if self._listener_ready and (
            self._mode == MODE_MOCK or self._capture_state == _CAPTURE_CAPTURING
        ):
            return STATE_LISTENING, None
        return STATE_STARTING, None

    def _active_calibration_locked(self) -> PhraseCalibration | None:
        calibration = self._calibration
        if calibration is not None and calibration.expired(self._clock()):
            self._calibration = calibration = None
        return calibration

    def _status_locked(self) -> dict[str, Any]:
        config = self._config
        base = config_status(config, self._server_url, self._labels)
        state, error_code = self._derive_state_locked()
        calibration = self._active_calibration_locked()
        phrases = base["phrases"]
        for phrase in phrases:
            phrase["problem"] = self._phrase_problem_locked(phrase["model_id"])
        capture_running = self._capture_state not in (None, _CAPTURE_FAILED)
        recording = self._recording
        return {
            "enabled": base["enabled"],
            "mode": self._mode,
            "state": state,
            "error_code": error_code,
            "sequence": self._sequence,
            "microphone": base["microphone"],
            "active_microphone": (
                dict(self._active_microphone) if self._active_microphone is not None else None
            ),
            "echo_cancellation": {
                "enabled": config.echo_cancellation,
                "state": self._echo_state if capture_running else "off",
            },
            "default_agent_id": base["default_agent_id"],
            "default_session_behavior": base["default_session_behavior"],
            "phrases": phrases,
            "recording": (
                {
                    "command_id": recording.command_id,
                    "model_id": recording.model_id,
                    "agent_id": recording.agent_id,
                }
                if recording is not None
                else None
            ),
            "commands": [dict(entry) for entry in self._commands.values()],
            "calibration": calibration.status(self._clock()) if calibration is not None else None,
            "limits": base["limits"],
        }

    def _commit_locked(self) -> None:
        """Announce a state transition as an event, then push the status."""
        state, error_code = self._derive_state_locked()
        previous_state, previous_error = self._reported_state
        if (state, error_code) != (previous_state, previous_error):
            self._reported_state = (state, error_code)
            logger.info("Voice state: %s%s", state, f" ({error_code})" if error_code else "")
            if state == STATE_MICROPHONE_DISCONNECTED:
                if previous_state != STATE_MICROPHONE_DISCONNECTED:
                    self._emit_locked(EVENT_MICROPHONE_DISCONNECTED, error_code=error_code)
            elif state == STATE_LISTENING and previous_state == STATE_MICROPHONE_DISCONNECTED:
                self._emit_locked(EVENT_MICROPHONE_RECONNECTED)
            elif state == STATE_ERROR:
                self._emit_locked(EVENT_ERROR, error_code=error_code)
        self._push_status_locked()

    def _push_status_locked(self) -> None:
        self._sequence += 1
        status = self._status_locked()
        try:
            self._sink.publish_status(status)
        except Exception:
            logger.exception("Voice status push failed")

    def _emit_recording_locked(self, kind: str, recording: _Recording) -> None:
        self._emit_locked(
            kind,
            command_id=recording.command_id,
            model_id=recording.model_id,
            agent_id=recording.agent_id,
        )

    def _emit_locked(self, kind: str, **fields: Any) -> None:
        self._sequence += 1
        event = {"sequence": self._sequence, "kind": kind}
        event.update({key: value for key, value in fields.items() if value is not None})
        try:
            self._sink.publish_event(event)
        except Exception:
            logger.exception("Voice event push failed")

    # -- Catalog helpers -------------------------------------------------------

    def _known_model_ids(self) -> frozenset[str]:
        with self._catalog_lock:
            models = self._catalog.list_models()
        self._store_labels(models)
        return frozenset(model.id for model in models)

    def _ensure_labels(self, *, force: bool = False) -> None:
        with self._lock:
            if self._labels_loaded and not force:
                return
        try:
            with self._catalog_lock:
                models = self._catalog.list_models()
        except Exception:
            logger.warning("Wakeword model labels could not be read", exc_info=True)
            return
        self._store_labels(models)

    def _store_labels(self, models: Sequence[WakewordModelDescriptor]) -> None:
        with self._lock:
            self._labels = {model.id: model.label for model in models}
            self._labels_loaded = True


def _create_echo_stage() -> Any:
    # Loads the WebRTC library; only reached when echo cancellation is enabled.
    from desktop.wakeword.echo import create_echo_stage

    return create_echo_stage()


def _agent_problem(client: VoiceServerClient, agent_id: str) -> str | None:
    try:
        client.get_agent(agent_id)
    except VoiceServerError as exc:
        return exc.error_code
    return None


def _normalized_url(server_url: str) -> str:
    return (server_url or "").strip().rstrip("/")
