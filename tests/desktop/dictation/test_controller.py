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
from typing import Any

import pytest

from desktop import hotkey
from desktop.dictation.controller import (
    DictationController,
)
from desktop.dictation.insertion import INSERT_CLIPBOARD, INSERT_FAILED, INSERT_PASTED
from desktop.speech.microphone import MicrophoneService
from desktop.speech.server_client import SpeechServerRejected
from tests.desktop.hotkey_test_support import FakeHotkeyApi
from tests.desktop.speech.speech_test_support import FakeSoundDevice, wait_until

TARGET_WINDOW = 7
HELD_KEYS = {ord("D"), hotkey.VK_CONTROL, hotkey.VK_MENU}
SERVER_URL = "http://server.lan:8420"
ESCAPE_REGISTRATION = (hotkey.ESCAPE_HOTKEY_ID, hotkey.MOD_NOREPEAT, hotkey.VK_ESCAPE)


class FakeServer:
    """Speech server double behind the controller's client factory."""

    def __init__(self) -> None:
        self.readiness: str | None = None
        self.transcript: str | Exception = "  Hallo Welt  "
        self.release_transcription = threading.Event()
        self.release_transcription.set()
        self.transcribing = threading.Event()
        self.uploads: list[bytes] = []
        self.urls: list[str] = []

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
        return 10_000_000

    def transcribe(self, audio: bytes, *, filename: str) -> str:
        self.server.uploads.append(audio)
        self.server.transcribing.set()
        assert self.server.release_transcription.wait(5)
        if isinstance(self.server.transcript, Exception):
            raise self.server.transcript
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

    def close(self) -> None:
        pass


@dataclass
class FakePage:
    recording: list[bool] = field(default_factory=list)

    def publish_dictation(self, recording: bool) -> None:
        self.recording.append(recording)


@dataclass
class Rig:
    controller: DictationController
    sd: FakeSoundDevice
    server: FakeServer
    inserter: FakeInserter
    cues: FakeCues
    page: FakePage
    apis: list[FakeHotkeyApi]
    path: Path

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

    def dictate(self) -> None:
        """One toggle take: start, wait for the microphone, end."""
        self.press()
        wait_until(lambda: "start" in self.cues.played)
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
                    "microphone": {"echo_cancellation": False},
                    "dictation": {"hotkey": {"enabled": True}, "mode": mode},
                }
            ),
            encoding="utf-8",
        )
        sd = FakeSoundDevice(pace=0.0025)
        server, inserter, cues, page = FakeServer(), FakeInserter(), FakeCues(), FakePage()
        apis: list[FakeHotkeyApi] = []

        def api_factory() -> FakeHotkeyApi:
            apis.append(FakeHotkeyApi())
            return apis[-1]

        controller = DictationController(
            settings_path=path,
            microphone=MicrophoneService(settings_path=path, audio_backend=sd),
            server_url=server_url,
            page=page,
            cues=cues,  # type: ignore[arg-type]
            inserter=inserter,
            client_factory=server.client,
            hotkey_supported=True,
            hotkey_api_factory=api_factory,
            **options,
        )
        controller.start()
        rig = Rig(controller, sd, server, inserter, cues, page, apis, path)
        rigs.append(rig)
        return rig

    yield make
    for rig in rigs:
        rig.server.release_transcription.set()
        rig.controller.stop()


def _wav_seconds(audio: bytes) -> float:
    with wave.open(io.BytesIO(audio)) as wav_file:
        frames: int = wav_file.getnframes()
        rate: int = wav_file.getframerate()
    return frames / rate


def test_a_toggle_take_records_until_the_second_press_and_types_the_stripped_transcript(
    make_rig: Any,
) -> None:
    rig = make_rig()

    rig.dictate()

    assert rig.inserter.inserted == [("Hallo Welt", TARGET_WINDOW)]
    assert rig.cues.played == ["start", "stop"]
    assert rig.page.recording[0] is True
    assert rig.page.recording[-1] is False
    assert len(rig.server.uploads) == 1
    assert _wav_seconds(rig.server.uploads[0]) >= 0.3
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

    rig.press()
    wait_until(lambda: "start" in rig.cues.played)
    rig.press()  # a second press while held changes nothing in hold mode
    rig.api.down -= HELD_KEYS
    rig.wait_idle()
    assert rig.inserter.inserted == [("Hallo Welt", TARGET_WINDOW)]

    rig.press()  # keys already up: the take ends before it has audio
    wait_until(lambda: "cancel" in rig.cues.played)
    rig.wait_idle()

    assert rig.cues.played == ["start", "stop", "cancel"]
    assert len(rig.server.uploads) == 1
    assert rig.controller.status()["last_failure"] is None


@pytest.mark.parametrize("during", ["recording", "transcribing"])
def test_escape_cancels_the_take_without_inserting(make_rig: Any, during: str) -> None:
    rig = make_rig()
    rig.server.release_transcription.clear()
    rig.press()
    wait_until(lambda: "start" in rig.cues.played)
    if during == "transcribing":
        rig.press()
        assert rig.server.transcribing.wait(5)
        assert rig.controller.status()["state"] == "transcribing"

    rig.escape()
    wait_until(lambda: "cancel" in rig.cues.played)
    rig.server.release_transcription.set()
    rig.wait_idle()

    assert rig.inserter.inserted == []
    assert rig.cues.played[-1] == "cancel"
    assert rig.controller.status()["last_failure"] is None


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

    assert rig.cues.played == ["start", "stop", "error"]
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
    wait_until(lambda: "error" in rig.cues.played)
    rig.wait_idle()

    assert rig.cues.played[-1] == "error"
    assert rig.server.uploads == []
    assert rig.controller.status()["last_failure"]["code"] == code


def test_a_take_ends_by_itself_at_the_length_limit_and_follows_the_server(
    make_rig: Any,
) -> None:
    rig = make_rig(max_recording_seconds=0.5)
    rig.controller.set_server_url("http://other.lan:8420")

    rig.press()
    wait_until(lambda: bool(rig.inserter.inserted))
    rig.wait_idle()

    assert rig.inserter.inserted == [("Hallo Welt", TARGET_WINDOW)]
    assert 0.5 <= _wav_seconds(rig.server.uploads[0]) < 1.0
    assert rig.server.urls == ["http://other.lan:8420"]


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
    wait_until(lambda: "start" in rig.cues.played)

    rig.controller.stop()

    assert not rig.controller.is_busy()
    assert rig.inserter.inserted == []
    rig.controller.start()
    assert len(rig.apis) == 1
