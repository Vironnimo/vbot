# Access: server setup

How a server resolves and records its Access Mode at start, the access records, reverse proxies, direct TLS and downgrades. Rules and invariants: `access.md`.

## Start precedence

- `server/main.py` resolves the configuration (`_resolve_access`) before serving; an invalid one prints `vBot server cannot start: <reason>` to stderr and exits `STARTUP_FAILED_EXIT_CODE` (3). Precedence:
  1. A packaged installation's own server (the installation record's port and data directory) runs with the installation's mode and host: explicit access flags that differ refuse the start, a different `--host` logs a WARN and the record wins, a missing, unreadable or non-file installation record refuses the start (`cli/application.md`). Loaded package layout establishes installation provenance before checking its record; a failed metadata check never falls back to another record, including on the Linux systemd startup path. It mirrors the configuration into the data directory's record, replacing an unreadable one with a WARN.
  2. Otherwise explicit access options with `--access` are served and recorded for the data directory.
  3. Without access options the data directory's record wins, mode and bind host; a different `--host` logs a WARN `Server binds the data directory's recorded address, not the requested one (...)`. Access options without `--access` (such as `--allow-host` alone) refuse the start while a record exists, so a partial option set never silently drops recorded options.
  4. Only a data directory without a record lets the mode follow `--host` (loopback -> `local`, otherwise `lan`, never `authenticated`); the result is recorded.
  An unreadable data-directory record refuses the start. The record is written inside the server control claim, before serving; a failed write refuses the start. The start line carries `access=<mode>` (`server.md`).

Startup resolves Access/TLS again under its server control claim before writing the access record or constructing the app. The earlier preflight only refuses invalid startup options before creating a data root; its result cannot overwrite configuration changed before admission (`server/process-lifecycle.md`).

## Data directory access record

- Generation 1 document `access/server.json` (kind `access_server`, `json-documents.md`), owner-only, fields `{host, mode, allow_networks, allow_hosts, public_url, trusted_proxies, tls_cert, tls_key}`. `vbot server start|restart` and scheduled restarts pass only `--host/--port/--data-dir`, so the record keeps the mode; the CLI retargets its start to the recorded bind host (`cli/server-lifecycle.md` -> Commands).

## Reverse proxies

Each authenticated server needs a dedicated hostname, with no unrelated services on other ports: browser cookies are host-scoped, not port-scoped; separate server instances use separate hostnames.

Behind a trusted proxy every proxy must append its connecting peer to `X-Forwarded-For`, set `X-Forwarded-Proto` and pass `Host` through; `USAGE.md` -> Server access has an nginx example. Trust only proxy addresses, not client networks: a forwarded request must identify a client outside the trusted proxies. Missing chains and chains containing only trusted proxies are refused instead of assigning an unproven client address.

## Direct TLS

- Direct TLS serves HTTPS and plain HTTP on one listener (`tls.py`): the first byte `0x16` starts a TLS session, anything else is HTTP, which the guard admits only from loopback in `authenticated` mode. So local tools keep `http://127.0.0.1:<port>`. Unclassified connections close after 10 s, handshakes after 30 s, and shutdown aborts them. A server shutdown closes an established TLS connection like a plain one: once uvicorn closes it (idle at once, busy after its response), it sends close_notify and closes the socket after the buffered data is written, without waiting for the client's close_notify; a client that does not read, such as an idle pooled connection, never sends one and would hold up the 5 s graceful shutdown. Closes during operation keep waiting for it (asyncio's 30 s `ssl_shutdown_timeout`). The certificate files are checked every 60 s; a change is loaded for new connections without restart, a broken or unavailable renewal keeps the previous certificate and warns once. Every later check retries a failed load even when the file timestamp did not change; successful recovery logs once. Missing or uncheckable certificate files follow the same degradation/recovery policy.

## Downgrades

- Authentication requires an explicit package compatibility guarantee: see `cli/application.md` -> Update operation. An unmarked older package cannot be selected as a new candidate while the installation records `authenticated`; a failed candidate may still restart the exact previously active baseline. This gate cannot constrain an older updater or an operator manually running an older executable, which may have no access guard.
