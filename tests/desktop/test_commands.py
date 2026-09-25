"""Recording a spoken command, then transcribing and sending it."""

from __future__ import annotations

import io
import threading
import wave
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, cast

import numpy as np
import pytest

from desktop.wakeword.capture import CaptureSubscription
from desktop.wakeword.commands import (
    MAX_COMMAND_WORKERS,
    Command,
    CommandOutcome,
    CommandPipeline,
    CommandRecorder,
    RecordingResult,
    encode_wav,
    is_voice_cancel_phrase,
)
from desktop.wakeword.server_client import (
    VoiceRequestCancelled,
    VoiceServerClient,
    VoiceServerError,
)
from tests.desktop.voice_fakes import AmplitudeVad, FakeSubscription, silence, tone, wait_until

QUIET = 100  # audible background below the speech level


def _wav(result: RecordingResult) -> tuple[int, bytes]:
    assert result.wav is not None
    with wave.open(io.BytesIO(result.wav), "rb") as wav_file:
        assert (wav_file.getnchannels(), wav_file.getsampwidth()) == (1, 2)
        return wav_file.getframerate(), wav_file.readframes(wav_file.getnframes())


def _quiet(seconds: float) -> np.ndarray:
    return np.full(round(seconds * 16000), QUIET, dtype=np.int16)


# -- Helpers -----------------------------------------------------------------------------


def test_encode_wav_wraps_pcm_at_the_given_rate() -> None:
    pcm = tone(0.1, 48000).tobytes()

    with wave.open(io.BytesIO(encode_wav(pcm, 48000)), "rb") as wav_file:
        assert wav_file.getframerate() == 48000
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.readframes(wav_file.getnframes()) == pcm


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        ("Abbrechen", True),
        ("abbrechen.", True),
        ("Vergiss es!", True),
        ("Mach das Licht an, ach, abbrechen", True),
        ("  vergiss   es ", True),
        ("abbrechen und weiter", False),
        ("nicht abbrechenden", False),
        ("turn on the lights", False),
        ("", False),
    ],
)
def test_cancel_phrases_end_or_are_the_transcript(transcript: str, expected: bool) -> None:
    assert is_voice_cancel_phrase(transcript) is expected


# -- Recorder ----------------------------------------------------------------------------


@dataclass
class Recording:
    subscription: FakeSubscription
    results: list[RecordingResult] = field(default_factory=list)
    done: threading.Event = field(default_factory=threading.Event)

    def on_done(self, result: RecordingResult) -> None:
        self.results.append(result)
        self.done.set()

    def result(self) -> RecordingResult:
        assert self.done.wait(5), "the recording did not end"
        return self.results[0]


@dataclass
class RecorderHarness:
    recorder: CommandRecorder
    stop: threading.Event
    budget: list[int]
    factory_calls: list[str]

    def record(
        self, pre_roll: list[Any] | None = None, subscription: FakeSubscription | None = None
    ) -> Recording:
        recording = Recording(subscription or FakeSubscription())
        assert self.recorder.start(
            pre_roll or [], cast(CaptureSubscription, recording.subscription), recording.on_done
        )
        return recording


class CountingDetector:
    """Neural detector double deciding by amplitude; counts resets."""

    def __init__(self) -> None:
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1

    def is_speech(self, pcm16: bytes) -> bool:
        samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.int32)
        return bool(samples.size) and int(np.abs(samples).max()) >= 1500


@pytest.fixture
def harness() -> Iterator[Callable[..., RecorderHarness]]:
    created: list[RecorderHarness] = []

    def create(*, detector: Any = None) -> RecorderHarness:
        calls: list[str] = []
        budget = [10_000_000]

        def detector_factory() -> Any:
            calls.append("detector")
            return detector

        def vad_factory() -> Any:
            calls.append("vad")
            return AmplitudeVad()

        stop = threading.Event()
        state = RecorderHarness(
            recorder=CommandRecorder(
                stop_event=stop,
                speech_detector_factory=detector_factory,
                fallback_vad_factory=vad_factory,
                budget_bytes=lambda: budget[0],
            ),
            stop=stop,
            budget=budget,
            factory_calls=calls,
        )
        created.append(state)
        return state

    yield create
    for state in created:
        state.stop.set()
        assert state.recorder.join(5), "recorder thread did not stop"


