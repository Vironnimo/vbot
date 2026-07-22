# Desktop

pywebview-based desktop accessor that embeds the normal WebUI and talks only to the vBot server over HTTP.

## Overview

`desktop/` owns the native window shell around the existing WebUI. It does not import core/server business logic and it does not manage vBot server processes. Desktop stays intentionally thin: it loads the same server-served WebUI that a browser would load from `/`, but inside a pywebview window — and because the server it loads can be remote (e.g. a Raspberry Pi), a Pi-server + Windows-client topology is a primary intended use.

Server selection lives **inside the window**: a shell-owned native connection screen (`desktop/connection.py`) handles first run and launch failures, while the connected WebUI exposes the Desktop-local remembered-server list under Settings → Desktop app → Connection. The last-used target auto-connects on launch; there is no silent localhost default or dead-end error page. The window attaches no native menu, so Windows does not add a permanent menu bar for this rare action. See the **Connection screen** and **Desktop Client** terms below.

The Desktop also includes a local wakeword voice pipeline (`desktop/wakeword/`) that runs entirely on-device: one or two active TFLite detector heads sharing one feature extractor → sounddevice recording with webrtcvad silence detection → upload to the vBot speech endpoint → send transcript as a chat message through server RPC. `desktop/wakeword/engine.py` owns the curated built-in model catalog, shared `pyopen-wakeword` inference runtime, deterministic same-window winner selection, and validated Desktop-local imports of finished custom TFLite models; model training remains external to vBot. Okay Nabu and Hey Nabu are active by default. `pyopen-wakeword`, `sounddevice`, and `webrtcvad` ship in the `[desktop]` optional-dependency group, so a standard Desktop install runs real (non-mock) wakeword detection out of the box.

## Terms

Domain-specific vocabulary for the Desktop accessor.

### Desktop Client
**Definition:** A server-less Desktop install: the pywebview accessor installed alone (`.[cli,desktop]`) with no server stack, no local WebUI build, no data-dir, and no autostart, meant to connect to a *remote* vBot server (e.g. a Pi). Created by `install.ps1 -DesktopClient` / `install.sh --desktop-client`; a Desktop add-on (`-Desktop` / `--desktop`) instead bolts the same accessor onto a full server install.
**Not:** A full install that happens to include the Desktop, and not the running window itself. The Desktop Client is the *install shape* — the absence of the whole server side — not the GUI process. The window it opens is still the same pywebview shell; what differs is that nothing local is there to connect to.

### Connection screen
**Definition:** The Desktop shell's own native, in-window server-selection/error screen (`desktop/connection.py`, rendered HTML — not a WebUI route). It lists remembered servers, takes a host/port to connect, and on any probe failure (unreachable / not-vBot / no-WebUI / invalid target) stays in place with the failed target prefilled and an inline error. It subsumes the retired static fallback page, so the Desktop never shows a dead-end.
**Not:** A WebUI view or page. The Connection screen is shell-owned native HTML the controller swaps onto the same window via `Window.load_html`; the WebUI (loaded via `Window.load_url`) is the *other* thing that window shows once connected.

## Interfaces

