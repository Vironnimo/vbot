"""The Voice controller: lifecycle, dispatch, commands, status and events end to end.

The listener runs on its real threads over doubles for the microphone
(:class:`FedSoundDevice`), the wakeword engine (:class:`ScriptedEngine`, fired
by the test), the speech decision (:class:`CountingDetector`) and the vBot
server (:class:`FakeVoiceServer`). The microphone delivers only the audio a test
feeds, so what detection and a recording hear never depends on thread timing:
:meth:`Rig.wake`, :meth:`Rig.say_wake_phrase` and :meth:`Rig.say_command` speak
through it, and a test feeds any further audio itself.
"""

from __future__ import annotations

import io
import logging
import threading
import time
import wave
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any, override

import numpy as np
import pytest

from desktop import settings as desktop_settings
from desktop.speech.capture import AudioCapture, CaptureStatus
from desktop.speech.microphone import MicrophoneService
from desktop.wakeword.config import PhraseConfig, VoiceConfigError
from desktop.wakeword.controller import VoiceControlError, VoiceController, VoiceRuntime
from desktop.wakeword.detection import DETECTION_CHUNK_SAMPLES
from tests.desktop.speech.speech_test_support import (
    FakeEchoStage,
    FakeInputStream,
    FakeSoundDevice,
    Overflow,
    silence,
    tone,
    wait_until,
)
from tests.desktop.wakeword.voice_test_support import (
    AmplitudeDetector,
    FakeVoiceServer,
    ScriptedEngine,
)

SERVER = "http://pi.lan:9000"
OKAY = "builtin/okay_nabu"
HEY = "builtin/hey_nabu"

COMMAND_END_SILENCE = 1.6
"""Silence that ends a recording: 1 s after speech, 1.5 s without any."""
HEARD_MARKERS = (600, 1200)
"""Quiet levels (no speech) of the markers that show detection caught up; they alternate."""

