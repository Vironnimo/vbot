# Desktop

pywebview-based desktop accessor that embeds the normal WebUI and talks only to the vBot server over HTTP.

## Overview

Packaged Windows distribution is owned by `cli/application/`, not Desktop. The native `vBot.exe` tray controls the local server and launches `vBot.Desktop.exe` independently. The installed Desktop shortcut uses the stable GUI companion `vBot.GUI.exe desktop`, which resolves the active version without opening a console. A combined package defaults that launch to its recorded server; explicit host/port and Desktop Client last-used behavior remain available. Closing Desktop does not stop the server. Package updates retain files needed by open Desktop processes. A launch while a Desktop is open only focuses that window (single instance, see Interfaces), so the new active version starts with the next launch after it closed. See `cli/windows-application.md`.

`desktop/` owns the native window shell around the existing WebUI. It does not import core/server business logic and it does not manage vBot server processes. Desktop stays intentionally thin: it loads the same server-served WebUI a browser would load from `/`, inside a pywebview window. The server may be local or remote.

Server selection lives **inside the window**: a shell-owned native connection screen (`desktop/connection.py`) handles first run and launch failures, while the connected WebUI exposes the Desktop-local remembered-server list under Settings -> Desktop app -> Connection. The last-used target auto-connects on launch; there is no silent localhost default and no dead-end error page. No native application menu is attached.

pywebview disables its renderer's default right-click menu outside Debug Mode, so the connected WebUI supplies a capability-gated Desktop context menu instead of enabling the whole debug surface. AppShell owns viewport/focus behavior for it; `desktop/system_actions.py` owns the thread-safe native clipboard and default-browser boundary. Ordinary browser accessors keep their native context menu.

Desktop Voice (`desktop/wakeword/`) listens on-device for wake phrases. A phrase either records a spoken command, which is uploaded as WAV to the active server's speech endpoint, transcribed there with the same transcription-audio profile as the Chat microphone, and sent to an Agent as a chat message through RPC, or asks the page to start or toggle Live voice. Detection, speech detection, echo cancellation, and command endpointing run locally; wake phrase model training stays outside vBot. Ownership and invariants are below (Voice); feature contracts are in `desktop/voice.md`.

Live voice (the `live_voice` Task Model, see `model_tasks/live.md`) runs in the loaded WebUI page; the Desktop supports it without owning the call. A wake phrase or an optional global shortcut asks the page to start or toggle a call; Desktop Voice keeps listening during a call, and the page holds the call while a spoken command records and sends the command wake phrases with each start; WebView2 switches make remembered remote HTTP servers secure contexts with microphone access. Contracts: `desktop/live-voice.md`.

## Terms

Domain-specific vocabulary for the Desktop accessor.

### Desktop Client
**Definition:** A server-less Desktop install: the pywebview accessor installed alone (`.[cli,desktop]`) with no server stack, no local WebUI build, no data-dir, and no autostart, meant to connect to a *remote* vBot server. Created by `install.ps1 -DesktopClient` / `install.sh --desktop-client`; a Desktop add-on (`-Desktop` / `--desktop`) instead bolts the same accessor onto a full server install.
**Not:** A full install that happens to include the Desktop, and not the running window itself. The Desktop Client is the *install shape* - the absence of the whole server side - not the GUI process.

### Connection screen
**Definition:** The Desktop shell's own native, in-window server-selection/error screen (`desktop/connection.py`, rendered HTML - not a WebUI route). It lists remembered servers, takes a host/port to connect, and on any probe failure (unreachable / not-vBot / no-WebUI / invalid target) stays in place with the failed target prefilled and an inline error. It subsumes the retired static fallback page, so the Desktop never shows a dead-end.
**Not:** A WebUI view or page. The Connection screen is shell-owned native HTML swapped onto the same window via `Window.load_html`; the WebUI (loaded via `Window.load_url`) is the *other* thing that window shows once connected.