def test_speech_then_silence_keeps_the_pre_roll_and_ends_after_a_second(
    harness: Callable[..., RecorderHarness],
) -> None:
    rig = harness()
    subscription = FakeSubscription()
    pre_roll = subscription.blocks(_quiet(0.32), recording_rate=48000)
    recording = rig.record(pre_roll, subscription)

    subscription.push_audio(tone(0.4, 16000), recording_rate=48000)
    subscription.push_audio(silence(1.5, 16000), recording_rate=48000)
    result = recording.result()

    assert result.outcome == "audio"
    rate, frames = _wav(result)
    assert rate == 48000
    pre_roll_bytes = b"".join(block.recording for block in pre_roll)
    assert frames.startswith(pre_roll_bytes)
    seconds = len(frames) / 2 / 48000
    assert 0.32 + 0.4 + 0.95 <= seconds <= 0.32 + 0.4 + 1.1
    assert recording.subscription.closed


def test_long_waits_before_speech_keep_only_the_last_pre_speech_audio(
    harness: Callable[..., RecorderHarness],
) -> None:
    rig = harness()
    recording = rig.record()

    recording.subscription.push_audio(_quiet(1.0))
    speech = recording.subscription.push_audio(tone(0.2, 16000))
    recording.subscription.push_audio(silence(1.2, 16000))
    result = recording.result()

    _, frames = _wav(result)
    before_speech = frames[: frames.index(speech[0].recording)]
    assert 0.4 <= len(before_speech) / 2 / 16000 <= 0.44  # whole 40 ms blocks


def test_no_speech_within_the_start_timeout(harness: Callable[..., RecorderHarness]) -> None:
    rig = harness()
    recording = rig.record()

    recording.subscription.push_audio(_quiet(2.0))

    assert recording.result() == RecordingResult("no_speech")


def test_a_user_stop_keeps_the_audio_captured_so_far(
    harness: Callable[..., RecorderHarness],
) -> None:
    rig = harness()
    recording = rig.record()
    speech = recording.subscription.push_audio(tone(0.4, 16000))
    wait_until(lambda: recording.subscription.empty)

    rig.recorder.stop()

    result = recording.result()
    assert result.outcome == "audio"
    assert _wav(result)[1] == b"".join(block.recording for block in speech)


def test_a_capture_gap_discards_the_recording(harness: Callable[..., RecorderHarness]) -> None:
    rig = harness()
    recording = rig.record()

    recording.subscription.push_audio(tone(0.2, 16000))
    recording.subscription.push_gap("read_failed")

    assert recording.result() == RecordingResult("failed", error_code="microphone_read_failed")


def test_a_rate_change_discards_the_recording(harness: Callable[..., RecorderHarness]) -> None:
    rig = harness()
    recording = rig.record()

    recording.subscription.push_audio(tone(0.2, 16000), recording_rate=48000)
    recording.subscription.push_audio(tone(0.2, 16000))

    assert recording.result() == RecordingResult("failed", error_code="microphone_read_failed")


def test_stopping_the_listener_cancels_the_recording(
    harness: Callable[..., RecorderHarness],
) -> None:
    rig = harness()
    recording = rig.record()
    recording.subscription.push_audio(tone(0.2, 16000))

    rig.stop.set()

    assert recording.result() == RecordingResult("cancelled")


def test_an_ended_capture_cancels_the_recording(harness: Callable[..., RecorderHarness]) -> None:
    rig = harness()
    recording = rig.record()

    recording.subscription.close()

    assert recording.result() == RecordingResult("cancelled")


def test_the_upload_budget_ends_the_recording(harness: Callable[..., RecorderHarness]) -> None:
    rig = harness()
    rig.budget[0] = 16000  # half a second of 16 kHz audio
    recording = rig.record()

    recording.subscription.push_audio(tone(3.0, 16000))
    result = recording.result()

    assert result.outcome == "audio"
    assert 16000 - 1280 < len(_wav(result)[1]) <= 16000


def test_one_recording_at_a_time_and_speech_deciders_are_reused(
    harness: Callable[..., RecorderHarness],
) -> None:
    detector = CountingDetector()
    rig = harness(detector=detector)
    first = rig.record()

    assert not rig.recorder.start([], cast(CaptureSubscription, FakeSubscription()), lambda _: None)

    first.subscription.push_audio(_quiet(2.0))
    assert first.result().outcome == "no_speech"
    assert rig.recorder.join(5)
    second = rig.record()
    second.subscription.push_audio(tone(0.2, 16000))
    second.subscription.push_audio(silence(1.2, 16000))

    assert second.result().outcome == "audio"
    assert rig.factory_calls == ["detector", "vad"]
    assert detector.resets == 2


