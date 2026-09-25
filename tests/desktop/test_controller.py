"""The Voice controller: lifecycle, dispatch, commands, status and events end to end.

The listener runs on its real threads over doubles for the microphone
(:class:`FakeSoundDevice`), the wakeword engine (:class:`ScriptedEngine`, fired
by the test), the speech decision (:class:`AmplitudeVad`) and the vBot server
(:class:`FakeVoiceServer`). The fake microphone delivers silence at about four
times real time unless a test feeds audio.
"""

from __future__ import annotations

import io
import logging
import threading
import time
import wave
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from desktop import settings as desktop_settings
from desktop.wakeword.config import PhraseConfig
from desktop.wakeword.controller import VoiceControlError, VoiceController, VoiceRuntime
from tests.desktop.voice_fakes import (
    AmplitudeVad,
    FakeEchoStage,
    FakeSoundDevice,
    FakeVoiceServer,
    Overflow,
    ScriptedEngine,
    tone,
    wait_until,
)

SERVER = "http://pi.lan:9000"
OKAY = "builtin/okay_nabu"
HEY = "builtin/hey_nabu"

STATUS_KEYS = {
    "enabled",
    "mode",
    "state",
    "error_code",
    "sequence",
    "microphone",
    "active_microphone",
    "echo_cancellation",
    "default_agent_id",
    "default_session_behavior",
    "phrases",
    "recording",
    "commands",
    "calibration",
    "limits",
}


