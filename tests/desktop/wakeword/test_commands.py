"""Recording a spoken command, then transcribing and sending it."""

from __future__ import annotations

import io
import threading
import wave
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from typing import Any, cast

import numpy as np
import pytest

from desktop.speech.capture import CaptureSubscription
from desktop.speech.server_client import SpeechRequestCancelled, SpeechServerError
from desktop.wakeword.commands import (
    MAX_COMMAND_WORKERS,
    MAX_RECORDING_SECONDS,
    Command,
    CommandOutcome,
    CommandPipeline,
    CommandRecorder,
    RecordingResult,
    is_voice_cancel_phrase,
)
from desktop.wakeword.server_client import VoiceServerClient
from tests.desktop.speech.speech_test_support import (
    FakeSubscription,
    silence,
    tone,
    wait_until,
)
from tests.desktop.wakeword.voice_test_support import (
    AmplitudeDetector,
)

QUIET = 100  # audible background below the speech level


def _wav(result: RecordingResult) -> tuple[int, bytes]:
    assert result.wav is not None
    with wave.open(io.BytesIO(result.wav), "rb") as wav_file:
        assert (wav_file.getnchannels(), wav_file.getsampwidth()) == (1, 2)
        return wav_file.getframerate(), wav_file.readframes(wav_file.getnframes())


def _quiet(seconds: float) -> np.ndarray:
    return np.full(round(seconds * 16000), QUIET, dtype=np.int16)


# -- Helpers -----------------------------------------------------------------------------


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