def test_the_result_handler_runs_on_the_recorder_thread_and_may_fail(
    harness: Callable[..., RecorderHarness],
) -> None:
    rig = harness()
    threads: list[threading.Thread] = []

    def on_done(_result: RecordingResult) -> None:
        threads.append(threading.current_thread())
        raise RuntimeError("handler broke")

    subscription = FakeSubscription()
    assert rig.recorder.start([], cast(CaptureSubscription, subscription), on_done)
    subscription.close()

    assert rig.recorder.join(5)
    assert [thread.name for thread in threads] == ["vbot-voice-recorder"]
    assert threads[0].daemon


# -- Pipeline ----------------------------------------------------------------------------


class FakeClient:
    """VoiceServerClient double; ``gate`` holds every transcription until set."""

    def __init__(self) -> None:
        self.transcript = "turn on the lights"
        self.transcribe_error: BaseException | None = None
        self.resolve_error: BaseException | None = None
        self.gate: threading.Event | None = None
        self.lock = threading.Lock()
        self.transcribing = 0
        self.uploads: list[bytes] = []
        self.sent: list[tuple[str, str, str]] = []

    def transcribe(self, audio: bytes) -> str:
        with self.lock:
            self.uploads.append(audio)
            self.transcribing += 1
        try:
            if self.gate is not None:
                assert self.gate.wait(5)
            if self.transcribe_error is not None:
                raise self.transcribe_error
            return self.transcript
        finally:
            with self.lock:
                self.transcribing -= 1

    def resolve_session(self, agent_id: str, session_behavior: str) -> str:
        if self.resolve_error is not None:
            raise self.resolve_error
        return f"s-{agent_id}-{session_behavior}"

    def send_command(self, agent_id: str, session_id: str, text: str) -> None:
        with self.lock:
            self.sent.append((agent_id, session_id, text))


@dataclass
class PipelineHarness:
    pipeline: CommandPipeline
    client: FakeClient
    stop: threading.Event
    stages: list[tuple[str, str]] = field(default_factory=list)
    outcomes: list[CommandOutcome] = field(default_factory=list)


def _command(command_id: str = "c1") -> Command:
    return Command(command_id, "builtin/okay_nabu", "main", "continue", b"RIFF-audio")


@pytest.fixture
def pipeline_harness() -> Iterator[Callable[..., PipelineHarness]]:
    created: list[PipelineHarness] = []

    def create(*, on_outcome: Callable[[CommandOutcome], None] | None = None) -> PipelineHarness:
        client = FakeClient()
        stop = threading.Event()
        stages: list[tuple[str, str]] = []
        outcomes: list[CommandOutcome] = []
        state = PipelineHarness(
            pipeline=CommandPipeline(
                cast(VoiceServerClient, client),
                stop_event=stop,
                on_stage=lambda command_id, stage: stages.append((command_id, stage)),
                on_outcome=on_outcome or outcomes.append,
            ),
            client=client,
            stop=stop,
            stages=stages,
            outcomes=outcomes,
        )
        created.append(state)
        return state

    yield create
    for state in created:
        if state.client.gate is not None:
            state.client.gate.set()
        assert state.pipeline.close(5), "command workers did not stop"


def test_a_command_is_transcribed_and_sent_to_its_session(
    pipeline_harness: Callable[..., PipelineHarness],
) -> None:
    rig = pipeline_harness()

    assert rig.pipeline.submit(_command())
    wait_until(lambda: bool(rig.outcomes))

    assert rig.stages == [("c1", "transcribing"), ("c1", "sending")]
    assert rig.client.uploads == [b"RIFF-audio"]
    assert rig.client.sent == [("main", "s-main-continue", "turn on the lights")]
    assert rig.outcomes == [
        CommandOutcome(
            "c1", "builtin/okay_nabu", "sent", agent_id="main", session_id="s-main-continue"
        )
    ]


