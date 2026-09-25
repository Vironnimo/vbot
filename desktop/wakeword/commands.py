"""Voice commands: recording a spoken command, then transcribing and sending it.

:class:`CommandRecorder` records one command at a time on its own thread
(``vbot-voice-recorder``) from a dedicated capture subscription that starts at
the detection pre-roll. Its own speech detector finds the start and the end
of the utterance on 32 ms hops:

- the pre-roll and later non-speech audio wait in a short pre-speech buffer
  (:data:`PRE_SPEECH_SECONDS`) and are kept once speech starts, so the first
  syllable is never clipped;
- :data:`SPEECH_END_SILENCE_SECONDS` of silence after speech ends the
  recording; no speech within :data:`SPEECH_START_TIMEOUT_SECONDS` ends it as
  ``no_speech``;
- the kept audio never reaches the server's upload budget;
- a user stop ends the recording and keeps what was captured;
- a capture gap discards the recording (``microphone_read_failed``).

The WAV uses the capture's recording rate (the echo stage output).

:class:`CommandPipeline` processes finished recordings on up to three daemon
workers (``vbot-voice-command-N``): transcribe, drop reserved cancel phrases,
resolve the Session, send. It never blocks the listener, and stops (without
publishing) once its stop event is set.
"""

from __future__ import annotations

import io
import logging
import re
import threading
import time
import wave
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from desktop.wakeword._speech_detection import (
    SPEECH_HOP_SAMPLES,
    SpeechDetector,
    frame_is_speech,
)
from desktop.wakeword.capture import AudioBlock, CaptureGap, CaptureSubscription
from desktop.wakeword.server_client import (
    VoiceRequestCancelled,
    VoiceServerClient,
    VoiceServerError,
)

logger = logging.getLogger("vbot.desktop.wakeword.commands")

PRE_SPEECH_SECONDS = 0.4
"""Audio kept from before the first speech (covers the detection pre-roll)."""

SPEECH_END_SILENCE_SECONDS = 1.0
SPEECH_START_TIMEOUT_SECONDS = 1.5
SUBSCRIPTION_SECONDS = 10.0
"""Queue bound of the recording subscription."""

OUTCOME_AUDIO = "audio"
OUTCOME_NO_SPEECH = "no_speech"
OUTCOME_FAILED = "failed"
OUTCOME_CANCELLED = "cancelled"

STAGE_TRANSCRIBING = "transcribing"
STAGE_SENDING = "sending"

EVENT_SENT = "sent"
EVENT_NO_SPEECH = "no_speech"
EVENT_CANCELLED = "cancelled"
EVENT_TRANSCRIPTION_FAILED = "transcription_failed"
EVENT_COMMAND_FAILED = "command_failed"

ERROR_MICROPHONE_READ_FAILED = "microphone_read_failed"
ERROR_PIPELINE_FAILED = "pipeline_failed"

MAX_COMMAND_WORKERS = 3

_HOP_BYTES = SPEECH_HOP_SAMPLES * 2
_HOP_SECONDS = SPEECH_HOP_SAMPLES / 16000
_SILENCE_HOPS = int(SPEECH_END_SILENCE_SECONDS / _HOP_SECONDS)
_START_TIMEOUT_HOPS = int(SPEECH_START_TIMEOUT_SECONDS / _HOP_SECONDS)
_READ_TIMEOUT_SECONDS = 0.1
_VOICE_CANCEL_PHRASES = frozenset(["abbrechen", "vergiss es"])
_NOT_CREATED = object()


