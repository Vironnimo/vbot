# Performance

Always-on, low-overhead measurement of the server process, History Windows that keep it across restarts, on-demand Recordings that produce Perfetto-compatible timelines, and on-demand Heap Censuses.

## Overview

`core/performance/` owns process-wide duration histograms, gauges and counters, the Event Loop monitor and stall watchdog, History Windows, Recordings and their files, and Heap Censuses. Instrumented code in any domain records through module-level functions into one process-wide sink, the same way it logs. The Runtime-owned `PerformanceService` runs the monitor, watchdog and History Windows and owns Recording start/stop, trace writing, retention and listing. Exposure is `performance.*` RPC (`server/rpc/performance_methods.py`) and the `vbot performance` CLI area (`cli/performance_management.py`, alias `perf`); there is no WebUI view. The WebUI measures itself (`webui/src/lib/clientMetrics.js`) and reports into the same sink through `performance.client_report`, under `webui.*` names.

The domain measures; it never changes behavior. It does not own what is measured: each owner decides its own measurement points (`performance/metric-catalog.md`). Provider wire capture is a separate subsystem (`debug.md`); a performance trace never contains request or response content.

Module split: `performance.py` (public API, sink, service), `_metrics.py` (log-scale histograms, gauges, counters, name cap), `_monitor.py` (lag task, samplers, watchdog thread, stack rendering), `_thread_cpu.py` (per-thread CPU time on Windows and Linux), `_gc.py` (cyclic garbage collection pause observer), `_recording.py` (event buffer, lanes, trace/summary files, retention), `_heap.py` (Heap Census), `_history.py` (History Window lines, day files, retention).

## Terms

Shared terms (Run, Session, Tool) live in `.vorch/GLOSSARY.md`.

### Recording
One bounded, explicitly started capture window. At most one is active per process. While active, measurements with a track also become trace spans, counted occurrences with a track become instant events, gauges become counter samples, and stalls become instant events; the Recording keeps its own window histograms, gauge maxima, counters and stalls. It stops on request or at `max_seconds`, then writes a trace and a summary file. Not a Debug trace (`debug.md`).

### Track
The trace group a measurement belongs to, rendered as one Perfetto process: a Session address (`session_track()`: `<agent>@<project>/<session>` or `<agent>/<session>`), `rpc`, `sqlite`, `events` (server-pushed invalidations), `webui` (WebUI client reports), `runtime` (Event Loop lag, stalls, process gauges) or `worker pool <name>`. Tracks exist only inside a Recording; histograms ignore them.

### Lane
A trace thread inside one Track. A span takes the lowest lane that is free at its start, so concurrent spans of one Track never overlap on a lane. Lane numbers carry no identity (lane 0 is not a particular Run).

### History Window
A fixed stretch of server time (10 minutes, `HISTORY_INTERVAL_SECONDS`) whose measurement changes are appended as one JSON line to disk when it ends, and at shutdown. Windows start when the service starts; each holds what the process-lifetime histograms and counters gained during it, the gauges' last and highest values, and its stalls. They survive restarts, unlike the snapshot, and need no preparation, unlike a Recording.

### Heap Census
An on-demand count of the objects the cyclic garbage collector tracks, per generation and by module-qualified type name (`module.qualname`), aggregated by module (first two package segments). Changes are relative to the same service's previous census. Objects frozen at startup (`server/process-lifecycle.md` -> `create_app` hooks) are only counted as `frozen`, so the generations show what serving created. Not a memory size: it counts container objects, whose number drives full-collection pauses.

### Stall
An Event Loop tick overdue by more than the stall threshold (250 ms). The watchdog thread samples the loop thread's Python stack every 50 ms during the stall and aggregates identical stacks; frames are `path:line qualname` only. A stall also records the Tasks awaiting the Task the loop ran (`awaited_by`), the CPU of the loop and of other busy threads, and the cyclic garbage collection pause time (`gc_ms`); how they are captured and read: `performance/diagnosis.md`.

## Interfaces

**Module API** (`core.performance`, usable from any thread):
- `measure(metric, *, track=None, name=None, args=None)` -> `Measurement` context manager, valid across awaits. The histogram always records; with a track and an active Recording the block also becomes a span named `name` (default: metric). `Measurement.annotate(**args)` adds span args; `Measurement.discard()` drops both the observation and the span.
- `record_span(metric, started, *, ended=None, track=None, name=None, args=None, min_span_ms=0.0)` records a duration that began at a `time.perf_counter()` value. A span is emitted only when the active Recording began before `started` and the duration reaches `min_span_ms`.
- `record_duration(metric, ms)` and `set_gauge(name, value, *, track=None)`; `session_track(agent_id, session_id, project_id=None)`.
- `count(metric, amount=1, *, track=None, args=None)` adds occurrences to a counter; with a track and an active Recording each call also becomes an instant event named `metric` with `args`. Use it for events, not durations.
- `merge_histogram(metric, histogram)` adds durations measured outside the process, already sorted into this module's buckets (`ExternalHistogram`, `bucket_index`; `performance/metric-catalog.md`). It has no track and no span.
- `PerformanceService(recordings_dir, *, samplers=..., history_interval_s=600)`: `start()`/`stop()`/`aclose()`, `snapshot()`, `start_recording(label=, max_seconds=)`, `stop_recording()`, `recording_status()`, `list_recordings(limit=)`, `heap_census(top=)`, `history(since=, until=, limit=, names=)`. `start()` opens the first History Window; `stop()`/`aclose()` append the last one (`reason` `shutdown`). `history_interval_s=None` disables History Windows. File and snapshot work runs on its own `performance` worker pool. Errors: `RecordingActiveError`, `RecordingInactiveError` (both `PerformanceError`); an invalid label or `max_seconds` raises `ValueError`.

