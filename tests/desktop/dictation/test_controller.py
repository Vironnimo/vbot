"""Desktop dictation: shortcut modes, cancel, failures, and the stored preference."""

from __future__ import annotations

import io
import json
import threading
import wave
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, override

import numpy as np
import pytest

from desktop import hotkey
from desktop.dictation.controller import (
    DictationController,
)
from desktop.dictation.insertion import INSERT_CLIPBOARD, INSERT_FAILED, INSERT_PASTED
from desktop.speech.microphone import MicrophoneService
from desktop.speech.server_client import SpeechServerRejected
from tests.desktop.hotkey_test_support import FakeHotkeyApi
from tests.desktop.speech.speech_test_support import (
    FakeInputStream,
    FakeSoundDevice,
    Overflow,
    silence,
    tone,
    wait_until,
)

TARGET_WINDOW = 7
HELD_KEYS = {ord("D"), hotkey.VK_CONTROL, hotkey.VK_MENU}
SERVER_URL = "http://server.lan:8420"
ESCAPE_REGISTRATION = (hotkey.ESCAPE_HOTKEY_ID, hotkey.MOD_NOREPEAT, hotkey.VK_ESCAPE)
RATE = 16000
LEAD_IN = silence(0.4, RATE)  # covers the audio dropped under the start cue
WORD = tone(0.6, RATE)


class StepClock:
    """The controller's clock: every reading moves it on by 5 ms.

    The take reads it once per round of its recording loop, and a round takes
    the next waiting audio block. The 0.2 s tail after an end request thus
    lasts about forty rounds instead of wall-clock time, in which a loaded
    machine may deliver too little fake audio for the take to count.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 0.005
        return self.now


class FakeMicrophone(FakeSoundDevice):
    """Opens only while ``can_open`` is set, so a test can hold a take before its first audio."""

    def __init__(self) -> None:
        super().__init__(pace=0.0025)
        self.can_open = threading.Event()
        self.can_open.set()

    @override
    def InputStream(  # noqa: N802 - mirrors sounddevice.InputStream
        self, *, samplerate: int, channels: int, dtype: str, blocksize: int, device: int
    ) -> FakeInputStream:
        assert self.can_open.wait(5)
        return super().InputStream(
            samplerate=samplerate,
            channels=channels,
            dtype=dtype,
            blocksize=blocksize,
            device=device,
        )


class FakeServer:
    """Speech server double behind the controller's client factory."""

    def __init__(self) -> None:
        self.readiness: str | None = None
        self.transcript: str | Exception = "  Hallo Welt  "
        self.release_transcription = threading.Event()
        self.release_transcription.set()
        self.transcribing = threading.Event()
        self.transcribed = threading.Event()
        self.uploads: list[bytes] = []
        self.urls: list[str] = []
        # Answer "Teil <n>" per upload that holds sound, "" for silence.
        self.numbered = False
        self.budget_bytes = 10_000_000

    def client(self, server_url: str, cancel: threading.Event) -> Any:
        self.urls.append(server_url)
        return _FakeClient(self)


@dataclass
class _FakeClient:
    server: FakeServer

    def speech_readiness(self) -> str | None:
        return self.server.readiness

    def prepare_transcription(self) -> str | None:
        return "loaded"

    def upload_budget_bytes(self) -> int:
        return self.server.budget_bytes

    def transcribe(self, audio: bytes, *, filename: str) -> str:
        self.server.uploads.append(audio)
        self.server.transcribing.set()
        assert self.server.release_transcription.wait(5)
        self.server.transcribed.set()
        if isinstance(self.server.transcript, Exception):
            raise self.server.transcript
        if self.server.numbered:
            heard = [upload for upload in self.server.uploads if _energy(upload) > 0]
            return f"Teil {len(heard)}" if _energy(audio) > 0 else ""
        return self.server.transcript

    def close(self) -> None:
        pass


@dataclass
class FakeInserter:
    outcome: str = INSERT_PASTED
    inserted: list[tuple[str, int]] = field(default_factory=list)

    def foreground_window(self) -> int:
        return TARGET_WINDOW

    def insert(self, text: str, target_window: int) -> str:
        self.inserted.append((text, target_window))
        return self.outcome


@dataclass
class FakeCues:
    played: list[str] = field(default_factory=list)

    def play(self, cue: str) -> None:
        self.played.append(cue)


@dataclass
class FakePage:
    """The page and the Voice controller: both learn when a take records."""

    recording: list[bool] = field(default_factory=list)
    wake_phrases_paused: list[bool] = field(default_factory=list)

    def publish_dictation(self, recording: bool) -> None:
        self.recording.append(recording)

    def pause_wake_phrases(self, paused: bool) -> None:
        self.wake_phrases_paused.append(paused)


