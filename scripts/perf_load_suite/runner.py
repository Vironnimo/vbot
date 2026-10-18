"""Run the load scenario level by level and assemble ``result.json``.

Every concurrency level gets a fresh vBot server with a fresh data directory;
the fake Provider process is shared by all levels and reset before each load
phase. A level that fails is recorded as failed and the next level still runs.

Two scenarios share the measurement (recording, process sampling, profiler,
UI probe, and for duration runs the snapshot series and heap census):

- ``sessions``: N Sessions receive the same scripted turns through
  ``chat.stream``. With ``ui_history_turns`` the Session the UI probe watches
  first gets that many completed turns, and the probe scrolls through all of
  them before the load phase.
- ``swarm``: one Swarm of the bundled Swarm Extension with N participants
  whose turns the fake Provider scripts (see ``swarm_driver``).
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil  # type: ignore[import-untyped]

from scripts.perf_load_suite.config import LoadConfig
from scripts.perf_load_suite.directive import PerfDirective
from scripts.perf_load_suite.driver import (
    UI_AGENT_ID,
    SessionTarget,
    Workload,
    drive_turns,
    run_command,
    seed_workload,
)
from scripts.perf_load_suite.fixture import write_fixture_project
from scripts.perf_load_suite.metrics import analyze_level, analyze_swarm_level
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
from scripts.perf_load_suite.swarm_driver import (
    SWARM_VIEW,
    ParticipantHistory,
    SwarmOutcome,
    SwarmRun,
    goal_prompt,
    prepare_swarm,
)
from scripts.perf_load_suite.timeline import (
    SnapshotSampler,
    copy_history,
    heap_census,
    heap_digest,
)
from scripts.perf_load_suite.ui_probe import (
    PROFILE_FILE,
    UiProbe,
    UiProbeError,
    ui_probe_unavailable_reason,
)

# Seed the watched history in batches and bound each request like a 200k Model.
UI_HISTORY_PROGRESS_TURNS = 50
UI_HISTORY_COMPACTION_TOKENS = 200_000


class LevelError(RuntimeError):
    """A level could not reach or finish its load phase."""


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
        "scenario": config.scenario,
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


@dataclass(frozen=True)
class LevelContext:
    """What one level's scenario runs against."""

    config: LoadConfig
    agents: int
    server: VbotServer
    fake: FakeProvider
    fixture_dir: Path
    level_dir: Path
    profile: bool
    ui_enabled: bool
    log: Callable[[str], None]


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
        "scenario": config.scenario,
        "sessions": agents,
        "turns": None if config.duration_minutes is not None else config.turns,
        "duration_minutes": config.duration_minutes,
        "status": "failed",
        "error": None,
        "notes": [],
        "files": {},
    }
    level_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    server = VbotServer(
        data_dir=data_dir,
        log_dir=level_dir,
        provider_api_base_url=fake.api_base_url,
        compaction_trigger_tokens=(
            UI_HISTORY_COMPACTION_TOKENS if config.ui_history_turns is not None else None
        ),
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
            context = LevelContext(
                config=config,
                agents=agents,
                server=server,
                fake=fake,
                fixture_dir=fixture_dir,
                level_dir=level_dir,
                profile=profile,
                ui_enabled=ui_enabled,
                log=log,
            )
            if config.scenario == "swarm":
                run_swarm_level(context, level)
            else:
                run_sessions_level(context, level)
            level["status"] = "ok"
    except Exception as exc:  # noqa: BLE001 - recorded in the level; later levels still run
        level["status"] = "failed"
        level["error"] = f"{type(exc).__name__}: {exc}"
        error_file = level_dir / "error.txt"
        error_file.write_text(traceback.format_exc(), encoding="utf-8")
        level["files"]["error"] = _relative(error_file, level_dir.parent)
        log(f"[{agents} agent(s)] FAILED: {level['error']} (details in {error_file})")
    finally:
        # After the server stopped: its shutdown writes a last history window.
        for name, copy, destination in (
            ("server_logs", copy_server_logs, level_dir / "server-logs"),
            ("performance_history", copy_history, level_dir / "performance-history"),
        ):
            try:
                copied = copy(data_dir, destination)
            except OSError as exc:
                level["notes"].append(f"copying {name.replace('_', ' ')} failed: {exc}")
            else:
                if copied is not None:
                    level["files"][name] = _relative(copied, level_dir.parent)
        level["files"]["server_console"] = _relative(server.console_log, level_dir.parent)
    level["wall_seconds"] = round(time.monotonic() - started, 1)
    if level["status"] == "ok":
        runs = level["client"]["runs"]
        log(
            f"[{agents} agent(s)] done: {runs['ok']}/{runs['total']} Runs ok, "
            f"load phase {level['load_seconds']}s"
        )
    return level


def run_sessions_level(context: LevelContext, level: dict[str, Any]) -> None:
    """N Sessions, each sent the scripted turns through ``chat.stream``."""
    config, agents, server = context.config, context.agents, context.server
    workload = seed_workload(
        server.rpc,
        fixture_dir=context.fixture_dir,
        sessions=agents,
        identity_agents=config.identity_agents,
        dedicated_ui_agent=context.ui_enabled,
    )
    level["identity_agents"] = list(workload.agent_ids)
    if config.history_tokens:
        context.log(f"[{agents} agent(s)] warming up {config.history_tokens} history tokens")
        warm_up(config, agents, server, workload)
    ui_session = workload.ui_session if config.ui_history_turns is not None else None
    if ui_session is not None:
        level["ui_history"] = seed_ui_history(context, ui_session)
    context.fake.reset()
    shape = (
        f"{config.duration_minutes} min"
        if config.duration_minutes is not None
        else f"{config.turns} turn(s)"
    )
    context.log(f"[{agents} agent(s)] load phase: {agents} Session(s) x {shape}")

    def directive_for(target: SessionTarget, turn_index: int) -> PerfDirective:
        return config.turn_directive(agents, target.index, turn_index)

    def phase(load: LoadPhase) -> list[Any]:
        return asyncio.run(
            drive_turns(
                server.base_url,
                workload.sessions,
                directive_for,
                turns=None if load.deadline is not None else config.turns,
                deadline=load.deadline,
                timeout_seconds=config.run_timeout_seconds,
            )
        )

    probe = (
        UiProbe(
            base_url=server.base_url,
            agent_id=UI_AGENT_ID,
            log_path=context.level_dir / "ui-probe.log",
            max_seconds=config.probe_seconds(),
            profile_path=context.level_dir / PROFILE_FILE if config.ui_profile else None,
            scroll_history=ui_session is not None,
        )
        if context.ui_enabled
        else None
    )
    if ui_session is not None:
        context.log(f"[{agents} agent(s)] UI probe: scrolling through the watched Session")
    measured, requests, runs = measure_load(context, probe=probe, phase=phase)
    _merge_measured(level, measured)
    if ui_session is not None:
        _check_scroll_through(context, level, earlier_turns=config.warmup_turns())
    _write_json(context.level_dir / "runs.json", [run.to_dict() for run in runs])
    level["files"]["runs"] = _relative(context.level_dir / "runs.json", context.level_dir.parent)
    level["client"] = client = analyze_level(runs, requests)
    runs_summary = client["runs"]
    if config.duration_minutes is None:
        expected_runs = agents * config.turns
    else:
        # A duration run sends as many turns as fit; every Session gets at least one.
        expected_runs = max(runs_summary["total"], agents)
    expected_calls = expected_runs * (config.steps - 1) * config.calls
    if (
        runs_summary["total"] != expected_runs
        or runs_summary["ok"] != expected_runs
        or runs_summary["tool_calls"] != expected_calls
        or runs_summary["tool_errors"]
    ):
        raise LevelError(
            f"measured workload incomplete or failed: {runs_summary['ok']}/{expected_runs} "
            f"Runs completed ({runs_summary['total']} recorded, "
            f"statuses={runs_summary['by_status']}), "
            f"{runs_summary['tool_calls']}/{expected_calls} Tool results, "
            f"{runs_summary['tool_errors']} Tool errors"
        )


def run_swarm_level(context: LevelContext, level: dict[str, Any]) -> None:
    """One Swarm with N participants; turns are scripted by the fake Provider."""
    config, agents, server = context.config, context.agents, context.server
    setup = prepare_swarm(
        server.rpc, fixture_dir=context.fixture_dir, participants=agents, tools=config.tools
    )
    level["participants"] = agents
    directive = config.swarm_directive(agents)
    context.fake.reset()
    shape = (
        f"{config.duration_minutes} min"
        if config.duration_minutes is not None
        else f"{config.turns} turn(s) each"
    )
    context.log(f"[{agents} agent(s)] load phase: one Swarm of {agents} participant(s), {shape}")
    swarm = SwarmRun(server.rpc, context.fake, setup, participants=agents, log=context.log)
    duration = config.duration_seconds
    timeout = (
        config.run_timeout_seconds * config.turns
        if duration is None
        else duration + config.run_timeout_seconds
    )

    def phase(load: LoadPhase) -> SwarmOutcome:
        swarm.start(goal_prompt(directive.render()))
        if load.probe is not None:
            failure = load.probe.select_run()
            if failure is not None:
                level["notes"].append(f"the UI probe could not select the Swarm: {failure}")
        return swarm.watch(
            turn_budget=None if duration is not None else config.turns,
            deadline=load.deadline,
            timeout_seconds=timeout,
        )

    probe = (
        UiProbe(
            base_url=server.base_url,
            view=SWARM_VIEW,
            log_path=context.level_dir / "ui-probe.log",
            max_seconds=config.probe_seconds(),
            profile_path=context.level_dir / PROFILE_FILE if config.ui_profile else None,
        )
        if context.ui_enabled
        else None
    )
    try:
        measured, requests, outcome = measure_load(context, probe=probe, phase=phase)
        _merge_measured(level, measured)
        histories = swarm.histories(outcome.final_swarm)
    finally:
        try:
            stopped = swarm.stop()
        except Exception as exc:  # noqa: BLE001 - reported; the level result decides
            level["notes"].append(f"stopping the Swarm failed: {type(exc).__name__}: {exc}")
        else:
            if stopped is not None:
                level["swarm_stop"] = {"state": stopped.get("state")}
    _record_swarm(context, level, directive, outcome, histories, requests)


def _record_swarm(
    context: LevelContext,
    level: dict[str, Any],
    directive: PerfDirective,
    outcome: SwarmOutcome,
    histories: Sequence[ParticipantHistory],
    requests: Sequence[dict[str, Any]],
) -> None:
    """Summarize and validate one Swarm level from histories and Provider records."""
    config = context.config
    progress = outcome.progress.get("participants") or {}
    labels: Counter[str] = Counter()
    participants = []
    problems: list[str] = []
    for history in histories:
        labels.update(history.tool_calls.values())
        entry = progress.get(history.key or "", {})
        turns = int(entry.get("turns") or 0)
        expected_calls = 1 + turns * (config.steps - 1) * config.calls
        unfinished = sorted({status for status in history.runs.values() if status != "completed"})
        unanswered = sorted(set(history.tool_calls) - set(history.tool_results))
        participants.append(
            {
                "participant_id": history.participant_id,
                "display_name": history.display_name,
                "key": history.key,
                "runs": len(history.runs),
                "turns": turns,
                "idle_answers": int(entry.get("idle") or 0),
                "tool_calls": len(history.tool_calls),
                "expected_tool_calls": expected_calls,
                "tool_errors": history.tool_errors,
            }
        )
        name = history.display_name or history.participant_id
        if history.key is None:
            problems.append(f"{name} never read the goal")
        if unfinished:
            problems.append(f"{name} has Runs in states {unfinished}")
        if unanswered:
            problems.append(f"{name} has {len(unanswered)} Tool calls without results")
        if history.tool_errors:
            problems.append(f"{name} has {history.tool_errors} failed Tool results")
        if len(history.tool_calls) != expected_calls:
            problems.append(
                f"{name} made {len(history.tool_calls)} Tool calls, expected {expected_calls}"
            )
        if config.duration_minutes is None and turns != config.turns:
            problems.append(f"{name} completed {turns}/{config.turns} turns")
        if turns < 1:
            problems.append(f"{name} completed no turn")
    keys = [history.key for history in histories]
    if len(histories) != context.agents:
        problems.append(f"the Swarm has {len(histories)} participants, expected {context.agents}")
    if len(set(keys)) != len(keys):
        problems.append("participants share a fake-Provider key")

    swarm = outcome.final_swarm
    newest_post = swarm.get("newest_post_sequence")
    runs = [status for history in histories for status in history.runs.values()]
    tool_durations = {
        call_id: duration
        for history in histories
        for call_id, (_ok, duration) in history.tool_results.items()
    }
    level["client"] = analyze_swarm_level(
        run_statuses=runs,
        tool_calls=sum(len(history.tool_calls) for history in histories),
        tool_errors=sum(history.tool_errors for history in histories),
        tool_durations=tool_durations,
        requests=requests,
        ideal_turn_ms=directive.scripted_text_ms(),
        started_at=outcome.started_at,
    )
    level["swarm"] = {
        "swarm_id": outcome.swarm_id,
        "participants": len(histories),
        "turn_budget": None if config.duration_minutes is not None else config.turns,
        "turns": {
            "scripted": sum(item["turns"] for item in participants),
            "idle": sum(item["idle_answers"] for item in participants),
        },
        "participant_runs": len(runs),
        "board_posts": newest_post + 1 if isinstance(newest_post, int) else None,
        "participant_posts": labels.get("swarm_board.post", 0),
        "kicks": outcome.kicks,
        "wiki_pages": swarm.get("newest_wiki_page_number"),
        "wound_down": outcome.wound_down,
        "polls": outcome.polls,
        "tool_calls_by_name": dict(labels.most_common()),
    }
    participants_file = context.level_dir / "participants.json"
    _write_json(participants_file, participants)
    level["files"]["participants"] = _relative(participants_file, context.level_dir.parent)
    if problems:
        raise LevelError("Swarm workload incomplete or failed: " + "; ".join(problems[:10]))


def warm_up(config: LoadConfig, agents: int, server: VbotServer, workload: Workload) -> None:
    """Grow every Session's history with unpaced text turns before measuring."""

    def directive_for(target: SessionTarget, turn_index: int) -> PerfDirective:
        return config.warmup_directive(agents, target.index, turn_index)

    drive_setup_turns(
        config,
        server,
        workload.sessions,
        directive_for,
        turns=config.warmup_turns(),
        label="warmup",
    )


def seed_ui_history(context: LevelContext, target: SessionTarget) -> dict[str, Any]:
    """Give the watched Session ``ui_history_turns`` completed turns before measuring.

    The turns go through the real pipeline (``chat.stream``, fake Provider,
    real Tools) unpaced, so the stored History has the shape real turns have,
    including the Compaction checkpoints automatic Compaction adds. A final
    ``/compact`` gives every N the same small Context for the load phase, which
    then cannot reach the Compaction trigger.
    """
    config, agents = context.config, context.agents
    total = config.ui_history_turns or 0
    started = time.monotonic()
    done = 0
    while done < total:
        batch = min(UI_HISTORY_PROGRESS_TURNS, total - done)

        def directive_for(
            _target: SessionTarget, turn_index: int, offset: int = done
        ) -> PerfDirective:
            return config.ui_history_directive(agents, offset + turn_index)

        drive_setup_turns(
            config, context.server, (target,), directive_for, turns=batch, label="UI history"
        )
        done += batch
        context.log(
            f"[{agents} agent(s)] UI history: {done}/{total} turns "
            f"({time.monotonic() - started:.0f} s)"
        )
    if total:
        error = asyncio.run(
            run_command(
                context.server.base_url,
                target,
                "/compact",
                timeout_seconds=config.run_timeout_seconds,
            )
        )
        if error is not None:
            raise LevelError(f"UI history Compaction failed: {error}")
    return {"turns": total, "seed_seconds": round(time.monotonic() - started, 1)}


def drive_setup_turns(
    config: LoadConfig,
    server: VbotServer,
    targets: Sequence[SessionTarget],
    directive_for: Callable[[SessionTarget, int], PerfDirective],
    *,
    turns: int,
    label: str,
) -> None:
    """Run unmeasured turns; fail the level unless each completed with its Tool results."""
    runs = asyncio.run(
        drive_turns(
            server.base_url,
            targets,
            directive_for,
            turns=turns,
            timeout_seconds=config.run_timeout_seconds,
        )
    )
    failed = [
        run
        for run in runs
        if not run.ok
        or run.tool_errors
        or len(run.tool_timings) != run.directive.tool_rounds * run.directive.calls
    ]
    if failed:
        first = failed[0]
        reason = first.error or (
            f"{len(first.tool_timings)} Tool results, {first.tool_errors} Tool errors"
        )
        raise LevelError(f"{len(failed)} {label} Run(s) failed, first: {first.status}: {reason}")


def _check_scroll_through(
    context: LevelContext, level: dict[str, Any], *, earlier_turns: int
) -> None:
    """Note an incomplete scroll-through and log what it measured."""
    scroll = (level.get("ui") or {}).get("scroll_through")
    if not isinstance(scroll, dict):
        return
    expected = (context.config.ui_history_turns or 0) + earlier_turns
    if scroll.get("status") != "ok":
        level["notes"].append(f"the UI scroll-through failed: {scroll.get('error')}")
    elif scroll.get("user_messages") != expected:
        level["notes"].append(
            f"the UI scroll-through showed {scroll.get('user_messages')} of {expected} "
            "earlier User messages"
        )
    page_ms = scroll.get("page_ms") or {}
    context.log(
        f"[{context.agents} agent(s)] UI scroll-through: {scroll.get('pages')} older pages, "
        f"{scroll.get('timeline_items')} timeline items, page p50 {page_ms.get('p50')} ms, "
        f"max {page_ms.get('max')} ms"
    )


@dataclass(frozen=True)
class LoadPhase:
    """What a scenario's load phase may use while it is measured."""

    probe: UiProbe | None
    deadline: float | None


def measure_load[T](
    context: LevelContext,
    *,
    probe: UiProbe | None,
    phase: Callable[[LoadPhase], T],
) -> tuple[dict[str, Any], list[dict[str, Any]], T]:
    """The measured phase: recording, samplers and probes around the scenario's load.

    Returns the measurements, the fake Provider's request records, and what
    ``phase`` returned. When the phase fails, the evidence files are still
    written before its error propagates.
    """
    config, server, fake, level_dir = (
        context.config,
        context.server,
        context.fake,
        context.level_dir,
    )
    duration = config.duration_seconds
    measured: dict[str, Any] = {"files": {}, "notes": []}
    stops: list[tuple[str, Callable[[], Any]]] = []
    outcomes: dict[str, Any] = {}
    failures: dict[str, str] = {}
    phase_error: Exception | None = None
    output: T | None = None
    heap_start = heap_census(server.rpc) if duration is not None else None
    if duration is not None and heap_start is None:
        measured["notes"].append("the server has no performance.heap; heap census skipped")
    load_started = load_finished = time.time()

    active_probe: UiProbe | None = None
    if probe is not None:
        try:
            probe.start()
            stops.append(("ui", probe.stop))
            active_probe = probe
        except UiProbeError as exc:
            measured["ui"] = {"status": "skipped", "reason": str(exc)}
            print(f"UI probe skipped: {exc}", file=sys.stderr)
    try:
        recording_seconds, capped = config.recording_seconds()
        if capped:
            measured["notes"].append(
                f"the server records at most {recording_seconds} s; the recording covers only "
                "the first hour of the load phase"
            )
        status = start_recording(
            server.rpc,
            label=f"perf-load {config.scenario} {context.agents} agent(s)",
            max_seconds=recording_seconds,
        )
        recording_id = status.get("recording_id")
        stops.append(
            (
                "recording",
                lambda: stop_recording(
                    server.rpc,
                    finished_in=server.data_dir if duration is not None else None,
                    recording_id=recording_id if isinstance(recording_id, str) else None,
                ),
            )
        )
        pids = {"server": server.pid, "harness": os.getpid()}
        if fake.pid is not None:
            pids["provider"] = fake.pid
        sampler = ProcessSampler(pids)
        sampler.start()
        stops.append(("processes", sampler.stop))
        if context.profile:
            name = "flamegraph-gil.svg" if config.profile_gil else "flamegraph.svg"
            profiler = PySpyRecorder(
                pid=server.pid, output=level_dir / name, gil_only=config.profile_gil
            )
            profiler.start()
            stops.append(("profile", profiler.stop))
        if duration is not None:
            snapshots = SnapshotSampler(
                server.base_url, interval_seconds=config.snapshot_interval_seconds
            )
            snapshots.start()
            stops.append(("snapshots", snapshots.stop))
        load_started = time.time()
        deadline = time.monotonic() + duration if duration is not None else None
        try:
            output = phase(LoadPhase(probe=active_probe, deadline=deadline))
        except Exception as exc:  # noqa: BLE001 - re-raised once the evidence is written
            phase_error = exc
    finally:
        load_finished = time.time()
        for name, stop in reversed(stops):
            try:
                outcomes[name] = stop()
            except Exception as exc:  # noqa: BLE001 - each stop runs; failures are reported
                failures[name] = f"{type(exc).__name__}: {exc}"
                print(f"warning: stopping {name} failed: {failures[name]}", file=sys.stderr)

    requests = fake.stats()
    _write_json(level_dir / "provider-requests.json", requests)
    measured["files"]["provider_requests"] = _relative(
        level_dir / "provider-requests.json", level_dir.parent
    )
    if "recording" in failures:
        if phase_error is not None:
            raise phase_error
        raise RecordingError(failures["recording"])
    stop_result = outcomes["recording"]
    _write_json(level_dir / "recording-summary.json", stop_result)
    measured["files"]["recording_summary"] = _relative(
        level_dir / "recording-summary.json", level_dir.parent
    )
    trace = copy_trace(stop_result, level_dir)
    if trace is not None:
        measured["files"]["trace"] = _relative(trace, level_dir.parent)
    else:
        measured["notes"].append("the recording produced no trace file")
    if phase_error is not None:
        raise phase_error

    digest = digest_recording(stop_result)
    if digest.get("stopped_reason") == "max_seconds":
        measured["notes"].append(
            f"the recording stopped at its {recording_seconds} s limit before the load phase "
            "ended; its figures cover only that part"
        )
    processes = outcomes.get("processes") or {}
    measured.update(
        {
            "load_seconds": round(load_finished - load_started, 2),
            "server": digest,
            "server_process": processes.get("server"),
            "provider_process": processes.get("provider"),
            "harness_process": processes.get("harness"),
        }
    )
    if "processes" in failures:
        measured["notes"].append(f"process sampling failed: {failures['processes']}")
    if duration is not None:
        timeline = outcomes.get("snapshots")
        if timeline is not None:
            measured["timeline"] = timeline
        else:
            measured["notes"].append(f"snapshot sampling failed: {failures.get('snapshots')}")
        if heap_start is not None:
            heap_end = heap_census(server.rpc)
            _write_json(level_dir / "heap.json", {"start": heap_start, "end": heap_end})
            measured["files"]["heap"] = _relative(level_dir / "heap.json", level_dir.parent)
            if heap_end is not None:
                measured["heap"] = heap_digest(heap_start, heap_end)
    if context.profile:
        measured["profile"] = outcomes.get("profile") or {
            "status": "failed",
            "error": failures.get("profile"),
        }
        flamegraph = measured["profile"].get("flamegraph")
        if flamegraph:
            measured["files"]["flamegraph"] = _relative(level_dir / flamegraph, level_dir.parent)
    if "ui" in outcomes:
        measured["ui"] = outcomes["ui"]
        profile = active_probe.profile_path if active_probe is not None else None
        if profile is not None and profile.is_file():
            measured["files"]["ui_profile"] = _relative(profile, level_dir.parent)
    elif "ui" in failures:
        measured["ui"] = {"status": "failed", "reason": failures["ui"]}
    return measured, requests, output  # type: ignore[return-value]


def _merge_measured(level: dict[str, Any], measured: dict[str, Any]) -> None:
    level["files"].update(measured.pop("files"))
    level["notes"].extend(measured.pop("notes"))
    level.update(measured)


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
        except OSError, subprocess.SubprocessError:
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
