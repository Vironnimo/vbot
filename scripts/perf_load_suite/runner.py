"""Run the load scenario level by level and assemble ``result.json``.

Every concurrency level gets a fresh vBot server with a fresh data directory;
the fake Provider process is shared by all levels and reset before each load
phase. A level that fails is recorded as failed and the next level still runs.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil  # type: ignore[import-untyped]

from core.performance import MAX_RECORDING_SECONDS
from scripts.perf_load_suite.directive import DEFAULT_TOOLS, PerfDirective
from scripts.perf_load_suite.driver import (
    UI_AGENT_ID,
    SessionTarget,
    Workload,
    drive_turns,
    seed_workload,
)
from scripts.perf_load_suite.fixture import write_fixture_project
from scripts.perf_load_suite.metrics import RunRecord, analyze_level
from scripts.perf_load_suite.profiling import PySpyRecorder
from scripts.perf_load_suite.recording import (
    RecordingError,
    check_instrumentation,
    copy_trace,
    digest_recording,
    start_recording,
    stop_recording,
)
from scripts.perf_load_suite.report import RESULT_KIND, RESULT_SCHEMA, write_result
from scripts.perf_load_suite.sampling import ProcessSampler
from scripts.perf_load_suite.stack import PROJECT_ROOT, FakeProvider, VbotServer
from scripts.perf_load_suite.ui_probe import UiProbe, UiProbeError, ui_probe_unavailable_reason

DEFAULT_LEVELS: tuple[int, ...] = (1, 10, 20, 30)
QUICK_LEVELS: tuple[int, ...] = (1, 5)
WARMUP_CHUNK_TOKENS = 20_000
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "perf-results"


class LevelError(RuntimeError):
    """A level could not reach or finish its load phase."""


@dataclass(frozen=True)
class LoadConfig:
    """Everything that shapes one harness invocation (stored in ``result.json``)."""

    levels: tuple[int, ...] = DEFAULT_LEVELS
    turns: int = 3
    identity_agents: int = 3
    steps: int = 4
    tokens: int = 400
    rate: float = 80.0
    think_ms: int = 600
    tools: tuple[str, ...] = DEFAULT_TOOLS
    calls: int = 1
    history_tokens: int = 0
    run_timeout_seconds: float = 300.0
    recording_max_seconds: int = 1800
    profile: bool = False
    profile_gil: bool = False
    ui: bool = False
    keep: bool = False
    output_root: Path = field(default=DEFAULT_OUTPUT_ROOT)

    def __post_init__(self) -> None:
        if not self.levels or any(level < 1 for level in self.levels):
            raise ValueError("levels must be positive Session counts")
        if len(set(self.levels)) != len(self.levels):
            raise ValueError("levels must be distinct")
        if self.turns < 1 or self.identity_agents < 1:
            raise ValueError("turns and identity agents must be at least 1")
        if self.run_timeout_seconds <= 0:
            raise ValueError("run timeout must be positive")
        if not 1 <= self.recording_max_seconds <= MAX_RECORDING_SECONDS:
            raise ValueError(f"recording limit must be from 1 to {MAX_RECORDING_SECONDS} seconds")
        if self.history_tokens < 0:
            raise ValueError("history tokens must not be negative")
        # Validates steps/tokens/rate/think_ms/tools/calls once, up front.
        self.turn_directive(self.levels[0], 0, 0)

    def turn_directive(self, level: int, session_index: int, turn_index: int) -> PerfDirective:
        return PerfDirective(
            tag=f"L{level}-s{session_index:03d}-t{turn_index + 1}",
            steps=self.steps,
            tokens=self.tokens,
            rate=self.rate,
            think_ms=self.think_ms,
            tools=self.tools,
            calls=self.calls,
        )

    def warmup_turns(self) -> int:
        return math.ceil(self.history_tokens / WARMUP_CHUNK_TOKENS) if self.history_tokens else 0

    def warmup_directive(self, level: int, session_index: int, warmup_index: int) -> PerfDirective:
        remaining = self.history_tokens - warmup_index * WARMUP_CHUNK_TOKENS
        return PerfDirective(
            tag=f"L{level}-s{session_index:03d}-w{warmup_index + 1}",
            warmup_tokens=min(WARMUP_CHUNK_TOKENS, remaining),
            rate=0,
        )

    @property
    def profiled_level(self) -> int | None:
        return max(self.levels) if self.profile or self.profile_gil else None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["levels"] = list(self.levels)
        data["tools"] = list(self.tools)
        data["output_root"] = str(self.output_root)
        data["sample_directive"] = self.turn_directive(self.levels[0], 0, 0).render()
        return data


def run_load(
    config: LoadConfig, *, log: Callable[[str], None] = print
) -> tuple[Path, dict[str, Any]]:
    """Run every level and return the run directory and the final result."""
    started = datetime.now(UTC)
    run_dir = create_run_dir(config.output_root, started)
    temp_root = Path(tempfile.mkdtemp(prefix="vbot-perf-load-"))
    result: dict[str, Any] = {
        "kind": RESULT_KIND,
        "schema": RESULT_SCHEMA,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": None,
        "run_dir": str(run_dir),
        "config": config.to_dict(),
        "git": git_info(),
        "machine": machine_info(),
        "levels": [],
    }
    ui_reason = ui_probe_unavailable_reason() if config.ui else None
    if ui_reason is not None:
        log(f"UI probe skipped: {ui_reason}")
    log(f"Results: {run_dir}")
    log(f"Temporary data: {temp_root}{' (kept)' if config.keep else ''}")
    try:
        write_result(run_dir, result)
        fixture_dir = write_fixture_project(temp_root / "fixture")
        with FakeProvider(log_path=run_dir / "fake-provider.log") as fake:
            profiled = False
            for agents in config.levels:
                profile = config.profiled_level == agents and not profiled
                profiled = profiled or profile
                level = run_level(
                    config,
                    agents=agents,
                    fake=fake,
                    fixture_dir=fixture_dir,
                    data_dir=temp_root / f"data-level-{agents:02d}",
                    level_dir=run_dir / f"level-{agents:02d}",
                    profile=profile,
                    ui_reason=ui_reason,
                    log=log,
                )
                result["levels"].append(level)
                write_result(run_dir, result)
    finally:
        result["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        write_result(run_dir, result)
        if not config.keep:
            remove_tree(temp_root, log=log)
    return run_dir, result


def create_run_dir(output_root: Path, started: datetime) -> Path:
    """Create ``load-<UTC timestamp>`` (suffixed when that name is taken)."""
    output_root.mkdir(parents=True, exist_ok=True)
    stem = f"load-{started.strftime('%Y%m%dT%H%M%SZ')}"
    for attempt in range(1, 100):
        candidate = output_root / (stem if attempt == 1 else f"{stem}-{attempt}")
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError(f"no free result folder name for {stem} in {output_root}")


def run_level(
    config: LoadConfig,
    *,
    agents: int,
    fake: FakeProvider,
    fixture_dir: Path,
    data_dir: Path,
    level_dir: Path,
    profile: bool,
    ui_reason: str | None,
    log: Callable[[str], None],
) -> dict[str, Any]:
    """Run one concurrency level on its own server; failures are recorded, not raised."""
    level: dict[str, Any] = {
        "agents": agents,
        "sessions": agents,
        "turns": config.turns,
        "status": "failed",
        "error": None,
        "notes": [],
        "files": {},
    }
    level_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    server = VbotServer(
        data_dir=data_dir, log_dir=level_dir, provider_api_base_url=fake.api_base_url
    )
    try:
        log(f"[{agents} agent(s)] starting vBot on {server.base_url}")
        with server:
            check_instrumentation(server.rpc)
            ui_enabled = config.ui and ui_reason is None
            if ui_enabled and not server.serves_webui():
                ui_enabled = False
                level["ui"] = {
                    "status": "skipped",
                    "reason": "the WebUI is not built (npm run build in webui/)",
                }
                log("UI probe skipped: the WebUI is not built (npm run build in webui/)")
            elif config.ui and ui_reason is not None:
                level["ui"] = {"status": "skipped", "reason": ui_reason}
            workload = seed_workload(
                server.rpc,
                fixture_dir=fixture_dir,
                sessions=agents,
                identity_agents=config.identity_agents,
                dedicated_ui_agent=ui_enabled,
            )
            level["identity_agents"] = list(workload.agent_ids)
            if config.history_tokens:
                log(f"[{agents} agent(s)] warming up {config.history_tokens} history tokens")
                warm_up(config, agents, server, workload)
            fake.reset()
            log(f"[{agents} agent(s)] load phase: {agents} Session(s) x {config.turns} turn(s)")
            measured = measure_load(
                config,
                agents=agents,
                server=server,
                fake=fake,
                workload=workload,
                level_dir=level_dir,
                profile=profile,
                ui_enabled=ui_enabled,
            )
            level["files"].update(measured.pop("files"))
            level["notes"].extend(measured.pop("notes"))
            level.update(measured)
            runs = level["client"]["runs"]
            expected_runs = agents * config.turns
            expected_calls = expected_runs * (config.steps - 1) * config.calls
            if (
                runs["total"] != expected_runs
                or runs["ok"] != expected_runs
                or runs["tool_calls"] != expected_calls
                or runs["tool_errors"]
            ):
                raise LevelError(
                    f"measured workload incomplete or failed: {runs['ok']}/{expected_runs} "
                    f"Runs completed ({runs['total']} recorded, statuses={runs['by_status']}), "
                    f"{runs['tool_calls']}/{expected_calls} Tool results, "
                    f"{runs['tool_errors']} Tool errors"
                )
            level["status"] = "ok"
    except Exception as exc:  # noqa: BLE001 - recorded in the level; later levels still run
        level["status"] = "failed"
        level["error"] = f"{type(exc).__name__}: {exc}"
        error_file = level_dir / "error.txt"
        error_file.write_text(traceback.format_exc(), encoding="utf-8")
        level["files"]["error"] = _relative(error_file, level_dir.parent)
        log(f"[{agents} agent(s)] FAILED: {level['error']} (details in {error_file})")
    finally:
        try:
            logs = copy_server_logs(data_dir, level_dir / "server-logs")
        except OSError as exc:
            level["notes"].append(f"copying the server logs failed: {exc}")
        else:
            if logs is not None:
                level["files"]["server_logs"] = _relative(logs, level_dir.parent)
        level["files"]["server_console"] = _relative(server.console_log, level_dir.parent)
    level["wall_seconds"] = round(time.monotonic() - started, 1)
    if level["status"] == "ok":
        runs = level["client"]["runs"]
        log(
            f"[{agents} agent(s)] done: {runs['ok']}/{runs['total']} Runs ok, "
            f"load phase {level['load_seconds']}s"
        )
    return level


def warm_up(config: LoadConfig, agents: int, server: VbotServer, workload: Workload) -> None:
    """Grow every Session's history with unpaced text turns before measuring."""

    def directive_for(target: SessionTarget, turn_index: int) -> PerfDirective:
        return config.warmup_directive(agents, target.index, turn_index)

    runs = asyncio.run(
        drive_turns(
            server.base_url,
            workload.sessions,
            directive_for,
            turns=config.warmup_turns(),
            timeout_seconds=config.run_timeout_seconds,
        )
    )
    failed = [run for run in runs if not run.ok]
    if failed:
        raise LevelError(
            f"{len(failed)} warmup Run(s) failed, first: {failed[0].status}: {failed[0].error}"
        )