def encode_wav(pcm16: bytes, sample_rate: int) -> bytes:
    """Wrap mono 16-bit PCM in a WAV container."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm16)
    return buffer.getvalue()


def is_voice_cancel_phrase(transcript: str) -> bool:
    """Whether a transcript is, or ends with, a reserved cancel phrase."""
    normalized = re.sub(r"[^\wäöüß]+", " ", transcript.casefold(), flags=re.UNICODE).strip()
    return any(
        normalized == phrase or normalized.endswith(f" {phrase}")
        for phrase in _VOICE_CANCEL_PHRASES
    )


@dataclass(frozen=True)
class RecordingResult:
    """How one recording ended; ``wav`` is set for ``audio``, ``error_code`` for ``failed``."""

    outcome: str
    wav: bytes | None = None
    error_code: str | None = None


class CommandRecorder:
    """Records one spoken command at a time.

    ``budget_bytes`` returns the current upload budget for the recorded audio
    payload without blocking. The speech detector and fallback VAD are created
    on the first recording and reused.
    """

    def __init__(
        self,
        *,
        stop_event: threading.Event,
        speech_detector_factory: Callable[[], SpeechDetector | None],
        fallback_vad_factory: Callable[[], Any | None],
        budget_bytes: Callable[[], int],
    ) -> None:
        self._stop = stop_event
        self._speech_detector_factory = speech_detector_factory
        self._fallback_vad_factory = fallback_vad_factory
        self._budget_bytes = budget_bytes
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._user_stop = threading.Event()
        self._detector: Any = _NOT_CREATED
        self._fallback_vad: Any = _NOT_CREATED

    def start(
        self,
        pre_roll: Sequence[AudioBlock],
        subscription: CaptureSubscription,
        on_done: Callable[[RecordingResult], None],
    ) -> bool:
        """Record from ``subscription`` seeded with ``pre_roll``; ``False`` while busy.

        ``on_done`` receives the result on the recorder thread, also when the
        recording is cancelled or fails. The recorder closes the subscription.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            user_stop = threading.Event()
            self._user_stop = user_stop
            self._thread = threading.Thread(
                target=self._run,
                args=(tuple(pre_roll), subscription, on_done, user_stop),
                name="vbot-voice-recorder",
                daemon=True,
            )
            self._thread.start()
        return True

    def stop(self) -> None:
        """End the current recording now and keep the audio captured so far."""
        with self._lock:
            self._user_stop.set()

    def join(self, timeout: float) -> bool:
        """Wait for the current recording thread; ``True`` once none runs."""
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _run(
        self,
        pre_roll: tuple[AudioBlock, ...],
        subscription: CaptureSubscription,
        on_done: Callable[[RecordingResult], None],
        user_stop: threading.Event,
    ) -> None:
        try:
            result = self._record(pre_roll, subscription, user_stop)
        except Exception:
            logger.exception("Voice command recording failed unexpectedly")
            result = RecordingResult(OUTCOME_FAILED, error_code=ERROR_PIPELINE_FAILED)
        finally:
            subscription.close()
        try:
            on_done(result)
        except Exception:
            logger.exception("Voice recording result handler failed")

    def _record(
        self,
        pre_roll: tuple[AudioBlock, ...],
        subscription: CaptureSubscription,
        user_stop: threading.Event,
    ) -> RecordingResult:
        detector, fallback_vad = self._speech_deciders()
        if detector is not None:
            detector.reset()
        budget = self._budget_bytes()
        rate = pre_roll[-1].recording_rate if pre_roll else None
        pre_speech: deque[AudioBlock] = deque()
        pre_speech_seconds = 0.0
        for block in pre_roll:
            if block.recording_rate != rate:
                pre_speech.clear()
                pre_speech_seconds = 0.0
                continue
            pre_speech.append(block)
            pre_speech_seconds += block.duration
        kept: list[bytes] = []
        kept_bytes = 0
        has_speech = False
        silent_hops = 0
        waited_hops = 0
        pending = bytearray()

        while True:
            if self._stop.is_set():
                return RecordingResult(OUTCOME_CANCELLED)
            if user_stop.is_set():
                logger.info("Voice recording stopped by the user")
                break
            item = subscription.read(_READ_TIMEOUT_SECONDS)
            if item is None:
                if subscription.closed:
                    return RecordingResult(OUTCOME_CANCELLED)
                continue
            if isinstance(item, CaptureGap):
                logger.warning("Voice recording lost audio (%s); discarding it", item.reason)
                return RecordingResult(OUTCOME_FAILED, error_code=ERROR_MICROPHONE_READ_FAILED)
            if rate is None:
                rate = item.recording_rate
            elif item.recording_rate != rate:
                logger.warning("Voice recording rate changed; discarding it")
                return RecordingResult(OUTCOME_FAILED, error_code=ERROR_MICROPHONE_READ_FAILED)

            pending += item.pcm16
            decisions: list[bool] = []
            while len(pending) >= _HOP_BYTES:
                hop = bytes(pending[:_HOP_BYTES])
                del pending[:_HOP_BYTES]
                decisions.append(frame_is_speech(hop, detector, fallback_vad))
            trailing_silence = _trailing_silence(decisions)

            if has_speech:
                if kept_bytes + len(item.recording) > budget:
                    logger.warning("Voice recording reached the speech upload budget; stopping")
                    break
                kept.append(item.recording)
                kept_bytes += len(item.recording)
                if any(decisions):
                    silent_hops = trailing_silence
                else:
                    silent_hops += len(decisions)
            elif any(decisions):
                has_speech = True
                kept.extend(block.recording for block in pre_speech)
                kept.append(item.recording)
                kept_bytes = sum(len(chunk) for chunk in kept)
                pre_speech.clear()
                silent_hops = trailing_silence
            else:
                pre_speech.append(item)
                pre_speech_seconds += item.duration
                while (
                    len(pre_speech) > 1
                    and pre_speech_seconds - pre_speech[0].duration >= PRE_SPEECH_SECONDS
                ):
                    pre_speech_seconds -= pre_speech.popleft().duration
                waited_hops += len(decisions)

            if has_speech and silent_hops >= _SILENCE_HOPS:
                break
            if not has_speech and waited_hops >= _START_TIMEOUT_HOPS:
                break

        if not has_speech or rate is None:
            logger.info("Voice recording heard no speech")
            return RecordingResult(OUTCOME_NO_SPEECH)
        return RecordingResult(OUTCOME_AUDIO, wav=encode_wav(b"".join(kept), rate))

    def _speech_deciders(self) -> tuple[SpeechDetector | None, Any | None]:
        if self._detector is _NOT_CREATED:
            self._detector = self._speech_detector_factory()
        if self._fallback_vad is _NOT_CREATED:
            self._fallback_vad = self._fallback_vad_factory()
        return self._detector, self._fallback_vad


