# Access

Who may reach a vBot server and how they prove it: access modes, the access guard at the server edge, paired Devices, pairing, login tickets, the local access token and direct TLS.

## Overview

The server runs in exactly one Access Mode (GLOSSARY -> Access Mode), chosen by the host when the server starts and never changeable through RPC:

- `local` ("This computer only"): loopback peers only; the server must bind a loopback address (default `127.0.0.1`).
- `lan` ("Home network without login"): loopback, RFC 1918, link-local and IPv6 ULA peers plus each `--allow-network` CIDR; no login (default bind `0.0.0.0`).
- `authenticated` ("With login (internet)"): any peer, but every protected request needs a credential. It requires `--public-url https://<origin>` and HTTPS through either direct TLS (`--tls-cert` + `--tls-key`, default bind `0.0.0.0`) or at least one `--trusted-proxy` that terminates HTTPS (default bind `127.0.0.1`).

The labels are `core.access.config.ACCESS_MODE_LABELS`; user-facing text uses them, flags and records use the mode names.

Owners:

- `core/access/config.py`: `AccessConfig`, the mode rules (`access_config`, `access_config_from_record`), `default_host`, `inferred_access_mode`, `check_bind_host`, address and host-name helpers (`normalize_host_name`) and `load_tls_context`. Used by `server/main.py`, `server/app.py` and `cli/application/` (installer, record, `vbot application access`).
- `core/access/server_record.py`: the data directory's access record `access/server.json` (`read_server_access`, `write_server_access`), which keeps a server's mode and bind host across restarts (Interfaces).
- `core/access/devices.py`: device tokens and the `DeviceStore` (`access/devices.json`).
- `core/access/local_token.py`: the local access token file.
- `core/utils/owner_only.py`: owner-only file permissions (POSIX `0600`; on Windows a protected DACL for the current user and `SYSTEM`), applied by the atomic writers' `owner_only=True` before any content is written. Used for the local access token, the device store, the data directory's access record and the server control record.
- `server/access/`: `AccessGuard` (`_guard.py`, ASGI), `AccessService` (`_service.py`, the access state of one app), pure peer/transport/route rules (`_rules.py`), in-memory pairing requests and login tickets (`_pairing.py`), rate limits and aggregated refusal logging (`_limits.py`), live connections for revocation (`_connections.py`), the principal (`_principal.py`), the unauthenticated endpoints (`_routes.py`) and direct TLS (`tls.py`).
- `server/rpc/access_methods.py`: `auth.login_ticket` and the `devices.*` RPCs.
- CLI: `vbot devices ...` (`cli/device_management.py`, `cli.md`), `vbot application access show|set` and the installer options (`cli/application.md`), and `cli/rpc_client.py::local_access_headers`.

Not owned here: the browser-origin guard (`server/_origins.py`, `server.md`), which still runs after the access guard; capability URL verification (`server/file_delivery.py`); Extension OAuth (`extensions.md`); credential storage.

Accessor status: the server contracts exist; no accessor signs in yet. The WebUI has no pairing or sign-in screen, the Desktop no pairing, and the CLI/TUI reach an `authenticated` server only on its own computer through the local access token. A remote browser therefore gets the public WebUI shell but 401 on every API call.

## Terms

Core terms (Access Mode, Device, Accessor) live in `.vorch/GLOSSARY.md`.

