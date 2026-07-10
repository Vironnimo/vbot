# Desktop

pywebview-based desktop accessor that embeds the normal WebUI and talks only to the vBot server over HTTP.

## Overview

`desktop/` owns the native window shell around the existing WebUI. It does not import core/server business logic and it does not manage vBot server processes. Desktop stays intentionally thin: it loads the same server-served WebUI that a browser would load from `/`, but inside a pywebview window — and because the server it loads can be remote (e.g. a Raspberry Pi), a Pi-server + Windows-client topology is a primary intended use.

Server selection lives **inside the window**: a shell-owned native connection screen (`desktop/connection.py`) lists remembered servers, lets the user add/select/remove one, and auto-connects to the last-used target on launch. There is no silent localhost default and no dead-end error page — every probe failure lands the user back on that same interactive screen with the failed host/port prefilled. A native "Server" menu switches/reconnects servers at runtime. See the **Connection screen** and **Desktop Client** terms below.

The Desktop also includes a local wakeword voice pipeline (`desktop/wakeword/`) that runs entirely on-device: openWakeWord detection → sounddevice recording with webrtcvad silence detection → upload to the vBot speech endpoint → send transcript as a chat message through server RPC. The voice stack (`sounddevice`/`webrtcvad`, alongside `openwakeword`) ships in the `[desktop]` optional-dependency group, so a standard Desktop install runs real (non-mock) wakeword detection out of the box.

## Terms

Domain-specific vocabulary for the Desktop accessor.

### Desktop Client
**Definition:** A server-less Desktop install: the pywebview accessor installed alone (`.[cli,desktop]`) with no server stack, no local WebUI build, no data-dir, and no autostart, meant to connect to a *remote* vBot server (e.g. a Pi). Created by `install.ps1 -DesktopClient` / `install.sh --desktop-client`; a Desktop add-on (`-Desktop` / `--desktop`) instead bolts the same accessor onto a full server install.
**Not:** A full install that happens to include the Desktop, and not the running window itself. The Desktop Client is the *install shape* — the absence of the whole server side — not the GUI process. The window it opens is still the same pywebview shell; what differs is that nothing local is there to connect to.

### Connection screen
**Definition:** The Desktop shell's own native, in-window server-selection/error screen (`desktop/connection.py`, rendered HTML — not a WebUI route). It lists remembered servers, takes a host/port to connect, and on any probe failure (unreachable / not-vBot / no-WebUI / invalid target) re-renders in place with the failed target prefilled and an inline error. It subsumes the retired static fallback page, so the Desktop never shows a dead-end.
**Not:** A WebUI view or page. The Connection screen is shell-owned native HTML the controller swaps onto the same window via `Window.load_html`; the WebUI (loaded via `Window.load_url`) is the *other* thing that window shows once connected.

## Interfaces

- `python desktop/main.py [--host] [--port] [--mock-wakeword]` (the same entrypoint `vbot desktop` invokes; see `cli.md`)
  - **Launch target.** An explicit `--host`/`--port` is a *deliberate* override and connects straight to that target (a missing half fills from `127.0.0.1`/`8420`). With **neither** flag given, the controller auto-connects to the last-used remembered server, or shows the connection screen on first run. The old silent `127.0.0.1:8420` auto-default is gone — `127.0.0.1:8420` survives only as a prefill *suggestion* in the connect form, never as an auto-connect target.
  - **Probe contract (`probe_target`).** Probes `GET /health` first and treats HTTP 200 with body exactly `{"status":"ok"}` as the vBot identity contract; then probes `/` and accepts a 2xx/3xx WebUI root. The four probe outcomes (`server_unreachable`, `not_vbot_server`, `webui_unavailable`, `invalid_target`) all render inline in the connection screen — there is no separate static fallback page anymore.
  - **Voice follows the window's active server.** At launch the effective target (override else last-used) seeds the voice worker's `server_url`; thereafter every successful in-window connect retargets and, when enabled, rebuilds the worker. An unchanged target is a no-op. An empty target produces the actionable `no_server` error without loading the engine or opening a microphone.
  - `--mock-wakeword` explicitly selects the no-microphone `MockWakewordWorker` for UI validation; it is the only path that simulates the state cycle. Without the flag, missing `openwakeword` or `sounddevice` selects `UnavailableWakewordWorker`, which exposes `voice_stack_unavailable` and never simulates listening or sending. `getWakewordStatus().mode` distinguishes `real` / `mock` / `unavailable` and the WebUI explains the selected mode.
  - pywebview ordering constraint: the window is created **before** the GUI loop with the connection screen as neutral initial content; `Window.load_url` / `load_html` may only run *after* `webview.start`, so the controller's connect/auto-connect is the post-loop entry callable passed to `start`.