def _trailing_silence(decisions: list[bool]) -> int:
    count = 0
    for is_speech in reversed(decisions):
        if is_speech:
            break
        count += 1
    return count


@dataclass(frozen=True)
class Command:
    """One recorded command waiting for the server."""

    command_id: str
    model_id: str
    agent_id: str
    session_behavior: str
    wav: bytes


@dataclass(frozen=True)
class CommandOutcome:
    """How one command ended: ``kind`` is an event kind (``sent``, ``command_failed``, ...)."""

    command_id: str
    model_id: str
    kind: str
    agent_id: str | None = None
    session_id: str | None = None
    error_code: str | None = None


class CommandPipeline:
    """Transcribes and sends recorded commands on up to ``max_workers`` daemon threads.

    ``on_stage(command_id, stage)`` reports ``transcribing`` and ``sending``;
    ``on_outcome`` receives exactly one :class:`CommandOutcome` per command,
    unless the pipeline stopped (``stop_event``) while it ran.
    """

    def __init__(
        self,
        client: VoiceServerClient,
        *,
        stop_event: threading.Event,
        on_stage: Callable[[str, str], None],
        on_outcome: Callable[[CommandOutcome], None],
        max_workers: int = MAX_COMMAND_WORKERS,
    ) -> None:
        self._client = client
        self._stop = stop_event
        self._on_stage = on_stage
        self._on_outcome = on_outcome
        self._max_workers = max_workers
        self._condition = threading.Condition()
        self._queue: deque[Command] = deque()
        self._workers: list[threading.Thread] = []
        self._idle_workers = 0
        self._closed = False

    def submit(self, command: Command) -> bool:
        """Queue one command; ``False`` once the pipeline was closed."""
        with self._condition:
            if self._closed or self._stop.is_set():
                return False
            self._queue.append(command)
            if self._idle_workers == 0 and len(self._workers) < self._max_workers:
                worker = threading.Thread(
                    target=self._work,
                    name=f"vbot-voice-command-{len(self._workers) + 1}",
                    daemon=True,
                )
                self._workers.append(worker)
                worker.start()
            self._condition.notify()
        return True

    def close(self, timeout: float) -> bool:
        """Discard queued commands and wait up to ``timeout`` for the workers.

        A command already talking to the server finishes its current request
        first; ``False`` when a worker is still busy after ``timeout``.
        """
        with self._condition:
            self._closed = True
            self._queue.clear()
            self._condition.notify_all()
            workers = list(self._workers)
        deadline = time.monotonic() + timeout
        for worker in workers:
            worker.join(max(0.0, deadline - time.monotonic()))
        return not any(worker.is_alive() for worker in workers)

    def _work(self) -> None:
        while True:
            with self._condition:
                self._idle_workers += 1
                self._condition.wait_for(lambda: self._closed or bool(self._queue))
                self._idle_workers -= 1
                if self._closed:
                    return
                command = self._queue.popleft()
            outcome = self._process(command)
            if outcome is not None and not self._stop.is_set():
                try:
                    self._on_outcome(outcome)
                except Exception:
                    logger.exception("Voice command outcome handler failed")

    def _process(self, command: Command) -> CommandOutcome | None:
        def outcome(kind: str, **fields: Any) -> CommandOutcome:
            return CommandOutcome(command.command_id, command.model_id, kind, **fields)

        try:
            self._on_stage(command.command_id, STAGE_TRANSCRIBING)
            try:
                transcript = self._client.transcribe(command.wav).strip()
            except VoiceServerError as exc:
                logger.warning("Voice command transcription failed (%s): %s", exc.error_code, exc)
                return outcome(EVENT_TRANSCRIPTION_FAILED, error_code=exc.error_code)
            if not transcript:
                logger.info("Voice command transcript was empty")
                return outcome(EVENT_NO_SPEECH)
            if is_voice_cancel_phrase(transcript):
                logger.info("Voice command cancelled by a cancel phrase")
                return outcome(EVENT_CANCELLED)
            if self._stop.is_set():
                return None
            self._on_stage(command.command_id, STAGE_SENDING)
            session_id = self._client.resolve_session(command.agent_id, command.session_behavior)
            self._client.send_command(command.agent_id, session_id, transcript)
        except VoiceRequestCancelled:
            logger.debug("Voice command stopped with its listener")
            return None
        except VoiceServerError as exc:
            logger.warning("Voice command failed (%s): %s", exc.error_code, exc)
            return outcome(
                EVENT_COMMAND_FAILED, agent_id=command.agent_id, error_code=exc.error_code
            )
        except Exception:
            if self._stop.is_set():
                logger.debug("Voice command stopped with its listener", exc_info=True)
                return None
            logger.exception("Voice command failed unexpectedly")
            return outcome(
                EVENT_COMMAND_FAILED, agent_id=command.agent_id, error_code=ERROR_PIPELINE_FAILED
            )
        logger.info(
            "Voice command sent (agent_id=%s, session_behavior=%s)",
            command.agent_id,
            command.session_behavior,
        )
        return outcome(EVENT_SENT, agent_id=command.agent_id, session_id=session_id)