@dataclass
class Rig:
    controller: DictationController
    sd: FakeMicrophone
    server: FakeServer
    inserter: FakeInserter
    cues: FakeCues
    page: FakePage
    apis: list[FakeHotkeyApi]
    path: Path
    echo_stages: list[str]

    def inserted_text(self) -> str:
        assert len(self.inserter.inserted) == 1
        return self.inserter.inserted[0][0]

    @property
    def api(self) -> FakeHotkeyApi:
        return self.apis[-1]

    def press(self) -> None:
        self.api.messages.put((hotkey.WM_HOTKEY, hotkey.HOTKEY_ID))

    def escape(self) -> None:
        wait_until(lambda: ESCAPE_REGISTRATION in self.api.registrations())
        self.api.messages.put((hotkey.WM_HOTKEY, hotkey.ESCAPE_HOTKEY_ID))

    def wait_idle(self) -> None:
        wait_until(lambda: not self.controller.is_busy())

    def start_take(self) -> None:
        """Press, then wait until the microphone delivered a spoken word to the take."""
        self.sd.feed(LEAD_IN, WORD)
        self.press()
        wait_until(lambda: "listen" in self.cues.played and self.sd.drained)

    def dictate(self) -> None:
        """One toggle take: start, speak, end."""
        self.start_take()
        self.press()
        self.wait_idle()


@pytest.fixture
def make_rig(tmp_path: Path) -> Iterator[Any]:
    rigs: list[Rig] = []

    def make(*, mode: str = "toggle", server_url: str = SERVER_URL, **options: Any) -> Rig:
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps(
                {
                    # Enabled: a take records without it all the same.
                    "microphone": {"echo_cancellation": True},
                    "dictation": {"hotkey": {"enabled": True}, "mode": mode},
                }
            ),
            encoding="utf-8",
        )
        sd = FakeMicrophone()
        server, inserter, cues, page = FakeServer(), FakeInserter(), FakeCues(), FakePage()
        apis: list[FakeHotkeyApi] = []
        echo_stages: list[str] = []

        def api_factory() -> FakeHotkeyApi:
            apis.append(FakeHotkeyApi())
            return apis[-1]

        controller = DictationController(
            settings_path=path,
            microphone=MicrophoneService(
                settings_path=path,
                audio_backend=sd,
                echo_stage_factory=lambda: echo_stages.append("created"),
            ),
            server_url=server_url,
            page=page,
            wake_phrases=page,
            cues=cues,  # type: ignore[arg-type]
            inserter=inserter,
            client_factory=server.client,
            hotkey_supported=True,
            hotkey_api_factory=api_factory,
            clock=StepClock(),
            **options,
        )
        controller.start()
        rig = Rig(controller, sd, server, inserter, cues, page, apis, path, echo_stages)
        rigs.append(rig)
        return rig

    yield make
    for rig in rigs:
        rig.sd.can_open.set()
        rig.server.release_transcription.set()
        rig.controller.stop()


def _samples(audio: bytes | np.ndarray) -> np.ndarray:
    if isinstance(audio, np.ndarray):
        return audio.astype(np.int64)
    with wave.open(io.BytesIO(audio)) as wav_file:
        frames = wav_file.readframes(wav_file.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.int64)


def _energy(audio: bytes | np.ndarray) -> int:
    return int(np.abs(_samples(audio)).sum())


def test_a_toggle_take_records_until_the_second_press_and_types_the_stripped_transcript(
    make_rig: Any,
) -> None:
    rig = make_rig()

    rig.dictate()

    assert rig.inserter.inserted == [("Hallo Welt", TARGET_WINDOW)]
    assert rig.cues.played == ["listen", "done"]
    assert rig.page.recording[0] is True
    assert rig.page.recording[-1] is False
    assert rig.page.wake_phrases_paused == rig.page.recording
    # Echo cancellation stays out of a take although the setting enables it.
    assert rig.echo_stages == []
    assert len(rig.server.uploads) == 1
    assert rig.server.urls == [SERVER_URL]
    # Escape belongs to the take: claimed while it runs, released after.
    assert ESCAPE_REGISTRATION in rig.api.registrations()
    wait_until(lambda: ("unregister", hotkey.ESCAPE_HOTKEY_ID) in rig.api.calls)
    status = rig.controller.status()
    assert (status["state"], status["last_failure"]) == ("idle", None)


