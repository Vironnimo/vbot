# Performance

Always-on, low-overhead measurement of the server process plus on-demand Recordings that produce Perfetto-compatible timelines.

## Overview

`core/performance/` owns process-wide duration histograms and gauges, the Event Loop monitor and stall watchdog, Recordings, and their files. Instrumented code in any domain records through module-level functions into one process-wide sink, the same way it logs. The Runtime-owned `PerformanceService` runs the monitor and watchdog and owns Recording start/stop, trace writing, retention and listing. Exposure is `performance.*` RPC (`server/rpc/performance_methods.py`) and the `vbot performance` CLI area (`cli/performance_management.py`, alias `perf`); there is no WebUI.

The domain measures; it never changes behavior. It does not own what is measured: each owner decides its own measurement points (catalog below). Provider wire capture is a separate subsystem (`debug.md`); a performance trace never contains request or response content.

Module split: `performance.py` (public API, sink, service), `_metrics.py` (log-scale histograms, name cap), `_monitor.py` (lag task, samplers, watchdog thread, stack rendering), `_recording.py` (event buffer, lanes, trace/summary files, retention).

## Terms

Shared terms (Run, Session, Tool) live in `.vorch/GLOSSARY.md`.

### Recording
One bounded, explicitly started capture window. At most one is active per process. While active, measurements with a track also become trace spans, gauges become counter samples, and stalls become instant events; the Recording keeps its own window histograms, gauge maxima and stalls. It stops on request or at `max_seconds`, then writes a trace and a summary file. Not a Debug trace (`debug.md`).

### Track
The trace group a measurement belongs to, rendered as one Perfetto process: a Session address (`session_track()`: `<agent>@<project>/<session>` or `<agent>/<session>`), `rpc`, `sqlite`, `runtime` (Event Loop lag, stalls, process gauges) or `worker pool <name>`. Tracks exist only inside a Recording; histograms ignore them.

### Lane
A trace thread inside one Track. A span takes the lowest lane that is free at its start, so concurrent spans of one Track never overlap on a lane. Lane numbers carry no identity (lane 0 is not a particular Run).

### Stall
An Event Loop tick overdue by more than the stall threshold (250 ms). The watchdog thread samples the loop thread's Python stack every 50 ms during the stall and aggregates identical stacks; frames are `path:line qualname` only.

## Interfaces

**Module API** (`core.performance`, usable from any thread):
- `measure(metric, *, track=None, name=None, args=None)` -> `Measurement` context manager, valid across awaits. The histogram always records; with a track and an active Recording the block also becomes a span named `name` (default: metric). `Measurement.annotate(**args)` adds span args; `Measurement.discard()` drops both the observation and the span.
- `record_span(metric, started, *, ended=None, track=None, name=None, args=None, min_span_ms=0.0)` records a duration that began at a `time.perf_counter()` value. A span is emitted only when the active Recording began before `started` and the duration reaches `min_span_ms`.
- `record_duration(metric, ms)` and `set_gauge(name, value, *, track=None)`; `session_track(agent_id, session_id, project_id=None)`.
- `PerformanceService(recordings_dir, *, samplers=...)`: `start()`/`stop()`/`aclose()`, `snapshot()`, `start_recording(label=, max_seconds=)`, `stop_recording()`, `recording_status()`, `list_recordings(limit=)`. File and snapshot work runs on its own `performance` worker pool. Errors: `RecordingActiveError`, `RecordingInactiveError` (both `PerformanceError`); an invalid label or `max_seconds` raises `ValueError`.

**RPC contract** (field names are a contract; an external load-test harness consumes them):
- `performance.snapshot` `{}` -> `{started_at, uptime_seconds, metrics, gauges, stalls, recording}`. `metrics` maps name -> `{count, sum_ms, min_ms, max_ms, p50_ms, p90_ms, p99_ms}`; `gauges` maps name -> latest value; `stalls` is the retained list (max 50, oldest first) of `{started_at, duration_ms, samples: [{count, stack: [frame]}]}` with samples ordered most frequent first; `recording` is the status below or `null`. Values are process-lifetime, not windowed.
- `performance.recording_start` `{label?, max_seconds?}` -> status `{recording_id, label, started_at, elapsed_seconds, max_seconds, event_count, truncated}`. `label` is non-blank, <= 200 characters; `max_seconds` is an integer 1..3600, default 300.
- `performance.recording_stop` `{}` -> `{recording_id, label, started_at, duration_seconds, event_count, truncated, stopped_reason, trace_path, summary: {metrics, gauges_max, stalls}}`. `summary` covers only the Recording window.
- `performance.recording_list` `{limit?}` (1..100, default 20) -> `{recordings: [...]}` newest first, each the stop result without `summary`.
- Errors: `performance_recording_active` (start while recording), `performance_recording_inactive` (stop while idle), `invalid_request` (unknown params, bad label/limits).
- `stopped_reason` is `requested` or `max_seconds`. `trace_path` is an absolute path with `/` separators.

**Files:** `<data_dir>/artifacts/performance/<recording_id>.trace.json` (Chrome Trace Event JSON object format: `{"traceEvents": [...], "displayTimeUnit": "ms"}`, `ts`/`dur` in microseconds from the Recording start) and `<recording_id>.summary.json` (list fields plus `stopped_at` and `summary`). Recording ids use the `perf_` prefix (`core/utils/ids.py`). Both files are atomic; the summary is written last and marks a complete Recording, so listing reads summaries only. The newest 20 Recordings are retained; older pairs are pruned after each write. Placement is owned by Storage (`storage.md`).

