# Desktop connection

Read when changing how the Desktop picks, probes and connects to a server: the launch target and launch wait, the probe and its outcomes, the Connection screen, `ConnectionController`, remembered servers, server switches from the WebUI, and how Voice and dictation follow the active server. Sign-in and pairing are in `desktop/sign-in.md`; the bridge methods behind the connection screen and the WebUI picker are in `desktop/bridge.md`.

## Connection screen

The Connection screen (`desktop/connection.py`, HTML from `desktop/connection_screen.py`, not a WebUI route) lists remembered servers (with "Forget sign-in" for signed-in ones), takes a host/port or `https://` address to connect, and on any probe or sign-in failure (unreachable / not-vBot / no-WebUI / invalid target / access denied / sign-in failures) stays in place with the failed target prefilled and an inline error. While the launch connect waits for an unreachable server it shows a waiting state naming the target instead (Launch wait); for a server that needs this Desktop paired it shows the pairing offer and then the code waiting for approval (`desktop/sign-in.md`). It subsumes the retired static fallback page, so the Desktop never shows a dead-end. It is shell-owned native HTML swapped onto the same window via `Window.load_html`; the WebUI (loaded via `Window.load_url`) is the other thing that window shows once connected.

- Unreachable/non-vBot/WebUI-less targets leave the window open on the interactive connection screen (failed values prefilled, inline error) instead of crashing or dead-ending. A host is a bare name/IP, or an `http://`/`https://` URL with at most a port; credentials, paths, queries, fragments, whitespace and other URL punctuation reject as `invalid_target`.
- A healthy server may exist without `webui/dist`: the probe returns `webui_unavailable` and the connection screen shows an inline "WebUI unavailable" error - not a dead end.

## Launch target

Explicit `--host`/`--port` is a deliberate override connecting straight to that target (missing half fills from `127.0.0.1`/`22332`; an `https://` address in `--host` carries its own port). With neither flag, the controller auto-connects to the last-used remembered server or shows the connection screen on first run - the old silent localhost auto-default is gone; `127.0.0.1:22332` survives only as a prefill suggestion in the connect form. A combined package defaults the packaged launch to its recorded server (`cli/application.md`).

The `--open-session` parameters (`open_agent`, `open_session`, contract in `desktop.md` -> Interfaces) ride only on the launch connect, never on later connects or server switches; a first run without a remembered server drops the request with the connection screen. Each value must be non-empty, printable, whitespace-free, and at most 512 characters.

## Launch wait