class RecordingSink:
    """VoiceEventSink double keeping every push in order."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pushes: list[tuple[str, dict[str, Any]]] = []

    def publish_status(self, status: Any) -> None:
        with self._lock:
            self._pushes.append(("status", dict(status)))

    def publish_event(self, event: Any) -> None:
        with self._lock:
            self._pushes.append(("event", dict(event)))

    @property
    def pushes(self) -> list[tuple[str, dict[str, Any]]]:
        with self._lock:
            return list(self._pushes)

    @property
    def statuses(self) -> list[dict[str, Any]]:
        return [payload for kind, payload in self.pushes if kind == "status"]

    @property
    def events(self) -> list[dict[str, Any]]:
        return [payload for kind, payload in self.pushes if kind == "event"]

    def kinds(self) -> list[str]:
        return [event["kind"] for event in self.events]

    def wait_for_event(self, kind: str, count: int = 1) -> dict[str, Any]:
        """Wait for the ``count``-th event of ``kind`` and return it."""

        def matching() -> list[dict[str, Any]]:
            return [event for event in self.events if event["kind"] == kind]

        wait_until(lambda: len(matching()) >= count, message=f"no {kind} event #{count}")
        return matching()[count - 1]


@dataclass
class Rig:
    voice: VoiceController
    sink: RecordingSink
    sd: FakeSoundDevice
    server: FakeVoiceServer
    live: list[tuple[str, str]]
    engines: list[ScriptedEngine]
    phrases: list[Sequence[PhraseConfig]]
    caplog: pytest.LogCaptureFixture
    now: list[float] = field(default_factory=lambda: [1000.0])
    fail_engine_start: bool = False

    @property
    def engine(self) -> ScriptedEngine:
        wait_until(lambda: bool(self.engines), message="no engine was created")
        return self.engines[-1]

    def state(self) -> tuple[str, str | None]:
        status = self.voice.status()
        return status["state"], status["error_code"]

    def wait_state(self, state: str, error_code: str | None = None) -> dict[str, Any]:
        wait_until(
            lambda: self.state() == (state, error_code),
            message=f"state never became {state} ({error_code}); it is {self.state()}",
        )
        return self.voice.status()

    def wait_ready(self, checks: int = 1) -> None:
        """Wait until the listener runs and ``checks`` readiness results were applied."""
        self.wait_state("listening")
        wait_until(
            lambda: self.caplog.text.count("Voice readiness:") >= checks,
            message="the readiness check did not finish",
        )

    def say_command(self, model_id: str = OKAY, *, speech: float = 0.4) -> dict[str, Any]:
        """Speak a wake phrase and a short command; returns ``recording_started``."""
        started = len([kind for kind in self.sink.kinds() if kind == "recording_started"])
        self.engine.fire(model_id)
        event = self.sink.wait_for_event("recording_started", started + 1)
        if speech:
            self.sd.feed(tone(speech, 16000))
        return event


def uploaded_wav(content: bytes) -> tuple[int, np.ndarray]:
    """Return the rate and samples of the WAV inside one multipart upload."""
    with wave.open(io.BytesIO(content[content.index(b"RIFF") :]), "rb") as wav_file:
        frames = wav_file.readframes(wav_file.getnframes())
        return wav_file.getframerate(), np.frombuffer(frames, dtype=np.int16)


@contextmanager
def speaking(sd: FakeSoundDevice) -> Iterator[None]:
    """Keep speech coming (short pauses only) until the block ends."""
    done = threading.Event()

    def speak() -> None:
        while not done.is_set():
            if sd.drained:
                sd.feed(tone(0.08, 16000))
            done.wait(0.02)

    thread = threading.Thread(target=speak, name="test-speaker", daemon=True)
    thread.start()
    try:
        yield
    finally:
        done.set()
        thread.join(5)


def _voice_threads(before: set[threading.Thread]) -> list[str]:
    return [
        thread.name
        for thread in threading.enumerate()
        if thread not in before and thread.name.startswith("vbot-") and thread.is_alive()
    ]


@pytest.fixture
def voice_rig(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> Iterator[Callable[..., Rig]]:
    caplog.set_level(logging.INFO, logger="vbot.desktop.wakeword.controller")
    before = set(threading.enumerate())
    rigs: list[Rig] = []

    def create(
        *,
        settings: dict[str, Any] | None = None,
        server: FakeVoiceServer | None = None,
        sd: FakeSoundDevice | None = None,
        echo_factory: Callable[[], Any] | None = None,
        mock: bool = False,
        stack_available: Callable[[], bool] | None = None,
        server_url: str = SERVER,
        frozen_clock: bool = False,
        start: bool = True,
    ) -> Rig:
        path = tmp_path / f"settings-{len(rigs)}.json"
        section: dict[str, Any] = {
            "enabled": True,
            "echo_cancellation": False,
            "server_profiles": {SERVER: {"target_agent_id": "main"}},
        }
        section.update(settings or {})
        desktop_settings.update_section(desktop_settings.WAKEWORD_KEY, lambda _: section, path)

        engines: list[ScriptedEngine] = []
        phrases_seen: list[Sequence[PhraseConfig]] = []
        live: list[tuple[str, str]] = []
        sink = RecordingSink()
        rig_ref: list[Rig] = []

        def engine_factory(
            phrases: Sequence[PhraseConfig], score_listener: Callable[[dict[str, float]], None]
        ) -> ScriptedEngine:
            engine = ScriptedEngine(
                [phrase.model_id for phrase in phrases], score_listener=score_listener
            )
            engine.fail_start = rig_ref[0].fail_engine_start
            phrases_seen.append(tuple(phrases))
            engines.append(engine)
            return engine

        server = server or FakeVoiceServer()
        sd = sd or FakeSoundDevice()
        now = [1000.0]
        runtime = VoiceRuntime(
            audio_backend=sd,
            engine_factory=engine_factory,
            speech_detector_factory=lambda: None,
            fallback_vad_factory=AmplitudeVad,
            transport=server.transport(),
            echo_stage_factory=echo_factory or (lambda: None),
            reconnect_interval=0.05,
            join_timeout=5.0,
            mock_frame_seconds=0.005,
            mock_stage_seconds=0.02,
        )
        voice = VoiceController(
            settings_path=path,
            server_url=server_url,
            sink=sink,
            live_requests=lambda action, source: live.append((action, source)),
            mock=mock,
            stack_available=stack_available or (lambda: True),
            runtime=runtime,
            clock=(lambda: now[0]) if frozen_clock else time.monotonic,
        )
        rig = Rig(voice, sink, sd, server, live, engines, phrases_seen, caplog, now)
        rig_ref.append(rig)
        rigs.append(rig)
        if start:
            voice.start()
        return rig

    yield create
    for rig in rigs:
        if rig.server.transcribe_gate is not None:
            rig.server.transcribe_gate.set()
        rig.voice.close()
    assert _voice_threads(before) == [], "Voice threads survived close()"


# -- Commands ------------------------------------------------------------------------


def test_a_spoken_command_is_recorded_transcribed_and_sent(
    voice_rig: Callable[..., Rig],
) -> None:
    rig = voice_rig()
    rig.wait_ready()

    started = rig.say_command()
    sent = rig.sink.wait_for_event("sent")

    assert rig.sink.kinds() == ["detected", "recording_started", "recording_ended", "sent"]
    assert started == {
        "sequence": started["sequence"],
        "kind": "recording_started",
        "command_id": "c1",
        "model_id": OKAY,
        "agent_id": "main",
    }
    assert sent == {
        "sequence": sent["sequence"],
        "kind": "sent",
        "command_id": "c1",
        "model_id": OKAY,
        "agent_id": "main",
        "session_id": "s-main",
    }
    assert [(params["session_id"], params["content"]) for params in rig.server.sent] == [
        ("s-main", "turn on the lights")
    ]
    rate, samples = uploaded_wav(rig.server.uploads[0])
    assert rate == 16000
    assert int(np.abs(samples.astype(np.int32)).max()) >= 1500
    statuses = rig.sink.statuses
    assert {"command_id": "c1", "model_id": OKAY, "agent_id": "main"} in [
        status["recording"] for status in statuses
    ]
    stages = [status["commands"][0]["stage"] for status in statuses if status["commands"]]
    assert list(dict.fromkeys(stages)) == ["transcribing", "sending"]
    assert rig.voice.status()["commands"] == []
    assert rig.voice.status()["recording"] is None


def test_a_cancel_phrase_discards_the_command(voice_rig: Callable[..., Rig]) -> None:
    rig = voice_rig(server=FakeVoiceServer(transcript="Licht an, ach, abbrechen"))
    rig.wait_ready()

    rig.say_command()
    cancelled = rig.sink.wait_for_event("cancelled")

    assert cancelled["command_id"] == "c1"
    assert rig.server.sent == []
    assert rig.state() == ("listening", None)


def test_no_speech_after_the_wake_phrase_sends_nothing(voice_rig: Callable[..., Rig]) -> None:
    rig = voice_rig()
    rig.wait_ready()

    rig.say_command(speech=0)
    rig.sink.wait_for_event("no_speech")

    assert rig.sink.kinds() == ["detected", "recording_started", "recording_ended", "no_speech"]
    assert rig.server.uploads == []


def test_the_user_stop_ends_the_recording_and_sends_what_was_said(
    voice_rig: Callable[..., Rig],
) -> None:
    rig = voice_rig()
    rig.wait_ready()
    rig.say_command(speech=0)

    with speaking(rig.sd):
        time.sleep(0.3)  # the speech continues, so only the stop can end the recording
        status = rig.voice.stop_recording()
        rig.sink.wait_for_event("sent")

    assert status["state"] == "listening"
    _, samples = uploaded_wav(rig.server.uploads[0])
    assert int(np.abs(samples.astype(np.int32)).max()) >= 1500


def test_the_upload_budget_ends_a_long_recording(voice_rig: Callable[..., Rig]) -> None:
    budget = 32000  # one second of 16 kHz audio
    rig = voice_rig(server=FakeVoiceServer(upload_limit=int(budget / 0.9) + 44 + 1))
    rig.wait_ready()
    rig.say_command(speech=0)

    with speaking(rig.sd):
        rig.sink.wait_for_event("sent")

    _, samples = uploaded_wav(rig.server.uploads[0])
    assert budget - 1280 * 3 < samples.nbytes <= budget


def test_a_capture_gap_discards_the_recording_and_listening_continues(
    voice_rig: Callable[..., Rig],
) -> None:
    rig = voice_rig()
    rig.wait_ready()

    rig.say_command(speech=0)
    rig.sd.feed(tone(0.2, 16000), Overflow(), tone(0.2, 16000))
    failed = rig.sink.wait_for_event("command_failed")

    assert failed["error_code"] == "microphone_read_failed"
    assert failed["command_id"] == "c1"
    assert rig.sink.kinds()[-2:] == ["recording_ended", "command_failed"]
    assert rig.server.uploads == []
    assert rig.state() == ("listening", None)

    rig.say_command()
    rig.sink.wait_for_event("sent")


def test_a_failing_command_keeps_listening(voice_rig: Callable[..., Rig]) -> None:
    rig = voice_rig(server=FakeVoiceServer(agents={}))
    rig.wait_ready()

    status = rig.voice.status()
    rig.engine.fire(OKAY)
    failed = rig.sink.wait_for_event("command_failed")

    assert [phrase["problem"] for phrase in status["phrases"]] == [
        "target_agent_unavailable",
        "target_agent_unavailable",
    ]
    assert rig.sink.kinds() == ["detected", "command_failed"]
    assert failed == {
        "sequence": failed["sequence"],
        "kind": "command_failed",
        "model_id": OKAY,
        "agent_id": "main",
        "error_code": "target_agent_unavailable",
    }
    assert rig.state() == ("listening", None)


@pytest.mark.parametrize(
    ("speech", "problem"),
    [
        ({"configured": False, "usable": False}, "speech_to_text_unconfigured"),
        ({"configured": True, "usable": False}, "speech_to_text_unavailable"),
    ],
)
def test_speech_to_text_problems_block_command_phrases_only(
    voice_rig: Callable[..., Rig], speech: dict[str, Any], problem: str
) -> None:
    rig = voice_rig(
        server=FakeVoiceServer(speech=speech),
        settings={
            "server_profiles": {
                SERVER: {
                    "target_agent_id": "main",
                    "phrase_actions": {HEY: {"type": "live_voice"}},
                }
            }
        },
    )
    rig.wait_ready()

    phrases = rig.voice.status()["phrases"]
    rig.engine.fire(OKAY)
    failed = rig.sink.wait_for_event("command_failed")

    assert {phrase["model_id"]: phrase["problem"] for phrase in phrases} == {
        OKAY: problem,
        HEY: None,
    }
    assert failed["error_code"] == problem


def test_a_phrase_without_an_agent_reports_the_missing_agent(
    voice_rig: Callable[..., Rig],
) -> None:
    rig = voice_rig(settings={"server_profiles": {}})
    rig.wait_state("listening")

    rig.engine.fire(OKAY)
    failed = rig.sink.wait_for_event("command_failed")

    assert failed["error_code"] == "missing_target_agent"
    assert "agent_id" not in failed
    assert rig.voice.status()["phrases"][0]["problem"] == "missing_target_agent"
    assert "recording_started" not in rig.sink.kinds()


def test_detection_continues_while_a_command_is_transcribed(
    voice_rig: Callable[..., Rig],
) -> None:
    gate = threading.Event()
    rig = voice_rig(server=FakeVoiceServer(transcribe_gate=gate))
    rig.wait_ready()

    rig.say_command()
    wait_until(rig.server.transcribing.is_set)
    rig.say_command()
    wait_until(lambda: len(rig.voice.status()["commands"]) == 2)
    gate.set()

    rig.sink.wait_for_event("sent", 2)
    assert sorted(event["command_id"] for event in rig.sink.events if event["kind"] == "sent") == [
        "c1",
        "c2",
    ]


# -- Live voice and phrase overlap ---------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "mode"),
    [
        ({"type": "live_voice"}, "toggle"),
        (
            {"type": "live_voice", "mode": "start"},
            "start",
        ),
    ],
)
def test_a_live_phrase_asks_the_page_for_live_voice(
    voice_rig: Callable[..., Rig], action: dict[str, Any], mode: str
) -> None:
    rig = voice_rig()
    rig.wait_ready()
    rig.voice.update_config({"phrase_actions": {HEY: action}})

    rig.engine.fire(HEY)
    rig.sink.wait_for_event("live_requested")

    assert rig.live == [(mode, "wakeword")]
    assert [(event["kind"], event.get("model_id")) for event in rig.sink.events] == [
        ("detected", HEY),
        ("live_requested", HEY),
    ]
    assert rig.voice.status()["recording"] is None


def test_during_a_recording_command_phrases_are_ignored_and_live_phrases_still_work(
    voice_rig: Callable[..., Rig],
) -> None:
    rig = voice_rig(
        settings={
            "server_profiles": {
                SERVER: {
                    "target_agent_id": "main",
                    "phrase_actions": {HEY: {"type": "live_voice", "mode": "start"}},
                }
            }
        }
    )
    rig.wait_ready()
    rig.say_command(speech=0)

    with speaking(rig.sd):
        engine = rig.engine
        engine.fire(OKAY)
        wait_until(lambda: engine.pending == 0)
        engine.fire(HEY)
        rig.sink.wait_for_event("live_requested")
        rig.voice.stop_recording()
        rig.sink.wait_for_event("sent")

    assert rig.sink.kinds() == [
        "detected",
        "recording_started",
        "detected",
        "live_requested",
        "recording_ended",
        "sent",
    ]
    assert rig.live == [("start", "wakeword")]


def test_more_than_two_phrases_listen_together(voice_rig: Callable[..., Rig]) -> None:
    active = [OKAY, HEY, "builtin/hey_jarvis", "builtin/alexa"]
    rig = voice_rig(settings={"active_model_ids": active})
    rig.wait_ready()

    rig.say_command("builtin/alexa")
    sent = rig.sink.wait_for_event("sent")

    assert list(rig.engine.model_ids) == active
    assert [phrase["model_id"] for phrase in rig.voice.status()["phrases"]] == active
    assert sent["model_id"] == "builtin/alexa"


# -- Lifecycle -------------------------------------------------------------------------


def test_a_config_change_during_a_recording_restarts_the_listener_and_drops_it(
    voice_rig: Callable[..., Rig],
) -> None:
    rig = voice_rig()
    rig.wait_ready()
    rig.say_command(speech=0)

    with speaking(rig.sd):
        rig.voice.update_config({"model_sensitivities": {OKAY: 0.7}})
        wait_until(lambda: len(rig.engines) == 2)
        rig.wait_state("listening")

    assert rig.sink.kinds() == ["detected", "recording_started", "recording_ended"]
    assert rig.phrases[-1][0] == PhraseConfig(OKAY, 0.7)
    assert rig.server.uploads == []
    assert len(rig.sd.streams) == 2
    assert rig.voice.status()["recording"] is None


def test_a_routing_change_applies_without_reopening_the_microphone(
    voice_rig: Callable[..., Rig],
) -> None:
    rig = voice_rig()
    rig.wait_ready()

    status = rig.voice.update_config({"default_session_behavior": "new"})
    rig.say_command()
    sent = rig.sink.wait_for_event("sent")

    assert status["default_session_behavior"] == "new"
    assert sent["session_id"] == "s-new"
    assert len(rig.sd.streams) == 1
    assert len(rig.engines) == 1


def test_a_server_switch_mid_command_rebuilds_the_listener_and_drops_the_old_command(
    voice_rig: Callable[..., Rig],
) -> None:
    gate = threading.Event()
    rig = voice_rig(server=FakeVoiceServer(transcribe_gate=gate))
    rig.wait_ready()
    rig.say_command()
    wait_until(rig.server.transcribing.is_set)

    rig.voice.set_server_url("http://other.lan:9000/")
    gate.set()
    wait_until(lambda: len(rig.engines) == 2)
    status = rig.wait_state("listening")

    assert status["default_agent_id"] is None
    assert status["commands"] == []
    assert status["phrases"][0]["problem"] == "missing_target_agent"
    assert "sent" not in rig.sink.kinds()
    assert rig.server.sent == []
    assert len(rig.sd.streams) == 2


def test_stopping_voice_mid_transcription_discards_the_command(
    voice_rig: Callable[..., Rig],
) -> None:
    gate = threading.Event()
    rig = voice_rig(server=FakeVoiceServer(transcribe_gate=gate))
    rig.wait_ready()
    rig.say_command()
    wait_until(rig.server.transcribing.is_set)

    assert rig.voice.set_enabled(False) == {"enabled": False, "error_code": None}
    gate.set()
    rig.wait_state("off")
    rig.voice.close()

    assert "sent" not in rig.sink.kinds()
    assert rig.server.sent == []
    assert rig.voice.status()["commands"] == []


def test_microphone_loss_and_recovery_are_announced(voice_rig: Callable[..., Rig]) -> None:
    rig = voice_rig()
    rig.wait_ready()

    rig.sd.feed(OSError("gone"), OSError("gone"), OSError("gone"))
    disconnected = rig.sink.wait_for_event("microphone_disconnected")
    rig.sink.wait_for_event("microphone_reconnected")

    assert disconnected["error_code"] == "microphone_read_failed"
    assert ("microphone_disconnected", "microphone_read_failed") in [
        (status["state"], status["error_code"]) for status in rig.sink.statuses
    ]
    rig.wait_state("listening")
    rig.say_command()
    rig.sink.wait_for_event("sent")


def test_an_engine_that_cannot_start_is_an_error_until_a_retry(
    voice_rig: Callable[..., Rig],
) -> None:
    rig = voice_rig(start=False)
    rig.fail_engine_start = True
    rig.voice.start()

    rig.wait_state("error", "engine_start_failed")
    error = rig.sink.wait_for_event("error")
    rig.fail_engine_start = False
    rig.voice.retry()

    rig.wait_state("listening")
    assert error["error_code"] == "engine_start_failed"


def test_voice_waits_for_start_and_needs_a_server(voice_rig: Callable[..., Rig]) -> None:
    rig = voice_rig(start=False, server_url="")

    assert (rig.sd.streams, rig.engines) == ([], [])
    rig.voice.start()

    rig.wait_state("error", "no_server")
    rig.voice.set_server_url(SERVER)
    rig.wait_state("listening")


def test_mock_mode_simulates_a_command_cycle_without_audio_or_network(
    voice_rig: Callable[..., Rig],
) -> None:
    rig = voice_rig(mock=True)

    status = rig.wait_state("listening")
    rig.sink.wait_for_event("sent")

    assert status["mode"] == "mock"
    assert rig.sink.kinds()[:4] == ["detected", "recording_started", "recording_ended", "sent"]
    assert rig.sd.streams == []
    assert rig.server.methods == []


def test_a_missing_voice_stack_is_reported_and_probed_again_on_retry(
    voice_rig: Callable[..., Rig],
) -> None:
    probes: list[bool] = []
    available = [False]

    def stack_available() -> bool:
        probes.append(available[0])
        return available[0]

    rig = voice_rig(stack_available=stack_available)
    status = rig.wait_state("error", "voice_stack_unavailable")
    assert status["mode"] == "unavailable"
    rig.voice.set_enabled(False)
    rig.voice.set_enabled(True)
    rig.wait_state("error", "voice_stack_unavailable")
    assert probes == [False]

    available[0] = True
    rig.voice.retry()

    assert rig.wait_state("listening")["mode"] == "real"
    assert probes == [False, True]


def test_close_stops_every_voice_thread(voice_rig: Callable[..., Rig]) -> None:
    before = {thread for thread in threading.enumerate() if thread.name.startswith("vbot-")}
    rig = voice_rig()
    rig.wait_ready()
    rig.say_command()
    wait_until(lambda: rig.voice.status()["recording"] is None)

    rig.voice.close()
    rig.voice.close()

    assert _voice_threads(before) == []
    rig.voice.start()
    assert len(rig.sd.streams) == 1


# -- Calibration ---------------------------------------------------------------------------


def test_calibration_suppresses_commands_and_throttles_its_status_pushes(
    voice_rig: Callable[..., Rig],
) -> None:
    rig = voice_rig(frozen_clock=True)
    rig.wait_ready()
    engine = rig.engine

    with pytest.raises(VoiceControlError) as inactive:
        rig.voice.restart_calibration()
    status = rig.voice.start_calibration(OKAY)
    pushes = len(rig.sink.statuses)
    engine.fire(OKAY)
    chunks = len(engine.chunks)
    wait_until(lambda: len(engine.chunks) >= chunks + 10)
    frozen_pushes = len(rig.sink.statuses) - pushes
    rig.now[0] += 0.2
    wait_until(lambda: len(rig.sink.statuses) > pushes)
    chunks = len(engine.chunks)
    wait_until(lambda: len(engine.chunks) >= chunks + 10)

    assert inactive.value.error_code == "calibration_inactive"
    assert status["calibration"]["model_id"] == OKAY
    assert status["calibration"]["phase"] == "noise"
    assert frozen_pushes == 0
    assert len(rig.sink.statuses) - pushes == 1
    assert engine.pending == 0
    assert "detected" not in rig.sink.kinds()
    assert rig.voice.stop_calibration()["calibration"] is None


def test_calibration_needs_a_listening_active_phrase(voice_rig: Callable[..., Rig]) -> None:
    rig = voice_rig()
    rig.wait_ready()

    with pytest.raises(VoiceControlError) as unknown:
        rig.voice.start_calibration("builtin/alexa")
    rig.voice.set_enabled(False)
    with pytest.raises(VoiceControlError) as off:
        rig.voice.start_calibration(OKAY)

    assert unknown.value.error_code == "calibration_unavailable"
    assert off.value.error_code == "calibration_unavailable"


# -- Echo cancellation -----------------------------------------------------------------------


def test_the_echo_stage_is_created_once_and_its_state_is_published(
    voice_rig: Callable[..., Rig],
) -> None:
    log: list[str] = []
    stages: list[FakeEchoStage] = []

    def factory() -> FakeEchoStage:
        stages.append(FakeEchoStage(log))
        return stages[-1]

    rig = voice_rig(settings={"echo_cancellation": True}, echo_factory=factory)
    rig.wait_ready()
    assert rig.voice.status()["echo_cancellation"] == {"enabled": True, "state": "active"}

    stages[0].state = "no_reference"
    wait_until(lambda: rig.voice.status()["echo_cancellation"]["state"] == "no_reference")
    rig.voice.retry()
    wait_until(lambda: len(rig.engines) == 2)
    rig.wait_ready(checks=2)
    rig.say_command()
    rig.sink.wait_for_event("sent")

    assert len(stages) == 1
    assert log.count("stage.open:16000") == 2
    assert uploaded_wav(rig.server.uploads[0])[0] == 48000


def test_an_unavailable_echo_stage_is_reported_and_not_created_again(
    voice_rig: Callable[..., Rig],
) -> None:
    calls: list[int] = []

    def factory() -> None:
        calls.append(1)
        return None

    rig = voice_rig(settings={"echo_cancellation": True}, echo_factory=factory)
    rig.wait_state("listening")
    rig.voice.retry()
    wait_until(lambda: len(rig.engines) == 2)
    status = rig.wait_state("listening")

    assert status["echo_cancellation"] == {"enabled": True, "state": "unavailable"}
    assert calls == [1]


def test_disabled_echo_cancellation_never_creates_the_stage(
    voice_rig: Callable[..., Rig],
) -> None:
    calls: list[int] = []

    def factory() -> FakeEchoStage:
        calls.append(1)
        return FakeEchoStage()

    rig = voice_rig(echo_factory=factory)
    status = rig.wait_state("listening")
    rig.voice.update_config({"echo_cancellation": True})
    wait_until(lambda: len(rig.engines) == 2)
    enabled = rig.wait_state("listening")

    assert status["echo_cancellation"] == {"enabled": False, "state": "off"}
    assert enabled["echo_cancellation"] == {"enabled": True, "state": "active"}
    assert calls == [1]


# -- Status and events -------------------------------------------------------------------------


def test_status_and_events_share_one_increasing_sequence(
    voice_rig: Callable[..., Rig],
) -> None:
    rig = voice_rig()
    rig.wait_ready()
    rig.say_command()
    rig.sink.wait_for_event("sent")

    sequences = [payload["sequence"] for _, payload in rig.sink.pushes]
    assert sequences == list(range(1, len(sequences) + 1))
    assert rig.voice.status()["sequence"] == sequences[-1]


def test_the_status_snapshot_has_the_bridge_shape(voice_rig: Callable[..., Rig]) -> None:
    rig = voice_rig()
    rig.wait_ready()

    status = rig.voice.status()

    assert set(status) == STATUS_KEYS
    assert status["mode"] == "real"
    assert status["active_microphone"] == {
        "index": 0,
        "name": "Mic",
        "host_api": "Windows WASAPI",
        "sample_rate": 16000,
    }
    assert status["phrases"][0] == {
        "model_id": OKAY,
        "label": status["phrases"][0]["label"],
        "sensitivity": 0.5,
        "action": {"type": "command", "agent_id": None, "session_behavior": None},
        "effective": {"type": "command", "agent_id": "main", "session_behavior": "active"},
        "problem": None,
    }
    assert status["limits"] == {
        "max_active_phrases": 8,
        "min_sensitivity": 0.05,
        "max_sensitivity": 0.95,
    }
    assert (status["recording"], status["commands"], status["calibration"]) == (None, [], None)