### Principal
Who a request acts for, attached by the guard: `host` (the local access token), `device` (a paired Device's token or cookie) or `open` (a `local`/`lan` request without a valid credential). An exempt route in `authenticated` mode without a credential has no principal (`None`). Logs and the device store name it as its `actor`: the device id, `host` or `open`.

### Effective client
The address a request counts as coming from: the direct peer, or in `authenticated` mode the address a trusted proxy forwarded. Rate limits, refusal logs and a Device's `last_client` use it.

### Local access token
`vbl_<43 base64url>`, written fresh at every server start to `<data-dir>/runtime/local-access-<port>.token` (owner-only) and removed at stop unless a newer server replaced it. The host's own tools send it as `Authorization: Bearer`. Accepted only from a direct loopback peer without any forwarding header.

### Pairing
How a new Device gets a token: it requests pairing and shows an 8-character code; someone already signed in (host or Device) approves the code; the new Device claims its token by polling. Requests live in memory for 10 minutes.

### Login ticket
A single-use, 60-second secret a signed-in principal creates (`auth.login_ticket`) so that a top-level browser navigation to `/auth/ticket/<ticket>` turns into a new browser Device with its cookie. Used by accessors that already hold a credential (Desktop, CLI) to open the WebUI in a browser.

### Route class
`classify_route` sorts every request into `exempt` (no principal needed), `shell` (the public WebUI shell: any GET/HEAD outside the server namespaces `/api`, `/ws`, `/_vbot`, `/auth`, `/health`) or `protected` (everything else, every WebSocket). It fails closed.

## Request pipeline

`create_app` adds `AccessGuard` last, so it runs first: access guard -> browser-origin guard -> verification-only guard (verification mode) -> routes. For each HTTP request and WebSocket:

1. Route class (above).
2. Peer and transport (`_rules.inspect_request`); a refusal answers 403 `access_denied` (WebSocket close 4403):
   - `local`/`lan`: any proxy or CDN header (`_PROXY_HEADERS`: `Forwarded`, `Via`, `X-Real-IP`, `X-Client-IP`, `X-Cluster-Client-IP`, `X-Original-Forwarded-For`, `True-Client-IP`, `CF-Connecting-IP`, `Fastly-Client-IP`, and every `X-Forwarded-*`) refuses the request (`proxy_headers`): a proxy in front means the request may come from anywhere. A peer outside the admitted networks is refused (`peer_not_admitted`).
   - `authenticated`: forwarding headers count only when the direct peer is a trusted proxy. The effective client is the rightmost `X-Forwarded-For` entry that is not a trusted proxy (entries with a port are accepted; any other non-address entry refuses, `invalid_forwarded_for`); the rightmost `X-Forwarded-Proto` must be `https` (for a WebSocket also `wss`). A direct peer needs HTTPS unless it is loopback (`insecure`).
   - uvicorn runs with `proxy_headers=False`; nothing else interprets forwarding headers.
   - Host name, in every mode (`HostPolicy`, against DNS rebinding): the `Host` must be an IP address or one of the server's names: `localhost`, this computer's host name, fully qualified name, short name and `<short>.local` (`machine_host_names`), the bind host, the public URL's host and each `--allow-host`. Anything else, or several `Host` headers, answers 403 `access_denied` "Access denied: unknown host name" (`host_not_allowed`; the log shows the Host). A request without `Host` passes.
3. Credential (`AccessService.authenticate`): `Authorization: Bearer` wins over the `__Host-vbot_device` cookie; a second `Authorization` header, another scheme, or more than one device cookie value is malformed (those cookies are not cleared: one of them may be valid). A `vbl_` token is checked against this start's local token, and only from a direct loopback peer; a `vbt_` token through the device store (in memory, constant-time hash comparison). In `local`/`lan` a refused credential still yields `open`; in `authenticated` it is logged, counted per effective client, and a dead cookie is cleared (`Max-Age=0`).
4. A `protected` route without a principal answers 401 `auth_required` with `WWW-Authenticate: Bearer` (WebSocket 4401), or 429 `rate_limited` with `Retry-After` once the client is over its failed-credential limit.
5. The principal goes into `scope["vbot.principal"]`. `/api/rpc` binds it for its own task (`bound_principal`), so RPC handlers read it with `current_principal()`; tasks the request starts (Runs, background work) inherit the context but get `None`, and Live calls dispatch with `None`.
6. Every request of a Device registers in `LiveConnections` at entry. Revoking or expiring the Device cancels its WebSockets (close 4401 `Device signed out`) and its responses that started and have not finished (an SSE stream or a download ends); a response that has not started yet becomes 401 `auth_required` "Device signed out". The request that revokes still answers (`devices.logout`).
7. Every HTTP response gets `X-Content-Type-Options: nosniff`, HSTS (`max-age=31536000`) when the request arrived over HTTPS, and on `shell` routes `Referrer-Policy: same-origin` plus the WebUI CSP (`WEBUI_CONTENT_SECURITY_POLICY`: same-origin scripts, connections, frames and workers; inline styles; remote `http(s)` images for Markdown; `frame-ancestors 'none'`, `object-src 'none'`, `base-uri 'none'`). A header the route set itself wins.

The browser-origin guard additionally accepts the configured public origin (`AccessConfig.public_origin`). In `authenticated` mode the OAuth callback URL is `<public-url>/api/oauth/callback`, otherwise the loopback URL `server.md` describes.

## Interfaces

- Accessor contracts (endpoints, payloads, cookie, ticket, error codes, close codes, rate limits) -> `access/accessor-contracts.md`.
- `create_app(access=AccessConfig, local_access_token=str)`: without `access` the mode follows the bind host (loopback -> `local`, otherwise `lan`); in-process test apps rely on this. The service is `app.state.access`.
- `server/main.py` resolves the configuration (`_resolve_access`) before serving; an invalid one prints `vBot server cannot start: <reason>` to stderr and exits `STARTUP_FAILED_EXIT_CODE` (3). Precedence:
  1. A packaged installation's own server (the installation record's port and data directory) runs with the installation's mode and host: explicit access flags that differ refuse the start, a different `--host` logs a WARN and the record wins, an unreadable installation record refuses the start (`cli/application.md`). It mirrors the configuration into the data directory's record, replacing an unreadable one with a WARN.
  2. Otherwise explicit access options with `--access` are served and recorded for the data directory.
  3. Without access options the data directory's record wins, mode and bind host; a different `--host` logs a WARN `Server binds the data directory's recorded address, not the requested one (...)`. Access options without `--access` (such as `--allow-host` alone) refuse the start while a record exists, so a partial option set never silently drops recorded options.
  4. Only a data directory without a record lets the mode follow `--host` (loopback -> `local`, otherwise `lan`, never `authenticated`); the result is recorded.
  An unreadable data-directory record refuses the start. The record is written inside the server control claim, before serving; a failed write refuses the start. The start line carries `access=<mode>` (`server.md`).
- Data directory access record: Generation 1 document `access/server.json` (kind `access_server`, `settings.md` -> JSON Document Contract), owner-only, fields `{host, mode, allow_networks, allow_hosts, public_url, trusted_proxies, tls_cert, tls_key}`. `vbot server start|restart` and scheduled restarts pass only `--host/--port/--data-dir`, so the record keeps the mode; the CLI retargets its start to the recorded bind host (`cli.md`).
- `current_principal()` is `None` outside the `/api/rpc` request's own task, so in-process RPC dispatch (Agents, Live) and tasks a request starts cannot use the access RPCs: they refuse with `access_denied`.
- Device store: Generation 1 document `access/devices.json` (kind `access_devices`, `settings.md` -> JSON Document Contract), entries `{id, name, kind, token_hash, created_at, last_used_at, last_client, approved_by}` keyed by `id` (`dev_<16>`). Only the SHA-256 of a token's secret part is stored; the token is returned once.
- `cli/rpc_client.py::local_access_headers` sends the local access token to loopback targets only, for `rpc_call` and `stream_run_events`.

## Invariants

- Fail closed: an unknown route is `protected`, every WebSocket is `protected`, a device store that does not load completely makes the store unavailable (no Device signs in, management answers `devices_unavailable`, nothing is written over the file), an unreadable installation record refuses the packaged server's start, an unreadable data-directory access record refuses any other server's start.
- A data directory that records a mode never falls back to a weaker one: a start without access options serves the record, and a packaged server mirrors the installation's mode into it.
- The local access token never authenticates through a proxy or from another computer, and `rpc_client` never sends it to a non-loopback target.
- Never log or return tokens, token hashes, poll secrets, tickets or pairing codes. Refusal logs cut the path to two segments (`/api/files/...`), because deeper segments can carry capabilities or tickets.
- `access/devices.json` and `access/server.json` are outside data snapshots and configuration backups (`DOCUMENTS_OUTSIDE_SNAPSHOTS`): a restore must never revive a revoked Device or a weaker access mode. Deleting `devices.json` revokes every Device.
- Pairing requests, login tickets and rate-limit windows live in memory only; a restart drops them. Memory stays bounded: a rate-limit window keeps at most its limit of events per key and 4096 keys (least recently used forgotten first), a refusal summary names at most 10 clients, and there are at most 256 pending pairing requests and 256 login tickets.
- Rate limits count per effective client, an IPv6 client per /64. A direct-loopback client's pairing starts and polls count only against its own per-client limit, never the shared budget, and its requests are outside the pending cap, so a flood from elsewhere cannot lock the host out of pairing. A full pending table drops the oldest undecided request of the client holding the most; only when every counted request is approved does a start answer 429.
- Revoking or expiring a Device withdraws what it approved and nobody claimed yet: its approved pairing requests poll as `denied`, its login tickets are dropped. A claim also re-checks the approver (`DeviceStore.add` raises `DeviceNotFoundError` for an approver that is no longer paired), so no Device is created on behalf of a revoked one. Devices it paired earlier stay.
- A removal (revoke, expiry) takes effect in memory before it is written; a failed write leaves the Device revoked for this process and answers `devices_unavailable`.
- A Device unused for 90 days expires: at start, and on its next use. Last use is kept in memory and written at most once an hour per Device, and at shutdown.
- No RPC changes the access mode; `vbot application access set` records a change that applies at the next start.
- Device management needs a principal. Approving requires a signed-in principal (`host` or `device`), never `open`. `vbot devices approve` refuses inside an Agent's Run (`VBOT_RUN_AGENT_ID`).

## Constraints & Gotchas

- New server routes go under an existing namespace (`/api`, `/ws`, `/_vbot`, `/auth`, `/health`). A GET/HEAD route anywhere else is classified `shell` and served without a credential in `authenticated` mode. A new namespace must be added to both `_SERVER_NAMESPACES` (`_rules.py`) and `_is_reserved_server_path` (`app.py`). For the same reason FastAPI's `/docs`, `/redoc` and `/openapi.json` are disabled.
- An exempt route is added only as an exact `(method, path)` in `_EXEMPT_ROUTES` or as a GET/HEAD capability route shape in `_CAPABILITY_ROUTES` whose handler verifies its own HMAC. The exempt set today: `/health`, `GET /api/auth/status`, `POST /api/auth/pair/start|poll`, `GET /api/oauth/callback` (the `SameSite=Strict` cookie is not sent on the cross-site return; the pending sign-in's state protects it), `POST /_vbot/control/shutdown` (its control secret protects it), `GET /auth/ticket/<ticket>` (the guard also exempts HEAD there; no handler answers it), and GET/HEAD of `/api/files/<cap>`, `/api/preview-assets/<cap>/<path>`, `/api/extension-assets/<cap>/<path>` and `/api/extension-runs/<cap>/events|timeline`. Any other method or shape under these prefixes is `protected`.
- Direct TLS serves HTTPS and plain HTTP on one listener (`tls.py`): the first byte `0x16` starts a TLS session, anything else is HTTP, which the guard admits only from loopback in `authenticated` mode. So local tools keep `http://127.0.0.1:<port>`. Unclassified connections close after 10 s, handshakes after 30 s, and shutdown aborts them. The certificate files are checked every 60 s; a change is loaded for new connections without restart, a broken renewal keeps the previous certificate and warns once.
- A reverse proxy on the same computer that adds no forwarding header is indistinguishable from a local client. In `local`/`lan` such a setup publishes the server without login; only `authenticated` mode is safe behind any proxy. Behind a trusted proxy the proxy must append the client to `X-Forwarded-For`, set `X-Forwarded-Proto` and pass `Host` through; `USAGE.md` -> Server access has an nginx example.
- `lan` trusts the source address. Anything that forwards outside traffic with a private or loopback source address (router port forwarding with source NAT, Windows `netsh interface portproxy`, Docker's userland proxy, SSH or VPN tunnels ending on the host) admits those clients without login. Reach vBot from outside the home network only in `authenticated` mode.
- The Host check accepts every IP address, so a client can always use the server's address. A name the server does not know (a LAN DNS name, a router alias) needs `--allow-host`; without it the browser gets 403 "unknown host name".
- Anyone with a shell on the host as the same user can read the local access token, which includes Agents' shell Tools. The token is equivalent to host access; the Agent Run guard on `devices approve` stops only accidental approvals.
- An older vBot version's `Installation.save()` drops the `server_access` record field; the mode is then inferred from the host. A downgrade to a version without access modes runs without login.
- In `local`/`lan` a credential that does not verify is ignored (`open`), and `devices.list|rename|revoke` work for `open` principals, so the host can clean up Devices after leaving `authenticated` mode.
- Logging out (`devices.logout`) cannot delete the HttpOnly cookie through RPC; the next request carries the dead cookie, is refused once (WARN) and clears it.

Log lines: INFO `Pairing approved|denied (request=... kind=... actor=...)`, `Device paired (device=... kind=... approved_by=... via=pairing|ticket)`, `Device not paired: its approver was revoked (approved_by=... via=...)`, `Device renamed|revoked (device=... actor=...)`, `Device expired (device=... idle_days=90)`, `Approvals of revoked devices withdrawn (pairings=... tickets=...)`, `TLS certificate reloaded|reload recovered (cert=...)`; WARN `Access refused (reason=... client=... path=...)` (plus `host=...` for `host_not_allowed`) for the first refusal of a reason, then one `Access refused repeatedly (reason=... count=... clients=... interval=60s)` per minute while it continues (at most 10 clients named, then `...`), `Device store unavailable; ...`, `Device store change failed`, `Writing device last use failed`, `Removing expired devices failed`, `TLS certificate reload failed; serving the previous certificate`; DEBUG pairing requests and unknown codes. Refusal reasons: `unknown_peer`, `proxy_headers`, `peer_not_admitted`, `invalid_forwarded_for`, `insecure`, `host_not_allowed`, `malformed_credentials`, `invalid_local_token`, `local_token_not_local`, `invalid_token`, `expired_device`, `invalid_ticket`, `pairing_rate_limited`.

Tests: `tests/server/test_access.py` (modes x route classes, WebSocket and SSE, forwarding-header spoofing, Host names, local token rules, cookies, pairing and ticket lifecycle, rate limits and the pending cap, revocation closing or refusing requests and withdrawing approvals, the principal binding, idle expiry, memory bounds), `tests/server/test_tls.py`, `tests/server/test_main.py` (startup configuration, installation and data-directory record precedence), `tests/core/access/` (including `test_server_record.py`), `tests/cli/test_server_management.py` (a start targets the recorded bind host), `tests/core/utils/test_atomic.py` (owner-only files), `tests/cli/test_cli_devices.py`, `tests/cli/application/test_command.py`, `test_install.py`, `test_state.py`.

## References

Read only when your task matches:

- Building or changing an accessor's sign-in (pairing, login tickets, the device cookie, accepted Host names, handling 401/403/429 and WebSocket 4401/4403, the local access token), or changing `/api/auth/*`, `/auth/ticket/*`, `auth.login_ticket` or the `devices.*` RPCs -> `access/accessor-contracts.md`