@pytest.mark.parametrize(
    ("configure", "expected"),
    [
        (
            lambda client: setattr(
                client, "transcribe_error", VoiceServerError("server_unreachable", "down")
            ),
            CommandOutcome(
                "c1", "builtin/okay_nabu", "transcription_failed", error_code="server_unreachable"
            ),
        ),
        (
            lambda client: setattr(client, "transcript", "   "),
            CommandOutcome("c1", "builtin/okay_nabu", "no_speech"),
        ),
        (
            lambda client: setattr(client, "transcript", "Licht an, vergiss es"),
            CommandOutcome("c1", "builtin/okay_nabu", "cancelled"),
        ),
        (
            lambda client: setattr(
                client, "resolve_error", VoiceServerError("target_agent_unavailable", "gone")
            ),
            CommandOutcome(
                "c1",
                "builtin/okay_nabu",
                "command_failed",
                agent_id="main",
                error_code="target_agent_unavailable",
            ),
        ),
        (
            lambda client: setattr(client, "resolve_error", ValueError("bug")),
            CommandOutcome(
                "c1",
                "builtin/okay_nabu",
                "command_failed",
                agent_id="main",
                error_code="pipeline_failed",
            ),
        ),
    ],
    ids=["transcription-failed", "empty-transcript", "cancel-phrase", "server-error", "bug"],
)
def test_each_command_ends_with_exactly_one_outcome(
    pipeline_harness: Callable[..., PipelineHarness],
    configure: Callable[[FakeClient], None],
    expected: CommandOutcome,
) -> None:
    rig = pipeline_harness()
    configure(rig.client)

    rig.pipeline.submit(_command())
    wait_until(lambda: bool(rig.outcomes))

    assert rig.outcomes == [expected]
    assert rig.client.sent == []


def test_a_cancelled_request_publishes_nothing(
    pipeline_harness: Callable[..., PipelineHarness],
) -> None:
    rig = pipeline_harness()
    rig.client.transcribe_error = VoiceRequestCancelled()

    rig.pipeline.submit(_command())
    wait_until(lambda: rig.client.uploads != [] and rig.client.transcribing == 0)

    assert rig.pipeline.close(5)
    assert rig.outcomes == []


def test_stopping_mid_transcription_discards_the_command(
    pipeline_harness: Callable[..., PipelineHarness],
) -> None:
    rig = pipeline_harness()
    rig.client.gate = threading.Event()
    rig.pipeline.submit(_command())
    wait_until(lambda: rig.client.transcribing == 1)

    rig.stop.set()
    rig.client.gate.set()

    assert rig.pipeline.close(5)
    assert rig.client.sent == []
    assert rig.outcomes == []
    assert rig.stages == [("c1", "transcribing")]
    assert not rig.pipeline.submit(_command("c2"))


def test_commands_run_on_at_most_three_workers(
    pipeline_harness: Callable[..., PipelineHarness],
) -> None:
    rig = pipeline_harness()
    rig.client.gate = threading.Event()

    for index in range(5):
        rig.pipeline.submit(_command(f"c{index}"))
    wait_until(lambda: rig.client.transcribing == MAX_COMMAND_WORKERS)
    workers = sorted(
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith("vbot-voice-command-")
    )
    rig.client.gate.set()
    wait_until(lambda: len(rig.outcomes) == 5)

    assert workers == ["vbot-voice-command-1", "vbot-voice-command-2", "vbot-voice-command-3"]
    assert sorted(outcome.command_id for outcome in rig.outcomes) == [f"c{i}" for i in range(5)]


def test_close_waits_until_its_deadline_and_discards_queued_commands(
    pipeline_harness: Callable[..., PipelineHarness],
) -> None:
    rig = pipeline_harness()
    rig.client.gate = threading.Event()
    rig.pipeline.submit(_command("busy"))
    wait_until(lambda: rig.client.transcribing == 1)
    for index in range(MAX_COMMAND_WORKERS + 1):
        rig.pipeline.submit(_command(f"queued-{index}"))

    assert rig.pipeline.close(0.05) is False
    rig.client.gate.set()

    assert rig.pipeline.close(5) is True
    assert {command_id for command_id, _ in rig.stages} <= {"busy", "queued-0", "queued-1"}
    assert not rig.pipeline.submit(_command("late"))


def test_a_failing_outcome_handler_keeps_the_worker(
    pipeline_harness: Callable[..., PipelineHarness],
) -> None:
    seen: list[str] = []

    def on_outcome(outcome: CommandOutcome) -> None:
        seen.append(outcome.command_id)
        raise RuntimeError("handler broke")

    rig = pipeline_harness(on_outcome=on_outcome)

    rig.pipeline.submit(_command("c1"))
    wait_until(lambda: seen == ["c1"])
    rig.pipeline.submit(_command("c2"))

    wait_until(lambda: seen == ["c1", "c2"])
