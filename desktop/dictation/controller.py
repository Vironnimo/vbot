"""Desktop dictation: record while the user speaks, then type the raw transcript.

The global shortcut (:data:`DICTATION_HOTKEY`, stored with the ``mode`` in the
``dictation`` settings section) works in any app while the Desktop runs:

- ``toggle`` mode: a press starts a recording, the next press ends it;
- ``hold`` mode: the recording runs while the combination is held; letting go
  of any of its keys ends it.

Escape cancels a recording or a running transcription. Each dictation (a
*take*) runs on its own thread (``vbot-dictation``): it opens its own capture of
the shared microphone (:class:`~desktop.speech.microphone.MicrophoneService`),
checks the server's speech-to-text in parallel (``vbot-dictation-prepare``),
uploads the recording to the server the window shows, and hands the stripped
transcript to the :class:`~desktop.dictation.insertion.TextInserter`. Cues of
the Desktop's :class:`~desktop.speech.cues.CuePlayer` mark the start (as soon as
the microphone delivers audio), the end, a cancel and a failure; audio during
the loud start of the listen cue is discarded so the cue is not transcribed.

A take has no length limit: it records until the user ends it. Long takes are
split into pieces at speech pauses (:mod:`desktop.dictation.pieces`), which
keeps every upload within the server's limit; each piece is transcribed on
``vbot-dictation-transcribe`` while the user keeps speaking, and the texts are
joined and inserted once at the end. A piece that cannot be transcribed ends
the take at once, so the user does not keep talking into nothing.

Only one take runs at a time; a press while one is transcribing is ignored. A
take that ends too short (under :data:`MIN_RECORDING_SECONDS`, for example a
tap in hold mode) is dropped like a cancel. A take keeps what it recorded when
the microphone fails midway. The status keeps the latest failure
(``last_failure``) so the settings can explain a failure cue afterwards.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from desktop import settings as desktop_settings
from desktop.dictation.insertion import (
    INSERT_CLIPBOARD,
    INSERT_PASTED,
    ClipboardTextInserter,
    TextInserter,
)
from desktop.dictation.pieces import PIECE_SEARCH_SECONDS, PIECE_TARGET_SECONDS, PieceCutter
from desktop.hotkey import (
    HOTKEY_ERROR_INVALID,
    HotkeyApi,
    HotkeyController,
    HotkeyHandlers,
    HotkeyPreference,
)
from desktop.speech.cues import (
    CUE_CANCEL,
    CUE_DONE,
    CUE_FAILED,
    CUE_LISTEN,
    LISTEN_LOUD_SECONDS,
    CuePlayer,
)
from desktop.speech.microphone import MicrophoneService
from desktop.speech.server_client import (
    DEFAULT_UPLOAD_BUDGET_BYTES,
    ERROR_SERVER_UNREACHABLE,
    ERROR_SPEECH_TO_TEXT_UNAVAILABLE,
    ERROR_SPEECH_TO_TEXT_UNCONFIGURED,
    SpeechRequestCancelled,
    SpeechServerClient,
    SpeechServerError,
)

logger = logging.getLogger("vbot.desktop.dictation")

DICTATION_HOTKEY = HotkeyPreference(
    section=desktop_settings.DICTATION_KEY,
    defaults={
        "enabled": False,
        "ctrl": True,
        "alt": True,
        "shift": False,
        "win": False,
        "key": "KeyD",
    },
    label="Dictation hotkey",
    thread_name="vbot-dictation-hotkey",
)

MODE_TOGGLE = "toggle"
MODE_HOLD = "hold"
MODES = frozenset({MODE_TOGGLE, MODE_HOLD})

STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_TRANSCRIBING = "transcribing"

ERROR_DICTATION_CONFIG_INVALID = "dictation_config_invalid"
# ``last_failure`` codes besides the speech server's and the microphone's.
ERROR_INSERT_FAILED = "insert_failed"
ERROR_DICTATION_FAILED = "dictation_failed"
NOTICE_INSERTED_TO_CLIPBOARD = "inserted_to_clipboard"
NOTICE_NOTHING_HEARD = "nothing_heard"

MIN_RECORDING_SECONDS = 0.3
# Recording continues this long after the end request, so the last word is not
# clipped when the shortcut comes right at its end.
TAIL_SECONDS = 0.2
# Audio this long after the microphone opened is dropped: the listen cue is loud
# then. It is shorter than anyone takes to start speaking after hearing the cue.
START_SKIP_SECONDS = LISTEN_LOUD_SECONDS + 0.05

_HOTKEY_FIELDS = ("enabled", "ctrl", "alt", "shift", "win", "key")
_MODE_KEY = "mode"
_SUBSCRIPTION_SECONDS = 30.0
_READ_TIMEOUT_SECONDS = 0.05
_CAPTURE_JOIN_SECONDS = 2.0
_STOP_JOIN_SECONDS = 2.0
_SERVER_NOT_READY = frozenset(
    {ERROR_SERVER_UNREACHABLE, ERROR_SPEECH_TO_TEXT_UNCONFIGURED, ERROR_SPEECH_TO_TEXT_UNAVAILABLE}
)


class DictationPage(Protocol):
    """Where the page learns whether a dictation records (to hold a Live voice call)."""

    def publish_dictation(self, recording: bool) -> None: ...


class WakePhrases(Protocol):
    """Desktop Voice, which must not act on wake phrases spoken into a dictation."""

    def pause_wake_phrases(self, paused: bool) -> None: ...


SpeechClientFactory = Callable[[str, threading.Event], SpeechServerClient]


class _Cancelled(Exception):  # noqa: N818 - a signal, not a failure
    """The user cancelled, or the take was too short to keep."""


class _Failed(Exception):  # noqa: N818 - names the outcome
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Take:
    """One dictation from the start press to the inserted text."""

    def __init__(self, mode: str, target_window: int, server_url: str) -> None:
        self.mode = mode
        self.target_window = target_window
        self.server_url = server_url
        self.finish_requested = threading.Event()
        self.cancelled = threading.Event()
        self.server_problem: str | None = None  # set by the preparation thread
        self.budget_bytes: int | None = None
        self.thread: threading.Thread | None = None

    def cancel(self) -> bool:
        """Cancel once; ``True`` for the first request."""
        if self.cancelled.is_set():
            return False
        self.cancelled.set()
        return True


class DictationController:
    """Desktop dictation preference, its global shortcut, and the running take.

    :meth:`start` (after the window is shown) and :meth:`stop` (on exit; final)
    bound the shortcut's registration. Every public method is thread-safe. The
    shortcut handlers run on the hotkey thread and only flag the running take.
    ``cues`` is the Desktop's shared player (its owner closes it); without one
    the takes are silent.
    """

    def __init__(
        self,
        *,
        settings_path: Path | None,
        microphone: MicrophoneService,
        server_url: str,
        page: DictationPage,
        wake_phrases: WakePhrases | None = None,
        cues: CuePlayer | None = None,
        inserter: TextInserter | None = None,
        client_factory: SpeechClientFactory | None = None,
        hotkey_supported: bool | None = None,
        hotkey_api_factory: Callable[[], HotkeyApi] | None = None,
        clock: Callable[[], float] = time.monotonic,
        piece_target_seconds: float = PIECE_TARGET_SECONDS,
        piece_search_seconds: float = PIECE_SEARCH_SECONDS,
    ) -> None:
        self._settings_path = settings_path
        self._microphone = microphone
        self._page = page
        self._wake_phrases = wake_phrases
        self._cues = cues
        self._inserter_instance = inserter
        self._client_factory = client_factory or _create_client
        self._clock = clock
        self._piece_target_seconds = piece_target_seconds
        self._piece_search_seconds = piece_search_seconds
        self._hotkey = HotkeyController(
            preference=DICTATION_HOTKEY,
            settings_path=settings_path,
            handlers=HotkeyHandlers(
                on_press=self._on_press,
                on_release=self._on_release,
                on_escape=self._on_escape,
            ),
            supported=hotkey_supported,
            api_factory=hotkey_api_factory,
        )
        self._lock = threading.Lock()
        self._mutation_lock = threading.Lock()
        self._server_url = server_url.strip()
        self._running = False
        self._stopped = False
        self._take: _Take | None = None
        self._state = STATE_IDLE
        self._last_failure: dict[str, str] | None = None

    @property
    def supported(self) -> bool:
        """Whether this platform can register the global shortcut."""
        return self._hotkey.supported

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Register the saved shortcut when enabled."""
        with self._lock:
            if self._running or self._stopped:
                return
            self._running = True
        self._hotkey.start()

    def stop(self) -> None:
        """Unregister the shortcut and cancel a running take for good."""
        with self._lock:
            self._running = False
            self._stopped = True
            take = self._take
        self._hotkey.stop()
        if take is not None:
            take.cancel()
            if take.thread is not None:
                take.thread.join(_STOP_JOIN_SECONDS)

    def set_server_url(self, server_url: str) -> None:
        """Follow the window's server; the next take transcribes there."""
        with self._lock:
            self._server_url = server_url.strip()

    def is_busy(self) -> bool:
        """Whether a take records or transcribes right now."""
        with self._lock:
            return self._take is not None

    # -- Settings ------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Return ``{supported, enabled, hotkey, mode, error_code, state, last_failure}``."""
        return self._compose(self._hotkey.status())

    def update(self, changes: object) -> dict[str, Any]:
        """Apply a partial change of the shortcut (``enabled``, ``ctrl``, ``alt``,
        ``shift``, ``win``, ``key``) and ``mode``; returns the status.

        An invalid combination reports ``hotkey_invalid``, an invalid mode or a
        non-object ``dictation_config_invalid``; neither persists anything.
        """
        if not isinstance(changes, Mapping) or (
            _MODE_KEY in changes and changes[_MODE_KEY] not in MODES
        ):
            return self._compose(self._hotkey.status(), ERROR_DICTATION_CONFIG_INVALID)
        hotkey_changes = {name: changes[name] for name in _HOTKEY_FIELDS if name in changes}
        with self._mutation_lock:
            hotkey_status = (
                self._hotkey.update(hotkey_changes) if hotkey_changes else self._hotkey.status()
            )
            if hotkey_status["error_code"] == HOTKEY_ERROR_INVALID:
                return self._compose(hotkey_status)
            if _MODE_KEY in changes:
                mode = changes[_MODE_KEY]
                desktop_settings.update_section(
                    desktop_settings.DICTATION_KEY,
                    lambda section: {**section, _MODE_KEY: mode},
                    self._settings_path,
                )
                logger.info("Dictation mode set to %s", mode)
        return self._compose(hotkey_status)

    def _read_mode(self) -> str:
        section = desktop_settings.read_section(desktop_settings.DICTATION_KEY, self._settings_path)
        mode = section.get(_MODE_KEY)
        return mode if mode in MODES else MODE_TOGGLE

    def _compose(
        self, hotkey_status: Mapping[str, Any], error_code: str | None = None
    ) -> dict[str, Any]:
        with self._lock:
            state = self._state
            last_failure = dict(self._last_failure) if self._last_failure else None
        return {
            **hotkey_status,
            "mode": self._read_mode(),
            "error_code": error_code or hotkey_status["error_code"],
            "state": state,
            "last_failure": last_failure,
        }

    # -- Shortcut handlers (hotkey thread) -----------------------------------

    def _on_press(self) -> None:
        with self._lock:
            if not self._running:
                return
            take = self._take
            if take is None:
                self._begin_locked()
                return
        if take.mode == MODE_TOGGLE:
            take.finish_requested.set()

    def _on_release(self) -> None:
        with self._lock:
            take = self._take
        if take is not None and take.mode == MODE_HOLD:
            take.finish_requested.set()

    def _on_escape(self) -> None:
        with self._lock:
            take = self._take
        if take is not None and take.cancel():
            logger.info("Dictation cancelled")
            self._play(CUE_CANCEL)

    def _begin_locked(self) -> None:
        mode = self._read_mode()
        take = _Take(mode, self._inserter().foreground_window(), self._server_url)
        take.thread = threading.Thread(
            target=self._run_take, args=(take,), name="vbot-dictation", daemon=True
        )
        self._take = take
        self._state = STATE_RECORDING
        logger.info("Dictation started (mode=%s)", mode)
        take.thread.start()

    # -- Take (take thread) --------------------------------------------------

    def _run_take(self, take: _Take) -> None:
        self._hotkey.arm_escape(True)
        failure: str | None = None
        try:
            self._dictate(take)
        except _Cancelled:
            pass
        except _Failed as exc:
            failure = exc.code
        except Exception:
            logger.exception("Dictation failed")
            failure = ERROR_DICTATION_FAILED
        finally:
            self._hotkey.arm_escape(False)
            self._announce_recording(False)
            if failure is not None:
                logger.warning("Dictation ended without inserting text: %s", failure)
                self._play(CUE_FAILED)
            with self._lock:
                self._take = None
                self._state = STATE_IDLE
                if failure is not None:
                    self._last_failure = {
                        "code": failure,
                        "at": datetime.now(UTC).isoformat(timespec="seconds"),
                    }

    def _dictate(self, take: _Take) -> None:
        if not take.server_url:
            raise _Failed(ERROR_SERVER_UNREACHABLE)
        client = self._client_factory(take.server_url, take.cancelled)
        try:
            preparation = threading.Thread(
                target=_prepare_server,
                args=(take, client),
                name="vbot-dictation-prepare",
                daemon=True,
            )
            preparation.start()
            transcriber = _Transcriber(take, client, preparation)
            try:
                self._announce_recording(True)
                try:
                    self._record(take, transcriber)
                finally:
                    self._announce_recording(False)
                with self._lock:
                    self._state = STATE_TRANSCRIBING
                self._play(CUE_DONE)
                text = transcriber.finish()
            finally:
                transcriber.stop()
        finally:
            client.close()
        if take.cancelled.is_set():
            raise _Cancelled
        self._insert(take, text)

    def _play(self, cue: str) -> None:
        if self._cues is not None:
            self._cues.play(cue)

    def _announce_recording(self, recording: bool) -> None:
        """While a take records, the page holds a Live call and wake phrases pause."""
        self._page.publish_dictation(recording)
        if self._wake_phrases is not None:
            try:
                self._wake_phrases.pause_wake_phrases(recording)
            except Exception:
                logger.warning("Dictation could not pause the wake phrases", exc_info=True)

    def _record(self, take: _Take, transcriber: _Transcriber) -> None:
        """Record until the take ends, handing each finished piece to ``transcriber``."""
        # Imported per take: the capture stack (numpy) stays out of Desktop startup.
        from desktop.speech.capture import (
            CAPTURE_CAPTURING,
            CAPTURE_OPENING,
            ERROR_MICROPHONE_UNAVAILABLE,
            AudioBlock,
        )

        stop = threading.Event()
        # Without echo cancellation: a take starts a new canceller, which needs
        # 10-20 s of playback to learn that no echo returns (a headset) and
        # until then removes the user's speech along with whatever plays.
        capture = self._microphone.create_capture(
            on_status=lambda _status: None, stop_event=stop, echo_cancellation=False
        )
        subscription = capture.subscribe(max_seconds=_SUBSCRIPTION_SECONDS)
        capture.start()
        cutter = PieceCutter(
            budget_bytes=lambda: take.budget_bytes or DEFAULT_UPLOAD_BUDGET_BYTES,
            target_seconds=self._piece_target_seconds,
            search_seconds=self._piece_search_seconds,
        )
        seconds = 0.0
        skipped = 0.0
        capturing = False
        end_at: float | None = None
        try:
            while True:
                if take.cancelled.is_set():
                    raise _Cancelled
                if take.server_problem is not None:
                    raise _Failed(take.server_problem)
                if transcriber.failure is not None:
                    raise _Failed(transcriber.failure)
                # An end requested before the start cue (a tap) drops the take.
                if take.finish_requested.is_set() and end_at is None:
                    if not capturing:
                        self._play(CUE_CANCEL)
                        raise _Cancelled
                    end_at = self._clock() + TAIL_SECONDS
                state = capture.status.state
                if not capturing and state == CAPTURE_CAPTURING:
                    capturing = True
                    self._play(CUE_LISTEN)
                elif not capturing and state != CAPTURE_OPENING:
                    raise _Failed(ERROR_MICROPHONE_UNAVAILABLE)
                elif capturing and state != CAPTURE_CAPTURING:
                    logger.warning("The microphone stopped during dictation; keeping the audio")
                    break
                if end_at is not None and self._clock() >= end_at:
                    break
                item = subscription.read(timeout=_READ_TIMEOUT_SECONDS)
                if not isinstance(item, AudioBlock):
                    continue
                if skipped < START_SKIP_SECONDS:
                    skipped += item.duration
                    continue
                seconds += item.duration
                for piece in cutter.add(item.recording, item.recording_rate, item.duration):
                    transcriber.submit(piece.pcm, piece.rate, piece.seconds)
        finally:
            stop.set()
            subscription.close()
            capture.join(_CAPTURE_JOIN_SECONDS)
        if seconds < MIN_RECORDING_SECONDS:
            logger.info("Dictation too short (%.2f s); dropped", seconds)
            self._play(CUE_CANCEL)
            raise _Cancelled
        last = cutter.finish()
        if last is not None:
            transcriber.submit(last.pcm, last.rate, last.seconds)
        logger.info("Dictation recorded %.1f s", seconds)

    def _insert(self, take: _Take, text: str) -> None:
        if not text:
            raise _Failed(NOTICE_NOTHING_HEARD)
        outcome = self._inserter().insert(text, take.target_window)
        if outcome == INSERT_PASTED:
            logger.info("Dictation inserted %d characters", len(text))
            return
        if outcome == INSERT_CLIPBOARD:
            raise _Failed(NOTICE_INSERTED_TO_CLIPBOARD)
        raise _Failed(ERROR_INSERT_FAILED)

    def _inserter(self) -> TextInserter:
        # Created on first use: the Win32 binding exists only on Windows.
        if self._inserter_instance is None:
            self._inserter_instance = ClipboardTextInserter()
        return self._inserter_instance


