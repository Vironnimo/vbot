# Access

Who may reach vBot: access modes, the server-edge guard, paired Devices, pairing, login tickets, the local access token and TLS.

## Overview

The host chooses one Access Mode at startup, never through RPC:

- `local` ("This computer only"): loopback peers only; the server must bind a loopback address (default `127.0.0.1`).
- `lan` ("Home network without login"): loopback, RFC 1918, link-local and IPv6 ULA peers plus each `--allow-network` CIDR; no login (default bind `0.0.0.0`).
- `authenticated` ("With login (internet)"): any peer, but every protected request needs a credential. It requires `--public-url https://<origin>` and HTTPS through either direct TLS (`--tls-cert` + `--tls-key`, default bind `0.0.0.0`) or at least one `--trusted-proxy` that terminates HTTPS (default bind `127.0.0.1`).

Labels: `core.access.config.ACCESS_MODE_LABELS`; flags and records use mode names.

Owners:

- `core/access/config.py`: `AccessConfig` and the mode, bind-host, host-name and TLS-context rules, used by `server/main.py`, `server/app.py` and `cli/application/` (installer, record, `vbot application access`).
- `core/access/server_record.py`: the data directory's access record `access/server.json`, which keeps a server's mode and bind host across restarts.
- `core/access/devices.py`: device tokens and the `DeviceStore` (`access/devices.json`).
- `core/access/local_token.py`: the local access token file and where a client may send the token (`LocalAccess.admits`).
- `core/utils/owner_only.py`: owner-only file permissions (POSIX `0600`; on Windows a protected DACL for the current user and `SYSTEM`), applied by the atomic writers' `owner_only=True` before any content is written; used for the local access token, the device store, the access record and the server control record.
- `server/access/`: the ASGI `AccessGuard` (`_guard.py`), `AccessService` (`_service.py`), pure peer/transport/route rules (`_rules.py`), pairing and login tickets (`_pairing.py`), rate limits and refusal logging (`_limits.py`), live connections (`_connections.py`), the principal (`_principal.py`), the unauthenticated endpoints (`_routes.py`) and direct TLS (`tls.py`).
- `server/rpc/access_methods.py`: `auth.login_ticket` and the `devices.*` RPCs.
- CLI: `vbot devices ...` (`cli/device_management.py`, pairing and logout in `cli/device_pairing.py`, `cli/server-access.md`), `vbot application access show|set` and the installer options (`cli/application.md`); `cli/server_client.py` attaches the credential (Invariants).

Not owned here: the browser-origin guard (`server/_origins.py`, `server.md`), which still runs after the access guard and also accepts the configured public origin; capability URL verification (`server/file_delivery.py`); Extension OAuth (`extensions/host.md` -> OAuth browser redirects) and its callback URL (`server/browser-capabilities.md`); credential storage.

How each accessor signs in: `access/accessor-contracts.md` -> Accessor notes.

## Terms

Core terms (Access Mode, Device, Accessor) live in `.vorch/GLOSSARY.md`.

### Principal
Who a request acts for: `host` (local access token), `device` (paired Device) or `open` (credential-free `local`/`lan`). Capabilities resolve their signed issuer; other credential-free exempt requests have no principal (`None`). Its `actor` in logs and the device store is the device id, `host` or `open`.

### Effective client
The address a request counts as coming from: the direct peer, or in `authenticated` mode the address a trusted proxy forwarded. Rate limits, refusal logs and a Device's `last_client` use it.

### Local access token
A `vbl_` bearer token created fresh at every server start and written, once the server listens, to the owner-only `<data-dir>/runtime/local-access-<port>.token`; removed at stop unless a newer server replaced it. The server accepts it only from a direct loopback peer without any forwarding header; where clients may send it: Invariants (file format: `access/accessor-contracts.md` -> Credentials).

### Pairing
How a new Device gets a token: it requests pairing and shows an 8-character code; someone already signed in (host or Device) approves the code; the new Device claims its token by polling. Requests live in memory for 10 minutes; a settled outcome answers repeated polls for a 60-second claim grace.

### Login ticket
A single-use, 60-second secret a signed-in principal creates (`auth.login_ticket`) so that a top-level browser navigation to `/auth/ticket/<ticket>` signs the browser in and lands it on the ticket's `next` page: a new browser Device with its cookie, or the browser's existing browser Device when it is signed in already. Accessors holding a credential (Desktop, CLI tray) use it to open the WebUI in a browser or the Desktop window.

### Ticket browser
A browser Device created by redeeming a login ticket (`via: "ticket"`, `approved_by` the issuer). Revocation and expiry follow its current issuer transitively; another issuer's ticket can renew the delegation. Independently paired Devices (`via: "pairing"`) never join that cascade.

### Route class
`classify_route` sorts every request into `exempt` (no Bearer/cookie required), `shell` (the public WebUI shell: any GET/HEAD outside the server namespaces `/api`, `/ws`, `/_vbot`, `/auth`, `/health`) or `protected` (everything else, every WebSocket). It fails closed.