- `python desktop/main.py [--host] [--port] [--mock-wakeword]` (the same entrypoint `vbot desktop` invokes; see `cli.md`); package installation also exposes the `vbot-desktop` GUI-script entrypoint, which Windows Start-menu shortcuts target so normal app launch uses the Windows GUI subsystem without a console.
  - **Launch target.** An explicit `--host`/`--port` is a *deliberate* override and connects straight to that target (a missing half fills from `127.0.0.1`/`8420`). With **neither** flag given, the controller auto-connects to the last-used remembered server, or shows the connection screen on first run. The old silent `127.0.0.1:8420` auto-default is gone — `127.0.0.1:8420` survives only as a prefill *suggestion* in the connect form, never as an auto-connect target.
  - **Probe contract (`probe_target`).** Probes `GET /health` first and treats HTTP 200 with body exactly `{"status":"ok"}` as the vBot identity contract; then probes `/` and accepts a 2xx/3xx WebUI root. The four probe outcomes (`server_unreachable`, `not_vbot_server`, `webui_unavailable`, `invalid_target`) all render inline in the connection screen — there is no separate static fallback page anymore.
  - **Voice follows the window's active server.** At launch the effective target (override else last-used) seeds the voice worker's `server_url`; thereafter every successful in-window connect retargets and, when enabled, rebuilds the worker. An unchanged target is a no-op. An empty target produces the actionable `no_server` error without loading the engine or opening a microphone.
  - `--mock-wakeword` explicitly selects the no-microphone `MockWakewordWorker` for UI validation; it is the only path that simulates the state cycle. Without the flag, missing `pyopen-wakeword` or `sounddevice` selects `UnavailableWakewordWorker`, which exposes `voice_stack_unavailable` and never simulates listening or sending. `getWakewordStatus().mode` distinguishes `real` / `mock` / `unavailable` and the WebUI explains the selected mode.
  - **Window-first startup.** The window is created **before** the GUI loop with the connection screen as neutral initial content; `Window.load_url` / `load_html` may only run after the loop is live, so the window's `shown` event runs connect/auto-connect and then starts Voice only when its persisted setting is enabled. With Voice disabled, normal Desktop startup never probes or imports `pyopen-wakeword` or `sounddevice`; enabled startup resolves those dependencies only after the native window is visible.
- **Connection controller (`desktop/connection.py`).** `ConnectionController` holds the live pywebview `Window` (handed over after creation via `attach_window`, since the window does not exist when the bridge is wired). `prepare_connect(host, port)` probes, remembers/marks a successful target, notifies the active-server listener, and returns a `PreparedConnection` containing either the accessor-marked navigation URL or inline error copy without replacing the current document; bridge callers use this seam so pywebview can deliver its Promise result before JavaScript navigates or updates the page. Native launch callers use `connect(host, port)`, which applies the same prepared outcome through `Window.load_url` / `Window.load_html`; `switch_to` / `reconnect` / `auto_connect` / `show_connection_screen` are thin wrappers. The controller reuses `probe_target` / `validate_host` / `validate_port` from `desktop/main.py` rather than re-deriving them. The active-server listener is set via `set_active_server_listener` (the launcher wires it to `bridge.set_server_url`, keeping the controller decoupled from the voice stack); a listener that raises is logged and swallowed so a worker-rebuild failure never breaks connection completion.
- **Remembered-servers operations** (`list_servers` / `add_server` / `remove_server` / `select_server` / `resolve_last_used`) wrap the settings store. `add_server` is keyed by `(host, port)` (a re-add refreshes the label in place, never duplicates); removing the last-used target also clears the last-used reference; `resolve_last_used` returns last-used → first remembered → `None` (first run).
- **Connected server management.** `webui/src/components/settings/DesktopConnectionSettings.svelte` is a Desktop-only client of the bridge operations: it marks the active target, manages remembered targets, and applies a successful prepared switch with `window.location.assign` only after the bridge Promise resolves. Settings groups Connection and Voice under the conditional Desktop app group. During a live outage AppShell's otherwise independent availability popup opens the same server picker in a modal outside the inert server-backed content, so removing the native menu does not remove recovery. First run and launch-time failure remain owned by the shell Connection screen because no WebUI is available yet.
- **Per-user Desktop settings store (`desktop/settings.py`).** Settings live in the **OS per-user config dir**, resolved by `resolve_config_dir`: Windows `%APPDATA%\vbot` (fallback `~/AppData/Roaming/vbot`), every other platform `$XDG_CONFIG_HOME/vbot` else `~/.config/vbot` (macOS falls into the XDG branch until a Mac installer exists). The file is `<config-dir>/settings.json`.
  - belongs to the Desktop app itself, not the shared server `data_dir`
  - on-disk schema `{ servers: [{host, port, label?}], last_used: {host, port}, wakeword: {…} }`; `last_used` is a `{host, port}` reference (not an index), so it survives list reordering
  - reads tolerate a missing/unreadable/malformed file by returning empty defaults; writes are an atomic same-directory temp-file replace and **preserve unrelated top-level keys** (a servers write keeps `wakeword`, etc.); each top-level section write holds one process-wide, per-file transaction lock across its complete read-modify-write, so concurrent pywebview threads cannot overwrite another section's change; malformed individual `servers` entries are dropped, not fatal
  - read/write retry a few times on transient I/O errors (e.g. a Windows file lock)
  - No legacy migration: the old program-adjacent `desktop/settings.json` is simply abandoned (it was gitignored dev state); users re-pick their server once.

