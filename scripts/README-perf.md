# Performance Suite

This document explains how to find out where vBot spends time and how to prove that an optimization helped. The suite has four parts:

| Part | Question it answers | Entry point |
|---|---|---|
| In-app measurement | What is slow in *this* running server, right now or since start? | `vbot performance ...` (RPC `performance.*`) |
| Load test | How does vBot behave with 1, 10, 20, 30 concurrent Agents? | `python scripts/perf_load.py` |
| Microbenchmarks | How expensive is one known hot path, and did a change make it cheaper? | `python scripts/perf_bench.py` |
| Profilers | *Why* is a measured phase slow? | py-spy, Perfetto, browser DevTools |

The usual loop: measure a baseline (load test and/or benchmarks) → find the slow phase (report, recording, stalls) → find the cause (trace, flamegraph, stall stacks) → change the code → rerun with `--compare` against the baseline.

None of this runs in the quality gates. Timing measurements are slow and noisy, so they stay manual. Results land in the git-ignored `perf-results/` folder; they are machine-specific and never committed.

## In-app measurement

The server measures itself all the time with negligible overhead (around a microsecond per measurement). The domain map is `.vorch/domain-maps/performance.md`; it lists the full metric catalog and the RPC contract.

What is measured:

- **Event Loop health:** `event_loop.lag` (how late a 100 ms timer wakes up; Windows timer granularity adds a 0–16 ms baseline, so judge by p99), `event_loop.utilization` (CPU share of the loop thread), and **stalls**: a watchdog thread notices when the loop has not ticked for more than 250 ms and samples the loop thread's Python stack, so each stall comes with the code location that blocked it.
- **Worker pools:** `worker_pool.<name>.wait` (queueing for a slot) and `.run`, plus `.active`/`.waiting` gauges for every named `BoundedWorkerPool`.
- **SQLite:** per database (`sessions`, ...): `sqlite.<database>.write` (one write transaction including commit/fsync), `sqlite.<database>.write_wait` (waiting for that database's single writer), `sqlite.<database>.read`. The load-suite digest and report use the `sqlite.sessions.*` series.
- **Chat per Model step:** `chat.request_build`, `provider.first_token`, `provider.response`, `chat.persist`, `chat.tool_round`, `tool.<name>`, `chat.compaction`, `chat.run`.
- **RPC:** `rpc.<method>` per registered method.
- **Process:** `process.cpu_percent`, `process.rss_mb`, `process.python_threads`, `asyncio.tasks`, `runs.active`, `runs.queued`.

```bash
vbot performance status                      # slowest operations since start, gauges, recent stalls
vbot performance record start --label slow-ui --max-seconds 300
vbot performance record stop                 # prints the trace path and the window's slowest operations
vbot performance recordings                  # stored recordings (newest 20 are kept)
```

Add the usual target options (`--port 8421` for the development instance, the worktree port for a worktree).

A **recording** is the tool for "it is slow right now": start it, let the slowness happen in real use, stop it. The trace (`<data-dir>/artifacts/performance/<id>.trace.json`) opens in [Perfetto](https://ui.perfetto.dev); the file is processed locally in the browser. Each Session is its own process row, so you see per Agent which phase (request build, first token, Tool round, persist) took how long and where Sessions wait for each other. Worker pools, SQLite, RPC and the runtime (Event Loop lag, stalls, process gauges) have rows of their own. Traces contain timings, ids and code locations, never message content.

## Load test

```bash
python scripts/perf_load.py --quick                      # smoke run: 1 and 5 Agents, 1 turn
python scripts/perf_load.py                              # baseline: 1, 10, 20, 30 Agents, 3 turns each
python scripts/perf_load.py --agents 1,10 --history-tokens 40000   # long histories
python scripts/perf_load.py --profile                    # + py-spy flamegraph at the highest level
python scripts/perf_load.py --ui                         # + headless browser watching one streaming Session
python scripts/perf_load.py --compare perf-results/load-<old>/result.json
python scripts/perf_load.py compare old/result.json new/result.json
```

Run `python scripts/perf_load.py --help` for every option (turn shape, Tools, timeouts, output folder).

What it does:

- Starts a scripted **fake Provider** (OpenAI-compatible, in its own process) and, per concurrency level, a **fresh vBot server** on a free port with a temporary data directory. It never touches an existing instance or data directory, passes no credentials to the server, and removes its processes and temporary files afterwards.
- Disables automatic Memory/Skill reflection reviews only in that temporary server's Settings (`reflection.enabled=false`), so additional background Runs do not change the scripted workload or appear as repeated Provider requests. Use the same harness settings for baseline and comparison runs.
- Creates the Sessions (spread over a few Identity Agents rooted in a fixture Project) and lets all of them run turns concurrently. Each turn is a directive in the user message (`[[perf id=... steps=4 tokens=400 rate=80 think_ms=600 tools=read,search_files,bash]]`): the fake Provider answers with real Tool calls (the Tools actually run) and then streams a text answer at a fixed rate.
- Directives use registry Tool names on every host; the fake Provider projects them to the offered Model names (`bash` becomes `powershell` on Windows). A level fails, with a nonzero harness exit code, unless every expected Run completes, every scripted Tool result arrives, and all Tools succeed. Failed levels retain their measurements and evidence files for diagnosis.
- Starts a server-side performance recording for each load phase and copies its trace and summary into the result.

Because the fake Provider's own timing is known, the report can separate vBot's cost from the Provider's:

| Report row | Meaning |
|---|---|
| Send→1st request | From the chat RPC until the Provider receives the first request: Run admission, context preparation, request build |
| TTFT overhead | Time to the first streamed token minus the scripted `think_ms` |
| Step overhead | Gap between the Provider finishing a Tool-call response and receiving the next request of the same Session: Tool execution + persistence + request build |
| Step excl. Tool exec | The same gap without the Tool's own runtime: pure vBot overhead per step |
| Delta latency | Provider emits a chunk → the client receives it over SSE (includes the deliberate ~40 ms delta batching) |
| Run duration / ideal | Measured Run time against the pure scripted Provider time |
| Server-side rows | Event Loop lag/utilization, stalls, SQLite, request build, persist, Tool rounds, worker pools — from the recording |

Output: `perf-results/load-<UTC>/` with `report.md`, `result.json` and one `level-NN/` folder per level (`runs.json`, `provider-requests.json`, `recording-summary.json`, the Perfetto trace, server logs, and with the options `flamegraph.svg` / `ui-probe.log`).

`--ui` needs Node.js, `npm ci` in `tests/e2e` (it reuses the E2E Playwright install) and a built WebUI. It reports Long Tasks and frame gaps in the browser while the load runs.

## Microbenchmarks

```bash
python scripts/perf_bench.py                      # backend and frontend
python scripts/perf_bench.py --quick --only backend
python scripts/perf_bench.py --filter chat.request
python scripts/perf_bench.py --compare perf-results/bench-latest.json
python scripts/perf_bench.py --tmp-dir D:/scratch # measure fsync cost on another disk
python scripts/perf_bench.py --list
```

Each benchmark calls the real vBot function on synthetic data in a temporary directory: Session append/load, request transform/wire conversion/JSON encoding and token estimation for 100- and 1000-message histories, Provider stream parsing, delta batching, SSE framing, and in the WebUI streaming Markdown rendering and Timeline projection (Vitest `*.bench.js` files in `webui/src/**/__benchmarks__/`, run through `npx vitest bench`). The runner warms up, calibrates iterations, takes 10 samples, and reports median, tail and ops/s. `--compare` flags changes beyond `--threshold` percent (default 10) that also leave the old min/max range. Results: `perf-results/bench-<UTC>.json` and `bench-latest.json`.

Some benchmarks reach into private functions (`_build_payload`, request-history helpers, the token count cache, `server._streams._sse_run_events`); when those change, update the benchmark with them.

## Profilers

- **py-spy** (dev extra) samples a running Python process without code changes and shows where CPU time goes. `perf_load.py --profile` records the server during the highest level. By hand against any server: `py-spy record -o flame.svg --pid <server-pid> --duration 30`; add `--gil` to see only threads holding the GIL; `py-spy dump --pid <pid>` prints all current thread stacks once. Some samples fail on Windows; judge flamegraphs by proportions, not totals.
- **Perfetto** (<https://ui.perfetto.dev>) opens recording traces. Use it to see ordering and waiting between Sessions, pools and SQLite, not just totals.
- **Stall stacks** (`vbot performance status`, recording summaries, and a rate-limited WARNING in the server log for stalls of at least 1 s) point directly at code that blocked the Event Loop.
- **Browser:** the DevTools Performance panel for UI work; `perf_load.py --ui` for Long Tasks under load.

## Reading results

- Compare runs from the same machine under similar conditions. Other load (gates, builds, other agents) distorts results; a background virus scanner can slow SQLite commits and process starts noticeably.
- Look at p99 and max, not only p50: concurrency problems show up as tails and as `*.wait` metrics (waiting for a pool slot or the SQLite writer).
- A slow phase with low `event_loop.utilization` is waiting (disk, locks, subprocesses); a slow phase with high utilization or stalls is CPU work on the Event Loop.
