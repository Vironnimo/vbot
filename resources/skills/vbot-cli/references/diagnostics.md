# Logs, Debug Traces, and Statistics

## Logs

```bash
vbot log list
vbot log read <daily-log-name>
```

- `list` shows daily log files newest-first. `read` takes the name exactly as listed (`<date>.log`) and returns parsed entries plus a cursor for live-tail handoff.

## Debug traces — raw provider traffic

Use when diagnosing provider or model problems.

```bash
vbot debug status
vbot debug probe <provider-id> --connection <provider:connection-id>
vbot debug traces
vbot debug trace <trace-id>
vbot debug clear
```

- `probe`, `traces`, and `trace` need debug mode enabled server-side: `vbot config set debug.enabled true`. `status` and `clear` always work.
- `probe` fetches the provider's models endpoint with the connection's credentials and prints status, duration, and a model preview; the full raw response is stored as a trace and read with `debug trace <trace-id>`.

## Statistics — usage aggregated from persisted sessions

Read-only reports computed on demand from stored chat history. One section per command; nothing is written.

```bash
vbot statistics overview [--since <iso>] [--until <iso>]
vbot statistics usage    [--since <iso>] [--until <iso>]
vbot statistics runs     [--since <iso>] [--until <iso>]
vbot statistics errors   [--since <iso>] [--until <iso>]
vbot statistics tools    [--since <iso>] [--until <iso>]
vbot statistics skills   [--since <iso>] [--until <iso>]
```

- `--since`/`--until` take ISO-8601 UTC timestamps (e.g. `2026-06-01T00:00:00Z`); omit both for all time. The server rejects malformed or inverted windows.
- `overview` = totals and activity; `usage` = tokens per provider/model incl. cache figures; `runs` = durations and cancel/failure rates; `errors` = failures by kind/provider/model; `tools` = per-tool call counts and error rates.
- `skills` reports skill usage for delete/improve decisions: it leads with the never-used list, then per skill shows origins, `offered` (sessions where the skill was available), `activated` (sessions that loaded it), `usage_rate`, and last activation.

Reading `skills` numbers correctly:

- Judge by **opportunity, not calendar age**: many offers with zero activations → strong delete/rework candidate; few offers → too new to judge, regardless of dates.
- `usage_rate` `0.00` means offered but never activated; `-` means never offered at all (e.g. no session started since the skill appeared).
- Deleted skills are not listed — the report covers the current skill inventory only.