def measure_load(
    config: LoadConfig,
    *,
    agents: int,
    server: VbotServer,
    fake: FakeProvider,
    workload: Workload,
    level_dir: Path,
    profile: bool,
    ui_enabled: bool,
) -> dict[str, Any]:
    """The measured phase: recording, samplers and probes around the concurrent turns."""

    def directive_for(target: SessionTarget, turn_index: int) -> PerfDirective:
        return config.turn_directive(agents, target.index, turn_index)

    measured: dict[str, Any] = {"files": {}, "notes": []}
    stops: list[tuple[str, Callable[[], Any]]] = []
    outcomes: dict[str, Any] = {}
    failures: dict[str, str] = {}
    runs: list[RunRecord] = []
    load_started = load_finished = time.time()

    if ui_enabled:
        probe = UiProbe(
            base_url=server.base_url,
            agent_id=UI_AGENT_ID,
            log_path=level_dir / "ui-probe.log",
            max_seconds=config.recording_max_seconds,
        )
        try:
            probe.start()
            stops.append(("ui", probe.stop))
        except UiProbeError as exc:
            measured["ui"] = {"status": "skipped", "reason": str(exc)}
            print(f"UI probe skipped: {exc}", file=sys.stderr)
    try:
        start_recording(
            server.rpc,
            label=f"perf-load {agents} agent(s)",
            max_seconds=config.recording_max_seconds,
        )
        stops.append(("recording", lambda: stop_recording(server.rpc)))
        pids = {"server": server.pid, "harness": os.getpid()}
        if fake.pid is not None:
            pids["provider"] = fake.pid
        sampler = ProcessSampler(pids)
        sampler.start()
        stops.append(("processes", sampler.stop))
        if profile:
            name = "flamegraph-gil.svg" if config.profile_gil else "flamegraph.svg"
            profiler = PySpyRecorder(
                pid=server.pid, output=level_dir / name, gil_only=config.profile_gil
            )
            profiler.start()
            stops.append(("profile", profiler.stop))
        load_started = time.time()
        runs = asyncio.run(
            drive_turns(
                server.base_url,
                workload.sessions,
                directive_for,
                turns=config.turns,
                timeout_seconds=config.run_timeout_seconds,
            )
        )
    finally:
        load_finished = time.time()
        for name, stop in reversed(stops):
            try:
                outcomes[name] = stop()
            except Exception as exc:  # noqa: BLE001 - each stop runs; failures are reported
                failures[name] = f"{type(exc).__name__}: {exc}"
                print(f"warning: stopping {name} failed: {failures[name]}", file=sys.stderr)
    if "recording" in failures:
        raise RecordingError(failures["recording"])

    requests = fake.stats()
    stop_result = outcomes["recording"]
    _write_json(level_dir / "runs.json", [run.to_dict() for run in runs])
    _write_json(level_dir / "provider-requests.json", requests)
    _write_json(level_dir / "recording-summary.json", stop_result)
    measured["files"]["runs"] = _relative(level_dir / "runs.json", level_dir.parent)
    measured["files"]["provider_requests"] = _relative(
        level_dir / "provider-requests.json", level_dir.parent
    )
    measured["files"]["recording_summary"] = _relative(
        level_dir / "recording-summary.json", level_dir.parent
    )
    trace = copy_trace(stop_result, level_dir)
    if trace is not None:
        measured["files"]["trace"] = _relative(trace, level_dir.parent)
    else:
        measured["notes"].append("the recording produced no trace file")

    processes = outcomes.get("processes") or {}
    measured.update(
        {
            "load_seconds": round(load_finished - load_started, 2),
            "client": analyze_level(runs, requests),
            "server": digest_recording(stop_result),
            "server_process": processes.get("server"),
            "provider_process": processes.get("provider"),
            "harness_process": processes.get("harness"),
        }
    )
    if "processes" in failures:
        measured["notes"].append(f"process sampling failed: {failures['processes']}")
    if profile:
        measured["profile"] = outcomes.get("profile") or {
            "status": "failed",
            "error": failures.get("profile"),
        }
        flamegraph = measured["profile"].get("flamegraph")
        if flamegraph:
            measured["files"]["flamegraph"] = _relative(level_dir / flamegraph, level_dir.parent)
    if "ui" in outcomes:
        measured["ui"] = outcomes["ui"]
    elif "ui" in failures:
        measured["ui"] = {"status": "failed", "reason": failures["ui"]}
    return measured