def test_a_hold_take_ends_when_the_combination_is_let_go_and_a_tap_is_dropped(
    make_rig: Any,
) -> None:
    rig = make_rig(mode="hold")
    rig.api.down |= HELD_KEYS

    rig.start_take()
    assert rig.controller.status()["state"] == "recording"  # held: still recording
    rig.api.down -= HELD_KEYS
    rig.wait_idle()
    assert rig.inserter.inserted == [("Hallo Welt", TARGET_WINDOW)]

    # A tap: the keys are already up while the microphone is still opening.
    rig.sd.can_open.clear()
    rig.press()
    wait_until(lambda: "cancel" in rig.cues.played)
    rig.sd.can_open.set()
    rig.wait_idle()

    assert rig.cues.played == ["listen", "done", "cancel"]
    assert len(rig.server.uploads) == 1
    assert rig.controller.status()["last_failure"] is None


@pytest.mark.parametrize("during", ["recording", "transcribing"])
def test_escape_cancels_the_take_without_inserting(make_rig: Any, during: str) -> None:
    rig = make_rig()
    rig.server.release_transcription.clear()
    rig.start_take()
    if during == "transcribing":
        rig.press()
        wait_until(lambda: rig.controller.status()["state"] == "transcribing")
        assert rig.server.transcribing.wait(5)

    rig.escape()
    wait_until(lambda: "cancel" in rig.cues.played)
    rig.wait_idle()

    assert rig.inserter.inserted == []
    assert rig.cues.played[-1] == "cancel"
    assert rig.controller.status()["last_failure"] is None

    if during == "transcribing":
        # A cancelled request that replies late cannot end or insert into the next take.
        rig.start_take()
        rig.server.release_transcription.set()
        assert rig.server.transcribed.wait(1)
        assert rig.controller.status()["state"] == "recording"
        assert rig.inserter.inserted == []
        rig.press()
        rig.wait_idle()
        assert rig.inserter.inserted == [("Hallo Welt", TARGET_WINDOW)]


@pytest.mark.parametrize(
    ("gap", "code"),
    [(Overflow(), "recording_interrupted"), (OSError("lost microphone"), "microphone_read_failed")],
    ids=["overflow", "read-failure"],
)
def test_lost_audio_ends_the_take_without_inserting_an_incomplete_transcript(
    make_rig: Any, gap: object, code: str
) -> None:
    rig = make_rig()
    rig.start_take()

    rig.sd.feed(gap, WORD)
    rig.wait_idle()

    assert rig.inserter.inserted == []
    assert rig.cues.played == ["listen", "failed"]
    assert rig.controller.status()["last_failure"]["code"] == code
    assert rig.page.recording[-1] is False
    assert rig.page.wake_phrases_paused[-1] is False


@pytest.mark.parametrize(
    ("transcript", "outcome", "code"),
    [
        (
            SpeechServerRejected("transcription_failed", "HTTP 500", status_code=500),
            INSERT_PASTED,
            "transcription_failed",
        ),
        ("  ", INSERT_PASTED, "nothing_heard"),
        ("Hallo", INSERT_CLIPBOARD, "inserted_to_clipboard"),
        ("Hallo", INSERT_FAILED, "insert_failed"),
    ],
    ids=["transcription", "silence", "clipboard", "insert"],
)
def test_a_take_that_types_nothing_signals_and_reports_why(
    make_rig: Any, transcript: str | Exception, outcome: str, code: str
) -> None:
    rig = make_rig()
    rig.server.transcript = transcript
    rig.inserter.outcome = outcome

    rig.dictate()

    assert rig.cues.played == ["listen", "done", "failed"]
    failure = rig.controller.status()["last_failure"]
    assert failure["code"] == code
    assert datetime.fromisoformat(failure["at"]).tzinfo is not None


@pytest.mark.parametrize(
    ("server_url", "readiness", "devices", "code"),
    [
        ("", None, True, "server_unreachable"),
        (
            "http://server.lan:8420",
            "speech_to_text_unconfigured",
            True,
            "speech_to_text_unconfigured",
        ),
        ("http://server.lan:8420", None, False, "microphone_unavailable"),
    ],
    ids=["no-server", "no-speech-to-text", "no-microphone"],
)
def test_a_take_that_cannot_run_ends_by_itself_with_an_error_cue(
    make_rig: Any, server_url: str, readiness: str | None, devices: bool, code: str
) -> None:
    rig = make_rig(server_url=server_url)
    rig.server.readiness = readiness
    if not devices:
        rig.sd.devices.clear()

    rig.press()
    wait_until(lambda: "failed" in rig.cues.played)
    rig.wait_idle()

    assert rig.cues.played[-1] == "failed"
    assert rig.server.uploads == []
    assert rig.controller.status()["last_failure"]["code"] == code