class _Transcriber:
    """Transcribes a take's pieces in order on one thread while the take records.

    The first piece waits for the server check. The first failure stops the
    work and is reported through :attr:`failure`; later pieces are not sent.
    """

    def __init__(
        self, take: _Take, client: SpeechServerClient, preparation: threading.Thread
    ) -> None:
        self._take = take
        self._client = client
        self._preparation = preparation
        self._pieces: queue.SimpleQueue[tuple[bytes, int, float] | None] = queue.SimpleQueue()
        self._texts: list[str] = []
        self._stopped = threading.Event()
        self.failure: str | None = None
        self._thread = threading.Thread(
            target=self._run, name="vbot-dictation-transcribe", daemon=True
        )
        self._thread.start()

    def submit(self, pcm: bytes, rate: int, seconds: float) -> None:
        self._pieces.put((pcm, rate, seconds))

    def finish(self) -> str:
        """Wait for every submitted piece; returns their joined texts."""
        self._pieces.put(None)
        self._thread.join()
        if self._take.cancelled.is_set():
            raise _Cancelled
        if self.failure is not None:
            raise _Failed(self.failure)
        return " ".join(text for text in self._texts if text)

    def stop(self) -> None:
        """Send no further piece (the take ended without :meth:`finish`)."""
        self._stopped.set()
        self._pieces.put(None)

    def _run(self) -> None:
        from desktop.speech.capture import encode_wav

        # Its verdict is more precise than a failed upload's.
        self._preparation.join()
        number = 0
        while True:
            item = self._pieces.get()
            if item is None or self._stopped.is_set() or self._take.cancelled.is_set():
                return
            if self._take.server_problem is not None:
                self.failure = self._take.server_problem
                return
            pcm, rate, seconds = item
            number += 1
            try:
                text = self._client.transcribe(encode_wav(pcm, rate), filename="dictation.wav")
            except SpeechRequestCancelled:
                return
            except SpeechServerError as exc:
                self.failure = exc.error_code
                return
            except Exception:
                logger.exception("Dictation could not transcribe piece %d", number)
                self.failure = ERROR_DICTATION_FAILED
                return
            logger.info("Dictation piece %d transcribed (%.1f s)", number, seconds)
            self._texts.append(text.strip())


def _prepare_server(take: _Take, client: SpeechServerClient) -> None:
    """Check speech-to-text and warm it up while the user speaks."""
    try:
        problem = client.speech_readiness()
        if problem in _SERVER_NOT_READY:
            take.server_problem = problem
            return
        client.prepare_transcription()
        take.budget_bytes = client.upload_budget_bytes()
    except SpeechRequestCancelled:
        return
    except Exception:
        logger.warning("Dictation could not prepare the server", exc_info=True)


def _create_client(server_url: str, cancel: threading.Event) -> SpeechServerClient:
    return SpeechServerClient(server_url, cancel=cancel)