### Wakeword settings schema

Nested under the `wakeword` key in the Desktop settings file:

```json
{
  "wakeword": {
    "enabled": false,
    "microphone": null,
    "active_model_ids": ["builtin/okay_nabu", "builtin/hey_nabu"],
    "model_sensitivities": {
      "builtin/okay_nabu": 0.5,
      "builtin/hey_nabu": 0.5
    },
    "server_profiles": {
      "http://pi.lan:8420": {
        "target_agent_id": "main",
        "session_behavior": "active"
      }
    }
  }
}
```

- `enabled` — whether the wakeword pipeline starts after the native Desktop window becomes visible; `false` keeps the ML/audio dependencies out of the startup path
- `microphone` — sounddevice device index or `null` for automatic selection; the worker tries the host defaults then compatible input devices, captures in a supported native format, and normalizes to 16 kHz mono PCM internally
- `active_model_ids` — ordered list of one or two unique catalog ids (`builtin/<name>` or `custom/<uuid>`); the default activates `builtin/okay_nabu` and `builtin/hey_nabu`, and the catalog keeps executable targets and local paths private
- `model_sensitivities` — optional float values keyed by model id, each constrained to 0.05–0.95 and mapped to score threshold `1.0 - sensitivity`; a missing entry uses 0.5, and inactive models retain their calibration
- `server_profiles` — map keyed by normalized active server base URL; each profile stores that server's Personal Agent `target_agent_id` and `session_behavior`, preventing a server switch from silently reusing another server's bare Agent id

Imported models live beside the Desktop settings file under `<config-dir>/wakewords/` as an opaque UUID-named `.tflite` file plus metadata. Imports accept one finished TFLite model up to 20 MiB, validate it by constructing a `pyopen-wakeword` detector before making it visible, and never expose the private local path to the WebUI. An import fills an available active-model slot; when both slots are occupied it remains installed but inactive. Deletion is permanent and is allowed only for an imported model that is not currently active. There is no legacy migration from the retired `engine` / `sensitivity` / `wake_phrase` or single `model_id` shapes.

### Python↔JS bridge

The Desktop exposes a single `DesktopBridge` instance as pywebview's `js_api`. The **same** bridge object stays the window's `js_api` across `Window.load_url` navigation, so it serves **both** callers: the shell connection screen and the remote WebUI. The WebUI detects Desktop mode via the `?accessor=desktop` query parameter and calls bridge methods through `window.pywebview.api.<method>()`. All methods return plain Python objects that pywebview serializes to JSON. A bridge method must never replace its calling document before returning: pywebview stores the Promise callback inside that document, so connection callers await a prepared payload and only then navigate with `window.location.assign` or update existing error elements through `textContent`. Because pywebview injects `window.pywebview.api` asynchronously, the WebUI waits up to ~5s for the `pywebviewready` DOM event before deciding Desktop capabilities are unavailable; on timeout both `wakeword` and `serverSelection` feature gates are false.

The connection methods delegate to the injected `ConnectionController` (the bridge owns no server-selection logic) and are serialized with a dedicated connection lock, separate from the wakeword config lock. The connection screen's JavaScript only ever calls `connect(host, port)`.

Bridge methods:

| Method | Returns | Description |
|---|---|---|
| `getDesktopCapabilities()` | `{ wakeword: true, serverSelection: true }` | Feature flags for WebUI gating |
| `getWakewordStatus()` | status dict | Current config + live worker state |
| `listMicrophones()` | device list | Input devices with compatibility and capture-rate metadata |
| `listWakewordModels()` | model descriptors | Curated TFLite built-ins (Okay Nabu and Hey Nabu first, then Hey Jarvis, Hey Mycroft, Hey Rhasspy, and Alexa) followed by valid Desktop-local imported models; descriptors expose id, label, source, format, and removability, never an executable target or local path |
| `importWakewordModel(filename, contentBase64)` | model descriptor plus `activated` | Validate and persist one finished custom TFLite model; activate it only when a slot is available |
| `deleteWakewordModel(modelId)` | `{ deleted: true }` | Permanently remove one inactive imported model; built-ins and all active models are protected |
| `setWakewordEnabled(enabled)` | — | Enable/disable the worker |
| `setWakewordConfig(config)` | — | Partial config update; validates one or two active model ids and keyed sensitivities, persists, and recreates/restarts the worker when enabled |
| `retryWakeword()` | — | Rebuild/restart an enabled worker after an actionable error |
| `connect(host, port)` | `{ status, url? / error_title?, error_body? }` | Prepare a connection for the shell Connection screen without replacing its calling document |
| `listServers()` | `[{host, port, label?, active}]` | Remembered servers with the window's active target marked |
| `addServer(host, port, label?)` | stored entry | Remember a server without connecting |
| `removeServer(host, port)` | `{ removed }` | Forget a remembered server |
| `selectServer(host, port)` | `{ status, url? / error_title?, error_body? }` | Prepare a remembered-server switch for JavaScript navigation |

The WebUI polls `getWakewordStatus()` every 500ms while Desktop is detected; settings UI polling carries only runtime fields into editable state. Status includes `active_model_ids` and their effective `model_sensitivities` alongside the current `state`, stable `error_code`, concrete `active_microphone`, `mode`, and a bounded sequence-numbered event history so short transitions such as detection are not lost between polls. App-level event consumption plays non-verbal Web Audio cues for detection, success, cancellation, no-speech/transcription failure, and fatal error; visual state remains authoritative if the host cannot play audio.

Worker states (exposed in `getWakewordStatus().state`): `off` → `starting` → `listening` → `wakeword_detected` → `recording` → `transcribing` → `sending` → `sent` → `listening`; recoverable utterance outcomes are `cancelled`, `no_speech`, and `transcription_failed`, while fatal startup/device/routing/send failures enter `error` with a machine-readable reason. The real worker closes the microphone while transcribing/sending, shows the outcome briefly, then reopens it. All active TFLite heads consume embeddings from one shared feature extractor; if multiple heads cross their thresholds in one window, the greatest threshold-normalized score wins and only one activation is emitted. Detection re-arms after all heads fall below threshold. The worker carries a rolling 320 ms detector pre-roll into the existing 300 ms pre-speech queue, so speech beginning in the trigger window is preserved but the wake phrase alone cannot become a command. WebRTC VAD then waits up to four seconds for post-detection command speech, ends after 1.5 seconds of trailing silence, and caps the command at 15 seconds.

## Conventions

- Desktop is an accessor only, not a server manager.
- Desktop may connect to localhost or LAN vBot servers over normal HTTP.
- The loaded UI is the normal WebUI root path `/`; no separate desktop-only frontend build or route is part of the current contract. Desktop-only controls are capability-gated sections/components in that shared WebUI, while the Connection screen remains shell-rendered native HTML rather than a route.
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
- Wakeword listening is independent of Chat TTS playback: asking an Agent to answer through TTS does not pause or disable the listener. This preserves interruption/readiness behavior but can let speaker output self-trigger the active model; the unresolved mitigation is tracked in `FLAGGED.md`.
- Startup validates the active server and that server's configured Personal Agent before resolving the selected catalog model, loading the engine, or opening the microphone. Missing server/target, a stale Agent id, missing/invalid model, model startup, device incompatibility, repeated reads, detection, session resolution, and send failures have distinct `error_code` values; stopped in-flight validation never continues into engine/microphone startup.
- A normalized transcript ending in the reserved German phrase `abbrechen` or `vergiss es` returns the recoverable `cancelled` outcome and is discarded before session resolution or `chat.stream`, so it cannot start a Run. The phrases are recognized only inside the same captured utterance; they do not cancel a Run that already started.
- `"active"` session behavior resolves the Agent's persisted `current_session_id` via `agent.get`; if unavailable, it falls back to the most recently active session from `session.list`, then creates a session.
- Transcripts are submitted with `chat.stream` and `input_origin: "speech_transcription"` so the Desktop worker returns to listening after the server accepts the Run instead of blocking until the Run completes, while the model still receives hidden context that the visible user text came from speech-to-text.
- Isolated microphone read errors during detection are recovered by reopening the stream; three consecutive failures transition to `error`.
- An empty/no-speech recording or failed transcription sends no chat message, exposes `no_speech` / `transcription_failed` long enough for visible and audible feedback, then returns to `listening`; one bad utterance must not stop future wakeword detection.
- A stop (disable / reconfigure / server switch) between capture and send discards the utterance: `_handle_detection` re-checks `_running` after recording and after transcription and bails without sending a now-stale command, and the retry loops honor `_running` (interruptible backoff, no retry after stop). A stop-induced empty resolve/send result does not publish `error` (the bridge publishes `off`). The in-flight HTTP request itself is best-effort — a single attempt may finish and its result is discarded.

