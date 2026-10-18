# Desktop

pywebview-based desktop accessor that embeds the normal WebUI and talks only to the vBot server over HTTP.

## Overview

`desktop/` owns the native window shell around the existing WebUI. It does not import core/server business logic and it does not manage vBot server processes: closing the window ends only the Desktop process, never the target server. Desktop stays intentionally thin: it loads the same server-served WebUI a browser would load from `/`, inside a pywebview window. The server may be local or remote; the Desktop connects to localhost/LAN servers over normal HTTP and to internet servers over HTTPS.

Packaged Windows distribution (the `vBot.exe` tray, `vBot.Desktop.exe`, the `vBot.GUI.exe desktop` shortcut) is owned by `cli/application/` (`cli/application.md`), not Desktop. A launch while a Desktop is open only focuses that window (`desktop/window.md` -> Single instance).

Server selection lives **inside the window**: a shell-owned native Connection screen handles first run, launch failures and pairing, while the connected WebUI exposes the Desktop-local remembered-server list under Settings -> Desktop app -> Connection. The last-used target auto-connects on launch; there is no silent localhost default and no dead-end error page. A server is a host and port (HTTP) or an `https://` address (`desktop/connection.md`).

A server in the Access Mode `authenticated` needs a sign-in (`desktop/access.py`): the own server's local access token over loopback, or the device token of a paired `https://` server; the window opens a login ticket natively (`desktop/sign-in.md`).

pywebview disables its renderer's default right-click menu outside Debug Mode, so the connected WebUI supplies a capability-gated Desktop context menu (`webui/app-shell.md` -> Desktop context menu) instead of enabling the whole debug surface; `desktop/system_actions.py` owns the thread-safe native clipboard and default-browser boundary.

Desktop Voice (`desktop/wakeword/`) and Desktop dictation (`desktop/dictation/`) share the speech core (`desktop/speech/`); the active server transcribes. Live voice (`model_tasks/live.md`) runs in the loaded WebUI page; the Desktop supports it without owning the call.

## Terms

Domain-specific vocabulary for the Desktop accessor.

### Desktop Client
**Definition:** A server-less Desktop install: the pywebview accessor installed alone (`.[cli,desktop]`) with no server stack, no local WebUI build, no data-dir, and no autostart, meant to connect to a *remote* vBot server. Created by `install.ps1 -DesktopClient` / `install.sh --desktop-client`; a Desktop add-on (`-Desktop` / `--desktop`) instead bolts the same accessor onto a full server install.
**Not:** A full install that happens to include the Desktop, and not the running window itself. The Desktop Client is the *install shape* - the absence of the whole server side - not the GUI process.

### Connection screen
**Definition:** The Desktop shell's own native, in-window server-selection/error screen (`desktop/connection.py`, HTML from `desktop/connection_screen.py`): remembered servers, a connect form, inline probe and sign-in errors with the failed target prefilled, the launch waiting state and the pairing offer (`desktop/connection.md` -> Connection screen).
**Not:** A WebUI view or page. It is shell-owned native HTML swapped onto the same window via `Window.load_html`; the WebUI (loaded via `Window.load_url`) is the *other* thing that window shows once connected.