## Request pipeline

`create_app` adds `AccessGuard` last, so it runs first: access guard -> browser-origin guard -> verification-only guard (verification mode) -> routes. For each HTTP request and WebSocket (rules in detail: `access/request-pipeline.md`):

1. Route class (above).
2. Peer and transport (`_rules.inspect_request`); a refusal answers 403 `access_denied` (WebSocket close 4403). In `local`/`lan` any proxy or CDN header refuses the request (a proxy in front means it may come from anywhere), as does a peer outside the admitted networks. In `authenticated` forwarding headers count only when the direct peer is a trusted proxy, and a direct peer needs HTTPS unless it is loopback. uvicorn runs with `proxy_headers=False`, so nothing else interprets forwarding headers. In every mode the `Host` must be an IP address or a name the server knows (`HostPolicy`, against DNS rebinding; a name it does not know, such as a LAN DNS name, needs `--allow-host`), else 403 "unknown host name".
3. Credential (`AccessService.authenticate`): `Authorization: Bearer` wins over the `__Host-vbot_device` cookie. A `vbl_` token counts only from a direct loopback peer, a `vbt_` token through the device store. In `local`/`lan` a refused credential still yields `open`; in `authenticated` it is logged, counted per effective client, and a dead cookie is cleared.
4. A `protected` route, or `GET /api/auth/status`, whose device token the unavailable device store could not check answers 503 `devices_unavailable` (Invariants). Otherwise a `protected` route without a principal answers 401 `auth_required` with `WWW-Authenticate: Bearer` (WebSocket 4401), or 429 `rate_limited` with `Retry-After` once the client is over its failed-credential limit.
5. The principal goes into `scope["vbot.principal"]`; `/api/rpc` binds it for its own task (`bound_principal`), where RPC handlers read it with `current_principal()` (Interfaces).
6. Every Device request, including its capability requests, registers in `LiveConnections`; revocation or expiry cancels body/admission waits and open connections. Owners settle admitted mutations; pending answers become 401.
7. Every HTTP response gets security headers (`nosniff`, HSTS over HTTPS, the WebUI CSP on `shell` routes); a header the route set itself wins.

## Interfaces

- `create_app(access=AccessConfig, local_access_token=str)`: without `access` the mode follows the bind host (loopback -> `local`, otherwise `lan`); in-process test apps rely on this. The service is `app.state.access`.
- `server/main.py` resolves the Access Mode before serving (`_resolve_access`); an invalid configuration prints `vBot server cannot start: <reason>` to stderr and exits `STARTUP_FAILED_EXIT_CODE` (3). Precedence and the records: `access/server-setup.md`; the start line carries `access=<mode>` (`server.md`).
- `current_principal()` is `None` outside the `/api/rpc` request's own task: tasks the request starts (Runs, background work) inherit the context but get `None`, and in-process RPC dispatch (Agents, Live) runs with `None`, so none of them can use the access RPCs: they refuse with `access_denied`.
- Device store: Generation 1 document `access/devices.json` (kind `access_devices`); only the SHA-256 of a token's secret part is stored, the token is returned once (fields and lifecycle: `access/devices-and-pairing.md`).
- Device changes: `AccessService.start(data_dir, on_change=...)` calls its listener on the Event Loop after every Device and pairing change; `server/app.py` publishes it as `resource_changed(kind="devices")` (`server/events-and-reconnect.md`).

## Invariants