STATUS_KEYS = {
    "enabled",
    "mode",
    "state",
    "error_code",
    "sequence",
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
class RecordingCues:
    played: list[str] = field(default_factory=list)

    def play(self, cue: str) -> None:
        self.played.append(cue)


class FedSoundDevice(FakeSoundDevice):
    """A microphone that delivers only the audio a test feeds.

    Reads wait while nothing is fed, so no silence runs ahead of the threads
    that consume the audio. Once the running capture's stop event is set
    (:meth:`capturing_until`), its reads return silence again so it can end;
    a listener's captures run one after another. ``feeds`` counts the
    :meth:`feed` calls.
    """

    def __init__(self) -> None:
        super().__init__(pace=0.0025)
        self._fed = threading.Condition()
        self._capture_stop = threading.Event()
        self.feeds = 0

    def capturing_until(self, stop_event: threading.Event) -> None:
        """Name the stop event of the capture that starts next."""
        with self._fed:
            self._capture_stop = stop_event

    @override
    def feed(self, *items: Any) -> None:
        super().feed(*items)
        with self._fed:
            self.feeds += 1
            self._fed.notify_all()

    @override
    def read(self, stream: FakeInputStream, frames: int) -> tuple[np.ndarray, bool]:
        with self._fed:
            stop = self._capture_stop
            while self.drained and not stop.is_set():
                self._fed.wait(0.005)  # setting the stop event does not notify
        return super().read(stream, frames)


class FedMicrophone(MicrophoneService):
    """The rig's microphone service: it tells the fed microphone which capture runs."""

    def __init__(self, *, sd: FedSoundDevice, **options: Any) -> None:
        super().__init__(audio_backend=sd, **options)
        self._sd = sd

    @override
    def create_capture(
        self,
        *,
        on_status: Callable[[CaptureStatus], None],
        stop_event: threading.Event,
        echo_cancellation: bool = True,
    ) -> AudioCapture:
        self._sd.capturing_until(stop_event)
        return super().create_capture(
            on_status=on_status, stop_event=stop_event, echo_cancellation=echo_cancellation
        )


class CountingDetector(AmplitudeDetector):
    """Speech detector double that counts the speech hops command recordings heard.

    The command recorder judges 32 ms hops (``is_speech``); detection scores
    whole chunks (``speech_probability``), which are not counted.
    """

    def __init__(self) -> None:
        super().__init__()
        self.speech_hops = 0

    @override
    def is_speech(self, pcm16: bytes) -> bool:
        speech = super().is_speech(pcm16)
        self.speech_hops += speech
        return speech

    @override
    def speech_probability(self, detection_pcm16: bytes) -> float:
        return 1.0 if super().is_speech(detection_pcm16) else 0.0


@dataclass
class Rig:
    voice: VoiceController
    microphone: MicrophoneService
    sink: RecordingSink
    sd: FedSoundDevice
    server: FakeVoiceServer
    live: list[tuple[str, str]]
    engines: list[ScriptedEngine]
    phrases: list[Sequence[PhraseConfig]]
    detectors: list[CountingDetector]
    caplog: pytest.LogCaptureFixture
    now: list[float] = field(default_factory=lambda: [1000.0])
    fail_engine_start: bool = False
    cues: RecordingCues = field(default_factory=RecordingCues)
    _heard_feeds: int = field(default=0, init=False)
    _markers: int = field(default=0, init=False)

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

    def heard_speech(self) -> bool:
        """Whether a command recording has heard speech."""
        return any(detector.speech_hops for detector in self.detectors)

    def wait_heard(self) -> None:
        """Wait until detection has handled all audio fed so far.

        Detection reads its own queue and may lag behind the capture: a quiet
        marker fed last shows when it caught up (two whole chunks at the
        marker's level; the level alternates, so the rest of the previous
        marker cannot pass for it).
        """
        if self.sd.feeds == self._heard_feeds:
            return
        engine = self.engine
        level = HEARD_MARKERS[self._markers % len(HEARD_MARKERS)]
        self._markers += 1
        scored = len(engine.peaks)
        self.sd.feed(np.full(4 * DETECTION_CHUNK_SAMPLES, level, dtype=np.int16))

        def at_level(peak: int) -> bool:
            return abs(peak - level) < level // 5  # the echo stage resamples the marker

        wait_until(
            lambda: any(
                at_level(first) and at_level(second)
                for first, second in pairwise(engine.peaks[scored:])
            ),
            message="detection never reached the fed audio",
        )
        self._heard_feeds = self.sd.feeds

    def wake(self, model_id: str = OKAY) -> None:
        """Speak a wake phrase: the engine reports it on the chunk fed with it."""
        self.wait_heard()
        self.engine.fire(model_id)
        self.sd.feed(np.zeros(DETECTION_CHUNK_SAMPLES, dtype=np.int16))

    def say_wake_phrase(self, model_id: str = OKAY) -> dict[str, Any]:
        """Speak a wake phrase that starts a recording; returns ``recording_started``.

        The recording then hears only the audio the test feeds.
        """
        started = len([kind for kind in self.sink.kinds() if kind == "recording_started"])
        self.wake(model_id)
        return self.sink.wait_for_event("recording_started", started + 1)

    def say_command(self, model_id: str = OKAY, *, speech: float = 0.4) -> dict[str, Any]:
        """Speak a wake phrase and a short command; returns ``recording_started``.

        The silence after the command ends the recording; ``speech=0`` says nothing.
        """
        event = self.say_wake_phrase(model_id)
        command = [tone(speech, 16000)] if speech else []
        self.sd.feed(*command, silence(COMMAND_END_SILENCE, 16000))
        return event


def uploaded_wav(content: bytes) -> tuple[int, np.ndarray]:
    """Return the rate and samples of the WAV inside one multipart upload."""
    with wave.open(io.BytesIO(content[content.index(b"RIFF") :]), "rb") as wav_file:
        frames = wav_file.readframes(wav_file.getnframes())
        return wav_file.getframerate(), np.frombuffer(frames, dtype=np.int16)


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
        echo_factory: Callable[[], Any] | None = None,
        echo_stage_wait: float | None = None,
        mock: bool = False,
        stack_available: Callable[[], bool] | None = None,
        server_url: str = SERVER,
        frozen_clock: bool = False,
        start: bool = True,
    ) -> Rig:
        path = tmp_path / f"settings-{len(rigs)}.json"
        section: dict[str, Any] = {
            "enabled": True,
            "server_profiles": {SERVER: {"target_agent_id": "main"}},
        }
        section.update(settings or {})
        microphone_section = {"echo_cancellation": section.pop("echo_cancellation", False)}
        desktop_settings.update_section(desktop_settings.WAKEWORD_KEY, lambda _: section, path)
        desktop_settings.update_section(
            desktop_settings.MICROPHONE_KEY, lambda _: microphone_section, path
        )

        engines: list[ScriptedEngine] = []
        phrases_seen: list[Sequence[PhraseConfig]] = []
        live: list[tuple[str, str]] = []
        detectors: list[CountingDetector] = []
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

        def speech_detector_factory() -> CountingDetector:
            detectors.append(CountingDetector())
            return detectors[-1]

        server = server or FakeVoiceServer()
        sd = FedSoundDevice()
        now = [1000.0]
        microphone = FedMicrophone(
            settings_path=path,
            sd=sd,
            echo_stage_factory=echo_factory or (lambda: None),
            echo_stage_wait=echo_stage_wait,
            reconnect_interval=0.05,
        )
        runtime = VoiceRuntime(
            engine_factory=engine_factory,
            speech_detector_factory=speech_detector_factory,
            transport=server.transport(),
            join_timeout=5.0,
            mock_frame_seconds=0.005,
            mock_stage_seconds=0.02,
        )
        cues = RecordingCues()
        voice = VoiceController(
            settings_path=path,
            microphone=microphone,
            server_url=server_url,
            sink=sink,
            live_requests=lambda action, source: live.append((action, source)),
            mock=mock,
            stack_available=stack_available or (lambda: True),
            cues=cues,  # type: ignore[arg-type]
            runtime=runtime,
            clock=(lambda: now[0]) if frozen_clock else time.monotonic,
        )
        rig = Rig(
            voice=voice,
            microphone=microphone,
            sink=sink,
            sd=sd,
            server=server,
            live=live,
            engines=engines,
            phrases=phrases_seen,
            detectors=detectors,
            caplog=caplog,
            now=now,
            cues=cues,
        )
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
    # The recording start asks the server to load its speech model meanwhile.
    wait_until(lambda: "speech.prepare_transcription" in rig.server.methods)
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
    # Status snapshots and events share one gap-free sequence.
    sequences = [payload["sequence"] for _, payload in rig.sink.pushes]
    assert sequences == list(range(1, len(sequences) + 1))
    assert rig.voice.status()["sequence"] == sequences[-1]


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
    rig.say_wake_phrase()

    rig.sd.feed(tone(0.4, 16000))  # no silence follows, so only the stop can end the recording
    wait_until(rig.heard_speech)
    status = rig.voice.stop_recording()
    rig.sink.wait_for_event("sent")

    assert status["state"] == "listening"
    _, samples = uploaded_wav(rig.server.uploads[0])
    assert int(np.abs(samples.astype(np.int32)).max()) >= 1500