- **Connection controller (`desktop/connection.py`).** `ConnectionController` holds the live pywebview `Window` (handed over after creation via `attach_window`, since the window does not exist when the menu/bridge are wired) and funnels every action through `connect(host, port)`: a successful probe remembers the target, marks it last-used, navigates the window to `…/?accessor=desktop` via `Window.load_url`, and notifies its active-server listener with the plain base URL; any failure re-renders the connection screen via `Window.load_html`. `switch_to` / `reconnect` / `auto_connect` / `show_connection_screen` are thin wrappers. It reuses `probe_target` / `validate_host` / `validate_port` from `desktop/main.py` rather than re-deriving them. The active-server listener is set via `set_active_server_listener` (the launcher wires it to `bridge.set_server_url`, keeping the controller decoupled from the voice stack); a listener that raises is logged and swallowed so a worker-rebuild failure never breaks a navigation that already succeeded.
- **Remembered-servers operations** (`list_servers` / `add_server` / `remove_server` / `select_server` / `resolve_last_used`) wrap the settings store. `add_server` is keyed by `(host, port)` (a re-add refreshes the label in place, never duplicates); removing the last-used target also clears the last-used reference; `resolve_last_used` returns last-used → first remembered → `None` (first run).
- **Native "Server" menu** (`build_server_menu`, attached via `webview.start(menu=…)`). "Switch…" opens the connection screen so the user can pick another server; "Reconnect" retries the last-used target. The menu is present even on the connection/error screen, keeping switching decoupled from the server's WebUI. `webview.menu` is imported lazily (`load_menu_module`); tests inject a fake module.
- **Per-user Desktop settings store (`desktop/settings.py`).** Settings live in the **OS per-user config dir**, resolved by `resolve_config_dir`: Windows `%APPDATA%\vbot` (fallback `~/AppData/Roaming/vbot`), every other platform `$XDG_CONFIG_HOME/vbot` else `~/.config/vbot` (macOS falls into the XDG branch until a Mac installer exists). The file is `<config-dir>/settings.json`.
  - belongs to the Desktop app itself, not the shared server `data_dir`
  - on-disk schema `{ servers: [{host, port, label?}], last_used: {host, port}, wakeword: {…} }`; `last_used` is a `{host, port}` reference (not an index), so it survives list reordering
  - reads tolerate a missing/unreadable/malformed file by returning empty defaults; writes are an atomic same-directory temp-file replace and **preserve unrelated top-level keys** (a servers write keeps `wakeword`, etc.); malformed individual `servers` entries are dropped, not fatal
  - read/write retry a few times on transient I/O errors (e.g. a Windows file lock)
  - No legacy migration: the old program-adjacent `desktop/settings.json` is simply abandoned (it was gitignored dev state); users re-pick their server once.

### Wakeword settings schema

Nested under the `wakeword` key in the Desktop settings file:

```json
{
  "wakeword": {
    "enabled": false,
    "engine": "openwakeword",
    "microphone": null,
    "sensitivity": 0.5,
    "wake_phrase": "hey_jarvis",
    "server_profiles": {
      "http://pi.lan:8420": {
        "target_agent_id": "main",
        "session_behavior": "active"
      }
    }
  }
}
```

- `enabled` — whether the wakeword pipeline starts on Desktop launch
- `engine` — display name of the detection engine (MVP: "openwakeword")
- `microphone` — sounddevice device index or `null` for automatic selection; the worker tries the host defaults then compatible input devices, captures in a supported native format, and normalizes to 16 kHz mono PCM internally
- `sensitivity` — float 0–1, mapped to score threshold `1.0 - sensitivity`
- `wake_phrase` — the wakeword phrase ("hey_jarvis")
- `server_profiles` — map keyed by normalized active server base URL; each profile stores that server's Personal Agent `target_agent_id` and `session_behavior`, preventing a server switch from silently reusing another server's bare Agent id

### Python↔JS bridge

