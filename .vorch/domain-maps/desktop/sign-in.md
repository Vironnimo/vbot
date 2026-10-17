# Desktop: server sign-in

Read when changing how the Desktop signs in to a server in the Access Mode `authenticated`: pairing on the connection screen, the window's login ticket, the stored device tokens, the local access token of the installation's own server, `Authorization` on native requests, and forgetting a sign-in. The Desktop root map is `desktop.md`; the server side and the wire contracts are `access.md` and `access/accessor-contracts.md`.

Source: `desktop/access.py` (`ServerAccess`: credentials and every sign-in HTTP call), `desktop/credentials.py` (`DeviceTokenStore`), `desktop/connection.py` (`ConnectionController`: what the window shows), `desktop/connection_screen.py` (pairing panel and sign-in error copy), `desktop/server_address.py` (addresses and origins), `desktop/speech/server_client.py` (`ServerAuthorization` on native requests). `desktop/main.py` builds one `ServerAccess` per process and hands it to the controller, Voice and dictation.

## When a sign-in happens

- After a successful probe (`/health`, then the WebUI root) `probe_target` reads `GET /api/auth/status` without a credential; `DesktopProbeResult.access_mode` carries `mode` when it is `local`, `lan` or `authenticated`. A server from before access modes (404) or an unreadable answer leaves it `None`: no sign-in, the WebUI's own gate stays the fallback.
- Only `access_mode == "authenticated"` signs in. `local`/`lan` servers open directly, as before.
- A 403 `access_denied` on `/health` is the probe outcome `access_denied` ("Access denied" on the connection screen); the Desktop never offers pairing for it.

## Credentials, in order

`ServerAccess` resolves one credential per server origin (`ServerAddress.origin`: scheme, lower-case host, port only when not the default):