def test_the_upload_budget_ends_a_long_recording(voice_rig: Callable[..., Rig]) -> None:
    budget = 32000  # one second of 16 kHz audio
    rig = voice_rig(server=FakeVoiceServer(upload_limit=int(budget / 0.9) + 44 + 1))
    rig.wait_ready()
    rig.say_wake_phrase()

    rig.sd.feed(tone(2.0, 16000))  # speech without a pause, longer than the budget
    rig.sink.wait_for_event("sent")

    _, samples = uploaded_wav(rig.server.uploads[0])
    assert budget - 1280 * 3 < samples.nbytes <= budget


def test_a_capture_gap_discards_the_recording_and_listening_continues(
    voice_rig: Callable[..., Rig],
) -> None:
    rig = voice_rig()
    rig.wait_ready()

    rig.say_wake_phrase()
    rig.sd.feed(tone(0.2, 16000), Overflow(), tone(0.2, 16000))
    failed = rig.sink.wait_for_event("command_failed")

    assert failed["error_code"] == "recording_interrupted"
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
    rig.wake()
    failed = rig.sink.wait_for_event("command_failed")

    assert [phrase["problem"] for phrase in status["phrases"]] == [
        "target_agent_unavailable",
        "target_agent_unavailable",
    ]
    assert rig.sink.kinds() == ["detected", "command_failed"]
    assert rig.cues.played == ["listen", "failed"]
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
    rig.wake()
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

    rig.wake()
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

    rig.wake(HEY)
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
    rig.say_wake_phrase()

    rig.wake(OKAY)
    rig.sd.feed(tone(0.4, 16000))
    rig.wake(HEY)
    rig.sink.wait_for_event("live_requested")
    rig.sd.feed(silence(COMMAND_END_SILENCE, 16000))
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


