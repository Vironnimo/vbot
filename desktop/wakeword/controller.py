"""Voice configuration, routing and generation admission behind the Desktop bridge.

The controller serializes settings mutations, accepts callbacks only for its
current listener generation and publishes ordered status/events under `_lock`.
The lock never covers network work, joins or PortAudio calls. Listener resources
and lazy stack selection live in `listener`, server checks in `readiness`, and
simulation timing in `_mock`; all return through this owner's admission checks.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from desktop import settings as desktop_settings
from desktop._windows import url_origin
from desktop.speech.cues import (
    CUE_DONE,
    CuePlayer,
)
from desktop.speech.destination import SpeechDestination
from desktop.speech.server_client import (
    ServerAuthorization,
)
from desktop.wakeword import contracts as voice
from desktop.wakeword import listener
from desktop.wakeword.calibration import PhraseCalibration
from desktop.wakeword.config import (
    MAX_ACTIVE_PHRASES,
    CommandAction,
    LiveVoiceAction,
    VoiceConfig,
    apply_voice_changes,
    config_status,
    forget_model,
    parse_voice_config,
    set_enabled,
)
from desktop.wakeword.engine import (
    WakewordModelCatalog,
    WakewordModelDescriptor,
    WakewordModelError,
)
from desktop.wakeword.readiness import ReadinessResult, ReadinessWorker
from desktop.wakeword.server_client import ERROR_TARGET_AGENT_UNAVAILABLE

if TYPE_CHECKING:
    from desktop.speech.capture import CaptureStatus
    from desktop.speech.microphone import MicrophoneService, MicrophoneSettings
    from desktop.wakeword.commands import (
        CommandOutcome,
        RecordingResult,
    )
    from desktop.wakeword.detection import Detection

logger = logging.getLogger("vbot.desktop.wakeword.controller")

_CAPTURE_CAPTURING = "capturing"
_CAPTURE_DISCONNECTED = "disconnected"
_CAPTURE_FAILED = "failed"
_CAPTURE_STOPPED = "stopped"
_STAGE_TRANSCRIBING = "transcribing"


class VoiceController:
    """Voice API of the Desktop facade: config, lifecycle, dispatch, status and events.

    ``live_requests(action, source)`` asks the page for a Live voice call and
    must not block. ``microphone`` is the Desktop's shared microphone: each
    listener captures from its current settings, and a change of them rebuilds
    a running listener. ``stack_available()`` reports whether the on-device stack
    imports; it runs once, lazily, on a background thread when a real
    listener first starts. ``cues`` is the Desktop's shared player (its owner
    closes it); events with a cue play it, and without a player Voice is
    silent. ``authorization`` signs the server requests in
    (``desktop.access.ServerAccess``). The listener starts with :meth:`start`
    (after the window is shown) and ends with :meth:`close`.
    """

    def __init__(
        self,
        *,
        settings_path: Path | None,
        microphone: MicrophoneService,
        server_url: str,
        sink: voice.VoiceEventSink,
        live_requests: Callable[[str, str, str | None], None],
        mock: bool = False,
        stack_available: Callable[[], bool] = lambda: True,
        cues: CuePlayer | None = None,
        catalog: WakewordModelCatalog | None = None,
        runtime: listener.VoiceRuntime | None = None,
        authorization: ServerAuthorization | None = None,
        own_server: Callable[[str], bool] = lambda _url: False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings_path = settings_path
        self._microphone = microphone
        self._sink = sink
        self._live_requests = live_requests
        self._cues = cues
        self._catalog = catalog if catalog is not None else WakewordModelCatalog(settings_path)
        self._runtime = runtime if runtime is not None else listener.VoiceRuntime()
        self._clock = clock

        self._mutation_lock = threading.Lock()
        self._catalog_lock = threading.Lock()
        self._lock = threading.Lock()

        self._destination = SpeechDestination(server_url, own_server)
        self._config = self._read_config()
        self._catalog.adopt_existing_models(self._config.selected_model_ids)
        self._server_url = _normalized_url(server_url)
        self._listeners = listener.ListenerFactory(
            microphone,
            self._catalog,
            self._catalog_lock,
            self._runtime,
            authorization,
            stack_available,
            mock=mock,
        )
        self._started = False
        self._closed = False
        self._generation = 0
        self._session: listener.ListenerSession | None = None
        self._transition_thread: threading.Thread | None = None
        self._sequence = 0
        self._reported_state: tuple[str, str | None] = (voice.STATE_OFF, None)
        self._labels: dict[str, str] = {}
        self._labels_loaded = False
        self._command_ids = itertools.count(1)
        self._model_download: dict[str, Any] | None = None
        self._download_cancelled: threading.Event | None = None
        self._download_pushed_at = 0.0
        # Runtime state of the current generation (see _reset_runtime_locked).
        self._recording: voice._Recording | None = None
        # While set (a Desktop dictation records), detections are ignored.
        self._wake_phrases_paused = False
        self._commands: dict[str, dict[str, Any]] = {}
        self._fatal_error: str | None = None
        self._capture_state: str | None = None
        self._capture_error: str | None = None
        self._active_microphone: dict[str, Any] | None = None
        self._echo_state = "off"
        self._listener_ready = False
        self._readiness = ReadinessResult()
        self._readiness_checked_at: float | None = None
        self._calibration: PhraseCalibration | None = None
        self._calibration_pushed_at = 0.0
        self._pushed_calibration_progress: tuple[str, int] | None = None
        self._reported_state = self._derive_state_locked()
        microphone.add_listener(self._on_microphone_changed)

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
                if self._download_cancelled is not None:
                    self._download_cancelled.set()
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
        with self._mutation_lock:
            if url == self._server_url:
                return
            logger.info("Voice follows the server %s", url or "(none)")
            with self._lock:
                # Retire the old recording and its workers before looking up
                # the new origin's consent. Settings and token reads can block.
                self._config = replace(self._config, enabled=False)
                self._transition_locked()
                self._server_url = url
                self._destination.origin = url_origin(url)
            self._apply_config(self._read_config())

    def _on_microphone_changed(self, _settings: MicrophoneSettings) -> None:
        """A new microphone or echo setting rebuilds a running listener."""
        with self._mutation_lock, self._lock:
            if not self._started or self._closed or not self._config.enabled:
                self._commit_locked()  # the status reports the new echo setting
                return
            logger.info("Voice follows the changed microphone settings")
            self._transition_locked()
            self._commit_locked()

    # -- Status --------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Return the current status snapshot."""
        self._ensure_labels()
        with self._lock:
            return self._status_locked()

    def pause_wake_phrases(self, paused: bool) -> None:
        """Ignore wake phrases while ``paused`` (a Desktop dictation records).

        The listener keeps running, so wake phrases work again right away; a
        command recording that already runs is not affected.
        """
        with self._lock:
            if self._wake_phrases_paused == paused:
                return
            self._wake_phrases_paused = paused
        logger.info("Wake phrases %s", "paused for a dictation" if paused else "resumed")

    def is_busy(self) -> bool:
        """Whether a command recording or a calibration is running right now."""
        with self._lock:
            return self._recording is not None or self._active_calibration_locked() is not None

    # -- Config --------------------------------------------------------------

    def _read_config(self) -> VoiceConfig:
        section = desktop_settings.read_section(desktop_settings.WAKEWORD_KEY, self._settings_path)
        return self._parse_config(section)

    def _parse_config(self, section: dict[str, Any]) -> VoiceConfig:
        return parse_voice_config({**section, "enabled": self._destination.enabled(section)})

    def set_enabled(self, enabled: object) -> dict[str, Any]:
        """Persist and apply the enable switch; returns ``{enabled, error_code}``.

        Readiness problems are reported through the status, never by refusing
        the switch, so ``error_code`` is always ``None``.
        """
        with self._mutation_lock:

            def change(raw: dict[str, Any]) -> dict[str, Any]:
                validated = set_enabled({}, enabled)["enabled"]
                self._destination.set_enabled(raw, validated)
                return raw

            section = desktop_settings.update_section(
                desktop_settings.WAKEWORD_KEY,
                change,
                self._settings_path,
            )
            config = self._parse_config(section)
            logger.info("Voice %s", "enabled" if config.enabled else "disabled")
            self._apply_config(config)
        return {"enabled": config.enabled, "error_code": None}

    def update_config(self, changes: object) -> dict[str, Any]:
        """Validate, persist and apply one partial config change; returns the status."""
        with self._mutation_lock:
            known_models = self._refresh_labels()
            with self._lock:
                server_url = self._server_url
            section = desktop_settings.update_section(
                desktop_settings.WAKEWORD_KEY,
                lambda raw: apply_voice_changes(
                    raw,
                    changes,
                    server_url=server_url,
                    known_model_ids=known_models.__contains__,
                ),
                self._settings_path,
            )
            self._apply_config(self._parse_config(section))
        return self.status()

    def retry(self) -> dict[str, Any]:
        """Rebuild the listener from the stored config; returns the status.

        The PortAudio device list is refreshed between the old listener's
        shutdown and the new one's start, so a device connected or a default
        input changed since then is used.
        """
        with self._mutation_lock:
            config = self._read_config()
            with self._lock:
                logger.info("Voice listener retry requested")
                self._listeners.retry()
                self._config = config
                self._transition_locked(refresh_devices=True)
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

    # -- Models --------------------------------------------------

    def list_models(self) -> list[dict[str, Any]]:
        """Return the downloadable catalog and the imported models."""
        with self._catalog_lock:
            models = self._catalog.list_models()
            self._store_labels({model.id: model.label for model in models})
        return [model.to_dict() for model in models]

    def import_model(self, filename: str, content: bytes) -> dict[str, Any]:
        """Install one model; activates it while fewer than the maximum phrases are active."""
        with self._catalog_lock:
            descriptor = self._catalog.import_model(filename, content)
        return self._select_model(descriptor)

    def model_label(self, model_id: object) -> str:
        """Validate a catalog selection before native page consent."""
        return self._resolve_model(model_id).label

    def _resolve_model(self, model_id: object) -> WakewordModelDescriptor:
        if not isinstance(model_id, str):
            raise WakewordModelError("Wakeword model id must be text")
        with self._catalog_lock:
            return self._catalog.resolve(model_id)

    def add_model(self, model_id: object) -> dict[str, Any]:
        """Install and select a catalog phrase; failed downloads never select it."""
        descriptor = self._resolve_model(model_id)
        model_id = descriptor.id
        cancelled = threading.Event()
        with self._lock:
            if self._model_download is not None or self._closed:
                raise WakewordModelError("A model install cannot start now")
            self._download_cancelled = cancelled
            self._model_download = {
                "model_id": model_id,
                "completed": 0,
                "total": descriptor.size_bytes or 0,
            }
            self._download_pushed_at = self._clock()
            self._commit_locked()

        def report(completed: int) -> None:
            with self._lock:
                assert self._model_download is not None
                self._model_download["completed"] = completed
                now = self._clock()
                if (
                    now - self._download_pushed_at >= 0.2
                    or completed == self._model_download["total"]
                ):
                    self._download_pushed_at = now
                    self._commit_locked()

        try:
            descriptor = self._catalog.install_model(model_id, progress=report, cancelled=cancelled)
            result = self._select_model(descriptor, cancelled=cancelled)
            with self._lock:
                retry = (
                    self._config.enabled
                    and model_id in self._config.active_model_ids
                    and self._fatal_error is not None
                )
            if retry:
                self.retry()
            return result
        finally:
            with self._lock:
                self._model_download = None
                self._download_cancelled = None
                self._commit_locked()

    def cancel_model_download(self) -> dict[str, bool]:
        """Request cancellation; the in-flight addition settles before its selection changes."""
        with self._mutation_lock, self._lock:
            if self._download_cancelled is not None:
                self._download_cancelled.set()
        return {"cancelled": True}

    def _select_model(
        self, descriptor: WakewordModelDescriptor, *, cancelled: threading.Event | None = None
    ) -> dict[str, Any]:
        activated = False
        with self._mutation_lock:
            if cancelled is not None and cancelled.is_set():
                raise WakewordModelError(
                    "Wakeword download cancelled", error_code="wakeword_model_download_cancelled"
                )
            known_models = self._refresh_labels()
            with self._lock:
                server_url = self._server_url

            def select(raw: dict[str, Any]) -> dict[str, Any]:
                nonlocal activated
                config = parse_voice_config(raw)
                active = list(config.active_model_ids)
                if (
                    descriptor.id not in config.selected_model_ids
                    and len(active) < MAX_ACTIVE_PHRASES
                ):
                    active.append(descriptor.id)
                activated = descriptor.id in active
                return apply_voice_changes(
                    raw,
                    {
                        "active_model_ids": active,
                        "selected_model_ids": list(
                            dict.fromkeys([*config.selected_model_ids, descriptor.id])
                        ),
                    },
                    server_url=server_url,
                    known_model_ids=known_models.__contains__,
                )

            section = desktop_settings.update_section(
                desktop_settings.WAKEWORD_KEY, select, self._settings_path
            )
            logger.info("Wakeword selected (model=%s activated=%s)", descriptor.id, activated)
            self._apply_config(self._parse_config(section))
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
                    error_code=voice.ERROR_WAKEWORD_MODEL_ACTIVE,
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
            self._apply_config(self._parse_config(section))
        return {"deleted": True}

    # -- Calibration ---------------------------------------------------------

    def start_calibration(self, model_id: object) -> dict[str, Any]:
        """Calibrate one active phrase; commands pause while it runs. Returns the status."""
        self._ensure_labels()
        with self._lock:
            state, _ = self._derive_state_locked()
            if (
                self._listeners.mode != voice.MODE_REAL
                or state != voice.STATE_LISTENING
                or not isinstance(model_id, str)
                or self._config.phrase(model_id) is None
                or self._recording is not None
                or self._wake_phrases_paused
            ):
                raise voice.VoiceControlError(
                    "Calibration needs Voice listening with this wake phrase active.",
                    error_code=voice.ERROR_CALIBRATION_UNAVAILABLE,
                )
            now = self._clock()
            self._calibration = PhraseCalibration(model_id, now=now)
            logger.info("Voice calibration started (model=%s)", model_id)
            self._commit_locked()
            return self._status_locked()

    def restart_calibration(self) -> dict[str, Any]:
        """Discard the measurements and start again from room noise. Returns the status."""
        self._ensure_labels()
        with self._lock:
            calibration = self._active_calibration_locked()
            if calibration is None:
                raise voice.VoiceControlError(
                    "No calibration is running.", error_code=voice.ERROR_CALIBRATION_INACTIVE
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
                if self._session is not None:
                    self._request_readiness_locked(self._session)
            self._commit_locked()

    def _transition_locked(
        self, *, refresh_devices: bool = False, fatal_error: str | None = None
    ) -> None:
        """Stop the running session and start the next one on a background thread.

        ``refresh_devices`` refreshes the PortAudio device list once the old
        session released its microphone, before the next one opens it.
        """
        self._generation += 1
        generation = self._generation
        session, self._session = self._session, None
        if session is not None:
            session.signal()
        self._reset_runtime_locked()
        self._fatal_error = fatal_error
        should_run = (
            fatal_error is None
            and self._started
            and not self._closed
            and self._config.enabled
            and bool(self._config.phrases)
        )
        if session is None and not should_run:
            return
        previous = self._transition_thread
        thread = threading.Thread(
            target=self._run_transition,
            args=(generation, previous, session, should_run, refresh_devices),
            name="vbot-voice-start" if should_run else "vbot-voice-stop",
            daemon=True,
        )
        self._transition_thread = thread
        thread.start()

    def _end_recording_locked(self) -> None:
        if self._recording is not None:
            self._emit_recording_locked(voice.EVENT_RECORDING_ENDED, self._recording)
            self._recording = None

    def _reset_runtime_locked(self) -> None:
        """Forget the runtime state of a stopped session; a recording ends for the page."""
        self._end_recording_locked()
        self._commands = {}
        self._capture_state = None
        self._capture_error = None
        self._active_microphone = None
        self._echo_state = "off"
        self._listener_ready = False
        self._readiness = ReadinessResult()
        self._readiness_checked_at = None
        self._calibration = None

    def _run_transition(
        self,
        generation: int,
        previous: threading.Thread | None,
        session: listener.ListenerSession | None,
        should_run: bool,
        refresh_devices: bool,
    ) -> None:
        timeout = self._runtime.join_timeout
        try:
            if previous is not None:
                previous.join(timeout)
            if session is not None:
                session.shutdown(timeout)
            if should_run:
                self._build_session(generation, refresh_devices=refresh_devices)
        except Exception:
            logger.exception("Voice listener could not start")
            self._on_listener_failed(generation, voice.ERROR_PIPELINE_FAILED)

    def _build_session(self, generation: int, *, refresh_devices: bool = False) -> None:
        with self._lock:
            if generation != self._generation:
                return
            config = self._config
            server_url = self._server_url
        self._ensure_labels()
        try:
            session = self._listeners.build(
                generation, config, server_url, self, refresh_devices=refresh_devices
            )
        except listener.ListenerStartError as exc:
            self._on_listener_failed(generation, exc.error_code)
            return
        with self._lock:
            if generation == self._generation:
                self._session = session
                if session.client is None:
                    self._listener_ready = True
                    logger.info("Voice mock listener started")
                    self._commit_locked()
                    session.start_mock(config.active_model_ids[0], self._runtime, self)
                else:
                    assert session.capture is not None and session.detection is not None
                    session.capture.start()
                    session.detection.start()
                    self._request_readiness_locked(session)
                    logger.info(
                        "Voice listener starting (phrases=%s, server=%s)",
                        ",".join(config.active_model_ids),
                        server_url,
                    )
                return
        session.shutdown(self._runtime.join_timeout)

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
                self._transition_locked(
                    fatal_error=status.error_code or voice.ERROR_PIPELINE_FAILED
                )
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
            self._transition_locked(fatal_error=error_code)
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
            elif (
                calibration.progress != self._pushed_calibration_progress
                or now - self._calibration_pushed_at >= voice.CALIBRATION_PUSH_INTERVAL_SECONDS
            ):
                self._push_status_locked()

    def _on_detection(self, generation: int, detection: Detection) -> None:
        live_mode: str | None = None
        recording: voice._Recording | None = None
        with self._lock:
            session = self._session
            if (
                generation != self._generation
                or session is None
                or session.stop_event.is_set()
                or self._calibration is not None
            ):
                return
            model_id = detection.model_id
            if self._wake_phrases_paused:
                logger.info("Wake phrase ignored during a dictation (model=%s)", model_id)
                return
            action = self._config.effective_action(model_id, self._server_url)
            if isinstance(action, LiveVoiceAction):
                self._emit_locked(voice.EVENT_DETECTED, model_id=model_id)
                self._emit_locked(voice.EVENT_LIVE_REQUESTED, model_id=model_id)
                live_mode = action.mode
                live_origin = self._destination.origin
            elif self._recording is not None:
                logger.info("Wake phrase ignored during a command recording (model=%s)", model_id)
                return
            else:
                self._emit_locked(voice.EVENT_DETECTED, model_id=model_id)
                problem = self._command_problem_locked(action)
                if problem is not None:
                    logger.info(
                        "Voice command not recorded (model=%s, problem=%s)", model_id, problem
                    )
                    self._emit_locked(
                        voice.EVENT_COMMAND_FAILED,
                        model_id=model_id,
                        agent_id=action.agent_id,
                        error_code=problem,
                    )
                    if problem != voice.ERROR_MISSING_TARGET_AGENT:
                        self._request_readiness_locked(session)
                    self._commit_locked()
                    return
                if self._readiness_stale_locked():
                    self._request_readiness_locked(session)
                recording = voice._Recording(
                    command_id=f"c{next(self._command_ids)}",
                    model_id=model_id,
                    agent_id=action.agent_id,
                    session_behavior=action.session_behavior or "active",
                )
                self._recording = recording
                self._emit_recording_locked(voice.EVENT_RECORDING_STARTED, recording)
            self._commit_locked()
        if live_mode is not None:
            self._live_requests(live_mode, voice.LIVE_REQUEST_SOURCE, live_origin)
        elif recording is not None:
            self._start_recording(generation, session, recording, detection)

    def _start_recording(
        self,
        generation: int,
        session: listener.ListenerSession,
        recording: voice._Recording,
        detection: Detection,
    ) -> None:
        from desktop.wakeword.commands import (
            OUTCOME_FAILED,
            SUBSCRIPTION_SECONDS,
            RecordingResult,
        )

        subscription = None
        try:
            assert session.capture is not None and session.recorder is not None
            session.prepare_transcription()
            after_index = detection.pre_roll[-1].index if detection.pre_roll else None
            subscription = session.capture.subscribe(
                max_seconds=SUBSCRIPTION_SECONDS, after_index=after_index
            )
            if session.recorder.start(
                detection.pre_roll,
                subscription,
                lambda result: self._on_recording_done(generation, recording, result),
            ):
                return
        except Exception:
            logger.exception("Voice command recording could not start")
        if subscription is not None:
            subscription.close()
        self._on_recording_done(
            generation,
            recording,
            RecordingResult(OUTCOME_FAILED, error_code=voice.ERROR_PIPELINE_FAILED),
        )

    def _on_recording_done(
        self, generation: int, recording: voice._Recording, result: RecordingResult
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
            self._emit_recording_locked(
                voice.EVENT_RECORDING_ENDED, recording, completed=result.outcome == OUTCOME_AUDIO
            )
            if result.outcome == OUTCOME_NO_SPEECH:
                self._emit_locked(
                    voice.EVENT_NO_SPEECH,
                    command_id=recording.command_id,
                    model_id=recording.model_id,
                )
            elif result.outcome == OUTCOME_FAILED:
                self._emit_locked(
                    voice.EVENT_COMMAND_FAILED,
                    command_id=recording.command_id,
                    model_id=recording.model_id,
                    agent_id=recording.agent_id,
                    error_code=result.error_code or voice.ERROR_PIPELINE_FAILED,
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
            # Submit before another detection can start its recording: the
            # pipeline's per-Agent order must match the recordings' order.
            if (
                command is not None
                and session is not None
                and session.pipeline is not None
                and not session.pipeline.submit(command)
            ):
                self._commands.pop(command.command_id, None)
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
            result = outcome.wakeword_result
            self._emit_locked(
                outcome.kind,
                command_id=outcome.command_id,
                model_id=outcome.model_id,
                agent_id=outcome.agent_id,
                session_id=outcome.session_id,
                error_code=outcome.error_code,
                message=result.message if result else None,
                notification_failed=True if result and result.notification_failed else None,
            )
            if outcome.error_code == ERROR_TARGET_AGENT_UNAVAILABLE and outcome.agent_id:
                self._readiness.agent_problems[outcome.agent_id] = outcome.error_code
            self._commit_locked()

    # -- Readiness -----------------------------------------------------------

    def _command_problem_locked(self, action: CommandAction) -> str | None:
        if action.agent_id is None:
            return voice.ERROR_MISSING_TARGET_AGENT
        if self._readiness.speech_problem is not None:
            return self._readiness.speech_problem
        return self._readiness.agent_problems.get(action.agent_id)

    def _phrase_problem_locked(self, model_id: str) -> str | None:
        action = self._config.effective_action(model_id, self._server_url)
        if isinstance(action, LiveVoiceAction):
            return None
        return self._command_problem_locked(action)

    def _readiness_stale_locked(self) -> bool:
        checked_at = self._readiness_checked_at
        return checked_at is None or self._clock() - checked_at > voice.READINESS_STALE_SECONDS

    def _request_readiness_locked(self, session: listener.ListenerSession) -> None:
        if session.client is None or session.stop_event.is_set():
            return
        if session.readiness is None:
            session.readiness = ReadinessWorker(
                session.client,
                session.stop_event,
                lambda result: self._on_readiness(session, result),
                recheck_seconds=self._runtime.readiness_recheck_seconds,
            )
        actions = [
            action
            for phrase in self._config.phrases
            if isinstance(
                action := self._config.effective_action(phrase.model_id, self._server_url),
                CommandAction,
            )
        ]
        session.readiness.request(actions)

    def _on_readiness(self, session: listener.ListenerSession, result: ReadinessResult) -> None:
        with self._lock:
            if session.generation != self._generation:
                return
            if (result.speech_problem, result.agent_problems) != (
                self._readiness.speech_problem,
                self._readiness.agent_problems,
            ):
                logger.info(
                    "Voice readiness: speech_to_text=%s, agents=%s",
                    result.speech_problem or "ready",
                    {agent: problem or "ready" for agent, problem in result.agent_problems.items()},
                )
            self._readiness = result
            self._readiness_checked_at = self._clock()
            if result.budget_bytes is not None:
                session.budget_bytes = result.budget_bytes
            self._commit_locked()

    # -- Mock mode -----------------------------------------------------------

    def _begin_mock(
        self, session: listener.ListenerSession, model_id: str
    ) -> voice._Recording | None:
        with self._lock:
            if session.generation != self._generation or self._recording is not None:
                return None
            action = self._config.effective_action(model_id, self._server_url)
            agent_id = action.agent_id if isinstance(action, CommandAction) else None
            recording = voice._Recording(
                f"c{next(self._command_ids)}", model_id, agent_id, "active"
            )
            self._emit_locked(voice.EVENT_DETECTED, model_id=model_id)
            self._recording = recording
            self._emit_recording_locked(voice.EVENT_RECORDING_STARTED, recording)
            self._commit_locked()
            return recording

    def _advance_mock(
        self, session: listener.ListenerSession, recording: voice._Recording, stage: str | None
    ) -> bool:
        with self._lock:
            if session.generation != self._generation:
                return False
            if self._recording is recording:
                self._recording = None
                self._emit_recording_locked(voice.EVENT_RECORDING_ENDED, recording, completed=True)
            if stage is not None:
                self._commands[recording.command_id] = {
                    "command_id": recording.command_id,
                    "model_id": recording.model_id,
                    "stage": stage,
                }
            elif self._commands.pop(recording.command_id, None) is not None:
                self._emit_locked(
                    voice.EVENT_SENT,
                    command_id=recording.command_id,
                    model_id=recording.model_id,
                    agent_id=recording.agent_id,
                )
            self._commit_locked()
            return True

    # -- Status and publication (under _lock) ------------------------------------

    def _derive_state_locked(self) -> tuple[str, str | None]:
        if not self._config.enabled:
            return voice.STATE_OFF, None
        if not self._config.phrases:
            return voice.STATE_IDLE, None
        if self._fatal_error is not None:
            return voice.STATE_ERROR, self._fatal_error
        if self._capture_state == _CAPTURE_DISCONNECTED:
            return voice.STATE_MICROPHONE_DISCONNECTED, self._capture_error
        if self._listener_ready and (
            self._listeners.mode == voice.MODE_MOCK or self._capture_state == _CAPTURE_CAPTURING
        ):
            return voice.STATE_LISTENING, None
        return voice.STATE_STARTING, None

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
            "mode": self._listeners.mode,
            "state": state,
            "error_code": error_code,
            "sequence": self._sequence,
            "active_microphone": (
                dict(self._active_microphone) if self._active_microphone is not None else None
            ),
            "echo_cancellation": {
                "enabled": self._microphone.settings.echo_cancellation,
                "state": self._echo_state if capture_running else "off",
            },
            "default_agent_id": base["default_agent_id"],
            "default_session_behavior": base["default_session_behavior"],
            "phrases": phrases,
            "selected_phrases": base["selected_phrases"],
            "model_download": dict(self._model_download)
            if self._model_download is not None
            else None,
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
            if state == voice.STATE_MICROPHONE_DISCONNECTED:
                if previous_state != voice.STATE_MICROPHONE_DISCONNECTED:
                    self._emit_locked(voice.EVENT_MICROPHONE_DISCONNECTED, error_code=error_code)
            elif (
                state == voice.STATE_LISTENING
                and previous_state == voice.STATE_MICROPHONE_DISCONNECTED
            ):
                self._emit_locked(voice.EVENT_MICROPHONE_RECONNECTED)
            elif state == voice.STATE_ERROR:
                self._emit_locked(voice.EVENT_ERROR, error_code=error_code)
        self._push_status_locked()

    def _push_status_locked(self) -> None:
        self._sequence += 1
        status = self._status_locked()
        if self._calibration is not None:
            self._pushed_calibration_progress = self._calibration.progress
            self._calibration_pushed_at = self._clock()
        try:
            self._sink.publish_status(status, origin=self._destination.origin)
        except Exception:
            logger.exception("Voice status push failed")

    def _emit_recording_locked(
        self, kind: str, recording: voice._Recording, *, completed: bool = False
    ) -> None:
        self._emit_locked(
            kind,
            command_id=recording.command_id,
            model_id=recording.model_id,
            agent_id=recording.agent_id,
        )
        if completed and self._cues is not None:
            self._cues.play(CUE_DONE)

    def _emit_locked(self, kind: str, **fields: Any) -> None:
        self._sequence += 1
        event = {"sequence": self._sequence, "kind": kind}
        event.update({key: value for key, value in fields.items() if value is not None})
        try:
            self._sink.publish_event(event, origin=self._destination.origin)
        except Exception:
            logger.exception("Voice event push failed")
        cue = voice._EVENT_CUES.get(kind)
        if cue is not None and self._cues is not None:
            self._cues.play(cue)

    # -- Catalog helpers -------------------------------------------------------

    def _refresh_labels(self) -> dict[str, str]:
        with self._catalog_lock:
            labels = self._catalog.model_labels()
            self._store_labels(labels)
            return labels

    def _ensure_labels(self, *, force: bool = False) -> None:
        with self._lock:
            if self._labels_loaded and not force:
                return
        try:
            self._refresh_labels()
        except Exception:
            logger.warning("Wakeword model labels could not be read", exc_info=True)

    def _store_labels(self, labels: dict[str, str]) -> None:
        with self._lock:
            self._labels = labels
            self._labels_loaded = True


def _normalized_url(server_url: str) -> str:
    return (server_url or "").strip().rstrip("/")