@pytest.mark.parametrize(
    ("options", "budget", "script", "whole_words"),
    [
        (
            {"piece_target_seconds": 0.5, "piece_search_seconds": 1.0},
            10_000_000,
            [WORD, silence(0.5, RATE), WORD, silence(0.5, RATE), WORD],
            True,
        ),
        (
            {"piece_target_seconds": 0.5, "piece_search_seconds": 0.3},
            10_000_000,
            [tone(2.0, RATE)],
            False,
        ),
        ({}, 44 + RATE, [tone(2.0, RATE)], False),
    ],
    ids=["cut-at-pauses", "no-pause", "upload-limit"],
)
def test_a_long_take_is_transcribed_in_pieces_and_inserted_as_one_text(
    make_rig: Any,
    options: dict[str, float],
    budget: int,
    script: list[np.ndarray],
    whole_words: bool,
) -> None:
    """No length limit: pieces keep every upload within the server's limit and lose no audio."""
    rig = make_rig(server_url="http://other.lan:8420", **options)
    rig.server.numbered = True
    rig.server.budget_bytes = budget
    rig.sd.feed(LEAD_IN, *script)
    rig.controller.set_server_url(SERVER_URL)

    rig.press()
    wait_until(lambda: rig.sd.drained and len(rig.server.uploads) >= 2)
    rig.press()
    rig.wait_idle()

    heard = [upload for upload in rig.server.uploads if _energy(upload) > 0]
    assert len(heard) >= 2
    assert rig.inserted_text() == " ".join(f"Teil {n}" for n in range(1, len(heard) + 1))
    assert sum(_energy(upload) for upload in heard) == sum(_energy(part) for part in script)
    assert all(len(upload) <= budget for upload in rig.server.uploads)
    if whole_words:
        assert [_energy(upload) for upload in heard] == [_energy(WORD)] * 3
    assert rig.server.urls == [SERVER_URL]
    assert rig.cues.played == ["listen", "done"]


def test_a_piece_that_cannot_be_transcribed_ends_the_take_at_once(make_rig: Any) -> None:
    rig = make_rig(piece_target_seconds=0.5, piece_search_seconds=0.4)
    rig.server.transcript = SpeechServerRejected(
        "transcription_failed", "HTTP 500", status_code=500
    )
    rig.sd.feed(LEAD_IN, WORD, silence(0.5, RATE))

    rig.press()  # never ended by the user
    wait_until(lambda: "failed" in rig.cues.played)
    rig.wait_idle()

    assert rig.inserter.inserted == []
    assert rig.cues.played == ["listen", "failed"]
    assert rig.controller.status()["last_failure"]["code"] == "transcription_failed"


def test_settings_persist_mode_and_shortcut_and_reject_invalid_changes(
    make_rig: Any,
) -> None:
    rig = make_rig()
    assert rig.controller.status() == {
        "supported": True,
        "enabled": True,
        "hotkey": {"ctrl": True, "alt": True, "shift": False, "win": False, "key": "KeyD"},
        "mode": "toggle",
        "error_code": None,
        "state": "idle",
        "last_failure": None,
    }

    assert rig.controller.update({"mode": "hold"})["mode"] == "hold"
    assert len(rig.apis) == 1  # a mode change keeps the registration
    assert rig.controller.update({"key": "KeyJ", "shift": True})["hotkey"]["key"] == "KeyJ"
    assert len(rig.apis) == 2
    invalid_mode = rig.controller.update({"mode": "push", "key": "KeyK"})
    invalid_key = rig.controller.update({"mode": "toggle", "key": "Enter"})
    not_an_object = rig.controller.update(["mode"])

    assert invalid_mode["error_code"] == "dictation_config_invalid"
    assert invalid_key["error_code"] == "hotkey_invalid"
    assert not_an_object["error_code"] == "dictation_config_invalid"
    stored = json.loads(rig.path.read_text(encoding="utf-8"))["dictation"]
    assert stored["mode"] == "hold"
    assert stored["hotkey"]["key"] == "KeyJ"


def test_stop_cancels_a_running_take_and_is_final(make_rig: Any) -> None:
    rig = make_rig()
    rig.press()
    wait_until(lambda: "listen" in rig.cues.played)

    rig.controller.stop()

    assert not rig.controller.is_busy()
    assert rig.inserter.inserted == []
    rig.controller.start()
    assert len(rig.apis) == 1