## External Dependencies

All four ship in the `[desktop]` optional-dependency group (`pyproject.toml`). They are still imported lazily/optionally in code so the backend test gate never requires the GUI/audio stack; `pyopen-wakeword` and `sounddevice` are not probed until Voice actually starts.

- **pywebview** — native window wrapper used to host the existing WebUI and the Connection screen; no application menu is attached.
- **pyopen-wakeword** — Home Assistant's platform-specific TFLite runtime, shared streaming feature extractor, and packaged Okay Nabu, Hey Jarvis, Hey Mycroft, Hey Rhasspy, and Alexa models. vBot also bundles the MIT-licensed `hey_nabu_v2.tflite` model from the Home Assistant Wake Words Collection; its pinned source and SHA-256 live in `THIRD_PARTY_NOTICES.md`. A Python import failure selects the non-simulating unavailable worker unless `--mock-wakeword` was explicitly requested.
- **sounddevice** — cross-platform PortAudio access. The worker probes device/rate/dtype support, prefers native 16 kHz when available, otherwise captures at a supported rate of at least 16 kHz and resamples to the engine's 16 kHz signed-PCM contract.
- **webrtcvad** — Google WebRTC VAD for silence detection during post-wakeword recording. Falls back to fixed-duration capture when not installed.

## Constraints & Gotchas

- A healthy vBot server may exist without `webui/dist`; in that case the probe returns `webui_unavailable` and Desktop shows the connection screen with a "WebUI unavailable" inline error (not a dead-end page).
- Desktop-local preferences must not be written into the shared server `data_dir`, because that directory belongs to the selected vBot instance. They live in the OS per-user config dir (`%APPDATA%\vbot` / XDG), which survives a package/venv reinstall — a real install puts the program inside a venv that is not user-writable.
- pywebview is imported lazily so backend tests and non-desktop development workflows do not require the optional GUI package. Never call `Window.load_url` or `Window.load_html` synchronously inside a `js_api` method invoked by the document being replaced; return a `PreparedConnection` payload first and let that document apply the outcome after its pywebview Promise resolves. The native `shown` startup callback does not have this restriction and continues through `ConnectionController.connect` before optional Voice startup.
- `pyopen-wakeword` and sounddevice are optional imports — normal Desktop mode reports Voice unavailable when either is missing; only the explicit mock flag simulates activity. webrtcvad remains optional for post-wake silence detection; without it, the worker uses fixed-duration recording.
- Automatic microphone selection may choose a host-API default/fallback whose index differs from the persisted `microphone: null`; `active_microphone` is the authoritative runtime device shown beside the automatic option. Explicit unsupported devices stay visible but disabled in the picker.
- The real wakeword worker runs in a daemon thread. Fatal failures leave a stable reason until config change, Retry, disable, or server switch rebuilds it; recoverable utterance failures return to listening automatically.
- Sensitivity must stay below 1.0 because a zero threshold combined with `score >= threshold` turns every zero score into a detection. Winner selection compares score-to-threshold ratios rather than raw scores so independently tuned heads remain comparable.
- Ordinary bridge status/config methods must return quickly and not block — they hold the config/state lock only during local reads/writes. Explicit model import is bounded validation work and uses a separate catalog lock so TFLite loading cannot stall status polling or worker-state publication.