### Desktop Voice
**Definition:** The Desktop's on-device wake phrase listener (`desktop/wakeword/`, "Voice" in Settings and code). A wake phrase is one active wakeword model; its phrase action decides what a detection does.
**Not:** Live voice (the page's `live_voice` Task Model call) or the Chat microphone (browser recording in the composer). Both are WebUI features that also run in browsers.

### Phrase action
**Definition:** What a detected wake phrase does on the current server: `command` (record a spoken command and send it to an Agent and Session behavior, defaulting to the server profile's) or `live_voice` (ask the page to start or toggle Live voice; nothing is recorded). Stored per server profile in `phrase_actions` (`desktop/voice.md` -> Stored settings).

## Interfaces

- `python desktop/main.py [--host] [--port] [--mock-wakeword]` (the entrypoint `vbot desktop` invokes; see `cli.md`); package installation also exposes the `vbot-desktop` GUI-script entrypoint that Windows Start-menu shortcuts target, so normal launch uses the GUI subsystem without a console.
- **Launch target:** explicit `--host`/`--port` is a deliberate override connecting straight to that target (missing half fills from `127.0.0.1`/`8420`). With neither flag, the controller auto-connects to the last-used remembered server or shows the connection screen on first run - the old silent localhost auto-default is gone; `127.0.0.1:8420` survives only as a prefill suggestion in the connect form.
- **Probe contract (`probe_target`):** probe `/health` against the vBot identity contract owned by `server.md`, then `/` accepting a 2xx/3xx WebUI root. Both requests share one bounded HTTP client/connection pool, closed on success or failure. Desktop probes and every Voice readiness check, speech upload, and RPC call use `trust_env=False`, so ambient proxy or `.netrc` configuration cannot divert local audio, transcripts, or RPC bodies. The four outcomes (`server_unreachable`, `not_vbot_server`, `webui_unavailable`, `invalid_target`) all render inline in the connection screen.
- **Voice follows the window's active server:** the effective launch target seeds Voice's server URL; every successful in-window connect calls `VoiceController.set_server_url` through the connection controller's active-server listener. A different URL rebuilds an enabled listener; an unchanged URL is a no-op; an empty target fails the listener with `no_server` before any engine or microphone opens.
- `--mock-wakeword` selects the mock mode (no microphone, no network) for UI validation - the only path that simulates Voice activity. Without it, a failed probe of the on-device stack selects the unavailable mode (`voice_stack_unavailable`, never simulating). The status `mode` distinguishes `real`/`mock`/`unavailable`.
- **Window-first startup:** the window is created before the GUI loop with the connection screen as initial content; `load_url`/`load_html` may only run once the loop is live. Its pywebview `background_color` is the canonical WebUI `Bg` (`#15130F`), so WebView2 exposes the app foundation instead of its white default during startup, navigation, restore, or renderer repaint gaps. The `shown` callback connects first, then starts Voice (`voice.start()`, a no-op while disabled) and the Live voice shortcut. With Voice disabled, startup never imports the capture, detection, command, echo, or speech-detection modules or the ML/audio dependencies (the package root loads `VoiceController` lazily). Fresh-process coverage: `tests/desktop/test_startup.py`.
- **Single instance (Windows, `desktop/_windows.py`):** before creating a window, `launch_desktop` claims a named mutex scoped to the Desktop config directory (`LocalBot.Desktop.<sha256 prefix of the resolved directory>`). A later launch for the same directory signals the running instance through a named auto-reset event and returns without a window (its explicit `--host/--port` is logged and ignored); the running instance's daemon listener brings its window to the front, restoring a minimized window (maximized again if it was). Different config directories (tests, portable setups) never collide; a failure to create the guard logs and continues unguarded; other platforms have no guard. A second process would share the WebView2 profile (differing browser arguments leave a blank window) and compete for the microphone and the global shortcut.
- **ConnectionController (`desktop/connection.py`)** holds the live pywebview `Window` (handed over via `attach_window`; the window does not exist when the bridge wires). `prepare_connect(host, port)` probes, remembers/marks success, notifies the active-server listener, and returns a `PreparedConnection` (navigation URL or inline error copy) *without* replacing the current document - pywebview must deliver its Promise result before JavaScript navigates. Each controller adds a fresh opaque `desktop_session` query value to that URL so the persistent WebView2 profile requests the current SPA document after an app restart. Native callers use `connect(host, port)` applying the same outcome through `load_url`/`load_html`. The controller reuses `probe_target`/`validate_host`/`validate_port` from `desktop/main.py`; a raising active-server listener is logged and swallowed so a Voice retarget failure never breaks connection completion.
- **Remembered servers** wrap the settings store: keyed by `(host, port)` (re-add refreshes the label, never duplicates), removing last-used clears the reference, resolution order last-used -> first remembered -> none (first run).
- **Connected-server management** lives in `webui/src/components/settings/DesktopConnectionSettings.svelte`, a Desktop-only bridge client applying prepared switches via `window.location.assign` after the Promise resolves. During a live outage AppShell's availability popup opens the same picker in a modal outside the inert content. First run and launch-time failure stay shell-owned - no WebUI exists yet.
- **Per-user settings store (`desktop/settings.py`):** lives in the OS per-user config dir (Windows `%APPDATA%\vbot`; elsewhere `$XDG_CONFIG_HOME/vbot` else `~/.config/vbot`), file `<config-dir>/settings.json`. It belongs to the Desktop app, never the shared server `data_dir`. Schema `{servers:[{host,port,label?}], last_used:{host,port}, window:{width,height}, wakeword:{...}, live_voice:{hotkey:{...}}}`; `wakeword` is owned by `desktop/wakeword/config.py` (`desktop/voice.md` -> Stored settings), `live_voice` is described in `desktop/live-voice.md`. Section owners use `read_section(key)` and `update_section(key, mutate)`: `mutate` gets an isolated copy and returns the whole new section, an unchanged section is not rewritten, and a raising `mutate` leaves the file untouched. Reads tolerate missing/broken files with empty defaults and drop malformed entries individually; writes are atomic same-directory replacements preserving unrelated top-level keys, each section write holding one process-wide transaction lock across its read-modify-write. An unreadable existing file, invalid UTF-8/JSON or non-object JSON rejects section mutations without replacing the original bytes; startup reads still use defaults. Transient I/O retries a few times; a final failed write logs and re-raises so bridge callers reject instead of faking success. No legacy migration - the retired program-adjacent file was gitignored dev state.
- **Standalone logging:** `desktop/main.py` attaches the `vbot.*` logger tree to daily files under `<config-dir>/logs/` in the server's format. Voice logs each actual state change (`Voice state: <state> (<error_code>)`), listener starts, readiness changes, and command outcomes with codes and ids; unchanged states stay silent. Logs never contain transcripts. This is Desktop-local because a Desktop Client has no server `data_dir`.
- **Window sizing:** first-run window at roughly 80% of the primary display capped at 1440x960 logical pixels (1280x800 if discovery fails), resize floor 800x600. Normal close persists width/height only; startup restores clamped to the current primary display's work area. The primary screen is the one containing origin (0,0) - never assume `screens[0]` - and is passed via pywebview's `screen=` parameter for correct centering and DPI conversion. Position is deliberately not persisted so OS centering keeps the window reachable after monitor changes.
- **Windows monitor scaling:** `desktop/_windows.py` establishes PerMonitorV2 before pywebview screen discovery. The Python-hosted .NET Framework also needs the `windows.config` opt-in and an explicit .NET 4.8 target before WinForms loads; Windows awareness alone leaves dynamic layout disabled. WinForms/WebView2 then handle monitor DPI changes, including native and browser layout. Primary screen/work-area snapshots from pywebview are physical pixels in this mode and are converted to logical pixels before sizing and centering. This bootstrap applies to packaged and source Desktop entrypoints. Regression coverage: `tests/desktop/test_windows.py`.
- **Native system actions (`desktop/system_actions.py`):** serialized clipboard/browser operations with payload-size validation and absolute HTTP(S)-only external URLs; Windows uses the native Unicode clipboard API, macOS `pbcopy`/`pbpaste`, Linux wl-copy/xclip/xsel with an operational failure when none exists. This boundary exists because a remote HTTP WebUI cannot reliably use the secure-context Clipboard API.

### Python<->JS bridge

One `DesktopBridge` instance (`desktop/bridge.py`) is pywebview's `js_api` and stays so across navigation, serving **both** the shell connection screen and the remote WebUI (detected via `?accessor=desktop`; calls go through `window.pywebview.api.<method>()`; methods return plain serializable objects). A bridge method must never replace its calling document before returning - the Promise callback lives in that document - so connection callers await a prepared payload and only then navigate or update error elements via `textContent`. Because pywebview injects `window.pywebview.api` asynchronously, the WebUI keeps capability gates false and retries discovery until a live bridge answers; transient failures are never converted into authoritative disabled capabilities or empty Voice state. Every call runs on its own thread. The facade holds no Voice state: Voice methods delegate to `VoiceController`, which serializes its own mutations; `_connection_lock` serializes server-selection calls. The connection screen's JavaScript only ever calls `connect(host, port)`.

Error contract (`_reject_with_error_codes`, applied to every public method): an exception whose `error_code` is a stable code (`[a-z][a-z0-9_]*`) is logged at WARNING (with its `field`, if any) and rejects the Promise with a `BridgeError` whose message is exactly that code; the WebUI reads it with `desktopErrorCode`. Any other failure is logged with its traceback and keeps its own message; a message that would look like a code is replaced by `The Desktop could not complete <method>`, so only deliberate codes reach the page as codes.

Bridge methods, grouped:

- Capabilities: `getDesktopCapabilities()` - `{wakeword: true, voiceApi: 2, serverSelection: true, contextMenu: true, liveHotkey, secureOrigins}`. The WebUI enables Voice only for `voiceApi === 2`.
- Clipboard/browser: `setClipboardText`, `getClipboardText` (bounded plain text), `openExternalUrl` (validated absolute HTTP(S) only).
- Servers: `connect`, `selectServer` (both return prepared outcomes - `{status, url?}` or inline error fields - never navigating themselves), `listServers` (active marked), `addServer`, `removeServer`.
- Voice (`desktop/voice.md` -> Bridge methods): `getVoiceStatus`, `setVoiceEnabled`, `updateVoiceConfig`, `listMicrophones`, `listWakewordModels`, `importWakewordModel`, `deleteWakewordModel`, `retryVoice`, `stopVoiceRecording`, `startVoiceCalibration`, `restartVoiceCalibration`, `stopVoiceCalibration`.
- Live voice shortcut: `getLiveHotkey` / `setLiveHotkey` (`desktop/live-voice.md`).

### Page pushes

`desktop/page_events.py` (`PageEventDispatcher`) is the only path from Python into the loaded page. It dispatches window events through `Window.evaluate_js`: `vbot-desktop-live` (cancelable Live voice requests, `desktop/live-voice.md`) and `vbot-desktop-voice` (Voice status snapshots and events, `desktop/voice.md` -> Events and push). `evaluate_js` blocks until the page answers and deadlocks on the GUI thread, so one daemon thread (`vbot-desktop-page-events`, started with the first push) delivers every push in order; producers never block, and the queue is bounded (at most 4 Live requests, one waiting status, 64 events). Pushes before `attach_window` are dropped. Delivery failures log one warning per failure streak; `close()` discards waiting pushes. A page without handlers (the connection screen) ignores the events.

### Voice

Ownership (all under `desktop/wakeword/`):

- `controller.py` - `VoiceController`, the single Voice owner: config application, listener lifecycle, detection dispatch, readiness, status snapshot and events, calibration, mock and unavailable modes. `VoiceRuntime` holds replaceable dependencies (tests inject doubles).
- `config.py` - the stored `wakeword` section: tolerant parsing, strict change validation, server profiles, effective phrase actions, the config part of the status. Pure, no I/O.
- `capture.py` - `AudioCapture` (one microphone stream fanned out to bounded subscriptions, recovery), the `EchoStage` protocol, and the process-wide `EchoStagePool`.
- `echo.py` - the WebRTC echo stage and its Windows playback loopback reference.
- `detection.py` - `DetectionLoop` over one capture subscription; `_speech_detection.py` - Silero/WebRTC speech decisions and the `SpeechGate`.
- `commands.py` - `CommandRecorder` (endpointing, WAV) and `CommandPipeline` (transcribe, resolve Session, send).
- `server_client.py` - `VoiceServerClient`, every HTTP call Voice makes.
- `engine.py` - model catalog, imports, and `MultiWakewordEngine`; `calibration.py` - `PhraseCalibration`; `_microphones.py` - device identity, capture formats, `AUDIO_BACKEND_LOCK`.
- Outside the package: `desktop/bridge.py` (facade), `desktop/page_events.py` (push), `desktop/main.py` (wiring, stack probe).

Threads and lifecycle:

- One listener generation owns capture, detection, recorder, command workers, and a server client, all stopped by one stop event. Every change that needs a new listener (enable, microphone, echo cancellation, active phrases or sensitivities, server URL, Retry) runs as a transition on a background thread: signal the old generation, shut it down with bounded joins (threads that do not stop in time are abandoned with a warning), then build the next. Routing changes (phrase actions, default Agent or Session behavior) apply without restarting capture.
- Callbacks carry their generation; callbacks of an older generation are dropped, so a slow thread can never revive or corrupt a newer listener.
- `start()` runs from the `shown` callback, `close()` once in `desktop/main.py`'s `finally`.
- Locks: `_mutation_lock` serializes bridge mutations (settings write, then apply); `_catalog_lock` serializes model file access; `_lock` guards runtime state and is never held across network calls, joins, or PortAudio calls. Status pushes and events are published under `_lock` so their order matches the shared sequence number.

## Conventions

- Desktop is an accessor only, not a server manager. Closing the window ends only the Desktop process, never the target server. It connects to localhost/LAN servers over normal HTTP.
- The loaded UI is the normal WebUI root `/` - no desktop-only frontend build or route exists. Desktop-only controls are capability-gated sections in that shared WebUI; the Connection screen stays shell-rendered native HTML, English-only for now (i18n deferred - see `FLAGGED.md`).
- Desktop inherits the WebUI 1:1 including Projects. There is deliberately **no native folder picker**: adding a project uses hand-typed server-path input, because the server can be remote where a local picker would browse the wrong filesystem.
- The Desktop enables document text selection; its capability-gated context menu exposes Copy for selected non-sensitive text, link copy/open for safe HTTP(S), Cut/Paste for writable controls (password fields Paste without leaking selections). Escape/outside press/scroll/resize dismiss it. The window title is `vBot`.
- Platform icons come from `desktop/icon.ico` (Windows; WinForms requires ICO - PNG input is rejected by `System.Drawing.Icon`) and `desktop/icon.png` (other platforms), passed to pywebview when present with a platform-default fallback.
- Bridge methods execute in separate threads - implementations must be thread-safe.
- Unreachable/non-vBot/WebUI-less targets leave the window open on the interactive connection screen (failed values prefilled, inline error) instead of crashing or dead-ending. Hosts are bare names/IPs only; schemes, paths, whitespace, URL punctuation reject as `invalid_target`.
- While listening, microphone audio is analyzed continuously on-device; nothing persists or leaves the device before a wake phrase matches, and only the command recording that follows is uploaded.
- Voice keeps listening during Chat TTS playback and Live voice calls. Echo cancellation (setting `echo_cancellation`, on by default) removes speaker output before detection and recording, with the playback loopback as reference on Windows. Where it is disabled, unavailable, or has no reference (`no_reference`, e.g. other platforms), speaker output can still trigger phrases.
- Readiness problems (no usable `speech_to_text`, unknown Agent, unreachable server) never refuse the enable switch: they appear per phrase in the status and block only that phrase's commands. Command failures are events; one bad utterance never stops detection.

## External Dependencies

All ship in the `[desktop]` optional group (`soxr` also in `[dev]` for tests); the audio and ML packages import lazily, so the backend test gate never needs the GUI/audio stack. The Windows runtime locks (`scripts/windows/requirements-*desktop*.lock`) pin them for packaged installs.

- **pywebview** - native window wrapper hosting the WebUI and Connection screen; no application menu attached.
- **pyopen-wakeword** (`pyopen_wakeword`) - platform-specific TFLite runtime, shared streaming feature extractor, packaged built-in models; vBot additionally bundles MIT-licensed `hey_nabu_v2.tflite` (pinned source + SHA-256 in `THIRD_PARTY_NOTICES.md`).
- **sounddevice** - PortAudio access for microphone enumeration and capture, preferring the device's default rate (minimum 16 kHz).
- **soxr** - stateful anti-aliasing resampling to the 16 kHz detection projection (linear interpolation aliased device noise straight into the detector spectrum). LGPL-2.1+; see `THIRD_PARTY_NOTICES.md`.
- **webrtcvad-wheels** - WebRTC VAD, the fail-open fallback for speech decisions when the neural detector cannot load.
- **onnxruntime** - runs the bundled Silero VAD v5 ONNX model (2.2 MB, vendored at `desktop/wakeword/models/silero_vad.onnx`, MIT, SHA-256 pinned in `THIRD_PARTY_NOTICES.md`) for noise-robust detection gating and endpointing on one CPU thread (~0.1 ms per 32 ms window). Optional: an absent or broken onnxruntime selects the WebRTC fallback and never fails Voice startup. Wheels exist for Windows x64/arm64, macOS arm64, and Linux x64/aarch64.
- **livekit** - only its local WebRTC audio processing module (AEC3 echo canceller, high-pass filter) through the bundled native FFI library; vBot contacts no LiveKit server. Apache 2.0, WebRTC BSD; see `THIRD_PARTY_NOTICES.md` -> Echo cancellation dependencies. Missing or failing -> echo cancellation `unavailable`, Voice keeps listening.
- **PyAudioWPatch** (Windows only) - WASAPI loopback capture of the default playback device as the echo reference; ships its own PortAudio copy, independent of sounddevice's. Missing -> echo state `no_reference`.

The stack probe that selects the unavailable mode (`_real_wakeword_available` in `desktop/main.py`) imports `pyopen_wakeword`, `sounddevice`, `soxr`, and `webrtcvad`; onnxruntime and livekit are optional within a working listener.

## Constraints & Gotchas

- A healthy server may exist without `webui/dist`: the probe returns `webui_unavailable` and the connection screen shows an inline "WebUI unavailable" error - not a dead end.
- Desktop-local preferences must never live in the shared server `data_dir` (it belongs to the selected instance); they live in the OS per-user config dir, which survives package/venv reinstalls - a real install puts the program in a non-user-writable venv.
- pywebview is imported lazily so backend tests and non-desktop workflows never require the GUI package. Never call `Window.load_url`/`load_html` synchronously inside a `js_api` method invoked by the document being replaced - return the `PreparedConnection` payload first and let that document apply it after its Promise resolves. The native `shown` startup callback is exempt and proceeds through `ConnectionController.connect` before optional Voice startup.
- Never call `Window.evaluate_js` from a bridge method or the GUI thread; push through `PageEventDispatcher`. Voice callbacks that reach the page (`live_requests`, the event sink) and `DetectionLoop.on_detection` must return promptly: they run on the capture/detection path.
- Keep clipboard and external-browser access behind `DesktopSystemActions` - never `navigator.clipboard`/`window.open`: the selected server can be remote plain HTTP and renderer behavior differs by platform. The Python boundary repeats HTTP(S)-only validation even though AppShell filters schemes first.
- Keep Voice imports lazy: the capture, detection, command, echo, and speech-detection modules and their native packages load only when a real listener is built (`VoiceController._build_session`). `tests/desktop/test_startup.py` fails when disabled Voice loads them.
- Every sounddevice/PortAudio query, open, close, and refresh goes through `AUDIO_BACKEND_LOCK` (`desktop/wakeword/_microphones.py`); a PortAudio refresh invalidates open streams, so it is skipped while a Voice stream is open, and the echo stage closes its loopback before a refresh.
- The livekit native side terminates the whole Desktop process on malformed audio frames; `desktop/wakeword/echo.py` validates every frame before it crosses the FFI. Keep that check when touching the echo stage.
- Only one capture owns the microphone; every consumer (detection, command recording) reads its own bounded subscription. A slow consumer gets an `overrun` gap, never back-pressure on capture; a command recording that sees any gap is discarded rather than uploaded with missing audio.
- WebView2 browser switches (secure remote HTTP origins, autoplay) travel through `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`, set right before `webview.start` and read once per process: a server added later is not a secure context until the Desktop restarts, and elevated processes ignore the variable. See `desktop/live-voice.md`.

## Tests

- Shell: `tests/desktop/test_main.py` (probing, launch wiring), `test_connection.py`, `test_settings.py`, `test_system_actions.py`, `test_windows.py` (DPI, single instance, browser arguments, permission hook), `test_bridge.py` (capabilities, error contract, system actions, servers, Voice delegation, hotkey), `test_page_events.py`, `test_hotkey.py`, `test_startup.py` (fresh-process import guard), `test_wakeword_settings.py` (`--mock-wakeword`).
- Voice: `desktop/voice.md` -> Tests. Live voice: `desktop/live-voice.md` -> Tests.
- WebUI: `webui/src/components/__tests__/DesktopConnectionSettings.test.js`, `AppShell.test.js` (Desktop context menu), `webui/src/lib/__tests__/desktopBridge.test.js`.

## References

Read these only when your task matches - not by default.

- Desktop Voice internals (listener pipeline and lifecycle detail, stored `wakeword` settings, Voice bridge methods, status snapshot, pushed events, readiness, command recording and sending, server client, microphones and capture, echo cancellation, wake phrase models, calibration, mock mode) -> `desktop/voice.md`
- Live voice from the Desktop (Live voice phrase action, global shortcut, pushed Live requests, the Live hold and wake phrases during a call, WebView2 secure origins, autoplay, and microphone permission) -> `desktop/live-voice.md`
