"""Level orchestration: scripted directives, result folders and the measured phase."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import scripts.perf_load_suite.runner as runner
from scripts.perf_load_suite.driver import SessionTarget, Workload
from scripts.perf_load_suite.metrics import RunRecord, ToolTiming, analyze_level
from scripts.perf_load_suite.recording import RecordingError


def test_turn_directives_carry_level_session_and_turn():
    config = runner.LoadConfig(levels=(10,), steps=3, tokens=50, rate=40, think_ms=100)

    directive = config.turn_directive(10, 7, 1)

    assert directive.tag == "L10-s007-t2"
    assert (directive.steps, directive.tokens, directive.rate, directive.think_ms) == (
        3,
        50,
        40,
        100,
    )


def test_history_is_warmed_up_in_bounded_chunks():
    config = runner.LoadConfig(levels=(1,), history_tokens=45_000)

    assert config.warmup_turns() == 3
    chunks = [config.warmup_directive(1, 0, index) for index in range(3)]
    assert [chunk.warmup_tokens for chunk in chunks] == [20_000, 20_000, 5_000]
    assert [chunk.tag for chunk in chunks] == ["L1-s000-w1", "L1-s000-w2", "L1-s000-w3"]
    assert runner.LoadConfig(levels=(1,)).warmup_turns() == 0


@pytest.mark.parametrize(
    "fields",
    [
        {"levels": ()},
        {"levels": (0,)},
        {"levels": (5, 5)},
        {"turns": 0},
        {"steps": 0},
        {"calls": 99},
        {"recording_max_seconds": 0},
        {"recording_max_seconds": 3601},
    ],
)
def test_invalid_configuration_is_rejected(fields):
    with pytest.raises(ValueError):
        runner.LoadConfig(**fields)


def test_profile_targets_the_highest_level():
    assert runner.LoadConfig(levels=(1, 30, 10), profile=True).profiled_level == 30
    assert runner.LoadConfig(levels=(1, 30, 10)).profiled_level is None


def test_result_folders_never_collide(tmp_path):
    started = datetime(2026, 9, 24, 13, 50, 8, tzinfo=UTC)

    first = runner.create_run_dir(tmp_path, started)
    second = runner.create_run_dir(tmp_path, started)

    assert first.name == "load-20260924T135008Z"
    assert second.name == "load-20260924T135008Z-2"


class Recorder:
    def __init__(self, events, name, result=None, error=None):
        self.events = events
        self.name = name
        self.result = result
        self.error = error

    def start(self):
        self.events.append(f"start {self.name}")

    def stop(self):
        self.events.append(f"stop {self.name}")
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def phase(monkeypatch, tmp_path):
    events = []
    state = SimpleNamespace(events=events, stop_error=None, drive_error=None)

    monkeypatch.setattr(
        runner, "start_recording", lambda rpc, **kw: events.append("start recording")
    )

    def stop_recording(rpc):
        events.append("stop recording")
        if state.stop_error is not None:
            raise state.stop_error
        return {"recording_id": "rec", "summary": {}, "trace_path": None}

    monkeypatch.setattr(runner, "stop_recording", stop_recording)
    monkeypatch.setattr(
        runner,
        "ProcessSampler",
        lambda pids: Recorder(events, "sampler", {"server": {"cpu_avg": 5.0}}),
    )
    monkeypatch.setattr(
        runner,
        "PySpyRecorder",
        lambda **kw: Recorder(events, "profiler", {"status": "ok", "flamegraph": "flamegraph.svg"}),
    )

    async def drive_turns(base_url, targets, directive_for, *, turns, timeout_seconds):
        events.append("drive")
        if state.drive_error is not None:
            raise state.drive_error
        directive = directive_for(targets[0], 0)
        return [
            RunRecord(
                tag=directive.tag,
                session_index=0,
                turn_index=0,
                agent_id="a",
                session_id="s",
                directive=directive,
                sent_at=1.0,
                finished_at=2.0,
                status="completed",
            )
        ]

    monkeypatch.setattr(runner, "drive_turns", drive_turns)
    state.server = SimpleNamespace(rpc=object(), pid=111, base_url="http://127.0.0.1:1")
    state.fake = SimpleNamespace(pid=222, stats=lambda: [])
    state.workload = Workload("p", ("a",), (SessionTarget(0, "a", "s"),), None)
    state.level_dir = tmp_path / "level-01"
    state.level_dir.mkdir()
    return state


def _measure(phase, *, profile=False):
    return runner.measure_load(
        runner.LoadConfig(levels=(1,)),
        agents=1,
        server=phase.server,
        fake=phase.fake,
        workload=phase.workload,
        level_dir=phase.level_dir,
        profile=profile,
        ui_enabled=False,
    )


def test_measured_phase_brackets_the_load_and_writes_level_files(phase):
    measured = _measure(phase, profile=True)

    assert phase.events == [
        "start recording",
        "start sampler",
        "start profiler",
        "drive",
        "stop profiler",
        "stop sampler",
        "stop recording",
    ]
    assert measured["client"]["runs"]["ok"] == 1
    assert measured["server_process"] == {"cpu_avg": 5.0}
    assert measured["profile"]["status"] == "ok"
    assert measured["files"]["flamegraph"] == "level-01/flamegraph.svg"
    assert measured["notes"] == ["the recording produced no trace file"]
    for name in ("runs.json", "provider-requests.json", "recording-summary.json"):
        assert (phase.level_dir / name).is_file()


def test_everything_is_stopped_when_the_load_fails(phase):
    phase.drive_error = RuntimeError("driver broke")

    with pytest.raises(RuntimeError, match="driver broke"):
        _measure(phase)

    assert phase.events[-2:] == ["stop sampler", "stop recording"]


def test_failed_recording_stop_fails_the_level(phase):
    phase.stop_error = RecordingError("recording ended early")

    with pytest.raises(RecordingError, match="recording ended early"):
        _measure(phase)

    assert "stop sampler" in phase.events


@pytest.mark.parametrize(
    ("statuses", "tool_results", "tool_ok", "expected_status"),
    [
        (["completed"], 1, True, "ok"),
        (["failed"], 1, True, "failed"),
        (["cancelled"], 1, True, "failed"),
        (["interrupted"], 1, True, "failed"),
        (["timeout"], 1, True, "failed"),
        (["pending"], 1, True, "failed"),
        ([], 0, True, "failed"),
        (["completed", "completed"], 1, True, "failed"),
        (["completed"], 0, True, "failed"),
        (["completed"], 2, True, "failed"),
        (["completed"], 1, False, "failed"),
    ],
)
def test_level_only_accepts_complete_successful_workload(
    monkeypatch, tmp_path, statuses, tool_results, tool_ok, expected_status
):
    config = runner.LoadConfig(levels=(1,), turns=1, steps=2)
    records = [
        RunRecord(
            tag=f"t{index}",
            session_index=index,
            turn_index=0,
            agent_id="a",
            session_id="s",
            directive=config.turn_directive(1, index, 0),
            sent_at=1.0,
            finished_at=2.0,
            status=status,
            tool_timings=[
                ToolTiming(f"c{call}", "read", 1.0, tool_ok) for call in range(tool_results)
            ],
        )
        for index, status in enumerate(statuses)
    ]
    measured = {
        "files": {"runs": "level-01/runs.json"},
        "notes": [],
        "client": analyze_level(records, []),
        "load_seconds": 1.0,
    }
    server = MagicMock(console_log=tmp_path / "console.log")
    monkeypatch.setattr(runner, "VbotServer", lambda **kwargs: server)
    monkeypatch.setattr(runner, "check_instrumentation", lambda rpc: None)
    monkeypatch.setattr(
        runner, "seed_workload", lambda *args, **kwargs: SimpleNamespace(agent_ids=("a",))
    )
    monkeypatch.setattr(runner, "measure_load", lambda *args, **kwargs: measured)

    level = runner.run_level(
        config,
        agents=1,
        fake=MagicMock(),
        fixture_dir=tmp_path / "fixture",
        data_dir=tmp_path / "data",
        level_dir=tmp_path / "level-01",
        profile=False,
        ui_reason=None,
        log=lambda message: None,
    )

    assert level["status"] == expected_status
    assert level["client"] == measured["client"]
    assert level["files"]["runs"] == "level-01/runs.json"
    assert runner.failed_levels([level]) == ([] if expected_status == "ok" else [1])
    if expected_status == "failed":
        assert (tmp_path / level["files"]["error"]).is_file()
