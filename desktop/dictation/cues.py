"""Short audio cues for Desktop dictation (Windows).

The user dictates into another app and does not watch the Desktop, so the
recording's start, its end, a cancel and a failure are audible. Each cue is a
soft sine tone sequence rendered once into an in-memory WAV and played by one
daemon thread (``vbot-dictation-cues``) in request order; a request never
blocks the caller. Other platforms stay silent.
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

logger = logging.getLogger("vbot.desktop.dictation.cues")

CUE_START = "start"
CUE_STOP = "stop"
CUE_CANCEL = "cancel"
CUE_ERROR = "error"

_SAMPLE_RATE = 22050
_VOLUME = 0.25
_FADE_SECONDS = 0.01
# (frequency in Hz, seconds); a frequency of 0 is a pause.
_CUE_TONES: dict[str, tuple[tuple[float, float], ...]] = {
    CUE_START: ((660.0, 0.07), (0.0, 0.02), (880.0, 0.09)),
    CUE_STOP: ((880.0, 0.07), (0.0, 0.02), (660.0, 0.09)),
    CUE_CANCEL: ((440.0, 0.12),),
    CUE_ERROR: ((330.0, 0.12), (0.0, 0.06), (330.0, 0.12)),
}
START_CUE_SECONDS = sum(seconds for _, seconds in _CUE_TONES[CUE_START])
"""How long the start cue sounds; the recording discards audio from that time."""


def render_cue(cue: str) -> bytes:
    """Return the cue as a mono 16-bit WAV file."""

    samples: list[int] = []
    for frequency, seconds in _CUE_TONES[cue]:
        count = round(seconds * _SAMPLE_RATE)
        fade = max(1, round(_FADE_SECONDS * _SAMPLE_RATE))
        for index in range(count):
            if frequency <= 0:
                samples.append(0)
                continue
            envelope = min(1.0, index / fade, (count - 1 - index) / fade)
            value = math.sin(2 * math.pi * frequency * index / _SAMPLE_RATE)
            samples.append(round(32767 * _VOLUME * envelope * value))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(_SAMPLE_RATE)
        wav_file.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return buffer.getvalue()


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
                    target=self._run, name="vbot-dictation-cues", daemon=True
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
                logger.warning("Dictation cue %s could not be played", cue, exc_info=True)


def _default_player() -> Callable[[bytes], None] | None:
    if sys.platform != "win32":
        return None
    import winsound

    def play(sound: bytes) -> None:
        winsound.PlaySound(sound, winsound.SND_MEMORY | winsound.SND_NODEFAULT)

    return play
