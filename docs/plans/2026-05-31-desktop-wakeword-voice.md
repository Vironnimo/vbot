## Plan: Desktop Wakeword Voice Entry

**Goal:** Add a desktop-only wakeword/voice entry path so users can speak "hey_jarvis" to trigger agent commands hands-free through the vBot Desktop app.

**Context:** The Desktop app (pywebview shell) currently embeds the normal WebUI with no Python↔JS bridge. The WebUI has push-to-talk recording via browser MediaRecorder. The server already exposes `/api/speech/transcribe` (multipart audio upload → transcription) and `chat.send`/`chat.stream` (RPC for chat submission). This plan adds local wakeword detection in the Desktop process and reuses the existing server speech + chat pipeline after wakeword-triggered recording. The Desktop stays an accessor — no direct core/ imports, no provider calls.

**Requirements:**
- Wakeword listening active only while Desktop app is open (no tray/background for MVP)
- Local wakeword detection via openWakeWord (phrase: "hey_jarvis")
- Post-wakeword recording with VAD silence detection (PyAudio + webrtcvad)
- Auto-send transcript immediately after transcription (no confirmation step)
- Configurable target Agent and Session behavior (active/latest or new)
- Settings → Voice panel in WebUI (gated by Desktop capabilities)
- Mic status indicator in Chat area (gray/blue-green pulsing/orange/spinner/red)
- Desktop settings persist in `desktop/settings.json`
- Desktop↔WebUI bridge via pywebview `js_api`

**Scope:**
- In: Desktop wakeword worker, pywebview JS bridge, WebUI Voice settings panel, Chat mic indicator, mock engine for UI validation, openWakeWord integration, PyAudio + webrtcvad pipeline
- Out: Porcupine or LiveKit engines, tray/background behavior, browser WebUI wakeword, top-level Wakeword tab, confirmation step for transcripts, TTS/synthesize integration

**Assumptions & Constraints:**
- openWakeWord is available as a Python package (`openwakeword`) with ONNX inference
- PyAudio can open the system default microphone at 16kHz mono 16-bit PCM
- webrtcvad works on 10/20/30ms frames at 16000Hz sample rate
- pywebview `js_api` bridge methods are called from JS as Promises, executed in separate threads — must be thread-safe
- No new server endpoints needed; existing `/api/speech/transcribe` + `chat.send`/`chat.stream` + `session.list` suffice
- Desktop uses httpx (already a core dependency) for async HTTP calls to the server
- MVP does not need microphone device selection UI (can default to system default, `null` in config)

### Milestones

| # | Milestone | Deliverable |
|---|---|---|
| M1 | Desktop Foundation | Bridge API wired into pywebview, settings schema extended, mock engine validates bridge |
| M2 | WebUI Voice Integration | Settings → Voice panel functional, mic indicator visible, gated by desktop capabilities |
| M3 | Wakeword Pipeline | openWakeWord detects "hey_jarvis", records audio, uploads to speech endpoint, sends transcript |
| M4 | Polish & Docs | Specs updated, tests passing, i18n complete |

### Phase Breakdown

#### Phase 1: Desktop Wakeword Foundation
**Goal of this phase:** Create the Desktop-side infrastructure — settings schema, pywebview JS bridge, wakeword engine abstraction, worker lifecycle, and mock engine.
**Can run in parallel with:** Phase 2 (no file overlap)

- **Task 1A: Extend Desktop settings schema with wakeword config** — files: [desktop/main.py]
  - Add `read_wakeword_settings()` / `write_wakeword_settings()` helpers alongside existing `read_settings()`/`write_settings()`
  - Wakeword config shape in `desktop/settings.json`:
    ```json
    {
      "host": "127.0.0.1",
      "port": 8420,
      "wakeword": {
        "enabled": false,
        "engine": "openwakeword",
        "microphone": null,
        "sensitivity": 0.5,
        "target_agent_id": null,
        "session_behavior": "active",
        "wake_phrase": "hey_jarvis"
      }
    }
    ```
  - `read_wakeword_settings(settings_path)` merges with defaults; malformed wakeword key falls back to defaults
  - `write_wakeword_settings(settings, settings_path)` writes the full dict (host + port + wakeword) via the existing atomic write helper
  - Use `wakeword` key within the existing `settings.json` file — not a separate file

