"""Audio cues of Desktop Voice and dictation (Windows).

The user talks to the Desktop without watching it, so the steps of a Voice
command and of a dictation are audible. Both use the one :class:`CuePlayer` the
Desktop creates. Each cue is a short sequence of soft sine tones (a quick
swell, then an exponential fade) rendered once into an in-memory WAV and played
by one daemon thread (``vbot-desktop-cues``) in request order; a request never
blocks the caller. Other platforms stay silent.

A cue sounds at once, never delayed. The listen cue rings out longer than the
others, so its end is still heard when a wireless headset that had idled
swallows its start while waking.
"""

from __future__ import annotations

import io
import logging
import math
import queue
import struct
import sys
import threading
import wave
from collections.abc import Callable

logger = logging.getLogger("vbot.desktop.speech.cues")

CUE_LISTEN = "listen"
"""Listening starts: a wake phrase was detected, a dictation records."""
CUE_DONE = "done"
"""A Voice command was sent, a dictation stopped recording."""
CUE_CANCEL = "cancel"
CUE_NO_SPEECH = "no_speech"
CUE_FAILED = "failed"
"""A command or dictation ended without its result."""
CUE_ERROR = "error"
"""Voice stopped listening because of an error."""

LISTEN_LOUD_SECONDS = 0.1
"""How long the listen cue is loud; its quiet tail does not disturb a recording."""

_SAMPLE_RATE = 44100
_PEAK = 0.12
_FLOOR = 0.0001
_ATTACK_SECONDS = 0.01
_TONE_SPACING_SECONDS = 0.12
# Per cue: (frequency in Hz, seconds until the tone has faded out).
_CUE_TONES: dict[str, tuple[tuple[float, float], ...]] = {
    CUE_LISTEN: ((760.0, 0.28),),
    CUE_DONE: ((660.0, 0.09), (880.0, 0.09)),
    CUE_CANCEL: ((520.0, 0.09), (360.0, 0.09)),
    CUE_NO_SPEECH: ((360.0, 0.09),),
    CUE_FAILED: ((320.0, 0.09), (260.0, 0.09)),
    CUE_ERROR: ((260.0, 0.09), (220.0, 0.09)),
}
_TONE_END_SECONDS = 0.01
"""A tone lasts this much past its fade."""


def render_cue(cue: str) -> bytes:
    """Return the cue as a mono 16-bit WAV file."""

    tones = _CUE_TONES[cue]
    length = (len(tones) - 1) * _TONE_SPACING_SECONDS + tones[-1][1] + _TONE_END_SECONDS
    samples = [0.0] * round(length * _SAMPLE_RATE)
    for position, (frequency, fade_seconds) in enumerate(tones):
        offset = round(position * _TONE_SPACING_SECONDS * _SAMPLE_RATE)
        count = min(round((fade_seconds + _TONE_END_SECONDS) * _SAMPLE_RATE), len(samples) - offset)
        for index in range(count):
            seconds = index / _SAMPLE_RATE
            value = math.sin(2 * math.pi * frequency * seconds)
            samples[offset + index] += _gain(seconds, fade_seconds) * value
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(_SAMPLE_RATE)
        wav_file.writeframes(
            struct.pack(f"<{len(samples)}h", *(round(32767 * sample) for sample in samples))
        )
    return buffer.getvalue()


def _gain(seconds: float, fade_seconds: float) -> float:
    """Exponential swell to the peak, then an exponential fade to silence."""

    if seconds < _ATTACK_SECONDS:
        return float(_FLOOR * (_PEAK / _FLOOR) ** (seconds / _ATTACK_SECONDS))
    if seconds < fade_seconds:
        progress = (seconds - _ATTACK_SECONDS) / (fade_seconds - _ATTACK_SECONDS)
        return float(_PEAK * (_FLOOR / _PEAK) ** progress)
    return 0.0


class CuePlayer:
    """Plays cues in order on its own thread; :meth:`play` never blocks.

    ``play_wav`` plays one WAV file synchronously (``winsound`` by default;
    ``None`` on other platforms keeps the player silent).
    """

    def __init__(self, play_wav: Callable[[bytes], None] | None = None) -> None:
        self._play_wav = play_wav if play_wav is not None else _default_player()
        self._sounds: dict[str, bytes] = {}
        self._queue: queue.SimpleQueue[str | None] = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._closed = False

    def play(self, cue: str) -> None:
        """Queue one cue (``CUE_*``)."""

        if self._play_wav is None:
            return
        with self._lock:
            if self._closed:
                return
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="vbot-desktop-cues", daemon=True
                )
                self._thread.start()
        self._queue.put(cue)

    def close(self) -> None:
        """Stop after the queued cues; idempotent."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
        if thread is not None:
            self._queue.put(None)
            thread.join(timeout=1.0)

    def _run(self) -> None:
        while (cue := self._queue.get()) is not None:
            try:
                sound = self._sounds.get(cue)
                if sound is None:
                    sound = self._sounds[cue] = render_cue(cue)
                assert self._play_wav is not None
                self._play_wav(sound)
            except Exception:
                logger.warning("Cue %s could not be played", cue, exc_info=True)


def _default_player() -> Callable[[bytes], None] | None:
    if sys.platform != "win32":
        return None
    import winsound

    def play(sound: bytes) -> None:
        winsound.PlaySound(sound, winsound.SND_MEMORY | winsound.SND_NODEFAULT)

    return play