@pytest.fixture
def harness() -> Iterator[Callable[..., RecorderHarness]]:
    created: list[RecorderHarness] = []

    def create(*, detector: Any = None) -> RecorderHarness:
        calls: list[str] = []
        budget = [10_000_000]

        def detector_factory() -> Any:
            calls.append("detector")
            return detector or AmplitudeDetector()

        stop = threading.Event()
        state = RecorderHarness(
            recorder=CommandRecorder(
                stop_event=stop,
                speech_detector_factory=detector_factory,
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


@pytest.mark.parametrize(
    ("reason", "error_code"),
    [
        ("read_failed", "microphone_read_failed"),
        ("overflow", "recording_interrupted"),
        ("overrun", "recording_interrupted"),
        ("history", "recording_interrupted"),
        ("echo_failed", "recording_interrupted"),
        ("echo_attached", "recording_interrupted"),
    ],
)
def test_a_capture_gap_discards_the_recording(
    harness: Callable[..., RecorderHarness], reason: str, error_code: str
) -> None:
    rig = harness()
    recording = rig.record()

    recording.subscription.push_audio(tone(0.2, 16000))
    recording.subscription.push_gap(reason)

    assert recording.result() == RecordingResult("failed", error_code=error_code)


def test_a_rate_change_discards_the_recording(harness: Callable[..., RecorderHarness]) -> None:
    rig = harness()
    recording = rig.record()

    recording.subscription.push_audio(tone(0.2, 16000), recording_rate=48000)
    recording.subscription.push_audio(tone(0.2, 16000))

    assert recording.result() == RecordingResult("failed", error_code="recording_interrupted")


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


def test_a_recording_that_never_pauses_ends_at_the_maximum_duration(
    harness: Callable[..., RecorderHarness],
) -> None:
    rig = harness()
    recording = rig.record()

    recording.subscription.push_audio(tone(MAX_RECORDING_SECONDS + 5, 16000))
    result = recording.result()

    assert result.outcome == "audio"
    seconds = len(_wav(result)[1]) / 2 / 16000
    assert MAX_RECORDING_SECONDS <= seconds <= MAX_RECORDING_SECONDS + 0.04


def test_one_recording_at_a_time_and_the_speech_detector_is_reused(
    harness: Callable[..., RecorderHarness],
) -> None:
    detector = AmplitudeDetector()
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
    assert rig.factory_calls == ["detector"]
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


def test_the_result_handler_can_already_start_the_next_recording(
    harness: Callable[..., RecorderHarness],
) -> None:
    rig = harness()
    second = Recording(FakeSubscription())
    started: list[bool] = []

    def on_done(_result: RecordingResult) -> None:
        started.append(
            rig.recorder.start([], cast(CaptureSubscription, second.subscription), second.on_done)
        )

    first = FakeSubscription()
    assert rig.recorder.start([], cast(CaptureSubscription, first), on_done)
    first.close()
    wait_until(lambda: bool(started))
    second.subscription.close()

    assert started == [True]
    assert second.result() == RecordingResult("cancelled")
    assert rig.recorder.join(5)


# -- Pipeline ----------------------------------------------------------------------------


class FakeClient:
    """VoiceServerClient double; ``gate`` holds every transcription until set."""

    def __init__(self) -> None:
        self.transcript = "turn on the lights"
        self.transcribe_error: BaseException | None = None
        self.resolve_error: BaseException | None = None
        self.gate: threading.Event | None = None
        self.transcribe_gates: dict[bytes, threading.Event] = {}
        self.transcripts: dict[bytes, str | BaseException] = {}
        self.send_gates: dict[str, threading.Event] = {}
        self.send_errors: dict[str, BaseException] = {}
        self.lock = threading.Lock()
        self.transcribing = 0
        self.uploads: list[bytes] = []
        self.resolved: list[str] = []
        self.sending: list[str] = []
        self.sent: list[tuple[str, str | None, str]] = []

    def transcribe(self, audio: bytes) -> str:
        with self.lock:
            self.uploads.append(audio)
            self.transcribing += 1
        try:
            if self.gate is not None:
                assert self.gate.wait(5)
            if audio in self.transcribe_gates:
                assert self.transcribe_gates[audio].wait(5)
            if self.transcribe_error is not None:
                raise self.transcribe_error
            result = self.transcripts.get(audio, self.transcript)
            if isinstance(result, BaseException):
                raise result
            return result
        finally:
            with self.lock:
                self.transcribing -= 1

    def resolve_session(self, agent_id: str, session_behavior: str) -> str:
        with self.lock:
            self.resolved.append(agent_id)
        if self.resolve_error is not None:
            raise self.resolve_error
        return f"s-{agent_id}-{session_behavior}"

    def send_command(self, agent_id: str, session_id: str | None, text: str) -> str:
        with self.lock:
            self.sending.append(text)
        if text in self.send_gates:
            assert self.send_gates[text].wait(5)
        if text in self.send_errors:
            raise self.send_errors[text]
        with self.lock:
            self.sent.append((agent_id, session_id, text))
        return session_id or f"s-{agent_id}-created"


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
        for gate in [*state.client.transcribe_gates.values(), *state.client.send_gates.values()]:
            gate.set()
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
                client, "transcribe_error", SpeechServerError("server_unreachable", "down")
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
                client, "resolve_error", SpeechServerError("target_agent_unavailable", "gone")
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
    rig.client.transcribe_error = SpeechRequestCancelled()

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


@pytest.mark.parametrize("warm", [False, True])
def test_commands_run_on_at_most_three_workers(
    pipeline_harness: Callable[..., PipelineHarness],
    warm: bool,
) -> None:
    rig = pipeline_harness()
    if warm:
        assert rig.pipeline.submit(_command("warm-up"))
        wait_until(lambda: bool(rig.outcomes) and rig.pipeline._idle_workers == 1)
        rig.outcomes.clear()
    rig.client.gate = threading.Event()

    # Deliver one burst before idle workers can wake and consume it.
    with rig.pipeline._condition:
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


def test_commands_send_in_submission_order_per_agent_without_waiting_workers(
    pipeline_harness: Callable[..., PipelineHarness],
) -> None:
    rig = pipeline_harness()
    transcribe_first = threading.Event()
    send_first = threading.Event()
    rig.client.transcribe_gates[b"first"] = transcribe_first
    rig.client.send_gates["first"] = send_first
    command_ids = ["first", "second", "third", "fourth", "other", "other-again"]
    rig.client.transcripts = {command_id.encode(): command_id for command_id in command_ids}

    assert rig.pipeline.submit(replace(_command("first"), wav=b"first"))
    wait_until(lambda: rig.client.transcribing == 1)
    for command_id in command_ids[1:-1]:
        assert rig.pipeline.submit(
            replace(
                _command(command_id),
                wav=command_id.encode(),
                agent_id="other" if command_id == "other" else "main",
            )
        )
    wait_until(lambda: any(outcome.command_id == "other" for outcome in rig.outcomes))

    # Later ready transcripts must not consume workers waiting for the first one.
    assert rig.client.resolved == ["other"]
    transcribe_first.set()
    wait_until(lambda: "first" in rig.client.sending)
    assert rig.pipeline.submit(
        replace(_command("other-again"), wav=b"other-again", agent_id="other")
    )
    wait_until(lambda: any(outcome.command_id == "other-again" for outcome in rig.outcomes))

    # Session resolution is ordered too, including while the prior send is in flight.
    assert rig.client.resolved == ["other", "main", "other"]
    send_first.set()
    wait_until(lambda: len(rig.outcomes) == len(command_ids))
    assert [text for agent_id, _, text in rig.client.sent if agent_id == "main"] == command_ids[:4]
    assert [outcome.command_id for outcome in rig.outcomes if outcome.agent_id == "main"] == (
        command_ids[:4]
    )


@pytest.mark.parametrize(
    ("transcript", "send_error", "kind"),
    [
        (SpeechServerError("server_unreachable", "down"), None, "transcription_failed"),
        (ValueError("bug"), None, "command_failed"),
        (" ", None, "no_speech"),
        ("abbrechen", None, "cancelled"),
        (SpeechRequestCancelled(), None, None),
        ("failed", SpeechServerError("send_failed", "down"), "command_failed"),
    ],
    ids=["transcription-failed", "bug", "empty", "cancel-phrase", "cancelled", "send-failed"],
)
@pytest.mark.parametrize("position", ["first", "middle"])
def test_a_command_without_a_delivery_releases_later_commands(
    pipeline_harness: Callable[..., PipelineHarness],
    transcript: str | BaseException,
    send_error: BaseException | None,
    kind: str | None,
    position: str,
) -> None:
    rig = pipeline_harness()
    release_first = threading.Event()
    rig.client.transcribe_gates[b"first"] = release_first
    failed_id = "first" if position == "first" else "middle"
    rig.client.transcripts = {b"first": "first", failed_id.encode(): transcript}
    if send_error is not None:
        rig.client.send_errors["failed"] = send_error

    assert rig.pipeline.submit(replace(_command("first"), wav=b"first"))
    wait_until(lambda: rig.client.transcribing == 1)
    if position == "middle":
        assert rig.pipeline.submit(replace(_command("middle"), wav=b"middle"))
    assert rig.pipeline.submit(_command("last"))
    assert rig.pipeline.submit(replace(_command("other"), agent_id="other"))
    wait_until(lambda: any(outcome.command_id == "other" for outcome in rig.outcomes))

    release_first.set()
    wait_until(lambda: any(outcome.command_id == "last" for outcome in rig.outcomes))
    assert [text for agent_id, _, text in rig.client.sent if agent_id == "main"] == (
        (["first"] if position == "middle" else []) + ["turn on the lights"]
    )
    assert [outcome.kind for outcome in rig.outcomes if outcome.command_id == failed_id] == (
        [] if kind is None else [kind]
    )


@pytest.mark.parametrize("later_transcripts_ready", [False, True])
def test_close_waits_until_its_deadline_and_discards_queued_commands(
    pipeline_harness: Callable[..., PipelineHarness],
    later_transcripts_ready: bool,
) -> None:
    rig = pipeline_harness()
    release_busy = threading.Event()
    if later_transcripts_ready:
        rig.client.transcribe_gates[b"busy"] = release_busy
    else:
        rig.client.gate = release_busy
    rig.pipeline.submit(replace(_command("busy"), wav=b"busy"))
    wait_until(lambda: rig.client.transcribing == 1)
    for index in range(MAX_COMMAND_WORKERS + 1):
        rig.pipeline.submit(_command(f"queued-{index}"))
    if later_transcripts_ready:
        wait_until(lambda: len(rig.client.uploads) == MAX_COMMAND_WORKERS + 2)
        wait_until(lambda: rig.client.transcribing == 1)

    assert rig.pipeline.close(0.05) is False
    release_busy.set()

    assert rig.pipeline.close(5) is True
    if not later_transcripts_ready:
        assert {command_id for command_id, _ in rig.stages} <= {"busy", "queued-0", "queued-1"}
    assert rig.client.sent == []
    assert rig.outcomes == []
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
