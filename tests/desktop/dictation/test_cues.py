"""Dictation cues: rendered as short WAV files and played in order off the caller's thread."""

from __future__ import annotations

import io
import threading
import wave

import pytest

from desktop.dictation.cues import (
    CUE_CANCEL,
    CUE_ERROR,
    CUE_START,
    CUE_STOP,
    START_CUE_SECONDS,
    CuePlayer,
    render_cue,
)


@pytest.mark.parametrize("cue", [CUE_START, CUE_STOP, CUE_CANCEL, CUE_ERROR])
def test_each_cue_is_a_short_mono_wav(cue: str) -> None:
    with wave.open(io.BytesIO(render_cue(cue))) as wav_file:
        seconds = wav_file.getnframes() / wav_file.getframerate()
        assert (wav_file.getnchannels(), wav_file.getsampwidth()) == (1, 2)
    assert 0.05 < seconds < 0.4
    if cue == CUE_START:
        assert seconds == pytest.approx(START_CUE_SECONDS, abs=0.001)


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
    for cue in (CUE_START, CUE_STOP, CUE_ERROR):
        player.play(cue)

    assert done.wait(2)
    player.close()
    assert played == [render_cue(CUE_START), render_cue(CUE_STOP), render_cue(CUE_ERROR)]
    assert threads == {"vbot-dictation-cues"}