The Desktop exposes a single `DesktopBridge` instance as pywebview's `js_api`. The **same** bridge object stays the window's `js_api` across `Window.load_url` navigation, so it serves **both** callers: the shell connection screen (which calls the connection methods) and the remote WebUI (which calls the wakeword methods). The WebUI detects Desktop mode via the `?accessor=desktop` query parameter and calls bridge methods through `window.pywebview.api.<method>()`. All methods return plain Python objects that pywebview serializes to JSON. Because pywebview injects `window.pywebview.api` asynchronously, the WebUI waits up to ~5s for the `pywebviewready` DOM event before deciding Desktop capabilities are unavailable; on timeout it falls back to browser mode (`getDesktopCapabilities()` → `{ wakeword: false }`).

The connection methods delegate to the injected `ConnectionController` (the bridge owns no server-selection logic) and are serialized with a dedicated connection lock, separate from the wakeword config lock. The connection screen's JavaScript only ever calls `connect(host, port)`.

Bridge methods:

| Method | Returns | Description |
|---|---|---|
| `getDesktopCapabilities()` | `{ wakeword: true }` | Feature flags for WebUI gating |
| `getWakewordStatus()` | status dict | Current config + live worker state |
| `listMicrophones()` | device list | Input devices with compatibility and capture-rate metadata |
| `setWakewordEnabled(enabled)` | — | Enable/disable the worker |
| `setWakewordConfig(config)` | — | Partial config update, persists, recreates/restarts worker when enabled |
| `retryWakeword()` | — | Rebuild/restart an enabled worker after an actionable error |
| `connect(host, port)` | `{ status }` | Probe + navigate via the controller (used by the connection screen) |
| `listServers()` | `[{host, port, label?}]` | Remembered servers |
| `addServer(host, port, label?)` | stored entry | Remember a server without connecting |
| `removeServer(host, port)` | `{ removed }` | Forget a remembered server |
| `selectServer(host, port)` | `{ status }` | Select and connect to a remembered server |

The WebUI polls `getWakewordStatus()` every 500ms while Desktop is detected; settings UI polling carries only runtime fields into editable state. Status includes the current `state`, stable `error_code`, concrete `active_microphone`, `mode`, and a bounded sequence-numbered event history so short transitions such as detection are not lost between polls. App-level event consumption plays non-verbal Web Audio cues for detection, success, cancellation, no-speech/transcription failure, and fatal error; visual state remains authoritative if the host cannot play audio.

Worker states (exposed in `getWakewordStatus().state`): `off` → `starting` → `listening` → `wakeword_detected` → `recording` → `transcribing` → `sending` → `sent` → `listening`; recoverable utterance outcomes are `cancelled`, `no_speech`, and `transcription_failed`, while fatal startup/device/routing/send failures enter `error` with a machine-readable reason. The real worker closes the microphone while transcribing/sending, shows the outcome briefly, then reopens it. Detection re-arms only after the score falls below threshold. openWakeWord's bundled VAD gates detection; post-detection WebRTC VAD waits up to four seconds for speech, retains 300 ms of pre-speech padding, ends after 1.5 seconds of trailing silence, and caps the command at 15 seconds.

## Conventions