### Desktop Voice
**Definition:** The Desktop's on-device wake phrase listener (`desktop/wakeword/`, "Voice" in Settings and code). A wake phrase is one active wakeword model; its phrase action decides what a detection does.
**Not:** Live voice (the page's `live_voice` Task Model call) or the Chat microphone (browser recording in the composer). Both are WebUI features that also run in browsers.

### Desktop dictation
**Definition:** The Desktop's global speech-to-text shortcut (`desktop/dictation/`, "Dictation" in Settings): it records while the user speaks, has the active server transcribe the recording, and pastes the raw transcript at the text cursor of whatever app has the focus.
**Not:** The Chat microphone (composer recording inside the WebUI) or Terminal dictation; neither leaves the page, and both also run in browsers. Not a Voice command: nothing goes to an Agent.

### Phrase action
**Definition:** What a detected wake phrase does on the current server: `command` (record a spoken command and send it to an Agent and Session behavior, defaulting to the server profile's) or `live_voice` (ask the page to start or toggle Live voice; nothing is recorded). Stored per server profile in `phrase_actions` (`desktop/voice.md` -> Stored settings).

## Interfaces

- `python desktop/main.py [--host] [--port] [--open-session AGENT_ADDRESS SESSION_ID] [--server-data-dir DIR] [--mock-wakeword]` (the entrypoint `vbot desktop` invokes; see `cli/server-lifecycle.md` -> Local accessor actions); the installed Windows Desktop shortcut uses `vBot.GUI.exe desktop` instead, so normal launch uses the GUI subsystem without a console. `--server-data-dir` names the data directory of the installation's own server, whose local access token signs the Desktop in to that server over loopback (`desktop/sign-in.md`); `cli/application/desktop.py` passes it, and `vbot desktop` passes the data directory every `vbot` command uses by default. Launch target and launch wait: `desktop/connection.md`.
- **Open a Session (`--open-session AGENT_ADDRESS SESSION_ID`):** the Agent address (`agent` or `agent@project`) and the Session id ride on the launch's first navigation to the WebUI as the URL-encoded query parameters `open_agent` and `open_session` (after `accessor`/`desktop_session`); the WebUI opens that Session and removes both parameters (`webui/app-shell.md` -> Startup and availability). Only the launch connect carries them (`desktop/connection.md` -> Launch target); a launch that finds a running Desktop hands the request over instead (`desktop/window.md` -> Request handoff).
- `--mock-wakeword` selects the mock mode (no microphone, no network) for UI validation - the only path that simulates Voice activity. Without it, a failed probe of the on-device stack selects the unavailable mode (`voice_stack_unavailable`, never simulating) (`desktop/voice.md` -> Mock and unavailable modes).
- **Update restart (packaged only):** `open_desktop` passes the relaunch contract `VBOT_DESKTOP_RELAUNCH` (`{version_file, version, command}`); without it (source runs) the Desktop never restarts. A packaged Desktop restarts into a newly activated version through a handoff (`desktop/update-restart.md`).
- **Per-user settings store (`desktop/settings.py`):** `<config-dir>/settings.json` in the OS per-user config dir (Windows `%APPDATA%\vbot`; elsewhere `$XDG_CONFIG_HOME/vbot` else `~/.config/vbot`); stored device tokens live beside it in `device-tokens.json`, never in `settings.json` (`desktop/sign-in.md` -> Token store). Each top-level section has one owner module, which uses `read_section(key)` and `update_section(key, mutate)`: `mutate` gets an isolated copy and returns the whole new section, an unchanged section is not rewritten, and a raising `mutate` leaves the file untouched. A file that cannot be read as a JSON object rejects mutations without replacing its bytes, and a failed write re-raises so bridge callers reject instead of faking success (mechanics and section owners: `desktop/window.md` -> Per-user settings store).
- Window startup, single instance, sizing, zoom, DPI, system actions, icons and the Desktop's own logs (`<config-dir>/logs/`, a Desktop Client has no server `data_dir`): `desktop/window.md`.

### Python<->JS bridge

One `DesktopBridge` instance (`desktop/bridge.py`) is pywebview's `js_api` and stays so across navigation, serving **both** the shell connection screen and the remote WebUI (detected via `?accessor=desktop`; `window.pywebview.api.<method>()`, plain serializable results). A bridge method must never replace its calling document before returning - the Promise callback lives in that document - so connection callers await a prepared payload and only then navigate or update error elements via `textContent`. Because pywebview injects `window.pywebview.api` asynchronously, the WebUI keeps capability gates false and retries discovery until a live bridge answers; transient failures are never converted into authoritative disabled capabilities or empty Voice state. Every call runs on its own thread, so implementations must be thread-safe. The facade holds no Voice or dictation state; it delegates to `VoiceController`, `MicrophoneService` and `DictationController`. A method rejects with a stable error code (`[a-z][a-z0-9_]*`) as the `BridgeError` message, which the WebUI reads with `desktopErrorCode`; other failures never pass for codes. The bridge is injected into every page, including any connected server's WebUI, so no bridge method returns a credential or a login ticket, and the methods a page could abuse are gated by `PagePermissions` or the connection screen's key. Method catalog, error contract and page policy: `desktop/bridge.md`.

### Page pushes

`desktop/page_events.py` (`PageEventDispatcher`) is the only path from Python into the loaded page: window events (`vbot-desktop-*`) dispatched through `Window.evaluate_js`. `evaluate_js` blocks until the page answers and deadlocks on the GUI thread, so one daemon thread (`vbot-desktop-page-events`) delivers every push in order; producers never block, and the queue is bounded per kind. Event list and bounds: `desktop/bridge.md` -> Page pushes.

## Conventions

- The loaded UI is the normal WebUI root `/` - no desktop-only frontend build or route exists. Desktop-only controls are capability-gated sections in that shared WebUI; the Connection screen stays shell-rendered native HTML, English-only (the shell has no i18n catalog).
- Desktop inherits the WebUI 1:1 including Projects. There is deliberately **no native folder picker**, because the server can be remote where a local picker would browse the wrong filesystem: server paths are chosen through the WebUI's server-listed picker, `PathField` (`webui.md` -> Ownership).
- Every Desktop HTTP client (probe, Voice, dictation, sign-in) uses `trust_env=False`, so ambient proxy or `.netrc` configuration cannot divert local audio, transcripts, or RPC bodies, and reuses the Desktop-owned verified TLS context (`desktop/_http.py`), preserving httpx's certificate bundle without environment overrides.
- Win32 ctypes bindings (single instance, hotkey, dictation insertion) load their DLLs with `win32_library()` and read errors with `win32_last_error()` from `desktop/_windows.py`: `ctypes.WinDLL` and `ctypes.get_last_error` exist only on Windows, and CI type-checks the Desktop on Linux as well (PROJECT.md -> Context -> Platforms).
- While listening, microphone audio is analyzed continuously on-device; nothing persists or leaves the device before a wake phrase matches, and only the command recording that follows is uploaded. Dictation uploads only the take between its shortcut presses.

## External Dependencies

All ship in the `[desktop]` optional group: pywebview (window and Connection screen host) plus the audio and ML packages of Voice and the speech core (`desktop/voice.md` -> Dependencies). They import lazily, so the backend test gate never needs the GUI/audio stack.

## Constraints & Gotchas

- Desktop-local preferences must never live in the shared server `data_dir` (it belongs to the selected instance); they live in the OS per-user config dir, which survives package/venv reinstalls - a real install puts the program in a non-user-writable venv.
- pywebview is imported lazily so backend tests and non-desktop workflows never require the GUI package. `load_url`/`load_html` run only once the GUI loop is live, and never synchronously inside a `js_api` method invoked by the document being replaced - return the `PreparedConnection` payload first and let that document apply it after its Promise resolves. The native `shown` startup callback is exempt and proceeds through `ConnectionController.launch_connect` / `auto_connect` before optional Voice startup.
- Never call `Window.evaluate_js` from a bridge method or the GUI thread; push through `PageEventDispatcher`. Voice callbacks that reach the page (`live_requests`, the event sink) and `DetectionLoop.on_detection` must return promptly: they run on the capture/detection path.
- Keep Voice imports lazy: with Voice disabled, startup never imports the capture, detection, command, echo, or speech-detection modules or their native packages; they load only when a real listener is built (`VoiceController._build_session`). The fresh-process test in `tests/desktop/test_main.py` fails when disabled Voice loads them.
- Back/Forward inside the window belongs to the WebUI (`webui/app-shell.md` -> Navigation); the shell adds no native history handling.
- Keep clipboard and external-browser access behind `DesktopSystemActions` - never `navigator.clipboard`/`window.open`: the selected server can be remote plain HTTP and renderer behavior differs by platform. The Python boundary repeats HTTP(S)-only validation even though AppShell filters schemes first.
- A new bridge method that acts beyond what a web page may do (reads local data, changes remembered servers or sign-ins, installs something, starts a process) goes through `PagePermissions` or the connection screen's key; every server's page can call it.
- Never hand a login ticket or token to page JavaScript, and never navigate the window because of what the WebUI shows: a page-started connect gets `about:blank` and the controller opens the ticket natively; a lost sign-in is handled once per connect (`desktop/sign-in.md`).
- Every sounddevice/PortAudio query, open, close, and refresh goes through `AUDIO_BACKEND_LOCK` (`desktop/speech/_microphones.py`); a PortAudio refresh invalidates open streams, so it is skipped while any capture stream (Voice or dictation) is open, and the echo stage closes its loopback before a refresh.

## References

Read these only when your task matches - not by default.

- Launch target and wait, probing and its outcomes, the Connection screen, `ConnectionController`, remembered servers, server switches -> `desktop/connection.md`
- Window startup order, single instance and launch request handoff, size, zoom, DPI, native clipboard/browser actions, icons, settings store mechanics, the Desktop's log files -> `desktop/window.md`
- Adding or changing a bridge method, capability, page-policy gate or confirmation, bridge error, or page push -> `desktop/bridge.md`
- Desktop Voice or the speech core (file ownership, pipeline, threads and locks, stored `wakeword` settings, status and events, commands, server client, microphones and capture, echo cancellation, models, calibration, mock mode, audio and ML dependencies) -> `desktop/voice.md`
- Live voice from the Desktop (phrase action, global shortcut, Live requests, the Live hold, WebView2 secure origins, autoplay, microphone permission) -> `desktop/live-voice.md`
- Desktop dictation (settings, shortcut, take flow, cues, paste and fallbacks, status and failure codes, Live hold) -> `desktop/dictation.md`
- Packaged update restart (pending detection, triggers and delays, handoff protocol, restored server, location and placement) -> `desktop/update-restart.md`
- Signing in to an `authenticated` server (pairing, the connection screen's key, device tokens and their file, login ticket, local access token, the WebView profile, 401 handling, lost sign-ins, Forget sign-in) -> `desktop/sign-in.md`