def copy_server_logs(data_dir: Path, destination: Path) -> Path | None:
    """Copy the server's daily log files out of the disposable data directory."""
    source = data_dir / "logs"
    if not source.is_dir():
        return None
    shutil.copytree(source, destination, dirs_exist_ok=True)
    return destination


def remove_tree(path: Path, *, log: Callable[[str], None], attempts: int = 5) -> None:
    """Delete a temporary tree, retrying while Windows still holds file handles."""
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if attempt == attempts - 1:
                log(f"warning: could not remove temporary data {path}: {exc}")
                return
            time.sleep(1.0)


def git_info() -> dict[str, Any]:
    def run(*args: str) -> str | None:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return completed.stdout.strip()

    status = run("status", "--porcelain")
    return {
        "commit": run("rev-parse", "--short=12", "HEAD"),
        "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(status) if status is not None else None,
    }


def machine_info() -> dict[str, Any]:
    memory = psutil.virtual_memory()
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu": platform.processor() or platform.machine(),
        "cpu_logical": psutil.cpu_count(logical=True),
        "cpu_physical": psutil.cpu_count(logical=False),
        "memory_gb": round(memory.total / 1024**3, 1),
    }


def failed_levels(levels: Sequence[dict[str, Any]]) -> list[int]:
    return [int(level["agents"]) for level in levels if level.get("status") != "ok"]


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _relative(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return str(path)