- Desktop is an accessor only, not a server manager.
- Desktop may connect to localhost or LAN vBot servers over normal HTTP.
- The loaded UI is the normal WebUI root path `/`; no separate desktop-only frontend build or route is part of the current contract. The connection screen is shell-rendered native HTML, not a WebUI route — `webui/` is untouched by this feature.
- The connection screen is **English-only** for now, mirroring the prior English-only Desktop fallback page (i18n deferred — see `FLAGGED.md`).
- Desktop inherits the WebUI 1:1, including the Projects tab. There is still **no native folder picker**: the bridge exposes no file/folder dialog, so adding a project uses the same hand-typed server-path input as the browser. This is deliberate — the server can be remote (e.g. a Pi), where a local picker would browse the wrong filesystem.
- The Desktop window title is `vBot`.
- The Desktop window enables pywebview document text selection so normal WebUI text can be selected inside the native shell.
- A custom `desktop/icon.png` is optional; when absent, pywebview's platform default icon is used.
- Closing the window ends only the Desktop process, never the target server.
- The Python↔JS bridge is a pywebview `js_api` object. Bridge methods execute in separate threads — implementations must be thread-safe.
- If the server is unreachable, is not a vBot server, or has no WebUI, Desktop stays open and shows the interactive connection screen (with the failed host/port prefilled and an inline error) instead of crashing or dead-ending.
- Hosts are plain host names or IP addresses only; schemes, paths, whitespace, and URL punctuation are rejected; a rejected host renders as the `invalid_target` connection-screen error.
- While listening is enabled, microphone audio is analyzed continuously on-device. Nothing is persisted or sent before a wakeword match; only the following command recording is uploaded for transcription.
- Startup validates the active server and that server's configured Personal Agent before loading the engine/opening the microphone. Missing server/target, a stale Agent id, model startup, device incompatibility, repeated reads, detection, session resolution, and send failures have distinct `error_code` values; stopped in-flight validation never continues into engine/microphone startup.
- A normalized transcript ending in the reserved German phrase `abbrechen` or `vergiss es` returns the recoverable `cancelled` outcome and is discarded before session resolution or `chat.stream`, so it cannot start a Run. The phrases are recognized only inside the same captured utterance; they do not cancel a Run that already started.
- `"active"` session behavior resolves the Agent's persisted `current_session_id` via `agent.get`; if unavailable, it falls back to the most recently active session from `session.list`, then creates a session.
- Transcripts are submitted with `chat.stream` and `input_origin: "speech_transcription"` so the Desktop worker returns to listening after the server accepts the Run instead of blocking until the Run completes, while the model still receives hidden context that the visible user text came from speech-to-text.
- Isolated microphone read errors during detection are recovered by reopening the stream; three consecutive failures transition to `error`.
- An empty/no-speech recording or failed transcription sends no chat message, exposes `no_speech` / `transcription_failed` long enough for visible and audible feedback, then returns to `listening`; one bad utterance must not stop future wakeword detection.
- A stop (disable / reconfigure / server switch) between capture and send discards the utterance: `_handle_detection` re-checks `_running` after recording and after transcription and bails without sending a now-stale command, and the retry loops honor `_running` (interruptible backoff, no retry after stop). A stop-induced empty resolve/send result does not publish `error` (the bridge publishes `off`). The in-flight HTTP request itself is best-effort — a single attempt may finish and its result is discarded.

## External Dependencies

All four ship in the `[desktop]` optional-dependency group (`pyproject.toml`). They are still imported lazily/optionally in code so the backend test gate never requires the GUI/audio stack.

- **pywebview** — native window wrapper used to host the existing WebUI and the connection screen; `webview.menu` provides the native Server menu.
- **openwakeword** — ONNX-based wakeword detection with its bundled VAD enabled to reduce non-speech activations. An import failure selects the non-simulating unavailable worker unless `--mock-wakeword` was explicitly requested.
- **sounddevice** — cross-platform PortAudio access. The worker probes device/rate/dtype support, prefers native 16 kHz when available, otherwise captures at a supported rate of at least 16 kHz and resamples to the engine's 16 kHz signed-PCM contract.
- **webrtcvad** — Google WebRTC VAD for silence detection during post-wakeword recording. Falls back to fixed-duration capture when not installed.

## Constraints & Gotchas

- A healthy vBot server may exist without `webui/dist`; in that case the probe returns `webui_unavailable` and Desktop shows the connection screen with a "WebUI unavailable" inline error (not a dead-end page).
- Desktop-local preferences must not be written into the shared server `data_dir`, because that directory belongs to the selected vBot instance. They live in the OS per-user config dir (`%APPDATA%\vbot` / XDG), which survives a package/venv reinstall — a real install puts the program inside a venv that is not user-writable.
- pywebview and `webview.menu` are imported lazily so backend tests and non-desktop development workflows do not require the optional GUI package. Behavior of runtime `Window.load_url` and the native `webview.menu` API varies by backend/version; the connection screen also auto-appears on any unreachable target, so server switching never depends solely on the menu.
- openWakeWord and sounddevice are optional imports — normal Desktop mode reports Voice unavailable when either is missing; only the explicit mock flag simulates activity. webrtcvad remains optional for post-wake silence detection; without it, the worker uses fixed-duration recording.
- Automatic microphone selection may choose a host-API default/fallback whose index differs from the persisted `microphone: null`; `active_microphone` is the authoritative runtime device shown beside the automatic option. Explicit unsupported devices stay visible but disabled in the picker.
- The real wakeword worker runs in a daemon thread. Fatal failures leave a stable reason until config change, Retry, disable, or server switch rebuilds it; recoverable utterance failures return to listening automatically.
- Bridge methods must return quickly and not block — they hold a threading.Lock for config access only during reads/writes to the local settings file.
