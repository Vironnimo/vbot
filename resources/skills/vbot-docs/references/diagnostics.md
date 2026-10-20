# Logs, Debug Traces, Statistics, and Performance

## Logs

```bash
vbot log list
vbot log read <daily-log-name> [--limit <count>] [--level error]
```

- `list` shows daily log files newest-first. `read` takes the name exactly as listed (`<date>.log`) and prints the newest 100 parsed entries by default, in chronological order. `--level` filters first; `--limit 0` prints all matches. The summary states how many matches are shown. This is a snapshot, not a live tail; the CLI still fetches the complete daily file from the server.

## Debug traces: raw Provider traffic

For Provider and Model problems.

```bash
vbot debug status
vbot debug probe <provider-id> --connection <provider:connection-id>
vbot debug traces
vbot debug trace <trace-id>
vbot debug clear
```

- `probe`, `traces`, and `trace` need debug mode enabled server-side: `vbot config set debug.enabled true`. `status` and `clear` always work.
- `probe` fetches the provider's models endpoint with the connection's credentials and prints status, duration, and a model preview; the full raw response is stored as a trace and read with `debug trace <trace-id>`.

## Performance: server responsiveness and timelines

For when vBot itself is slow: delayed replies, stalled streams, slow Tool rounds or an unresponsive interface.

```bash
vbot performance status
vbot performance history [--hours <hours>] [--metric <name>]... [--at <time>]
vbot performance heap [--top <count>]
vbot performance record start [--label <text>] [--max-seconds <seconds>]
vbot performance record stop
vbot performance recordings [--limit <count>]
```

- Measurement is always on. `status` reports since server start: the slowest operations by p99 and by total time, key gauges (Event Loop utilization, process CPU and memory, active and queued Runs), and recent Event Loop stalls with the code locations that blocked the server.
- `history` shows stored 10-minute summaries of the last 24 hours (`--hours` up to 336), also from earlier server processes, so a slowdown the user reports afterwards can still be examined: `--at "2026-09-28 16:40"` (local time, as in log lines) shows the windows around that time, `--metric <name>` one value per window. `heap` counts the objects in memory by type, for a growing memory use.
- To examine a specific slowdown, start a recording, let the slow behavior happen, then stop it. `record stop` prints the trace file path and the slowest operations of exactly the recorded window. Only one recording runs at a time; it stops automatically after `--max-seconds`, and `recordings` still lists it. Give the user the trace path: the file opens in https://ui.perfetto.dev, with one track per Session.
- Measurements, traces and summaries contain timings, ids and code locations, never message content.

## Statistics: usage from stored Sessions

Reports summarize stored Session activity. Request the section the question needs.

For Model response speed and waiting time, use `vbot statistics usage`; its Model and Provider rows show rates by measurement basis and first-token median, p95, and average.

```bash
vbot statistics overview [--since <iso>] [--until <iso>]
vbot statistics usage    [--since <iso>] [--until <iso>]
vbot statistics runs     [--since <iso>] [--until <iso>]
vbot statistics compactions [--since <iso>] [--until <iso>]
vbot statistics errors   [--since <iso>] [--until <iso>]
vbot statistics tools    [--since <iso>] [--until <iso>]
vbot statistics skills   [--since <iso>] [--until <iso>]
```

- `--since`/`--until` take ISO-8601 UTC timestamps (e.g. `2026-06-01T00:00:00Z`); omit both for all time. The server rejects malformed or inverted windows.
- Ranked Provider/Model/Run/error/Tool breakdowns show at most the top 20 rows; totals cover the selected window.
- `overview` = totals and activity; `usage` = tokens per provider/model incl. cache figures; `runs` = durations and cancel/failure rates; `compactions` = checkpoints, reclaimed context, and per-Session breakdowns; `errors` = failures by kind/provider/model; `tools` = per-tool call counts and error rates.
- `skills` reports Skill usage for delete/improve decisions: it separates Skills whose recorded offers produced no conversion from Skills with no offer data, then per Skill shows origins, `offered` (Sessions whose catalog recorded it), `activated` (all Sessions that loaded it), `activated_after_offer`, `offer_conversion`, and last activation.

Reading `skills` numbers:

- Judge a Skill by its offers, not its age: many offers and no activation make it a candidate to delete or rework; few offers are too few to judge.
- `offer_conversion` counts only Sessions that both offered and activated the Skill. `0.00` means offers without activation, `-` no offer data. Sessions without offer data can still count toward `activated` but not toward this rate.
- Deleted Skills are not listed; the report covers the current skill inventory only.