**Trace events:** each Track emits a `process_name` metadata event and each Lane a `thread_name` (`lane <n>`); spans are `X` events with `cat` = first metric segment; gauges and `event_loop.lag` are `C` counters; stalls are process-scoped `i` events named `event_loop.stall` with `duration_ms` and `samples` args.

## Metric Catalog

Durations are milliseconds. Names are dotted lowercase words with low cardinality.

- `rpc.<method>` - every registered RPC method in `server/rpc/dispatcher.py::dispatch_method`, track `rpc`. Unknown method names return before measurement.
- Per kernel database `<name>` (`sessions`, ...): `sqlite.<name>.write_wait` (lock wait; spans only >= 1 ms), `sqlite.<name>.write` (each `BEGIN IMMEDIATE`..`COMMIT`/`ROLLBACK` attempt, so a busy retry counts twice) and `sqlite.<name>.read` (the whole read context including the caller's work, plus the writer-lock wait when no WAL reader is available) - `core/database/_runtime.py`, track `sqlite` (`database.md`). The load suite reads the `sqlite.sessions.*` series.
- `worker_pool.<pool>.wait` (admission; spans only >= 1 ms) and `.run` (span named after the callable), gauges `.active`/`.waiting` - every `BoundedWorkerPool` (`core/utils/workers.py`), track `worker pool <pool>`; an `OrderedWorker` records only `.run`.
- On the Session Track, with `run_id` args: `chat.run` (whole Run, `run_kind` arg; `_run_execution.py`), `chat.request_build` (first step: from request-state build to send, or from the loop top when a pre-request Compaction ran; later steps: loop top to send), `provider.response` (each send attempt incl. the too-large retry, `iteration` arg), `provider.first_token` (streaming: first non-heartbeat delta; non-streaming: the whole `adapter.send`), `chat.persist` (spans `persist assistant` / `persist tool results`), `chat.tool_round` (`tool_calls` arg) and `tool.<name>` (one Tool dispatch; discarded for unknown Tool names). Sources: `core/chat/_agentic_progression.py`, `request_runner.py`, `tool_dispatch.py`.
- `chat.compaction` - whole automatic attempt or manual Compaction Run (`trigger` arg `auto`/`manual`), `core/compaction/run_coordination.py`.
- `event_loop.lag` - how late each 100 ms monitor tick woke.
- Gauges sampled about once per second: `event_loop.utilization` (loop-thread CPU / wall time), `process.cpu_percent`, `process.rss_mb`, `process.python_threads` (Python threads only: the OS thread count needs a system-wide process scan on Windows, milliseconds of GIL time per sample), `asyncio.tasks`, and the Runtime-injected `runs.active` / `runs.queued`. `performance.dropped_metrics` counts observations dropped by the name cap.

## Conventions

- The sink is the documented exception to constructor injection (`.vorch/PROJECT.md` -> Conventions): it holds only measurements, never state other code reads to decide behavior. The service, monitor and files stay Runtime-owned.
- Metric names, span names and args never carry content: no prompts, messages, Tool arguments, paths from requests or credentials. Ids and counts are allowed as span args only, never in metric names. Only code-defined or registry-validated names may become metric names (for example registered RPC methods, dispatched Tool names).
- More than 1000 distinct histogram or gauge names are rejected: the observation is dropped, `performance.dropped_metrics` counts it, and one WARNING is logged.
- Recording start/stop and a shutdown discard log one INFO event; a stall >= 1 s logs a WARNING with its top frames, at most once per 30 s (suppressed count reported). Sampler failures warn once per gauge.
- Measure at the owner's boundary with `measure` or `record_span`; use `min_span_ms` for high-frequency short waits so they stay histogram-only in traces.

## Constraints & Gotchas

- The monitor and watchdog start with the Runtime whenever a running Event Loop exists, in every `safe_startup_mode`; a synchronous start without a loop does not monitor. Shutdown discards an active Recording without writing it.
- Windows timer granularity (about 15.6 ms) gives `event_loop.lag` a baseline of 0-16 ms; judge lag by p99 and stalls, not p50.
- A system suspend or debugger pause can show up as one long stall whose samples do not explain it.
- A Recording buffers at most 500,000 events (roughly 75-175 MB in memory) and sets `truncated` when full; histograms continue. Trace JSON is written off the Event Loop in 1000-event chunks.
- A span appears only if it began inside the Recording; `measure` blocks open when the Recording stops are dropped from the trace but still counted in histograms.
- Stack sampling uses `sys._current_frames()`; frames are rendered relative to the vBot root or the Python library root.
- `measure()` costs about 0.7 us per call without a Recording and about 2.4 us with a track while recording (Python 3.14, Windows); keep it out of per-token or per-byte loops.

## Tests

`tests/core/performance/` (histograms, sink, monitor/watchdog, Recording files), `tests/server/rpc/test_performance_methods.py`, `tests/server/rpc/test_dispatcher.py`, `tests/cli/test_cli_performance.py`, `tests/core/runtime/test_runtime_performance.py`, `tests/core/chat/test_chat_loop_performance.py`, `tests/core/database/test_connections.py`, `tests/core/utils/test_workers.py`.