**RPC and files:** `performance.snapshot`, `recording_start`, `recording_stop`, `recording_list`, `heap`, `history` and `client_report` (`server/rpc/performance_methods.py`). Their field names are a contract: an external load-test harness and the `vbot performance` CLI consume them. Shapes, errors, Recording and History Window files and trace events: `performance/rpc-and-files.md`.

## Conventions

- The sink is the documented exception to constructor injection (`.vorch/PROJECT.md` -> Conventions). The service, monitor and files stay Runtime-owned.
- Metric names, span names and args never carry content: no prompts, messages, Tool arguments, paths from requests or credentials. Ids and counts are allowed as span args only, never in metric names. Only code-defined or registry-validated names may become metric names (for example registered RPC methods, dispatched Tool names).
- More than 1000 distinct histogram, gauge or counter names (each kind separately) are rejected: the observation is dropped, `performance.dropped_metrics` counts it, and one WARNING is logged.
- Recording start/stop and a shutdown discard log one INFO event; a stall >= 1 s logs a WARNING at most once per 30 s (its content: `performance/diagnosis.md`). Sampler failures warn once per gauge.
- **Import cost:** a package `__init__.py` that re-exports a large stack loads its exports on first access (`_EXPORT_MODULES` plus a module `__getattr__`, as in `core/channels`, `core/chat`, `core/memory`, `core/models`, `core/performance`, `core/providers` and `core/sessions`), because Python runs a package's `__init__` before any of its submodules: an eager one makes importing one constant import the whole package, which once cost `vbot --help` over a second. A light export maps to the leaf module that defines it, and a constant that accessors need without the stack lives in an import-free leaf (`core/channels/_choices.py`, `core/performance/_limits.py`; CLI contract: `cli.md` -> Interfaces). A heavy third-party package that only one operation needs is imported where that operation first runs, in a worker when that is on the Event Loop: curl_cffi before the first public fetch (`tools/web_fetch.md`), sqlite-vec (which imports numpy) with the Passage index's first connection or vector (`recall/passage-index.md` -> Index storage), and the MCP SDK once an MCP connection exists (`extensions/mcp.md`); server import and `Runtime.start` pay none of them.
- Measure at the owner's boundary with `measure` or `record_span`; use `min_span_ms` for high-frequency short waits so they stay histogram-only in traces.

## Constraints & Gotchas

- The monitor and watchdog start with the Runtime whenever a running Event Loop exists, in every `safe_startup_mode`; a synchronous start without a loop does not monitor. Shutdown discards an active Recording without writing it.
- The garbage collection callback runs inside the collector on whatever thread allocated, possibly while that thread holds a lock such as the metric registry's. It must stay lock-free (timestamps, a float total and a bounded deque); histograms and spans are recorded later on the Event Loop.
- The server process freezes its startup heap once the app is ready (`server/process-lifecycle.md` -> `create_app` hooks), so full collections skip modules, classes and Runtime services; `gc.gen2` then reflects the objects created while serving.
- A `gc.gen2` pause grows with the number of tracked objects created since the freeze, and full collections come more often the more container objects survive into the old generation; the server raises the old generation's threshold to make them rarer (`server/process-lifecycle.md` -> `create_app` hooks). Long-retained dict/list graphs therefore drive the pauses: finished Runs keep only their settled ending (`runs.md`), and Model requests share the live request view instead of copying it per step (`chat.md`). Keep new per-Run or per-step data out of long-lived containers, or bound it.
- The process sink is shared by every `PerformanceService` of the process; only one should run History Windows, because ending a window resets the gauge peaks.
- `measure()` costs about 0.7 us per call without a Recording and about 2.4 us with a track while recording (Python 3.14, Windows); keep it out of per-token or per-byte loops.

## Tests

`tests/core/performance/`, `tests/server/rpc/test_performance_methods.py`, `tests/cli/test_cli_performance.py`, `webui/src/lib/__tests__/clientMetrics.test.js`; owners test their own measurement points.

## References

Read these only when your task matches - not by default.

- Changing `performance.*` RPC shapes, the `vbot performance` CLI area, Recording or History Window files, or trace events -> `performance/rpc-and-files.md`
- Adding, renaming or interpreting a metric, or changing WebUI client measurement -> `performance/metric-catalog.md`
- Diagnosing stalls, Event Loop lag or garbage collection pauses, or changing the watchdog or the Heap Census -> `performance/diagnosis.md`
- Changing the worker pools whose `worker_pool.*` metrics the catalog lists -> `runtime/workers.md`