Every launch connect (a restart successor's server, explicit `--host`/`--port`, last-used) waits for a server that is still starting. While the probe says `server_unreachable`, the connection screen shows `Waiting for <host>:<port> …` with the target prefilled, and `ConnectionController` starts another attempt about every `LAUNCH_RETRY_SECONDS` (0.25 s) for up to `LAUNCH_WAIT_SECONDS` (60 s) on its own daemon thread (`vbot-desktop-launch-wait`). A success connects exactly as a first one would (same navigation URL with the restored location and `--open-session` parameters, one active-server notification); at the deadline the normal `server_unreachable` error shows. Other outcomes fail at once, also during the wait. A connect the user starts (`prepare_connect`: connection screen, WebUI server switch) never waits and ends the launch connect (and a pending pairing) under the controller's `_lock`, so a late launch outcome never replaces the user's choice; the screen hides the waiting state when the user connects. `ConnectionController.close()`, first in the shutdown, ends it when the window closes. The wait logs one INFO line when it starts and one when it ends (connected, or the user connected, or the window closed); giving up is the usual connection-failure WARNING with the waited time.

## Probe contract

`probe_target` probes `/health` against the vBot identity contract owned by `server.md`, then `/` accepting a 2xx/3xx WebUI root. Both requests share one bounded HTTP client/connection pool, closed on success or failure. The budget is `PROBE_TIMEOUT_SECONDS` (2 s); a loopback target (`localhost` or a loopback IP) gets only `LOOPBACK_CONNECT_TIMEOUT_SECONDS` (0.2 s) to connect, because a refused loopback connect takes about 2 s of SYN retries on Windows while a listening server's kernel completes the handshake at once, so a launch attempt or user connect against a server that is not listening yet fails in 0.2 s. The probe uses the Desktop's HTTP client rules (`desktop.md` -> Conventions); connection pools and cancellation remain per client. A 403 `access_denied` answer to `/health` is the outcome `access_denied` (the server's access guard refused this computer). After a successful WebUI root the probe reads `GET /api/auth/status` and reports the server's Access Mode (`access_mode`; `None` for a server without access modes). The failure outcomes (`server_unreachable`, `not_vbot_server`, `webui_unavailable`, `invalid_target`, `access_denied`) and the sign-in failures all render inline in the connection screen.

## ConnectionController

`ConnectionController` (`desktop/connection.py`) holds the live pywebview `Window` (handed over via `attach_window`; the window does not exist when the bridge wires). `prepare_connect(host, port)` probes, signs in when the server needs it, remembers/marks success, notifies the active-server listener, and returns a `PreparedConnection` (navigation URL, pairing offer, or inline error copy) *without* replacing the current document - pywebview must deliver its Promise result before JavaScript navigates. Each controller adds a fresh opaque `desktop_session` query value to that URL so the persistent WebView2 profile requests the current SPA document after an app restart. The launch uses `launch_connect(host, port)` / `auto_connect()`, which apply the outcome themselves through `load_url`/`load_html` (Launch wait above). `page_loaded`, wired to the window's `loaded` event, continues a window sign-in or shows the pairing offer after a page-started connect; `start_pairing(host, port, name)` asks an offered server for a code; `screen_key` is the key only the connection screen carries (`desktop/sign-in.md`). The controller reuses `probe_target` from `desktop/main.py` and address parsing from `desktop/server_address.py`; a raising active-server listener is logged and swallowed so a Voice retarget failure never breaks connection completion.

## Voice and dictation follow the active server

The effective launch target seeds their server URL; every successful in-window connect calls `VoiceController.set_server_url` and `DictationController.set_server_url` through the connection controller's active-server listener. For Voice a different URL rebuilds an enabled listener, an unchanged URL is a no-op, and an empty target fails the listener with `no_server` before any engine or microphone opens; dictation uses the URL from its next take on (an empty one fails that take with `server_unreachable`).

## Remembered servers

Remembered servers wrap the settings store: keyed by `(scheme, host, port)` (re-add refreshes the label, never duplicates; a successful connect without a label, such as choosing a saved server on the connection screen, keeps the stored one; a server that answered is remembered even when its sign-in fails or waits for pairing, but only a connect that navigates marks it last-used), removing a server also forgets its stored sign-in, removing last-used clears the reference, resolution order last-used -> first remembered -> none (first run). Every `(host, port)` surface (bridge, `--host`, restart and activation requests) carries an HTTPS server as `host` = its `https://` address (`ServerAddress.bridge_host`).

- Stored shape: `servers: [{host, port, label?, scheme?}]` and `last_used: {host, port, scheme?}` in `<config-dir>/settings.json`; `scheme` is written only as `"https"` (absent means HTTP, so older files keep working), and an entry with any other scheme is dropped on read.
- An older Desktop rewriting `servers` keeps only `{host, port, label}`: a downgrade turns an HTTPS entry into an HTTP entry on port 443, which then fails to connect.

## Server switches from the WebUI

Connected-server management lives in `webui/src/components/settings/DesktopConnectionSettings.svelte`, a Desktop-only bridge client applying prepared switches via `window.location.assign` after the Promise resolves. During a live outage AppShell's availability popup opens the same picker in a modal outside the inert content. First run and launch-time failure stay shell-owned - no WebUI exists yet.

## Tests

`tests/desktop/test_main.py` (probing, launch wiring, `--open-session` on a fresh start), `test_connection.py` (launch connect and its wait, user connects, sign-in flows), `test_connection_screen.py`, `test_server_address.py`, `test_settings.py`; WebUI: `webui/src/components/__tests__/DesktopConnectionSettings.test.js`.
