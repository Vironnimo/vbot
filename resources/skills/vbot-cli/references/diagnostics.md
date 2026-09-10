# Logs, Debug Traces, and Statistics

## Logs

```bash
vbot log list
vbot log read <daily-log-name> [--limit <count>] [--level error]
```

- `list` shows daily log files newest-first. `read` takes the name exactly as listed (`<date>.log`) and prints the newest 100 parsed entries by default, in chronological order. `--level` filters first; `--limit 0` prints all matches. The summary states how many matches are shown. This is a snapshot, not a live tail; the CLI still fetches the complete daily file from the server.

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

Reports summarize persisted Session activity. The server maintains a disposable statistics index; reporting does not change Session history. Request the section relevant to the question.

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

Reading `skills` numbers correctly:

- Judge by **opportunity, not calendar age**: many offers with zero activations → strong delete/rework candidate; few offers → too new to judge, regardless of dates.
- `offer_conversion` counts only Sessions where the Skill was both recorded in the offered catalog and activated. `0.00` means observed offers but no matching activation; `-` means there is no offer data. Older Sessions without catalog metadata may still contribute to `activated`, but cannot inflate this rate.
- Deleted skills are not listed — the report covers the current skill inventory only.