def test_paused_wake_phrases_are_ignored_until_resumed(voice_rig: Callable[..., Rig]) -> None:
    rig = voice_rig()
    rig.wait_ready()
    rig.voice.update_config({"phrase_actions": {HEY: {"type": "live_voice", "mode": "start"}}})

    rig.voice.pause_wake_phrases(True)
    rig.wake(OKAY)
    rig.wake(HEY)
    rig.wait_heard()
    rig.voice.pause_wake_phrases(False)
    rig.wake(HEY)
    rig.sink.wait_for_event("live_requested")

    assert rig.sink.kinds() == ["detected", "live_requested"]
    assert rig.live == [("start", "wakeword")]
    assert rig.voice.status()["recording"] is None


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
    rig.say_wake_phrase()

    rig.voice.update_config({"model_sensitivities": {OKAY: 0.7}})
    wait_until(lambda: len(rig.engines) == 2)
    rig.wait_state("listening")

    assert rig.sink.kinds() == ["detected", "recording_started", "recording_ended"]
    assert rig.phrases[-1][0] == PhraseConfig(OKAY, 0.7)
    assert rig.server.uploads == []
    assert len(rig.sd.streams) == 2
    assert rig.voice.status()["recording"] is None


def test_an_active_phrase_whose_model_is_gone_can_still_be_removed(
    voice_rig: Callable[..., Rig],
) -> None:
    rig = voice_rig(settings={"active_model_ids": [OKAY, "custom/gone"]})
    rig.wait_state("listening")

    with pytest.raises(VoiceConfigError) as added:
        rig.voice.update_config({"active_model_ids": [OKAY, "custom/gone", "custom/other"]})
    status = rig.voice.update_config({"active_model_ids": [OKAY]})

    assert added.value.field == "active_model_ids"
    assert [phrase["model_id"] for phrase in status["phrases"]] == [OKAY]


def test_a_routing_change_applies_without_reopening_the_microphone(
    voice_rig: Callable[..., Rig],
) -> None:
    rig = voice_rig()
    rig.wait_ready()

    status = rig.voice.update_config({"default_session_behavior": "new"})
    rig.say_command()
    sent = rig.sink.wait_for_event("sent")

    assert status["default_session_behavior"] == "new"
    # The command goes out for a new Session, which the server creates with it.
    assert "new_session" in rig.server.sent[-1]
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


def test_retry_refreshes_the_devices_while_no_microphone_stream_is_open(
    voice_rig: Callable[..., Rig],
) -> None:
    rig = voice_rig()
    rig.wait_state("listening")

    rig.voice.retry()
    wait_until(lambda: len(rig.engines) == 2)
    rig.wait_state("listening")

    assert rig.sd.events == [
        "stream.open",
        "stream.close",
        "terminate",
        "initialize",
        "stream.open",
    ]


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
    assert rig.cues.played[:2] == ["listen", "done"]
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
    rig.wake()
    rig.sd.feed(silence(0.8, 16000))  # ten more score frames
    rig.wait_heard()
    frozen_pushes = len(rig.sink.statuses) - pushes
    rig.now[0] += 0.2
    rig.sd.feed(silence(0.8, 16000))
    rig.wait_heard()

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
    rig.sd.feed(silence(0.04, 16000))  # the capture publishes the echo state after a block
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


def test_a_microphone_change_rebuilds_the_listener_and_disabled_echo_never_creates_the_stage(
    voice_rig: Callable[..., Rig],
) -> None:
    calls: list[int] = []

    def factory() -> FakeEchoStage:
        calls.append(1)
        return FakeEchoStage()

    rig = voice_rig(echo_factory=factory)
    status = rig.wait_state("listening")
    rig.microphone.update({"echo_cancellation": True})
    wait_until(lambda: len(rig.engines) == 2)
    enabled = rig.wait_state("listening")

    assert status["echo_cancellation"] == {"enabled": False, "state": "off"}
    assert enabled["echo_cancellation"] == {"enabled": True, "state": "active"}
    assert calls == [1]


def test_a_slow_echo_stage_does_not_delay_listening(voice_rig: Callable[..., Rig]) -> None:
    ready = threading.Event()

    def factory() -> FakeEchoStage:
        assert ready.wait(5)
        return FakeEchoStage()

    rig = voice_rig(
        settings={"echo_cancellation": True}, echo_factory=factory, echo_stage_wait=0.05
    )
    try:
        listening = rig.wait_state("listening")
    finally:
        ready.set()

    def attached() -> bool:
        if rig.sd.drained:
            rig.sd.feed(silence(0.04, 16000))  # the capture attaches a ready stage between blocks
        return bool(rig.voice.status()["echo_cancellation"]["state"] == "active")

    wait_until(attached)
    rig.wait_ready()
    rig.say_command()
    rig.sink.wait_for_event("sent")

    assert listening["echo_cancellation"] == {"enabled": True, "state": "starting"}
    assert uploaded_wav(rig.server.uploads[0])[0] == 48000


# -- Status snapshot -------------------------------------------------------------------------


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