1. The local access token, only for a loopback address when the Desktop was started with `--server-data-dir <data-dir>` (`cli/application/desktop.py` passes it for the installation's own server), read from `<data-dir>/runtime/local-access-<port>.token` at every use (at most 256 bytes, must match `vbl_<43>`). A refused one is read again once: the server writes a new one at each start. It is never sent to another host or port, and it is no pairing (`signed_in` stays false, nothing to forget).
2. The device token stored when this Desktop paired with that origin.
3. None: the Desktop pairs.

## Window sign-in (login ticket)

- `sign_in(address, landing)` calls `auth.login_ticket {name: "vBot Desktop window on <computer>", next: landing}` with the credential. `landing` is the WebUI page with the Desktop parameters (`ConnectionController._app_path`: `/?accessor=desktop&desktop_session=<id>`, plus a launch's `open_agent`/`open_session`). A 401 / `auth_required` drops a device token and tries the next credential; `auth_disabled` means the server left `authenticated` (open directly); `access_denied`, `rate_limited` and `devices_unavailable` pass through as connection statuses and drop nothing; anything else, including a malformed ticket path, is `sign_in_failed`.
- The window then loads `<base>/auth/ticket/<ticket>[<location>]` itself through `Window.load_url`, so the navigation is browser-initiated and WebView2 stores and sends the `SameSite=Strict` `__Host-vbot_device` cookie. The server redirects to `landing`; a launch's location fragment rides on the ticket URL, because a redirect without its own fragment keeps the request's (RFC 7231 7.1.2). So the window loads the WebUI once. On the next pywebview `loaded` event at the server's origin (`ConnectionController.page_loaded`, wired in `main.py`) the controller does nothing when the page carries its own `desktop_session`; a window the server sent elsewhere loads the WebUI URL itself; a window still on `/auth/ticket/...` means the server refused the ticket: the connection screen shows `sign_in_failed`.
- The pending sign-in (`_Landing`) is single-use and expires after `SIGN_IN_LANDING_SECONDS` (60 s, a ticket's lifetime); showing the connection screen drops it. Pages that load later are the WebUI's business.
- Invariant: a ticket never reaches page JavaScript. The bridge is injected into every page, including any server's WebUI, so a page-started connect (`connect`, `selectServer`) returns `url: "about:blank"` (`SIGN_IN_PAGE`) instead; the page navigates there and the controller opens the ticket on that page's `loaded` event. `PreparedConnection.ticket_url` stays out of the bridge payload and its repr.
- The first redemption creates a browser Device named `vBot Desktop window on <computer>`, approved by the credential's actor (this Desktop's device id, or `host`) with `via: "ticket"`. The WebView2 profile persists (`private_mode=False`), so the window keeps its cookie and later redemptions keep that Device (`access.md` -> Invariants); the Desktop does no window Device bookkeeping. Revoking or logging out this Desktop's Device revokes its window's Device with it on the server.

## Pairing

- `start_pairing` posts `{kind: "desktop", name: "vBot Desktop on <computer>"}` (computer name from `platform.node()`, control characters removed, at most 80 characters). The connection screen shows the pairing panel ("Approve this Desktop", the code, where to approve it: Settings -> Access & devices -> Add device on a signed-in device, or `vbot devices approve <code>` on the server's computer) with a Cancel button.
- A background worker (`vbot-desktop-pairing`) polls every `poll_interval` (clamped to 1-60 s); a 429 waits its `Retry-After` (at most 60 s); a poll without an answer (network error, a proxy's error page without the error envelope) is repeated until the code expires; another error envelope fails the pairing with that code. The deadline is `expires_in` from the start.
- Approved: the token is stored before anything else (also when the wait was ended meanwhile), then the Desktop signs in without pairing again and the window opens the ticket. Denied or expired: the screen shows "Sign-in denied" / "Code expired" when the connection screen started the pairing (`on_screen`).
- `cancelPairing()`, a connect the user starts, a lost sign-in reconnect, and `close()` end a pending pairing; a late outcome never replaces the window's content. A pairing started from the WebUI (`selectServer`) returns `pairing` plus the same copy as `error_title`/`error_body`; on approval the window navigates, failures are only logged.

## Native requests and lost sign-ins

- `SpeechServerClient` (Voice, dictation) sends `ServerAuthorization.header(server_url)` as `Authorization`. On a 401 it calls `rejected(server_url, header)`: a refused local access token retries once when the file changed; a refused device token is dropped and the sign-in-lost listener (the controller) learns about it, as it does for a 401 without a credential. A 401 that remains surfaces as the operation's error (`status_code` 401, `rpc_code` `auth_required`); a 403 is never retried.
- The controller reacts to a lost sign-in only for the window's server and only once per activation (`_lost_handled`, reset by the next successful connect): it runs a launch connect again, which shows pairing. So a server that keeps refusing never loops the window, and the Desktop never navigates because of what the WebUI shows; the WebUI handles its own lost cookie.

## Forgetting a sign-in

- `forgetSignIn(host, port)` and removing a remembered server (`removeServer`) call `ServerAccess.forget`: the local record goes at once; a background thread (`vbot-desktop-sign-out`) then calls `devices.logout` with the device token (best effort, logged), which revokes the window's ticket Device with this Desktop's. Forgetting the window's server's sign-in also marks its lost sign-in handled, so the window does not pair again by itself; the next connect does.
- The connection screen offers "Forget sign-in" beside each remembered server with a stored device token; `listServers` reports it as `signed_in`.

## Token store

- `<config-dir>/device-tokens.json` beside `settings.json` (never in `settings.json`, never in the server data dir), written atomically through an owner-only temporary file: `{format_version: 1, servers: {<origin>: {token_dpapi | token, device_id, paired_at}}}` (earlier builds also wrote `webview_device_id`; it is kept but no longer read).
- Windows wraps the token with DPAPI (`CryptProtectData`, current user, no UI, the origin as entropy) into `token_dpapi`; other platforms keep `token` in a `0600` file. Neither protects against another process of the same user; no keyring dependency.
- Only the Desktop writes the file. A Desktop Client's tray reads it through `DeviceTokenStore` to sign in as this Desktop (`cli/application/tray.md` -> Server connection) and never removes a token.
- Readers keep fields they do not know; a damaged document reads as empty and is replaced by the next write; a token that does not match its `device_id`, or a DPAPI blob another user or origin cannot open, reads as no token. `save_device` accepts only a `vbt_` token of the given device id.
- Tokens, tickets, poll secrets and pairing codes never appear in logs or exception messages.

## Constraints & Gotchas

- `http://127.0.0.1:<port>` reaches a direct-TLS server too (the server answers plain HTTP from loopback), so the installation's own Desktop keeps a loopback HTTP address and the local access token; `cli/application/desktop.py` maps a wildcard bind host (`0.0.0.0`, `::`, `*`, empty) to `127.0.0.1`. An own server bound to one specific non-loopback address cannot be reached over loopback; that Desktop pairs like any other.
- While the server's device store is unavailable it answers 503 `devices_unavailable` instead of 401 (`access.md` -> Invariants); the Desktop shows it as a connection status and keeps its token.
- A window whose WebView lost its cookie (a cleared profile) gets a new browser Device at the next sign-in; the old one stays until it expires after 90 days or this Desktop's Device is revoked. A window signed in with the local access token gets a Device approved by `host`, which no revocation of this Desktop takes along.

## Tests

`tests/desktop/test_access.py` (credential order, local token rules and re-read, the ticket's `next`, sign-in failures, the pairing state machine with a fake clock, native 401 handling, forget, no secrets in logs), `test_credentials.py` (round trip, DPAPI or `0600`, unknown fields, damaged documents), `test_connection.py` (ticket landing on the asked page with its fragment in one load, page-started connects never seeing the ticket, pairing on the screen and from the WebUI, cancel, lost sign-in once, forget), `test_connection_screen.py` (pairing panel, Forget sign-in, sign-in error copy), `tests/desktop/speech/test_server_client.py` (Bearer header and 401 retry). `tests/desktop/access_test_support.py` holds `FakeAccessServer`, an in-memory server answering the pairing endpoints and the access RPCs through `httpx.MockTransport`.
