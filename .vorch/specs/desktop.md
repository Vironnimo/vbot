# Desktop

pywebview-based desktop accessor that embeds the normal WebUI and talks only to the vBot server over HTTP.

## Overview

`desktop/` owns the native window shell around the existing WebUI. It does not
import core/server business logic and it does not manage vBot server processes.
Desktop stays intentionally thin: it loads the same server-served WebUI
that a browser would load from `/`, but inside a pywebview window.

The Desktop now includes a local wakeword voice pipeline (`desktop/wakeword/`)
that runs entirely on-device: openWakeWord detection → sounddevice recording with
webrtcvad silence detection → upload to the vBot speech endpoint → send
transcript as a chat message through server RPC.

## Interfaces

- `python desktop/main.py [--host] [--port] [--mock-wakeword]`
  - resolves the target server URL from CLI args, then Desktop-local settings,
    then defaults `127.0.0.1:8420`
  - persists the resolved host/port to Desktop-local settings
  - probes `GET /health` first and treats HTTP 200 with `{"status":"ok"}` as
    the vBot identity contract
  - probes `/` after health succeeds and opens a pywebview window pointed at
    `http://<host>:<port>/?accessor=desktop` only when the WebUI root returns 2xx/3xx
  - `--mock-wakeword` forces a `MockWakewordEngine` for UI validation without
    a real microphone; when omitted, the real `OpenWakeWordEngine` is used,
    falling back to the mock engine if openWakeWord cannot be imported
  - if the configured host is invalid, the server is unreachable, the target is
    not a vBot server, or a reachable vBot server has no WebUI, shows an escaped
    in-window message instead of crashing
- Desktop-local settings file (`desktop/settings.json`)
  - stores the last-used host, port, and wakeword configuration
  - lives alongside `desktop/main.py`
  - belongs to the Desktop app itself, not the shared server `data_dir`
  - current source-run filename: `desktop/settings.json` (gitignored)
  - malformed or non-object JSON is treated as empty settings and overwritten
    with the next resolved target
  - malformed `wakeword` key (missing or non-dict) falls back to defaults

### Wakeword settings schema

Nested under the `wakeword` key in `desktop/settings.json`:

```json
{
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

- `enabled` — whether the wakeword pipeline starts on Desktop launch
- `engine` — display name of the detection engine (MVP: "openwakeword")
- `microphone` — sounddevice device index or `null` for system default
- `sensitivity` — float 0–1, mapped to score threshold `1.0 - sensitivity`
- `target_agent_id` — agent ID to send transcripts to, or `null` for none
- `session_behavior` — `"active"` uses the latest session; `"new"` creates one
- `wake_phrase` — the wakeword phrase ("hey_jarvis")

### Python↔JS bridge

The Desktop exposes a `DesktopBridge` instance as pywebview's `js_api`. The
WebUI detects Desktop mode via the `?accessor=desktop` query parameter and
calls bridge methods through `window.pywebview.api.<method>()`. All methods
return plain Python objects that pywebview serializes to JSON.

Bridge methods:

| Method | Returns | Description |
|---|---|---|
| `getDesktopCapabilities()` | `{ wakeword: true }` | Feature flags for WebUI gating |
| `getWakewordStatus()` | status dict | Current config + live worker state |
| `setWakewordEnabled(enabled)` | — | Enable/disable the worker |
| `setWakewordConfig(config)` | — | Partial config update, persists, restarts worker |

The WebUI polls `getWakewordStatus()` every 500ms while Desktop is detected
and wakeword is enabled. Worker state transitions are published through the
bridge's `publish_state()` method, which the poll picks up.

Worker states (exposed in `getWakewordStatus().state`):
`off` → `listening` → `wakeword_detected` → `recording` → `transcribing` → `sending` → `listening` (or → `error` at any point).

## Conventions

- Desktop is an accessor only, not a server manager.
- Desktop may connect to localhost or LAN vBot servers over normal HTTP.
- The loaded UI is the normal WebUI root path `/`; no separate desktop-only
  frontend build or route is part of the current contract.
- The Desktop window title is `vBot`.
- A custom `desktop/icon.png` is optional; when absent, pywebview's platform
  default icon is used.
- Closing the window ends only the Desktop process, never the target server.
- The Python↔JS bridge is a pywebview `js_api` object. Bridge methods execute
  in separate threads — implementations must be thread-safe.
- If the server is unreachable or has no WebUI, Desktop stays open and shows an
  in-window message instead of crashing.
- Hosts are plain host names or IP addresses only; schemes, paths, whitespace,
  and URL punctuation are rejected into an in-window invalid-target message.
- Wakeword detection runs locally. Audio is only recorded after the wake phrase
  is detected. No audio leaves the device before a wakeword match.

## External Dependencies

- **pywebview** — native window wrapper used to host the existing WebUI.
- **openwakeword** (optional) — ONNX-based wakeword detection. Falls back to
  mock engine when not installed.
- **sounddevice** (optional) — cross-platform microphone access via PortAudio.
- **webrtcvad** (optional) — Google WebRTC VAD for silence detection during
  post-wakeword recording. Falls back to fixed-duration capture when not
  installed.

## Constraints & Gotchas

- A healthy vBot server may exist without `webui/dist`; in that case Desktop
  must show a user-facing in-window message that the target server has no WebUI.
- Desktop-local preferences must not be written into the shared server
  `data_dir`, because that directory belongs to the selected vBot instance.
- Desktop currently assumes a source-run shell, so settings live beside
  `desktop/main.py` rather than in a later packaging-specific app directory.
- pywebview is imported lazily so backend tests and non-desktop development
  workflows do not require the optional GUI package.
- openWakeWord, sounddevice, and webrtcvad are optional imports — the Desktop
  launches with the mock engine when any of them is missing.
- The wakeword worker runs in a daemon thread. If it crashes silently, the
  bridge state transitions to `error` and the WebUI shows a red indicator.
- Bridge methods must return quickly and not block — they hold a threading.Lock
  for config access only during reads/writes to the local settings file.