- **Task 1B: Create bridge API class** — files: [desktop/wakeword/__init__.py, desktop/wakeword/bridge.py]
  - `DesktopBridge` class exposed to pywebview via `js_api`:
    - `getDesktopCapabilities()` → `{ wakeword: true }`
    - `getWakewordStatus()` → `{ enabled, state, engine, microphone, sensitivity, target_agent_id, session_behavior, wake_phrase }` where `state` ∈ `{off, listening, wakeword_detected, recording, transcribing, sending, error}`
    - `setWakewordEnabled(enabled: bool)` → starts/stops worker
    - `setWakewordConfig(config: dict)` → partial update of wakeword settings, persists, applies to worker
  - Thread-safe state management: `threading.Lock` for config dict, `threading.Event` for worker control
  - All bridge methods return plain Python objects (serializable to JSON via pywebview)

- **Task 1C: Wire bridge into pywebview + add query param** — files: [desktop/main.py]
  - Add `?accessor=desktop` query param to the URL loaded in pywebview (append to existing `target.url`)
  - Pass `DesktopBridge` instance as `js_api` to `webview.create_window()`
  - Worker lifecycle: start worker if `wakeword.enabled` on window launch; stop worker on window close
  - Bridge instantiation happens before `webview.create_window()` so the API is ready when JS calls it

- **Task 1D: Implement mock/no-op wakeword backend** — files: [desktop/wakeword/engine.py]
  - `WakewordEngine` abstract interface with `start()`, `stop()`, `detect(audio_chunk) -> float` (score 0–1)
  - `MockWakewordEngine`: returns configurable scores for UI testing, no real mic/detection
  - Engine selection: use mock when `--mock-wakeword` CLI flag is set or when openWakeWord import fails
  - Mock publishes fake state transitions through the bridge for UI validation

**Dependencies:** None (foundation phase)
**Done when:** Bridge API returns correct capabilities/status from Desktop, mock engine allows UI validation, settings persist correctly

---

