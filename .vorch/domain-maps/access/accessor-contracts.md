# Access: accessor contracts

What an accessor (WebUI, Desktop, CLI/TUI, tray) relies on to reach a server in each Access Mode and to sign in. Rules and owners: `access.md`. Source: `server/access/_routes.py`, `_service.py`, `_guard.py`, `server/rpc/access_methods.py`; tests: `tests/server/test_access.py`.

## Errors and refusals

- HTTP errors of the guard and the `/api/auth/*` endpoints use the RPC envelope `{"ok": false, "error": {"code", "message"}}` with `Cache-Control: no-store`. Accessors branch on `code`, never on `message`.
  - 401 `auth_required` (`WWW-Authenticate: Bearer`): a protected request without a valid credential in `authenticated` mode. Show pairing or sign-in; a host tool re-reads the local access token once (the server writes a new one each start).
  - 403 `access_denied`: the peer or transport is not admitted (wrong network, proxy headers in `local`/`lan`, plain HTTP from another computer in `authenticated`). Retrying does not help; do not offer pairing.
  - 429 `rate_limited` with `Retry-After` (seconds): too many failed credentials, pairing starts or polls from this client.
  - 409 `auth_disabled`: pairing and login tickets exist only in `authenticated` mode.
  - 503 `devices_unavailable`: the device store cannot be read or written; the host must repair it.
  - 400 `invalid_request` (also 413/415 for an oversized or non-JSON body).
- WebSockets refused by the guard are accepted and closed at once: 4401 (sign in) or 4403 (not admitted), so the browser sees the code instead of a bare handshake failure. A Device's open WebSocket closes with 4401 `Device signed out` when it is revoked or expires; its SSE streams end. Accessors stop reconnecting on 4401/4403.
- RPC errors from the access RPCs travel in the normal `/api/rpc` envelope (HTTP 200): `auth_required`, `access_denied`, `rate_limited` (`data.retry_after`), `auth_disabled`, `devices_unavailable`, `invalid_request`, `pairing_not_found`, `device_not_found`.

## Credentials

- Device token `vbt_<device id>_<43 base64url>`: Desktop and CLI Devices send `Authorization: Bearer <token>`. It is shown once, in the approved poll answer; keep it in the accessor's secret storage.
- Browser Devices get the cookie `vbot_device` instead: `HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=34560000` (400 days). Page code never sees the token. `Authorization` wins when both are present. A refused cookie comes back cleared (`Max-Age=0`).
- Local access token `vbl_<43 base64url>` in `<data-dir>/runtime/local-access-<port>.token`, owner-only, rewritten at every server start: tools on the server's computer send it as `Authorization: Bearer` to a loopback URL (`http://127.0.0.1:<port>` works in every mode, also with direct TLS). Read it at each connection and again after a 401; never send it to another computer. `cli/rpc_client.py::local_access_headers` is the reference.
- In `local` and `lan` no credential is needed and a wrong one is ignored.
- A Device unused for 90 days expires and must pair again.

## Endpoints

All exempt from the credential requirement (the peer and transport rules still apply).

- `GET /api/auth/status` -> `{mode, mode_label, authenticated, principal, device}`: `mode` `local|lan|authenticated` with its label; `principal` `host|device|open` or `null`; `authenticated` is `principal != null` (so `true` with `open` in `local`/`lan`); `device {id, name, kind}` for a Device, else `null`. An accessor calls it first to decide whether to show pairing.
- `POST /api/auth/pair/start {kind, name}` (`kind`: `browser|desktop|cli`; `name` 1-80 characters, whitespace collapsed, no control characters) -> `{request_id, code, poll_secret, expires_in: 600, poll_interval: 2}`. Show `code` (`XXXX-XXXX`, alphabet `23456789ABCDEFGHJKMNPQRSTVWXYZ`) and tell the user to approve it on a device that is already signed in, or with `vbot devices approve <code>` on the server's computer. Keep `poll_secret` private; it alone can claim the result. Limits: 10 starts per client and 100 overall per 10 minutes, at most 256 pending requests (429).
- `POST /api/auth/pair/poll {request_id, poll_secret}` (each a string of at most 128 characters) -> one of
  - `{status: "pending", expires_in}`: poll again after `poll_interval`;
  - `{status: "approved", device: {id, name, kind}, token}` for `desktop`/`cli`; for `browser` the same without `token`, and the response sets the `vbot_device` cookie;
  - `{status: "denied"}`;
  - `{status: "expired"}`: unknown, expired, already claimed, or the secret does not match.
  The Device is created when the approved request is claimed; a decided request is handed out once, later polls answer `expired`. Limits: 60 polls per client and 600 overall per minute (429).
- `GET /auth/ticket/<ticket>`: redeems a login ticket once. Success: 303 to `/` with the `vbot_device` cookie, `Cache-Control: no-store`, `Referrer-Policy: no-referrer`; the new browser Device is named as the ticket asked and approved by the ticket's issuer. Failure (unknown, used, expired, or not `authenticated` mode): 400 HTML page "Sign-in link not valid", counted as a failed credential.

## RPCs

Via `POST /api/rpc`; each needs a principal, so in-process dispatch is refused with `access_denied`.

- `auth.login_ticket {name?}` -> `{ticket, path: "/auth/ticket/<ticket>", expires_in: 60}`: needs a signed-in `host` or `device` principal in `authenticated` mode; `name` defaults to `Browser`. Open `<public-url><path>` in the system browser at once; never log or store the ticket.
- `devices.list {}` -> `{devices: [{id, name, kind, created_at, last_used_at, last_client, approved_by, current}]}`, oldest first; `current` marks the calling Device; `approved_by` is `host` or a device id.
- `devices.rename {id, name}` -> `{device}`.
- `devices.revoke {id}` -> `{revoked: [id]}`; its live connections close at once.
- `devices.revoke_others {}` -> `{revoked: [...]}`: every Device except the calling one.
- `devices.logout {}` -> `{revoked: [own id]}`: only for a Device principal. The browser cookie cannot be deleted through RPC; the next request clears it.
- `devices.pending {code}` -> `{code, name, kind, client, requested_at, expires_in}`: what the approver confirms before approving (`client` is the requester's effective address).
- `devices.approve {code}` / `devices.deny {code}` -> `{status: "approved"|"denied", name}`.
  Pending, approve and deny need a signed-in principal in `authenticated` mode; codes are accepted in any case, with or without the dash or spaces. An unknown code is `pairing_not_found`; 10 wrong codes per principal (50 overall) per 10 minutes block further attempts (`rate_limited`).
- Failed credentials: 20 per client and 200 overall per 10 minutes, then 429 until the window frees up.
