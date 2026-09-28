# Performance Suite

This document explains how to find out where vBot spends time and how to prove that an optimization helped. The suite has four parts:

| Part | Question it answers | Entry point |
|---|---|---|
| In-app measurement | What is slow in *this* running server, right now or since start? | `vbot performance ...` (RPC `performance.*`) |
| Load test | How does vBot behave with 1, 10, 20, 30 concurrent Agents or Swarm participants, and over longer runs? | `python scripts/perf_load.py` |
| Microbenchmarks | How expensive is one known hot path, and did a change make it cheaper? | `python scripts/perf_bench.py` |
| Profilers | *Why* is a measured phase slow? | py-spy, Perfetto, browser DevTools |

The usual loop: measure a baseline (load test and/or benchmarks) → find the slow phase (report, recording, stalls) → find the cause (trace, flamegraph, stall stacks) → change the code → rerun with `--compare` against the baseline.

None of this runs in the regular test suites or CI. Timing measurements are slow and noisy, so they stay manual. Results land in the git-ignored `perf-results/` folder; they are machine-specific and never committed.

## In-app measurement

The server measures itself all the time with negligible overhead (around a microsecond per measurement). The domain map is `.vorch/domain-maps/performance.md`; it lists the full metric catalog and the RPC contract.

What is measured:

- **Event Loop health:** `event_loop.lag` (how late a 100 ms timer wakes up; Windows timer granularity adds a 0–16 ms baseline, so judge by p99), `event_loop.utilization` (CPU share of the loop thread), and **stalls**: a watchdog thread notices when the loop has not ticked for more than 250 ms and samples the loop thread's Python stack, so each stall comes with the code location that blocked it and the share of it spent in garbage collection (`gc_ms`). On Windows and Linux it also records how much CPU the loop used (`loop_cpu_ms`) and names the other threads that were busy meanwhile, with their stacks.
- **Garbage collection:** `gc.gen0`/`gc.gen1`/`gc.gen2`, one observation per collection pause in any thread. On the packaged Python 3.13, `gc.gen2` is the full collection that blocks every thread, including the Event Loop.
- **Worker pools:** `worker_pool.<name>.wait` (queueing for a slot) and `.run`, plus `.active`/`.waiting` gauges for every named `BoundedWorkerPool`.
- **SQLite:** per database (`sessions`, ...): `sqlite.<database>.write` (one write transaction including commit/fsync), `sqlite.<database>.write_wait` (waiting for that database's single writer), `sqlite.<database>.read`. The load-suite digest and report use the `sqlite.sessions.*` series.
- **Chat per Model step:** `chat.request_build`, `provider.first_token`, `provider.response`, `chat.persist`, `chat.tool_round`, `tool.<name>`, `chat.compaction`, `chat.run`.
- **RPC:** `rpc.<method>` per registered method.
- **Server push (counters):** `events.<type>` per `/ws` event, `events.resource_changed.<kind>` per invalidation kind, `events.sse` per Run event sent over SSE. Compare an invalidation count with the RPC counts it causes: `events.resource_changed.extensions=500` next to `rpc.extensions.page_descriptors count=11500` means every invalidation made 23 clients or pages reload. In a recording, each invalidation is a marker on the `events` row, followed by the RPC spans it caused.
- **WebUI (reported by each open browser tab about once a minute):** `webui.rpc` (RPC time as the browser sees it, including its wait for a connection), `webui.rpc_errors`, `webui.long_task` and `webui.long_animation_frame` (main-thread blocks of 50 ms or more), `webui.invalidations.<kind>` (invalidations the tabs received), `webui.invalidation_rpcs.<kind>.<method>` (RPCs a tab started within 250 ms after such an invalidation, the reload wave it caused) and `webui.extension_page.invalidations.<reason>` (Extension pages told to refresh). In a recording, each report is a marker on the `webui` row.
- **Process:** `process.cpu_percent`, `process.rss_mb`, `process.python_threads`, `asyncio.tasks`, `runs.active`, `runs.queued`.

```bash
vbot performance status                      # slowest operations since start, gauges, recent stalls
vbot performance record start --label slow-ui --max-seconds 300
vbot performance record stop                 # prints the trace path and the window's slowest operations
vbot performance recordings                  # stored recordings (newest 20 are kept)
vbot performance heap --top 30               # what the garbage collector tracks, by type and module
vbot performance history                     # one line per 10 minutes of the last 24 hours, also before restarts
vbot performance history --metric gc.gen2 --metric rpc.chat.send --hours 48
vbot performance history --at "2026-09-28 16:40"   # everything about the window of that log time
```

Add the usual target options (`--port 8421` for the development instance, the worktree port for a worktree).

**History windows** answer "what happened yesterday afternoon": every 10 minutes and at shutdown the server appends what changed in that window (metrics, counters, gauge maxima, stalls) to `<data-dir>/artifacts/performance/history/<date>.jsonl`, kept 14 days. `history` shows one health line per window; copy a time from a slow log line into `--at` to see that window in full, and use `--metric` to follow one metric over hours or days.

A **recording** is the tool for "it is slow right now": start it, let the slowness happen in real use, stop it. The trace (`<data-dir>/artifacts/performance/<id>.trace.json`) opens in [Perfetto](https://ui.perfetto.dev); the file is processed locally in the browser. Each Session is its own process row, so you see per Agent which phase (request build, first token, Tool round, persist) took how long and where Sessions wait for each other. Worker pools, SQLite, RPC and the runtime (Event Loop lag, stalls, process gauges) have rows of their own. Traces contain timings, ids and code locations, never message content.

A **heap census** answers "what fills the heap" when `gc.gen2` pauses grow: it counts the objects the garbage collector tracks per generation and by type (`core.runs.RunEvent`, `builtins.dict`, ...) and by module. Run it twice some minutes apart under load; the second run lists which types grew. A census blocks the Event Loop for up to about 100 ms on a heap of 5 million objects, so do not run it in a loop.

## Load test

```bash
python scripts/perf_load.py --quick                      # smoke run: 1 and 5 Agents, 1 turn
python scripts/perf_load.py                              # baseline: 1, 10, 20, 30 Agents, 3 turns each
python scripts/perf_load.py --agents 1,10 --history-tokens 40000   # long histories
python scripts/perf_load.py --profile                    # + py-spy flamegraph at the highest level
python scripts/perf_load.py --ui                         # + headless browser watching one streaming Session
python scripts/perf_load.py --scenario swarm --agents 3 --turns 2 --ui   # one Swarm, 3 participants, Swarm page open
python scripts/perf_load.py --agents 10 --duration 30 --ui              # 30 minutes of turns: memory, Tasks, gen2 over time
python scripts/perf_load.py --agents 3 --duration 2 --ui-profile        # --ui plus a CPU profile of the WebUI's JavaScript
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

Output: `perf-results/load-<UTC>/` with `report.md`, `result.json` and one `level-NN/` folder per level (`runs.json`, `provider-requests.json`, `recording-summary.json`, the Perfetto trace, server logs, the server's `performance-history/` windows, and depending on the options `participants.json`, `heap.json`, `flamegraph.svg` / `ui-probe.log` / `ui-profile.cpuprofile`).

The report also lists the recording's **counters** (`events.*`, `webui.*`, top 25; all of them are in `result.json`). They show the push traffic behind the load: how many `/ws` events, SSE events and invalidations per kind the level caused.

`--ui` needs Node.js, `npm ci` in `tests/e2e` (it reuses the E2E Playwright install) and a built WebUI. It reports Long Tasks and frame gaps in the browser while the load runs, and counts every `/api/rpc` request the browser makes, per method, including those of Extension page frames (`extensions.operation` is split into `extensions.operation:<extension>/<operation>`). The table *UI RPC calls by method* shows calls, total and maximum browser-side time and failures. A method called about every two seconds while nothing changes is polling; a burst after every Run event is a reload wave, which the server counters `webui.invalidation_rpcs.<kind>.<method>` attribute to the invalidation kind that caused it.

`--ui-profile` does the same and also samples the WebUI page's JavaScript while the load runs. The V8 CPU profile lands in the level folder as `ui-profile.cpuprofile`; open it in the DevTools Performance panel (*Load profile*) or in [speedscope](https://www.speedscope.app) to see which functions the Long Tasks spend their time in. The regular build is minified, so build the WebUI with `npx vite build --minify false` (in `webui/`) first for readable function names, and rebuild it normally afterwards. Sampling adds overhead of its own: compare Long Task counts only between runs without `--ui-profile`.

### Swarm scenario

`--scenario swarm` measures the bundled Swarm Extension instead of plain Sessions. Each level starts one Swarm with N participants on a fresh server:

- The harness saves a Swarm profile (all participants on the fake Provider's model, the directive's Tools within the fixture Project's Tool ceiling) and starts the Swarm with a goal that carries the directive. Participants read the goal from the Board through `swarm_board`; the fake Provider takes the directive from that Tool result.
- Each participant turn is scripted like a Session turn: Tool-call rounds, then a streamed text answer. `--tools` may name a Swarm Tool action; the default `swarm_board.post,read,swarm_board.read` posts a short progress note, reads a file and reads the Board. Scripted actions are `swarm_board.post`, `swarm_board.read`, `swarm_wiki.create`, `swarm_wiki.list` and `swarm_state`.
- Board posts wake the other participants, which starts their next turn. `--turns` is a budget per participant, counted from its own history: once spent, the participant answers a short plain text without Tools, which ends its chain of wake-ups.
- A post that reaches a participant inside its running Run does not wake it again afterwards, so when all participants run at once, the Swarm can fall idle before every budget is spent. The harness then posts a user check-in to the Board (a *kick*, which wakes every participant). The report counts kicks; many kicks mean lost wake-ups, not slowness.
- The level ends when every budget is spent and the Swarm is idle (no Run active, no Provider request in flight); then the harness stops the Swarm. It fails when a participant fails or the Swarm needs attention, after five kicks without progress, or at the timeout (`--run-timeout` per turn; with `--duration`, the duration plus one `--run-timeout`), and it fails unless every participant completed exactly its budget with successful Tool results. `participants.json` lists each participant's turns, Runs and Tool calls.

Extra report rows: participants and participant Runs, scripted and idle turns, Board posts (total, by participants, kicks), *Start->1st request* (from `swarms.start` to the first Provider request), and *turn duration* (first Provider request of a turn to its completed text answer) against the ideal scripted time. The Session-only rows (TTFT, delta latency, Run duration) are left out. With `--ui`, the browser opens the Swarm page, selects the running Swarm and reports frame gaps of the WebUI and of the page frame separately.

### Long runs

`--duration MINUTES` replaces `--turns` and shows what grows over time. Sessions (or Swarm participants) keep receiving turns until the time is up; turns already running finish. In the Swarm scenario, the budget is unlimited and, once the time is up, every new turn answers with the short idle text.

- Every `--snapshot-interval` seconds (default 60) the harness reads `performance.snapshot`: RSS, asyncio Tasks, Python threads, active Runs, `gc.gen2` collections with maximum and total pause, Event Loop lag p99 and the counter total. The report shows this time series and first/last/max rows. Lag and `gc.gen2` figures are cumulative since the server started.
- A heap census (`performance.heap`) at the start and end of the load phase lists the types and modules that grew most. Its object counts exclude the objects the server freezes after startup. Servers without the RPC skip the census with a note.
- The server's recording stops after at most an hour; a longer run keeps sampling and notes that the recording covers only the first hour. The time series and the copied `performance-history/` windows cover the whole run.

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
- **Browser:** the DevTools Performance panel for UI work; `perf_load.py --ui` for Long Tasks under load; the `webui.*` metrics for what real tabs experienced.

## Reading results

- Compare runs from the same machine under similar conditions. Other load (gates, builds, other agents) distorts results; a background virus scanner can slow SQLite commits and process starts noticeably.
- Look at p99 and max, not only p50: concurrency problems show up as tails and as `*.wait` metrics (waiting for a pool slot or the SQLite writer).
- A slow phase with low `event_loop.utilization` is waiting (disk, locks, subprocesses); a slow phase with high utilization or stalls is CPU work on the Event Loop.
- A stall with only a few samples and a high `gc_ms` is a garbage collection pause, not the code in its stack; compare `gc.gen2` max with the stall duration.
- A stall whose `loop_cpu_ms` is close to its CPU window is the loop computing; near zero, the loop waited. If another thread is named with high `cpu_ms`, it most likely held the GIL (or a lock the loop waited for); its stack shows what it did.
- `webui.rpc` far above `rpc.<method>` means the time went into the network or the browser's connection queue, not the server. `webui.invalidation_rpcs.<kind>.*` divided by `webui.invalidations.<kind>` is the number of RPCs one invalidation costs a tab; an RPC the user happened to start in the same 250 ms counts too. `webui.*` values arrive up to a minute late.