- Fail closed: an unknown route is `protected`, every WebSocket is `protected`, a device store that does not load completely makes the store unavailable (no Device signs in, management answers `devices_unavailable`, nothing is written over the file), an unreadable installation record refuses the packaged server's start, an unreadable data-directory access record refuses any other server's start.
- An unavailable device store is neither a sign-in nor a sign-out: in `authenticated` mode a protected request or `GET /api/auth/status` carrying a well-formed device token gets 503 `devices_unavailable` with `Retry-After: 30` (WebSocket 4503); the token is not counted as a failure and its cookie is not cleared. A missing or malformed credential still gets 401. No accessor (CLI, tray, TUI, Desktop, WebUI) removes a credential for a 503; only a 401 for that credential does.
- A data directory that records a mode never falls back to a weaker one: a start without access options serves the record, and a packaged server mirrors the installation's mode into it.
- The local access token never authenticates through a proxy or from another computer. No accessor sends it anywhere but plain `http://` to a loopback IP literal the token file records, on the file's port (`LocalAccess.admits`; the Desktop's `desktop/access.py` applies the same rule): a host name may resolve to an address another program holds (`localhost` -> `::1` while the server holds `127.0.0.1`), and a request to any other address may reach whatever listens there, another user's program included. `cli/server_client.py` (`http_options`, `websocket_target`) applies this, and sends a stored Device token only to its own `https://` origin, for every CLI, TUI and tray request; any other target carries no credential.
- Device tokens and pairing travel only over `https://` (CLI and Desktop). A plain `http://` server in `authenticated` mode is reachable for credentials only from its own computer, through the local access token. Decision: an SSH or other tunnel to a remote server that requires login does not sign in; the user connects to the server's `https://` address or runs `vbot` on the server's computer (`USAGE.md` -> Server access).
- Never log or return tokens, token hashes, poll secrets, tickets or pairing codes. Refusal logs cut the path to two segments (`/api/files/...`), because deeper segments can carry capabilities or tickets.
- `access/devices.json` and `access/server.json` are outside data snapshots and configuration backups (`DOCUMENTS_OUTSIDE_SNAPSHOTS`): a restore must never revive a revoked Device or a weaker access mode. Deleting `devices.json` revokes every Device.
- Pairing requests, login tickets and rate-limit windows live in memory only, bounded; a restart drops them.
- No RPC changes the access mode; `vbot application access set` records a change that applies at the next start.
- Device management needs a principal. Approving requires a signed-in principal (`host` or `device`), never `open`.
- Actions that hand out access refuse inside an Agent's Run (`VBOT_RUN_AGENT_ID`, `cli.device_management.run_refusal`): `vbot devices approve`, the tray's login ticket minting, and `vbot application access set`. The guard lives in the CLI only. The server has no reliable signal that a request comes from a Run (an Agent runs as the same user as the host, can unset the variable and read the local access token, and the CLI forwards no marker), so `devices.approve`/`devices.deny` do not check one; do not add a weaker one.

## Constraints & Gotchas

- New server routes go under an existing namespace (`/api`, `/ws`, `/_vbot`, `/auth`, `/health`). A GET/HEAD route anywhere else is classified `shell` and served without a credential in `authenticated` mode. A new namespace must be added to both `_SERVER_NAMESPACES` (`_rules.py`) and `_is_reserved_server_path` (`app.py`). For the same reason FastAPI's `/docs`, `/redoc` and `/openapi.json` are disabled.
- An exempt route is added only as an exact `(method, path)` in `_EXEMPT_ROUTES` or as a GET/HEAD capability route shape in `_CAPABILITY_ROUTES` whose handler verifies its own HMAC; in `authenticated` mode the guard also verifies its recipient grant (`server/browser-capabilities.md`). Other methods or shapes under these prefixes are `protected`. The exempt set: `access/request-pipeline.md` -> Exempt routes.
- Direct TLS serves HTTPS and plain HTTP on one listener (`tls.py`); the guard admits plain HTTP only from loopback in `authenticated` mode, so local tools keep `http://127.0.0.1:<port>`.
- A reverse proxy on the same computer that adds no forwarding header is indistinguishable from a local client. In `local`/`lan` such a setup publishes the server without login; only `authenticated` mode is safe behind any proxy (proxy setup: `access/server-setup.md`).
- `lan` trusts the source address. Anything that forwards outside traffic with a private or loopback source address (router port forwarding with source NAT, Windows `netsh interface portproxy`, Docker's userland proxy, SSH or VPN tunnels ending on the host) admits those clients without login. Reach vBot from outside the home network only in `authenticated` mode.
- Anyone with a shell on the host as the same user can read the local access token, which includes Agents' shell Tools. The token is equivalent to host access; the CLI's Agent Run guard (Invariants) stops only accidental approvals, sign-in links and access mode changes.
- Refusals log the first of each reason at WARN, then one summary line per minute while they continue (`_limits.py`; lines and reasons: `access/request-pipeline.md` -> Refusal reasons).

Tests: `tests/server/test_access.py`, `test_access_lifecycle.py`, `test_tls.py`, `test_main.py`, `tests/core/access/`, `tests/core/utils/test_atomic.py`, `tests/cli/test_cli_devices.py`, `test_server_management.py`, `tests/cli/application/`.

## References

Read these only when your task matches - not by default.

- Building or changing an accessor's sign-in (pairing, login tickets, the device cookie, accepted Host names, handling 401/403/429/503 and WebSocket 4401/4403/4503, the local access token), or changing `/api/auth/*`, `/auth/ticket/*`, `auth.login_ticket` or the `devices.*` RPCs -> `access/accessor-contracts.md`
- Changing the guard's peer, forwarding-header, Host, credential, exempt-route or response-header rules, or the refusal reasons (`_rules.py`, `_guard.py`, `AccessService.authenticate`) -> `access/request-pipeline.md`
- Changing how a server resolves or records its Access Mode at start (`_resolve_access`, the access records, `vbot application access`), reverse-proxy setup, or direct TLS (`tls.py`) -> `access/server-setup.md`
- Changing Devices, pairing claims, login tickets, rate limits, revocation, ticket browsers, Device names or expiry (`DeviceStore`, `_pairing.py`, `_limits.py`) -> `access/devices-and-pairing.md`