#### Phase 2: WebUI Voice Integration ⚡
**Goal of this phase:** Add the Settings → Voice panel and Chat mic status indicator to the WebUI, gated by Desktop capability detection.
**Can run in parallel with:** Phase 1 (no file overlap — Phase 1 is desktop/**, Phase 2 is webui/**)

- **Task 2A: Desktop capability detection + bridge client** — files: [webui/src/lib/desktopBridge.js, webui/src/App.svelte]
  - `desktopBridge.js`:
    - `isDesktop()` — checks `window.location.search` for `accessor=desktop` AND `window.pywebview?.api` exists
    - `hasWakeword()` — calls `window.pywebview.api.getDesktopCapabilities()` (returns Promise), caches result
    - `getWakewordStatus()` — wraps bridge call with error handling (bridge absent → returns disabled)
    - `setWakewordEnabled(enabled)` / `setWakewordConfig(config)` — wraps bridge calls
    - `onWakewordStatusChange(callback)` — poll-based subscription (pywebview bridge is pull-only from JS side, or use `window.pywebview.api` events if available; default: 500ms poll when Desktop is detected and wakeword is enabled)
  - `App.svelte`:
    - Import `isDesktop`, `hasWakeword` from `desktopBridge.js`
    - On mount, detect desktop capabilities and store in app-level state
    - Pass `desktopCapabilities` to children via props (or a shared store)
    - Pass `wakewordStatus` and bridge methods to ChatView and SettingsView

- **Task 2B: Voice settings state management** — files: [webui/src/lib/wakewordSettings.js]
  - `createVoiceSettingsState()` → initial state with defaults
  - `applyWakewordStatus(state, status)` — hydrates from bridge status
  - `buildVoiceSettingsPayload(state)` — builds config for `setWakewordConfig()`
  - `voiceSettingsDirty(state, lastSaved)` — dirty check helper
  - Handles: enabled, engine display, microphone, sensitivity 0–1, target_agent_id, session_behavior (active/new), wake_phrase

- **Task 2C: Settings → Voice panel component** — files: [webui/src/components/WakewordVoiceSettings.svelte, webui/src/components/SettingsView.svelte]
  - `WakewordVoiceSettings.svelte`:
    - Gated on `desktopCapabilities?.wakeword` prop — renders nothing when absent
    - Enable/disable toggle (calls `setWakewordEnabled`)
    - Engine display (read-only text for MVP — only "openWakeWord" available)
    - Microphone display (read-only for MVP — "System default" when null, device name string otherwise)
    - Target Agent dropdown (populated from shared agents list)
    - Session behavior radio/select: "Active/latest session" vs "New session each time"
    - Sensitivity slider (0 to 1, step 0.05, labeled: "Less sensitive" / "More sensitive")
    - Wake phrase display (read-only: "hey_jarvis")
    - Live state display: off / listening / recording / transcribing / sending / error (text + color dot)
    - Privacy note paragraph (static text)
    - Auto-save behavior: save on change via debounced `setWakewordConfig()` (800ms), NOT through server RPC
    - Save feedback via existing toast system
  - `SettingsView.svelte`:
    - Add `{ id: 'voice', labelKey: 'settings.voice.title', labelFallback: 'Voice', ... }` to panels array
    - Gate visibility: only include in panels when `desktopCapabilities?.wakeword` prop is true
    - Import and render `WakewordVoiceSettings` when `activePanelId === 'voice'`
    - Follow existing panel patterns (auto-save, sticky footer, toast feedback)

- **Task 2D: Mic status indicator in Chat** — files: [webui/src/components/AppShell.svelte, webui/src/components/ChatView.svelte]
  - Indicator design: small (8-10px) colored dot, no text label, tooltip on hover
  - Placement: near the agent bar area in ChatView, or in the AppShell sidebar footer
    - Preferred: ChatView toolbar area, inline with existing agent info / token badge
    - Click: navigates to Settings → Voice (`selectPanel('voice')`)
  - States (from `wakewordStatus.state`):
    | State | Color | Animation |
    |---|---|---|
    | `off` / disabled | gray (`--text-lo` / `#5e4c38`) | none |
    | `listening` | green (`--green` / `#4ade80`) | gentle pulse (CSS animation) |
    | `recording` | orange (`--amber` / `#f59e0b`) | none (solid) |
    | `transcribing` / `sending` | accent (`--accent` / `#e8870a`) | spinner (CSS animation) |
    | `error` | red (`--red` / `#fc8181`) | none |
  - Tooltip text matches state (i18n)
  - Only visible when `desktopCapabilities?.wakeword` is true
  - Receives status updates from `desktopBridge.onWakewordStatusChange()`

- **Task 2E: i18n strings** — files: [webui/src/lib/i18n.js]
  - New keys needed (all with English fallbacks):
    - `settings.voice.title` → "Voice"
    - `settings.voice.subtitle` → "Wakeword detection and voice command settings."
    - `settings.voice.enabled` → "Wakeword listening"
    - `settings.voice.engine` → "Engine"
    - `settings.voice.microphone` → "Microphone"
    - `settings.voice.sensitivity` → "Sensitivity"
    - `settings.voice.targetAgent` → "Target Agent"
    - `settings.voice.sessionBehavior` → "Session"
    - `settings.voice.sessionBehaviorActive` → "Use active session"
    - `settings.voice.sessionBehaviorNew` → "New session each time"
    - `settings.voice.wakePhrase` → "Wake phrase"
    - `settings.voice.state` → "Status"
    - `settings.voice.privacyNote` → "Wakeword detection runs locally on your device. Audio is only recorded after the wake phrase is detected. Transcription uses your configured vBot speech backend."
    - `settings.voice.saveSuccess` → "Voice settings updated."
    - `settings.voice.systemDefaultMic` → "System default"
    - `voice.state.off` → "Disabled"
    - `voice.state.listening` → "Listening"
    - `voice.state.wakewordDetected` → "Wakeword detected"
    - `voice.state.recording` → "Recording"
    - `voice.state.transcribing` → "Transcribing"
    - `voice.state.sending` → "Sending"
    - `voice.state.error` → "Error"

**Dependencies:** Phase 1 (bridge API contract must exist so the WebUI knows the method names and return shapes)
**Done when:** Voice panel renders in Settings when Desktop is detected, mic indicator shows correct state/color, all strings use i18n, panel saves config through bridge

---

#### Phase 3: Wakeword Pipeline
**Goal of this phase:** Implement the real wakeword detection → recording → upload → send pipeline using openWakeWord, PyAudio, and webrtcvad.

- **Task 3A: openWakeWord engine wrapper** — files: [desktop/wakeword/engine.py]
  - `OpenWakeWordEngine` class implementing the `WakewordEngine` interface:
    - `__init__(wake_phrase, sensitivity)`: loads `openwakeword.model.Model` with the configured wake phrase model, sets inference framework to `onnx`
    - `start()`: initializes PyAudio stream (16kHz, 16-bit, mono, input) in a background thread
    - `stop()`: closes PyAudio stream, stops thread
    - `detect(audio_chunk) -> float`: calls `model.predict(audio_chunk)`, returns score for the configured wake phrase (clamped 0–1)
    - Audio chunk size: 1280 samples (80ms at 16kHz) — openWakeWord's expected frame size
  - PyAudio stream reading: read chunks of 1280 samples (2560 bytes for 16-bit mono)
  - Convert bytes to numpy array (int16) for openWakeWord
  - Sensitivity → threshold: `threshold = 1.0 - sensitivity` (higher sensitivity = lower threshold)

- **Task 3B: Wakeword worker thread** — files: [desktop/wakeword/worker.py, desktop/main.py]
  - `WakewordWorker` class:
    - Owns the wakeword engine instance
    - Runs in a `threading.Thread` (daemon)
    - State machine: `off → listening → wakeword_detected → recording → transcribing → sending → listening` (or → error at any point)
    - **Listening phase**: continuous PyAudio read → engine.detect() → check score > threshold
    - **Recording phase**: on detection, start collecting audio frames; use webrtcvad to detect silence
      - VAD config: mode 1 (moderate aggressiveness), 30ms frames at 16kHz
      - Silence detection: 1.5 seconds of consecutive non-speech frames ends recording
      - Max recording duration: 15 seconds (safety cap)
    - **Transcribing phase**: POST audio bytes to `{server_url}/api/speech/transcribe` via httpx (multipart upload)
    - **Sending phase**: call `chat.stream` RPC via httpx with `{ agent_id, session_id, content: transcript }`
      - Session resolution for "active" behavior: call `session.list` RPC first, pick the first session (latest)
      - Session resolution for "new" behavior: call `session.create` RPC with `make_current: true`
    - **Error handling**: transient HTTP errors (network, 429, 502, 503) → retry up to 3 times with backoff; other errors → set state to `error`, log warning
    - Publish state to bridge after every transition
  - Wire worker lifecycle into `desktop/main.py`:
    - Create worker on Desktop launch if `wakeword.enabled` is true
    - Stop worker on window close or when bridge `setWakewordEnabled(false)` is called
    - `setWakewordEnabled(true)` recreates worker with current config

- **Task 3C: Audio pipeline helpers** — files: [desktop/wakeword/worker.py]
  - `list_microphones()`: enumerate PyAudio input devices (for future UI use; MVP uses default)
  - `open_microphone_stream(device_index)`: PyAudio stream factory with standard params
  - `record_until_silence(stream, vad, max_seconds)`: collect frames, run VAD on each, return full audio bytes
  - `upload_audio(audio_bytes, server_url)`: httpx async POST to speech endpoint
  - `send_transcript(transcript, server_url, agent_id, session_id)`: httpx async POST to RPC endpoint
  - Audio format for transcription: WebM/opus or WAV — determine based on what the speech endpoint accepts; likely WAV (16-bit PCM) since that's what we capture

**Dependencies:** Phase 1 (bridge + settings infrastructure)
**Done when:** Speaking "hey_jarvis" triggers recording, audio is transcribed via the configured server speech backend, transcript appears as a chat message in the target Session

---

#### Phase 4: Integration Verification, Tests & Docs
**Goal of this phase:** Verify end-to-end flow, add focused tests, update project documentation.

- **Task 4A: Backend tests** — files: [tests/desktop/]
  - `tests/desktop/test_wakeword_settings.py` — settings read/write/merge with defaults, malformed JSON fallback
  - `tests/desktop/test_bridge.py` — bridge methods return correct shapes, thread safety of concurrent access
  - `tests/desktop/test_engine_mock.py` — mock engine returns expected scores, state transitions work
  - `tests/desktop/test_worker.py` — worker state machine transitions, VAD silence detection logic (with synthetic audio frames)
  - Test with `--mock-wakeword` flag patterns

- **Task 4B: Frontend tests** — files: [webui/src/lib/__tests__/, webui/src/components/__tests__/]
  - `desktopBridge.test.js` — `isDesktop()` detection, bridge method stubs, error handling when bridge absent
  - `wakewordSettings.test.js` — state creation, dirty checking, payload building
  - `WakewordVoiceSettings.test.js` — panel rendering when gated/ungated, control interactions
  - Mic indicator rendering test in ChatView context

- **Task 4C: Update specs** — files: [.vorch/specs/desktop.md, .vorch/specs/webui.md, .vorch/PROJECT.md]
  - `.vorch/specs/desktop.md`:
    - Document the new Python↔JS bridge contract (replaces "No Python↔JavaScript bridge is part of the current contract.")
    - Document Desktop-local wakeword settings schema
    - Document wakeword worker lifecycle
    - Add external dependencies: openWakeWord, PyAudio, webrtcvad
  - `.vorch/specs/webui.md`:
    - Document `desktopBridge.js` interface
    - Document `WakewordVoiceSettings` component
    - Document mic status indicator states
    - Add Settings → Voice panel to panel list
  - `.vorch/PROJECT.md`:
    - Add wakeword engine to External Dependencies (Desktop group)
    - Update Specs table if needed
    - Note the new bridge capability in Context section

- **Task 4D: Update dependency manifests** — files: [pyproject.toml]
  - Add to `desktop` optional dependency group: `openwakeword`, `pyaudio`, `webrtcvad`
  - These remain optional — Desktop runs fine without them (uses mock engine)

**Dependencies:** Phases 1, 2, 3 complete
**Done when:** All tests pass with `python scripts/quality.py` and `python scripts/quality-frontend.py`, specs accurately describe the new behavior

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| openWakeWord model loading fails on some platforms | Med | High | Graceful fallback to mock engine; clear error message in bridge status |
| PyAudio device enumeration inconsistent across OS | Med | Med | Use system default mic for MVP; document device selection as future enhancement |
| pywebview JS bridge threading issues (calls from JS block worker) | Med | Med | Keep bridge methods fast and lock-free for reads; use Events for async signaling |
| webrtcvad frame size constraints too rigid for real-world audio | Low | Med | Test with actual microphone input early; adjust frame size if needed |
| Audio format mismatch between PyAudio recording and speech endpoint | Low | Low | Verify speech endpoint accepts WAV; convert if needed (standard audio processing) |
| Desktop worker crashes silently (daemon thread) | Med | High | Log errors to Desktop console; publish error state to bridge so UI shows red indicator |

### New Dependencies

- `openwakeword` — local wakeword detection engine (ONNX-based, "hey_jarvis" model included)
- `pyaudio` — cross-platform microphone access via PortAudio
- `webrtcvad` — Google WebRTC Voice Activity Detection for silence detection
