# Logs and Debug Traces

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

- `probe`, `traces`, and `trace` need debug mode enabled server-side: `vbot config set debug '{"enabled": true}'`. `status` and `clear` always work.
- `probe` fetches the provider's models endpoint with the connection's credentials and prints status, duration, and a model preview; the full raw response is stored as a trace and read with `debug trace <trace-id>`.
