"""Desktop cues: rendered as short WAV files and played in order off the caller's thread."""

from __future__ import annotations

import io
import threading
import wave

import pytest

from desktop.speech.cues import (
    CUE_CANCEL,
    CUE_DONE,
    CUE_ERROR,
    CUE_FAILED,
    CUE_LISTEN,
    CUE_NO_SPEECH,
    LISTEN_LOUD_SECONDS,
    CuePlayer,
    render_cue,
)


def _seconds(sound: bytes) -> float:
    with wave.open(io.BytesIO(sound)) as wav_file:
        assert (wav_file.getnchannels(), wav_file.getsampwidth()) == (1, 2)
        frames: int = wav_file.getnframes()
        rate: int = wav_file.getframerate()
    return frames / rate


@pytest.mark.parametrize(
    "cue", [CUE_LISTEN, CUE_DONE, CUE_CANCEL, CUE_NO_SPEECH, CUE_FAILED, CUE_ERROR]
)
def test_each_cue_is_a_short_mono_wav(cue: str) -> None:
    assert 0.05 < _seconds(render_cue(cue)) < 0.4


def test_the_listen_cue_rings_out_past_its_loud_start() -> None:
    # Heard even when a waking headset swallows the start.
    assert _seconds(render_cue(CUE_LISTEN)) > 2 * LISTEN_LOUD_SECONDS


def test_cues_play_in_order_on_their_own_thread_past_a_failing_one() -> None:
    played: list[bytes] = []
    threads: set[str] = set()
    done = threading.Event()

    def play(sound: bytes) -> None:
        threads.add(threading.current_thread().name)
        played.append(sound)
        if len(played) == 1:
            raise RuntimeError("audio device busy")
        if len(played) == 3:
            done.set()

    player = CuePlayer(play)
    for cue in (CUE_LISTEN, CUE_DONE, CUE_FAILED):
        player.play(cue)

    assert done.wait(2)
    player.close()
    assert played == [render_cue(CUE_LISTEN), render_cue(CUE_DONE), render_cue(CUE_FAILED)]
    assert threads == {"vbot-desktop-cues"}
